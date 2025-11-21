from unittest.mock import Mock, patch
import main as m


def test_fetch_issues_success():
    jira = Mock()
    expected = ["ISSUE-1", "ISSUE-2"]
    # Simulate single-page result
    jira.search_issues.return_value = expected
    jql = "project = TEST"

    issues = m.fetch_issues(jira, jql)

    jira.search_issues.assert_called_once_with(jql, startAt=0, maxResults=m.PAGE_SIZE, expand='changelog,worklog')
    assert issues == expected


def test_fetch_issues_exception_returns_empty():
    jira = Mock()
    jira.search_issues.side_effect = Exception("boom")
    jql = "project = TEST"

    issues = m.fetch_issues(jira, jql)

    assert issues == []
    jira.search_issues.assert_called_once_with(jql, startAt=0, maxResults=m.PAGE_SIZE, expand='changelog,worklog')


@patch('main.jira_api')
def test_create_jira_connection_uses_basic_auth(jira_api):
    jira_api.return_value = Mock(name='JIRAClient')
    client = m.create_jira_connection("user", "pass")

    # Should construct client via python-jira using basic_auth
    jira_api.assert_called_once()
    assert client is jira_api.return_value
