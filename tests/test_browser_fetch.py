import pytest
import requests
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    return llm

@patch("topic_researcher.get_provider")
@patch("topic_researcher.requests.Session")
def test_fetch_url_403_with_llm_advice(mock_session_class, mock_get_provider, mock_llm):
    mock_get_provider.return_value = mock_llm
    
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session
    
    resp_403 = MagicMock()
    resp_403.status_code = 403
    resp_403.reason = "Forbidden"
    
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.text = "Success content"
    resp_200.headers = {"Content-Type": "text/html"}
    
    # Side effect for session.get
    # 1. attempt 1 (headers_list[0]) -> 403
    # 2. attempt 2 (headers_list[1]) -> 403
    # 3. LLM advice attempt -> 200
    mock_session.get.side_effect = [resp_403, resp_403, resp_200]
    
    # Mock LLM advice
    mock_llm.predict.return_value = MagicMock(text='{"headers": {"User-Agent": "Special UA"}, "cookies": {"accepted": "true"}, "params": {}, "reasoning": "Try this"}')
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    url = "https://forbidden.com"
    resp = researcher._fetch_url(url)
    
    assert resp.status_code == 200
    assert resp.text == "Success content"
    
    # Verify calls
    assert mock_session.get.call_count == 3
    # Check that the 3rd call used the LLM headers
    args, kwargs = mock_session.get.call_args
    assert kwargs["headers"]["User-Agent"] == "Special UA"
    assert kwargs["cookies"]["accepted"] == "true"

@patch("topic_researcher.get_provider")
@patch("topic_researcher.requests.Session")
def test_fetch_url_405_retry(mock_session_class, mock_get_provider, mock_llm):
    mock_get_provider.return_value = mock_llm
    
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session
    
    resp_405 = MagicMock()
    resp_405.status_code = 405
    resp_405.reason = "Method Not Allowed"
    
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.text = "Success"
    resp_200.headers = {"Content-Type": "text/html"}
    
    mock_session.get.side_effect = [resp_405, resp_200]
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    resp = researcher._fetch_url("https://example.com")
    assert resp.status_code == 200
    assert mock_session.get.call_count == 2

@patch("topic_researcher.get_provider")
@patch("topic_researcher.requests.Session")
def test_read_source_integration(mock_session_class, mock_get_provider, mock_llm):
    mock_get_provider.return_value = mock_llm
    
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session
    
    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.text = "Fetched Content"
    resp_200.headers = {"Content-Type": "text/plain"}
    mock_session.get.return_value = resp_200
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    content = researcher._read_source("https://example.com")
    assert content == "Fetched Content"
    mock_session.get.assert_called()
