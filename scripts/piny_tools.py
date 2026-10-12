#!/usr/bin/env python3
"""Find the host tools a profile names, so a profile can move between Windows machines.

A profile records absolute paths, and those paths are the only part of this skill that is
bound to one machine: every script reads its executables out of the profile and hard-codes
none of them. That makes a profile portable in principle and useless in practice, because a
path copied from another machine fails with ``FileNotFoundError: [WinError 2] The system
cannot find the file specified`` and names neither the profile key nor the tool.

This module answers "where is this tool on THIS machine" with an explicit search order, and
always reports WHICH step answered:

    1. the profile's own value, when it points at a file that exists;
    2. PATH, via ``shutil.which`` (an installed toolchain usually lands here);
    3. the conventional install directories per tool;
    4. the uninstall/install registry keys SEGGER and Arm write (Windows only).

Nothing here writes anything, imports a third-party package, or raises when a tool is absent:
discovery is a report, and the caller decides what to do about a miss. Python standard
library only, like the rest of the package.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: Sentinel for "the caller did not say which search space to use", so that the default can be
#: computed FOR THE PLATFORM AT RUN TIME. A plain ``bases=INSTALL_BASES`` default cannot do
#: that: it is evaluated once, at import, and would hand a Windows tuple to a Linux host.
_DEFAULT = object()

#: Extensions a Windows executable can carry. On other platforms ``shutil.which`` handles
#: the executable bit and a bare name is right, so this is only consulted on Windows.
WINDOWS_EXE_SUFFIXES = ('.exe', '.bat', '.cmd')

#: Where each tool is installed by default, as (relative path, human label) pairs. The paths
#: are relative to a base directory taken from the environment or from ``BASE_DIRECTORIES``,
#: and the first existing candidate wins. Order matters: a year-stamped SEGGER directory
#: pins one release while the version-less name is a symlink the user controls, so the
#: version-less name is tried first and the stamped ones are kept as a fallback.
INSTALL_PATTERNS = {
    'jlink_gdb_server': (
        ('SEGGER/JLink/JLinkGDBServerCL.exe', 'SEGGER/JLink (version-less)'),
        ('SEGGER/JLink*/JLinkGDBServerCL.exe', 'SEGGER/JLink* (release-stamped)'),
    ),
    'systemview': (
        ('SEGGER/SystemView/SystemView.exe', 'SEGGER/SystemView (version-less)'),
        ('SEGGER/SystemView*/SystemView.exe', 'SEGGER/SystemView* (release-stamped)'),
    ),
    'openocd': (
        ('OpenOCD/bin/openocd.exe', 'OpenOCD/bin'),
        ('openocd/bin/openocd.exe', 'openocd/bin'),
        ('chocolatey/lib/openocd/tools/install/bin/openocd.exe', 'chocolatey openocd'),
    ),
}

#: Directories searched with ``INSTALL_PATTERNS``, most specific first. ``%ProgramFiles%``
#: style variables are expanded; unset variables drop out silently, which is what makes this
#: work on a machine that has only some of them. Linux has its own list, in ``piny_linux.py``.
INSTALL_BASES = (
    r'%ProgramFiles%',
    r'%ProgramFiles(x86)%',
    r'%ProgramW6432%',
    r'%ProgramData%',
    r'%LOCALAPPDATA%\Programs',
    r'%USERPROFILE%\scoop\apps',
    r'%ChocolateyInstall%',
    r'C:\Program Files',
    r'C:\Program Files (x86)',
)

#: Which module describes each platform's search space. The module is imported lazily, because
#: a Windows-only import here would make the whole package unimportable on the machine it is
#: supposed to migrate TO, and ``winreg`` is not the only such import (``ctypes.windll`` is
#: another). An empty value means "this machine uses the vocabulary defined in this file".
PLATFORM_INVENTORY = {'nt': '', 'posix': 'piny_linux'}

#: Registry locations that record an install directory rather than a bare executable. Each
#: entry is (tool key, hive, subkey, value names to try, relative executable path). The
#: value is read as a directory unless it already ends in an executable suffix.
REGISTRY_LOOKUPS = (
    ('jlink_gdb_server', 'HKLM', r'SOFTWARE\SEGGER\J-Link',
     ('InstallPath', 'InstallDir', 'Path'), 'JLinkGDBServerCL.exe'),
    ('jlink_gdb_server', 'HKLM', r'SOFTWARE\WOW6432Node\SEGGER\J-Link',
     ('InstallPath', 'InstallDir', 'Path'), 'JLinkGDBServerCL.exe'),
    ('systemview', 'HKLM', r'SOFTWARE\SEGGER\SystemView',
     ('InstallPath', 'InstallDir', 'Path'), 'SystemView.exe'),
    ('systemview', 'HKLM', r'SOFTWARE\WOW6432Node\SEGGER\SystemView',
     ('InstallPath', 'InstallDir', 'Path'), 'SystemView.exe'),
)

#: PATH names each tool is commonly installed under, tried when the profile key is absent
#: or wrong. ``jlink_gdb_server`` also covers ``JLinkGDBServer.exe`` (the GUI build), since
#: a machine may only have that one on PATH.
PATH_NAMES = {
    'gdb': ('arm-none-eabi-gdb',),
    'jlink_gdb_server': ('JLinkGDBServerCL', 'JLinkGDBServer'),
    'systemview': ('SystemView',),
    'openocd': ('openocd',),
}

#: Companion executables that live beside ``JLinkGDBServerCL.exe`` and are used by the trace
#: path (``JLink.exe`` for CommanderScript probes of RAM, ``JLinkRTTLogger.exe`` for RTT).
JLINK_COMPANIONS = ('JLink.exe', 'JLinkRTTLogger.exe', 'JLinkGDBServer.exe')


def _is_windows():
    return os.name == 'nt'


def _is_linux():
    """POSIX and not macOS: macOS has its own layouts and is deliberately not claimed here."""
    return os.name == 'posix' and sys.platform.startswith('linux')


def inventory(platform=None):
    """The layout module for ``platform`` (default: this machine), or None for the built-ins.

    Returning None -- rather than a module with the same names -- is the point: on Windows the
    vocabulary in this file IS the platform's, and a caller can tell the two apart.
    """
    platform = os.name if platform is None else platform
    if platform == 'nt':
        return None
    name = PLATFORM_INVENTORY.get(platform) or (PLATFORM_INVENTORY.get('posix')
                                                if platform.startswith('linux') else '')
    if not name:
        return None
    import importlib
    return importlib.import_module(name)


def default_bases(platform=None):
    """The install directories to search when the caller does not name any."""
    module = inventory(platform)
    return tuple(module.INSTALL_BASES) if module is not None else INSTALL_BASES


def path_names(key, platform=None):
    """The names ``key`` is installed under on PATH, most preferred first."""
    module = inventory(platform)
    if module is not None:
        return tuple(module.PATH_NAMES.get(key, ()))
    return tuple(PATH_NAMES.get(key, ()))


def companion_names(platform=None):
    """Executables that live beside the J-Link GDB server on this platform."""
    module = inventory(platform)
    return tuple(module.JLINK_COMPANIONS) if module is not None else JLINK_COMPANIONS


def _existing_file(candidate):
    """``Path(candidate)`` when it is a real file, else None. Never raises on junk input."""
    if not candidate:
        return None
    try:
        path = Path(str(candidate).strip().strip('"'))
    except (TypeError, ValueError):
        return None
    return path if path.is_file() else None


def _candidates_named(directory, name):
    """A concrete name and any extensioned variants that exist in ``directory``."""
    if not directory:
        return []
    base = Path(directory)
    names = [name]
    if _is_windows() and not name.lower().endswith(WINDOWS_EXE_SUFFIXES):
        names += [name + suffix for suffix in WINDOWS_EXE_SUFFIXES]
    found = []
    for candidate in names:
        path = base / candidate
        if path.is_file():
            found.append(path)
    return found


def search_path(name):
    """The PATH hit for ``name``, if any. Wrapper kept separate so tests can patch one place."""
    hit = shutil.which(name)
    return Path(hit) if hit else None


def search_install_directories(key, bases=_DEFAULT):
    """install-directory hits for ``key`` on THIS platform, as (path, label) pairs, best first.

    On Linux the walk itself belongs to ``piny_linux.py``, because the names there are prefixes
    rather than files: SEGGER's Linux builds append letters where Windows appends ``.exe``. The
    Linux module is asked first and its result is returned unchanged, so there is exactly one
    implementation per platform rather than a merged list that nobody can reason about.
    """
    if bases is _DEFAULT:
        module = inventory()
        if module is not None:
            return module.search_install_directories(key)
        bases = INSTALL_BASES
    patterns = INSTALL_PATTERNS.get(key, ())
    if not patterns:
        return []
    resolved = []
    for raw in bases:
        expanded = os.path.expandvars(raw)
        if '%' in expanded:            # an unset variable: nothing to search here
            continue
        base = Path(expanded)
        if not base.is_dir():
            continue
        resolved.append(base)
    hits = []
    for base in resolved:
        for pattern, label in patterns:
            if '*' in pattern:
                for match in sorted(base.glob(pattern)):
                    if match.is_file():
                        hits.append((match, label))
            else:
                candidate = base / pattern
                if candidate.is_file():
                    hits.append((candidate, label))
    return hits


def read_registry_directory(hive, subkey, value_names):
    """Read an install directory out of the Windows registry, or None. Never raises.

    ``winreg`` is only imported here, so the module still imports on a machine without it and
    a locked-down session without registry access degrades to "no registry hit" instead of
    taking the whole command down. That matters because discovery runs before anything else
    and must not be the reason a debug session cannot start.
    """
    if not _is_windows():
        return None
    try:
        import winreg
    except ImportError:
        return None
    root = getattr(winreg, {'HKLM': 'HKEY_LOCAL_MACHINE', 'HKCU': 'HKEY_CURRENT_USER',
                            'HKCR': 'HKEY_CLASSES_ROOT'}.get(hive, 'HKEY_LOCAL_MACHINE'), None)
    if root is None:
        return None
    try:
        with winreg.OpenKey(root, subkey) as handle:
            for name in value_names:
                try:
                    value, _ = winreg.QueryValueEx(handle, name)
                except OSError:
                    continue
                if value:
                    return str(value)
    except OSError:
        return None
    return None


def search_registry(key):
    hits = []
    for entry_key, hive, subkey, value_names, exe in REGISTRY_LOOKUPS:
        if entry_key != key:
            continue
        raw = read_registry_directory(hive, subkey, value_names)
        if not raw:
            continue
        path = Path(raw)
        candidate = path if path.is_file() else path / exe
        if candidate.is_file():
            hits.append((candidate, 'registry: ' + hive + '\\' + subkey))
    return hits


def _deduplicate(found):
    """Drop repeat paths, keeping the first label. Windows compares paths case-insensitively.

    Without this, the same file shows up once per step that found it -- a PATH hit is usually
    also an install-directory hit and a registry hit -- and the "alternatives" list stops being
    a list of alternatives at all.
    """
    seen = set()
    unique = []
    for path, label in found:
        try:
            key = os.path.normcase(os.path.abspath(str(path)))
        except (OSError, ValueError):
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append((path, label))
    return unique


def _delegate(key, profile_value, bases, with_registry):
    """Ask the platform module, if this machine has one, to do the whole lookup.

    Delegating the WHOLE lookup rather than only the directory walk keeps one platform's
    preferences in one file: on Linux the order of PATH spellings and of name prefixes decides
    which of two real J-Link builds is run, and splitting that decision across two modules is
    how a machine ends up starting the GUI server it cannot drive.
    """
    module = inventory()
    if module is None:
        return None
    return module.find_tool(key, profile_value, bases=None if bases is _DEFAULT else bases,
                            with_registry=with_registry)


def find_tool(key, profile_value=None, bases=_DEFAULT, with_registry=True):
    """Locate one tool and say which step answered.

    Returns ``{'key','path','source','exists','searched','found'}``. ``path`` is the profile's
    own value whenever that value is a real file -- an explicit configuration always wins, so
    discovery can never quietly redirect a working setup. ``found`` is the list of every
    candidate the later steps turned up, so a caller can show the alternatives rather than
    hiding them. ``bases`` defaults to the platform's own list, resolved at call time.
    """
    delegated = _delegate(key, profile_value, bases, with_registry)
    if delegated is not None:
        return delegated
    if bases is _DEFAULT:
        bases = INSTALL_BASES
    searched = ['profile', 'PATH', 'install-directories']
    if with_registry:
        searched.append('registry')
    explicit = _existing_file(profile_value)
    if explicit is not None:
        return {'key': key, 'path': str(explicit), 'source': 'profile', 'exists': True,
                'searched': searched, 'found': []}
    found = []
    for name in PATH_NAMES.get(key, ()):
        hit = search_path(name)
        if hit is not None:
            found.append((hit, 'PATH: ' + name))
            break
    found += search_install_directories(key, bases)
    if with_registry:
        found += search_registry(key)
    found = _deduplicate(found)
    if found:
        path, label = found[0]
        return {'key': key, 'path': str(path), 'source': label, 'exists': True,
                'searched': searched,
                'found': [{'path': str(p), 'source': l} for p, l in found]}
    return {'key': key, 'path': str(profile_value) if profile_value else None,
            'source': 'not found' if not profile_value else 'profile value is not a file',
            'exists': False, 'searched': searched, 'found': []}


#: Profile keys that name an executable this skill starts, and what each is for. The
#: description is what turns a miss into an instruction instead of an OS error message, so it
#: names the TOOL rather than one platform's spelling of its file name.
TOOL_KEYS = {
    'gdb': 'the Arm GNU toolchain GDB (the MI client that talks to the debug transport)',
    'jlink_gdb_server': "SEGGER's J-Link GDB server CLI (the transport for a SEGGER probe)",
    'systemview': "SEGGER's SystemView (the CPU-load recorder and CSV exporter)",
    'openocd': 'OpenOCD (only needed for a non-SEGGER probe)',
}

#: Which profile keys each probe actually needs, so a miss can be judged fatal or not.
REQUIRED_BY_PROBE = {
    'jlink': ('jlink_gdb_server', 'gdb'),
    'openocd': ('openocd', 'gdb'),
}


def discover(profile, bases=_DEFAULT, with_registry=_DEFAULT):
    """Resolve every tool key in ``profile`` and report what is missing and what to do.

    Returns ``{'tools','resolved','missing','required','usable','summary'}``. ``resolved`` maps
    a profile key to the path that should actually be used, which lets the caller run from a
    discovered path without the profile being rewritten first. Both ``bases`` and
    ``with_registry`` default to the platform's own answer: a registry lookup on Linux is not
    "disabled", it is meaningless, and saying so is the platform module's job.
    """
    if with_registry is _DEFAULT:
        with_registry = inventory() is None        # only Windows looks in a registry
    probe = profile.get('probe') or 'openocd'
    required = REQUIRED_BY_PROBE.get(probe, ())
    tools = {}
    for key, description in TOOL_KEYS.items():
        if not profile.get(key) and key not in required:
            continue                      # optional and unconfigured: not a migration problem
        report = find_tool(key, profile.get(key), bases=bases, with_registry=with_registry)
        report['description'] = description
        report['required'] = key in required
        tools[key] = report
    resolved = {k: v['path'] for k, v in tools.items() if v['exists']}
    missing = [k for k, v in tools.items() if v['required'] and not v['exists']]
    return {'tools': tools, 'resolved': resolved, 'missing': missing, 'required': list(required),
            'probe': probe, 'usable': not missing}


def jlink_companions(server_path):
    """The J-Link CLIs that sit beside a resolved GDB server, with their existence.

    Which names to look for is the platform's business: Windows has ``JLink.exe`` and
    ``JLinkRTTLogger.exe``, Linux the same tools spelled ``JLinkExe`` and ``JLinkRTTLoggerExe``.
    """
    base = Path(server_path).parent if server_path else None
    out = {}
    for name in companion_names():
        candidate = (base / name) if base else None
        out[name] = {'path': str(candidate) if candidate else None,
                     'exists': bool(candidate and candidate.is_file())}
    return out


def _normalise_path(text):
    """One spelling for one file, so two working paths are never compared as text.

    A profile written by hand on Windows carries forward slashes ("E:/Ele/Tool/..."), which is
    the shape JSON examples use, while every path discovery derives is a native ``Path`` with
    backslashes. Comparing those as strings reported an already-correct tool as a discrepancy
    and told the user to replace a working path with the same path -- which teaches them to
    ignore the suggestion. Case, slash direction and '.'/'..' segments are all irrelevant here,
    so they are removed before any comparison.
    """
    try:
        return os.path.normcase(os.path.abspath(str(text)))
    except (OSError, ValueError):
        return str(text).replace('/', os.sep).replace('\\', os.sep).casefold()


def _same_path(left, right):
    return _normalise_path(left) == _normalise_path(right)


def profile_overrides(discovery, profile, extra=None):
    """A profile-shaped dict of only the keys whose resolved path differs from the profile.

    This is what a user pastes into their own profile, so it deliberately contains nothing
    else: no defaults, no comments, no values that were already correct.
    """
    overrides = {}
    for key, path in sorted(discovery['resolved'].items()):
        if not _same_path(profile.get(key) or '', path):
            overrides[key] = path
    if extra:
        overrides.update({k: v for k, v in extra.items() if v is not None})
    return overrides
