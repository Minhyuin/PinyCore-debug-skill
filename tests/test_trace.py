import sys,tempfile,unittest,csv,os,threading
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import piny_trace as t

class TraceTests(unittest.TestCase):
    def test_formatted_numbers(self):
        self.assertEqual(t.number(' 98.06 % '),98.06)
        self.assertAlmostEqual(t.number('0.051 237 833 s'),0.051237833)
        for bad in ['nan','inf','unknown','']:
            with self.assertRaises(ValueError):t.number(bad)
    def fixtures(self,root,idle='90.00 %',overflow=False):
        c=Path(root)/'contexts.csv';e=Path(root)/'events.csv'
        c.write_text('Name,Type,Core,Stack Information,CPU Load,Total Run Time,Activations\ninsTask,@6,Core 0,384 @ 0x20000000,10.00 %,0.5 s,2000\nIdle,,Core 0,,'+idle+',4.5 s,4000\n',encoding='utf-8')
        e.write_text('sequencenum,timestamp,context,event,detail,eventint\n0,0.000 000,Idle,Start,,10\n1,0.001 000,Idle,Init,,24\n2,1.000 000,insTask,Task Start,,4\n3,5.000 000,Idle,Stop,,11\n'+('4,5.001 000,Idle,Overflow,lost,1\n' if overflow else ''),encoding='utf-8')
        return c,e
    def test_summary_uses_native_cpu_percentage(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d);s=t.summarize(c,e)
            self.assertEqual(s['trace_busy_percent'],10.0)
            self.assertEqual(s['recorded_seconds'],5.0)
            self.assertTrue(s['quality']['usable'])
    def test_overflow_invalidates_quality(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d,overflow=True);s=t.summarize(c,e)
            self.assertFalse(s['quality']['usable'])
            self.assertEqual(s['quality']['overflow_events'],1)
    def test_no_idle_never_fabricates_busy(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d);c.write_text(c.read_text().replace('Idle,','Unknown,'))
            s=t.summarize(c,e);self.assertIsNone(s['trace_busy_percent']);self.assertFalse(s['quality']['usable'])
    def test_bad_percentage_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d,idle='101 %')
            with self.assertRaises(ValueError):t.summarize(c,e)
    def test_missing_csv_fields_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d);c.write_text('Name,Wrong\nIdle,10\n')
            with self.assertRaises(ValueError):t.summarize(c,e)
    def test_missing_stop_is_reported_not_blocking(self):
        # SystemView still exports complete CPU-load numbers for a recording that was
        # ended by halting the target, so an absent Stop event is a note, not a failure.
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d);e.write_text(e.read_text().replace('Stop,,11','Other,,5'))
            s=t.summarize(c,e)
            self.assertTrue(s['quality']['usable'])
            self.assertEqual(s['quality']['stop_events'],0)
            self.assertTrue(any('missing_stop_event' in n for n in s['quality']['notes']))
    def test_duplicate_starts_are_reported_as_note(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d)
            e.write_text(e.read_text()+'4,5.002 000,,Start,,10\n')
            s=t.summarize(c,e)
            self.assertTrue(s['quality']['usable'])
            self.assertEqual(s['quality']['start_events'],2)
            self.assertTrue(any('duplicate_start_events' in n for n in s['quality']['notes']))
    def test_context_load_sum_guard_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d,idle='80.00 %')
            s=t.summarize(c,e)
            self.assertFalse(s['quality']['usable'])
            self.assertIn('context_load_sum_not_100:Core 0',s['quality']['issues'])
    def test_torn_export_is_detected_as_incomplete(self):
        # A half-written SystemView export exists, is non-empty and still parses, but its
        # incomplete rows carry the 2**63 ns sentinel as a CPU load. File existence alone
        # must not be mistaken for a finished export.
        with tempfile.TemporaryDirectory() as d:
            c=Path(d)/'contexts.csv';e=Path(d)/'events.csv'
            c.write_text('Name,Type,Core,Stack Information,Activations,Total Blocked Time,Total Run Time,'
                         'Time Interrupted,CPU Load,Last Run Time\n'
                         'SysTick,#15,Core 0,,10332,,9 223 372 036.854 775 808 s,0.000 000 ms,10894144524.79 %,'
                         '9 223 372 036 854.775 808 ms\n',encoding='utf-8')
            e.write_text('sequencenum,timestamp,context,event,detail,eventint\n',encoding='utf-8')
            self.assertEqual(t.validate_export(c,e),'CPU Load outside 0..100 in contexts CSV')
    def test_complete_export_validates(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d)
            e.write_text(e.read_text()+''.join('4,5.%03d 000,Idle,Task Start,,4\n'%i for i in range(30)),encoding='utf-8')
            self.assertIsNone(t.validate_export(c,e))
    def test_truncated_export_validates(self):
        # An export cut off before any event row was flushed still parses as CSV.
        with tempfile.TemporaryDirectory() as d:
            c,e=self.fixtures(d)
            e.write_text('sequencenum,timestamp,context,event,detail,eventint\n',encoding='utf-8')
            self.assertEqual(t.validate_export(c,e),'events CSV holds no events')
    def test_missing_export_validates(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(t.validate_export(Path(d)/'contexts.csv',Path(d)/'events.csv'),
                             'missing or empty contexts.csv')
    def test_duration_guard(self):
        for secs in [-1,0,float('inf'),float('nan'),301]:
            with self.assertRaises(ValueError):t.validate_seconds(secs)
        self.assertEqual(t.validate_seconds(5),5)
    def test_diff_percentage_points(self):
        a={'trace_busy_percent':10,'recorded_seconds':5,'quality':{'usable':True}}
        b={'trace_busy_percent':8,'recorded_seconds':5,'quality':{'usable':True}}
        self.assertEqual(t.compare(a,b)['busy_delta_percentage_points'],-2)
        b['quality']['usable']=False
        with self.assertRaises(ValueError):t.compare(a,b)

class RecordTests(unittest.TestCase):
    """The live path: SystemView cannot be started recording from a command line.

    On the board, `SystemView.exe -start -save ... -delay ... -quit` opens a window and
    then does nothing for a minute, because -start/-stop/-save are control options that
    only a running instance executes. These tests pin the parts of the replacement that
    can fail silently: LoadDataOnStart is what made a failed capture export the recorder's
    own most recent file (byte identical to the SEGGER sample), the control socket must
    report a missing listener instead of hanging, and a run that writes no recording has
    to fail instead of leaving the previous capture to be analyzed.
    """

    def test_load_data_on_start_is_disabled(self):
        text='[%General]\nTriggerMode=4\n[Preferences]\nLoadDataOnStart=true\nConnectTimeout=20000\n'
        patched=t._patch_ini(text)
        self.assertIn('LoadDataOnStart=false',patched)
        self.assertNotIn('LoadDataOnStart=true',patched)
        self.assertIn('ConnectTimeout=20000',patched)
        self.assertEqual(patched.count('LoadDataOnStart'),1)

    def test_load_data_on_start_is_added_when_absent(self):
        patched=t._patch_ini('[%General]\nTriggerMode=4\n')
        self.assertTrue(patched.endswith('LoadDataOnStart=false\n'))

    def test_control_command_reports_no_listener(self):
        port=t.free_port()
        with self.assertRaises(OSError):
            t._systemview_command('-start',port,timeout=1.0,reply_timeout=1.0)

    def test_record_without_a_recording_fails_and_clears_stale_exports(self):
        out=Path(tempfile.mkdtemp())
        (out/'live.svdat').write_bytes(b'previous capture')
        (out/'contexts.csv').write_text('Name,CPU Load\nIdle,100.00 %\n',encoding='utf-8')
        # A missing executable fails before anything else; what matters is that the stale
        # capture is already gone, so it can never be analyzed as this run's result.
        with self.assertRaises(FileNotFoundError):
            t.systemview_record(Path('definitely-not-systemview.exe'),out,1.0,
                                auto_accept_license=False,timeout=5.0,control_port=t.free_port())
        self.assertFalse((out/'live.svdat').exists())
        self.assertFalse((out/'contexts.csv').exists())

    def test_record_rejects_an_out_of_range_window(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                t.systemview_record(Path('x.exe'),Path(d),0.05)

    def test_context_load_sum_is_reported_and_gates_the_measurement(self):
        """The sum of the exported CPU loads is the check that caught a bad board capture.

        At 4000 kHz the J-Link could not drain the target's 1 KB SysView RTT buffer:
        2847 overflow events were dropped, the exported loads summed to 65.4 % and Idle was
        understated as 48.5 % - a 51.5 % busy reading against a true 23.9 %. The sum has to
        be visible in the summary and has to block, and it must be computed over the native
        CPU Load column (Total Run Time / recording span) rather than re-derived here.
        """
        contexts=('Name,Type,Core,CPU Load,Activations\n'
                  'Idle,,Core 0,48.47 %,\n'
                  'insTask,,Core 0,12.10 %,\n'
                  'Scheduler,,Core 0,4.83 %,\n')
        events=('timestamp,event,eventint,detail\n'
                '0.000000000,Init,24,"Cycle Freq.: 550000000"\n'
                '0.100000000,Start,10,\n'
                '11.500000000,Stop,11,\n')
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            (d/'contexts.csv').write_text(contexts,encoding='utf-8')
            (d/'events.csv').write_text(events,encoding='utf-8')
            summary=t.summarize(d/'contexts.csv',d/'events.csv')
        self.assertEqual(summary['quality']['context_load_sum'],{'Core 0':65.4})
        self.assertIn('context_load_sum_not_100:Core 0',summary['quality']['issues'])
        self.assertFalse(summary['quality']['usable'])
        self.assertEqual(summary['trace_busy_percent'],51.53)

    def test_complete_context_load_sum_passes(self):
        contexts=('Name,Type,Core,CPU Load,Activations\n'
                  'Idle,,Core 0,76.09 %,\n'
                  'insTask,,Core 0,16.91 %,\n'
                  'Scheduler,,Core 0,2.96 %,\n'
                  'SysTick,,Core 0,1.12 %,\n'
                  'AppTask,,Core 0,2.28 %,\n'
                  'Tmr Svc,,Core 0,0.63 %,\n'
                  'BuzzerTask,,Core 0,0.00 %,\n'
                  'CmdTask,,Core 0,0.00 %,\n')
        events=('timestamp,event,eventint,detail\n'
                '0.000000000,Init,24,"Cycle Freq.: 550000000"\n'
                '0.100000000,Start,10,\n'
                '11.800000000,Stop,11,\n')
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            (d/'contexts.csv').write_text(contexts,encoding='utf-8')
            (d/'events.csv').write_text(events,encoding='utf-8')
            summary=t.summarize(d/'contexts.csv',d/'events.csv')
        self.assertEqual(summary['quality']['context_load_sum'],{'Core 0':99.99})
        self.assertTrue(summary['quality']['usable'],summary['quality'])
        self.assertEqual(summary['trace_busy_percent'],23.91)

    def test_save_path_is_absolute(self):
        """Measured on the board: a relative -save path writes nothing at all.

        `-save artifacts\\board-run2\\live.svdat` answered with the command banner and then
        reported 'SystemView Error' 2 s later, because SystemView resolves the path against
        its own working directory rather than this process's. The identical run with an
        absolute path produced 2296373 bytes, so the skill must always hand it an absolute
        one - and must do it whatever shape of --output the user typed.
        """
        with tempfile.TemporaryDirectory() as base:
            out=Path(base)/'artifacts'/'board-run3'
            out.mkdir(parents=True)
            commands=[]
            def fake_command(command,port=None,timeout=1.0,reply_timeout=6.0):
                commands.append(command)
                if command.startswith('-save'):
                    path=Path(command[len('-save '):])
                    self.assertTrue(path.is_absolute(),
                                    '-save must never be given a relative path: %r'%command)
                    path.write_bytes(b'recording')
                return 'SEGGER SystemView V3.60e Command Server\r\n'
            cwd=Path.cwd()
            previous=cwd
            os.chdir(base)  # run from the parent, as `record --output artifacts\\board-run3` does
            # Real sleeps make this test wait out the recording window and the save timeout;
            # blanket no-op sleeps instead spin the poll loops into hundreds of thousands of
            # mock calls. A bounded sleep keeps both the elapsed time and the call count small.
            def quick_sleep(seconds=0.0):
                threading.Event().wait(min(seconds,0.01))
            try:
                with mock.patch.object(t.piny_view,'accept_license',return_value={'dismissed':True}), \
                     mock.patch.object(t,'_systemview_command',side_effect=fake_command), \
                     mock.patch.object(t,'stop_process',return_value=True), \
                     mock.patch.object(t.time,'sleep',side_effect=quick_sleep), \
                     mock.patch.object(t.piny_view.time,'sleep',side_effect=quick_sleep), \
                     mock.patch.object(t.subprocess,'Popen',return_value=FakeProcess()):
                    recording,meta=t.systemview_record(Path('SystemView.exe'),
                                                       Path('artifacts')/'board-run3',1.0,
                                                       auto_accept_license=True,timeout=30.0,
                                                       control_port=t.free_port())
            finally:
                os.chdir(previous)
            self.assertEqual(recording,out.resolve()/'live.svdat')
            self.assertTrue(recording.is_file())
            self.assertTrue(any(c.startswith('-save') for c in commands),commands)
            self.assertEqual(meta['bytes'],9)


class FakeProcess:
    """Just enough of subprocess.Popen for systemview_record.

    It stays alive while the recording is driven (anything else raises the 'exited before
    its control server answered' error) and then reports a clean exit, so the shutdown path
    does not have to wait out its full 10 s patience for a process that is never coming back.
    """

    pid=4242
    def __init__(self):
        self.polls=0
    def poll(self):
        self.polls+=1
        return None if self.polls<4 else 0


if __name__=='__main__':unittest.main()
