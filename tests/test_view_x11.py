"""Tests for the X11 licence-dialog backend.

These run on Windows as well as on Linux, by design: the point of the module is that its REPORT
shape and its fallback order are right, and both of those can be pinned without an X server.
What cannot be pinned here is that a real Qt dialog on a real X session is dismissed -- that is
a measurement, and it has not been made yet (see the module docstring).

``available()`` is therefore never asserted to be True or False. The tests that need X state the
assumption they actually need and skip when it does not hold, so the suite is honest on a
machine with no display instead of quietly passing.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import piny_view_x11 as x  # noqa: E402


DIALOG_RECT = (401, 237, 1135, 627)          # the measured V3.60e geometry, same as Windows


class BackendContractTests(unittest.TestCase):
    """The dispatcher needs these names, with these shapes. Nothing here touches a display."""

    def test_every_forwarded_name_exists(self):
        import piny_view as v
        for name in v._FORWARDED:
            self.assertTrue(hasattr(x, name),
                            'the dispatcher forwards %r, so the X11 backend must define it'
                            % name)

    def test_the_public_api_is_the_same_as_the_windows_backend(self):
        # A caller should not have to know which backend ran, so the two must offer the same
        # functions. Compared as sets rather than by listing them twice: a name added to one
        # backend and not the other is exactly the drift this catches.
        import piny_view_win32 as w
        public = lambda module: {name for name in dir(module) if not name.startswith('_')}
        windows_only = {'windows', 'child_windows', 'find_dialog', 'find_main_window',
                        'find_message_box', 'button_layout', 'post_click', 'post_enter',
                        'click_point', 'click_accept', 'find_default_button',
                        'dismiss_message_box', 'stable_rect', 'text_width'}
        expected = (public(w) & windows_only) - {'stable_rect', 'text_width'}
        missing = expected - public(x)
        self.assertFalse(missing, 'the X11 backend is missing: %s' % sorted(missing))

    def test_available_answers_false_instead_of_raising_without_a_display(self):
        # Not "is False": a developer machine running this suite under an X session has a
        # display. What must hold either way is that asking the question never raises.
        self.assertIn(x.available(), (True, False))

    def test_a_missing_display_is_not_an_error(self):
        # `_DISPLAY_FAILED` is what makes the module's own `_display()` answer None, so it is set
        # directly rather than by emptying `$DISPLAY` and hoping libX11 refuses: on the CI runner
        # that hope did not hold, the assertion on `available()` was never reached, and the failure
        # surfaced several calls later as `AssertionError: None != []` from `windows()` -- a
        # message with nothing in it about the display.
        saved_rect, saved_width = x._stable_rect, x._text_width
        x._DISPLAY, x._DISPLAY_FAILED = None, True
        x._stable_rect = lambda window, timeout=0.0: None
        x._text_width = lambda text, extent=9: None
        try:
            self.assertFalse(x.available(), 'a failed display must not read as available')
            self.assertEqual(x.windows(), [])
            self.assertEqual(x.child_windows(1), [])
            self.assertIsNone(x.find_dialog())
            self.assertIsNone(x.find_message_box())
            self.assertIsNone(x.find_main_window(timeout=0.0))
            self.assertEqual(x.button_layout({'hwnd': 1}), {})
        finally:
            x._stable_rect, x._text_width = saved_rect, saved_width
            x._DISPLAY, x._DISPLAY_FAILED = None, False
            x.available()                                 # re-ask with the real environment


class ButtonLayoutTests(unittest.TestCase):
    """The fraction is the documented Windows measurement; here it is an assumption, but the
    arithmetic around it -- which corner of the dialog the press lands in -- is checkable."""

    def layout(self, returncode=DIALOG_RECT):
        with mock.patch.object(x, '_stable_rect', return_value=returncode):
            return x.button_layout({'hwnd': 1})

    def test_accept_is_computed_from_the_dialog_rectangle(self):
        layout = self.layout()
        left, top, right, bottom = DIALOG_RECT
        width, height = right - left, bottom - top
        self.assertEqual(layout['Accept']['centre'],
                         (left + int(width * 0.80), top + int(height * 0.90)))

    def test_decline_is_to_the_right_of_accept(self):
        layout = self.layout()
        self.assertGreater(layout['Decline']['centre'][0], layout['Accept']['centre'][0])

    def test_decline_never_leaves_the_dialog(self):
        # A narrow dialog would otherwise put the fallback press outside the window, where it
        # lands on whatever is behind -- the failure mode the Windows backend also guards.
        layout = self.layout((100, 100, 300, 300))
        self.assertLess(layout['Decline']['centre'][0], 300)

    def test_a_vanished_dialog_reports_no_layout(self):
        self.assertEqual(self.layout(None), {})


class EditSafetyTests(unittest.TestCase):
    """A malformed staircase would be worse than none: the report claims a method, so the
    method names have to be the ones the code can actually produce."""

    def test_send_event_is_the_fallback_when_xtest_is_missing(self):
        with mock.patch.object(x, '_libraries', return_value=(mock.Mock(), None)), \
             mock.patch.object(x, '_display', return_value=1), \
             mock.patch.object(x, '_root_window', return_value=1):
            lib = x._libraries()[0]
            lib.XSendEvent.return_value = 1
            method = x._send_button_event({'hwnd': 1}, (10, 20), None)
        self.assertEqual(method, 'xsend')

    def test_no_input_path_reports_none_rather_than_claiming_a_click(self):
        with mock.patch.object(x, '_libraries', return_value=(mock.Mock(), None)), \
             mock.patch.object(x, '_display', return_value=1), \
             mock.patch.object(x, '_root_window', return_value=1):
            x._libraries()[0].XSendEvent.return_value = 0
            self.assertIsNone(x._send_button_event({'hwnd': 1}, (10, 20), None))

    def test_post_click_reports_the_method_that_ran(self):
        with mock.patch.object(x, '_libraries', return_value=(mock.Mock(), None)), \
             mock.patch.object(x, '_display', return_value=1), \
             mock.patch.object(x, '_root_window', return_value=1):
            x._libraries()[0].XSendEvent.return_value = 1
            report = x.post_click({'hwnd': 1}, (10, 20))
        self.assertTrue(report['clicked'])
        self.assertEqual(report['method'], 'xsend')
        self.assertEqual((report['x'], report['y']), (10, 20))

    def test_the_staircase_tries_the_fraction_before_the_children(self):
        window = {'hwnd': 1, 'rect': DIALOG_RECT}
        tried = []

        def fake_send(window, point, xtst):
            tried.append(point)
            return 'xtest'

        child = {'hwnd': 2, 'class': '', 'text': '', 'rect': (500, 600, 600, 620)}
        with mock.patch.object(x, '_stable_rect', return_value=DIALOG_RECT), \
             mock.patch.object(x, 'child_windows', return_value=[child]), \
             mock.patch.object(x, '_libraries', return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(x, '_send_button_event', side_effect=fake_send):
            report = x.click_accept(window)
        self.assertTrue(report['clicked'])
        self.assertEqual(report['where'], 'fraction')
        self.assertEqual(len(tried), 1, 'the first staircase step succeeded, so it must stop')

    def test_a_press_that_no_step_can_deliver_is_not_reported_as_a_click(self):
        window = {'hwnd': 1, 'rect': DIALOG_RECT}
        with mock.patch.object(x, '_stable_rect', return_value=DIALOG_RECT), \
             mock.patch.object(x, 'child_windows', return_value=[]), \
             mock.patch.object(x, '_libraries', return_value=(mock.Mock(), mock.Mock())), \
             mock.patch.object(x, '_send_button_event', return_value=None):
            report = x.click_accept(window)
        self.assertFalse(report['clicked'])
        self.assertIn('XTest', report['reason'])


class DefaultButtonTests(unittest.TestCase):
    """The shape rule is shared with the Windows backend, so it is pinned identically."""

    def test_a_child_in_the_bottom_band_is_the_button(self):
        window = {'hwnd': 1, 'rect': (696, 289, 1214, 786)}
        children = [
            {'hwnd': 2, 'class': '', 'text': '', 'rect': (709, 366, 1201, 725)},   # text area
            {'hwnd': 4, 'class': '', 'text': '', 'rect': (1108, 745, 1201, 773)},  # OK button
        ]
        with mock.patch.object(x, 'child_windows', return_value=children):
            button = x.find_default_button(window)
        self.assertIsNotNone(button)
        self.assertEqual(button['rect'], [1108, 745, 1201, 773])
        self.assertEqual(button['point'], (1154, 759))

    def test_no_child_windows_means_no_guess(self):
        window = {'hwnd': 1, 'rect': (100, 100, 400, 300)}
        with mock.patch.object(x, 'child_windows', return_value=[]):
            self.assertIsNone(x.find_default_button(window))

    def test_a_tiny_child_is_not_a_button(self):
        window = {'hwnd': 1, 'rect': (100, 100, 500, 500)}
        children = [{'hwnd': 2, 'class': '', 'text': '', 'rect': (200, 480, 205, 483)}]
        with mock.patch.object(x, 'child_windows', return_value=children):
            self.assertIsNone(x.find_default_button(window))


class TitleMatchingTests(unittest.TestCase):
    """Both backends match the same titles, in the same case-insensitive way. A mismatch here
    would mean the Linux backend looks for a dialog Windows already knows the name of."""

    def test_the_dialog_marker_matches_the_real_title(self):
        import piny_view as v
        real = 'No Commercial-Use License | SEGGER SystemView V3.60e'
        self.assertIn(v.DIALOG_TITLE.casefold(), real.casefold())

    def test_a_message_box_is_not_the_main_window(self):
        import piny_view as v
        main = {'hwnd': 1, 'title': 'SEGGER SystemView V3.60e', 'class': '', 'rect': (0, 0, 1, 1)}
        box = {'hwnd': 2, 'title': 'SystemView Error', 'class': '', 'rect': (0, 0, 1, 1)}
        with mock.patch.object(x, 'windows', return_value=[main, box]):
            self.assertEqual(x.find_message_box()['hwnd'], 2)
        with mock.patch.object(x, 'windows', return_value=[main]):
            self.assertIsNone(x.find_message_box())

    def test_the_main_window_lookup_ignores_the_dialog(self):
        import piny_view as v
        dialog = {'hwnd': 1, 'title': 'No Commercial-Use License | ' + v.MAIN_TITLE,
                  'class': '', 'rect': (0, 0, 1, 1)}
        main = {'hwnd': 2, 'title': v.MAIN_TITLE + ' V3.60e', 'class': '', 'rect': (0, 0, 1, 1)}
        with mock.patch.object(x, 'windows', return_value=[dialog, main]):
            self.assertEqual(x.find_main_window()['hwnd'], 2)


class XErrorHandlerTests(unittest.TestCase):
    """libX11 exits the process on an unhandled protocol error, so the handler is a safety
    property of this module rather than a detail: a window that disappears between the
    enumeration and the query must produce an empty answer, not a dead capture.

    The marker is reset between tests rather than patched, and the difference is not cosmetic:
    patching the LIST hands the module a second list while an already-installed handler keeps
    writing into the first one, so the assertion would read a list nothing ever updates.
    """

    def setUp(self):
        self._saved = (x._LAST_X_ERROR[0], x._X_ERROR_CALLBACK)

    def tearDown(self):
        x._LAST_X_ERROR[0], x._X_ERROR_CALLBACK = self._saved

    def test_the_error_handler_returns_instead_of_exiting(self):
        event = mock.Mock()
        event.contents.error_code = 3
        event.contents.resourceid = 0x1
        x._LAST_X_ERROR[0] = None
        self.assertEqual(x._on_x_error(None, event), 0)
        self.assertEqual(x._LAST_X_ERROR[0], (3, 0x1))

    def test_a_handler_that_cannot_read_the_event_still_returns(self):
        # The struct is filled by the library; a handler that raised here would replace the
        # process exit it exists to prevent with a different one.
        self.assertEqual(x._on_x_error(None, None), 0)
        self.assertEqual(x._on_x_error(None, mock.Mock(contents=None)), 0)

    def test_the_install_is_attempted_once_and_keeps_its_callback_alive(self):
        # XSetErrorHandler returns the PREVIOUS handler as a function pointer, so the stub hands
        # back an address rather than a Mock: ctypes has to be able to cast whatever comes back.
        lib = mock.Mock()
        lib.XSetErrorHandler.return_value = 0
        x._LAST_X_ERROR[0], x._X_ERROR_CALLBACK = None, None
        x._quiet_xlib(lib)
        first = x._X_ERROR_CALLBACK
        x._quiet_xlib(lib)                           # a second load must not re-install
        self.assertIsNotNone(first, 'a callback ctypes does not own must stay referenced')
        lib.XSetErrorHandler.assert_called_once()
        self.assertEqual(x._LAST_X_ERROR[0], 'installed')

    def test_an_old_libx11_without_the_symbol_is_not_an_error(self):
        lib = mock.Mock()
        del lib.XSetErrorHandler                       # the symbol is absent
        x._LAST_X_ERROR[0], x._X_ERROR_CALLBACK = None, None
        x._quiet_xlib(lib)                             # must not raise
        self.assertNotEqual(x._LAST_X_ERROR[0], 'installed')


if __name__ == '__main__':
    unittest.main()
