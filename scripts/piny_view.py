#!/usr/bin/env python3
"""Answer SEGGER SystemView's blocking licence dialog, on Windows or on Linux.

Why this module exists
----------------------
SystemView V3.60e shows a modal Qt dialog ("No Commercial-Use License | SEGGER SystemView
V3.60e") before its window is created. While that dialog is open the process never parses its
command line, so ``SystemView -load R.svd -export out.csv -quit`` simply hangs until it is
killed. The dialog therefore has to be answered by whoever drives the CLI, and it has no flag
to suppress it.

This file is the PLATFORM-NEUTRAL part: the titles and geometry both backends share, the choice
of backend, and one report shape for both. The two backends are deliberately separate files,
because the way a dialog is answered is the one thing that genuinely differs by platform:

  * ``piny_view_win32`` posts window messages. It works on a locked desktop, where a
    synthesised pointer move never lands (measured: a full-screen BitBlt comes back white and
    ``GetForegroundWindow`` returns 0), but ``PostMessageW`` still reaches its window.
  * ``piny_view_x11`` sends X events and, when that does not take, drives a real pointer click
    through XTest. X11 has no exact equivalent of ``PostMessage``, so the fallback is not
    optional there.

Both report the same keys, so a caller never has to care which one ran. A platform with no
backend reports ``answered: False`` with a reason instead of pretending or raising: the caller
is expected to run SystemView by hand there and can still parse the export.

Python standard library only, like the rest of the package.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import sys
import time

#: Title of the blocking modal. Matched case-insensitively as a substring.
DIALOG_TITLE = 'No Commercial-Use License'
#: Window title of the SystemView main window (proves the modal is gone).
MAIN_TITLE = 'SEGGER SystemView'
#: Accept button position as a fraction of the dialog client area. SEGGER lays this
#: dialog out identically on every launch of a given version; measured across runs
#: the dialog rect was stable to the pixel (401,237)-(1135,627).
ACCEPT_FRACTION = (0.80, 0.90)
#: Default button of a SystemView message box as a fraction of its rectangle. The boxes
#: are small and single-button, so the middle of the bottom bar is the OK button.
DEFAULT_BUTTON_FRACTION = (0.50, 0.88)
#: One click press/release pair, kept short so the modal is not missed.
CLICK_SETTLE_S = 0.35
#: The dialog is measurable before it is responsive. Clicking the instant it exists has
#: been observed to land on the button without effect, so wait this long after first
#: sighting before the first press, and this long between retries.
DIALOG_ARM_S = 3.5
RETRY_INTERVAL_S = 3.0
#: Titles of the modal boxes SystemView raises over its main window. They are ordinary Qt
#: windows, not Win32 #32770 message boxes, so they are matched by title. While one is open
#: every later control command is ignored, which makes a recording run that leaves one behind
#: look exactly like a recorder that cannot reach the target.
_MESSAGEBOX_TITLES = ('System Information', 'SystemView Error', 'SEGGER SystemView Error')
#: License-dialog button layout. The padding values are measured on V3.60e and are toolkit
#: metrics rather than Windows metrics, so both backends use them.
BUTTON_PAD_PX = 14
BUTTON_GAP_PX = 10
BUTTON_EDGE_PX = 12
BUTTON_BOTTOM_PX = 12
BUTTON_HEIGHT_PX = 26
BUTTON_FONT_PX = 9

#: How long to let a freshly started SystemView settle before the first press, per platform.
#: The Windows value was measured against V3.60e; the X11 one has NOT been measured on a real
#: X session -- it is a deliberately generous starting point, and `accept_license` retries
#: every `RETRY_INTERVAL_S` while the dialog is up, so arriving early costs one retry rather
#: than the run. Lowering it on a real machine is a measurement, not a guess to copy from here.
ARM_SECONDS = {'nt': DIALOG_ARM_S, 'posix': 4.0}

#: Backend module per platform, most capable first. Resolved at call time rather than imported
#: at module level: a Windows-only backend imported here would make this file unimportable on
#: Linux, and each backend imports the constants above, so an eager import would also be a
#: circular one. The list is a tuple so a caller (or a test) can see the whole preference order
#: instead of having to infer it.
BACKENDS = {
    'nt': ('piny_view_win32',),
    'posix': ('piny_view_x11',),
}


def _candidates():
    """Backend module names for this platform, in preference order."""
    platform = 'nt' if os.name == 'nt' else 'posix'
    return BACKENDS.get(platform, ())


def backend():
    """The first backend module that can actually work here, or None.

    "Can work" is the backend's own answer, not a platform name: ``piny_view_x11`` reports
    False when there is no X display (a plain SSH session, a container), because a backend
    that cannot reach a display must not be chosen just because the platform matches.
    """
    import importlib
    for name in _candidates():
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        try:
            if module.available():
                return module
        except Exception:                    # a broken display is a reason, not a crash
            continue
    return None


def backend_name():
    """The backend that would be used, for a report. ``None`` when there is none."""
    module = backend()
    return module.__name__.rsplit('.', 1)[-1] if module is not None else None


def available():
    """True when this machine can answer the dialog without a human."""
    return backend() is not None


def _call(name, *args, **kwargs):
    """Call a backend function, or raise an error that says why it cannot be called.

    Raising is right for the functions that only run once a dialog has been found: by then the
    caller has already established that a dialog exists, so a missing backend is a genuine
    contradiction rather than a platform without support. The lookups that may legitimately
    find nothing (``find_dialog`` and friends) return None instead.
    """
    module = backend()
    if module is None:
        raise RuntimeError('License dialog handling needs a supported desktop session: on '
                           'Windows user32, on Linux an X display (and XTest). This session '
                           'has neither, so the dialog has to be answered by hand.')
    _overlay(module)
    return getattr(module, name)(*args, **kwargs)


#: The backend surface this module forwards. A forwarding entry is a stand-in for a platform
#: function, so a caller (or a test) can replace it here -- ``piny_view.child_windows = fake`` --
#: without having to reach into the backend module by name. Only entries that were REPLACED are
#: pushed down; see ``_overlay``. ``_pid`` is deliberately absent: which process owns a window is
#: a genuinely per-platform question (``GetWindowThreadProcessId`` versus the ``_NET_WM_PID``
#: property, which X11 may not even have), so each backend answers it inside its own ``windows()``
#: rather than being forced into a shared shape.
_FORWARDED = ('_text_width', '_stable_rect', '_client_size', '_enumerate_windows',
              '_child_text', 'windows', 'child_windows', 'find_dialog', 'find_main_window',
              'find_message_box')


def _backend_call(name, *args, **kwargs):
    """Call ``name`` in the backend that is chosen right now, or ``None`` when there is none.

    A forwarding function and a replaced one both have to work, and the difference only shows up
    when someone patches a name here (a test, or a caller pinning a metric):

      * not replaced -- the forwarding function would call itself, so the backend's own function
        is reached by looking it up in the module here, at call time;
      * replaced -- the replacement is what runs.

    Looking it up at call time rather than copying it into the backend module is what keeps the
    patch scoped to the caller. A copy lives on: the next call from anywhere would still use the
    previous caller's stand-in, which on a real X display means poking a window handle that only
    ever existed in a test.
    """
    module = backend()
    if module is None:
        return None
    function = globals().get(name)
    if getattr(function, '_dispatch_forward', False):
        return getattr(module, name)(*args, **kwargs)
    return function(*args, **kwargs)               # a replacement: calling it IS the point


def _overlay(module):
    """Point ``module``'s forwarded names at this file, so a patch here reaches the backend.

    An entry still holding its original forwarding function must NOT be copied down (it forwards
    to the same name, so the call would recurse until the stack runs out). What is installed
    instead is a small proxy that decides at call time which of the two it is -- see
    ``_backend_call``. The ``_dispatch_forward`` marker is what distinguishes the two cases.
    """
    for name in _FORWARDED:
        function = globals().get(name)
        if function is None:
            continue
        if getattr(function, '_dispatch_forward', False):
            setattr(module, name, _proxy(name))
        else:
            setattr(module, name, function)


def _proxy(name):
    def call(*args, **kwargs):
        return _backend_call(name, *args, **kwargs)
    call.__name__ = name
    call._dispatch_forward = True                  # never proxied twice
    return call


def _text_width(text, extent=BUTTON_FONT_PX):
    """Measure a button label. Forwarded so a caller can pin the font metric."""
    return _call('_text_width', text, extent)
_text_width._dispatch_forward = True


def _optional(name, default, *args, **kwargs):
    """Call a backend function when there is one, else return ``default``."""
    module = backend()
    if module is None or not hasattr(module, name):
        return default
    _overlay(module)
    return getattr(module, name)(*args, **kwargs)


def windows(process_id=None):
    """Visible top-level windows, optionally filtered to one process id."""
    return _optional('windows', [], process_id)
windows._dispatch_forward = True


def find_dialog(process_id=None):
    """The licence dialog's window, or None."""
    return _optional('find_dialog', None, process_id)
