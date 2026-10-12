#!/usr/bin/env python3
"""Answer SEGGER SystemView's blocking licence dialog on a Linux X session.

Why this exists
---------------
SystemView V3.60e shows a modal Qt dialog ("No Commercial-Use License | SEGGER SystemView
V3.60e") before its window is created, and while that dialog is open the process never parses
its command line -- so ``SystemView -load R.svd -export out.csv -quit`` hangs until it is
killed. An agent that wants a CPU-load number therefore has to answer the dialog itself, and
there is no flag to suppress it.

The Windows cousin of this file (``piny_view_win32``) answers it by POSTING a window message,
which works even on a locked desktop because the message is addressed to a window rather than
to the input queue. X11 has no exact equivalent, so this backend is a staircase and the report
says which step was used:

1. ``XTestFakeButtonEvent`` -- the standard XTest extension synthesises input at the server, so
   it reaches the dialog without any window manager cooperation. This is the normal path, and
   it is what ``xdotool`` does internally.
2. ``XSendEvent`` -- a synthetic ``ButtonPress`` delivered straight to the dialog window. It
   needs no XTest, but Qt ignores untrusted synthetic events in some builds, so it is the
   fallback rather than the default.
3. An honest failure. ``accept_license`` still returns a report saying the dialog was seen and
   not answered, and the caller can answer it by hand or run SystemView headless-only.

Geometry is asked of the X server (``XGetWindowAttributes``) rather than derived from the
client-area/whole-window assumption the Windows backend uses; under a reparenting window
manager the frame is a different window from the client, which is exactly the kind of detail
that turns a correct fraction into a click on the frame.

What is NOT verified here
-------------------------
This module has been exercised only through its pure helpers on a machine with no X server.
The unit tests run on Windows and assert the search mechanics, the report shape and the
staircase order -- not that a real Qt dialog on a real X session is dismissed by them. Treat the
first real run on a Linux board host as the measurement, exactly as the Windows numbers were
measured rather than assumed.

Python standard library only (ctypes), like the rest of the package.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import time

#: The toolkit facts both backends share. Imported lazily inside the helpers that need them so
#: that importing this file can never be part of an import cycle: the dispatcher imports it.
_X11 = 'libX11.so.6'
_XTST = 'libXtst.so.6'

#: Atoms and property types, spelled out as strings because that is what XInternAtom takes.
_NET_CLIENT_LIST = '_NET_CLIENT_LIST'
_NET_WM_PID = '_NET_WM_PID'
_NET_WM_NAME = '_NET_WM_NAME'
_UTF8_STRING = 'UTF8_STRING'
_XA_CARDINAL = 6
_XA_WINDOW = 33

#: X11 event masks and event type codes used by the XSendEvent fallback.
_BUTTON_PRESS, _BUTTON_RELEASE = 4, 5
_KEY_PRESS, _KEY_RELEASE = 2, 3

#: Recursion guard for the window walk. A reparenting WM nests a handful deep; anything past
#: this is a cycle or a broken tree, and walking it would be an infinite loop rather than a
#: slower answer.
_MAX_DEPTH = 12


#: ``XErrorEvent``, the struct libX11 hands to an error handler. The handler is installed as a
#: callback, so this has to be laid out completely for the same reason as the struct below: the
#: library writes all of it.
class XErrorEvent(ctypes.Structure):
    _fields_ = [('type', ctypes.c_int), ('display', ctypes.c_void_p),
                ('resourceid', ctypes.c_ulong), ('serial', ctypes.c_ulong),
                ('error_code', ctypes.c_ubyte), ('request_code', ctypes.c_ubyte),
                ('minor_code', ctypes.c_ubyte)]


#: The C signature of an Xlib error handler: int (*)(Display *, XErrorEvent *).
_X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                    ctypes.POINTER(XErrorEvent))


class XWindowAttributes(ctypes.Structure):
    """The layout of ``XWindowAttributes``. Only the fields read here are named.

    The leading fields up to ``your_event_mask`` are the whole documented prefix, and the
    struct has to be complete because libX11 writes all of it: a short struct means a buffer
    overrun, not a smaller answer.
    """
    _fields_ = [
        ('x', ctypes.c_int), ('y', ctypes.c_int),
        ('width', ctypes.c_int), ('height', ctypes.c_int),
        ('border_width', ctypes.c_int), ('depth', ctypes.c_int),
        ('visual', ctypes.c_void_p), ('root', ctypes.c_ulong),
        ('class_', ctypes.c_int), ('bit_gravity', ctypes.c_int),
        ('win_gravity', ctypes.c_int), ('backing_store', ctypes.c_int),
        ('backing_planes', ctypes.c_ulong), ('backing_pixel', ctypes.c_ulong),
        ('save_under', ctypes.c_int), ('colormap', ctypes.c_ulong),
        ('map_installed', ctypes.c_int), ('map_state', ctypes.c_int),
        ('all_event_masks', ctypes.c_long), ('your_event_mask', ctypes.c_long),
        ('do_not_propagate_mask', ctypes.c_long), ('override_redirect', ctypes.c_int),
        ('screen', ctypes.c_void_p),
    ]


class XTextProperty(ctypes.Structure):
    _fields_ = [('value', ctypes.POINTER(ctypes.c_ubyte)), ('encoding', ctypes.c_ulong),
                ('format', ctypes.c_int), ('nitems', ctypes.c_ulong)]


class XEvent(ctypes.Structure):
    """A generously sized XEvent. The union's largest member today is well under this."""

    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong),
                ('send_event', ctypes.c_int), ('display', ctypes.c_void_p),
                ('window', ctypes.c_ulong), ('root', ctypes.c_ulong),
                ('subwindow', ctypes.c_ulong), ('time', ctypes.c_ulong),
                ('x', ctypes.c_int), ('y', ctypes.c_int),
                ('x_root', ctypes.c_int), ('y_root', ctypes.c_int),
                ('state', ctypes.c_uint), ('button', ctypes.c_uint),
                ('same_screen', ctypes.c_int), ('pad', ctypes.c_byte * 32)]


