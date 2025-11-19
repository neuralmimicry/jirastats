from main import get_monthly_worklog_times
import unittest
from unittest.mock import Mock
import os
import getpass
from unittest.mock import patch  # Correct import for the patch function
from datetime import datetime


class TestGetMonthlyWorklogTimes(unittest.TestCase):
    def setUp(self):
        # Create a mock issue object with nested properties
        self.issue = Mock()
        self.issue.fields.worklog.worklogs = [
            Mock(started="2023-04-01T12:30:45.000+0000", timeSpentSeconds=7200,
                 author=Mock(displayName='John Doe')),
            Mock(started="2023-04-02T14:00:00.000+0000", timeSpentSeconds=3600,
                 author=Mock(displayName='John Doe')),
        ]
        self.issue.fields.customfield_10900 = [Mock(value='UniVerse'), Mock(value='Python')]
        self.issue.fields.customfield_10952 = Mock(value='Development')

    def test_get_monthly_worklog_times(self):
        """
        Test that the get_monthly_worklog_times function aggregates worklog times correctly.
        """
        result = get_monthly_worklog_times(self.issue)
        expected = {
            'John Doe': {
                '2023-04': {
                    'time_spent': 10800,
                    'Development (UniVerse)': {
                        'time_spent': 10800
                    }
                }
            }
        }
        self.assertEqual(result, expected)


if __name__ == '__main__':
    unittest.main()