find_dialog._dispatch_forward = True


def find_main_window(process_id=None, timeout=0.0, poll=0.25):
    """The SystemView main window, waiting up to ``timeout`` seconds for it to appear."""
    return _optional('find_main_window', None, process_id, timeout, poll)
find_main_window._dispatch_forward = True


def find_message_box(process_id=None):
    """A modal box raised over the main window, or None."""
    return _optional('find_message_box', None, process_id)
find_message_box._dispatch_forward = True


def child_windows(hwnd):
    """Child windows of ``hwnd``. Empty is the normal answer on both platforms."""
    return _optional('child_windows', [], hwnd)
child_windows._dispatch_forward = True


def find_default_button(window):
    """The dialog's default button, or None when the toolkit exposes no child controls."""
    return _optional('find_default_button', None, window)


def arm_seconds(platform=None):
    """The settle delay to use before the first press on this platform."""
    key = platform or ('nt' if os.name == 'nt' else 'posix')
    return ARM_SECONDS.get(key, DIALOG_ARM_S)


def button_layout(window, client=None):
    """Where the dialog's buttons are, in window coordinates."""
    return _call('button_layout', window, client)


def post_click(window, point):
    """Press a point of a window directly, without moving the pointer."""
    return _call('post_click', window, point)


def post_enter(window):
    """Post Return to a window. Recorded as a dead end: it DECLINES the licence."""
    return _call('post_enter', window)


