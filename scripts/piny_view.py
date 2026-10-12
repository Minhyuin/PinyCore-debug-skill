#!/usr/bin/env python3
"""Dismiss the SEGGER SystemView non-commercial license dialog.

Why this module exists
----------------------
SystemView V3.60e shows a modal Qt dialog ("No Commercial-Use License | SEGGER
SystemView V3.60e") before its window is created. While that dialog is open the
process never parses its command line, so ``SystemView.exe -load R.svd -export
out.csv -quit`` simply hangs until it is killed. The dialog therefore has to be
answered by whoever drives the CLI.

The dialog exposes no command-line flag to suppress it. This module finds the
dialog by window title, waits until its geometry is stable, and presses the
"Accept" button. Everything it does is recorded so an artifact can show exactly
which window was clicked and where.

Coordinate-free fallback
------------------------
``--no-auto-accept-license`` lets the caller answer the dialog by hand; the
routine then only observes and reports, never clicks.

Windows only. Python standard library only (ctypes).
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes as wt
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

_LEFTDOWN, _LEFTUP = 0x0002, 0x0004
_WM_KEYDOWN, _WM_KEYUP = 0x0100, 0x0101
_WM_LBUTTONDOWN, _WM_LBUTTONUP = 0x0201, 0x0202
_MK_LBUTTON = 0x0001
_VK_RETURN = 0x0D
#: License-dialog button layout, measured on V3.60e (see button_layout).
BUTTON_PAD_PX = 14
BUTTON_GAP_PX = 10
BUTTON_EDGE_PX = 12
BUTTON_BOTTOM_PX = 12
BUTTON_HEIGHT_PX = 26
BUTTON_FONT_PX = 9
BUTTON_FONT = 'Segoe UI'


def available():
    """True when the Windows window/dialog bridge can be used at all."""
    return hasattr(ctypes, 'windll') and hasattr(ctypes.windll, 'user32')


def _user32():
    if not available():
        raise RuntimeError('License dialog handling requires Windows (user32 unavailable)')
    return ctypes.windll.user32


def _enumerate_windows(user32):
    """Visible top-level windows as dicts: hwnd, title, pid, class, rect."""
    found = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                rect = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                found.append({'hwnd': hwnd, 'title': buffer.value, 'class': cls.value,
                              'rect': (rect.left, rect.top, rect.right, rect.bottom)})
        return True

    user32.EnumWindows(proto(visit), 0)
    return found


def windows(process_id=None):
    """Visible top-level windows, optionally filtered to one process id."""
    return [w for w in _enumerate_windows(_user32())
            if process_id is None or w['hwnd'] and _pid(w['hwnd']) == process_id]


#: Titles of the modal boxes SystemView raises over its main window. They are ordinary Qt
#: windows (class QWidget), not Win32 #32770 message boxes, so they are matched by title.
#: While one is open every later control command is ignored, which makes a recording run
#: that leaves one behind look exactly like a recorder that cannot reach the target.
MESSAGEBOX_TITLES = ('System Information', 'SystemView Error', 'SEGGER SystemView Error')


def _child_text(user32, parent):
    """Text of every child control of a window, in creation order.

    Qt renders its own widgets and exposes no message text here, so this is usually empty;
    it is kept because a Win32 message box, if one ever appears, states the reason in these
    static controls.
    """
    texts = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def visit(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value and not buffer.value.startswith('qt_'):
                texts.append(buffer.value)
        return True

    user32.EnumChildWindows(parent, proto(visit), 0)
    return texts


def find_message_box(process_id=None):
    """The open SystemView modal box, if any, with its text and child widget count."""
    for window in _enumerate_windows(_user32()):
        title = window['title']
        if not any(title.startswith(known) for known in MESSAGEBOX_TITLES):
            continue
        if process_id is not None and _pid(window['hwnd']) != process_id:
            continue
        return {'hwnd': window['hwnd'], 'title': title, 'rect': window['rect'],
                'text': _child_text(_user32(), window['hwnd'])}
    return None


def _pid(hwnd):
    owner = wt.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return owner.value


def _alive(process_id):
    """True while a process id still exists, without holding a handle to it."""
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(process_id))
    if not handle:
        return False
    try:
        code = wt.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def find_dialog(process_id=None):
    """The license dialog window, or None."""
    for window in windows():
        if DIALOG_TITLE.casefold() in window['title'].casefold():
            if process_id is None or _pid(window['hwnd']) == process_id:
                return window
    return None


def find_main_window(process_id=None, timeout=0.0, poll=0.25):
    """The SystemView main window, or None.

    The main window only exists after the license dialog has been answered, so it is the
    signal that the application really moved on rather than merely hiding its modal.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for window in windows():
            if DIALOG_TITLE.casefold() in window['title'].casefold():
                continue
            if MAIN_TITLE.casefold() in window['title'].casefold():
                if process_id is None or _pid(window['hwnd']) == process_id:
                    return window
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def _stable_rect(window, timeout=5.0, interval=0.15):
    """Wait until a window's rect stops changing so the click lands where it was measured."""
    user32 = _user32()
    previous = window['rect']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        rect = wt.RECT()
        user32.GetWindowRect(window['hwnd'], ctypes.byref(rect))
        current = (rect.left, rect.top, rect.right, rect.bottom)
        if current == previous and (rect.right - rect.left) > 100:
            return current
        previous = current
    return previous


