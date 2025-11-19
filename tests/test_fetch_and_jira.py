from unittest.mock import Mock, patch
import main as m


def test_fetch_issues_success():
    jira = Mock()
    expected = ["ISSUE-1", "ISSUE-2"]
    jira.search_issues.return_value = expected
    jql = "project = TEST"

    issues = m.fetch_issues(jira, jql)

    jira.search_issues.assert_called_once_with(jql, maxResults=False, expand='changelog,worklog')
    assert issues == expected


def test_fetch_issues_exception_returns_empty():
    jira = Mock()
    jira.search_issues.side_effect = Exception("boom")
    jql = "project = TEST"

    issues = m.fetch_issues(jira, jql)

    assert issues == []
    jira.search_issues.assert_called_once_with(jql, maxResults=False, expand='changelog,worklog')


@patch('main.jira_api')
@patch('main.base64.b64encode')
def test_create_jira_connection_encodes_and_calls(b64encode, jira_api):
    b64encode.return_value = b"enc"
    jira_api.return_value = Mock(name='JIRAClient')
    client = m.create_jira_connection("user", "pass")

    b64encode.assert_called_once_with(b"user:pass")
    jira_api.assert_called_once()
    assert client is jira_api.return_value