def click_point(window, point, settle=CLICK_SETTLE_S):
    """Press the pointer at a point of a window."""
    return _call('click_point', window, point, settle)


def click_accept(window, fraction=ACCEPT_FRACTION, process_id=None):
    """Press the dialog's Accept button, preferring a posted click over a real one."""
    return _call('click_accept', window, fraction, process_id)


#: What ``dismiss_message_box`` reports when there is no box to dismiss. A dict rather than None
#: because callers test it for truthiness and then read keys out of it -- ``piny_trace`` polls
#: exactly that way -- so "nothing was there" has to be a report, not an absence.
_MESSAGEBOX_ABSENT = {'seen': False, 'dismissed': False, 'title': None,
                      'note': 'no message box'}


def dismiss_message_box(process_id=None, fraction=None):
    """Dismiss a SystemView message box if one is up. Reports what it saw."""
    return _optional('dismiss_message_box', dict(_MESSAGEBOX_ABSENT), process_id, fraction)


def _alive(process_id):
    """True when ``process_id`` is still running. Used to tell refusal from acceptance.

    Kept here rather than in a backend because it needs no window system, and because both
    platforms have a case where a process that has just exited is still listed: Windows keeps a
    handle open (exit code 259 is STILL_ACTIVE) and Linux keeps a zombie entry in /proc.
    """
    if not process_id:
        return False
    if os.name == 'nt':
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(process_id))
        if not process:
            return False
        try:
            code = ctypes.wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code))
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        with open('/proc/%d/stat' % int(process_id), 'rb') as handle:
            fields = handle.read().rsplit(b')', 1)[-1].split()
        return bool(fields) and fields[0] != b'Z'
    except (OSError, ValueError):
        pass
    try:
        os.kill(int(process_id), 0)
    except OSError as error:
        return getattr(error, 'errno', None) == 1        # EPERM: alive, just not ours
    except (TypeError, ValueError):
        return False
    return True


