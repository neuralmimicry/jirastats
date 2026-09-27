import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@pytest.fixture(autouse=True)
def mock_research_cache(tmp_path):
    with patch("topic_researcher.RESEARCH_CACHE_ROOT", str(tmp_path)):
        yield str(tmp_path)

@pytest.fixture
def mock_llm_provider():
    with patch("topic_researcher.get_provider") as mock_get:
        mock_llm = MagicMock()
        mock_llm.get_context_window.return_value = 8192
        mock_llm.estimate_tokens.side_effect = lambda x: len(x) // 4
        mock_get.return_value = mock_llm
        yield mock_llm

@patch('topic_researcher._conf_get')
@patch('topic_researcher.jira_fetch_issues')
def test_link_following_jira_to_confluence(mock_jira_fetch, mock_conf_get, mock_llm_provider):
    # 1. Setup mocks
    researcher = TopicResearcher(jira_base_url="https://test.atlassian.net", jira_auth=("u", "p"), llm_provider="openai")
    
    # Mock Jira issue with Confluence link in description
    issue = MagicMock()
    issue.key = "PROJ-1"
    issue.summary = "Summary 1"
    issue.description = "See https://test.atlassian.net/wiki/spaces/SPACE/pages/123/Doc"
    issue.comments = []
    issue.status = "Open"
    issue.issuetype = "Task"
    issue.assignee = "User A"
    issue.reporter = "User B"
    issue.comment_count = 0
    issue.commenters = []
    issue.parent_key = None
    issue.created = None
    issue.updated = None
    
    # Second Jira issue linked from Confluence
    issue2 = MagicMock()
    issue2.key = "PROJ-2"
    issue2.summary = "Summary 2"
    issue2.description = "Linked issue"
    issue2.comments = []
    issue2.status = "In Progress"
    issue2.issuetype = "Story"
    issue2.assignee = "User A"
    issue2.reporter = "User B"
    issue2.comment_count = 0
    issue2.commenters = []
    issue2.parent_key = None
    issue2.created = None
    issue2.updated = None

    # Mock Jira fetch to return issue 1 on first call, issue 2 on second
    mock_jira_fetch.side_effect = [[issue], [issue2]]
    
    # Mock Confluence page with Jira link in body
    page_data = {
        "results": [
            {
                "content": {
                    "id": "123",
                    "title": "Linked Doc",
                    "body": {"storage": {"value": "Discussed in PROJ-2"}},
                    "history": {"createdBy": {"displayName": "User A"}},
                    "version": {"by": {"displayName": "User A"}},
                    "_links": {"webui": "/wiki/spaces/SPACE/pages/123/Doc"}
                }
            }
        ]
    }
    mock_conf_get.return_value = page_data
    
    # 2. Execute queries
    queries = {
        "jql": "project = PROJ",
        "cql": "",
        "search_queries": [],
        "llm_questions": []
    }
    
    results = researcher._execute_queries(queries)
    
    # 3. Assertions
    # Should have fetched PROJ-1 initially
    assert any(it["key"] == "PROJ-1" for it in results["jira_issues"])
    
    # Should have followed link to Confluence page 123
    assert any(it["id"] == "123" for it in results["confluence_pages"])
    
    # Should have followed link from Confluence page 123 back to Jira PROJ-2
    assert any(it["key"] == "PROJ-2" for it in results["jira_issues"])
    
    # Verify mock calls
    assert mock_jira_fetch.call_count >= 2
    assert mock_conf_get.call_count >= 1