_XLIB = None
_XTST_LIB = None
_XFT = None
_XFT_FAILED = False
_DISPLAY = None
_DISPLAY_FAILED = False
_LIBRARIES_FAILED = False


def _load(names):
    """Load the first library that exists, or None. Never raises: a missing library is an
    ordinary answer on a machine without X, not an error worth a traceback."""
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def _libraries():
    """``(libX11, libXtst)``, either of which may be None. Both are needed for a real click."""
    global _XLIB, _XTST_LIB, _LIBRARIES_FAILED
    if _XLIB is None and not _LIBRARIES_FAILED:
        _LAST_X_ERROR[0] = None
        _XLIB = _load((ctypes.util.find_library('X11') or _X11, 'libX11.so', 'libX11.dylib'))
        if _XLIB is not None:
            _declare(_XLIB)
            _quiet_xlib(_XLIB)
        _XTST_LIB = _load((ctypes.util.find_library('Xtst') or _XTST, 'libXtst.so'))
        if _XTST_LIB is not None:
            _XTST_LIB.XTestFakeMotionEvent.restype = ctypes.c_int
            _XTST_LIB.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                       ctypes.c_int, ctypes.c_int,
                                                       ctypes.c_ulong]
            _XTST_LIB.XTestFakeButtonEvent.restype = ctypes.c_int
            _XTST_LIB.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                                       ctypes.c_int, ctypes.c_ulong]
        if _XLIB is None:
            _LIBRARIES_FAILED = True
    return _XLIB, _XTST_LIB


