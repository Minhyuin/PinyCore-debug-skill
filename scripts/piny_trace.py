#!/usr/bin/env python3
"""RTT capture and native SEGGER SystemView CSV analysis for PinyCore."""
from __future__ import annotations
import argparse
import csv
import json
import math
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from piny_debug import ROOT, HIDDEN, __version__, ProbeLock, OpenOCD, GDB, elf_identity, free_port, load_profile, restore_target, stop_process, tcl_word, tool_path
import piny_view


def number(text):
    text=re.sub(r'\s+','',str(text))
    match=re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:%|s|ms|us|ns)?',text)
    if not match:raise ValueError('Invalid numeric CSV field: '+text)
    value=float(match[1])
    if not math.isfinite(value):raise ValueError('Nonfinite CSV value')
    return value


def validate_seconds(value):
    if not math.isfinite(value) or not .2<=value<=300:raise ValueError('Capture duration must be 0.2..300 seconds')
    return value


def summarize(contexts_path,events_path):
    contexts=[]
    with Path(contexts_path).open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not {'Name','CPU Load','Type'}.issubset(reader.fieldnames or []):raise ValueError('Unsupported SystemView context CSV schema (English headers required)')
        for row in reader:
            load=number(row['CPU Load'])
            if not 0<=load<=100:raise ValueError('CPU Load outside 0..100')
            contexts.append({'name':row['Name'],'type':row['Type'],'core':row.get('Core','Core 0'),
                             'stack':row.get('Stack Information',''),'cpu_percent':load,
                             'total_run_time':row.get('Total Run Time',''),'activations':row.get('Activations','')})
    count=0;overflow=0;starts=0;stops=0;first=None;last=None;backwards=False;frequency=None;descriptions=[]
    with Path(events_path).open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not {'timestamp','event','eventint'}.issubset(reader.fieldnames or []):raise ValueError('Unsupported SystemView events CSV schema')
        for row in reader:
            stamp=number(row['timestamp']);event=int(row['eventint'],0);count+=1
            if first is None:first=stamp
            if last is not None and stamp<last:backwards=True
            last=stamp
            overflow+=event==1;starts+=event==10;stops+=event==11
            if event==24:
                match=re.search(r'Cycle Freq\.:\s*(\d+)',row.get('detail',''))
                if match:frequency=int(match[1])
            if event==14:descriptions.append(row.get('detail',''))
    duration=(last-first) if count else 0
    cores=sorted({r['core'] for r in contexts});busy={};issues=[];notes=[];sums={}
    if not contexts:issues.append('no_contexts')
    for core in cores:
        rows=[r for r in contexts if r['core']==core]
        idle=[r for r in rows if r['name'].casefold()=='idle']
        if len(idle)!=1:busy[core]=None;issues.append('missing_or_ambiguous_idle:'+core)
        else:busy[core]=round(100-idle[0]['cpu_percent'],4)
        # SystemView's CPU Load column is Total Run Time / recording span, so a complete
        # export sums to 100 % per core. This sum is the strongest available check on the
        # measurement: measured on this board at 4000 kHz, dropped events left it at 65.4 %
        # and Idle understated as 48.5 % (busy 51.5 %, wrong), while at 24000 kHz it is
        # 99.99 % and Idle 76.09 % (busy 23.91 %, reproducible at 12000 kHz too).
        sums[core]=round(sum(r['cpu_percent'] for r in rows),4)
        if abs(sums[core]-100)>1:issues.append('context_load_sum_not_100:'+core)
    if overflow:issues.append('trace_overflow')
    if duration<=0 or count<3:issues.append('insufficient_trace')
    if backwards:issues.append('nonmonotonic_timestamps')
    # A recording that is ended by halting the target has no Stop event, and SystemView
    # still writes complete native CPU-load exports for it. That is a documented property
    # of the recording, not a defect in it, so it is reported without invalidating the
    # measurement. Anything that would actually corrupt the numbers stays blocking.
    if starts!=1:notes.append('missing_or_multiple_start_events')
    if stops!=1:notes.append('missing_stop_event: recording did not end with a SystemView Stop event')
    if starts>1:notes.append('duplicate_start_events: device restarted during the recording')
    return {'trace_busy_percent':busy[cores[0]] if len(cores)==1 else None,'busy_by_core':busy,
            'recorded_seconds':duration,'event_count':count,'cycle_frequency_hz':frequency,
            'system_descriptions':descriptions,'contexts':contexts,
            'quality':{'usable':not issues,'issues':issues,'notes':notes,'overflow_events':overflow,
                       'start_events':starts,'stop_events':stops,'context_load_sum':sums},
            'interpretation':'Busy = 100 - native SystemView Idle CPU Load over the entire recording; uninstrumented ISR time may be misattributed. Not a complete hardware utilization measurement.'}


