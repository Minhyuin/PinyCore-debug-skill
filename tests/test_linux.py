#!/usr/bin/env python3
"""Tests for the Linux search space (scripts/piny_linux.py) and its dispatch from piny_tools.

These run ON WINDOWS and that is deliberate, not an oversight. What they check is the part that
is testable anywhere: which directories are walked, which name prefixes match, in what order
candidates are preferred, and that the platform dispatch hands Linux callers the Linux
vocabulary. What they CANNOT check is whether a file really is at /opt/SEGGER on a Linux box --
the tests build that directory tree themselves. A live Linux install is a CI job, not a unit
test, and the Linux half of a real rig is still unverified (see references/portability.md).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import piny_linux as pl                                        # noqa: E402
import piny_tools as pt                                        # noqa: E402


def touch(path, mode=0o755):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'')
    try:
        path.chmod(mode)
    except OSError:                       # pragma: no cover - a filesystem without modes
        pass
    return path


class BaseDirectoryTests(unittest.TestCase):
    def test_unset_variables_drop_out_instead_of_being_searched_literally(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            bases = pl.base_directories()
        self.assertNotIn('%XDG_DATA_HOME%', bases)
        self.assertNotIn('$XDG_DATA_HOME', bases)
        self.assertTrue(all('%' not in b and '$' not in b for b in bases))

    def test_an_unset_home_does_not_produce_a_literal_tilde_path(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            bases = pl.base_directories()
        self.assertNotIn(str(Path.home() / '.local' / 'bin'), bases)

    def test_the_vendor_default_directory_is_searched(self):
        # /opt/SEGGER is where SEGGER's Linux installers put J-Link and SystemView, so a
        # search space that omits it cannot find the tools it exists to find.
        self.assertIn('/opt/SEGGER', pl.INSTALL_BASES)

    def test_the_interpreter_bin_directory_is_offered_for_gdb(self):
        # A virtualenv holding arm-none-eabi-gdb is a legitimate pin, and it is normally not on
        # PATH when a command runs under sudo.
        with tempfile.TemporaryDirectory() as t:
            fake_python = touch(Path(t) / 'bin' / 'python3')
            with mock.patch.object(sys, 'executable', str(fake_python)):
                directories = pl.python_bin_dirs()
        self.assertIn(str(fake_python.parent), directories)

    def test_a_missing_interpreter_directory_is_not_reported(self):
        with mock.patch.object(sys, 'executable', str(Path(tempfile.gettempdir()) / 'no' / 'python')):
            self.assertEqual(pl.python_bin_dirs(), [])


class PrefixTests(unittest.TestCase):
    def test_a_build_spelling_of_the_windows_name_matches(self):
        # Measured against SEGGER's Linux naming: JLinkGDBServerCLExe, not JLinkGDBServerCL.
        self.assertTrue(pl._matches_prefix('JLinkGDBServerCLExe', 'JLinkGDBServerCL'))
        self.assertTrue(pl._matches_prefix('JLinkGDBServer.exe', 'JLinkGDBServer'))
        self.assertTrue(pl._matches_prefix('SystemView', 'SystemView'))

    def test_a_neighbouring_file_is_not_mistaken_for_the_tool(self):
        # /opt/SEGGER/JLink* contains JLinkDevices.xml (a device database). Running it would be
        # a bewildering failure, so a candidate has to continue the prefix with a build
        # spelling rather than with anything at all.
        self.assertFalse(pl._matches_prefix('JLinkDevices.xml', 'JLinkGDBServerCL'))
        self.assertFalse(pl._matches_prefix('JLinkGUIServerExe', 'JLink'))
        self.assertFalse(pl._matches_prefix('SystemViewSplashScreen', 'SystemView'))
        self.assertFalse(pl._matches_prefix('', 'SystemView'))

    def test_a_prefix_that_only_starts_the_name_is_not_enough(self):
        self.assertFalse(pl._matches_prefix('JLinkRTTLoggerExe', 'JLinkGDBServerCL'))


class InstallDirectoryTests(unittest.TestCase):
    """A fake Linux install tree, built here so the walk can be checked on any host."""

    def _tree(self, root):
        root = Path(root)
        touch(root / 'SEGGER' / 'JLink' / 'JLinkGDBServerCLExe')
        touch(root / 'SEGGER' / 'JLink_V878' / 'JLinkGDBServerCLExe')
        touch(root / 'SEGGER' / 'JLink_V878' / 'JLinkDevices.xml')
        touch(root / 'SEGGER' / 'SystemView' / 'SystemView')
        touch(root / 'SEGGER' / 'SystemView_V360e' / 'SystemView')
        touch(root / 'usr' / 'bin' / 'openocd')
        touch(root / 'opt' / 'arm-gnu-toolchain-14.3.rel1-x86_64-arm-none-eabi' / 'bin'
              / 'arm-none-eabi-gdb')
        return root

    def test_the_version_less_install_wins_over_a_release_stamped_one(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._tree(t)
            hits = pl.search_install_directories('jlink_gdb_server', bases=[root])
        self.assertTrue(hits, 'the walk found nothing at all')
        self.assertEqual(Path(hits[0][0]).parent.name, 'JLink',
                         'the directory the user controls must win over the last installer')
        self.assertIn('version-less', hits[0][1])

    def test_the_release_stamped_install_is_still_offered(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._tree(t)
            hits = pl.search_install_directories('jlink_gdb_server', bases=[root])
        self.assertIn('JLink_V878', [Path(p).parent.name for p, _ in hits])

    def test_a_file_without_the_execute_bit_is_not_a_tool(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._tree(t)
            stripped = root / 'SEGGER' / 'JLink' / 'JLinkGDBServerCLExe'
            stripped.chmod(0o644)
            with mock.patch.object(pl.os, 'access', side_effect=lambda p, m: False):
                hits = pl.search_install_directories('jlink_gdb_server', bases=[root])
        self.assertNotIn(str(stripped), [str(p) for p, _ in hits])

    def test_a_stamped_arm_toolchain_is_found(self):
        # The Arm toolchain arrives as an extracted archive whose directory carries the release
        # in its name, so an exact path cannot be written down in advance.
        with tempfile.TemporaryDirectory() as t:
            root = self._tree(t)
            with mock.patch.object(pl, 'python_bin_dirs', return_value=[]):
                hits = pl.search_install_directories('gdb', bases=[root / 'opt'])
        self.assertTrue(hits)
        self.assertEqual(Path(hits[0][0]).name, 'arm-none-eabi-gdb')

    def test_openocd_is_found_in_a_plain_bin_directory(self):
        with tempfile.TemporaryDirectory() as t:
            root = self._tree(t)
            hits = pl.search_install_directories('openocd', bases=[root / 'usr' / 'bin'])
        self.assertEqual(Path(hits[0][0]).name, 'openocd')

    def test_a_symlinked_install_is_not_reported_twice(self):
        # /usr/bin/openocd -> /opt/openocd/bin/openocd is the normal packaging. Reporting the
        # same file once per spelling makes the "alternatives" list meaningless.
        with tempfile.TemporaryDirectory() as t:
            real = touch(Path(t) / 'real' / 'openocd')
            link = Path(t) / 'link'
            link.mkdir()
            try:
                (link / 'openocd').symlink_to(real)
            except (OSError, NotImplementedError):    # pragma: no cover - needs privilege
                self.skipTest('symlinks are not available to this account')
            hits = pl.search_install_directories('openocd', bases=[Path(t) / 'real', link])
        self.assertEqual(len(hits), 1, hits)

    def test_an_unknown_tool_key_returns_nothing_instead_of_raising(self):
        self.assertEqual(pl.search_install_directories('nonsense', bases=[]), [])


class FindToolTests(unittest.TestCase):
    def test_the_profile_value_wins_when_it_is_really_there(self):
        with tempfile.TemporaryDirectory() as t:
            real = touch(Path(t) / 'my-gdb')
            report = pl.find_tool('gdb', real, bases=[])
        self.assertEqual(report['source'], 'profile')
        self.assertTrue(report['exists'])
        self.assertEqual(report['found'], [], 'a working profile is never second-guessed')

    def test_a_profile_value_that_is_a_directory_is_completed_by_a_known_name(self):
        with tempfile.TemporaryDirectory() as t:
            real = touch(Path(t) / 'SystemView')
            report = pl.find_tool('systemview', Path(t), bases=[])
        self.assertEqual(report['path'], str(real))

    def test_a_path_hit_is_preferred_to_a_vendor_directory(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            packaged = touch(root / 'usr' / 'bin' / 'openocd')
            touch(root / 'opt' / 'OpenOCD' / 'bin' / 'openocd')
            with mock.patch.object(pl, 'search_path', side_effect=lambda n: packaged if n == 'openocd' else None):
                report = pl.find_tool('openocd', None, bases=[root / 'opt'])
        self.assertEqual(report['path'], str(packaged))
        self.assertEqual(report['source'], 'PATH: openocd')

    def test_every_path_spelling_is_tried_and_the_first_that_exists_wins(self):
        seen = []

        def fake_which(name):
            seen.append(name)
            return '/usr/bin/' + name if name == 'JLinkGDBServerCL' else None

        with mock.patch.object(pl, 'search_path', side_effect=fake_which), \
             mock.patch.object(pl, 'search_install_directories', return_value=[]):
            report = pl.find_tool('jlink_gdb_server', None, bases=[])
        self.assertEqual(report['path'], '/usr/bin/JLinkGDBServerCL')
        self.assertEqual(seen[:2], ['JLinkGDBServerCLExe', 'JLinkGDBServerCL'])
        self.assertEqual(report['source'], 'PATH: JLinkGDBServerCL')

    def test_a_miss_says_what_was_searched_and_never_raises(self):
        with mock.patch.object(pl, 'search_path', return_value=None), \
             mock.patch.object(pl, 'search_install_directories', return_value=[]):
            report = pl.find_tool('jlink_gdb_server', None, bases=[])
        self.assertFalse(report['exists'])
        self.assertEqual(report['source'], 'not found')
        self.assertEqual(report['searched'], ['profile', 'PATH', 'install-directories'])


class PlatformDispatchTests(unittest.TestCase):
    def test_the_inventory_map_names_a_module_per_platform(self):
        self.assertEqual(pt.PLATFORM_INVENTORY['posix'], 'piny_linux')
        self.assertEqual(pt.PLATFORM_INVENTORY['nt'], '')

    def test_windows_uses_the_vocabulary_in_piny_tools(self):
        self.assertIsNone(pt.inventory('nt'))
        self.assertEqual(pt.default_bases('nt'), pt.INSTALL_BASES)
        self.assertEqual(pt.path_names('gdb', 'nt'), pt.PATH_NAMES['gdb'])

    def test_linux_uses_the_vocabulary_in_piny_linux(self):
        self.assertIs(pt.inventory('posix'), pl)
        self.assertEqual(pt.default_bases('posix'), pl.INSTALL_BASES)
        self.assertEqual(pt.path_names('jlink_gdb_server', 'posix'),
                         pl.PATH_NAMES['jlink_gdb_server'])
        self.assertNotEqual(pt.default_bases('posix'), pt.INSTALL_BASES)

    def test_the_registry_is_not_searched_on_a_posix_platform(self):
        # Not "disabled by a flag": a registry does not exist there, and a report that lists it
        # under "searched" would be telling the user to go and look in it.
        with mock.patch.object(pt, 'inventory', return_value=pl), \
             mock.patch.object(pl, 'search_path', return_value=None), \
             mock.patch.object(pl, 'search_install_directories', return_value=[]):
            report = pt.discover({'probe': 'jlink', 'jlink_gdb_server': '/opt/x/JLinkGDBServerCLExe',
                                  'gdb': '/opt/y/arm-none-eabi-gdb'})
        self.assertNotIn('registry', report['tools']['gdb']['searched'])

    def test_a_linux_lookup_runs_the_jlink_search_without_windows_patterns(self):
        # piny_tools.find_tool must not fall back to the Windows INSTALL_PATTERNS when piny_linux
        # answers; the whole point of the split is that only one of them runs.
        calls = []

        def fake_linux_find(key, value=None, bases=None, with_registry=False):
            calls.append((key, value))
            return {'key': key, 'path': '/usr/bin/' + key, 'source': 'PATH: x', 'exists': True,
                    'searched': ['profile', 'PATH', 'install-directories'], 'found': []}

        with mock.patch.object(pt, 'inventory', return_value=pl), \
             mock.patch.object(pl, 'find_tool', side_effect=fake_linux_find), \
             mock.patch.object(pt, 'search_install_directories') as windows_search:
            report = pt.find_tool('gdb')
        self.assertEqual(calls, [('gdb', None)])
        self.assertEqual(report['path'], '/usr/bin/gdb')
        self.assertFalse(windows_search.called,
                         'the Windows directory walk ran on a Linux lookup')

    def test_the_module_imports_and_reports_a_platform_without_raising(self):
        report = pl.describe()
        self.assertEqual(report['platform'], 'linux')
        self.assertIn('jlink_gdb_server', report['patterns'])


if __name__ == '__main__':
    unittest.main()
