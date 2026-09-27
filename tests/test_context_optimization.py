import pytest
import os
from unittest.mock import MagicMock, patch
from llm_providers import OpenAIProvider, GeminiProvider, OllamaProvider, get_provider
from topic_researcher import TopicResearcher

def test_openai_context_window():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
        p = OpenAIProvider(model="gpt-4o")
        assert p.get_context_window() == 128000
        
        p2 = OpenAIProvider(model="gpt-4")
        assert p2.get_context_window() == 8192
        
        p3 = OpenAIProvider(model="gpt-4-32k")
        assert p3.get_context_window() == 32768

def test_gemini_context_window():
    with patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
        p = GeminiProvider(model="gemini-2.5-flash")
        assert p.get_context_window() == 1000000
        
        p2 = GeminiProvider(model="gemini-1.5-pro")
        assert p2.get_context_window() == 2000000

@patch("requests.post")
def test_ollama_context_window(mock_post):
    # Mock /api/show response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "parameters": "num_ctx 8192",
        "model_info": {"llama.context_length": 8192}
    }
    mock_post.return_value = mock_resp
    
    p = OllamaProvider(model="llama3")
    assert p.get_context_window() == 8192
    
    # Test fallback
    mock_resp.status_code = 404
    p2 = OllamaProvider(model="phi3")
    assert p2.get_context_window() == 128000

def test_topic_researcher_dynamic_thresholds():
    mock_llm = MagicMock()
    # Mock 128k context window (e.g. gpt-4o)
    mock_llm.get_context_window.return_value = 128000
    
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        researcher = TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "pass"),
            llm_provider="openai"
        )
        
        # Threshold should be capped at 10000
        assert researcher.token_threshold == 10000
        assert researcher.max_content_chars == 100000 # Capped at 100k
        
    # Mock 4k context window (e.g. gpt-3.5-turbo or small local)
    mock_llm.get_context_window.return_value = 4096
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        researcher = TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "pass"),
            llm_provider="openai"
        )
        assert researcher.token_threshold == 1024 # 4096 // 4
        assert researcher.max_content_chars == 8192 # 4096 * 2

@patch("topic_researcher.get_provider")
def test_efficient_context_trigger(mock_get_provider):
    mock_llm = MagicMock()
    mock_llm.get_context_window.return_value = 4000 # 1000 threshold
    mock_llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    mock_get_provider.return_value = mock_llm
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # threshold = 1000 tokens = 4000 chars
    
    # Small draft: 2000 chars = 500 tokens
    small_draft = "A" * 2000
    context = researcher._get_token_efficient_context(small_draft)
    assert context == small_draft # Should not be efficient
    
    # Large draft: 6000 chars = 1500 tokens
    large_draft = "# Title\n" + "Content\n" * 800
    context = researcher._get_token_efficient_context(large_draft)
    assert "Existing Table of Contents" in context # Should be efficient
