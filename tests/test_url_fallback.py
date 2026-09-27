import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher
import requests

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.predict.return_value = MagicMock(text="Fallback search results")
    return llm

@patch('topic_researcher.get_provider')
@patch('topic_researcher.requests.Session')
@patch('topic_researcher.jira_fetch_issues')
@patch('topic_researcher._conf_get')
def test_jira_fetch_failure_fallback(mock_conf_get, mock_jira_fetch, mock_session_class, mock_get_provider, mock_llm):
    mock_get_provider.return_value = mock_llm
    
    # Simulate Jira fetch failure
    mock_jira_fetch.side_effect = Exception("410 Client Error: Gone")
    
    # Mock LLM response for fallback analysis
    # First call: Fallback analysis of URL
    # Second call: Search simulation (inside MockSearchEngine.search)
    mock_llm.predict.side_effect = [
        MagicMock(text='{"search_terms": "TEST project", "jira_keywords": "TEST", "reasoning": "URL suggests TEST project"}'), # Fallback analysis
        MagicMock(text='Simulated search result content') # MockSearchEngine.search
    ]

    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = {"jql": "project = TEST"}
    results = researcher._execute_queries(queries)
    
    # Check if fallback was attempted
    # Current implementation would just have the error in results["jira_issues"]
    # We want it to have some issues found via fallback
    assert len(results["jira_issues"]) > 0
    # In the new implementation, it should NOT just be an error dict if fallback succeeded
    assert any("error" not in issue for issue in results["jira_issues"])

@patch('topic_researcher.get_provider')
@patch('topic_researcher.requests.Session')
def test_read_source_failure_fallback(mock_session_class, mock_get_provider, mock_llm):
    mock_get_provider.return_value = mock_llm
    
    # Simulate URL fetch failure in _read_source
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.reason = "Not Found"
    mock_session.get.return_value = mock_resp
    
    # Mock LLM response for fallback analysis
    # First call: Fallback analysis of URL
    # Second call: Search simulation
    mock_llm.predict.side_effect = [
        MagicMock(text='{"search_terms": "VMO2 Test Strategy", "reasoning": "URL suggests test strategy"}'),
        MagicMock(text='This is the searched content for the failed URL.')
    ]

    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # This should now try fallback instead of just raising exception or returning error string
    content = researcher._read_source("https://example.com/failed-page")
    
    assert "searched content" in content
