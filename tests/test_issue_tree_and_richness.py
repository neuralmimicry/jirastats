import pytest
import datetime as dt
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher
from atlassian_utils import IssueInfo

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    return llm

@pytest.fixture
def researcher(mock_llm):
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        with patch("topic_researcher.GoogleSearchEngine.verify", return_value=(True, "Success")):
            with patch("topic_researcher.TopicResearcher._fetch_available_containers"):
                r = TopicResearcher(
                    jira_base_url="https://test.atlassian.net",
                    jira_auth=("user", "token"),
                    llm_provider="openai"
                )
                r._containers_fetched = True
                return r

def test_evaluate_issue_richness(researcher):
    # High richness issue
    high_issue = MagicMock(spec=IssueInfo)
    high_issue.description = "A very long description that should contribute to high richness. " * 20
    high_issue.comment_count = 10
    high_issue.commenters = ["User1", "User2", "User3", "User4"]
    high_issue.updated = dt.datetime.now(dt.timezone.utc)
    
    evaluation = researcher._evaluate_issue_richness(high_issue)
    assert evaluation["richness"] == "High"
    assert evaluation["score"] >= 5
    
    # Low richness issue
    low_issue = MagicMock(spec=IssueInfo)
    low_issue.description = "Short"
    low_issue.comment_count = 0
    low_issue.commenters = []
    low_issue.updated = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)
    
    evaluation = researcher._evaluate_issue_richness(low_issue)
    assert evaluation["richness"] == "Low"
    assert "Minimal information" in evaluation["observations"]
    assert "Potentially stale" in evaluation["observations"]

@patch("topic_researcher.jira_fetch_issues")
def test_fetch_issue_tree(mock_jira_fetch, researcher):
    # Mock parent issue
    parent_issue = MagicMock(spec=IssueInfo)
    parent_issue.key = "PARENT-1"
    parent_issue.summary = "Parent Summary"
    parent_issue.status = "In Progress"
    parent_issue.issuetype = "Epic"
    parent_issue.description = "Parent Description"
    parent_issue.parent_key = None
    
    # Mock sibling issue
    sibling_issue = MagicMock(spec=IssueInfo)
    sibling_issue.key = "SIBLING-1"
    sibling_issue.summary = "Sibling Summary"
    sibling_issue.status = "Done"
    
    def side_effect(base_url, auth, projects, jql, limit):
        if "key = PARENT-1" in jql:
            return [parent_issue]
        if "parent = PARENT-1" in jql:
            return [sibling_issue]
        return []
        
    mock_jira_fetch.side_effect = side_effect
    
    tree = researcher._fetch_issue_tree("ISSUE-1", "PARENT-1")
    
    assert tree["parent"]["key"] == "PARENT-1"
    assert len(tree["siblings"]) == 1
    assert tree["siblings"][0]["key"] == "SIBLING-1"
    
    assert mock_jira_fetch.call_count == 2

@patch("topic_researcher.jira_fetch_issues")
def test_execute_jql_with_context(mock_jira_fetch, researcher):
    issue = MagicMock(spec=IssueInfo)
    issue.key = "ISSUE-1"
    issue.summary = "Test Issue"
    issue.description = "Detailed description here..."
    issue.status = "Open"
    issue.issuetype = "Task"
    issue.assignee = "Assignee"
    issue.reporter = "Reporter"
    issue.comment_count = 2
    issue.commenters = ["User1", "User2"]
    issue.parent_key = "PARENT-1"
    issue.created = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    issue.updated = dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc)
    
    parent_issue = MagicMock(spec=IssueInfo)
    parent_issue.key = "PARENT-1"
    parent_issue.summary = "Parent Summary"
    parent_issue.description = "Parent Description"
    parent_issue.status = "In Progress"
    parent_issue.issuetype = "Epic"
    parent_issue.parent_key = None
    
    def side_effect(base_url, auth, projects, jql, limit):
        if "key = ISSUE-1" in jql or "text ~" in jql:
            return [issue]
        if "key = PARENT-1" in jql:
            return [parent_issue]
        return []
        
    mock_jira_fetch.side_effect = side_effect
    
    results = []
    researcher._execute_jql("key = ISSUE-1", results)
    
    assert len(results) == 1
    assert results[0]["key"] == "ISSUE-1"
    assert "richness_evaluation" in results[0]
    assert "context_tree" in results[0]
    assert results[0]["context_tree"]["parent"]["key"] == "PARENT-1"
    assert results[0]["created"] == "2025-01-01T00:00:00+00:00"