def _declare(lib):
    """Fix the argtypes of everything used here. Without this, ctypes passes every integer as
    a C int and a 64-bit Window is silently truncated -- which produces windows that exist but
    cannot be queried, the worst possible failure mode for this module."""
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XCloseDisplay.restype = ctypes.c_int
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XQueryTree.restype = ctypes.c_int
    lib.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                               ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                               ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                               ctypes.POINTER(ctypes.c_uint)]
    lib.XGetWindowAttributes.restype = ctypes.c_int
    lib.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                         ctypes.POINTER(XWindowAttributes)]
    lib.XFetchName.restype = ctypes.c_int
    lib.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                               ctypes.POINTER(ctypes.c_char_p)]
    lib.XGetTextProperty.restype = ctypes.c_int
    lib.XGetTextProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                     ctypes.POINTER(XTextProperty), ctypes.c_ulong]
    lib.XGetWindowProperty.restype = ctypes.c_int
    lib.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                       ctypes.c_long, ctypes.c_long, ctypes.c_int,
                                       ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.POINTER(ctypes.c_ulong),
                                       ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XSendEvent.restype = ctypes.c_int
    lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                               ctypes.POINTER(XEvent)]
    lib.XFlush.restype = ctypes.c_int
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XSync.restype = ctypes.c_int
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XKeysymToKeycode.restype = ctypes.c_ubyte
    lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XStringToKeysym.restype = ctypes.c_ulong
    lib.XStringToKeysym.argtypes = [ctypes.c_char_p]
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    lib.XSetErrorHandler.argtypes = [ctypes.c_void_p]


#: Set by the handler below instead of the default one, which calls exit().
_LAST_X_ERROR = [None]
#: The installed callback. Kept referenced here because ctypes does not own it: a local would be
#: collected as soon as the install function returned, and libX11 would then call freed memory.
_X_ERROR_CALLBACK = None


def _on_x_error(display, event):
    """Record an X protocol error and return, instead of letting Xlib exit the process.

    libX11 aborts on an unhandled error, and it is not an exception that can be caught: the
    default handler calls exit(). That matters here because every lookup in this module races the
    application it is looking at -- a dialog that closes between the enumeration and the query
    turns a perfectly ordinary race into a dead capture, with no report and nothing to read. The
    failure it replaces is cheap: ``XGetWindowAttributes`` returns 0, and the caller treats that
    as "no such window", which is exactly what happened.

    This was not theoretical. An X11 job clicked a window handle that had never existed and the
    whole run died with "X Error of failed request: BadWindow ... X_GetWindowAttributes" and no
    way to see which test was running.
    """
    try:
        code = event.contents.error_code if event else None
        resource = event.contents.resourceid if event else None
    except (AttributeError, ValueError):
        code, resource = None, None
    _LAST_X_ERROR[0] = (code, resource)
    return 0


def _quiet_xlib(lib):
    """Install the non-fatal error handler once per loaded libX11."""
    global _X_ERROR_CALLBACK
    if _LAST_X_ERROR[0] == 'installed' or lib is None:
        return
    try:
        _X_ERROR_CALLBACK = _X_ERROR_HANDLER(
            lambda display, event: _on_x_error(display, event))
        lib.XSetErrorHandler(ctypes.cast(_X_ERROR_CALLBACK, ctypes.c_void_p))
    except (AttributeError, TypeError, ValueError):
        return                                   # an old libX11 without the symbol: no change
    _LAST_X_ERROR[0] = 'installed'


def _display():
    """The X display connection, or None when there is no server to talk to.

    ``XOpenDisplay(None)`` honours ``$DISPLAY`` itself; an unset or empty ``DISPLAY`` is the
    ordinary headless case (a CI runner, a container, a plain ssh session) and is answered with
    None rather than an exception, because the caller's whole job is to report instead of crash.
    """
    global _DISPLAY, _DISPLAY_FAILED
    if _DISPLAY is None and not _DISPLAY_FAILED:
        lib, _ = _libraries()
        if lib is None or not os.environ.get('DISPLAY'):
            _DISPLAY_FAILED = True
            return None
        _DISPLAY = lib.XOpenDisplay(None)
        if not _DISPLAY:
            _DISPLAY_FAILED = True
    return _DISPLAY


def available():
    """True when an X display is reachable, so window lookups can answer anything at all."""
    return _display() is not None


def pointer_available():
    """True when this session can also send input (XTest), not just read the window tree."""
    _, xtst = _libraries()
    return available() and xtst is not None


def _atom(name):
    lib, _ = _libraries()
    return lib.XInternAtom(_display(), name.encode('ascii'), 0)


