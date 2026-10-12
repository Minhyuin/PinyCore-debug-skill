"""Tests for the SystemView license-dialog driver (scripts/piny_view.py).

The dialog cannot be tested by launching the real application, so these tests drive the
module's window lookups directly: they check the decision logic -- exactly one press when
the dialog goes away, repeated presses while it stays, and a report that tells an accepted
dialog whose main window never appeared (wedged launch) apart from a healthy one.
"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import piny_view as v

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
        saved = v._text_width
        v._text_width = lambda text, extent=v.BUTTON_FONT_PX: 8 * len(text)
        try:
            layout = v.button_layout({'hwnd': 1, 'rect': (0, 0, 900, 441)}, client=(900, 441))
        finally:
            v._text_width = saved
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
        # the main window, the right-hand one exited the process.
        saved = v._text_width
        v._text_width = lambda text, extent=v.BUTTON_FONT_PX: 8 * len(text)
        try:
            layout = v.button_layout({'hwnd': 1, 'rect': (0, 0, 720, 352)}, client=(720, 352))
        finally:
            v._text_width = saved
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

    def test_button_is_found_by_shape_in_the_bottom_band(self):
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

    def test_no_button_child_reports_none_rather_than_guessing(self):
        window = {'hwnd': 1, 'rect': (100, 100, 400, 300)}
        saved = v.child_windows
        v.child_windows = lambda hwnd: [{'hwnd': 2, 'class': 'QWidget', 'text': '',
                                         'rect': (110, 110, 390, 290)}]
        try:
            self.assertIsNone(v.find_default_button(window))
        finally:
            v.child_windows = saved

    def test_message_box_titles_do_not_match_the_main_window(self):
        for title in ['SEGGER SystemView V3.60e',
                      'SEGGER SystemView V3.60e - live.svdat - FreeRTOS Demo Application']:
            self.assertFalse(any(title.startswith(known) for known in v._MESSAGEBOX_TITLES))
        for title in ['System Information | SEGGER SystemView V3.60e',
                      'SystemView Error', 'SEGGER SystemView Error']:
            self.assertTrue(any(title.startswith(known) for known in v._MESSAGEBOX_TITLES))


if __name__ == '__main__':
    unittest.main()
