import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher, GoogleSearchEngine

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    llm.predict.return_value.text = "Mock LLM Response"
    return llm

@pytest.fixture
def researcher(mock_llm):
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        with patch("topic_researcher.GoogleSearchEngine.verify", return_value=(True, "Success")):
            return TopicResearcher(
                jira_base_url="https://test.atlassian.net",
                jira_auth=("user", "token"),
                llm_provider="openai",
                google_api_key="test_key",
                google_cse_id="test_cx"
            )

def test_google_search_engine_success():
    engine = GoogleSearchEngine("api_key", "cse_id")
    
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "items": [
            {"title": "Result 1", "snippet": "Snippet 1", "link": "https://example.com/1"},
            {"title": "Result 2", "snippet": "Snippet 2", "link": "https://example.com/2"}
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    
    with patch("requests.get", return_value=mock_resp):
        results = engine.search("test query")
        
    assert len(results) == 2
    assert results[0]["title"] == "Result 1"
    assert results[0]["url"] == "https://example.com/1"

@patch("topic_researcher.jira_fetch_issues")
@patch("topic_researcher._conf_get")
def test_execute_queries_with_google_search(mock_conf, mock_jira, researcher):
    mock_jira.return_value = []
    mock_conf.return_value = {"results": []}
    
    queries = {
        "search_queries": ["test query"],
        "llm_questions": []
    }
    
    # Mock search engine
    researcher.search_engine.search = MagicMock(return_value=[
        {"title": "Result 1", "snippet": "Snippet 1", "url": "https://example.com/1"}
    ])
    
    # Mock _read_source to simulate fetching full content
    researcher._read_source = MagicMock(return_value="Full content of Result 1")
    
    results = researcher._execute_queries(queries)
    
    assert len(results["search_results"]) == 1
    assert results["search_results"][0]["url"] == "https://example.com/1"
    assert results["search_results"][0]["full_content"] == "Full content of Result 1"
    researcher._read_source.assert_called_once_with("https://example.com/1")

def test_google_search_engine_missing_creds():
    engine = GoogleSearchEngine("", "")
    results = engine.search("test")
    assert results == []

def test_google_search_engine_error():
    engine = GoogleSearchEngine("key", "cx")
    with patch("requests.get", side_effect=Exception("API Error")):
        results = engine.search("test")
    assert results == []
