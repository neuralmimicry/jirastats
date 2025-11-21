import unittest
from unittest.mock import Mock, patch

from main import fetch_issues


class TestFetchIssues(unittest.TestCase):
    @patch('main.print')  # Optional: mock print to suppress output during tests
    def test_fetch_issues_successful(self, mock_print):
        # Create a mock JIRA connector
        mock_jira_connector = Mock()
        expected_issues = ['ISSUE-1', 'ISSUE-2', 'ISSUE-3']

        # Set up the mock to return a specific list of issues
        mock_jira_connector.search_issues.return_value = expected_issues

        # Define a sample JQL query
        jql_query = "project = TEST"

        # Call the function
        issues = fetch_issues(mock_jira_connector, jql_query)

        # Assertions to ensure it returns the correct issues
        # With paginated client path we now call with startAt/maxResults
        import main as m
        mock_jira_connector.search_issues.assert_called_once_with(
            jql_query, startAt=0, maxResults=m.PAGE_SIZE, expand='changelog,worklog'
        )
        self.assertEqual(issues, expected_issues)

    def test_fetch_issues_with_exception(self):
        # Create a mock JIRA connector that raises an exception when search_issues is called
        mock_jira_connector = Mock()
        mock_jira_connector.search_issues.side_effect = Exception("Connection Error")

        # Define a sample JQL query
        jql_query = "project = TEST"

        # Call the function
        issues = fetch_issues(mock_jira_connector, jql_query)

        # Assertions to check the function handles exceptions correctly
        self.assertEqual(issues, [])
        import main as m
        mock_jira_connector.search_issues.assert_called_once_with(
            jql_query, startAt=0, maxResults=m.PAGE_SIZE, expand='changelog,worklog'
        )


if __name__ == '__main__':
    unittest.main()