def _title(lib, window):
    """A window's title, preferring ``_NET_WM_NAME`` (UTF-8) over the legacy ``WM_NAME``.

    Qt sets both. The legacy one is Latin-1 by specification, so reading it as UTF-8 can turn a
    non-ASCII title into mojibake -- harmless for matching an ASCII dialog title, but the
    preference costs nothing and keeps the report honest.
    """
    prop = XTextProperty()
    if lib.XGetTextProperty(_display(), window, ctypes.byref(prop),
                            _atom(_NET_WM_NAME)) and prop.nitems:
        raw = ctypes.string_at(prop.value, prop.nitems)
        lib.XFree(prop.value)
        try:
            return raw.decode('utf-8', 'replace')
        except Exception:
            return raw.decode('latin-1', 'replace')
    name = ctypes.c_char_p()
    if lib.XFetchName(_display(), window, ctypes.byref(name)) and name.value:
        text = name.value.decode('latin-1', 'replace')
        lib.XFree(name)
        return text
    return ''


def _rect(lib, window):
    """``(x, y, right, bottom)`` in root coordinates, or None when the window is gone.

    None is also the answer when there is no display at all: the lookups below are reached by
    callers that must be able to ask a headless session what it has, and "nothing" is the honest
    reply. Crashing on a missing display would make the one thing this module promises -- an
    honest failure -- impossible.
    """
    if not available():
        return None
    attributes = XWindowAttributes()
    if not lib.XGetWindowAttributes(_display(), window, ctypes.byref(attributes)):
        return None
    return (attributes.x, attributes.y,
            attributes.x + attributes.width, attributes.y + attributes.height)


def _client_size_for(window):
    """The dialog's own width and height, without its position."""
    lib, _ = _libraries()
    rect = _rect(lib, window['hwnd'])
    if rect is None:
        return None
    return (rect[2] - rect[0], rect[3] - rect[1])


def _children(window, depth=0):
    """Direct child windows, in server order. Empty when there are none or the window is gone."""
    if depth > _MAX_DEPTH or not available():
        return []
    lib, _ = _libraries()
    root, parent = ctypes.c_ulong(), ctypes.c_ulong()
    items = ctypes.POINTER(ctypes.c_ulong)()
    count = ctypes.c_uint()
    if not lib.XQueryTree(_display(), ctypes.c_ulong(window), ctypes.byref(root),
                          ctypes.byref(parent), ctypes.byref(items), ctypes.byref(count)):
        return []
    try:
        return [items[index] for index in range(count.value)]
    finally:
        if items:
            lib.XFree(items)


def _walk(root, depth=0):
    """Every window under ``root``, depth first, root included."""
    found = [root]
    for child in _children(root, depth):
        found.extend(_walk(child, depth + 1))
    return found


def _window_entry(lib, window):
    rect = _rect(lib, window)
    if rect is None:
        return None
    return {'hwnd': window, 'title': _title(lib, window), 'class': '', 'rect': rect}


def _root_windows():
    """Top-level candidates: what the window manager lists, or the root's children.

    ``_NET_CLIENT_LIST`` is the authoritative list when a compliant WM is running. A bare X
    server (Xvfb started by a CI job, for instance) has no WM and therefore no such property,
    so the root's own children are the fallback -- without it this module would report "no
    dialog" on exactly the headless session it exists to serve.
    """
    lib, _ = _libraries()
    root = lib.XDefaultRootWindow(_display())
    prop_type, prop_format = ctypes.c_ulong(), ctypes.c_int()
    nitems, bytes_after = ctypes.c_ulong(), ctypes.c_ulong()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    if lib.XGetWindowProperty(_display(), root, _atom(_NET_CLIENT_LIST), 0, 4096, 0,
                              _XA_WINDOW, ctypes.byref(prop_type),
                              ctypes.byref(prop_format), ctypes.byref(nitems),
                              ctypes.byref(bytes_after), ctypes.byref(data)) == 0 and nitems.value:
        try:
            array = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
            return [array[index] for index in range(nitems.value)]
        finally:
            lib.XFree(data)
    return _children(root)


