import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher, GoogleSearchEngine, MockSearchEngine
import requests

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.predict.return_value = MagicMock(text="{}")
    return llm

def test_google_search_verify_success():
    engine = GoogleSearchEngine(api_key="valid_key", cse_id="valid_cx")
    
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        
        ok, msg = engine.verify()
        assert ok is True
        assert msg == "Success"
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert kwargs["params"]["key"] == "valid_key"
        assert kwargs["params"]["cx"] == "valid_cx"

def test_google_search_verify_failure():
    engine = GoogleSearchEngine(api_key="invalid_key", cse_id="invalid_cx")
    
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"error": {"message": "Invalid API key"}}
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("Forbidden", response=mock_resp)
        mock_get.return_value = mock_resp
        
        ok, msg = engine.verify()
        assert ok is False
        assert "Invalid API key" in msg

def test_topic_researcher_fallback_on_verify_failure(mock_llm):
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        with patch("topic_researcher.GoogleSearchEngine.verify", return_value=(False, "Unauthorized")):
            researcher = TopicResearcher(
                jira_base_url="https://test.atlassian.net",
                jira_auth=("user", "token"),
                llm_provider="openai",
                google_api_key="bad_key",
                google_cse_id="bad_cx"
            )
            # Should fall back to MockSearchEngine
            assert isinstance(researcher.search_engine, MockSearchEngine)

def test_topic_researcher_success_on_verify(mock_llm):
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        with patch("topic_researcher.GoogleSearchEngine.verify", return_value=(True, "Success")):
            researcher = TopicResearcher(
                jira_base_url="https://test.atlassian.net",
                jira_auth=("user", "token"),
                llm_provider="openai",
                google_api_key="good_key",
                google_cse_id="good_cx"
            )
            # Should stay with GoogleSearchEngine
            assert isinstance(researcher.search_engine, GoogleSearchEngine)
            assert researcher.search_engine.api_key == "good_key"
