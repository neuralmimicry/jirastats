import types
import builtins
from datetime import datetime
from unittest.mock import Mock, patch

import main as m


def test_seconds_to_work_units():
    assert m.seconds_to_work_units(0) == 0
    assert m.seconds_to_work_units(14399) == 0
    assert m.seconds_to_work_units(14400) == 1
    assert m.seconds_to_work_units(28801) == 2


def test_normalize_name():
    assert m.normalize_name("John Doe!") == "john doe"
    assert m.normalize_name("Mary-Jane") == "maryjane"
    assert m.normalize_name("O'Neil") == "oneil"


def test_sorting_key_suffix_order():
    # non-UniVerse should come before UniVerse, and both grouped by base name
    k1 = m.sorting_key("Alpha (non-UniVerse)")
    k2 = m.sorting_key("Alpha (UniVerse)")
    assert k1 < k2


def test_convert_month_string_to_datetime():
    dt = m.convert_month_string_to_datetime("2023-04")
    assert isinstance(dt, datetime)
    assert dt.year == 2023 and dt.month == 4 and dt.day == 1


def test_get_holidays_calendar_supported_codes():
    # should not raise and return an object iterable-like
    for code in ["GB", "UK", "US", "CA", "DE", "FR", "ZZ"]:
        cal = m._get_holidays_calendar(code)
        # Calendar should have __contains__ for a date
        assert hasattr(cal, "__contains__")


def test__get_field_and__get_epic_key():
    issue = types.SimpleNamespace()
    fields = types.SimpleNamespace()
    issue.fields = fields
    # set attributes for different access patterns
    fields.created = "2025-01-01"
    fields.updated = "2025-01-02"
    # epic field as object with key attribute
    class EpicLink:
        def __init__(self, key):
            self.key = key
    setattr(fields, "customfield_10014", EpicLink("EPIC-123"))

    assert m._get_field(issue, "created") == "2025-01-01"
    assert m._get_field(issue, "updated") == "2025-01-02"

    epic = m._get_epic_key(issue, {"epic_link": ["customfield_10014"]})
    assert epic == "EPIC-123"


def test_get_monthly_worklog_times_universe_tag():
    issue = Mock()
    # worklogs for same month and author
    issue.fields.worklog.worklogs = [
        Mock(started="2023-04-01T12:30:45.000+0000", timeSpentSeconds=7200, author=Mock(displayName="John Doe")),
        Mock(started="2023-04-02T14:00:00.000+0000", timeSpentSeconds=3600, author=Mock(displayName="John Doe")),
    ]
    # skills include UniVerse so suffix should be (UniVerse)
    issue.fields.customfield_10900 = [Mock(value="UniVerse"), Mock(value="Python")]
    issue.fields.customfield_10952 = Mock(value="Development")

    result = m.get_monthly_worklog_times(issue)
    assert result == {
        "John Doe": {
            "2023-04": {
                "time_spent": 10800,
                "Development (UniVerse)": {"time_spent": 10800},
            }
        }
    }