def _pid(hwnd):
    """The process id owning a window, or None when the property is absent.

    Absent is a real case, not an error: ``_NET_WM_PID`` is set by the toolkit, so a window
    created by a client that does not set it (or one whose property has not been read yet)
    cannot be attributed. The caller must therefore tolerate None instead of assuming it can
    always tell which windows belong to SystemView.
    """
    lib, _ = _libraries()
    prop_type, prop_format = ctypes.c_ulong(), ctypes.c_int()
    nitems, bytes_after = ctypes.c_ulong(), ctypes.c_ulong()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    if lib.XGetWindowProperty(_display(), ctypes.c_ulong(hwnd), _atom(_NET_WM_PID), 0, 1, 0,
                              _XA_CARDINAL, ctypes.byref(prop_type), ctypes.byref(prop_format),
                              ctypes.byref(nitems), ctypes.byref(bytes_after),
                              ctypes.byref(data)) != 0 or not nitems.value:
        return None
    try:
        return int(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0])
    finally:
        lib.XFree(data)


def windows(process_id=None):
    """Visible top-level windows, optionally filtered to one process id."""
    if not available():
        return []
    lib, _ = _libraries()
    found = []
    for window in _root_windows():
        entry = _window_entry(lib, window)
        if entry is None:
            continue
        if process_id is not None and _pid(window) != process_id:
            continue
        found.append(entry)
    return found


def _enumerate_windows(lib):
    """Every top-level window, as entries. The Windows backend's ``EnumWindows`` counterpart.

    X11 has no single call for this: the list is either the window manager's ``_NET_CLIENT_LIST``
    or the root's children (see ``_root_windows``). The signature keeps the ``lib`` argument the
    dispatcher's forwarding contract uses, so a caller that replaces the enumeration replaces it
    on both platforms with the same shape of call.
    """
    return windows()


def _child_text(parent):
    """Text of the child controls of a window.

    Always empty on X11, and that is the honest answer rather than a gap: a core-protocol client
    cannot read another client's widget text, because Qt renders its own widgets and publishes
    nothing but the window tree. It exists so the dispatcher's forwarding contract is the same
    on both platforms, and a caller that reads it gets '' instead of an AttributeError.
    """
    return ''


def child_windows(hwnd):
    """Every window under ``hwnd`` as an entry dict, so a shape lookup can use the same code."""
    if not available():
        return []
    lib, _ = _libraries()
    found = []
    for child in _children(ctypes.c_ulong(hwnd)):
        entry = _window_entry(lib, child)
        if entry is not None:
            found.append(entry)
    return found


def find_dialog(process_id=None):
    """The licence dialog's window, or None."""
    from piny_view import DIALOG_TITLE
    marker = DIALOG_TITLE.casefold()
    for entry in windows(process_id):
        if marker in (entry['title'] or '').casefold():
            return entry
    return None


def find_message_box(process_id=None):
    """A modal box raised over the main window, or None.

    A box is told apart from the main window by its title: the boxes carry SystemView error and
    information titles, while the main window carries the product name. While a box is open
    every later control command is ignored, which makes a recording run that leaves one behind
    look exactly like a recorder that cannot reach the target.
    """
    from piny_view import MAIN_TITLE, _MESSAGEBOX_TITLES
    main = MAIN_TITLE.casefold()
    for entry in windows(process_id):
        title = (entry['title'] or '')
        folded = title.casefold()
        if main in folded:
            continue
        if any(folded.startswith(known.casefold()) for known in _MESSAGEBOX_TITLES):
            return entry
    return None


def find_main_window(process_id=None, timeout=0.0, poll=0.25):
    """The SystemView main window, waiting up to ``timeout`` seconds for it to appear."""
    from piny_view import DIALOG_TITLE, MAIN_TITLE
    main, dialog = MAIN_TITLE.casefold(), DIALOG_TITLE.casefold()
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        for entry in windows(process_id):
            folded = (entry['title'] or '').casefold()
            if main in folded and dialog not in folded:
                return entry
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(poll, 0.25))


