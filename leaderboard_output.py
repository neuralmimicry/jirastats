import unittest
from unittest.mock import patch, mock_open
import csv

from main import leaderboard_output, seconds_to_work_units


class TestLeaderboardOutput(unittest.TestCase):
    @patch('main.open', new_callable=mock_open)
    @patch('main.csv.writer')
    @patch('main.seconds_to_work_units', side_effect=lambda x: str(int(x/3600)) + ' hours')
    def test_leaderboard_output(self, mock_seconds_to_work_units, mock_csv_writer, mock_file):
        # Setup test data
        sorted_leaderboard = [
            ('Jane Doe', {'tasks_completed': 5, 'qa_returns': 3, 'total_time': 7200, 'throughput': 2.5, 'qa_return_rate': 60}),
            ('John Smith', {'tasks_completed': 7, 'qa_returns': 1, 'total_time': 14400, 'throughput': 3.5, 'qa_return_rate': 14.29})
        ]

        # Call the function
        leaderboard_output(sorted_leaderboard)

        # Check that file was opened correctly
        mock_file.assert_called_once_with('leaderboard.csv', mode='w', newline='')

        # Create a mock writer object and ensure it's used correctly
        mock_writer = mock_csv_writer.return_value
        header = ["Name", "Total Coding Duration", "QA Returns", "Tasks Completed", "Average Coding Duration", "Throughput (tasks/month)", "QA Return Rate (%)"]
        mock_writer.writerow.assert_any_call(header)
        expected_calls = [
            ['Jane Doe', '2 hours', 3, 5, '0 hours', 2.5, '60.00%'],
            ['John Smith', '4 hours', 1, 7, '0 hours', 3.5, '14.29%']
        ]
        calls = [call.args[0] for call in mock_writer.writerow.call_args_list if call.args[0][0] in ['Jane Doe', 'John Smith']]
        self.assertEqual(calls, expected_calls)

        # Verify print statement (optional)
        with patch('main.print') as mock_print:
            leaderboard_output(sorted_leaderboard)
            mock_print.assert_called_with("Leaderboard data has been written to leaderboard.csv")


if __name__ == '__main__':
    unittest.main()