#: A healthy native export finishes in ~14 s. If the CSVs stop growing while SystemView is
#: still alive, that launch has hung and is killed instead of burning the whole timeout:
#: measured on the bundled SEGGER sample, roughly one launch in three stops after writing
#: a partial contexts.csv and never proceeds.
EXPORT_STALL_SECONDS = 25.0
#: A run that exports nothing at all by this point has stalled before its first write.
EXPORT_QUIET_SECONDS = 20.0
#: SystemView creates its main window only once the license dialog has been answered, so
#: waiting for it tells an accepted dialog apart from a wedged launch. A healthy launch
#: reaches the window well inside the time one export takes.
MAIN_WINDOW_SECONDS = 12.0
#: SystemView answers nothing while it writes a recording out. Measured on this board: a
#: 21 s live recording became a 3489556 byte .svdat about 21 s after -save. A shorter wait
#: declared a good recording missing.
SAVE_WRITE_SECONDS = 45.0


def validate_export(contexts_path,events_path):
    """Check that a SystemView CSV export is complete. Returns None or a reason string.

    SystemView writes its exports incrementally while the GUI is still up, so a run that
    is abandoned or killed half way leaves a file that exists, is non-empty and parses -
    but carries incomplete rows (a missing Stop event makes "Last Run Time" overflow to
    the 2**63 ns sentinel, which turns into CPU loads far above 100 %). File existence is
    therefore not completion; this is the cheap check that can tell the difference.
    """
    contexts_path=Path(contexts_path);events_path=Path(events_path)
    for path in (contexts_path,events_path):
        if not path.is_file() or path.stat().st_size==0:return 'missing or empty '+path.name
    try:
        with contexts_path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            if not {'Name','CPU Load'}.issubset(reader.fieldnames or []):
                return 'unsupported contexts CSV schema'
            rows=0
            for row in reader:
                load=number(row['CPU Load'])
                if not 0<=load<=100:return 'CPU Load outside 0..100 in contexts CSV'
                rows+=1
    except (ValueError,UnicodeDecodeError) as exc:
        return 'incomplete contexts CSV: '+str(exc)
    if not rows:return 'empty contexts CSV'
    try:
        with events_path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            if not {'timestamp','event','eventint'}.issubset(reader.fieldnames or []):
                return 'unsupported events CSV schema'
            events=sum(1 for _ in reader)
    except (ValueError,UnicodeDecodeError) as exc:
        return 'incomplete events CSV: '+str(exc)
    if not events:return 'events CSV holds no events'
    return None