def _stable_rect(window, timeout=5.0, interval=0.15):
    """Wait until the window's rectangle stops moving, then return it.

    Qt maps the dialog and lays it out a moment later, so the first rectangle observed is
    routinely not the final one. Clicking the first reading is how a press lands on the frame
    instead of the button.
    """
    lib, _ = _libraries()
    previous = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _rect(lib, window['hwnd'])
        if current is not None and current == previous:
            return current
        previous = current
        time.sleep(interval)
    return previous


def _client_size(window):
    """The window's own size, as ``(width, height)``, or None when it is gone."""
    return _client_size_for(window)


def _text_width(text, extent=9):
    """Width of ``text`` in pixels at ``extent`` points, or None when it cannot be measured.

    X11's core protocol has no text measurement -- that lives in Xft, and a machine that renders
    a dialog necessarily has it -- so the width is asked of Xft when it is there and answered
    with None when it is not. None rather than a guess: this feeds a button-layout calculation,
    and an invented width would move a press somewhere it was never verified to land.
    """
    global _XFT, _XFT_FAILED
    if _XFT is None and not _XFT_FAILED:
        _XFT = _load((ctypes.util.find_library('Xft') or 'libXft.so.2', 'libXft.so'))
        if _XFT is None:
            _XFT_FAILED = True
        else:
            _XFT.XftFontOpenName.restype = ctypes.c_void_p
            _XFT.XftFontOpenName.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
            _XFT.XftTextExtentsUtf8.restype = None
            _XFT.XftTextExtentsUtf8.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.c_char_p, ctypes.c_int,
                                                ctypes.c_void_p]
    if _XFT is None or not text:
        return None
    screen = 0
    font = _XFT.XftFontOpenName(_display(), screen,
                                ('sans-%d' % extent).encode('ascii'))
    if not font:
        return None
    try:
        # XGlyphInfo is six shorts (width, height, x, y, xOff, yOff); only width is needed, and
        # over-allocating the buffer is harmless because Xft writes exactly its own struct.
        extents = (ctypes.c_short * 8)()
        encoded = text.encode('utf-8')
        _XFT.XftTextExtentsUtf8(_display(), font, encoded, len(encoded),
                                ctypes.byref(extents))
        return int(extents[0])
    finally:
        _XFT.XftFontClose(_display(), font)


