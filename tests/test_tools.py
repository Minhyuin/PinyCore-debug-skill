"""Portability tests: can this package run on a machine that is not the one it grew up on?

The point of these tests is that they must not depend on what is installed HERE. Every lookup
is exercised against a temporary tree or a patched probe, so a machine with J-Link installed and
a machine without one run the same assertions -- otherwise "portable" would be tested by the
accident of this developer's disk layout.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import piny_debug as d
import piny_tools as pt

def touch(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    return str(path)

class FindToolTests(unittest.TestCase):
    def test_a_profile_value_that_exists_wins_over_everything(self):
        with tempfile.TemporaryDirectory() as t:
            good = touch(Path(t) / 'JLinkGDBServerCL.exe')
            with patch.object(pt, 'search_path', return_value=Path(t) / 'other.exe'):
                report = pt.find_tool('jlink_gdb_server', good, bases=[])
            self.assertTrue(report['exists'])
            self.assertEqual(report['path'], good)
            self.assertEqual(report['source'], 'profile')
            self.assertEqual(report['found'], [], 'a working profile must not be second-guessed')

    def test_an_absent_profile_value_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as t:
            found = touch(Path(t) / 'arm-none-eabi-gdb.exe')
            with patch.object(pt, 'search_path', return_value=Path(found)):
                report = pt.find_tool('gdb', 'E:/gone/arm-none-eabi-gdb.exe', bases=[])
            self.assertTrue(report['exists'])
            self.assertEqual(report['path'], found)
            self.assertEqual(report['source'], 'PATH: arm-none-eabi-gdb')

    def test_install_directories_are_searched_from_a_fixture(self):
        # No environment variable and no `%` expansion here on purpose: that expansion is a
        # Windows spelling, so a test that needs it cannot be run on the other platform in CI.
        # What this asserts is the part that is portable -- the layout is globbed and, when two
        # layouts match, the version-less one is preferred. `bases=[t]` is exactly the "here is
        # where the tools live on this machine" input the profile supplies.
        with tempfile.TemporaryDirectory() as t:
            wanted = touch(Path(t) / 'SEGGER' / 'JLink' / 'JLinkGDBServerCL.exe')
            touch(Path(t) / 'SEGGER' / 'JLink_V878' / 'JLinkGDBServerCL.exe')
            with patch.object(pt, 'search_path', return_value=None):
                report = pt.find_tool('jlink_gdb_server', None, bases=[t], with_registry=False)
            self.assertEqual(report['path'], wanted)
            self.assertEqual(report['source'], 'SEGGER/JLink (version-less)')

    @unittest.skipUnless(sys.platform == 'win32',
                         'INSTALL_BASES is a list of Windows spellings; on POSIX os.path.expandvars '
                         'leaves %NAME% untouched, so there is no machine here to search')
    def test_install_directories_are_globbed_and_expanded(self):
        with tempfile.TemporaryDirectory() as t:
            wanted = touch(Path(t) / 'SEGGER' / 'JLink_V878' / 'JLinkGDBServerCL.exe')
            with patch.object(pt, 'search_path', return_value=None), \
                 patch.dict(os.environ, {'PINY_TEST_ROOT': t}):
                report = pt.find_tool('jlink_gdb_server', None, bases=[r'%PINY_TEST_ROOT%'],
                                      with_registry=False)
            self.assertEqual(report['path'], wanted)
            self.assertIn('release-stamped', report['source'])

    def test_an_unset_variable_is_skipped_not_searched_as_a_literal(self):
        with patch.object(pt, 'search_path', return_value=None):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('PINY_TEST_ABSENT', None)
                self.assertEqual(pt.search_install_directories('systemview',
                                                               bases=[r'%PINY_TEST_ABSENT%']), [])

    def test_the_version_less_install_name_is_preferred_to_a_stamped_one(self):
        with tempfile.TemporaryDirectory() as t:
            plain = touch(Path(t) / 'SEGGER' / 'SystemView' / 'SystemView.exe')
            touch(Path(t) / 'SEGGER' / 'SystemView_V360e' / 'SystemView.exe')
            with patch.object(pt, 'search_path', return_value=None):
                report = pt.find_tool('systemview', None, bases=[t], with_registry=False)
            self.assertEqual(report['path'], plain)

    def test_registry_answers_are_used_and_labelled(self):
        with tempfile.TemporaryDirectory() as t:
            wanted = touch(Path(t) / 'JLinkGDBServerCL.exe')
            with patch.object(pt, 'search_path', return_value=None), \
                 patch.object(pt, 'read_registry_directory', return_value=t):
                report = pt.find_tool('jlink_gdb_server', None, bases=[], with_registry=True)
            self.assertEqual(report['path'], wanted)
            self.assertIn('registry', report['source'])

    def test_a_missing_tool_says_so_instead_of_raising(self):
        with patch.object(pt, 'search_path', return_value=None):
            report = pt.find_tool('openocd', 'E:/gone/openocd.exe', bases=[], with_registry=False)
        self.assertFalse(report['exists'])
        self.assertEqual(report['source'], 'profile value is not a file')
        self.assertEqual(report['path'], 'E:/gone/openocd.exe',
                         'the report must repeat what the profile said, so the user can see it')
        self.assertIn('install-directories', report['searched'])

    def test_a_tool_never_configured_is_not_reported_as_missing(self):
        with patch.object(pt, 'search_path', return_value=None):
            report = pt.find_tool('systemview', None, bases=[], with_registry=False)
        self.assertFalse(report['exists'])
        self.assertEqual(report['source'], 'not found')
        self.assertIsNone(report['path'])

class DiscoverTests(unittest.TestCase):
    def test_required_keys_depend_on_the_probe(self):
        with patch.object(pt, 'search_path', return_value=None):
            report = pt.discover({'probe': 'jlink'}, bases=[], with_registry=False)
        self.assertEqual(report['required'], ['jlink_gdb_server', 'gdb'])
        self.assertIn('jlink_gdb_server', report['missing'])
        self.assertNotIn('openocd', report['tools'],
                         'an unconfigured optional tool is not a migration problem')
        self.assertFalse(report['usable'])

    def test_an_openocd_rig_is_judged_by_its_own_tools(self):
        with tempfile.TemporaryDirectory() as t:
            server = touch(Path(t) / 'openocd.exe')
            gdb = touch(Path(t) / 'arm-none-eabi-gdb.exe')
            profile = {'probe': 'openocd', 'openocd': server, 'gdb': gdb}
            report = pt.discover(profile, bases=[], with_registry=False)
        self.assertTrue(report['usable'])
        self.assertEqual(report['missing'], [])
        self.assertEqual(report['resolved']['openocd'], server)

    def test_companions_are_reported_beside_the_server(self):
        with tempfile.TemporaryDirectory() as t:
            jlink = touch(Path(t) / 'JLink.exe')
            companions = pt.jlink_companions(touch(Path(t) / 'JLinkGDBServerCL.exe'))
            linux = pt.companion_names('posix')
        self.assertTrue(companions['JLink.exe']['exists'])
        self.assertFalse(companions['JLinkRTTLogger.exe']['exists'])
        self.assertEqual(companions['JLink.exe']['path'], jlink)
        # Which CLIs sit beside the server is a platform fact, not a constant: Linux spells the
        # same two tools JLinkExe and JLinkRTTLoggerExe, and a Windows list would report both
        # of them as missing on a machine that has them.
        self.assertIn('JLinkExe', linux)
        self.assertIn('JLinkRTTLoggerExe', linux)
        self.assertNotIn('JLink.exe', linux)

    def test_an_already_correct_profile_yields_no_overrides_at_all(self):
        with tempfile.TemporaryDirectory() as t:
            profile = {'probe': 'jlink', 'gdb': 'E:/gone/gdb.exe',
                       'jlink_gdb_server': touch(Path(t) / 'JLinkGDBServerCL.exe')}
            with patch.object(pt, 'search_path', return_value=None):
                report = pt.discover(profile, bases=[], with_registry=False)
            overrides = pt.profile_overrides(report, profile)
        self.assertEqual(overrides, {},
                         'a value that already works must never be repeated back as a change')
        self.assertNotIn('gdb', overrides, 'gdb was not found, so there is nothing to suggest')

    def test_a_forward_slash_profile_path_is_not_reported_as_a_discrepancy(self):
        # A hand-written Windows profile uses forward slashes; discovery answers with native
        # backslashes. Same file, so there is nothing to change -- comparing the two as strings
        # told the user to replace a working path with itself.
        with tempfile.TemporaryDirectory() as t:
            server = touch(Path(t) / 'JLinkGDBServerCL.exe')
            profile = {'probe': 'jlink', 'jlink_gdb_server': str(server).replace('\\', '/')}
            with patch.object(pt, 'search_path', return_value=None):
                report = pt.discover(profile, bases=[], with_registry=False)
            overrides = pt.profile_overrides(report, profile)
        self.assertEqual(overrides, {},
                         'the profile already names this file; it must not be echoed back')

    def test_overrides_carry_a_discovered_replacement_for_a_stale_path(self):
        with tempfile.TemporaryDirectory() as t:
            found = touch(Path(t) / 'arm-none-eabi-gdb.exe')
            profile = {'probe': 'jlink', 'gdb': 'E:/gone/gdb.exe',
                       'jlink_gdb_server': 'E:/gone/JLinkGDBServerCL.exe'}

            def which(name):
                return str(found) if name == 'arm-none-eabi-gdb' else None
            with patch.object(pt, 'search_path', side_effect=which):
                report = pt.discover(profile, bases=[], with_registry=False)
            overrides = pt.profile_overrides(report, profile)
        self.assertEqual(list(overrides), ['gdb'])
        self.assertEqual(overrides['gdb'], found)
        self.assertNotIn('jlink_gdb_server', overrides,
                         'the server was not found, so no replacement may be suggested for it')

class LoadProfileTests(unittest.TestCase):
    def profile_file(self, directory, **extra):
        payload = {'name': 'test', 'project': directory, 'elf': 'build/app.elf',
                   'probe': 'jlink', 'gdb': 'E:/gone/gdb.exe',
                   'jlink_gdb_server': 'E:/gone/JLinkGDBServerCL.exe'}
        payload.update(extra)
        path = Path(directory) / 'profile.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def test_discovery_is_recorded_when_it_is_on(self):
        with tempfile.TemporaryDirectory() as t:
            profile = d.load_profile(self.profile_file(t))
        self.assertIn('_tools', profile)
        self.assertEqual(profile['_profile_path'], str(Path(t) / 'profile.json'))
        self.assertEqual(profile['_elf'], Path(t) / 'build' / 'app.elf')

    def test_strict_mode_leaves_the_profile_exactly_as_written(self):
        with tempfile.TemporaryDirectory() as t:
            path = self.profile_file(t)
            with patch.object(pt, 'find_tool', side_effect=AssertionError('must not search')):
                profile = d.load_profile(path, discover_tools=False)
        self.assertEqual(profile['_tools'], {})

    def test_a_broken_path_names_the_key_and_offers_a_replacement(self):
        with tempfile.TemporaryDirectory() as t:
            profile = d.load_profile(self.profile_file(t), discover_tools=False)
            with patch.object(d, 'discover_tool_for_message', return_value='C:/found/gdb.exe'):
                with self.assertRaises(RuntimeError) as caught:
                    d.tool_path(profile, 'gdb')
        message = str(caught.exception)
        self.assertIn('"gdb"', message)
        self.assertIn('E:/gone/gdb.exe', message)
        self.assertIn(str(Path(t) / 'profile.json'), message)
        self.assertIn('C:/found/gdb.exe', message)

    def test_a_missing_key_says_which_file_to_edit(self):
        with tempfile.TemporaryDirectory() as t:
            profile = d.load_profile(self.profile_file(t), discover_tools=False)
            with self.assertRaisesRegex(RuntimeError, 'no "systemview" entry'):
                d.tool_path(profile, 'systemview')

    def test_a_discovered_tool_is_used_without_editing_the_profile(self):
        with tempfile.TemporaryDirectory() as t:
            real = touch(Path(t) / 'JLinkGDBServerCL.exe')
            profile = d.load_profile(self.profile_file(t), discover_tools=False)
            profile['_tools'] = {'jlink_gdb_server': {'path': real, 'source': 'install-directories'}}
            self.assertEqual(d.tool_path(profile, 'jlink_gdb_server'), real)

class DoctorTests(unittest.TestCase):
    def build(self, directory, **extra):
        payload = {'name': 'test', 'project': directory, 'elf': 'build/app.elf', 'probe': 'jlink',
                   'gdb': 'E:/gone/gdb.exe', 'jlink_gdb_server': 'E:/gone/JLinkGDBServerCL.exe',
                   'idcode_address': '0x5c001000', 'expected_device_id': '0x483'}
        payload.update(extra)
        return _loaded(directory, payload)

    def test_doctor_reports_what_is_missing_and_never_raises(self):
        with tempfile.TemporaryDirectory() as t:
            profile = self.build(t)
            with patch.object(pt, 'search_path', return_value=None), \
                 patch.object(pt, 'search_install_directories', return_value=[]), \
                 patch.object(pt, 'read_registry_directory', return_value=None), \
                 patch.object(d, 'tick_symbol_from_elf', return_value=None):
                report = d.doctor(profile)
        self.assertFalse(report['usable'])
        self.assertIn('jlink_gdb_server', report['missing_required'])
        joined = ' | '.join(report['problems'])
        self.assertIn('required tool not found: jlink_gdb_server', joined)
        self.assertIn('ELF not built yet', joined)
        self.assertIn('arm-none-eabi-nm', report['tick_note'])

    def test_a_missing_project_directory_is_a_problem_with_the_key_named(self):
        with tempfile.TemporaryDirectory() as t:
            profile = self.build(t, project=str(Path(t) / 'not-here'))
            with patch.object(d, 'tick_symbol_from_elf', return_value=None):
                report = d.doctor(profile)
        self.assertFalse(report['project_exists'])
        self.assertTrue(any('set "project" in the profile' in p for p in report['problems']))

    def test_a_resolved_tick_symbol_is_reported_with_its_meaning(self):
        with tempfile.TemporaryDirectory() as t:
            profile = self.build(t)
            with patch.object(d, 'tick_symbol_from_elf', return_value='0x20007e5c'):
                report = d.doctor(profile)
        self.assertEqual(report['tick_symbol_resolved'], '0x20007e5c')
        self.assertIn('moves on every rebuild', report['tick_note'])

    def test_the_serial_is_never_guessed(self):
        with tempfile.TemporaryDirectory() as t:
            profile = self.build(t, jlink_serial=None)
            with patch.object(d, 'tick_symbol_from_elf', return_value='0x20007e5c'):
                report = d.doctor(profile)
        self.assertIn('never guessed', report['serial_note'])

    def test_write_profile_is_opt_in_and_merges_into_the_existing_file(self):
        with tempfile.TemporaryDirectory() as t:
            profile = self.build(t)
            target = Path(t) / 'written.json'
            target.write_text(json.dumps({'name': 'mine', 'notes': 'keep me'}), encoding='utf-8')
            with patch.object(d, 'tick_symbol_from_elf', return_value=None):
                report = d.doctor(profile, write_profile=target)
            merged = json.loads(target.read_text(encoding='utf-8'))
        self.assertEqual(merged['name'], 'mine', 'an unrelated key must survive the merge')
        self.assertEqual(merged['notes'], 'keep me')
        self.assertIn('wrote_profile', report)
        self.assertEqual(report['wrote_profile']['keys'], sorted(report['suggested_profile_overrides']))

    def test_doctor_does_not_write_anything_unless_asked(self):
        with tempfile.TemporaryDirectory() as t:
            profile = self.build(t)
            before = sorted(p.name for p in Path(t).iterdir())
            with patch.object(d, 'tick_symbol_from_elf', return_value=None):
                d.doctor(profile)
            after = sorted(p.name for p in Path(t).iterdir())
        self.assertEqual(before, after)

    def test_the_portable_view_separates_machine_keys_from_rig_keys(self):
        with tempfile.TemporaryDirectory() as t:
            profile = self.build(t, jlink_speed_khz=24000, freertos_tick_symbol='0x20007e5c')
            with patch.object(d, 'tick_symbol_from_elf', return_value=None):
                report = d.doctor(profile, portable=True)
        kinds = {entry['key']: entry['kind'] for entry in report['portable']['bound_keys']}
        self.assertEqual(kinds['project'], 'machine')
        self.assertEqual(kinds['jlink_gdb_server'], 'machine')
        self.assertEqual(kinds['jlink_speed_khz'], 'target')
        self.assertEqual(kinds['freertos_tick_symbol'], 'target')
        self.assertIn('re-verify them there', report['portable']['note'])

def _loaded(directory, payload):
    """Build a profile the way ``load_profile`` would, without discovery reaching this machine."""
    path = Path(directory) / 'p.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return d.load_profile(path, discover_tools=False)
