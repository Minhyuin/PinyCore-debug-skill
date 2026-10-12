"""Tests for the SystemView license-dialog driver (scripts/piny_view.py).

The dialog cannot be tested by launching the real application, so these tests drive the
module's window lookups directly: they check the decision logic -- exactly one press when
the dialog goes away, repeated presses while it stays, and a report that tells an accepted
dialog whose main window never appeared (wedged launch) apart from a healthy one.
"""
import contextlib, sys, unittest
from unittest import mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import piny_view as v
import piny_view_win32 as win32

DIALOG = {'hwnd': 101, 'title': 'No Commercial-Use License | SEGGER SystemView V3.60e',
          'class': 'Qt5152QWindowIcon', 'rect': (401, 237, 1135, 627)}
MAIN = {'hwnd': 202, 'title': 'SEGGER SystemView V3.60e', 'class': 'Qt5152QWindowIcon',
        'rect': (37, 43, 1492, 817)}


class _FakeView:
    """Replaces the window lookups; records how often the dialog was pressed."""

    def __init__(self, dialog_calls=1000, main_window=None, alive=True):
        self.dialog_calls = dialog_calls
        self.presses = []
        self.main_window = main_window
        self.alive = alive

    def install(self, case):
        remaining = [self.dialog_calls]

        def find_dialog(process_id=None):
            if remaining[0] <= 0:
                return None
            remaining[0] -= 1
            return dict(DIALOG)

        def click(window, fraction=v.ACCEPT_FRACTION, process_id=None, confirmed=True):
            self.presses.append((window['rect'], tuple(fraction), process_id))
            return {'posted': True, 'clicked': True, 'x': 988, 'y': 588, 'confirmed': confirmed,
                    'dialog_rect': list(window['rect']), 'fraction': list(fraction),
                    'method': 'post-click' if confirmed else 'mouse'}

        def find_main(process_id=None, timeout=0.0, poll=0.25):
            return dict(self.main_window) if self.main_window else None

        def alive(process_id):
            return self.alive

        saved = (v.find_dialog, v.click_accept, v.find_main_window, v._alive)
        v.find_dialog, v.click_accept, v.find_main_window, v._alive = (
            find_dialog, click, find_main, alive)

        def restore():
            v.find_dialog, v.click_accept, v.find_main_window, v._alive = saved
        case.addCleanup(restore)


#: Skipped only where no backend can be chosen at all -- no user32, or no X display. The assertions
#: below run against ``_FakeView``, so they describe the dispatcher's poll/arm/retry loop, which is
#: the same code on every platform; what the skip says is that piny_view itself would report it has
#: no dialog handling to offer, so there would be nothing to drive.
@unittest.skipUnless(v.available(),
                     'no dialog backend in this session (needs user32 on Windows, an X display on '
                     'Linux), so accept_license can only report and never act')
