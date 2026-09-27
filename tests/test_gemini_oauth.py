
import pytest
import os
from unittest.mock import patch, MagicMock
from llm_providers import GeminiProvider, LLMError

def test_gemini_oauth_init():
    with patch.dict(os.environ, {"GEMINI_ACCESS_TOKEN": "test-token"}, clear=True):
        provider = GeminiProvider(model="gemini-2.5-flash")
        assert provider.access_token == "test-token"
        assert provider.api_key is None

    with patch.dict(os.environ, {"GOOGLE_ACCESS_TOKEN": "test-token-google"}, clear=True):
        provider = GeminiProvider(model="gemini-2.5-flash")
        assert provider.access_token == "test-token-google"

    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(LLMError):
            GeminiProvider()

@patch('llm_providers._http_post')
def test_gemini_oauth_predict(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]}
    mock_post.return_value = mock_resp
    
    with patch.dict(os.environ, {"GEMINI_ACCESS_TOKEN": "test-token"}, clear=True):
        provider = GeminiProvider(model="gemini-2.5-flash")
        messages = [{"role": "user", "content": "Hi"}]
        provider.predict(messages)
        
        args, kwargs = mock_post.call_args
        assert kwargs['headers']['Authorization'] == "Bearer test-token"
        assert "key=" not in args[0]

@patch('llm_providers._http_post')
def test_gemini_api_key_predict(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]}
    mock_post.return_value = mock_resp
    
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True):
        provider = GeminiProvider(model="gemini-2.5-flash")
        messages = [{"role": "user", "content": "Hi"}]
        provider.predict(messages)
        
        args, kwargs = mock_post.call_args
        assert 'Authorization' not in kwargs['headers']
        assert kwargs['headers']['x-goog-api-key'] == "test-key"
        assert "key=" not in args[0]
        assert "v1beta" in args[0]

@patch('llm_providers._http_post')
def test_gemini_priority_predict(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]}
    mock_post.return_value = mock_resp
    
    # If both are present, API Key should be preferred
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "GEMINI_ACCESS_TOKEN": "test-token"}, clear=True):
        provider = GeminiProvider(model="gemini-2.5-flash")
        messages = [{"role": "user", "content": "Hi"}]
        provider.predict(messages)
        
        args, kwargs = mock_post.call_args
        assert kwargs['headers']['x-goog-api-key'] == "test-key"
        assert 'Authorization' not in kwargs['headers']

@patch('llm_providers.requests.get')
def test_gemini_health_check(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp
    
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True):
        provider = GeminiProvider(model="gemini-2.5-flash")
        provider.health_check()
        
        args, kwargs = mock_get.call_args
        assert kwargs['headers']['x-goog-api-key'] == "test-key"
        assert "v1beta" in args[0]
