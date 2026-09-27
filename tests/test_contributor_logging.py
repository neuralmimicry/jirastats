import pytest
import logging
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@patch("topic_researcher.get_provider")
@patch("topic_researcher.jira_fetch_issues")
@patch("topic_researcher._conf_get")
def test_contributor_logging(mock_conf_get, mock_jira_fetch, mock_get_provider, caplog):
    # Setup mocks
    mock_llm = MagicMock()
    mock_llm.get_context_window.return_value = 8192
    mock_llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    mock_get_provider.return_value = mock_llm
    
    # Mock LLM response for queries
    mock_llm.predict.side_effect = [
        MagicMock(text='{"jql": "project=PROJ", "cql": "space=SPACE", "search_queries": [], "llm_questions": []}'), # _generate_queries
        MagicMock(text='YES') # _is_complete
    ]
    
    # Mock Jira issues
    mock_issue = MagicMock()
    mock_issue.key = "PROJ-123"
    mock_issue.summary = "Test Issue"
    mock_issue.description = "Test Description"
    mock_issue.status = "Open"
    mock_jira_fetch.return_value = [mock_issue]
    
    # Mock Confluence pages
    mock_conf_get.return_value = {
        "results": [
            {
                "content": {
                    "id": "98765",
                    "title": "Test Page",
                    "body": {"storage": {"value": "Page content"}}
                }
            }
        ]
    }
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # Mock _formulate_document and _agentic_debate_and_refine to avoid more LLM calls
    researcher._formulate_document = MagicMock(return_value="Draft")
    researcher._agentic_debate_and_refine = MagicMock(return_value="Refined Draft")
    researcher._sanity_check_document = MagicMock(side_effect=lambda x, final_pass=False: x)
    
    # Create a dummy source file
    with open("test_topic.txt", "w") as f:
        f.write("Topic\nRequirements")
    
    caplog.set_level(logging.INFO)
    
    researcher.run("test_topic.txt", "test_out.md", max_iterations=1)
    
    # Check logs
    assert "Tracked Jira contribution: PROJ-123" in caplog.text
    assert "Tracked Confluence contribution: Test Page (98765)" in caplog.text
    assert "Research report contributed by Jira issues: PROJ-123" in caplog.text
    assert "Research report contributed by Confluence pages: Test Page (98765)" in caplog.text
    
    import os
    if os.path.exists("test_topic.txt"):
        os.remove("test_topic.txt")
    if os.path.exists("test_out.md"):
        os.remove("test_out.md")

@patch("topic_researcher.get_provider")
@patch("topic_researcher._jira_get")
@patch("topic_researcher._conf_get")
def test_contributor_logging_from_source_urls(mock_conf_get, mock_jira_get, mock_get_provider, caplog):
    # Setup mocks
    mock_llm = MagicMock()
    mock_llm.get_context_window.return_value = 8192
    mock_llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    mock_get_provider.return_value = mock_llm
    mock_llm.predict.return_value = MagicMock(text='YES')
    
    # Mock Jira issue API response
    mock_jira_get.return_value = {
        "key": "SOURCE-1",
        "fields": {
            "summary": "Source Issue",
            "issuetype": {"name": "Task"},
            "status": {"name": "Done"},
            "description": "Source Description"
        }
    }
    
    # Mock Confluence page API response
    mock_conf_get.return_value = {
        "title": "Context Page",
        "body": {"storage": {"value": "Context content"}}
    }
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # Mock other parts to avoid full loop
    researcher._generate_queries = MagicMock(return_value={"jql": None, "cql": None})
    researcher._formulate_document = MagicMock(return_value="Draft")
    researcher._agentic_debate_and_refine = MagicMock(return_value="Refined Draft")
    researcher._sanity_check_document = MagicMock(side_effect=lambda x, final_pass=False: x)
    
    caplog.set_level(logging.INFO)
    
    # Run with Jira source URL and Confluence context URL
    researcher.run(
        "https://test.atlassian.net/browse/SOURCE-1", 
        "test_out_urls.md", 
        max_iterations=1,
        context_sources=["https://test.atlassian.net/wiki/spaces/S/pages/555/Title"]
    )
    
    # Check logs
    assert "Tracked Jira contribution: SOURCE-1" in caplog.text
    assert "Tracked Confluence contribution: Context Page (555)" in caplog.text
    
    import os
    if os.path.exists("test_out_urls.md"):
        os.remove("test_out_urls.md")

@patch("topic_researcher.get_provider")
@patch("topic_researcher.jira_fetch_issues")
@patch("topic_researcher._conf_get")
def test_contributor_logging_from_fallback(mock_conf_get, mock_jira_fetch, mock_get_provider, caplog):
    # Setup mocks
    mock_llm = MagicMock()
    mock_llm.get_context_window.return_value = 8192
    mock_llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    mock_get_provider.return_value = mock_llm
    
    # LLM responses: 
    # 1. _generate_queries (returns a failed query)
    # 2. _fallback_url_research plan (for Jira)
    # 3. Search simulation for fallback 1
    # 4. _fallback_url_research plan (for Confluence)
    # 5. Search simulation for fallback 2
    # 6. _is_complete
    mock_llm.predict.side_effect = [
        MagicMock(text='{"jql": "INVALID", "cql": "INVALID", "search_queries": [], "llm_questions": []}'),
        MagicMock(text='{"search_terms": "test", "jira_keywords": "FALLBACK-1", "confluence_keywords": "", "reasoning": "testing"}'),
        MagicMock(text='Search simulation result'),
        MagicMock(text='{"search_terms": "test", "jira_keywords": "", "confluence_keywords": "Fallback Page", "reasoning": "testing"}'),
        MagicMock(text='Search simulation result'),
        MagicMock(text='YES')
    ]
    
    # Mock Jira failure then success on fallback
    def jira_fetch_side_effect(*args, **kwargs):
        if kwargs.get('jql') == "INVALID":
            raise Exception("Jira Error")
        mock_issue = MagicMock()
        mock_issue.key = "FALLBACK-1"
        mock_issue.summary = "Fallback Summary"
        mock_issue.description = "Fallback Desc"
        return [mock_issue]
    mock_jira_fetch.side_effect = jira_fetch_side_effect
    
    # Mock Confluence failure then success on fallback
    def conf_get_side_effect(*args, **kwargs):
        if "/rest/api/search" in args[2] and kwargs.get('params', {}).get('cql') == "INVALID":
            raise Exception("Conf Error")
        return {
            "results": [{
                "content": {
                    "id": "111",
                    "title": "Fallback Page",
                    "body": {"storage": {"value": "Fallback content"}}
                }
            }]
        }
    mock_conf_get.side_effect = conf_get_side_effect
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    researcher._formulate_document = MagicMock(return_value="Draft")
    researcher._agentic_debate_and_refine = MagicMock(return_value="Refined Draft")
    researcher._sanity_check_document = MagicMock(side_effect=lambda x, final_pass=False: x)
    
    with open("test_fallback.txt", "w") as f:
        f.write("Topic\nReqs")
        
    caplog.set_level(logging.INFO)
    researcher.run("test_fallback.txt", "test_out_fallback.md", max_iterations=1)
    
    assert "Tracked Jira contribution: FALLBACK-1" in caplog.text
    assert "Tracked Confluence contribution: Fallback Page (111)" in caplog.text
    
    import os
    if os.path.exists("test_fallback.txt"):
        os.remove("test_fallback.txt")
    if os.path.exists("test_out_fallback.md"):
        os.remove("test_out_fallback.md")