def systemview_export(executable,recording,out,auto_accept_license=True,timeout=90,recover_existing=False):
    """Convert a SystemView recording into native CSV exports.

    SystemView V3.60e gates startup behind a modal non-commercial license dialog and
    does not parse its command line until that dialog is answered; the observed
    successful window is ~13 s, while a launch nobody answers hangs indefinitely. So the
    process has to be supervised instead of called with subprocess.run, and its exit code
    is not believed on its own - the CSVs are re-read and validated, and a run that
    exports incompletely is retried.
    """
    out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True);recording=Path(recording).resolve()
    if not recording.is_file() or recording.stat().st_size==0:raise ValueError('Missing or empty recording')
    contexts=out/'contexts.csv';events=out/'events.csv'
    if contexts.exists() or events.exists():
        reason=validate_export(contexts,events)
        if reason is None and recover_existing:
            return contexts,events,{'exit_code':None,'elapsed_seconds':0.0,'license':None,
                                    'command':None,'attempts':[],'export_attempts':0,
                                    'recovered_existing_export':True}
        if reason is None:
            raise FileExistsError('Complete SystemView export already present; pass '
                                  '--recover-existing to reuse it instead of refusing to touch it')
        if not recover_existing:
            raise FileExistsError('Refusing to reuse stale SystemView export files ('
                                  +reason+'); use a new output directory')
    logs=[]
    deadline=time.monotonic()+timeout
    for attempt in range(1,4):
        if attempt>1 and time.monotonic()>=deadline:break
        for path in (contexts,events):
            if path.exists():path.unlink()
        argv=[str(executable),'-single','-port',str(free_port()),'-load',str(recording),
              '-export-contexts',str(contexts),'-export',str(events),'-quit']
        (out/'systemview-command.json').write_text(json.dumps(argv,indent=2),encoding='utf-8')
        begun=time.time()
        log=(out/'systemview.log').open('w' if attempt==1 else 'a',encoding='utf-8')
        log.write('argv='+json.dumps(argv)+'\n');log.write('attempt='+str(attempt)+'\n')
        proc=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace')
        license_report=None;exit_code=None;reason='export never became complete'
        stalled=False;last_stamp=None;grew_at=time.monotonic()
        try:
            license_report=piny_view.accept_license(proc.pid,timeout=max(5,min(30,deadline-time.monotonic())),
                                                    auto_accept=auto_accept_license,
                                                    main_window_timeout=MAIN_WINDOW_SECONDS)
            log.write('license='+json.dumps(license_report)+'\n');log.flush()
            def size_stamp():
                total=0
                for path in (contexts,events):
                    if not path.is_file():return None
                    total+=path.stat().st_size
                    if path.stat().st_mtime<begun-1:return None
                return total
            while time.monotonic()<deadline:
                stamp=size_stamp()
                if stamp is None:
                    # Still importing: nothing written yet. Only give up if that state
                    # outlives a whole healthy run.
                    if time.monotonic()-begun>EXPORT_QUIET_SECONDS:
                        stalled=True;reason='SystemView wrote no export within %gs'%EXPORT_QUIET_SECONDS
                        break
                else:
                    if stamp!=last_stamp:
                        last_stamp=stamp;grew_at=time.monotonic()
                    elif time.monotonic()-grew_at>=EXPORT_STALL_SECONDS:
                        reason=validate_export(contexts,events)
                        if reason is None:break
                        if proc.poll() is None:
                            # The export stopped growing while SystemView is still alive.
                            stalled=True
                            reason='SystemView stalled after writing %s: %s'%(stamp,reason)
                            break
                        # The process is gone and what it left behind is incomplete.
                        break
                if proc.poll() is not None and stamp is not None:
                    reason=validate_export(contexts,events)
                    if reason is None:break
                time.sleep(.2)
            else:
                reason=reason or 'export never became complete'
        finally:
            if proc.poll() is None:
                stop_process(proc)
                license_report=(license_report or {})
                license_report['process_killed']=True
            exit_code=proc.poll()
            elapsed=round(time.time()-begun,2)
            log.write(f'exit_code={exit_code}\nelapsed_seconds={elapsed}\nstalled={stalled}\nvalidation={reason}\n')
            log.close()
        logs.append({'exit_code':exit_code,'elapsed_seconds':elapsed,'attempt':attempt,
                     'license':license_report,'command':argv,'validation':reason,'stalled':stalled})
        if reason is None:break
        if time.monotonic()>=deadline:break
    if reason is not None:
        detail=' last export invalid: '+reason+'.' if reason else ''
        raise TimeoutError('SystemView did not produce complete CSV exports within %ss.%s See %s'
                           %(timeout,detail,out/'systemview.log'))
    last=logs[-1]
    return contexts,events,{'exit_code':last['exit_code'],'elapsed_seconds':last['elapsed_seconds'],
                            'license':last['license'],'command':last['command'],
                            'attempts':logs,'export_attempts':len(logs)}


