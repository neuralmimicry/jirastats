from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import main as m


def test_analyze_issue_transitions_within_office_hours():
    issue = Mock()
    issue.changelog = Mock()
    issue.changelog.histories = [
        Mock(created="2023-05-01T09:30:00.000+0000", items=[
            Mock(field='status', fromString='Ready to Develop', toString='In Progress')
        ]),
        Mock(created="2023-05-01T16:30:00.000+0000", items=[
            Mock(field='status', fromString='In Progress', toString='For Peer Review')
        ]),
    ]

    total_seconds, qa_returns = m.analyze_issue_transitions(issue)

    # Expected 7 hours within office hours that day
    start_dt = datetime.strptime("2023-05-01T09:30:00.000+0000", '%Y-%m-%dT%H:%M:%S.%f%z')
    end_dt = datetime.strptime("2023-05-01T16:30:00.000+0000", '%Y-%m-%dT%H:%M:%S.%f%z')
    expected = 0
    current_time = start_dt
    while current_time < end_dt:
        if current_time.weekday() < 5 and 9 <= current_time.hour < 17 and current_time.date() not in m._get_holidays_calendar('GB'):
            end_of_hour = current_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            next_hour_within_office = min(end_of_hour, end_dt)
            expected += int((next_hour_within_office - current_time).total_seconds())
        current_time += timedelta(hours=1)
        current_time = current_time.replace(minute=0, second=0, microsecond=0)

    assert total_seconds == expected
    assert qa_returns == 0


def test_analyze_issue_transitions_handles_missing_histories():
    # Simulate cached-like issue without changelog/histories
    issue = NS()
    # No changelog attribute at all
    total_seconds, qa_returns = m.analyze_issue_transitions(issue)
    assert total_seconds == 0
    assert qa_returns == 0
