"""Phone QA must scroll clipped controls clear of the system touch region."""

import xml.etree.ElementTree as ET

from scripts import verify_native_driver as phone


def test_native_tap_scrolls_a_clipped_control_clear_of_the_status_bar(monkeypatch):
    scrolled = False
    taps = []

    def hierarchy():
        bounds = "[100,400][980,554]" if scrolled else "[100,96][980,159]"
        return ET.fromstring(
            '<hierarchy><node text="Check chargers near plan origin" '
            f'enabled="true" bounds="{bounds}" /></hierarchy>'
        )

    def adb(*arguments):
        nonlocal scrolled
        if arguments[:3] == ("shell", "input", "swipe"):
            scrolled = True
        if arguments[:3] == ("shell", "input", "tap"):
            taps.append(tuple(map(int, arguments[3:])))
        return "mCurrentFocus=Window{qa com.trickee.driver/.MainActivity}"

    monkeypatch.setattr(phone, "hierarchy", hierarchy)
    monkeypatch.setattr(phone, "adb", adb)
    monkeypatch.setattr(phone.time, "sleep", lambda delay: None)
    phone.tap("Check chargers near plan origin")
    assert scrolled
    assert taps == [(540, 477)]


def test_native_connection_fields_are_reachable_from_a_long_active_trip(monkeypatch):
    offset = 15000
    taps = []

    def hierarchy():
        if offset == 0:
            return ET.fromstring('<hierarchy><node text="Driver credential" '
                                 'enabled="true" bounds="[100,600][980,760]" /></hierarchy>')
        return ET.fromstring('<hierarchy><node text="Trip details" '
                             'enabled="true" bounds="[100,600][980,760]" /></hierarchy>')

    def adb(*arguments):
        nonlocal offset
        if arguments[:3] == ("shell", "input", "swipe"):
            offset = max(0, min(40000, offset + int(arguments[4]) - int(arguments[6])))
        if arguments[:3] == ("shell", "input", "tap"):
            taps.append(tuple(map(int, arguments[3:])))
        return "mCurrentFocus=Window{qa com.trickee.driver/.MainActivity}"

    monkeypatch.setattr(phone, "hierarchy", hierarchy)
    monkeypatch.setattr(phone, "adb", adb)
    monkeypatch.setattr(phone.time, "sleep", lambda delay: None)
    phone.tap("Driver credential")
    assert taps == [(540, 680)]