def accept_license(process_id=None, timeout=30.0, auto_accept=True, poll=0.25, fraction=None,
                   arm_seconds=None, retry_interval=RETRY_INTERVAL_S, main_window_timeout=0.0):
    """Answer the SystemView license dialog.

    Returns a report dict. ``answered`` is True when the dialog was seen and is gone from
    the screen at the end. When ``auto_accept`` is False the dialog is only observed: the
    caller has to click it and the routine reports what it saw. The press is retried every
    ``retry_interval`` seconds while the dialog remains, because a press that arrives
    before the modal is responsive is silently ignored by Qt.

    With ``main_window_timeout`` > 0 the main window is looked for after the dialog goes
    away, and the result is reported in ``main_window``. That is what distinguishes "the
    modal was dismissed" from "the application actually continued": an accepted dialog
    whose main window never appears means the launch is wedged and the caller should
    restart the process instead of waiting for an export that cannot happen.

    The loop lives here rather than in a backend because it encodes a trap that is the same on
    both platforms: the dialog vanishes for BOTH answers, so a missing dialog is not proof of
    consent. Enter (or Decline) withdraws it and SystemView then exits rc=1, which is why the
    process is checked before ``answered`` is set. Measured on Windows: posted Return -> dialog
    gone 1 s later, rc=1.
    """
    import time
    report = {'dialog_seen': False, 'auto_accept': bool(auto_accept), 'answered': False,
              'waited_seconds': 0.0, 'title': None, 'rect': None, 'clicks': [],
              'main_window': None, 'method': None, 'backend': backend_name(), 'note': None}
    if not available():
        report['note'] = ('no dialog backend for this session: answer the license dialog by '
                          'hand, or run SystemView with a display attached '
                          '(Windows: user32; Linux: an X display with XTest)')
        return report
    arm_seconds = arm_seconds if arm_seconds is not None else sys.modules[__name__].arm_seconds()
    start = time.monotonic()
    dialog = None
    while time.monotonic() - start < timeout:
        dialog = find_dialog(process_id)
        if dialog:
            break
        time.sleep(poll)
    if not dialog:
        report['waited_seconds'] = round(time.monotonic() - start, 2)
        report['note'] = ('no license dialog appeared within timeout; either it was already '
                          'answered for this session or SystemView failed to start')
        return report
    report['dialog_seen'] = True
    report['title'] = dialog['title']
    report['rect'] = list(dialog['rect'])
    if not auto_accept:
        report['note'] = 'auto-accept disabled; answer the dialog in the GUI'
    armed_at = time.monotonic() + arm_seconds
    next_press = 0.0
    while time.monotonic() - start < timeout:
        current = find_dialog(process_id)
        if current is None:
            if process_id is not None and not _alive(process_id):
                report['note'] = ('the license dialog vanished but SystemView exited; the modal '
                                  'was DECLINED, not accepted (posting Return does this)')
                break
            report['answered'] = True
            break
        now = time.monotonic()
        if auto_accept and now >= armed_at and now >= next_press:
            click = click_accept(current, fraction or ACCEPT_FRACTION, process_id)
            click['method'] = 'post-click' if click.get('confirmed') else 'mouse'
            report['clicks'].append(click)
            report['method'] = click['method']
            next_press = now + retry_interval
        time.sleep(poll)
    report['waited_seconds'] = round(time.monotonic() - start, 2)
    if report['answered']:
        report['note'] = 'license dialog accepted after %d press(es)' % len(report['clicks'])
    else:
        report['note'] = ((report['note'] + '; ') if report['note'] else '') + \
            'license dialog still present after timeout'
    if main_window_timeout > 0 and report['answered']:
        window = find_main_window(process_id, timeout=main_window_timeout)
        if window is None:
            report['note'] += ('; the SystemView main window did not appear within '
                              '%.1fs, so the launch is wedged' % main_window_timeout)
        else:
            report['main_window'] = {'title': window['title'], 'rect': list(window['rect'])}
    return report


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Answer the SEGGER SystemView license dialog')
    parser.add_argument('--pid', type=int, default=None, help='only consider this process id')
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--observe-only', action='store_true',
                        help='report the dialog without clicking (answer it by hand)')
    parser.add_argument('--fraction', nargs=2, type=float, default=list(ACCEPT_FRACTION),
                        metavar=('FX', 'FY'), help='Accept button position as a fraction of the window')
    parser.add_argument('--main-window-timeout', type=float, default=8.0,
                        help='after the dialog is gone, wait this long for the main window')
    args = parser.parse_args()
    result = accept_license(args.pid, args.timeout, auto_accept=not args.observe_only,
                            fraction=tuple(args.fraction),
                            main_window_timeout=args.main_window_timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result['answered'] or not result['dialog_seen']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
