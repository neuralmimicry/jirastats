import pytest
import json
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    # Mock context window and estimate_tokens to avoid MagicMock vs int comparison errors
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
                # Ensure it doesn't try to filter based on real environment
                r._containers_fetched = True
                r._available_projects = set()
                r._available_spaces = set()
                return r

def test_generate_queries_empty_response(researcher, mock_llm):
    # Simulate empty response from LLM
    mock_llm.predict.return_value.text = ""
    
    # Should use fallback queries
    queries = researcher._generate_queries("Topic", "Reqs")
    
    assert 'text ~ "Topic"' in queries["jql"]
    assert 'text ~ "Reqs"' in queries["jql"]
    assert "Topic" in queries["search_queries"]

def test_generate_queries_malformed_json(researcher, mock_llm):
    # Simulate malformed JSON response from LLM
    mock_llm.predict.return_value.text = "Here is your JSON: { 'invalid': 'json' }"
    
    queries = researcher._generate_queries("Topic", "Reqs")
    
    assert 'text ~ "Topic"' in queries["jql"]

def test_generate_queries_missing_keys(researcher, mock_llm):
    # Simulate JSON missing required keys
    mock_llm.predict.return_value.text = '{"jql": "project = PROJ"}'
    
    queries = researcher._generate_queries("Topic", "Reqs")
    
    assert queries["jql"] == "project = PROJ"
    # missing keys should be filled from fallbacks
    assert "Topic" in queries["search_queries"]
    assert "llm_questions" in queries

def test_formulate_document_empty_response(researcher, mock_llm):
    mock_llm.predict.return_value.text = ""
    result = researcher._formulate_document("Topic", "Reqs", {}, current_draft="Previous Draft")
    assert result == "Previous Draft"

def test_agentic_debate_empty_critic(researcher, mock_llm):
    mock_llm.predict.return_value.text = "" # Critic returns nothing
    result = researcher._agentic_debate_and_refine("Topic", "Reqs", "Original Draft")
    assert result == "Original Draft"

def test_fallback_research_empty_response(researcher, mock_llm):
    mock_llm.predict.return_value.text = ""
    results = researcher._fallback_url_research("https://fail.com", "404")
    assert results == []

def test_extract_json_robustness(researcher):
    # Test with extra text and stray braces
    text = """
    Here are the queries {stray brace}:
    ```json
    {
      "jql": "project = \\"PROJ\\"",
      "cql": "space = \\"SPACE\\"",
      "search_queries": ["test"],
      "llm_questions": ["q"]
    }
    ```
    Hope this {helps}.
    """
    result = researcher._extract_json(text)
    assert result is not None
    assert result["jql"] == 'project = "PROJ"'
    
    # Test without backticks but with extra text
    text2 = """
    Some text before
    {
      "key": "value"
    }
    Some text after
    """
    result2 = researcher._extract_json(text2)
    assert result2 is not None
    assert result2["key"] == "value"

def test_sanitize_queries_extended(researcher):
    queries = {
        "jql": 'page = "test" AND title ~ "something"',
        "cql": 'page = "test" AND text ~ "something"',
    }
    sanitized = researcher._sanitize_queries(queries)
    assert sanitized["jql"] == 'text = "test" AND summary ~ "something"'
    assert sanitized["cql"] == 'title = "test" AND text ~ "something"'

def test_fallback_queries_escaping(researcher, mock_llm):
    # Mock LLM to return empty response
    mock_llm.predict.return_value.text = ""
    
    # Topic with quotes
    topic = 'The "Best" Strategy'
    reqs = 'Use "Standard" language'
    
    queries = researcher._generate_queries(topic, reqs)
    
    # Check that quotes are escaped in fallback JQL/CQL
    assert 'text ~ "The \\"Best\\" Strategy"' in queries["jql"]
    assert 'text ~ "Use \\"Standard\\" language"' in queries["jql"]
