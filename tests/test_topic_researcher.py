import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher, MockSearchEngine

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    # Mock context window and estimate_tokens
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    # Mock for _generate_queries
    llm.predict.side_effect = [
        # NEW: Identify subjects
        MagicMock(text="NONE"),
        # NEW: Extract names
        MagicMock(text="NONE"),
        # First iteration generate queries
        MagicMock(text='{"jql": "project = TEST", "cql": "text ~ TEST", "search_queries": ["test topic"], "llm_questions": ["What is test?"]}'),
        # Mock search result
        MagicMock(text='Snippet 1: Test info. Snippet 2: More test info.'),
        # Relevance check (Snippet)
        MagicMock(text='YES'),
        # LLM question answer
        MagicMock(text='Answer to what is test.'),
        # NEW: Follow-up query generation
        MagicMock(text='{"jql": "", "cql": ""}'),
        # Formulate document
        MagicMock(text='# Test Document\n\nThis is a test document in British English.'),
        # Critic feedback
        MagicMock(text='Improvement: add implementation details.'),
        # Editor refinement
        MagicMock(text='# Test Document\n\nThis is a refined test document in British English.'),
        # Is complete?
        MagicMock(text='NO. Missing details about implementation.'),
        
        # Second iteration generate queries
        MagicMock(text='{"jql": "", "cql": "", "search_queries": [], "llm_questions": ["detail implementation"]}'),
        # LLM question answer
        MagicMock(text='Implementation details...'),
        # NEW: Follow-up query generation
        MagicMock(text='{"jql": "", "cql": ""}'),
        # Formulate document
        MagicMock(text='# Test Document\n\nThis is a test document with implementation details in British English.'),
        # Critic feedback
        MagicMock(text='Looks good.'),
        # Editor refinement
        MagicMock(text='# Test Document\n\nThis is a test document with implementation details in British English.'),
        # Is complete?
        MagicMock(text='YES'),
        # Final Sanity Check (Triggered by final_pass=True)
        MagicMock(text='# Test Document\n\nThis is a test document with implementation details in British English. Polished.')
    ]
    return llm

@patch('topic_researcher.get_provider')
@patch('topic_researcher.jira_fetch_issues')
@patch('topic_researcher._conf_get')
def test_topic_researcher_run(mock_conf_get, mock_jira_fetch, mock_get_provider, mock_llm, tmp_path):
    mock_get_provider.return_value = mock_llm
    
    issue = MagicMock()
    issue.key = "TEST-1"
    issue.summary = "Test Summary"
    issue.description = "Test Desc"
    issue.status = "Open"
    issue.issuetype = "Task"
    issue.assignee = "User A"
    issue.reporter = "User B"
    issue.comment_count = 0
    issue.commenters = []
    issue.parent_key = None
    issue.updated = None
    issue.created = None
    mock_jira_fetch.return_value = [issue]
    
    mock_conf_get.return_value = {"results": [{"content": {"id": "123", "title": "Test Page", "body": {"storage": {"value": "Test Body"}}}}] }

    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    source_file = tmp_path / "source.txt"
    source_file.write_text("Topic: AI in Telco\nRequirements: Must cover British market and use British English.")
    
    output_file = tmp_path / "output.md"
    
    researcher.run(str(source_file), str(output_file), max_iterations=2)
    
    assert output_file.exists()
    content = output_file.read_text()
    assert "# Test Document" in content
    assert "implementation details" in content
    
    # Verify mock calls
    assert mock_llm.predict.call_count >= 5
    mock_jira_fetch.assert_called()
    mock_conf_get.assert_called()

def test_mock_search_engine():
    mock_llm = MagicMock()
    search_engine = MockSearchEngine(mock_llm)
    mock_llm.predict.return_value = MagicMock(text="Search result content")
    
    results = search_engine.search("test query")
    assert len(results) == 1
    assert "Search result for: test query" in results[0]["title"]
    assert "Search result content" in results[0]["snippet"]