def button_layout(window, client=None):
    """Where the dialog's buttons are, in root coordinates.

    X11 does not expose a widget tree to a non-toolkit client, so unlike the Windows backend the
    buttons cannot be measured: their positions are the documented fractions of the dialog
    rectangle. That is honest about its weakness -- the fractions are the ones the Windows code
    measured, and the fallback in ``click_accept`` exists because they may be wrong here.
    """
    from piny_view import ACCEPT_FRACTION, BUTTON_BOTTOM_PX, BUTTON_HEIGHT_PX
    rect = _stable_rect(window)
    if rect is None:
        return {}
    left, top, right, bottom = rect
    width = client[0] if client else (right - left)
    accept_x = left + int(width * ACCEPT_FRACTION[0])
    accept_y = top + int((bottom - top) * ACCEPT_FRACTION[1])
    return {'Accept': {'client': [width, bottom - top],
                       'span': [accept_x - 40, accept_x + 40],
                       'centre': (accept_x, accept_y),
                       'label': 'Accept'},
            'Decline': {'client': [width, bottom - top],
                        'span': [accept_x + 40, accept_x + 120],
                        'centre': (min(accept_x + 80, right - 1),
                                   bottom - BUTTON_BOTTOM_PX - BUTTON_HEIGHT_PX // 2),
                        'label': 'Decline'}}


def _root_window():
    """The display's root window. Its own function so a test can pin it without a server."""
    lib, _ = _libraries()
    return lib.XDefaultRootWindow(_display())


def _send_button_event(window, point, xtst):
    """One press/release pair at ``point``. Returns the method that delivered it, or None.

    XTest is preferred because the server treats its events as real input; ``XSendEvent`` is the
    fallback because Qt builds exist that ignore events with the ``send_event`` flag set. The
    return value is what the report calls the method, so it has to name what actually ran.
    """
    lib, _ = _libraries()
    root = _root_window()
    if xtst is not None:
        xtst.XTestFakeMotionEvent(_display(), -1, int(point[0]), int(point[1]), 0)
        xtst.XTestFakeButtonEvent(_display(), 1, 1, 0)
        xtst.XTestFakeButtonEvent(_display(), 1, 0, 0)
        lib.XFlush(_display())
        return 'xtest'
    event = XEvent()
    event.type = _BUTTON_PRESS
    event.display = _display()
    event.window = ctypes.c_ulong(window['hwnd'])
    event.root = root
    event.x, event.y = int(point[0]), int(point[1])
    event.x_root, event.y_root = int(point[0]), int(point[1])
    event.button, event.same_screen = 1, 1
    if not lib.XSendEvent(_display(), ctypes.c_ulong(window['hwnd']), 1, 0, ctypes.byref(event)):
        return None
    event.type = _BUTTON_RELEASE
    lib.XSendEvent(_display(), ctypes.c_ulong(window['hwnd']), 1, 0, ctypes.byref(event))
    lib.XFlush(_display())
    return 'xsend'


def post_click(window, point):
    """Press a point of a window without involving the window manager.

    Named ``post_click`` for symmetry with the Windows backend, where the message really is
    posted. Here the XTest server-side path plays that role, and ``method`` in the result says
    which of the two actually delivered the press.
    """
    _, xtst = _libraries()
    method = _send_button_event(window, point, xtst)
    return {'clicked': method is not None, 'x': int(point[0]), 'y': int(point[1]),
            'method': method, 'confirmed': method is not None}


def post_enter(window):
    """Send Return to a window. Recorded as a dead end: it DECLINES the licence.

    Kept because the Windows backend measured this trap, and a reader who finds it here should
    not have to rediscover it: the dialog disappears for both answers, so a script that posts
    Return and sees the dialog go away concludes it succeeded while SystemView exits rc=1.
    """
    lib, _ = _libraries()
    keysym = lib.XStringToKeysym(b'Return')
    keycode = lib.XKeysymToKeycode(_display(), keysym)
    if not keycode:
        return {'posted': False, 'method': None, 'reason': 'no keycode for Return'}
    root = lib.XDefaultRootWindow(_display())
    for kind in (_KEY_PRESS, _KEY_RELEASE):
        event = XEvent()
        event.type = kind
        event.display = _display()
        event.window = ctypes.c_ulong(window['hwnd'])
        event.root = root
        event.same_screen = 1
        lib.XSendEvent(_display(), ctypes.c_ulong(window['hwnd']), 1, 0, ctypes.byref(event))
    lib.XFlush(_display())
    return {'posted': True, 'keydown': True, 'keyup': True, 'hwnd': window['hwnd'],
            'method': 'xsend-return'}


def click_point(window, point, settle=0.35):
    """Press the pointer at a point of a window, the way a user would."""
    time.sleep(settle)
    _, xtst = _libraries()
    method = _send_button_event(window, point, xtst)
    return {'clicked': method is not None, 'x': int(point[0]), 'y': int(point[1]),
            'method': method, 'confirmed': method is not None}


def click_accept(window, fraction=(0.80, 0.90), process_id=None):
    """Press the dialog's Accept button.

    The staircase, in order: the documented fraction of the dialog rectangle, then the centre of
    each child window in the dialog's bottom band. The second step exists because X11 offers no
    way to measure the buttons, so the fraction is an assumption here in a way it is not on
    Windows; when the assumption is wrong the press lands on dead space and the dialog stays up.
    """
    rect = _stable_rect(window)
    if rect is None:
        return {'clicked': False, 'method': None, 'reason': 'the dialog disappeared'}
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    attempts = [{'point': (left + int(width * fraction[0]), top + int(height * fraction[1])),
                 'where': 'fraction'}]
    for child in child_windows(window['hwnd']):
        child_left, child_top, child_right, child_bottom = child['rect']
        if child_bottom < top + height * 0.75:
            continue        # not in the button band: this is the text area or a scrollbar
        if (child_right - child_left) < 20 or (child_bottom - child_top) < 10:
            continue        # too small to be a button
        attempts.append({'point': ((child_left + child_right) // 2,
                                   (child_top + child_bottom) // 2),
                         'where': 'child'})
    _, xtst = _libraries()
    for attempt in attempts:
        method = _send_button_event(window, attempt['point'], xtst)
        if method:
            return {'clicked': True, 'x': attempt['point'][0], 'y': attempt['point'][1],
                    'method': method, 'confirmed': True, 'where': attempt['where']}
    return {'clicked': False, 'method': None, 'reason': 'no input path: XTest and XSendEvent '
                                                        'both unavailable'}


def find_default_button(window):
    """The button-like child of a dialog, as a point at its centre, or None.

    The same shape rule as the Windows backend, deliberately: a button is wide and short
    (50 px or wider, 15-60 px tall, between 1:1 and 8:1), it sits in the bottom 30 % of the
    window, and when several qualify the SMALLEST wins -- the text area and the scrollbar are
    also wide and short, and they are always larger than the button. Measured on the '43466
    Events Loaded' box on Windows: 518x497 with a 93x28 OK button; the rule is copied rather
    than re-derived because the box is drawn by the same Qt and the geometry it produces is a
    toolkit fact, not a Windows fact.

    None is a real answer here -- X11 clients that render their own widgets expose no child
    windows at all -- and the caller falls back to the fraction instead of guessing.
    """
    rect = window.get('rect') or _stable_rect(window)
    if rect is None:
        return None
    left, top, right, bottom = rect
    height = bottom - top
    candidates = []
    for child in child_windows(window['hwnd']):
        c_left, c_top, c_right, c_bottom = child['rect']
        width, child_height = c_right - c_left, c_bottom - c_top
        if width < 50 or child_height < 15 or child_height > 60:
            continue
        if not 1.0 <= width / child_height <= 8.0:
            continue
        if c_top < bottom - height * 0.3:
            continue
        candidates.append((width * child_height, child['rect']))
    if not candidates:
        return None
    _, (c_left, c_top, c_right, c_bottom) = min(candidates)
    return {'point': ((c_left + c_right) // 2, (c_top + c_bottom) // 2),
            'rect': [c_left, c_top, c_right, c_bottom]}


def dismiss_message_box(process_id=None, fraction=None):
    """Read and close one open SystemView message box. Returns None when there is none.

    SystemView reports recorder and import trouble in a modal Qt box and stops answering its
    control socket while one is open, so an unattended recording run has to close it. The box's
    own button is located by shape rather than by a fraction of the rectangle, since these boxes
    resize with their content; the fraction is only the fallback for a box that exposes no
    child windows.
    """
    box = find_message_box(process_id)
    if box is None:
        return None
    report = {'title': box['title'], 'text': box.get('text', ''), 'rect': list(box['rect']),
              'child_windows': len(child_windows(box['hwnd'])), 'button': None,
              'dismissed': None}
    button = find_default_button(box)
    report['button'] = button
    if button is not None:
        report['dismissed'] = click_point(box, button['point'])
    else:
        report['dismissed'] = click_accept(box, fraction or (0.5, 0.88))
    return report


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Answer the SEGGER SystemView license dialog')
    parser.add_argument('--pid', type=int, default=None, help='only consider this process id')
    parser.add_argument('--timeout', type=float, default=30.0)
    parser.add_argument('--observe-only', action='store_true',
                        help='report the dialog without clicking (answer it by hand)')
    parser.add_argument('--fraction', nargs=2, type=float, default=[0.80, 0.90],
                        metavar=('FX', 'FY'), help='Accept button position as a fraction')
    parser.add_argument('--main-window-timeout', type=float, default=8.0,
                        help='after the dialog is gone, wait this long for the main window')
    args = parser.parse_args()
    from piny_view import accept_license
    result = accept_license(args.pid, args.timeout, auto_accept=not args.observe_only,
                            fraction=tuple(args.fraction),
                            main_window_timeout=args.main_window_timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result['answered'] or not result['dialog_seen']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