def _text_width(text, extent=BUTTON_FONT_PX):
    """Rendered width of a label in the dialog's font, in pixels."""
    user32, gdi32 = _user32(), ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    gdi32.CreateFontW.restype = ctypes.c_void_p
    font = gdi32.CreateFontW(-extent, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 0, 0, BUTTON_FONT)
    previous = gdi32.SelectObject(hdc, ctypes.c_void_p(font))
    size = wt.SIZE()
    gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(size))
    gdi32.SelectObject(hdc, ctypes.c_void_p(previous))
    gdi32.DeleteObject(ctypes.c_void_p(font))
    user32.ReleaseDC(0, hdc)
    return size.cx


def _client_size(window):
    """Client width and height of a window, in the caller's DPI-virtualised coordinates."""
    client = wt.RECT()
    _user32().GetClientRect(window['hwnd'], ctypes.byref(client))
    return client.right, client.bottom


def button_layout(window, client=None):
    """Where the license dialog's three buttons are, in CLIENT coordinates.

    The buttons cannot be enumerated: the dialog reports no child windows and Qt exposes no
    accessible children, so they only exist as painted pixels. SEGGER lays them out right to
    left as Decline, Accept, License Manager, each as wide as its label in 9 pt Segoe UI plus
    14 px of padding, 10 px apart, 12 px from the right and bottom client edges and 26 px tall.
    Verified twice on V3.60e by pressing each computed centre:
      - exp53 in a DPI-aware process, client 900x441: (859,418) declined and exited rc=1,
        (793,418) did nothing, (708,418) opened the main window 'SEGGER SystemView V3.60e';
      - exp54 in a DPI-unaware process, client 720x352: (613,327) accepted and the command
        server came up 2 s later, while (679,327) and (528,327) are Decline and License Manager.
    The client size differs between the two only because Windows virtualises coordinates by the
    caller's DPI awareness; the layout is computed inside the client rect, so both agree.
    """
    width, height = client or _client_size(window)
    if width < 100 or height < 60:
        raise RuntimeError(f'License dialog client area looks wrong: {width}x{height}')
    result = {}
    x = width - BUTTON_EDGE_PX
    for name in ('Decline', 'Accept', 'License Manager'):
        span = _text_width(name) + 2 * BUTTON_PAD_PX
        x -= span
        result[name] = {'centre': (x + span // 2, height - BUTTON_BOTTOM_PX - BUTTON_HEIGHT_PX // 2),
                        'span': [x, x + span], 'client': [width, height]}
        x -= BUTTON_GAP_PX
    return result


def post_click(window, point):
    """Press a point of the dialog with a POSTED mouse message.

    This is what works while the console is locked: the lock screen owns the screen, so
    SetCursorPos and mouse_event are delivered to a desktop nobody can see and the dialog never
    receives them (measured: explorer's 'Backstop Window' is the window at every point of the
    dialog, and a full-screen capture returns the lock screen). A posted message does not need
    the input desktop, and Qt DOES hit-test it: a press in the dialog body or title bar does
    nothing, while a press on a button acts.
    """
    user32 = _user32()
    hwnd = window['hwnd']
    x, y = int(point[0]), int(point[1])
    lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    down = bool(user32.PostMessageW(hwnd, _WM_LBUTTONDOWN, _MK_LBUTTON, lparam))
    time.sleep(0.12)
    up = bool(user32.PostMessageW(hwnd, _WM_LBUTTONUP, 0, lparam))
    return {'posted': down and up, 'down': down, 'up': up, 'client_point': [x, y],
            'hwnd': hwnd, 'dialog_rect': list(window['rect']), 'method': 'post-click'}


def post_enter(window):
    """DEPRECATED -- posting Return to the license dialog DECLINES it and exits SystemView.

    Kept as a recorded trap rather than a tool: the dialog vanishes, which looks like success,
    but the process then exits with rc=1 (measured twice, exp42/43). Enter on this dialog is the
    negative path; never use it to answer the modal. Use ``post_click`` on the Accept button.
    """
    user32 = _user32()
    hwnd = window['hwnd']
    down = bool(user32.PostMessageW(hwnd, _WM_KEYDOWN, _VK_RETURN, 0))
    time.sleep(CLICK_SETTLE_S)
    up = bool(user32.PostMessageW(hwnd, _WM_KEYUP, _VK_RETURN, 0))
    return {'posted': down and up, 'keydown': down, 'keyup': up, 'hwnd': hwnd,
            'dialog_rect': list(window['rect']), 'method': 'post-return-declines'}


def click_accept(window, fraction=ACCEPT_FRACTION, process_id=None):
    """Press the Accept button of the license dialog.

    Attempts, in order: a posted press on Accept's computed client position (works on a locked
    desktop), then the synthetic mouse sequence a human click produces -- foreground the dialog,
    move the cursor over the button, then send a left-button down/up pair. The fallback keeps the
    historical fixed-fraction point (measured on V3.60e: (988,588) for rect (401,237)-(1135,627)).
    """
    user32 = _user32()
    left, top, right, bottom = _stable_rect(window)
    if right - left < 100 or bottom - top < 100:
        raise RuntimeError(f'License dialog geometry looks wrong: {(left, top, right, bottom)}')
    buttons = button_layout(window)
    posted = post_click(window, buttons['Accept']['centre'])
    posted['button'] = buttons
    time.sleep(CLICK_SETTLE_S)
    if find_dialog(process_id) is None:
        posted['confirmed'] = True
        return posted
    # Fallback for an unlocked console: the fraction is applied to the CLIENT area, whose live
    # size is authoritative here (window rects may be DPI-virtualised for this process).
    client_width, client_height = _client_size(window)
    x = int(right - client_width + client_width * fraction[0])
    y = int(bottom - client_height + client_height * fraction[1])
    user32.SetForegroundWindow(window['hwnd'])
    time.sleep(CLICK_SETTLE_S)
    user32.SetCursorPos(x, y)
    time.sleep(CLICK_SETTLE_S)
    user32.mouse_event(_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(_LEFTUP, 0, 0, 0, 0)
    return {'clicked': True, 'x': x, 'y': y, 'dialog_rect': [left, top, right, bottom],
            'fraction': list(fraction), 'method': 'mouse',
            'posted_first': posted, 'button': buttons}


def child_windows(hwnd):
    """Every child window of hwnd as dicts with class, text and rectangle."""
    user32 = _user32()
    found = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def visit(child, _):
        rect = wt.RECT()
        user32.GetWindowRect(child, ctypes.byref(rect))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, cls, 256)
        length = user32.GetWindowTextLengthW(child)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(child, buffer, length + 1)
        found.append({'hwnd': child, 'class': cls.value, 'text': buffer.value,
                      'rect': (rect.left, rect.top, rect.right, rect.bottom)})
        return True

    user32.EnumChildWindows(hwnd, proto(visit), 0)
    return found


def find_default_button(window):
    """The button-like child of a dialog, as a point at its centre.

    SystemView's Qt boxes carry no button text and their size changes with the content,
    so a fixed offset from the window rectangle cannot be trusted: the '43466 Events
    Loaded' box measured 518x497 and its OK button was the small wide widget in the
    bottom band. Picking that widget by shape finds the button on any box layout, and a
    caller that gets None falls back to a fraction of the rectangle.
    """
    left, top, right, bottom = window['rect']
    height = bottom - top
    candidates = []
    for child in child_windows(window['hwnd']):
        c_left, c_top, c_right, c_bottom = child['rect']
        width, child_height = c_right - c_left, c_bottom - c_top
        if width < 50 or child_height < 15 or child_height > 60:
            continue
        if not 1.0 <= width / child_height <= 8.0:
            continue
        if c_top < bottom - height * 0.3:      # buttons sit in the bottom band
            continue
        candidates.append((width * child_height, child['rect']))
    if not candidates:
        return None
    _, (c_left, c_top, c_right, c_bottom) = min(candidates)
    return {'point': ((c_left + c_right) // 2, (c_top + c_bottom) // 2),
            'rect': [c_left, c_top, c_right, c_bottom]}


def dismiss_message_box(process_id=None, fraction=None):
    """Read and close one open SystemView message box. Returns None when there is none.

    SystemView reports recorder and import trouble in a modal Qt box (observed titles
    'System Information | SEGGER SystemView V3.60e' and 'SystemView Error'). It does not
    answer the control socket while such a box is open, so an unattended recording run has
    to close it; the box's own button is located by shape rather than by a fraction of the
    rectangle, since these boxes resize with their content.
    """
    box = find_message_box(process_id)
    if box is None:
        return None
    report = {'title': box['title'], 'text': box['text'], 'rect': list(box['rect']),
              'child_windows': len(child_windows(box['hwnd'])), 'button': None, 'dismissed': None}
    button = find_default_button(box)
    report['button'] = button
    try:
        if button is not None:
            report['dismissed'] = click_point(box, button['point'])
        else:
            report['dismissed'] = click_accept(box, fraction or DEFAULT_BUTTON_FRACTION)
    except RuntimeError as exc:
        report['dismissed'] = {'clicked': False, 'error': str(exc)}
    return report


def click_point(window, point, settle=CLICK_SETTLE_S):
    """Press the left mouse button at an absolute screen point, over this window."""
    user32 = _user32()
    x, y = int(point[0]), int(point[1])
    user32.SetForegroundWindow(window['hwnd'])
    time.sleep(settle)
    user32.SetCursorPos(x, y)
    time.sleep(settle)
    user32.mouse_event(_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(_LEFTUP, 0, 0, 0, 0)
    return {'clicked': True, 'x': x, 'y': y}


def accept_license(process_id=None, timeout=30.0, auto_accept=True, poll=0.25, fraction=None,
                   arm_seconds=DIALOG_ARM_S, retry_interval=RETRY_INTERVAL_S,
                   main_window_timeout=0.0):
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
    """
    report = {'dialog_seen': False, 'auto_accept': bool(auto_accept), 'answered': False,
              'waited_seconds': 0.0, 'title': None, 'rect': None, 'clicks': [],
              'main_window': None, 'method': None, 'note': None}
    if not available():
        report['note'] = 'not Windows: license dialog must be answered manually'
        return report
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
            # The dialog vanishes for BOTH answers. Enter (or the Decline button) withdraws it
            # and then SystemView exits rc=1, so a missing dialog alone is NOT proof of consent:
            # check the process. Measured: posted Return -> dialog gone 1 s later, rc=1.
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
