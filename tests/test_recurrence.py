from datetime import datetime
from trickee.recurrence import Recurrence, occurrences


def test_month_end_and_dst():
    rule = Recurrence(frequency="MONTHLY", count=3)
    dates = occurrences(
        datetime.fromisoformat("2024-01-31T09:00:00-05:00"),
        datetime.fromisoformat("2024-01-31T10:00:00-05:00"),
        "America/New_York",
        rule,
    )
    assert [s.day for s, e in dates] == [31, 29, 31]
    assert [s.hour for s, e in dates] == [9, 9, 9]
    assert dates[-1][0].utcoffset().total_seconds() == -14400


import pytest
from pydantic import ValidationError


def test_gap_overlap_and_interval():
    dates = occurrences(
        datetime.fromisoformat("2024-03-09T02:30:00-05:00"),
        datetime.fromisoformat("2024-03-09T03:30:00-05:00"),
        "America/New_York",
        Recurrence(frequency="DAILY", count=3),
    )
    assert [(s.day, s.hour) for s, e in dates] == [(9, 2), (10, 3), (11, 2)]
    assert all((e.timestamp() - s.timestamp()) == 3600 for s, e in dates)
    dates = occurrences(
        datetime.fromisoformat("2024-11-02T01:30:00-04:00"),
        datetime.fromisoformat("2024-11-02T02:30:00-04:00"),
        "America/New_York",
        Recurrence(frequency="DAILY", count=3),
    )
    assert dates[1][0].fold == 0
    assert dates[1][0].utcoffset().total_seconds() == -14400
    weekly = occurrences(
        datetime.fromisoformat("2024-02-01T09:00:00+00:00"),
        datetime.fromisoformat("2024-02-01T10:00:00+00:00"),
        "UTC",
        Recurrence(frequency="WEEKLY", interval=2, count=3),
    )
    assert [s.day for s, e in weekly] == [1, 15, 29]


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"count": 367},
        {"interval": 0, "count": 3},
        {"count": 3, "until": "2024-05-01T00:00:00Z"},
        {"until": "2024-05-01T00:00:00"},
    ],
)
def test_invalid_rule(values):
    with pytest.raises(ValidationError):
        Recurrence(frequency="DAILY", **values)


def test_until_bound_and_monthly_nonleap():
    start = datetime.fromisoformat("2025-01-31T09:00:00+00:00")
    end = datetime.fromisoformat("2025-01-31T10:00:00+00:00")
    dates = occurrences(
        start, end, "UTC", Recurrence(frequency="MONTHLY", until="2025-03-31T09:00:00Z")
    )
    assert [s.day for s, e in dates] == [31, 28, 31]
    with pytest.raises(ValueError, match="366"):
        occurrences(
            start,
            end,
            "UTC",
            Recurrence(frequency="DAILY", until="2030-01-01T00:00:00Z"),
        )
    with pytest.raises(ValueError, match="first occurrence"):
        occurrences(
            start,
            end,
            "UTC",
            Recurrence(frequency="DAILY", until="2024-01-01T00:00:00Z"),
        )


def test_datetime_boundary_becomes_validation_error():
    from trickee.personal_workspace import CalendarEvent

    for start, end, zone in [
        ("9999-12-31T20:00:00-05:00", "9999-12-31T21:00:00-05:00", "UTC"),
        ("0001-01-01T01:00:00+14:00", "0001-01-01T02:00:00+14:00", "UTC"),
        ("9999-12-01T09:00:00Z", "9999-12-01T10:00:00Z", "UTC"),
    ]:
        with pytest.raises(ValidationError):
            CalendarEvent(
                title="Boundary",
                starts_at=start,
                ends_at=end,
                timezone=zone,
                recurrence={"frequency": "MONTHLY", "count": 3},
            )