def analyze(executable,recording,out,auto_accept_license=True,timeout=90,recover_existing=False):
    contexts,events,export=systemview_export(executable,recording,out,auto_accept_license,timeout,
                                             recover_existing)
    summary=summarize(contexts,events)
    summary['recording']=str(Path(recording).resolve())
    summary['data_origin']='imported_recording_not_live_target'
    summary['systemview_export']=export
    (Path(out)/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary


#: SystemView writes its settings back on exit, so a recording run saves the user's file
#: and restores it afterwards.
SYSTEMVIEW_INI = Path.home()/'AppData'/'Roaming'/'SEGGER'/'SEGGER SystemView.ini'
#: The running SystemView instance accepts command lines on this local port.
SYSTEMVIEW_CONTROL_PORT = 19050
#: Measured on the first board recording: launch to listening instance takes ~7 s.
SYSTEMVIEW_SOCKET_SECONDS = 60.0


def _systemview_command(command,port=SYSTEMVIEW_CONTROL_PORT,timeout=5.0,reply_timeout=8.0):
    """Send one command line to a running SystemView instance and return its reply.

    Each command goes on its own connection: the instance answers with a one line banner
    and closes. A refused connection means no instance is listening (yet).
    """
    with socket.create_connection(('127.0.0.1',port),timeout=timeout) as sock:
        sock.sendall(command.encode('utf-8'))
        sock.settimeout(reply_timeout);chunks=[]
        try:
            while True:
                data=sock.recv(4096)
                if not data:break
                chunks.append(data)
        except socket.timeout:pass
        return b''.join(chunks).decode('utf-8','replace')


def _patch_ini(text):
    """Disable SystemView's auto-load so a stale recording cannot be mistaken for a capture."""
    lines=[];seen=False
    for line in text.splitlines():
        if line.startswith('LoadDataOnStart='):
            lines.append('LoadDataOnStart=false');seen=True
        else:lines.append(line)
    if not seen:lines.append('LoadDataOnStart=false')
    return '\n'.join(lines)+'\n'


def _watch_message_boxes(process_id, stop, seen, interval=0.5):
    """Close SystemView's modal boxes until `stop` is set, recording each one.

    They are informational ('43466 Events Loaded') or error boxes raised by the import and
    the recorder. While one is open the control socket answers nothing, so the save at the
    end of a recording would otherwise be sent into a blocked event loop and the recording
    would be lost. Runs in a daemon thread; `seen` collects the boxes for the log.
    """
    while not stop.is_set():
        try:
            box = piny_view.dismiss_message_box(process_id)
        except Exception as exc:                 # never let the watcher kill a recording
            seen.append({'title': 'watcher-error', 'text': [str(exc)]})
            return
        if box is not None:
            box['at'] = round(time.time(), 2)
            seen.append(box)
        stop.wait(interval)


def systemview_record(executable,out,seconds,device=None,probe=None,
                      auto_accept_license=True,timeout=240.0,speed_khz=4000,
                      control_port=SYSTEMVIEW_CONTROL_PORT):
    """Record a live SystemView trace from the target through a J-Link probe.

    SystemView cannot start a recording from its command line: `-start`, `-stop` and
    `-save` are command line *control* options that only a running instance executes, so
    this launches the recorder with the connection parameters, answers the mandatory
    `No Commercial-Use License` dialog, and then drives the recording over the local
    control socket. The control block is left to the recorder's own RAM search rather
    than pinned with -rttcbaddr, so this does not depend on one firmware build's layout.

    The `LoadDataOnStart` setting is turned off for the run: with it on, SystemView loads
    its most recent recording at startup, and a failed capture then leaves files that are
    byte identical to that older recording - which looks exactly like success.
    """
    validate_seconds(seconds)
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    # ABSOLUTE, always. SystemView does not resolve -save against this process's working
    # directory but against its own, so a relative path lands in (or, when that directory
    # does not exist there, silently fails to land in) the SEGGER preferences folder:
    # measured, `-save artifacts\board-run2\live.svdat` produced 0 bytes and a
    # 'SystemView Error' box 2 s later, while the identical run with an absolute path
    # produced 2296373 bytes. That was the whole reason record() kept failing.
    recording=(out/'live.svdat').resolve()
    for stale in (recording,out/'contexts.csv',out/'events.csv'):
        if stale.exists():stale.unlink()
    command=[str(executable),'-recorder','J-Link','-device',device or 'STM32H723VG',
             '-if','SWD','-speed',str(speed_khz)]
    log_path=out/'systemview-record.log'
    with log_path.open('w',encoding='utf-8') as log:
        log.write('command='+json.dumps(command)+'\nprobe='+str(probe or 'jlink')+'\n')
    backup=SYSTEMVIEW_INI.with_suffix('.ini.pinycore-record-backup')
    restored=False
    proc=None
    steps=[]
    license_report=None
    begun=time.monotonic()
    console=out/'systemview-record-console.log'
    try:
        if SYSTEMVIEW_INI.is_file():
            backup.write_text(SYSTEMVIEW_INI.read_text(encoding='utf-8',errors='replace'),encoding='utf-8')
            SYSTEMVIEW_INI.write_text(_patch_ini(backup.read_text(encoding='utf-8',errors='replace')),encoding='utf-8')
        with log_path.open('a',encoding='utf-8') as log:
            log.write('launched_at='+str(time.time())+'\n')
        # The recorder's own output is kept: when it exits before answering the control
        # socket, that console is the only place the reason appears.
        with console.open('wb') as stream:
            proc=subprocess.Popen(command,cwd=str(out),stdin=subprocess.DEVNULL,
                                  stdout=stream,stderr=subprocess.STDOUT,creationflags=HIDDEN)
            license_report=piny_view.accept_license(proc.pid,timeout=min(30.0,timeout),
                                                   main_window_timeout=MAIN_WINDOW_SECONDS) \
                if auto_accept_license else {'note':'auto-accept disabled','auto_accept':False}
            steps.append({'at':round(time.monotonic()-begun,2),'step':'license','report':license_report})
            deadline=time.monotonic()+min(SYSTEMVIEW_SOCKET_SECONDS,timeout)
            while time.monotonic()<deadline:
                if proc.poll() is not None:break
                try:_systemview_command('-port',control_port,timeout=1.0,reply_timeout=1.0)
                except OSError:time.sleep(.5)
                else:break
            else:raise TimeoutError('SystemView did not start its control server within %gs'%SYSTEMVIEW_SOCKET_SECONDS)
            if proc.poll() is not None:
                raise RuntimeError('SystemView exited with code %s before its control server answered; '
                                   'see %s'%(proc.poll(),console))
            steps.append({'at':round(time.monotonic()-begun,2),'step':'listening'})
            boxes=[];stop_watch=threading.Event()
            watcher=threading.Thread(target=_watch_message_boxes,args=(proc.pid,stop_watch,boxes),daemon=True)
            watcher.start()
            try:
                steps.append({'at':round(time.monotonic()-begun,2),'step':'-start',
                              'reply':_systemview_command('-start',control_port)})
                time.sleep(seconds)
                steps.append({'at':round(time.monotonic()-begun,2),'step':'-stop',
                              'reply':_systemview_command('-stop',control_port)})
                # The path must NOT be quoted: SystemView V3.60e writes nothing at all when
                # the filename arrives wrapped in double quotes, and reports no error for it.
                save_command='-save '+str(recording)
                for attempt in range(1,4):
                    reply=_systemview_command(save_command,control_port,reply_timeout=30.0)
                    steps.append({'at':round(time.monotonic()-begun,2),'step':save_command,
                                  'attempt':attempt,'reply':reply})
                    # Writing the recording is not instant: measured at ~21 s for a 21 s
                    # recording (3489556 bytes) on this board, and SystemView says nothing
                    # while it does it. Waiting only ~10 s here once turned a successful
                    # save into a false 'no recording' failure.
                    waited=0.0
                    while waited<SAVE_WRITE_SECONDS and not (recording.is_file() and recording.stat().st_size):
                        time.sleep(.5);waited+=.5
                    if recording.is_file() and recording.stat().st_size:break
            finally:
                stop_watch.set();watcher.join(timeout=5.0)
                steps.append({'at':round(time.monotonic()-begun,2),'step':'message-boxes',
                              'count':len(boxes),'boxes':boxes})
    finally:
        if proc is not None and proc.poll() is None:
            try:_systemview_command('-quit',control_port,timeout=2.0,reply_timeout=2.0)
            except OSError:pass
            deadline=time.monotonic()+10.0
            while time.monotonic()<deadline and proc.poll() is None:time.sleep(.2)
        if proc is not None and proc.poll() is None:stop_process(proc)
        if backup.is_file():
            try:shutil.copyfile(backup,SYSTEMVIEW_INI);restored=True
            except OSError:restored=False
        # Written here rather than on the success path: a run that dies half way still has
        # to say how far it got.
        with log_path.open('a',encoding='utf-8') as log:
            log.write('exit_code=%s\nelapsed_seconds=%s\nini_restored=%s\n'
                      %(proc.poll() if proc is not None else None,round(time.monotonic()-begun,2),restored))
            for step in steps:log.write('step='+json.dumps(step,ensure_ascii=False,default=str)+'\n')
            if recording.is_file():log.write('bytes=%s\n'%recording.stat().st_size)
    if not recording.is_file() or recording.stat().st_size==0:
        raise RuntimeError('SystemView produced no recording at %s after %.0fs; see %s'
                           %(recording,seconds,log_path))
    return recording,{'bytes':recording.stat().st_size,'seconds':seconds,
                      'probe':probe or 'jlink','device':device or 'STM32H723VG',
                      'speed_khz':speed_khz,'license':license_report,'steps':steps,
                      'ini_restored':restored,'command':command,'log':str(log_path),
                      'console_log':str(console),
                      'message_boxes':[s for s in steps if s.get('step')=='message-boxes']}


def record(p,out,seconds,auto_accept_license=True,timeout=240.0,device=None,
           control_port=SYSTEMVIEW_CONTROL_PORT,allow_overflow=False,speed_khz=None):
    """Record from the board and analyze it, keeping the export checks of capture().

    Returns (summary, quality_ok). `quality_ok` is False exactly when the recording
    carries dropped events or an incomplete context set, which makes every CPU load in it
    an undercount: the caller decides whether that is acceptable rather than this function
    quietly returning numbers that cannot be trusted.
    """
    # Resolved once through the profile, so a copied profile with a stale SystemView path is
    # reported by name instead of failing inside a subprocess with an OS error.
    systemview=tool_path(p,'systemview')
    recording,meta=systemview_record(systemview,out,seconds,
                                     device=device or p.get('jlink_device'),
                                     auto_accept_license=auto_accept_license,
                                     timeout=timeout,
                                     speed_khz=speed_khz or p.get('jlink_speed_khz') or 4000,
                                     control_port=control_port or p.get('systemview_control_port')
                                     or SYSTEMVIEW_CONTROL_PORT)
    summary=analyze(systemview,recording,out,auto_accept_license,timeout=90,recover_existing=True)
    summary['recording_meta']=meta
    summary['data_origin']='live_target_capture'
    issues=summary['quality']['issues']
    # The recorded span always covers the request plus whatever was still sitting in the
    # target's RTT buffer when -start arrived: measured 11.7-11.8 s of trace for a 6 s
    # request, and 16.1 s for a 10 s request. Only a span shorter than the request or more
    # than twice it indicates a recording that really did not run as asked.
    if not seconds<=summary['recorded_seconds']<=seconds*2:
        issues.append('recorded_duration_differs_from_requested_window')
    summary['quality']['usable']=not issues
    summary['quality']['allow_overflow']=bool(allow_overflow)
    (Path(out)/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary,not issues


def compare(before,after):
    if not before['quality']['usable'] or not after['quality']['usable']:raise ValueError('Cannot compare invalid/incomplete traces')
    a=before.get('trace_busy_percent');b=after.get('trace_busy_percent')
    if a is None or b is None:raise ValueError('Comparison currently requires one identifiable idle context on one core')
    return {'busy_before_percent':a,'busy_after_percent':b,'busy_delta_percentage_points':round(b-a,4),
            'recorded_seconds_before':before['recorded_seconds'],'recorded_seconds_after':after['recorded_seconds'],
            'caveat':'Descriptive difference only. Same workload, warmup, build mode, instrumentation, IRQ coverage and comparable capture windows must be established independently.'}


def _integer(value):
    match=re.match(r'(0x[0-9a-fA-F]+|\d+)',value)
    if not match:raise ValueError('Expected GDB integer: '+value)
    return int(match[1],0)


def _ram_address(profile,address,size=1):
    if not any(start<=address and address+size<=start+length for start,length in profile['ram_ranges']):
        raise ValueError('RTT metadata points outside configured RAM')
    return address


def capture(p,out,seconds,auto_accept_license=True):
    validate_seconds(seconds);out=Path(out);meta={'elf':elf_identity(p['_elf']),'requested_seconds':seconds,'data_origin':'live_target','isr_coverage':p['isr_coverage']}
    raw=out/'trace.bin';rtt_started=False;server_started=False;stream=None;started=False
    with ProbeLock(),OpenOCD(p,out) as ocd:
        initial=ocd.state();meta['initial_state']=initial
        if initial!='running':raise RuntimeError('Capture requires an already running target; refusing to resume a previously halted target')
        try:
            ocd.command('halt');ocd.command('wait_halt 3000')
            ocd.command('verify_image '+tcl_word(p['_elf'].as_posix()),timeout=90)
            with GDB(p,out,ocd.gdb_port) as gdb:
                address=_integer(gdb.evaluate('(unsigned int)&_SEGGER_RTT'))
                up=_integer(gdb.evaluate('_SEGGER_RTT.MaxNumUpBuffers'));down=_integer(gdb.evaluate('_SEGGER_RTT.MaxNumDownBuffers'))
                if not 1<=up<=16 or not 1<=down<=16:raise RuntimeError('RTT not initialized or invalid descriptor count')
                _ram_address(p,address,24+24*(up+down))
                channels=[]
                for i in range(up):
                    name=gdb.evaluate(f'_SEGGER_RTT.aUp[{i}].sName')
                    if '"SysView"' in name:channels.append(i)
                if len(channels)!=1:raise RuntimeError('Expected exactly one initialized SysView RTT channel')
                channel=channels[0]
                if channel>=down or '"SysView"' not in gdb.evaluate(f'_SEGGER_RTT.aDown[{channel}].sName'):
                    raise RuntimeError('Matching SysView down channel missing')
                enabled=_integer(gdb.evaluate('_SYSVIEW_Globals.MainContext.EnableState'))
                if enabled:raise RuntimeError('SystemView recording already active; refusing to steal it')
                size=_integer(gdb.evaluate(f'_SEGGER_RTT.aUp[{channel}].SizeOfBuffer'))
                buffer=_integer(gdb.evaluate(f'(unsigned int)_SEGGER_RTT.aUp[{channel}].pBuffer'))
                _ram_address(p,buffer,size)
                meta.update(rtt_address=hex(address),rtt_channel=channel,rtt_buffer_bytes=size,
                            system_core_clock_hz=_integer(gdb.evaluate('SystemCoreClock')),elf_verified=True)
            restore_target(ocd,initial)
            ocd.command(f'rtt setup {address:#x} {24+24*(up+down)} '+tcl_word('SEGGER RTT'))
            ocd.command(f'rtt polling_interval {int(p["rtt_poll_ms"])}')
            ocd.command('rtt start');rtt_started=True
            meta['rtt_channels']=ocd.command('rtt channels')
            port=free_port();ocd.command(f'rtt server start {port} {channel}');server_started=True
            stream=socket.create_connection(('127.0.0.1',port),timeout=5);stream.settimeout(.1)
            # Drain old inactive trace data before issuing a fresh Start command.
            end=time.monotonic()+.5
            while time.monotonic()<end:
                try:
                    if not stream.recv(65536):raise ConnectionError('RTT closed before start')
                except socket.timeout:pass
            stream.sendall(b'\x01');started=True
            begin=time.monotonic();total=0
            with raw.open('xb') as f:
                def read_until(end):
                    nonlocal total
                    while time.monotonic()<end:
                        try:chunk=stream.recv(65536)
                        except socket.timeout:continue
                        if not chunk:raise ConnectionError('RTT stream disconnected')
                        total+=len(chunk)
                        if total>64*1024*1024:raise RuntimeError('Capture exceeded 64 MiB safety limit')
                        f.write(chunk)
                read_until(begin+seconds)
                stream.sendall(b'\x02');started=False
                read_until(time.monotonic()+1)
            meta['raw_bytes']=total;meta['capture_wall_seconds']=time.monotonic()-begin
        finally:
            errors=[]
            if stream:
                if started:
                    try:stream.sendall(b'\x02');time.sleep(.1)
                    except OSError as exc:errors.append('stop command: '+str(exc))
                stream.close()
            if server_started:
                try:ocd.command(f'rtt server stop {port}')
                except Exception as exc:errors.append(str(exc))
            if rtt_started:
                try:ocd.command('rtt stop')
                except Exception as exc:errors.append(str(exc))
            try:
                restore_target(ocd,initial);meta['final_state']=ocd.state()
                if meta['final_state']!=initial:errors.append('target state restoration failed')
            except Exception as exc:errors.append(str(exc))
            meta['cleanup_errors']=errors
            (out/'capture-metadata.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
            if errors:raise RuntimeError('Capture cleanup failed: '+'; '.join(errors))
    summary=analyze(tool_path(p,'systemview'),raw,out,auto_accept_license)
    summary.update(meta)
    if summary['cycle_frequency_hz']!=meta['system_core_clock_hz']:
        summary['quality']['issues'].append('timestamp_frequency_mismatch')
    if not seconds<=summary['recorded_seconds']<=seconds*2:
        summary['quality']['issues'].append('recorded_duration_differs_from_requested_window')
    summary['quality']['usable']=not summary['quality']['issues']
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    if not summary['quality']['usable']:raise RuntimeError('Trace quality gate failed; see summary.json: '+', '.join(summary['quality']['issues']))
    return summary


def build_parser():
    """The CLI, as a value. Kept separate from ``main`` so the repository checker can enumerate
    the real subcommands and catch documentation that names one this script does not have -- the
    file map in STATUS.md spent a while claiming `doctor` and `capture` lived here."""
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--version',action='version',version='piny_trace.py '+__version__)
    ap.add_argument('--profile',type=Path,default=ROOT/'profiles/pinycore.json')
    sub=ap.add_subparsers(dest='action',required=True)
    a=sub.add_parser('analyze');a.add_argument('recording',type=Path);a.add_argument('--output',type=Path,required=True)
    a.add_argument('--no-auto-accept-license',action='store_true',
                   help='leave the SystemView license dialog for a human to answer')
    a.add_argument('--recover-existing',action='store_true',
                   help='reuse complete CSVs already in the output directory, or overwrite '
                        'incomplete ones, instead of refusing to touch it')
    a.add_argument('--export-timeout',type=float,default=90.0,
                   help='seconds allowed for the native SystemView export (default 90)')
    c=sub.add_parser('compare');c.add_argument('before',type=Path);c.add_argument('after',type=Path)
    r=sub.add_parser('record',help='record a live trace from the board and analyze it')
    r.add_argument('--output',type=Path,required=True)
    r.add_argument('--seconds',type=float,default=10.0,help='recording length in seconds (default 10)')
    r.add_argument('--device',default=None,
                   help='target name for the recorder (default: the profile jlink_device)')
    r.add_argument('--control-port',type=int,default=None,
                   help='port of the SystemView command server (default: the profile value)')
    r.add_argument('--record-timeout',type=float,default=240.0)
    r.add_argument('--speed',type=int,default=None,
                   help='J-Link interface speed in kHz (default: the profile jlink_speed_khz)')
    r.add_argument('--no-auto-accept-license',action='store_true',
                   help='leave the SystemView license dialog for a human to answer')
    r.add_argument('--allow-overflow',action='store_true',
                   help='do not fail on dropped events or an incomplete context set; the '
                        'reported loads are then undercounts, never complete measurements')
    return ap


def main():
    args=build_parser().parse_args()
    try:
        if args.action=='analyze':
            result=analyze(load_profile(args.profile)['systemview'],args.recording,args.output,
                           not args.no_auto_accept_license,args.export_timeout,
                           args.recover_existing)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        if args.action=='record':
            summary,quality_ok=record(load_profile(args.profile),args.output,args.seconds,
                                      not args.no_auto_accept_license,args.record_timeout,
                                      args.device,args.control_port,args.allow_overflow,
                                      args.speed)
            print(json.dumps(summary,ensure_ascii=False,indent=2))
            if not quality_ok and not args.allow_overflow:
                print(json.dumps({'ok':False,'error':'Trace quality gate failed; the CPU loads above are '
                                  'undercounts. Fix the lost events or pass --allow-overflow to record '
                                  'them explicitly.','issues':summary['quality']['issues']},
                                 ensure_ascii=False))
                return 1
            return 0
        result=compare(json.loads(args.before.read_text(encoding='utf-8-sig')),json.loads(args.after.read_text(encoding='utf-8-sig')))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except Exception as exc:print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False));return 1

if __name__=='__main__':raise SystemExit(main())
