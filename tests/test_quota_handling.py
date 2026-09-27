import pytest
import json
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher
from llm_providers import LLMQuotaError, LLMResponse

@pytest.fixture
def mock_primary_llm():
    llm = MagicMock()
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    return llm

@pytest.fixture
def mock_fallback_llm():
    llm = MagicMock()
    llm.get_context_window.return_value = 4096
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    return llm

@patch('topic_researcher.get_provider')
def test_quota_relevance_heuristic_fallback(mock_get_provider, mock_primary_llm):
    mock_get_provider.return_value = mock_primary_llm
    
    # Simulate Quota error on predict
    mock_primary_llm.predict.side_effect = LLMQuotaError("Quota exceeded")
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="gemini"
    )
    
    # Topic and requirements
    topic = "Testing Strategy"
    reqs = "Expand on functional and performance testing"
    
    # Content that should be relevant based on keywords
    content = "This document describes the testing strategy, focusing on performance and functional aspects."
    
    # Should fall back to heuristic and return True
    is_relevant = researcher._is_content_relevant(content, topic, reqs)
    
    assert is_relevant is True
    assert researcher._quota_reached is True
    assert mock_primary_llm.predict.call_count == 1

def test_heuristic_relevance_logic():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="ollama"
    )
    
    topic = "VMO2 Cross-Domain Test Strategy"
    reqs = "Focus on Jira and Confluence data"
    
    # Matches multiple keywords
    assert researcher._heuristic_relevance_check("VMO2 test strategy using Jira", topic, reqs) is True
    
    # Matches exactly (case insensitive)
    assert researcher._heuristic_relevance_check("vmo2 cross-domain test strategy is here", topic, reqs) is True
    
    # Irrelevant
    assert researcher._heuristic_relevance_check("How to bake a cake with flour", topic, reqs) is False

@patch('topic_researcher.get_provider')
def test_quota_provider_fallback(mock_get_provider, mock_primary_llm, mock_fallback_llm):
    # Setup mock_get_provider to return primary then fallback
    mock_get_provider.side_effect = [mock_primary_llm, mock_fallback_llm]
    
    # Primary hits quota
    mock_primary_llm.predict.side_effect = LLMQuotaError("Primary quota hit")
    
    # Fallback works
    mock_fallback_llm.predict.return_value = LLMResponse(text='{"jql": "fallback", "cql": "", "search_queries": [], "llm_questions": []}', raw={})
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="gemini",
        fallback_llm_provider="ollama"
    )
    
    queries = researcher._generate_queries("Topic", "Reqs")
    
    assert queries["jql"] == "fallback"
    assert researcher._quota_reached is True
    assert mock_primary_llm.predict.call_count == 1
    assert mock_fallback_llm.predict.call_count == 1

@patch('topic_researcher.get_provider')
def test_circuit_breaker_prevents_llm_calls(mock_get_provider, mock_primary_llm):
    mock_get_provider.return_value = mock_primary_llm
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="gemini"
    )
    
    researcher._quota_reached = True
    
    # Relevance check should use heuristic immediately
    topic = "Test"
    reqs = "Reqs"
    content = "Irrelevant content"
    
    is_relevant = researcher._is_content_relevant(content, topic, reqs)
    
    assert is_relevant is False
    assert mock_primary_llm.predict.call_count == 0
