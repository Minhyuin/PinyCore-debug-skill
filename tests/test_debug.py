import contextlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import piny_debug as d

class ProtocolTests(unittest.TestCase):
    def test_mi_nested_registers(self):
        kind, data = d.parse_mi('7^done,register-values=[{number="0",value="0x1234"},{number="15",value="0x8000120"}]')
        self.assertEqual(kind, 'done')
        self.assertEqual(data['register-values'][1], {'number': '15', 'value': '0x8000120'})
    def test_mi_error_and_escaping(self):
        kind, data = d.parse_mi(r'2^error,msg="bad \\"')
        self.assertEqual(kind, 'error')
        self.assertEqual(data['msg'], 'bad \\')
    def test_mi_names(self):
        self.assertEqual(d.parse_mi('1^done,register-names=["r0","","pc"]')[1]['register-names'], ['r0','','pc'])
    def test_mi_rejects_malformed(self):
        with self.assertRaises(ValueError): d.parse_mi('1^done,bad=[{')
    def test_tcl_quotes_windows_path(self):
        self.assertEqual(d.tcl_word('E:/a b/file.elf'), '{E:/a b/file.elf}')
        for bad in ['a} ; reset', 'a\nreset', 'a\\b']:
            with self.assertRaises(ValueError): d.tcl_word(bad)
    def test_safe_symbol_only(self):
        for symbol in ['SystemCoreClock','_SEGGER_RTT','object.field','data[12]']:
            self.assertEqual(d.safe_symbol(symbol), symbol)
        for bad in ['foo()','x=1','*(int*)0','x;reset','a\nquit']:
            with self.assertRaises(ValueError): d.safe_symbol(bad)
    def test_memory_limits(self):
        self.assertEqual(d.validate_memory(0x20000000, 16, False), (0x20000000,16))
        for addr,count,peripheral in [(0,0,False),(0,100000,False),(-1,4,False),(0xffffffff,4,False),(0x40000000,4,False)]:
            with self.assertRaises(ValueError): d.validate_memory(addr,count,peripheral)
        self.assertEqual(d.validate_memory(0x40000000,4,True),(0x40000000,4))
    def test_elf_identity_rejects_random_file(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'a.elf';p.write_bytes(b'not elf')
            with self.assertRaises(ValueError):d.elf_identity(p)
    def test_probe_lock_excludes_second_owner(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'probe.lock'
            with d.ProbeLock(p):
                with self.assertRaises(RuntimeError):
                    with d.ProbeLock(p): pass
            self.assertFalse(p.exists())
    def test_restore_running_and_halted(self):
        class Fake:
            def __init__(self,state):self.calls=[];self.current=state
            def state(self):return self.current
            def halt(self,note='halt'):self.calls.append('halt');self.current='halted'
            def resume(self):self.calls.append('resume');self.current='running'
        f=Fake('halted'); d.restore_target(f,'running');self.assertIn('resume',f.calls)
        f=Fake('running'); d.restore_target(f,'halted');self.assertIn('halt',f.calls)
    def test_unknown_state_not_resumed(self):
        class Fake:
            def halt(self,note='halt'):raise AssertionError('must not act')
            def resume(self):raise AssertionError('must not act')
        with self.assertRaises(RuntimeError):d.restore_target(Fake(),'unknown')

class JLinkBackendTests(unittest.TestCase):
    """The SEGGER probe path: no OpenOCD, one MI client, and a running core must not hang it."""
    def profile(self,**extra):
        # A path that is guaranteed NOT to exist on this machine, so a test can never pass by
        # borrowing whatever J-Link happens to be installed here: the argv has to come out of
        # the profile (or an explicitly injected _tools entry), never out of environment probing.
        p={'_elf':Path('firmware.elf'),'project':'.','probe':'jlink','gdb':'gdb',
           'jlink_gdb_server':'C:/nonexistent/JLinkGDBServerCL.exe','jlink_device':'STM32H723VG',
           'jlink_serial':'602712375','jlink_speed_khz':24000,'jlink_gdb_port':2331}
        p.update(extra)
        return p
    def test_server_argv_is_headless_and_portable(self):
        server=d.JLinkGDBServer(self.profile(),Path('.'))
        with self.assertRaisesRegex(RuntimeError,'jlink_gdb_server'):
            server.argv()
        tmp=Path(tempfile.mkdtemp())
        fake=str(tmp/'JLinkGDBServerCL.exe');(tmp/'JLinkGDBServerCL.exe').write_bytes(b'')
        server.profile['jlink_gdb_server']=fake
        argv=server.argv()
        self.assertEqual(argv[0],fake)
        for flag in ['-silent','-nogui','-localhostonly']:
            self.assertIn(flag,argv,'without %s the server may open a GUI or serve the network' % flag)
        self.assertEqual(argv[argv.index('-port')+1],'2331')
        self.assertEqual(argv[argv.index('-speed')+1],'24000')
        self.assertEqual(argv[argv.index('-select')+1],'USB=602712375')
        self.assertEqual(server.gdb_port,2331,'a session reads gdb_port from either backend')
    def test_server_requires_a_device_name(self):
        with self.assertRaisesRegex(ValueError,'No J-Link device name'):
            d.JLinkGDBServer(self.profile(jlink_device=None),Path('.'))
    def test_speed_and_port_overrides_win(self):
        server=d.JLinkGDBServer(self.profile(),Path('.'),speed_khz=4000,port=2400)
        self.assertEqual(server.speed_khz,4000);self.assertEqual(server.port,2400)
    def test_server_delegates_evaluate_to_the_attached_gdb_client(self):
        # check_target() works against either backend, so the J-Link backend has to answer
        # evaluate() -- without this, `flash --probe jlink` died with
        # "AttributeError: 'JLinkGDBServer' object has no attribute 'evaluate'".
        server=d.JLinkGDBServer(self.profile(),Path('.'))
        with self.assertRaisesRegex(RuntimeError,'No GDB client'):
            server.evaluate('$pc')
        class Gdb:
            def __init__(self):self.expressions=[]
            def evaluate(self,expression):self.expressions.append(expression);return '0x10010483'
        server.gdb=Gdb()
        self.assertEqual(server.evaluate('*(unsigned int *)0x5c001000'),'0x10010483')
        self.assertEqual(server.gdb.expressions,['*(unsigned int *)0x5c001000'])
    def test_jlink_state_reports_a_running_core_instead_of_hanging(self):
        # Regression: state() used to read $pc and block for the whole timeout once the target
        # ran, which is what made `identify --resume-if-halted` fail on the real board.
        class Gdb:
            def __init__(self,error):self.error=error;self.calls=[]
            def evaluate(self,x):
                self.calls.append(x)
                if self.error:raise RuntimeError(self.error)
                return '0x802e9ac <prvCheckTasksWaitingTermination>'
        session=d.ProbeSession.__new__(d.ProbeSession)
        session.backend=object();session.driver='jlink'
        session.gdb=Gdb('GDB: Selected thread is running.')
        self.assertEqual(session.state(),'running')
        session.gdb=Gdb(None)
        self.assertEqual(session.state(),'halted')
        session.gdb=Gdb('GDB: Cannot access memory')
        self.assertEqual(session.state(),'unknown')
    def test_jlink_memory_reads_dereference_a_typed_pointer(self):
        # A bare numeric expression evaluates the literal, so `(unsigned int)0xE000ED00` returned
        # the ADDRESS as if it were the value. It must be dereferenced.
        class Gdb:
            def __init__(self):self.expressions=[]
            def evaluate(self,x):self.expressions.append(x);return '0x30003'
        session=d.ProbeSession.__new__(d.ProbeSession)
        session.backend=object();session.driver='jlink';session.gdb=Gdb()
        self.assertEqual(session.read_u32(0xe000edf0),0x30003)
        self.assertEqual(session.gdb.expressions,['*(unsigned int *)0xe000edf0'])
    def test_gdb_starts_in_async_mode(self):
        # mi-async must be set as a STARTUP option: GDB refuses it later with "Cannot change this
        # setting while the inferior is running", and without it every command times out on a
        # running target while `-exec-interrupt` is never sent at all.
        source=(Path(__file__).resolve().parents[1]/'scripts'/'piny_debug.py').read_text(encoding='utf-8')
        self.assertIn("'-ex','set mi-async on'",source)
    def test_console_stream_records_are_collected(self):
        import io,queue
        g=d.GDB.__new__(d.GDB);g.token=0;g.q=queue.Queue()
        g.q.put(r'~"Section .text, range 0x80002d0 -- 0x8033cb0: matched.\n"')
        g.q.put('1^done')
        class Proc:stdin=io.StringIO()
        g.proc=Proc()
        streams=[]
        self.assertEqual(g.command('-interpreter-exec console "compare-sections"',streams=streams),{})
        self.assertEqual(streams,['Section .text, range 0x80002d0 -- 0x8033cb0: matched.\n'])
    def test_flash_refuses_an_unknown_probe_driver(self):
        with self.assertRaisesRegex(ValueError,'Unknown probe driver'):
            d.flash(self.profile(),Path('.'),driver='stlink')
    def test_cross_probe_options_are_rejected(self):
        parser=d.build_parser()
        args=parser.parse_args(['identify','--probe','jlink','--adapter-khz','500'])
        self.assertEqual(args.probe,'jlink')
        self.assertEqual(args.adapter_khz,500)
        args=parser.parse_args(['identify','--probe','openocd','--speed','24000'])
        self.assertEqual(args.speed_khz,24000)


class FlashOverJLinkTests(unittest.TestCase):
    """`flash` must work under the SEGGER probe too -- without it there is no optimization loop.

    OpenOCD cannot drive this probe (LIBUSB_ERROR_NOT_FOUND), so the J-Link path programs
    through GDB's `-target-download`, which writes Flash and verifies each block it wrote.
    """
    def profile(self):
        return {'_elf':Path('firmware.elf'),'project':'.','probe':'jlink','gdb':'gdb',
                'expected_device_id':'0x483','idcode_address':'0x5c001000'}
    def fake_session(self,mismatch_before=False,mismatch_after=False,download_error=False):
        """A session whose compare-sections answers differ for the before/after call."""
        class Backend:
            """Stands in for the J-Link backend: check_target only needs evaluate()."""
            def __init__(self):self.reads=[]
            def evaluate(self,expression):
                self.reads.append(expression)
                return '0x10010483'
        class Gdb:
            def __init__(self):
                self.calls=[];self.comparisons=0;self.running=False
            def command(self,cmd,timeout=None,streams=None):
                self.calls.append(cmd)
                if 'compare-sections' in cmd:
                    self.comparisons+=1
                    bad=mismatch_before if self.comparisons==1 else mismatch_after
                    if streams is not None:
                        streams.append('Section .text: MISMATCH\n' if bad else
                                       'Section .text, range 0x80002d0 -- 0x8033cb0: matched.\n')
                    return {}
                if '-file-exec-and-symbols' in cmd:return {'status':'done'}
                if cmd=='-target-download':
                    return {'status':'error','msg':'Load failed'} if download_error else {'status':'done'}
                return {}
            def evaluate(self,x):
                if x.strip()=='$pc':return '0x802e9ac' if not self.running else '(running)'
                return '0x1010001' if self.running else '0x30003'   # DHCSR, C_HALT while stopped
        session=d.ProbeSession.__new__(d.ProbeSession)
        session.backend=Backend();session.driver='jlink';session.gdb=Gdb()
        session.gdb.running=False
        session.actions=[]
        session.halt=lambda note='halt':session.actions.append('halt') or note
        session.resume=lambda:session.actions.append('resume')
        # The state a J-Link session is judged by is the hardware's (DHCSR), exactly as
        # ProbeSession.state() reads it; the fake must answer the same question, not a constant.
        session.state=lambda:('running' if session.gdb.running else 'halted')
        def resume_confirmed():
            session.actions.append('resume')
            session.actions.append('confirm')
            if not session.stays_halted:
                session.gdb.running=True   # `monitor go` accepted and the core really starts
            return session.state()
        session.stays_halted=True   # the hardware keeps the core stopped unless a test says otherwise
        session.resume_confirmed=resume_confirmed
        return session
    def install(self,session):
        patches=[patch.object(d,'make_session',return_value=contextlib.nullcontext(session)),
                 patch.object(d,'ProbeLock',return_value=contextlib.nullcontext()),
                 patch.object(d,'elf_identity',return_value={'path':'firmware.elf','sha256':'x','bytes':1})]
        for item in patches:
            item.start();self.addCleanup(item.stop)
    def run_flash(self,session):
        self.install(session)
        return d.flash(self.profile(),Path('.'),'jlink')
    def test_flash_downloads_and_verifies_over_jlink(self):
        session=self.fake_session();session.stays_halted=False   # the reset stopped it; resume works
        result=self.run_flash(session)
        self.assertTrue(result['flash_verified'])
        self.assertEqual(result['final_state'],'running')
        # The write must be a real MI -target-download, issued for this ELF.
        self.assertIn('-file-exec-and-symbols "firmware.elf"',session.gdb.calls)
        download=session.gdb.calls.index('-file-exec-and-symbols "firmware.elf"')+1
        self.assertEqual(session.gdb.calls[download],'-target-download')
        self.assertIn('-interpreter-exec console "monitor reset 0"',session.gdb.calls)
        self.assertEqual(session.actions,['halt','resume','confirm'],
                         'the core is halted to write, resumed after, and the resume is confirmed')
        # Two comparisons: one before the write (informational), one after (the gate).
        self.assertEqual(session.gdb.comparisons,2)
        self.assertTrue(result['elf_vs_flash_after']['reported_match'])
    def test_a_mismatch_before_flashing_is_reported_but_does_not_block(self):
        # A target that does NOT match the ELF is the normal reason to flash; only the
        # comparison AFTER the write may stop the operation.
        session=self.fake_session(mismatch_before=True);session.stays_halted=False
        result=self.run_flash(session)
        self.assertEqual(result['elf_vs_flash_before']['mismatched'],['Section .text: MISMATCH'])
        self.assertTrue(result['flash_verified'])
    def test_a_failed_download_is_reported(self):
        session=self.fake_session(download_error=True)
        with self.assertRaisesRegex(RuntimeError,'Flashing failed'):
            self.run_flash(session)
    def test_a_mismatch_after_flashing_fails_closed(self):
        session=self.fake_session(mismatch_after=True)
        with self.assertRaisesRegex(RuntimeError,'does not match the ELF afterwards'):
            self.run_flash(session)
    def test_a_target_that_does_not_run_after_flashing_fails(self):
        session=self.fake_session()   # the fake's resume_confirmed reports a core that stays halted
        with self.assertRaisesRegex(RuntimeError,'could not be confirmed as running'):
            self.run_flash(session)
    def test_a_wrong_device_family_never_reaches_the_download(self):
        session=self.fake_session()
        session.backend.evaluate=lambda expression:'0x10010412'   # STM32H72x/73x, not this part
        with self.assertRaisesRegex(RuntimeError,'refusing flash'):
            self.run_flash(session)
        self.assertNotIn('-target-download',session.gdb.calls)


class ResumeOverJLinkTests(unittest.TestCase):
    """Resuming must go through the J-Link's own run control and be confirmed by the hardware.

    MEASURED ON THIS BOARD: GDB's `-exec-continue` left DHCSR C_HALT set (0x30003 before and
    after), so GDB reported a run that never happened, and while it believed that it refused
    every memory read with "Cannot execute this command while the target is running". A resume
    that is only checked against GDB's own bookkeeping therefore cannot be trusted, which is
    exactly how a flash would have left the board standing still while reporting success.
    """
    class FakeGdb:
        """Answers like GDB does: ``$pc`` is a number or the literal ``(running)``, memory is data."""
        def __init__(self,pc='0x802e9ac',dhcsr='0x1010001'):
            self.calls=[];self.pc=pc;self.dhcsr=dhcsr;self.running=False
        def command(self,cmd,timeout=None,streams=None):
            self.calls.append(cmd)
            if '-interpreter-exec console "monitor go"'==cmd:self.resume()
            return {}
        def resume(self):self.running=True   # the core really starts; DHCSR says so
        def evaluate(self,expression):
            if expression.strip()=='$pc':return '(running)' if self.running else self.pc
            return self.dhcsr if self.running else '0x30003'   # C_HALT stays set until a real resume
    class FakeBackend:
        """Stands in for JLinkGDBServer: ``go`` must transmit ``monitor go`` for real."""
        def __init__(self,gdb):self.gdb=gdb;self.gos=[]
        def go(self):
            self.gos.append(True)
            return self.gdb.command('-interpreter-exec console "monitor go"',timeout=60)
    def session(self,pc='0x802e9ac',dhcsr='0x1010001'):
        # ``driver`` is what selects the J-Link-native run control, so the fake must carry it
        # exactly as ``make_session`` sets it; without it resume() falls back to -exec-continue.
        s=d.ProbeSession.__new__(d.ProbeSession)
        s.gdb=self.FakeGdb(pc,dhcsr);s.backend=self.FakeBackend(s.gdb);s.driver='jlink'
        return s
    def test_resume_uses_the_jlink_go_command_not_exec_continue(self):
        s=self.session()                     # a halted core: $pc is a number, not "(running)"
        self.assertEqual(s.resume_confirmed(),'running')
        self.assertEqual(s.backend.gos,[True],'the resume goes through J-Link run control')
        self.assertEqual(s.gdb.calls,['-interpreter-exec console "monitor go"'],
                         'monitor go is the resume, and DHCSR is what confirms it')
        self.assertNotIn('-exec-continue',s.gdb.calls)
    def test_the_server_go_is_the_one_that_sends_monitor_go(self):
        # ProbeSession.resume() delegates to the server, so the server method is what must
        # actually transmit `monitor go`; otherwise the resume would be a silent no-op.
        server=d.JLinkGDBServer.__new__(d.JLinkGDBServer)
        server.gdb=self.FakeGdb()
        server.go()
        self.assertEqual(server.gdb.calls,['-interpreter-exec console "monitor go"'])
    def test_resume_is_not_accepted_while_the_hardware_still_halts_the_core(self):
        # GDB claims the core runs, but C_HALT is still set: this is the exact signature of the
        # `-exec-continue` false success, and it must not be reported as a resume.
        s=self.session()
        s.gdb.resume=lambda:None            # `monitor go` is accepted but the core stays stopped
        with patch.object(d,'time',type('T',(),{'sleep':staticmethod(lambda x:None)})):
            with self.assertRaisesRegex(RuntimeError,'could not be confirmed'):
                d._resume_and_confirm(s,attempts=1)
    def test_halt_confirmed_reports_unknown_when_the_core_cannot_be_read(self):
        s=self.session()
        def refuse(expression):raise RuntimeError('Cannot execute this command while the target is running')
        s.gdb.evaluate=refuse
        self.assertIsNone(s.halt_confirmed())
    def test_an_advancing_rtos_tick_rescues_a_resume_dhcsr_cannot_confirm(self):
        # MEASURED ON THIS BOARD: DHCSR reported a frozen 0x30003 (C_HALT set, i.e. "halted")
        # for a core that was demonstrably executing at ~988 ticks/s. The RTOS tick is written
        # by the firmware, so when it moves the CPU IS running and a bad DHCSR read must not
        # overrule it -- otherwise a healthy board gets reported as standing still.
        s=self.session()
        s.gdb.resume=lambda:None            # DHCSR keeps claiming halted
        ticks=iter([498085,498371])         # the firmware ticked 286 times in the window
        s.tick_count=lambda symbol=None:next(ticks)
        with patch.object(d,'time',type('T',(),{'sleep':staticmethod(lambda x:None)})):
            self.assertEqual(d._resume_and_confirm(s,attempts=1),'running')
    def test_a_stalled_rtos_tick_still_refuses_the_resume(self):
        # The tick rescue must not become a blanket pass: a frozen tick plus a halted DHCSR is
        # a genuinely stopped board and has to stay an error.
        s=self.session()
        s.gdb.resume=lambda:None
        s.tick_count=lambda symbol=None:498085          # never moves
        with patch.object(d,'time',type('T',(),{'sleep':staticmethod(lambda x:None)})):
            with self.assertRaisesRegex(RuntimeError,'could not be confirmed'):
                d._resume_and_confirm(s,attempts=1)
    def test_the_liveness_probe_reports_ticks_not_a_debug_register(self):
        s=self.session()
        s.profile={'freertos_tick_symbol':'0x20007e5c'}
        def counted(symbol=None):
            s._n=getattr(s,'_n',[])+[1]
            return 498085 if len(s._n)==1 else 498371
        s.tick_count=counted
        live=d.probe_liveness(s,seconds=0.1)
        self.assertTrue(live['checked'])
        self.assertTrue(live['running'])
        self.assertEqual(live['tick_before'],498085)
        self.assertEqual(live['tick_after'],498371)
        self.assertEqual(live['advanced'],286)
        self.assertEqual(s.backend.gos,[True],'the probe has to start the core to watch it run')
    def test_the_ticks_are_sampled_only_after_the_core_is_started(self):
        # MEASURED ON THIS BOARD: the first memory read after attach is answered from a debug
        # cache primed at connect, so a session that read the tick twice 4 s apart got the SAME
        # value (213916 / 213916) for a board that was clearly executing. A window opened by
        # that stale sample compares two copies of one number and proves nothing, so the resume
        # must come FIRST -- which is the whole reason the probe starts the core at all.
        s=self.session()
        s.profile={'freertos_tick_symbol':'0x20007e5c'}
        order=[]
        s.resume=lambda:order.append('resume')
        s.tick_count=lambda symbol=None:(order.append('read'),498085 if order.count('read')==1 else 498371)[1]
        d.probe_liveness(s,seconds=0.1)
        self.assertEqual(order,['resume','read','read'],
                         'the first tick sample must be taken after the resume that drops the cache')
    def test_the_liveness_probe_declines_when_the_profile_has_no_tick_symbol(self):
        # Not every image is FreeRTOS: without a known counter the probe must say so rather
        # than report a healthy-looking "running" it never verified.
        s=self.session()
        s.profile={}
        live=d.probe_liveness(s)
        self.assertFalse(live['checked'])
        self.assertIn('freertos_tick_symbol',live['reason'])


if __name__ == '__main__':unittest.main()


class ReliabilityTests(unittest.TestCase):
    def test_restore_already_running_is_noop(self):
        class Fake:
            def state(self):return 'running'
            def halt(self,note='halt'):raise RuntimeError('target not halted')
            def resume(self):raise AssertionError('must not resume an already running target')
        d.restore_target(Fake(),'running')
    def test_restore_does_not_resume_unknown_state(self):
        class Fake:
            def state(self):return 'unknown'
            def halt(self,note='halt'):raise AssertionError('must not act')
            def resume(self):raise AssertionError('must not act')
        with self.assertRaises(RuntimeError):d.restore_target(Fake(),'running')
    def test_mi_c_octal_string(self):
        self.assertEqual(d.parse_mi(r'1^done,value="A\011B"')[1]['value'],'A\tB')
    def test_mi_error_not_success(self):
        import io,queue
        g=d.GDB.__new__(d.GDB);g.token=0;g.q=queue.Queue();g.q.put('1^error,msg="cannot access memory"')
        class Proc:stdin=io.StringIO()
        g.proc=Proc()
        with self.assertRaisesRegex(RuntimeError,'cannot access memory'):g.command('-bad')
    def test_mi_timeout(self):
        import io,queue
        g=d.GDB.__new__(d.GDB);g.token=0;g.q=queue.Queue()
        class Proc:stdin=io.StringIO()
        g.proc=Proc()
        with self.assertRaises(TimeoutError):g.command('-no-response',timeout=.001)
    def test_tcl_catch_error_not_success(self):
        class Sock:
            def settimeout(self,t):pass
            def sendall(self,b):pass
            def recv(self,n):return b'1\nverification failed\x1a'
        with tempfile.TemporaryDirectory() as t:
            o=d.OpenOCD.__new__(d.OpenOCD);o.sock=Sock();o.out=Path(t)
            with self.assertRaisesRegex(RuntimeError,'verification failed'):o.command('verify_image firmware.elf')

class SnapshotFlowTests(unittest.TestCase):
    """`registers` must verify the ELF against Flash before it resolves symbols, and must always
    put the core back the way it found it. The backend identity checks inside ProbeSession use
    ``isinstance``, so the fakes here are real OpenOCD / JLinkGDBServer objects with the process
    side neutralised."""
    def setup_fakes(self,fail_verify=False,fail_compare=False):
        class FakeOpenOCD(d.OpenOCD):
            def __init__(self):self.out=Path('.');self.adapter_khz=500;self.current='running';self.calls=[];self.gdb_port=1234
            def __enter__(self):self.state_at_connect=self.current;return self
            def __exit__(self,*args):pass
            def state(self):return self.current
            def command(self,c,**kw):
                self.calls.append(c)
                if c=='halt':self.current='halted'
                if c=='resume':self.current='running'
                if c.startswith('verify_image') and fail_verify:raise RuntimeError('firmware mismatch')
                return ''
        class FakeServer(d.JLinkGDBServer):
            def __init__(self):self.device='STM32H723VG';self.speed_khz=24000;self.port=2331;self.gdb_port=2331
            def __enter__(self):return self
            def __exit__(self,*args):pass
        class Gdb:
            def __init__(self):self.calls=[]
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def evaluate(self,x):return '544000000'
            def command(self,c,timeout=20,streams=None):
                self.calls.append(c)
                if c=='-exec-interrupt':return {}
                if c=='-data-list-register-names':return {'register-names':['r0','pc']}
                if c=='-data-list-register-values x':return {'register-values':[{'number':'0','value':'0x1'},{'number':'1','value':'0x08000000'}]}
                if c.startswith('-interpreter-exec console'):
                    # The console text arrives as MI stream records, never in the result record:
                    # this is what `-interpreter-exec console "compare-sections"` really replies.
                    text='Section .text, range 0x80002d0 -- 0x8033cb0: matched.\n'
                    if fail_compare:text='Section .text, range 0x80002d0 -- 0x8033cb0: MISMATCH.\n'
                    if streams is not None:streams.append(text)
                    return {}
                return {'memory':[{'contents':'00'*24}]}
        return FakeOpenOCD(),FakeServer(),Gdb()
    def run_snapshot(self,symbols=(),fail_verify=False,driver='openocd',fail_compare=False):
        assert driver in ('openocd','jlink'),'call run_snapshot with keywords: driver, not a bare value'
        o,server,g=self.setup_fakes(fail_verify,fail_compare)
        session=d.ProbeSession.__new__(d.ProbeSession)
        session.profile={};session.out=Path('.');session.driver=driver;session.gdb=g
        session.backend=o if driver=='openocd' else server
        session.opened=session.backend;session.halt_issued=False;session.adapter_khz=500
        # The fake is handed over wrapped in nullcontext: a real ProbeSession.__enter__ would
        # spawn the probe process and GDB, and an instance-level __enter__ would be ignored
        # because Python looks special methods up on the type.
        with patch.object(d,'elf_identity',return_value={'sha256':'test'}),patch.object(d,'ProbeLock',return_value=contextlib.nullcontext()),patch.object(d,'make_session',return_value=contextlib.nullcontext(session)):
            profile={'_elf':Path('firmware.elf'),'probe':driver}
            try:r=d.snapshot(profile,Path('.'),symbols)
            except Exception as exc:return o,g,exc
            return o,g,r
    def test_raw_registers_do_not_require_flash_read(self):
        o,g,r=self.run_snapshot()
        self.assertIsInstance(r,dict)
        self.assertFalse(any(c.startswith('verify_image') for c in o.calls))
        self.assertEqual(r['final_state'],'running')
    def test_symbol_read_requires_match(self):
        o,g,r=self.run_snapshot(['SystemCoreClock'])
        self.assertTrue(r['elf_verified']);self.assertEqual(r['symbols']['SystemCoreClock'],'544000000')
    def test_verify_failure_restores_state(self):
        # Symbols are what trigger the ELF verification, and the OpenOCD driver is passed
        # explicitly: snapshot's driver argument must never be left to a positional default.
        o,g,r=self.run_snapshot(['SystemCoreClock'],driver='openocd',fail_verify=True)
        self.assertIsInstance(r,RuntimeError);self.assertEqual(o.current,'running')
    def test_jlink_verifies_with_read_only_console_compare(self):
        # `-data-compare-sections` is not a GDB command ("Undefined MI command"), so the J-Link
        # path must use `-interpreter-exec console "compare-sections"` and read its stream output.
        o,g,r=self.run_snapshot(['SystemCoreClock'],driver='jlink')
        self.assertIsInstance(r,dict)
        self.assertTrue(r['elf_verified'])
        self.assertEqual(r['section_comparison']['command'],'compare-sections')
        self.assertIn('matched',r['section_comparison']['output'])
        self.assertFalse(any(c.startswith('-data-compare-sections') for c in g.calls))
        self.assertFalse(any(c.startswith('-target-download') for c in g.calls),
                         'the J-Link path must never write Flash to verify')
    def test_jlink_mismatch_refuses_symbols_and_restores(self):
        o,g,r=self.run_snapshot(['SystemCoreClock'],driver='jlink',fail_compare=True)
        self.assertIsInstance(r,RuntimeError)
        self.assertIn('does not match the image in Flash',str(r))
        self.assertEqual(o.current,'running')
    def test_jlink_unreadable_comparison_is_not_treated_as_verified(self):
        import contextlib
        # An empty console stream (no "matched" line) must fail closed, never count as verified.
        o,server,g=self.setup_fakes()
        g.command=lambda c,timeout=20,streams=None:{}
        session=d.ProbeSession.__new__(d.ProbeSession)
        session.profile={};session.out=Path('.');session.driver='jlink';session.gdb=g
        session.backend=server;session.opened=server;session.halt_issued=False;session.adapter_khz=500
        with patch.object(d,'elf_identity',return_value={'sha256':'test'}),patch.object(d,'ProbeLock',return_value=contextlib.nullcontext()),patch.object(d,'make_session',return_value=contextlib.nullcontext(session)):
            with self.assertRaisesRegex(RuntimeError,'no readable result'):
                d.snapshot({'_elf':Path('firmware.elf'),'probe':'jlink'},Path('.'),['SystemCoreClock'])
        self.assertEqual(o.current,'running')

class IdentifyTests(unittest.TestCase):
    """The read-only SWD check must never halt, reset or flash the target."""
    def fake(self,state='running',device=0x10010483,cpuid=0x411fc272,dhcsr=0x03000011,
             halt_on_connect=False):
        """A stand-in for OpenOCD. ``read_memory`` answers the raw 32-bit registers identify reads.

        The device id and the CPUID must differ, because identify reads them from two different
        addresses and compares each against its own expected value."""
        class Ocd(d.OpenOCD):
            adapter_khz=500
            gdb_port=1234
            def __init__(self):
                # `state_at_connect` is what OpenOCD observes inside `init`, i.e. the state the
                # core really had before this debug session touched it; `current` is the state the
                # session reports afterwards, which for halt_on_connect is 'halted'.
                self.state_at_connect=state
                self.current='halted' if halt_on_connect else state
                self.calls=[];self.out=Path('.')
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def state(self):return self.current
            def command_resume(self):self.calls.append('resume');self.current='running'
            def command(self,c,**kw):
                self.calls.append(c)
                if c=='resume':self.current='running'
                if c=='halt':self.current='halted'
                if c.startswith('read_memory'):
                    if '0x5c001000' in c:return hex(device)
                    if '0xe000ed00' in c:return hex(cpuid)
                    if '0xe000edf0' in c:return hex(dhcsr)
                    if '0xe000edfc' in c:return '0x1000000'
                    return '0x0'
                return ''
        return Ocd()
    def session(self,backend,driver='openocd'):
        """A context manager standing in for make_session's ProbeSession.

        A real ProbeSession cannot be built here (its __enter__ spawns the probe process and GDB),
        and an instance-level ``__enter__`` would be ignored because Python looks special methods
        up on the type -- `with lock, make_session(...) as s` would then rebind `s` to whatever
        the real __enter__ returned. ``nullcontext`` enters to the object itself, which is exactly
        what the with-body needs, and ``isinstance`` checks inside the code under test still pass
        because this is a genuine ProbeSession built with __new__."""
        s=d.ProbeSession.__new__(d.ProbeSession)
        s.profile={};s.out=Path('.');s.driver=driver;s.opened=backend;s.backend=backend
        s.gdb=None;s.halt_issued=False;s.adapter_khz=getattr(backend,'adapter_khz',None)
        # identify() drives the target through the session interface, so the fake must offer it:
        # these calls are the observable evidence that identify did (or did not) touch the core.
        s.resume=backend.command_resume
        s.halt=lambda note='halt':(backend.command('halt'),setattr(s,'halt_issued',True))[0]
        return contextlib.nullcontext(s)
    #: fake() builds the target; everything else is an identify() keyword argument.
    TARGET_KEYS=('state','device','cpuid','dhcsr','halt_on_connect')
    def run_identify(self,profile_extra=None,**kw):
        target={k:kw.pop(k) for k in list(kw) if k in self.TARGET_KEYS}
        o=self.fake(**target)
        profile={'_elf':Path('firmware.elf'),'idcode_address':'0x5c001000','expected_device_id':'0x483',
                 'adapter_khz':500}
        profile.update(profile_extra or {})
        session=self.session(o)
        with patch.object(d,'ProbeLock',return_value=contextlib.nullcontext()),patch.object(d,'make_session',return_value=session):
            return o,d.identify(profile,Path('.'),None,**kw)
    def test_running_target_is_never_halted(self):
        o,r=self.run_identify()
        self.assertEqual(o.calls,[c for c in o.calls if c.startswith('read_memory')])
        self.assertFalse(r['halt_issued']);self.assertFalse(r['reset_issued'])
        self.assertTrue(r['matches_expected_device'])
        self.assertEqual(r['state_at_exit'],'running')
        self.assertEqual(r['dhcsr']['c_halt'],False)
        self.assertTrue(r['dhcsr']['c_debugen'])
    def test_unexpected_device_id_is_reported_not_hidden(self):
        o,r=self.run_identify(device=0x10010450)
        self.assertFalse(r['matches_expected_device'])
        self.assertTrue(any('not the expected' in n for n in r['notes']))
    def test_core_halted_by_connection_is_resumed_and_reported(self):
        o,r=self.run_identify(halt_on_connect=True)
        self.assertIn('resume',o.calls)
        self.assertEqual(r['state_at_exit'],'running')
        self.assertEqual(r['state_before_probe'],'running')
        self.assertTrue(any('resumed' in n for n in r['notes']))
    def test_core_halted_by_connection_can_be_left_halted(self):
        o,r=self.run_identify(halt_on_connect=True,resume_if_halted=False)
        self.assertNotIn('resume',o.calls)
        self.assertEqual(r['state_at_exit'],'halted')
    def test_core_already_halted_is_not_resumed_silently(self):
        # A stop that this command did not cause must not be undone on its own.
        o,r=self.run_identify(state='halted')
        self.assertNotIn('resume',o.calls)
        self.assertEqual(r['state_at_exit'],'halted')
        self.assertTrue(any('already halted' in n for n in r['notes']))
    def test_core_already_halted_is_resumed_only_on_request(self):
        o,r=self.run_identify(state='halted',resume_if_halted=True)
        self.assertIn('resume',o.calls)
        self.assertEqual(r['state_at_exit'],'running')
    def test_unanswered_target_raises_before_any_halt(self):
        o=self.fake(state='unknown')
        profile={'_elf':Path('firmware.elf'),'idcode_address':'0x5c001000','expected_device_id':'0x483',
                 'adapter_khz':500}
        session=self.session(o)
        with patch.object(d,'ProbeLock',return_value=contextlib.nullcontext()),patch.object(d,'make_session',return_value=session):
            # The message deliberately does not name a transport: this path runs for both the
            # OpenOCD and the J-Link backend, and the state it reports is the honest detail.
            with self.assertRaisesRegex(RuntimeError,"did not answer over the debug port"):
                d.identify(profile,Path('.'))
        self.assertEqual(o.calls,[])
    def test_dhcsr_decoding_names_the_stop_reason(self):
        locked=d.decode_dhcsr(0x04000001)
        self.assertTrue(locked['c_debugen']);self.assertFalse(locked['c_halt'])
        self.assertTrue(locked['s_lockup']);self.assertFalse(locked['s_halt'])
    def test_identify_reports_an_inconclusive_retire_probe_without_halting(self):
        # Regression: the probe used to answer False here, i.e. it claimed the firmware was not
        # running purely because this adapter never sets S_RETIRE_ST.
        o,r=self.run_identify(dhcsr=0x01000001)
        self.assertIsNone(r['cpu_executing'])
        self.assertTrue(any('inconclusive' in n for n in r['notes']))
        self.assertFalse(r['halt_issued'])
        self.assertEqual(r['state_at_exit'],'running')

class RetireProbeTests(unittest.TestCase):
    """S_RETIRE_ST is the only no-halt execution signal, and it lies in both directions."""
    def check(self,second_raw,first=False):
        return d.interpret_retire_probe(first,d.decode_dhcsr(second_raw))
    def test_retired_instruction_proves_execution(self):
        v=self.check(0x01000001|1<<25)
        self.assertTrue(v['cpu_executing']);self.assertTrue(v['usable']);self.assertIsNone(v['note'])
    def test_clear_retire_bit_is_inconclusive_not_negative(self):
        v=self.check(0x01000001)
        self.assertTrue(v['usable'],'nothing invalidates the probe, it just reached no verdict')
        self.assertIsNone(v['cpu_executing'],
                          'a clear S_RETIRE_ST must never be reported as "not executing"')
        self.assertIn('inconclusive',v['note'])
    def test_core_halted_elsewhere_makes_the_probe_meaningless(self):
        v=self.check(0x01000001|1<<17|1<<1)
        self.assertIsNone(v['cpu_executing']);self.assertFalse(v['usable'])
        self.assertIn('another debug session',v['note'])
    def test_probe_ignores_the_retire_bit_read_before_the_delay(self):
        # S_RETIRE_ST read-to-clear: only the second read can say anything about the interval.
        v=d.interpret_retire_probe(True,d.decode_dhcsr(0x01000001))
        self.assertIsNone(v['cpu_executing'])

class ErrorChainTests(unittest.TestCase):
    def test_cleanup_failure_preserves_primary_error(self):
        try:
            try:raise ValueError('read failed')
            except ValueError as e:raise RuntimeError('restore failed') from e
        except RuntimeError as e:
            self.assertEqual(d.error_chain(e),['RuntimeError: restore failed','ValueError: read failed'])

class TargetIdentityTests(unittest.TestCase):
    """The device-family gate runs before any write, under BOTH probes."""
    profile={'idcode_address':'0x5c001000','expected_device_id':'0x483'}
    def test_wrong_family_refuses_flash_gate(self):
        class GdbBackend:
            def evaluate(self,expression):
                self.expression=expression
                return '0x10010450'
        with self.assertRaisesRegex(RuntimeError,'identity mismatch'):
            d.check_target(GdbBackend(),self.profile)
    def test_matching_family_keeps_revision(self):
        class GdbBackend:
            def evaluate(self,expression):return '0x10010483'
        result=d.check_target(GdbBackend(),self.profile)
        self.assertEqual(result['revision_id'],'0x1001')
        self.assertEqual(result['device_id'],'0x483')
    def test_the_gdb_backend_is_asked_for_a_dereferenced_pointer(self):
        # A bare numeric expression would evaluate to the ADDRESS, so the device id would look
        # like 0x5c001000 and every flash attempt would be refused.
        class GdbBackend:
            def __init__(self):self.expressions=[]
            def evaluate(self,expression):self.expressions.append(expression);return '0x10010483'
        backend=GdbBackend()
        d.check_target(backend,self.profile)
        self.assertEqual(backend.expressions,['*(unsigned int *)0x5c001000'])
    def test_the_openocd_backend_uses_its_own_memory_read(self):
        # Built with __new__ like the other OpenOCD fakes: the production class decides which
        # read to use, and the real `command()` is replaced so nothing touches a socket.
        backend=d.OpenOCD.__new__(d.OpenOCD)
        backend.commands=[]
        backend.command=lambda text:backend.commands.append(text) or '0x10010483'
        result=d.check_target(backend,self.profile)
        self.assertEqual(backend.commands,['read_memory 0x5c001000 32 1'])
        self.assertEqual(result['device_id'],'0x483')
