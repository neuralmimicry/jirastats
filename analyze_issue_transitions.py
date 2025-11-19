import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta
import holidays

from main import analyze_issue_transitions


class TestAnalyzeIssueTransitions(unittest.TestCase):
    def setUp(self):
        # Mock setup including office hour transitions, and non-office hour transitions
        self.issue = Mock()
        self.issue.changelog = Mock()
        self.issue.changelog.histories = [
            Mock(created="2023-05-01T09:30:00.000+0000", items=[
                Mock(field='status', fromString='Ready to Develop', toString='In Progress')
            ]),
            Mock(created="2023-05-01T16:30:00.000+0000", items=[
                Mock(field='status', fromString='In Progress', toString='For Peer Review')
            ]),
            Mock(created="2023-05-02T10:00:00.000+0000", items=[
                Mock(field='status', fromString='For Peer Review', toString='Done')
            ])
        ]

    def test_analyze_issue_transitions_within_office_hours(self):
        """
        Test that analyze_issue_transitions function calculates the time spent correctly
        within office hours and excludes non-office times.
        """
        total_seconds, qa_returns = analyze_issue_transitions(self.issue)
        # Manually calculate expected office hours from mock data
        start_dt = datetime.strptime("2023-05-01T09:30:00.000+0000", '%Y-%m-%dT%H:%M:%S.%f%z')
        end_dt = datetime.strptime("2023-05-01T16:30:00.000+0000", '%Y-%m-%dT%H:%M:%S.%f%z')
        uk_holidays = holidays.UnitedKingdom()

        expected_seconds = 0
        current_time = start_dt
        while current_time < end_dt:
            if current_time.weekday() < 5 and 9 <= current_time.hour < 17 and current_time.date() not in uk_holidays:
                end_of_hour = current_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                next_hour_within_office = min(end_of_hour, end_dt)
                if 9 <= next_hour_within_office.hour <= 17:
                    expected_seconds += (next_hour_within_office - current_time).total_seconds()
            current_time += timedelta(hours=1)
            current_time = current_time.replace(minute=0, second=0, microsecond=0)

        self.assertEqual(total_seconds, expected_seconds)
        self.assertEqual(qa_returns, 0)


if __name__ == '__main__':
    unittest.main()
