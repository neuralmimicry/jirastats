import unittest
from unittest.mock import Mock

from main import sort_issues_by_priority


class TestSortIssuesByPriority(unittest.TestCase):
    def test_sort_issues_by_priority(self):
        # Create mock issues with alphanumeric custom field values
        mock_issue1 = Mock()
        mock_issue1.fields = Mock(customfield_10104='A002')

        mock_issue2 = Mock()
        mock_issue2.fields = Mock(customfield_10104='A003')

        mock_issue3 = Mock()
        mock_issue3.fields = Mock(customfield_10104='A001')

        # List of unsorted issues
        issues = [mock_issue1, mock_issue2, mock_issue3]

        # Expected sorted order based on the alphanumeric value
        expected_sorted_issues = [mock_issue3, mock_issue1, mock_issue2]

        # Call the function
        sorted_issues = sort_issues_by_priority(issues)

        # Extract the alphanumeric indices for comparison
        sorted_indices = [issue.fields.customfield_10104 for issue in sorted_issues]
        expected_indices = [issue.fields.customfield_10104 for issue in expected_sorted_issues]

        self.assertEqual(sorted_indices, expected_indices)


if __name__ == '__main__':
    unittest.main()
