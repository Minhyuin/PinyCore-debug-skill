#!/usr/bin/env python3
"""Managed, auditable PinyCore debug sessions. Python standard library only."""
from __future__ import annotations
import argparse
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

# Kept in step with the release tag by .github/workflows/release.yml, which refuses to publish a
# tag that disagrees with the scripts shipped under it. Bump this in the same commit as any
# behaviour change worth a tag; a version that only lives in the README is a version nobody reads.
__version__ = '0.2.0'


def parse_mi(line):
    match = re.match(r'^\d*\^(\w+)(.*)$', line.strip())
    if not match: raise ValueError('Not a GDB/MI result record: ' + line)
    text = match[2]; pos = 0
    def value():
        nonlocal pos
        if pos >= len(text): raise ValueError('Truncated MI value')
        if text[pos] == '"':
            start=pos; pos+=1
            while pos < len(text):
                c=text[pos];pos+=1
                if c == '\\':pos+=1
                elif c == '"':return ast.literal_eval(text[start:pos])
            raise ValueError('Unterminated MI string')
        if text[pos] in '[{':
            end=']' if text[pos]=='[' else '}'; is_dict=end=='}';pos+=1
            result={} if is_dict else []
            while pos<len(text) and text[pos]!=end:
                m=re.match(r'([\w-]+)=',text[pos:])
                if m:
                    pos+=len(m[0]);v=value()
                    if is_dict: result[m[1]]=v
                    else:result.append({m[1]:v})
                elif is_dict:raise ValueError('Missing MI key')
                else:result.append(value())
                if pos<len(text) and text[pos]==',':pos+=1
                elif pos<len(text) and text[pos]!=end:raise ValueError('Missing MI separator')
            if pos>=len(text):raise ValueError('Unterminated MI container')
            pos+=1;return result
        raise ValueError('Unsupported MI value')
    data={}
    while pos<len(text):
        if text[pos]!=',':raise ValueError('Missing MI result separator')
        pos+=1;m=re.match(r'([\w-]+)=',text[pos:])
        if not m:raise ValueError('Invalid MI key')
        pos+=len(m[0]);data[m[1]]=value()
    return match[1],data


def tcl_word(value):
    value=str(value)
    if any(c in value for c in '{}\\\r\n\x00'):raise ValueError('Unsafe Tcl argument')
    return '{'+value+'}'


def safe_symbol(value):
    if not re.fullmatch(r'[A-Za-z_][\w]*(?:(?:\.[A-Za-z_]\w*)|(?:\[\d+\]))*',value):
        raise ValueError('Only symbol/member/constant-array-index reads are permitted')
    return value


def validate_memory(address,count,allow_peripheral=False):
    if not 1<=count<=4096 or not 0<=address<=0xffffffff or address+count>0x100000000:
        raise ValueError('Memory request must be 1..4096 bytes within the 32-bit address space')
    if address+count>0x40000000 and not allow_peripheral:
        raise ValueError('Peripheral/system memory needs explicit --allow-peripheral-read (read side effects possible)')
    return address,count


def elf_identity(path):
    path=Path(path);data=path.read_bytes()
    if len(data)<52 or data[:6]!=b'\x7fELF\x01\x01' or struct.unpack_from('<H',data,18)[0]!=40:
        raise ValueError('Expected a little-endian ELF32 ARM firmware')
    return {'path':str(path.resolve()),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}


class ProbeLock:
    def __init__(self,path=None):
        self.path=Path(path or Path(tempfile.gettempdir())/'PinyCore-debug-probe.lock')
        self.token=uuid.uuid4().hex
    def __enter__(self):
        try:fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError:raise RuntimeError(f'Probe session lock exists: {self.path}. Check owner PID/debug processes before manually clearing a stale lock.')
        with os.fdopen(fd,'w') as f:json.dump({'pid':os.getpid(),'token':self.token},f)
        return self
    def __exit__(self,*args):
        if self.path.exists() and json.loads(self.path.read_text())['token']==self.token:self.path.unlink()


def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def stop_process(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:proc.wait(timeout=4)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=4)


class OpenOCD:
    def __init__(self,profile,out,adapter_khz=None):
        self.profile=profile;self.out=Path(out);self.proc=None;self.sock=None;self.log=None
        self.adapter_khz=int(adapter_khz or profile['adapter_khz']);self.state_at_connect=None
        self.gdb_port=free_port();self.tcl_port=free_port()
        while self.tcl_port==self.gdb_port:self.tcl_port=free_port()
    def __enter__(self):
        p=self.profile;self.out.mkdir(parents=True,exist_ok=True)
        self.log=(self.out/'openocd.log').open('w',encoding='utf-8')
        argv=[tool_path(p,'openocd'),'-c','bindto 127.0.0.1','-c',f'gdb_port {self.gdb_port}',
              '-c',f'tcl_port {self.tcl_port}','-c','telnet_port disabled',
              '-f',str(Path(p['project'])/p['interface_config']),'-f',p['target_config'],
              '-c',f'adapter speed {self.adapter_khz}','-c','init']
        (self.out/'openocd-command.json').write_text(json.dumps(argv,indent=2),encoding='utf-8')
        try:
            self.proc=subprocess.Popen(argv,cwd=p['project'],stdout=self.log,stderr=subprocess.STDOUT,creationflags=HIDDEN)
            end=time.monotonic()+15
            while time.monotonic()<end:
                if self.proc.poll() is not None:raise RuntimeError('OpenOCD exited; see '+str(self.out/'openocd.log'))
                try:self.sock=socket.create_connection(('127.0.0.1',self.tcl_port),timeout=.3);break
                except OSError:time.sleep(.1)
            if self.sock is None:raise TimeoutError('OpenOCD startup timed out')
            self.sock.settimeout(20)
            self.command('targets '+p['target'])
            # Sampled before this command does anything with the target: `init` has already
            # opened the debug port, so this is the untouched state the connection found.
            try:self.state_at_connect=self.state()
            except RuntimeError:self.state_at_connect=None
            return self
        except BaseException:self.__exit__(None,None,None);raise
    def command(self,cmd,timeout=20):
        self.sock.settimeout(timeout)
        wrapped='set __pc_code [catch {'+cmd+'} __pc_result]; format "%d\\n%s" $__pc_code $__pc_result'
        self.sock.sendall(wrapped.encode()+b'\x1a');data=bytearray()
        while b'\x1a' not in data:
            chunk=self.sock.recv(65536)
            if not chunk:raise ConnectionError('OpenOCD Tcl connection closed')
            data.extend(chunk)
            if len(data)>8*1024*1024:raise RuntimeError('Oversized Tcl response')
        text=data.split(b'\x1a',1)[0].decode('utf-8',errors='replace')
        code,sep,result=text.partition('\n')
        with (self.out/'tcl.log').open('a',encoding='utf-8') as f:f.write(cmd+'\n'+text+'\n')
        if not sep or code!='0':raise RuntimeError('OpenOCD command failed: '+cmd+'\n'+text)
        return result
    def state(self):return self.command(self.profile['target']+' curstate').strip()
    def __exit__(self,*args):
        if self.sock:
            try:self.command('shutdown',timeout=2)
            except (OSError,RuntimeError):pass
            self.sock.close();self.sock=None
        stop_process(self.proc)
        if self.log:self.log.close()