class LicenseDialogTests(unittest.TestCase):
    def test_absent_dialog_is_not_an_error(self):
        fake = _FakeView(dialog_calls=0)
        fake.install(self)
        report = v.accept_license(timeout=0.2, poll=0.01)
        self.assertFalse(report['dialog_seen'])
        self.assertFalse(report['answered'])
        self.assertEqual(report['clicks'], [])
        self.assertIn('no license dialog appeared', report['note'])

    def test_observe_only_never_clicks(self):
        fake = _FakeView(dialog_calls=1000)
        fake.install(self)
        report = v.accept_license(timeout=0.2, auto_accept=False, poll=0.01)
        self.assertTrue(report['dialog_seen'])
        self.assertFalse(report['answered'])
        self.assertEqual(fake.presses, [])
        self.assertIn('auto-accept disabled', report['note'])

    def test_dialog_answered_once_with_main_window_confirmed(self):
        # Two dialog probes: the first sees it, the second confirms it is gone while the
        # process is still alive, which is the only proof of consent on this locked console.
        fake = _FakeView(dialog_calls=2, main_window=MAIN)
        fake.install(self)
        report = v.accept_license(process_id=4242, timeout=5.0, poll=0.01, arm_seconds=0.0,
                                  main_window_timeout=1.0)
        self.assertTrue(report['dialog_seen'])
        self.assertTrue(report['answered'])
        self.assertEqual(len(fake.presses), 1)
        self.assertEqual(fake.presses[0][2], 4242)
        self.assertEqual(report['method'], 'post-click')
        self.assertEqual(report['main_window']['title'], MAIN['title'])
        self.assertIn('accepted after 1 press', report['note'])

    def test_declined_dialog_is_not_mistaken_for_consent(self):
        # Regression, measured on the real modal: posting Return (VK_RETURN) withdraws the
        # dialog in about a second, but SystemView then EXITS rc=1 -- Enter is the Decline
        # path, and the neighbouring button that exits is at the far right, not the middle.
        # A vanished dialog alone therefore proves nothing; the process must still be alive.
        fake = _FakeView(dialog_calls=1, main_window=MAIN, alive=False)
        fake.install(self)
        report = v.accept_license(process_id=4242, timeout=5.0, poll=0.01, arm_seconds=0.0,
                                  main_window_timeout=1.0)
        self.assertFalse(report['answered'])
        self.assertIn('DECLINED', report['note'])
        self.assertIn('exited', report['note'])
        self.assertIsNone(report['main_window'])   # no point probing a dead launch

    def test_unanswered_dialog_keeps_pressing_and_is_reported(self):
        # A press that Qt ignores leaves the dialog up forever. That must be distinguishable
        # from a healthy launch, otherwise the caller waits out the whole export timeout.
        fake = _FakeView(dialog_calls=1000, main_window=MAIN)
        fake.install(self)
        report = v.accept_license(process_id=4242, timeout=0.25, poll=0.01, arm_seconds=0.0,
                                  retry_interval=0.05, main_window_timeout=1.0)
        self.assertTrue(report['dialog_seen'])
        self.assertFalse(report['answered'])
        self.assertIsNone(report['main_window'])
        self.assertGreaterEqual(len(fake.presses), 2)
        self.assertIn('still present after timeout', report['note'])

    def test_accept_is_the_middle_button_not_the_right_one(self):
        # Regression, measured on the real modal: the buttons read License Manager, Accept,
        # Decline from left to right. A press on the right-hand button (the one a naive
        # "bottom-right corner" guess reaches) DECLINES and SystemView exits rc=1, which is
        # exactly how the first attempts lost the launch. Pin the order and the geometry.
        #
        # The Win32 backend by name, not the dispatcher: this is a MEASUREMENT of that dialog, and
        # the X11 backend lays the buttons out from a documented fraction instead (it has no way to
        # measure them), so it has two entries and no 'License Manager'. Asked through the
        # dispatcher this case described whichever backend the host happened to have: green on
        # Windows, `KeyError: 'Decline'` on the X11 CI job.
        saved = win32._text_width
        win32._text_width = lambda text, extent=v.BUTTON_FONT_PX: 8 * len(text)
        try:
            layout = win32.button_layout({'hwnd': 1, 'rect': (0, 0, 900, 441)}, client=(900, 441))
        finally:
            win32._text_width = saved
        for name, entry in layout.items():
            self.assertEqual(entry['client'], [900, 441])
            self.assertEqual(entry['span'][1] - entry['span'][0],
                             8 * len(name) + 2 * v.BUTTON_PAD_PX)
            self.assertEqual(entry['centre'][1],
                             441 - v.BUTTON_BOTTOM_PX - v.BUTTON_HEIGHT_PX // 2)
        # Right to left: Decline hugs the edge, Accept sits in the middle, License Manager left.
        self.assertEqual(layout['Decline']['span'][1], 900 - v.BUTTON_EDGE_PX)
        self.assertGreater(layout['Accept']['span'][0], layout['License Manager']['span'][1])
        self.assertGreater(layout['Decline']['span'][0], layout['Accept']['span'][1])

    def test_declining_press_is_right_of_the_accepted_one(self):
        # The two verified presses from exp53, in one client space: the middle button opened
        # the main window, the right-hand one exited the process. Win32 by name for the reason
        # given above -- these coordinates are that dialog's.
        saved = win32._text_width
        win32._text_width = lambda text, extent=v.BUTTON_FONT_PX: 8 * len(text)
        try:
            layout = win32.button_layout({'hwnd': 1, 'rect': (0, 0, 720, 352)}, client=(720, 352))
        finally:
            win32._text_width = saved
        self.assertGreater(layout['Decline']['centre'][0], layout['Accept']['centre'][0])
        self.assertGreater(layout['Accept']['centre'][0], layout['License Manager']['centre'][0])

    def test_accepted_dialog_without_main_window_is_flagged(self):
        fake = _FakeView(dialog_calls=2, main_window=None)
        fake.install(self)
        report = v.accept_license(timeout=5.0, poll=0.01, arm_seconds=0.0,
                                  main_window_timeout=0.05)
        self.assertTrue(report['answered'])
        self.assertIsNone(report['main_window'])
        self.assertIn('main window did not appear', report['note'])

    def test_no_main_window_probe_when_not_requested(self):
        fake = _FakeView(dialog_calls=2, main_window=MAIN)
        fake.install(self)
        report = v.accept_license(timeout=5.0, poll=0.01, arm_seconds=0.0,
                                  main_window_timeout=0.0)
        self.assertTrue(report['answered'])
        self.assertIsNone(report['main_window'])

    def test_dialog_title_matching_is_case_insensitive(self):
        self.assertTrue(v.DIALOG_TITLE.casefold() in DIALOG['title'].casefold())
        # The main window title contains the product name, not the dialog marker, so the
        # two lookups cannot be confused for one another.
        self.assertNotIn(v.DIALOG_TITLE.casefold(), MAIN['title'].casefold())
        self.assertIn(v.MAIN_TITLE.casefold(), MAIN['title'].casefold())

    def test_a_session_without_a_dialog_backend_reports_instead_of_raising(self):
        if v.available():
            self.skipTest('this session has a dialog backend (user32 or an X display)')
        report = v.accept_license(timeout=0.1)
        self.assertFalse(report['answered'])
        self.assertIsNone(report['backend'])
        self.assertIn('no dialog backend', report['note'])


class MessageBoxTests(unittest.TestCase):
    """SystemView's blocking boxes are Qt windows whose layout changes with the content.

    Measured on the '43466 Events Loaded' box: the window was 518x497 and its OK button sat
    at ~(1120, 736), which is 0.90 across and 0.92 down - a fraction that misses the same
    button on a shorter box. These tests pin the shape-based lookup and the report.
    """

    def test_the_button_search_is_among_the_names_pushed_to_the_backend(self):
        # `find_default_button` is the one public entry point that is NOT a single forwarding call
        # and still has to see a replacement made here, because the backend's own copy calls that
        # module's `child_windows`. Dropping it from this tuple breaks the search on any platform
        # whose backend exists -- it failed only on Linux CI before, which is exactly the kind of
        # breakage a local run cannot notice, so the wiring is asserted instead.
        self.assertIn('find_default_button', v._PUSH_ON_REPLACE)
        self.assertTrue(set(v._FORWARDED) <= set(v._PUSH_ON_REPLACE))

    def test_button_is_found_by_shape_in_the_bottom_band(self):
        # Needs a backend: the shape rule lives in each backend, and with none reachable (a Linux
        # runner with no DISPLAY, say) the dispatcher has nothing to ask and answers None -- which
        # the Ubuntu job reported as a failure in this test. Skipping is the honest outcome there;
        # the rule itself is exercised on Windows and in the X11 CI job.
        if v.backend() is None:
            self.skipTest('no dialog backend in this session to run the shape rule in')
        window = {'hwnd': 1, 'rect': (696, 289, 1214, 786)}   # 518 x 497, measured
        children = [
            {'hwnd': 2, 'class': 'QWidget', 'text': '', 'rect': (709, 366, 1201, 725)},  # text area
            {'hwnd': 3, 'class': 'QWidget', 'text': 'qt_scrollarea_hcontainer',
             'rect': (710, 703, 1179, 724)},                                            # scrollbar
            {'hwnd': 4, 'class': 'QWidget', 'text': '', 'rect': (1108, 745, 1201, 773)},  # OK button
        ]
        saved = v.child_windows
        v.child_windows = lambda hwnd: children
        try:
            button = v.find_default_button(window)
        finally:
            v.child_windows = saved
        self.assertIsNotNone(button)
        self.assertEqual(button['rect'], [1108, 745, 1201, 773])
        self.assertEqual(button['point'], (1154, 759))

    def test_a_replaced_child_lookup_is_what_the_button_search_uses(self):
        # The regression for a Linux-only failure: the search for the default button lives in the
        # backend and calls that module's own `child_windows`, so replacing it HERE used to change
        # nothing and the backend went to a real display (on the Ubuntu runner: `AssertionError:
        # unexpectedly None`). This pins the wiring without needing a display. The backend is put
        # back afterwards because reaching it installs a wrapper in it.
        backend = v.backend()
        if backend is None:
            self.skipTest('no dialog backend in this session')
        window = {'hwnd': 1, 'rect': (696, 289, 1214, 786)}
        children = [{'hwnd': 4, 'class': 'QWidget', 'text': '', 'rect': (1108, 745, 1201, 773)}]
        seen = []
        saved_local, saved_backend = v.child_windows, backend.child_windows
        original = backend.find_default_button
        v.child_windows = lambda hwnd: (seen.append(hwnd), children)[1]
        try:
            # TWICE on purpose: the overlay runs on every call, and the second call is where a
            # wrapper around the first wrapper showed up (the Linux job asked once inside a test
            # that then asserted on a second call and got None).
            for _ in range(2):
                button = v.find_default_button(window)
                self.assertIsNotNone(button, 'the second call lost the replaced lookup')
                self.assertEqual(button['rect'], [1108, 745, 1201, 773])
        finally:
            v.child_windows = saved_local
            backend.child_windows = saved_backend
            backend.find_default_button = original
        self.assertEqual(seen, [1, 1], 'the replaced lookup was called once per search')
        self.assertIsNotNone(button)
        self.assertEqual(button['rect'], [1108, 745, 1201, 773])

    def test_no_button_child_reports_none_rather_than_guessing(self):
        # Not load-bearing -- this case expects None and gets None either way -- but without the
        # guard the pass would be earned by an absent backend rather than by the shape rule, and
        # that is the kind of green that hides a regression.
        if v.backend() is None:
            self.skipTest('no dialog backend in this session to run the shape rule in')
        window = {'hwnd': 1, 'rect': (100, 100, 400, 300)}
        saved = v.child_windows
        v.child_windows = lambda hwnd: [{'hwnd': 2, 'class': 'QWidget', 'text': '',
                                         'rect': (110, 110, 390, 290)}]
        try:
            self.assertIsNone(v.find_default_button(window))
        finally:
            v.child_windows = saved

    def test_the_backend_stops_seeing_a_stand_in_once_it_is_taken_back(self):
        # The regression for the case that only the X11 CI job could see. Reaching the backend
        # copies this module's stand-in into it; if the copy outlives the caller, a LATER test asks
        # the backend a question about a real display and is answered with a widget list from a
        # display that never existed. The X11 job reported it as
        # `test_a_missing_display_is_not_an_error` -> `child_windows(1) == [{'hwnd': 2, ...}]`.
        # The copy installed in the backend therefore re-reads THIS module at call time.
        backend = v.backend()
        if backend is None:
            self.skipTest('no dialog backend in this session')
        real = backend.child_windows          # whatever the backend does on its own
        calls = []
        marker = [{'hwnd': 2, 'class': 'QWidget', 'text': '', 'rect': (110, 110, 390, 290)}]
        saved_local = v.child_windows
        try:
            v.child_windows = lambda hwnd: (calls.append(hwnd), marker)[1]
            v.find_default_button({'hwnd': 1, 'rect': (100, 100, 400, 300)})
            self.assertEqual(backend.child_windows(1), marker,
                             'the backend did not see the replacement while it was in place')
            v.child_windows = saved_local
            # Two queries, and both are the search doing its job: the backend's own
            # `find_default_button` reads this module first, and the wrapping call re-reads it to
            # decide what the backend's own lookup now is. A third would mean the copy had started
            # answering for itself.
            self.assertEqual(calls, [1, 1])
            # `child_windows` is a forwarded name, so the search must have left a copy in the
            # backend. Asserted rather than returned around: a case that quietly stops exercising
            # the copy when the copy disappears is the shape of a test that reports success for
            # having done nothing.
            self.assertIsInstance(backend.child_windows, v._Copy,
                                  'nothing was pushed into the backend, so there is no copy to '
                                  'stop seeing the stand-in')
            # Each backend reaches its own child list through its own primitive, and there is no
            # name that is right on both -- so whichever this backend has is the one replaced. An
            # empty enumeration is the honest answer from a display that has nothing on it, and
            # nothing real is touched.
            patched = contextlib.ExitStack()
            names = []
            if hasattr(backend, '_user32'):
                names.append('_user32')
                patched.enter_context(mock.patch.object(backend, '_user32', return_value=mock.Mock()))
            if hasattr(backend, '_display'):
                # The connection has to be a number ctypes can pass: a Mock becomes a c_ulong and
                # then "cannot be interpreted as an integer". A stand-in library is what makes the
                # enumeration actually run and answer "nothing" (XQueryTree returns a Mock, which
                # is falsy), rather than only proving that no library raises.
                names.append('_display')
                patched.enter_context(mock.patch.object(backend, '_libraries',
                                                        return_value=(mock.Mock(), None)))
                patched.enter_context(mock.patch.object(backend, '_display', return_value=1))
            self.assertTrue(names, backend.__name__ + ' has no child primitive this case can pin')
            with patched:
                # Only the X11 module connects through `_display`, and only the win32 module has a
                # `_user32`, so the proof that the stand-in took effect has to name whichever
                # primitive this backend was pinned through.
                if '_display' in names:
                    self.assertEqual(backend._display(), 1,
                                     'the connection stand-in is not in effect')
                else:
                    self.assertIsInstance(backend._user32(), mock.Mock,
                                          'the library stand-in is not in effect')
                self.assertEqual(backend.child_windows(1), [],
                                 'the backend still answers from a stand-in nobody owns')
        finally:
            v.child_windows = saved_local
            backend.child_windows = real

    def test_message_box_titles_do_not_match_the_main_window(self):
        for title in ['SEGGER SystemView V3.60e',
                      'SEGGER SystemView V3.60e - live.svdat - FreeRTOS Demo Application']:
            self.assertFalse(any(title.startswith(known) for known in v._MESSAGEBOX_TITLES))
        for title in ['System Information | SEGGER SystemView V3.60e',
                      'SystemView Error', 'SEGGER SystemView Error']:
            self.assertTrue(any(title.startswith(known) for known in v._MESSAGEBOX_TITLES))


if __name__ == '__main__':
    unittest.main()
