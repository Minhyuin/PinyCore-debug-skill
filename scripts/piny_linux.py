#!/usr/bin/env python3
"""Where the host tools live on Linux, for the platform-independent search in piny_tools.

Why this is a separate module
-----------------------------
The Windows search can afford to name exact files, because SEGGER and Arm both ship one
spelling per tool and a registry key pins the install directory. Linux has neither:

  * SEGGER's Linux packages have changed the spelling of their executables more than once.
    The J-Link GDB server is ``JLinkGDBServerCLExe`` -- a stray ``Exe`` where Windows writes
    ``.exe`` -- while the GUI build is ``JLinkGDBServer`` in some releases and
    ``JLinkGDBServer.exe`` in others. SystemView is ``SystemView``, and the splash dialog this
    skill has to dismiss is titled ``SystemView splash screen``.
  * The install directory carries a release stamp (``/opt/SEGGER/JLink_V878``,
    ``/opt/arm-gnu-toolchain-14.3.rel1-x86_64-arm-none-eabi``), and the version-less name the
    vendor's installer creates is a directory the user is free to repoint.
  * There is no registry. What a package manager recorded lives in the dpkg/rpm databases,
    which are not worth parsing from here while PATH already answers for a packaged install.

So this file describes the search SPACE -- directories and name PREFIXES -- and
``piny_tools.search_install_directories`` walks it. Refusing to look in the right directory is
worse than looking for a name that is not there, which is why a prefix is allowed to be a
little loose; ``_STRICT_SUFFIXES`` is what keeps it from being reckless.

What is verified
----------------
Nothing here has been run against a real Linux install of J-Link, SystemView or the Arm
toolchain: this machine is Windows, and the directories and spellings below are taken from the
vendors' installers and package layout rather than from a live machine. What IS verified is the
search itself -- globbing, expansion of the ``$HOME`` spellings, prefix preference order and
the executable-bit requirement are unit-tested and run on Linux in CI. A misspelled prefix
therefore shows up as "tool not found, here is what I searched", never as a crash and never as
the wrong file being run.

Python standard library only, like the rest of the package.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

#: Directories walked for install patterns, in the order a user would expect them to win.
#: ``/usr/bin`` and ``/usr/local/bin`` come first because a distribution package or a
#: ``make install`` is the spelling a user can explain; ``/opt/SEGGER`` is the vendor default
#: for the Linux installers, and ``/snap/bin`` covers a snap-packaged probe host.
#: ``$HOME``-style spellings are expanded by the caller and vanish when unset, exactly as the
#: ``%ProgramFiles%`` spellings do on Windows.
INSTALL_BASES = (
    '/usr/local/bin',
    '/usr/bin',
    '/opt/SEGGER',
    '/opt',
    '/usr/local',
    '/snap/bin',
    '/app',
    r'%HOME%/.local/bin',
    r'%HOME%/.local/opt',
    r'%XDG_DATA_HOME%',
)

#: Executable names on PATH, most preferred first. A distribution package is what a user can
#: explain, so PATH is asked before the vendor directories are globbed. The J-Link server has
#: two builds: the command-line one, which is the only one this skill can drive, and the GUI
#: one, which a machine may have instead.
PATH_NAMES = {
    'gdb': ('arm-none-eabi-gdb',),
    'jlink_gdb_server': ('JLinkGDBServerCLExe', 'JLinkGDBServerCL', 'JLinkGDBServer',
                         'JLinkGDBServer.exe'),
    'systemview': ('SystemView',),
    'openocd': ('openocd',),
}

#: Executable-name prefixes per tool key, in preference order, for each search directory.
#: A prefix is matched exactly, or with one of ``_STRICT_SUFFIXES`` appended -- SEGGER's Linux
#: builds do carry letters after the Windows spelling of the name, but only those letters.
#: Being loose here is what lets one entry cover a J-Link release that renamed its binary,
#: while ``JLinkDevices.xml`` and ``JLinkGUIServerExe`` stay filtered out.
INSTALL_PATTERNS = {
    'gdb': (('arm-none-eabi-gdb', 'Arm GNU toolchain'),),
    'jlink_gdb_server': (('JLinkGDBServerCL', 'SEGGER J-Link GDB server'),
                         ('JLinkGDBServer', 'SEGGER J-Link GDB server (GUI build)')),
    'systemview': (('SystemView', 'SEGGER SystemView'),),
    'openocd': (('openocd', 'OpenOCD'),),
}

#: Labels per install DIRECTORY glob, so a report can say whether a hit came from the
#: version-less directory the user controls or from whatever release the last installer left
#: behind. Two installs that both answer are not equally good, and a report that cannot say
#: which one answered leaves the user to guess.
INSTALL_DIR_LABELS = {
    'SEGGER/JLink': 'version-less',
    'SEGGER/JLink*': 'release-stamped',
    'SEGGER/SystemView': 'version-less',
    'SEGGER/SystemView*': 'release-stamped',
    'arm-gnu-toolchain*/bin': 'release-stamped',
    'ArmGNUToolchain/*/bin': 'release-stamped',
    'OpenOCD/bin': 'vendor',
    'openocd/bin': 'vendor',
    '': 'bin',
}

#: Directories globbed for those prefixes, relative to a base. A ``*`` is a release stamp:
#: ``SEGGER/JLink_V878``, ``arm-gnu-toolchain-14.3.rel1-x86_64-arm-none-eabi``. An empty entry
#: means the base directory itself, which is how a distribution package (``/usr/bin/openocd``)
#: and a vendor extraction directory (``/opt/SEGGER/JLink``) are both covered.
INSTALL_DIRS = ('SEGGER/JLink', 'SEGGER/JLink*', 'SEGGER/SystemView', 'SEGGER/SystemView*',
                'arm-gnu-toolchain*/bin', 'ArmGNUToolchain/*/bin', 'OpenOCD/bin', 'openocd/bin',
                '')

#: Letters a Linux build of a Windows-named tool may add. ``JLinkGDBServerCLExe`` is the real
#: case: SEGGER writes ``Exe`` where Windows writes ``.exe``.
_STRICT_SUFFIXES = ('Exe', 'CL', 'CLExe', '.exe')

#: Executables that live beside the resolved J-Link server and are used by the trace path.
JLINK_COMPANIONS = ('JLinkExe', 'JLinkRTTLoggerExe', 'JLink', 'JLinkRTTLogger',
                    'JLinkGDBServerCLExe', 'JLinkGDBServer')

#: Digits in a path segment mean a release stamp, e.g. ``JLink_V878`` or ``...-14.3.rel1-...``.
#: Used only to rank two real candidates, never to filter one out.
_STAMP = re.compile(r'[0-9]')


def base_directories():
    """``INSTALL_BASES`` with ``$HOME``-style spellings expanded and unsearchable ones dropped."""
    resolved = []
    for raw in INSTALL_BASES:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if '%' in expanded or '$' in expanded:      # an unset variable: nothing to search there
            continue
        if os.path.isdir(expanded):
            resolved.append(expanded)
    return resolved


def python_bin_dirs():
    """The ``bin`` directory of the running interpreter, which is never in PATH under sudo.

    A virtualenv whose ``bin`` holds ``arm-none-eabi-gdb`` is a legitimate way to pin a
    toolchain, and it is discovered rather than guessed from ``sys.executable``, because the
    interpreter may itself be a symlink: the resolved path is what is used.
    """
    found = []
    for candidate in (sys.executable, os.path.realpath(sys.executable or '')):
        if not candidate:
            continue
        directory = os.path.dirname(os.path.abspath(candidate))
        if directory and os.path.isdir(directory) and directory not in found:
            found.append(directory)
    return found


def _matches_prefix(name, prefix):
    """``name`` is the prefix itself, or the prefix plus one known build spelling."""
    name = (name or '').strip()
    if not name or name == prefix:
        return bool(name)
    if name.startswith(prefix):
        return name[len(prefix):] in _STRICT_SUFFIXES
    return False


def _executable(candidate):
    """Regular file with the execute bit set: a directory named like the tool is not a tool."""
    return candidate.is_file() and os.access(str(candidate), os.X_OK)


def _version_less_first(path):
    """Sort key that prefers a stamp-free directory, then a shorter path, then the plain text.

    ``/opt/SEGGER/JLink/`` is the spelling the user chose or repointed, while
    ``/opt/SEGGER/JLink_V878/`` is whatever the last installer left behind, so the former has
    to win even though both are real. The extra keys keep the order deterministic without
    pretending to know which release is newer.
    """
    text = str(path)
    stamped = bool(_STAMP.search(os.path.basename(os.path.dirname(text))))
    return (stamped, len(text), text)


def _directories(base):
    """Every existing directory this platform convention names under ``base``, best first.

    Returns ``(path, label)`` pairs: the label says whether the directory is the version-less
    one the user controls or a release-stamped one an installer left behind.
    """
    found = []
    for relative in INSTALL_DIRS:
        if not relative:
            candidates = [Path(base)]
        elif '*' in relative:
            candidates = sorted(Path(base).glob(relative), key=_version_less_first)
        else:
            candidates = [Path(base) / relative]
        for candidate in candidates:
            try:
                if candidate.is_dir() and all(candidate != known for known, _ in found):
                    found.append((candidate, INSTALL_DIR_LABELS.get(relative, 'install')))
            except OSError:
                continue
    return found


def search_install_directories(key, bases=None):
    """Every executable under ``bases`` whose name starts like this tool, best first.

    ``(path, label)`` pairs. The prefix order is the preference order: a distribution-package
    spelling is asked for at every base before a vendor directory is globbed, so a hit is never
    a consequence of which directory happened to be walked first.
    """
    prefixes = INSTALL_PATTERNS.get(key, ())
    if not prefixes:
        return []
    search_bases = list(bases) if bases is not None else base_directories()
    if key in ('gdb', 'systemview'):
        search_bases += python_bin_dirs()
    hits = []
    for prefix, label in prefixes:
        for raw_base in search_bases:
            expanded = os.path.expandvars(os.path.expanduser(str(raw_base)))
            if '%' in expanded or '$' in expanded or not os.path.isdir(expanded):
                continue
            for directory, where in _directories(expanded):
                try:
                    entries = sorted(directory.iterdir(), key=_version_less_first)
                except OSError:
                    continue
                for entry in entries:
                    if _matches_prefix(entry.name, prefix) and _executable(entry):
                        hits.append((entry, label + ' (' + where + ')'))
    return hits


def _deduplicate(found):
    seen = set()
    unique = []
    for path, label in found:
        key = os.path.realpath(str(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append((path, label))
    return unique


def search_path(name):
    """The PATH hit for ``name``, if any. Wrapper kept separate so tests can patch one place."""
    hit = shutil.which(name)
    return Path(hit) if hit else None


def search_path_names(key):
    """One PATH hit per spelling, in preference order. ``shutil.which`` already checks the bit."""
    hits = []
    for name in PATH_NAMES.get(key, ()):
        hit = search_path(name)
        if hit is not None:
            hits.append((hit, 'PATH: ' + name))
    return hits


def find_tool(key, profile_value=None, bases=None, with_registry=False):
    """Locate one tool on Linux and say which step answered.

    The contract is ``piny_tools.find_tool``'s: an explicit profile value always wins when it
    is a real file, and the return value is the same dict. ``with_registry`` is accepted and
    ignored -- there is no registry here -- so a caller can stay platform-blind.
    """
    searched = ['profile', 'PATH', 'install-directories']
    explicit = None
    if profile_value:
        text = os.path.expandvars(os.path.expanduser(str(profile_value)))
        candidate = Path(text)
        if candidate.is_file():
            explicit = candidate
        elif candidate.is_dir():
            for name in PATH_NAMES.get(key, ()):
                inner = candidate / name
                if _executable(inner):
                    explicit = inner
                    break
    if explicit is not None:
        return {'key': key, 'path': str(explicit), 'source': 'profile', 'exists': True,
                'searched': searched, 'found': []}
    found = _deduplicate(search_path_names(key) + search_install_directories(key, bases))
    if found:
        path, label = found[0]
        return {'key': key, 'path': str(path), 'source': label, 'exists': True,
                'searched': searched,
                'found': [{'path': str(p), 'source': l} for p, l in found]}
    return {'key': key, 'path': str(profile_value) if profile_value else None,
            'source': 'not found' if not profile_value else 'profile value is not a file',
            'exists': False, 'searched': searched, 'found': []}


def describe():
    """What this platform search covers, for ``doctor --portable`` on a Linux host."""
    return {'platform': 'linux', 'bases': base_directories(), 'python_bin': python_bin_dirs(),
            'patterns': {k: [p for p, _ in v] for k, v in INSTALL_PATTERNS.items()}}