class GDB:
    def __init__(self,profile,out,port):self.profile=profile;self.out=Path(out);self.port=port;self.proc=None;self.token=0;self.q=queue.Queue()
    def __enter__(self):
        self.log=(self.out/'gdb-mi.log').open('w',encoding='utf-8')
        try:
            self.proc=subprocess.Popen([tool_path(self.profile,'gdb'),'--quiet','--nx','--nh','--interpreter=mi2',
                # Required, not cosmetic: with the default synchronous MI mode GDB answers NOTHING
                # once the target runs -- every read times out and `-exec-interrupt` is never even
                # sent to the server. With mi-async on at startup, `-exec-interrupt` answers `^done`
                # in 0.05 s and a stopped-register read answers `(running)` instead of hanging.
                '-ex','set mi-async on'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,
                cwd=self.profile['project'],creationflags=HIDDEN)
            def pump():
                for line in self.proc.stdout:
                    self.log.write(line);self.log.flush();self.q.put(line.rstrip())
                self.q.put(None)
            self.thread=threading.Thread(target=pump,daemon=True);self.thread.start()
            self.command('-gdb-set auto-load off')
            self.command('-file-exec-and-symbols '+json.dumps(str(self.profile['_elf']).replace('\\','/')))
            self.command(f'-target-select remote 127.0.0.1:{self.port}')
            return self
        except BaseException:self.__exit__(None,None,None);raise
    def command(self,cmd,timeout=20,streams=None):
        """Send one MI command and return its result record.

        Output written by console commands ("~\\"...\\"" records) is NOT part of the result record,
        so a caller that needs it must pass a list in `streams` to collect those lines -- without
        that, `-interpreter-exec console` looks like it produced nothing at all.
        """
        self.token+=1;token=str(self.token);self.proc.stdin.write(token+cmd+'\n');self.proc.stdin.flush()
        deadline=time.monotonic()+timeout
        while True:
            try:line=self.q.get(timeout=max(.001,deadline-time.monotonic()))
            except queue.Empty:raise TimeoutError('GDB timed out: '+cmd)
            if line is None:raise RuntimeError('GDB exited before responding')
            if line.startswith(token+'^'):
                kind,result=parse_mi(line)
                if kind=='error':raise RuntimeError('GDB: '+result.get('msg','unknown error'))
                return result
            if streams is not None and line[:2] in ('~"','@"','&"'):
                payload=line[1:].strip()
                if payload.startswith('"') and payload.endswith('"'):
                    try:streams.append(ast.literal_eval(payload))
                    except (ValueError,SyntaxError):pass
            if time.monotonic()>=deadline:raise TimeoutError('GDB timed out: '+cmd)
    def evaluate(self,expression):return self.command('-data-evaluate-expression '+json.dumps(expression))['value']
    def __exit__(self,*args):
        if self.proc and self.proc.poll() is None:
            try:self.command('-target-disconnect',timeout=2)
            except (RuntimeError,TimeoutError,OSError):pass
            try:self.command('-gdb-exit',timeout=2)
            except (RuntimeError,TimeoutError,OSError):pass
        stop_process(self.proc)
        if hasattr(self,'thread'):self.thread.join(timeout=3)
        if hasattr(self,'log'):self.log.close()
        if self.proc:
            for stream in [self.proc.stdin,self.proc.stdout]:
                if stream:stream.close()


def restore_target(session,initial):
    """Put the target back into the state this command found it in, or fail loudly.

    Works through the session interface, so it restores correctly whether the transport is
    OpenOCD's Tcl commands or a J-Link session's GDB commands.
    """
    if initial not in ('running','halted'):raise RuntimeError('Refusing to restore unknown target state: '+str(initial))
    current=session.state()
    if current==initial:return
    if current not in ('running','halted'):raise RuntimeError('Target state unknown; manual recovery required: '+str(current))
    if initial=='running':session.resume()
    else:session.halt('restore')
    if session.state()!=initial:raise RuntimeError('Target did not return to '+initial)


class JLinkGDBServer:
    """SEGGER J-Link GDB Server as the debug transport, for probes OpenOCD cannot drive.

    An attached SEGGER probe is owned by SEGGER's kernel driver, so OpenOCD fails with
    ``LIBUSB_ERROR_NOT_FOUND``; SEGGER's own GDB server is the supported route. It speaks the
    same GDB remote protocol, so the existing ``GDB`` MI client works unchanged -- only the
    process that owns the probe differs, and with it the command names.

    Server output is captured to ``jlink-gdb-server.log``. The server does not flush its
    "waiting for GDB" line until a client attaches, so readiness is judged by "the process is
    still alive", never by connecting a socket first: the probe is exclusive, and a bare
    connect can consume the single client slot before the real GDB arrives.
    """

    def __init__(self,profile,out,device=None,speed_khz=None,port=None):
        self.profile=profile;self.out=Path(out);self.proc=None;self.log=None
        self.device=device or profile.get('jlink_device')
        if not self.device:raise ValueError('No J-Link device name: pass --device or set jlink_device in the profile')
        self.speed_khz=int(speed_khz or profile.get('jlink_speed_khz') or 4000)
        self.port=int(port or profile.get('jlink_gdb_port') or 2331)
        # OpenOCD calls this gdb_port; expose the same name so a session can pick either backend.
        self.gdb_port=self.port

    def argv(self):
        """The exact server command line, kept separate so it can be asserted in tests."""
        p=self.profile
        argv=[tool_path(p,'jlink_gdb_server'),'-device',self.device,'-if','SWD','-speed',str(self.speed_khz),
              '-port',str(self.port),'-silent','-nogui','-localhostonly']
        if p.get('jlink_serial'):argv+=['-select','USB='+str(p['jlink_serial'])]
        return argv

    def __enter__(self):
        p=self.profile;self.out.mkdir(parents=True,exist_ok=True)
        argv=self.argv()
        (self.out/'jlink-gdb-server-command.json').write_text(json.dumps(argv,indent=2),encoding='utf-8')
        self.log=(self.out/'jlink-gdb-server.log').open('w',encoding='utf-8')
        try:
            self.proc=subprocess.Popen(argv,cwd=p['project'],stdout=self.log,stderr=subprocess.STDOUT,creationflags=HIDDEN)
            end=time.monotonic()+JLINK_STARTUP_SECONDS
            while time.monotonic()<end:
                if self.proc.poll() is not None:
                    raise RuntimeError('J-Link GDB Server exited; see '+str(self.out/'jlink-gdb-server.log'))
                time.sleep(.05)
            if self.proc.poll() is not None:
                raise RuntimeError('J-Link GDB Server exited during startup; see '+str(self.out/'jlink-gdb-server.log'))
            return self
        except BaseException:self.__exit__(None,None,None);raise
    def __exit__(self,*args):
        stop_process(self.proc)
        if self.log:
            self.log.write('\n[managed by PinyCore-debug: server stopped]\n');self.log.close()
    def evaluate(self,expression):
        """Forward a GDB expression to the MI client this server is paired with.

        The GDB client is attached by ``ProbeSession``, so the server delegates instead of
        holding a second connection. This is what lets a caller work against either backend
        without asking which probe is attached (``check_target`` does exactly that).
        """
        if getattr(self,'gdb',None) is None:
            raise RuntimeError('No GDB client is attached to this J-Link server yet')
        return self.gdb.evaluate(expression)
    def go(self):
        """Start the core through the server's own ``monitor go``.

        MEASURED ON THIS BOARD, and the reason this exists instead of ``-exec-continue``: with
        the SEGGER GDB server, GDB's continue leaves DHCSR C_HALT set (``0x30003`` before and
        after), i.e. GDB believes the target runs while the core stays stopped, and while it
        believes that it also refuses every memory read with "Cannot execute this command while
        the target is running" -- so nothing can be verified either way. ``monitor go`` goes
        through the J-Link's own run control and does clear it (``0x30003`` -> ``0x1010001``).
        """
        if getattr(self,'gdb',None) is None:
            raise RuntimeError('No GDB client is attached to this J-Link server yet')
        return self.gdb.command('-interpreter-exec console "monitor go"',timeout=60)


class ProbeSession:
    """One target session with the probe-specific command names hidden behind one interface.

    ``read_u32``/``read_memory`` must stay safe to call before any halt: ``identify`` relies on
    that to read identity and DHCSR without ever stopping the CPU.
    """

    def __init__(self,profile,out,driver,adapter_khz=None,device=None,speed_khz=None,port=None):
        self.profile=profile;self.out=Path(out);self.driver=driver;self.opened=None;self.gdb=None
        self.backend=None;self.halt_issued=False
        if driver=='openocd':
            self.backend=OpenOCD(profile,out,adapter_khz)
            self.adapter_khz=self.backend.adapter_khz
        elif driver=='jlink':
            self.backend=JLinkGDBServer(profile,out,device,speed_khz,port)
            self.adapter_khz=self.backend.speed_khz
        else:raise ValueError('Unknown probe driver: '+repr(driver))
    def __enter__(self):
        self.backend.__enter__();self.opened=self.backend
        try:
            self.gdb=GDB(self.profile,self.out,self.backend.gdb_port).__enter__()
            # Hand the MI client to the backend so a caller can use either probe through one
            # interface (JLinkGDBServer.evaluate forwards here; check_target relies on it).
            self.backend.gdb=self.gdb
            return self
        except BaseException:
            self.backend.__exit__(None,None,None);self.backend=None;raise
    def __exit__(self,*args):
        if self.backend is not None:self.backend.gdb=None
        if self.gdb is not None:
            try:self.gdb.__exit__(None,None,None)
            finally:self.gdb=None
        if self.backend is not None:
            try:self.backend.__exit__(None,None,None)
            finally:self.backend=None
    def read_u32(self,address):
        if isinstance(self.backend,OpenOCD):
            return int(self.backend.command(f'read_memory {address:#x} 32 1').strip(),0)
        # A bare numeric expression only evaluates the literal: it never touches memory, so
        # `(unsigned int)0xE000ED00` returns 0xE000ED00 itself. Dereference a typed pointer.
        text=self.gdb.evaluate(f'*(unsigned int *){address:#x}').strip()
        # GDB annotates a value that lands on a known symbol: it answers
        # `0x802e9ac <prvCheckTasksWaitingTermination>`, not a bare number. Only the first
        # token is the value, so int(text,0) on the whole reply must never be trusted.
        return int(text.split()[0],0)
    def read_memory(self,address,count):
        return self.gdb.command(f'-data-read-memory-bytes {address:#x} {count}')['memory']
    def halt(self,note='halt'):
        if isinstance(self.backend,OpenOCD):
            self.backend.command('halt');self.backend.command('wait_halt 3000')
        else:self.gdb.command('-exec-interrupt')
        self.halt_issued=True;return note
    def resume(self):
        """Start the core. On J-Link this must go through ``monitor go``; see that method.

        The branch is decided by ``self.driver``, not by the backend's class: a backend is a
        duck-typed handle (its class is irrelevant to callers, and tests substitute their own),
        while the driver is the recorded choice of probe and is exactly what ``__init__`` used
        to build the backend. An ``isinstance`` test against ``JLinkGDBServer`` here would be
        dead code -- that class is never the object stored in ``self.backend``.
        """
        if isinstance(self.backend,OpenOCD):self.backend.command('resume')
        elif self.driver=='jlink':self.backend.go()
        else:self.gdb.command('-exec-continue')
    def resume_confirmed(self):
        """Start the core, then prove it; never report a halt as a successful resume.

        The proof is the hardware's, because GDB's report is not trustworthy here: measured on
        this board, `-exec-continue` left C_HALT set and GDB still called the target running,
        and even after a genuinely working `monitor go` GDB kept answering `info registers pc`
        with a number. So success requires ``state()`` to say 'running' -- which, on a J-Link
        session, means DHCSR's halt bits are clear. 'unknown' is retried and then reported as
        not running: a resume that cannot be verified must never be presented as one.
        """
        state='unknown'
        for _ in range(3):
            self.resume()
            time.sleep(0.3)
            state=self.state()
            if state=='running':return state
        return state
    def tick_count(self,symbol=None):
        """Read the RTOS tick counter, or None when it cannot be read.

        None means "not asked", never zero: a zero tick is a legitimate value right after a
        reset, and collapsing the two would turn an unreadable probe into a false answer.
        ``probe_liveness`` walks the same path, so a session that cannot name the counter
        must degrade to "not checked" instead of raising.
        """
        # getattr, not self.profile: a session that never reached __init__ (a test double, a
        # half-built handle) has no profile, and an unanswerable liveness probe may not become
        # an exception that masks the real result.
        profile=getattr(self,'profile',None) or {}
        if symbol is None:
            text=profile.get('freertos_tick_symbol') or tick_symbol_from_elf(profile)
        else:
            text=symbol
        if not text:return None
        try:return self.read_u32(int(text,0) if str(text).startswith('0x') else int(text,0))
        except (RuntimeError,ValueError,TypeError,AttributeError):return None
    def tick_advances(self,seconds=1.5,symbol=None):
        """Say whether the firmware is EXECUTING, by watching the RTOS tick counter move.

        Why this and not DHCSR: MEASURED ON THIS BOARD, a halted core reported
        ``C_HALT = 0``, and the same reading came back frozen across sessions -- the
        debug-status register cannot be trusted as a liveness witness through this GDB
        server. The tick counter can be: it is written by the RTOS tick handler on every
        tick, so it moves only while the firmware actually executes, and no debugger
        bookkeeping can forge it.

        The resume before the first read is NOT incidental. MEASURED ON THIS BOARD: the first
        memory read after attach is answered from a debug cache primed when the session
        opened, so a window that OPENED with that sample compared two copies of one stale
        value -- two reads 4 s apart both returned 213916 for a board that was executing.
        After ``monitor go`` the cache is dropped and the same address answered 217714 ->
        217715 -> 217716 on three consecutive reads with no delay at all. Starting the core
        first is therefore what makes ``before`` a real sample and the window meaningful.
        """
        self.resume()
        before=self.tick_count(symbol)
        if before is None:return None
        time.sleep(max(0.2,float(seconds)))
        after=self.tick_count(symbol)
        if after is None:return None
        return after!=before
    def halt_confirmed(self):
        """Ask the core itself whether it is stopped, instead of asking GDB what it thinks.

        DHCSR S_HALT/C_HALT are set by the hardware and cleared by the hardware. This is the
        only resume confirmation that cannot be fooled by GDB's own bookkeeping -- the check
        that caught ``-exec-continue`` reporting a run that never happened. Returns None when
        the core cannot be asked (a running target refuses the memory read), because None must
        never be read as "running".
        """
        try:raw=self.read_u32(DHCSR)
        except RuntimeError:return None
        bits=decode_dhcsr(raw)
        return None if (bits['c_halt'] or bits['s_halt']) else True
    def state(self):
        """Running or halted -- with the HARDWARE's answer preferred over GDB's bookkeeping.

        GDB's `$pc` is not a state probe. MEASURED ON THIS BOARD: after a `monitor go` that
        really did start the core (DHCSR C_HALT cleared to 0x1010001), GDB still answered
        `info registers pc` with a plain number, i.e. "halted", while the core was executing.
        Judging the run state from that is how a resume that never happened gets reported as
        success. DHCSR is written by the hardware, so it decides whenever it can be read; GDB
        is the fallback for the OpenOCD path, where the backend answers directly.
        """
        if isinstance(self.backend,OpenOCD):return self.backend.state()
        forced=self.hardware_state()
        if forced is not None:return forced
        # With mi-async on (see GDB.__enter__) a register read never blocks on a running target:
        # GDB either answers "(running)" or refuses with "Selected thread is running.".
        try:value=self.gdb.evaluate('$pc')
        except RuntimeError as exc:
            if 'running' in str(exc):return 'running'
            return 'unknown'
        if '(running)' in value:return 'running'
        if value.strip():return 'halted'
        return 'unknown'
    def hardware_state(self):
        """The core's own run state from DHCSR, or None when it cannot be established.

        None means "unreadable", not "running": a core that is executing may refuse the memory
        read (the J-Link GDB server answers "Cannot execute this command while the target is
        running"), and that refusal is also what a disabled debug port looks like. Callers must
        therefore treat None as unknown and fall back, never as evidence that the core runs.
        """
        try:raw=self.read_u32(DHCSR)
        except RuntimeError:return None
        bits=decode_dhcsr(raw)
        if bits['c_halt'] or bits['s_halt']:return 'halted'
        return 'running'


#: How long the J-Link GDB Server is given to come up before GDB is allowed to connect.
JLINK_STARTUP_SECONDS=1.5


def make_session(p,out,adapter_khz=None,device=None,speed_khz=None,port=None,driver=None):
    """Open the session for the profile's probe. ``driver`` overrides the profile."""
    chosen=driver or p.get('probe') or 'openocd'
    return ProbeSession(p,out,chosen,adapter_khz,device,speed_khz,port)


def load_profile(path,discover_tools=True):
    """Read a profile and attach what the rest of the module needs from it.

    Absolute tool paths are the only machine-specific part of this package, and a profile
    copied from another computer carries paths that do not exist here. Rather than failing
    later with ``FileNotFoundError`` from an unnamed subprocess, the profile's tools are
    resolved once, here: a value that exists is used verbatim, and an absent one is looked up
    with :mod:`piny_tools`. The result lands in ``_tools`` (key -> ``{path,source,exists}``)
    and every caller opens a tool through :func:`tool_path`.

    Discovery is on by default because failing with a profile key and a suggested replacement
    is strictly more useful than failing with an OS error, and because it only ever substitutes
    a tool the machine actually has. Pass ``discover_tools=False`` for a strict run that uses
    the profile exactly as written.
    """
    source=Path(path)
    p=json.loads(source.read_text(encoding='utf-8-sig'))
    p['_elf']=Path(p['project'])/p['elf']
    p['_profile_path']=str(source)
    p['_tools']={}
    if discover_tools:
        import piny_tools
        for key,report in piny_tools.discover(p)['tools'].items():
            if report['exists']:
                p['_tools'][key]={'path':report['path'],'source':report['source']}
    return p


def tool_path(p,key):
    """The executable to run for ``key``, or an error that names the key and what it is for.

    A missing tool is a configuration problem, not a crash inside a subprocess, so this is
    where the message is written: it names the profile key, what the tool does, where the
    profile pointed, and what to do about it.
    """
    resolved=(p.get('_tools') or {}).get(key)
    value=(resolved or {}).get('path') or p.get(key)
    if value and Path(str(value)).is_file():
        return str(value)
    if not value:
        raise RuntimeError('Profile has no "'+key+'" entry and no '+key+' was found on this '
                           'machine. Set "'+key+'" in '+str(p.get('_profile_path') or 'the profile')+
                           ' to the full path of the executable.')
    hint=''
    with_discovery=discover_tool_for_message(key)
    if with_discovery:
        hint=' Found one here instead: '+with_discovery+' -- run "doctor" to see the full list.'
    raise RuntimeError('Profile key "'+key+'" points at '+str(value)+', which is not a file on '
                       'this machine. Set it to the right path in '+
                       str(p.get('_profile_path') or 'the profile')+'.'+hint)


def discover_tool_for_message(key):
    """Best-effort single-tool lookup used only to make an error message useful."""
    try:
        import piny_tools
        found=piny_tools.find_tool(key)
        return found['path'] if found['exists'] else None
    except Exception:
        return None


def snapshot(p,out,symbols=(),memory=None,allow_peripheral=False,driver=None,device=None,speed_khz=None):
    for symbol in symbols:safe_symbol(symbol)
    if memory:validate_memory(*memory,allow_peripheral)
    result={'elf':elf_identity(p['_elf']),'elf_verified':False,'probe':driver or p.get('probe') or 'openocd'}
    with ProbeLock(),make_session(p,out,None,device,speed_khz,None,driver) as session:
        initial=session.state();result['initial_state']=initial
        if initial not in ('running','halted'):raise RuntimeError('Unexpected target state: '+str(initial))
        try:
            session.halt('snapshot')
            if symbols:
                if isinstance(session.backend,OpenOCD):
                    session.backend.command('verify_image '+tcl_word(p['_elf'].as_posix()),timeout=90)
                else:
                    # The J-Link GDB server has no verify_image, so the console comparison is
                    # used; a mismatch must stop the symbolic read (see the helper).
                    comparison=verify_flash_matches_elf(session,p['_elf'])
                    result['section_comparison']={'command':comparison['command'],
                                                  'output':comparison['output']}
                    if comparison['mismatched']:
                        raise RuntimeError('ELF does not match the image in Flash; refusing to resolve '
                                           'symbols against it: '+json.dumps(comparison['mismatched']))
                    if not comparison['reported_match']:
                        raise RuntimeError('compare-sections produced no readable result, so the ELF was '
                                           'not verified against the target: '
                                           +json.dumps(comparison['output']))
                result['elf_verified']=True
            names=session.gdb.command('-data-list-register-names')['register-names']
            values=session.gdb.command('-data-list-register-values x')['register-values']
            result['registers']={names[int(v['number'])]:v['value'] for v in values if names[int(v['number'])]}
            result['symbols']={s:session.gdb.evaluate(safe_symbol(s)) for s in symbols}
            result['fault_status']=session.gdb.command('-data-read-memory-bytes 0xe000ed28 24')['memory']
            if memory:
                address,count=validate_memory(*memory,allow_peripheral)
                result['memory']=session.gdb.command(f'-data-read-memory-bytes {address:#x} {count}')['memory']
        finally:
            restore_target(session,initial)
            result['final_state']=session.state()
            if result['final_state']!=initial:raise RuntimeError('Target state restoration failed: '+str(result['final_state']))
    return result


def check_target(backend,p):
    """Read the DBGMCU device identifier and refuse anything that is not the expected family.

    Takes the backend of either probe (``OpenOCD`` or ``JLinkGDBServer``/``GDB``): this is the
    last gate before Flash is written, so it must work under the SEGGER probe too.
    """
    address=int(p['idcode_address'],0);expected=int(p['expected_device_id'],0)
    if isinstance(backend,OpenOCD):
        raw=int(backend.command(f'read_memory {address:#x} 32 1').strip(),0)
    else:
        raw=int(backend.evaluate(f'*(unsigned int *){address:#x}').strip(),0)
    if raw & 0xfff != expected:raise RuntimeError(f'Target identity mismatch: expected {expected:#x}, got {raw & 0xfff:#x}; refusing flash')
    return {'device_id':hex(raw & 0xfff),'revision_id':hex(raw >> 16),'raw_idcode':hex(raw)}


def verify_flash_matches_elf(session,elf):
    """Read-only comparison of the image in Flash against the ELF, for the J-Link backend.

    GDB has no MI command for this -- ``-data-compare-sections`` answers ``Undefined MI
    command: data-compare-sections`` -- so the console command is used through MI. It only
    reads the target and reports MISMATCH; unlike ``-target-download`` it never writes Flash.
    Returns the raw output plus the mismatching lines, so the caller decides what a mismatch
    means instead of this function deciding it.
    """
    console=[]
    session.gdb.command('-interpreter-exec console "compare-sections"',timeout=120,streams=console)
    output=''.join(console)
    mismatched=[line.strip() for line in output.splitlines() if 'MISMATCH' in line.upper()]
    return {'command':'compare-sections','output':output,'mismatched':mismatched,
            'reported_match':'matched' in output.lower()}


def load_image(session,elf):
    """Program the ELF into the target through the GDB connection (J-Link backend).

    ``-target-download`` writes the ELF's loadable sections to Flash AND verifies every block
    it wrote (GDB reports each section as ``Load failed`` or ``done``), so a successful reply
    is evidence the bytes in Flash are the bytes in the file. Symbol files are off-limits here
    (the ELF and the Flash are expected to differ -- that is why the flashing is happening),
    so the caller must NOT have run ``compare-sections`` expecting agreement.
    """
    reply=session.gdb.command(f'-file-exec-and-symbols "{Path(elf).as_posix()}"',timeout=60)
    if reply.get('status')=='error':
        raise RuntimeError('GDB refused the ELF: '+json.dumps(reply))
    download=session.gdb.command('-target-download',timeout=600)
    text=json.dumps(download)
    if download.get('status')=='error' or 'failed' in text.lower():
        raise RuntimeError('Flashing failed: '+text)
    return {'command':'-target-download','reply':download}


def _state_after(build,seconds=6.0):
    """Poll a callable returning the target state until it stops being 'unknown'."""
    deadline=time.monotonic()+seconds
    state='unknown'
    while time.monotonic()<deadline:
        state=build()
        if state in ('running','halted'):return state
        time.sleep(0.25)
    return state


def tick_symbol_from_elf(profile):
    """Resolve the FreeRTOS tick counter out of the ELF, so a profile need not hard-code it.

    ``xTickCount`` is a static in the image, so its address moves on every rebuild: a profile
    that carries the address is a profile that silently reads the wrong word after the next
    build. Resolving it from the symbols of the image the profile already names removes that
    whole class of mistake, and a profile value still wins when it is set, because a user who
    wrote an address deliberately may be reading a different image on purpose.

    Returns a ``0x...`` string or None. Every failure path returns None: a missing toolchain,
    a stripped image, or a symbol that is not there must degrade to "liveness not checked",
    never to an exception in the middle of a flash.
    """
    elf=(profile or {}).get('_elf')
    if not elf:return None
    try:
        elf=Path(elf)
        if not elf.is_file():return None
    except (TypeError,ValueError):return None
    nm=None
    try:
        gdb=(profile.get('_tools') or {}).get('gdb',{}).get('path') or profile.get('gdb')
        if gdb:
            candidate=Path(gdb).parent/('arm-none-eabi-nm.exe' if os.name=='nt' else 'arm-none-eabi-nm')
            if candidate.is_file():nm=str(candidate)
        if nm is None:
            nm=shutil.which('arm-none-eabi-nm')
        if nm is None:return None
        cp=subprocess.run([nm,'-C','-n',str(elf)],capture_output=True,text=True,errors='replace',
                          timeout=30,creationflags=HIDDEN)
        if cp.returncode!=0:return None
    except (OSError,subprocess.SubprocessError):return None
    # nm's second column is one character: an upper-case letter is a global, lower-case a
    # local. xTickCount is a file-scope static in tasks.c, so 'b' is the expected type.
    for line in cp.stdout.splitlines():
        parts=line.split()
        if len(parts)>=3 and parts[2].split('(')[0].strip()=='xTickCount':
            try:return hex(int(parts[0],16))
            except ValueError:continue
    return None


def probe_liveness(session,seconds=1.5):
    """Report whether the firmware EXECUTES, by watching the RTOS tick advance.

    This is the honest answer to "did the flash leave the board running?", and it is the
    check that a debug-status register cannot give. MEASURED ON THIS BOARD: DHCSR reported a
    halted core (``0x30003``, frozen across sessions) while `xTickCount` advanced ~988/s, so
    a tool that trusted DHCSR alone would have called a running board stopped. The tick is
    the only witness here that the firmware itself produces.
    """
    profile=getattr(session,'profile',None) or {}
    # A profile value is used when present; otherwise the address is read out of the image.
    # Falling back to the ELF is what lets a copied profile work on a rebuilt project.
    symbol=profile.get('freertos_tick_symbol') or tick_symbol_from_elf(profile)
    if not symbol:
        return {'checked':False,'reason':'no freertos_tick_symbol in the profile and xTickCount '
                                         'could not be resolved from the ELF named by "elf"'}
    # THE FIRST MEMORY READ AFTER ATTACH IS STALE. MEASURED ON THIS BOARD: a session that read
    # the tick twice, 4 s apart with no run control in between, got the SAME value both times
    # (213916 / 213916) while the board was demonstrably executing. The J-Link GDB server answers
    # from a debug cache primed at attach. Running the core forces a genuine read: after
    # `monitor go` the same address answered 217714 -> 217715 -> 217716 across three consecutive
    # reads with no delay. So the window must OPEN with a read primed by a resume, otherwise
    # "tick_before" is a stale sample and the comparison says nothing about any window at all.
    session.resume()
    before=session.tick_count(symbol)
    if before is None:
        return {'checked':False,'symbol':symbol,'reason':'the tick counter could not be read'}
    time.sleep(max(0.2,float(seconds)))
    after=session.tick_count(symbol)
    if after is None:
        return {'checked':False,'symbol':symbol,'tick_before':before,
                'reason':'the tick counter could not be read back'}
    advanced=after-before if after>=before else after+2**32-before
    return {'checked':True,'symbol':symbol,'tick_before':before,'tick_after':after,
            'advanced':advanced,'seconds':float(seconds),'running':advanced>0,
            'rate_hz':round(advanced/float(seconds),1) if advanced>0 else 0.0}


def _resume_and_confirm(session,attempts=3):
    """Start the target and prove it is running; never report a halt as success.

    A reset is not enough: measured on this board, `monitor reset 0` over the J-Link GDB
    server left the core HALTED (DHCSR C_HALT set, `identify` reporting `state_at_entry
    halted`), so a flash routine that stopped after the reset would leave a freshly written
    board standing still. Resume explicitly and confirm, and retry rather than assume.

    The confirmation is deliberately TWO independent witnesses, because each one alone has
    been observed to lie on this board:
      * DHCSR's halt bits -- written by hardware, but returned frozen (and wrong) here once;
      * the RTOS tick counter -- produced by the firmware, so it cannot be forged, but it
        needs the profile to know where the counter lives.
    Either witness reporting "executing" is accepted; when neither can be read the state is
    reported as such rather than assumed to be running.
    """
    state='unknown'
    for _ in range(max(1,attempts)):
        state=session.resume_confirmed()
        if state=='running':return state
        if session.tick_advances(1.5) is True:return 'running'
    raise RuntimeError('The target could not be confirmed as running after the reset: state is '
                       +repr(state)+'. Refusing to report a board that may be standing still as flashed.' )


def flash(p,out,driver=None,device=None,speed_khz=None,port=None):
    """Program the ELF and leave the target running. Requires the caller's flash authorization.

    Two backends, because the probe decides which one can work: OpenOCD's own `program ...
    verify reset` for an OpenOCD-drivable probe, and GDB's `-target-download` over SEGGER's
    GDB server for a SEGGER probe (OpenOCD cannot drive one: LIBUSB_ERROR_NOT_FOUND).
    Both check the device family identifier before writing anything.
    """
    chosen=driver or p.get('probe') or 'openocd'
    if chosen not in ('openocd','jlink'):raise ValueError('Unknown probe driver: '+repr(chosen))
    result={'elf':elf_identity(p['_elf']),'probe':chosen}
    if chosen=='openocd':
        with ProbeLock(),OpenOCD(p,out) as ocd:
            result['initial_state']=ocd.state()
            result['target_identity']=check_target(ocd,p)
            ocd.command('program '+tcl_word(p['_elf'].as_posix())+' verify reset',timeout=150)
            result['flash_verified']=True
            result['final_state']=_state_after(ocd.state)
    elif chosen=='jlink':
        with ProbeLock(),make_session(p,out,None,device,speed_khz,port,'jlink') as session:
            result['initial_state']=session.state()
            result['target_identity']=check_target(session.backend,p)
            session.halt('flash')
            # Informational only: a mismatch is the normal reason to flash, never a blocker here.
            result['elf_vs_flash_before']=verify_flash_matches_elf(session,p['_elf'])
            result['download']=load_image(session,p['_elf'])
            after=verify_flash_matches_elf(session,p['_elf'])
            if after['mismatched'] or not after['reported_match']:
                raise RuntimeError('Flash written but the target does not match the ELF afterwards: '
                                   +json.dumps(after['output']))
            result['flash_verified']=True
            result['elf_vs_flash_after']=after
            session.gdb.command('-interpreter-exec console "monitor reset 0"',timeout=60)
            time.sleep(0.5)
            result['final_state']=_resume_and_confirm(session)
            # Ask the firmware itself, after the resume, whether it is executing. This is the
            # only check here that a debug-status register cannot answer wrongly.
            result['liveness']=probe_liveness(session)
    else:
        raise ValueError('Unknown probe driver: '+repr(chosen))
    if result.get('final_state')!='running':
        raise RuntimeError('Target is not running after flashing: '+str(result.get('final_state')))
    return result


def doctor(p,portable=False,write_profile=None):
    """Report whether this machine can run the profile, and how to fix it if it cannot.

    This is the migration entry point, so it answers the questions a copied profile raises
    instead of just listing paths: which profile key failed, whether the tool exists somewhere
    else on THIS machine, and what to put in the profile to make it work. A path that is
    merely absent is not an error -- it is a finding with a suggested replacement.

    ``write_profile`` is the only writing this command can do, and it is off unless the caller
    asks for it by name, so a diagnostic can never rewrite a working configuration by accident.
    """
    import piny_tools
    discovery=piny_tools.discover(p)
    tools={}
    for key,report in discovery['tools'].items():
        entry={'path':report['path'],'exists':report['exists'],'source':report['source'],
               'required':report['required'],'description':report['description'],
               'searched':report['searched']}
        if report['found']:
            entry['alternatives']=report['found'][:6]
        tools[key]=entry
    if p.get('jlink_gdb_server'):
        tools.update({k:v for k,v in piny_tools.jlink_companions(
            discovery['resolved'].get('jlink_gdb_server') or p['jlink_gdb_server']).items()})
    # A version string is the fastest way to tell "the tool is here" from "some other program
    # with the same name is here", so ask the ones that can be asked.
    for key in ('gdb','openocd'):
        actual=discovery['resolved'].get(key)
        if actual:
            try:
                cp=subprocess.run([actual,'--version'],capture_output=True,text=True,
                                  errors='replace',timeout=10,creationflags=HIDDEN)
                tools[key]['version']=(cp.stdout+cp.stderr).splitlines()[:3]
            except OSError as exc:
                tools[key]['version_error']=str(exc)
    project=Path(p['project']) if p.get('project') else None
    overrides=piny_tools.profile_overrides(discovery,p)
    report={'profile':p['name'],'profile_path':str(p.get('_profile_path') or ''),
            'project':p['project'],'project_exists':bool(project and project.is_dir()),
            'probe':discovery['probe'],'elf':str(p['_elf']),'elf_exists':p['_elf'].is_file(),
            'tools':tools,'missing_required':discovery['missing'],
            'usable':discovery['usable'] and bool(project and project.is_dir()),
            'suggested_profile_overrides':overrides,
            'note':'Tool inventory only; not proof of target connection or matching firmware.'}
    if not p.get('jlink_serial'):
        report['serial_note']=('jlink_serial is unset, so the GDB server picks whichever probe it '
                               'finds. With more than one attached the choice is not yours to make; '
                               'set it to the serial printed by "JLink.exe -CommanderScript" or '
                               'JLinkConfig. It is deliberately never guessed here.')
    if not p.get('freertos_tick_symbol'):
        resolved=tick_symbol_from_elf(p)
        note=('No freertos_tick_symbol in the profile, so the liveness check resolves xTickCount out '
              'of the ELF. ')
        if resolved:
            report['tick_symbol_resolved']=resolved
            report['tick_note']=(note+'It resolves to '+resolved+' for this image now, but the address '
                                 'moves on every rebuild, so re-run doctor after one.')
        else:
            report['tick_note']=note+('It could NOT be resolved (no ELF, no arm-none-eabi-nm beside '
                                      'the configured gdb, or xTickCount absent), which means the '
                                      'liveness check will report checked:false and a flash will '
                                      'have no proof that the board is running.')
    report['problems']=_portability_problems(p,report,discovery)
    if portable:
        report['portable']=portability_report(p,discovery,report)
    if write_profile:
        target=Path(write_profile)
        current=json.loads(target.read_text(encoding='utf-8-sig')) if target.is_file() else {}
        merged=dict(current);merged.update(overrides)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(merged,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        report['wrote_profile']={'path':str(target),'keys':sorted(overrides)}
    return report


def _portability_problems(p,report,discovery):
    """One line per thing that would stop a session on this machine, phrased as an action."""
    problems=[]
    if not report['project_exists']:
        problems.append('project directory does not exist: '+str(p['project'])+
                        ' (set "project" in the profile to the checkout on this machine)')
    for key in report['missing_required']:
        tool=report['tools'].get(key,{})
        problems.append('required tool not found: '+key+' -- '+tool.get('description','')+
                        ' (searched: '+', '.join(tool.get('searched') or [])+')')
    if report['project_exists'] and not report['elf_exists']:
        problems.append('ELF not built yet: '+str(p['_elf'])+
                        ' (build the project, or point "elf" at the right image)')
    ids=p.get('idcode_address')
    if not ids:
        problems.append('idcode_address is unset, so the device-family check before flashing '
                        'cannot run')
    return problems


def portability_report(p,discovery,doctor_report,reference=None):
    """What in this profile is bound to THIS machine, as opposed to the target.

    The distinction matters when a profile is copied: a wrong tool path is a migration bug,
    while a probe serial or a measured speed that belongs to another rig is a fact about the
    hardware and must be re-measured rather than "fixed".
    """
    machine_keys=('project','gdb','openocd','systemview','jlink_gdb_server')
    target_keys=('probe','jlink_device','jlink_serial','jlink_speed_khz','expected_device_id',
                 'idcode_address','freertos_tick_symbol','cycle_frequency_hz','ram_ranges')
    bound=[]
    for key in machine_keys:
        entry=doctor_report['tools'].get(key)
        if key=='project':
            value=p.get('project')
            bound.append({'key':key,'value':value,'kind':'machine',
                          'ok':doctor_report['project_exists'],
                          'why':'a checkout path: must point at the tree on this machine'})
            continue
        value=p.get(key)
        if value is None and key not in discovery['tools']:
            continue
        bound.append({'key':key,'value':value,'kind':'machine',
                      'ok':bool(entry and entry['exists']),
                      'source':(entry or {}).get('source'),
                      'why':'an executable path: discovery can relocate it, this profile cannot'})
    for key in target_keys:
        if key in p:
            bound.append({'key':key,'value':p[key],'kind':'target','ok':None,
                          'why':_TARGET_KEY_WHY.get(key,'a property of the rig, not of the machine')})
    return {'bound_keys':bound,
            'machine_keys':list(machine_keys),'target_keys':list(target_keys),
            'note':('"machine" entries may legitimately differ per computer and can be relocated '
                    'by discovery. "target" entries describe the board and probe: copying them to '
                    'another rig is how a plausible-looking profile produces wrong numbers, so '
                    're-verify them there instead of trusting the copy.')}


#: Why each target-side key must be re-established on a new rig rather than copied.
_TARGET_KEY_WHY={
    'probe':'which transport this rig uses; a SEGGER probe needs jlink, anything else needs openocd',
    'jlink_device':'the exact part number, from the board rather than from this file',
    'jlink_serial':'identifies ONE probe; a different probe has a different serial',
    'jlink_speed_khz':'measured per rig: too low silently drops trace events and understates Idle',
    'expected_device_id':'the device family the safety gate refuses to flash without',
    'idcode_address':'where that ID lives on this family',
    'freertos_tick_symbol':'an address inside THIS image; it moves on every rebuild',
    'cycle_frequency_hz':'the clock SystemView divides by; wrong here scales every CPU load',
    'ram_ranges':'the memory map SystemView scans for the RTT control block',
}


#: Debug Halting Control and Status Register. Read-only from the debugger's side; reading it
#: never halts the core, so the C_HALT bit tells the truth about a CPU this command found.
DHCSR=0xe000edf0
#: Debug Exception and Monitor Control Register: shows whether a vector catch or monitor is
#: armed, which a debugger can leave behind and mistake for a target fault.
DEMCR=0xe000edfc
#: Cortex-M7 CPUID (ARMv7-M, r1p2) as observed on this board's Cortex-M7.
EXPECTED_CPUID=0x411fc272
#: Low 12 bits of STM32 DBGMCU_IDC. 0x483 is STM32H72x/H73x.
EXPECTED_DEVICE_ID=0x483
#: Bits of DHCSR that describe the debug state rather than the core's own operation.
_DHCSR_BITS=((0,'C_DEBUGEN'),(1,'C_HALT'),(2,'C_STEP'),(3,'C_MASKINTS'),(17,'S_HALT'),
             (24,'S_RESET_ST'),(25,'S_RETIRE_ST'),(26,'S_LOCKUP'),(27,'S_SLEEP'),(28,'S_LOCKUP_EXT'),
             (29,'S_EXIT'),(30,'S_HALT_EXT'))
#: Gap between the two DHCSR reads used to check the core is really executing instructions.
RETIRE_PROBE_SECONDS=0.25
#: How long the RTOS tick is watched when deciding whether the firmware really executes. The
#: tick period on this board is 1 ms, so 1.5 s leaves ample room to be unambiguous while staying
#: a rounding error next to a capture: a running board accumulates ~1500 ticks, a stopped one 0.
LIVENESS_PROBE_SECONDS=1.5
#: Reset cause register of the STM32H7 RCC (RCC base 0x58024C00 + 0x0D0). READING IT CLEARS
#: THE FLAGS, so it is only read when the caller asks for it: the reset history is evidence
#: and a second read cannot recover it.
RCC_RSR=0x58024c00+0x0d0
_RCC_RSR_BITS=((16,'PINRST'),(17,'BORRST'),(18,'SFTRST'),(19,'IWDG1RST'),(20,'WWDG1RST'),
               (21,'LPWRRST'),(22,'D1RST'),(23,'D2RST'),(24,'CPURST'),(25,'D1D2RST'))


def decode_dhcsr(raw):
    """Human-readable view of DHCSR: which debug controls are set and why the core stopped."""
    return {'raw':hex(raw),'bits':{name:bool(raw>>shift&1) for shift,name in _DHCSR_BITS},
            'c_debugen':bool(raw&1),'c_halt':bool(raw>>1&1),'c_step':bool(raw>>2&1),
            's_halt':bool(raw>>17&1),'s_reset_st':bool(raw>>24&1),'s_retire_st':bool(raw>>25&1),
            's_lockup':bool(raw>>26&1),'s_sleep':bool(raw>>27&1)}


def interpret_retire_probe(first_s_retire_st,second):
    """Decide what two DHCSR reads across a delay say about whether the core executes.

    S_RETIRE_ST only sets when the CPU retires an instruction, and reading DHCSR clears it, which
    makes it the one execution signal available without halting. It is however unreliable in both
    directions: it stays clear whenever the core is halted by *any* debugger (an open IDE counts),
    and some adapters never report retirement at all. A clear bit must therefore never be turned
    into "the firmware is not running" -- the honest answers are True (proved) and None
    (inconclusive), with the reason recorded.
    """
    if second['c_halt'] or second['s_halt']:
        return {'usable':False,'cpu_executing':None,
                'note':('S_RETIRE_ST is meaningless here because the core is already halted by another '
                        'debug session; this read-only probe cannot tell whether the firmware executes')}
    if second['s_retire_st']:
        return {'usable':True,'cpu_executing':True,'note':None}
    return {'usable':True,'cpu_executing':None,
            'note':('S_RETIRE_ST never set: either the core is not executing or this adapter does not '
                    'report retirement, so this probe is inconclusive rather than negative; '
                    'registers --allow-halt is the check that settles it')}


def identify(p,out,adapter_khz=None,resume_if_halted=None,read_reset_cause=False,
             driver=None,device=None,speed_khz=None):
    """Prove the debug link works without stopping the CPU.

    Reads identity and debug-status registers over the debug port only: no ``halt``,
    ``reset``, ``program`` or Flash access is issued, so a running real-time workload keeps
    running. This is the check to run before asking for the halt that a core-register
    snapshot needs; if it fails, the debug path is broken and no register reading is honest.

    The CPU state is sampled as soon as the transport answers, before any other command, so a
    core that came up halted can be attributed: if it was running before the debug connection
    halted it, this command puts it back. A core that was already halted is reported and left
    alone, because resuming a stop this command did not cause would be its own unrequested
    change to the system under test.
    """
    chosen=driver or p.get('probe') or 'openocd'
    if adapter_khz and chosen!='openocd':
        raise ValueError('--adapter-khz is an OpenOCD setting; use --speed for J-Link (kHz)')
    if speed_khz and chosen!='jlink':
        raise ValueError('--speed is a J-Link setting; use --adapter-khz for OpenOCD (kHz)')
    _rate=int(adapter_khz or p.get('adapter_khz')) if chosen=='openocd' else int(speed_khz or p.get('jlink_speed_khz') or 4000)
    with ProbeLock() as _lock,make_session(p,out,adapter_khz,device,speed_khz,None,driver) as session:
        # OpenOCD samples the state inside `init`, before this function gets control, so its
        # pre-open state is exact. A J-Link session has no equivalent, and its first
        # read-only register access is the earliest observation available.
        is_openocd=isinstance(session.backend,OpenOCD)
        pre_open=getattr(session.backend,'state_at_connect',None) if is_openocd else None
        state=session.state()
        result={'probe':chosen,'adapter_khz':_rate,
                'halt_issued':False,'reset_issued':False,
                'device_id':None,'expected_device_id':hex(EXPECTED_DEVICE_ID),
                'matches_expected_device':None,'cpuid':None,'expected_cpuid':hex(EXPECTED_CPUID),
                'dhcsr':None,'state_at_entry':state,'state_before_probe':pre_open,
                'state_at_exit':None,'notes':[],'liveness':None}
        if state not in ('running','halted'):
            result['state_at_exit']=state
            raise RuntimeError('Target did not answer over the debug port; state is '+repr(state)+
                               '. See '+str(Path(out)/('openocd.log' if chosen=='openocd' else 'jlink-gdb-server.log')))
        # DHCSR ALONE IS NOT ENOUGH TO CALL A CORE "HALTED". MEASURED ON THIS BOARD: DHCSR
        # reported a frozen 0x30003 (C_HALT set) for a core that was executing its RTOS tick
        # at ~1000/s, and the same reading came back across sessions. Worse, a J-Link session
        # attaches with the core stopped and cannot report a pre-attach state (unlike OpenOCD,
        # whose `state_at_connect` is sampled inside `init`), so this command cannot tell
        # "the board was already stopped" from "this connection stopped it" by register alone.
        #
        # The tick settles both questions: it is written by the firmware, so it advances only
        # while the CPU really executes. Watch it for a moment and let it decide:
        #   tick moved  -> the core WAS running (this connection had halted it) and is put back;
        #   tick frozen -> the core was genuinely stopped, and it is left exactly as found.
        # The only write either way is the resume that restores a halt this command caused,
        # which `--leave-halted` suppresses.
        # A tick address in the profile is honoured, but it is no longer REQUIRED: the address
        # is a property of the image, so it is resolved from the ELF when the profile omits it.
        tick_symbol=p.get('freertos_tick_symbol') or tick_symbol_from_elf(p)
        if tick_symbol and not p.get('freertos_tick_symbol'):
            result['notes'].append('xTickCount resolved from the ELF at '+tick_symbol+
                                   ' (no freertos_tick_symbol in the profile)')
        if state=='halted' and tick_symbol and resume_if_halted is not False:
            cross=probe_liveness(session,LIVENESS_PROBE_SECONDS)
            result['liveness']=cross
            if cross.get('running'):
                result['state_at_entry']='running'
                result['state_at_exit']='running'
                result['dhcsr_note']=('DHCSR reported the core halted, but its RTOS tick advanced by '
                                      +str(cross['advanced'])+' in '+str(cross['seconds'])
                                      +'s ('+str(cross['rate_hz'])+' Hz), so the core was executing; '
                                      'the connection had stopped it and it was resumed. DHCSR is not a '
                                      'liveness witness on this probe.')
                result['notes'].append(result['dhcsr_note'])
            else:
                result['notes'].append('the RTOS tick did not advance, so the core really is stopped; '
                                       'DHCSR alone could not have told this from a running core')
                if resume_if_halted:
                    result['state_at_exit']=session.resume_confirmed()
                    result['notes'].append('resumed on request a core that was already halted')
                else:
                    result['notes'].append('the core was left stopped and no register was written. '
                                           'Pass --resume-if-halted to start it.')
        address=int(p['idcode_address'],0)
        raw=session.read_u32(address)
        result['device_id']=hex(raw&0xfff);result['revision_id']=hex(raw>>16)
        result['matches_expected_device']=(raw&0xfff==EXPECTED_DEVICE_ID)
        if not result['matches_expected_device']:
            result['notes'].append('device id is not the expected STM32H72x/H73x value')
        cpu=session.read_u32(0xe000ed00)
        result['cpuid']=hex(cpu)
        if cpu!=EXPECTED_CPUID:result['notes'].append('CPUID differs from the Cortex-M7 r1p2 seen previously')
        result['dhcsr']=decode_dhcsr(session.read_u32(DHCSR))
        demcr=session.read_u32(DEMCR)
        result['demcr']={'raw':hex(demcr),'vc_mmerr':bool(demcr>>4&1),'vc_nocperr':bool(demcr>>5&1),
                         'vc_chkerr':bool(demcr>>6&1),'vc_staterr':bool(demcr>>7&1),
                         'vc_buserr':bool(demcr>>8&1),'vc_interr':bool(demcr>>9&1),
                         'vc_harderr':bool(demcr>>10&1),'mon_en':bool(demcr>>16&1),
                         'mon_pend':bool(demcr>>17&1),'trcena':bool(demcr>>24&1)}
        # S_RETIRE_ST is the CPU's own progress counter: it only sets when the core executes
        # another instruction, and reading DHCSR clears it. So it tells whether the core is
        # really executing code, which a software register snapshot cannot otherwise show
        # without halting it.
        result['cpu_executing']=None
        if state=='running':
            first=result['dhcsr']['s_retire_st']
            time.sleep(RETIRE_PROBE_SECONDS)
            again=decode_dhcsr(session.read_u32(DHCSR))
            result['retire_probe']={'first_s_retire_st':first,'second_s_retire_st':again['s_retire_st'],
                                    'c_halt_after':again['c_halt']}
            verdict=interpret_retire_probe(first,again)
            result['retire_probe'].update({k:v for k,v in verdict.items() if k!='note'})
            result['cpu_executing']=verdict['cpu_executing']
            if verdict['note']:
                result['note_retire']=verdict['note']
                result['notes'].append(verdict['note'])
        result['notes'].append('read-only debug identity check: no halt, no reset, no Flash access')
        if read_reset_cause:
            rsr=session.read_u32(RCC_RSR)
            result['reset_cause']={'address':hex(RCC_RSR),'raw':hex(rsr),
                                   'flags':{name:bool(rsr>>shift&1) for shift,name in _RCC_RSR_BITS},
                                   'cleared_by_this_read':True,
                                   'note':('RCC_RSR is read-to-clear: these flags are now gone. '
                                           'IMPORTANT: it says what reset the CPU last took, not '
                                           'whether it is being held in reset now')}
            if rsr==0:
                result['notes'].append('no reset flag was set in RCC_RSR, so this read cannot explain '
                                       'the core state and a further reset history is unrecoverable')
        result['state_at_exit']=session.state()
        # A core that the liveness probe just watched execute must not be relabelled "halted" by
        # the very register that was already shown to lie here -- and relabelling it would then
        # send the block below off to resume a core that is already running.
        if result['liveness'] and result['liveness'].get('running'):
            result['state_at_exit']='running'
        elif result['state_at_exit']=='halted':
            if pre_open=='running':
                result['notes'].append('the core was running before the debug connection and halted by it')
                if resume_if_halted is not False:
                    result['state_at_exit']=session.resume_confirmed()
                    result['notes'].append('resumed the core that the connection had halted')
            else:
                if resume_if_halted:
                    result['state_at_exit']=session.resume_confirmed()
                    result['notes'].append('resumed on request a core that was already halted')
                else:
                    result['notes'].append('the core was already halted before this command; it was left halted '
                                           'and no register was written. Pass --resume-if-halted to start it.')
        if result['state_at_exit']!=result['state_at_entry']:
            result['notes'].append('WARNING: target state changed during this command '
                                   '('+str(result['state_at_entry'])+' -> '+str(result['state_at_exit'])+')')
    return result


def license_check(p):
    """Prove the SystemView license dialog can be answered without human input.

    Launches SystemView on the bundled sample recording and reports what happened to the
    modal; nothing is exported and no target is touched.
    """
    import piny_view
    systemview=tool_path(p,'systemview')
    sample=Path(systemview).parent/'Sample'/'OS_IP_WebServer.SVDat'
    report={'executable':systemview,'sample':str(sample),'sample_exists':sample.is_file(),
            'bridge_available':piny_view.available()}
    if not sample.is_file():raise ValueError('Bundled SystemView sample recording not found: '+str(sample))
    argv=[systemview,'-single','-port',str(free_port()),'-load',str(sample)]
    proc=subprocess.Popen(argv,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        report['argv']=argv
        report['license']=piny_view.accept_license(proc.pid,timeout=30)
        report['main_window']=[w['title'] for w in piny_view.windows(proc.pid)]
        report['ok']=bool(report['license']['answered'])
        if not report['ok']:
            report['note']=('license dialog not answered; SystemView CLI parsing stays blocked. '
                            'Set --no-auto-accept-license to answer it by hand.')
    finally:
        stop_process(proc)
        report['killed']=True
    return report


def error_chain(exc):
    chain=[];seen=set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc));chain.append(type(exc).__name__+': '+str(exc))
        exc=exc.__cause__ or exc.__context__
    return chain


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',action='version',version='piny_debug.py '+__version__)
    parser.add_argument('--profile',type=Path,default=ROOT/'profiles'/'pinycore.json')
    parser.add_argument('--output',type=Path)
    sub=parser.add_subparsers(dest='action',required=True)
    probe=argparse.ArgumentParser(add_help=False)
    probe.add_argument('--probe',choices=('openocd','jlink'),default=None,
                       help='debug transport (default: the profile probe); jlink starts SEGGER\'s own GDB server')
    probe.add_argument('--device',default=None,help='J-Link target device name (default: the profile jlink_device)')
    probe.add_argument('--speed',type=int,default=None,dest='speed_khz',
                       help='J-Link interface speed in kHz (default: the profile jlink_speed_khz)')
    doc=sub.add_parser('doctor',help='report whether this machine can run the profile, and how to fix it')
    doc.add_argument('--portable',action='store_true',
                     help='also list which profile keys belong to this machine and which belong to the rig')
    doc.add_argument('--write-profile',type=Path,default=None,metavar='PATH',
                     help='write the discovered tool paths into PATH (asked for by name; never automatic)')
    sub.add_parser('license-check')
    ident=sub.add_parser('identify',help='read device identity over the debug port without halting the CPU',
                         parents=[probe])
    ident.add_argument('--adapter-khz',type=int,default=None,help='override the profile SWD clock')
    resume=ident.add_mutually_exclusive_group()
    resume.add_argument('--resume-if-halted',dest='resume_if_halted',action='store_true',default=None,
                        help='start the core even if it was already halted before this command')
    resume.add_argument('--leave-halted',dest='resume_if_halted',action='store_false',
                        help='never resume, even a core this connection halted itself')
    ident.add_argument('--reset-cause',action='store_true',
                       help='also read RCC_RSR for the last reset cause (read-to-clear!)')
    ident.add_argument('--allow-peripheral-read',action='store_true',
                       help='required together with --reset-cause: RCC is a peripheral register')
    regs=sub.add_parser('registers',parents=[probe]);regs.add_argument('--allow-halt',action='store_true',required=True)
    regs.add_argument('--symbol',action='append',default=[])
    regs.add_argument('--memory',nargs=2,type=lambda x:int(x,0),metavar=('ADDRESS','BYTES'))
    regs.add_argument('--allow-peripheral-read',action='store_true')
    fl=sub.add_parser('flash',parents=[probe],help='program the ELF and leave the target running')
    fl.add_argument('--allow-flash',action='store_true',required=True)
    cap=sub.add_parser('capture');cap.add_argument('--seconds',type=float,default=5)
    cap.add_argument('--allow-halt',action='store_true',required=True)
    cap.add_argument('--allow-rtt-write',action='store_true',required=True)
    cap.add_argument('--no-auto-accept-license',action='store_true',
                     help='leave the SystemView license dialog for a human to answer')
    return parser


def main():
    parser=build_parser()
    args=parser.parse_args();p=load_profile(args.profile)
    out=args.output or ROOT/'artifacts'/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    out=out.resolve();out.mkdir(parents=True,exist_ok=True)
    start=time.time()
    try:
        if args.action=='doctor':data=doctor(p,args.portable,args.write_profile)
        elif args.action=='license-check':data=license_check(p)
        elif args.action=='identify':
            if args.reset_cause and not args.allow_peripheral_read:
                raise ValueError('--reset-cause reads RCC, a peripheral register: '
                                 'pass --allow-peripheral-read as well')
            data=identify(p,out,args.adapter_khz,args.resume_if_halted,args.reset_cause,
                          args.probe,args.device,args.speed_khz)
        elif args.action=='registers':
            data=snapshot(p,out,args.symbol,args.memory,args.allow_peripheral_read,
                          args.probe,args.device,args.speed_khz)
        elif args.action=='flash':data=flash(p,out,args.probe,args.device,args.speed_khz)
        else:
            from piny_trace import capture
            data=capture(p,out,args.seconds,not args.no_auto_accept_license)
        result={'ok':True,'action':args.action,'output':str(out),'elapsed_seconds':round(time.time()-start,3),**{k:v for k,v in data.items() if not k.startswith('_')}}
        code=0
    except (Exception,KeyboardInterrupt) as exc:
        result={'ok':False,'action':args.action,'output':str(out),'error':type(exc).__name__+': '+str(exc),'error_chain':error_chain(exc),'elapsed_seconds':round(time.time()-start,3)}
        code=1
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2));return code

if __name__=='__main__':sys.exit(main())
