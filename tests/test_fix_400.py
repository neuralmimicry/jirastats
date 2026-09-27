import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@pytest.fixture(autouse=True)
def mock_research_cache(tmp_path):
    with patch("topic_researcher.RESEARCH_CACHE_ROOT", str(tmp_path)):
        yield str(tmp_path)

@pytest.fixture(autouse=True)
def mock_llm_provider():
    with patch("topic_researcher.get_provider") as mock_get:
        mock_llm = MagicMock()
        mock_llm.get_context_window.return_value = 8192
        mock_llm.estimate_tokens.side_effect = lambda x: len(x) // 4
        mock_get.return_value = mock_llm
        yield mock_llm

def test_sanitize_quoted_fields():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = {
        "jql": '"project" = "PROJ" AND "assignee" = "Das"',
        "cql": '"space" = "SPACE" AND "creator" = "Das"'
    }
    sanitized = researcher._sanitize_queries(queries)
    
    assert sanitized["jql"] == 'project = "PROJ" AND assignee = "Das"'
    assert sanitized["cql"] == 'space = "SPACE" AND creator = "Das"'

def test_sanitize_parens_in_text_search():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = {
        "jql": 'text ~ "Payment (Basic)" AND (status = "Open")',
        "cql": 'text ~ "Payment (Basic)" OR (title ~ "Manual")'
    }
    sanitized = researcher._sanitize_queries(queries)
    
    # Note: sanitize_queries also balances parens at the end, 
    # but let's check the core escaping logic.
    assert 'text ~ "Payment \\(Basic\\)"' in sanitized["jql"]
    assert 'text ~ "Payment \\(Basic\\)"' in sanitized["cql"]

def test_splitting_threshold():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # Create a query > 800 chars
    long_query = " OR ".join([f'text ~ "term{i}"' for i in range(100)])
    assert len(long_query) > 800
    
    split = researcher._split_query(long_query)
    assert len(split) > 1
    for q in split:
        assert len(q) <= 800

def test_sanitize_space_not_empty():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = {
        "jql": "project = TEST",
        "cql": 'space is not EMPTY AND (creator = "Jozsef" OR contributor = "Jozsef")'
    }
    sanitized = researcher._sanitize_queries(queries)
    assert "space is not EMPTY" not in sanitized["cql"]
    assert sanitized["cql"] == '(creator = "Jozsef" OR contributor = "Jozsef")'

def test_sanitize_backwards_in():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = {
        "jql": "project = TEST",
        "cql": '"Neil Venter" IN (creator, contributor)'
    }
    sanitized = researcher._sanitize_queries(queries)
    assert 'creator = "Neil Venter"' in sanitized["cql"]
    assert 'contributor = "Neil Venter"' in sanitized["cql"]
    assert 'OR' in sanitized["cql"]

def test_sanitize_now_hallucination():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = {
        "jql": 'updated >= "now(\"-6M\")" AND status = Open',
        "cql": 'lastmodified >= "now(\"-6M\")" AND space = TEST'
    }
    sanitized = researcher._sanitize_queries(queries)
    
    # JQL should be converted to weeks
    assert 'updated >= -24w' in sanitized["jql"]
    
    # CQL should be converted to absolute date
    # Format: lastmodified >= "YYYY-MM-DD"
    assert 'lastmodified >= "' in sanitized["cql"]
    import re
    assert re.search(r'lastmodified >= "\d{4}-\d{2}-\d{2}"', sanitized["cql"])

@patch('topic_researcher._conf_get')
@patch('topic_researcher.jira_fetch_issues')
def test_report_on_lookup_error(mock_jira_fetch, mock_conf_get, mock_llm_provider, tmp_path):
    # Simulate 400 error for Confluence
    mock_conf_get.side_effect = Exception("400 Client Error: Bad Request")
    mock_jira_fetch.return_value = []
    
    researcher = TopicResearcher(jira_base_url="https://test.atlassian.net", jira_auth=("u", "p"), llm_provider="openai")
    
    def llm_side_effect(messages, **kwargs):
        system = kwargs.get('system', '')
        if "Identify research subjects" in system:
            return MagicMock(text="[]")
        if "Extract any other person names" in system:
            return MagicMock(text="[]")
        if "Identify specific research queries" in system:
            return MagicMock(text='{"jql": "project=TEST", "cql": "space=TEST", "search_queries": [], "llm_questions": []}')
        if "technical document" in system and "sanity check" in system:
            return MagicMock(text="Due to a technical lookup error, certain information could not be verified.")
        if "Critical Reviewer" in system:
            return MagicMock(text="Looks good.")
        if "Professional British Editor" in system:
            return MagicMock(text="Due to a technical lookup error, certain information could not be verified.")
        if "formulate or update a comprehensive document" in system:
            return MagicMock(text="Due to a technical lookup error, certain information could not be verified.")
        if "meets all requirements" in system:
            return MagicMock(text="YES")
        if "syntactically invalid" in system:
            return MagicMock(text='{"jql": "project=TEST", "cql": "space=TEST"}')
        return MagicMock(text="Default Response")

    mock_llm_provider.predict.side_effect = llm_side_effect
    
    source_file = tmp_path / "source.txt"
    source_file.write_text("Topic: Test\nRequirements: Test")
    
    output_file = tmp_path / "report.md"
    
    researcher.run(str(source_file), str(output_file), max_iterations=1)
    
    assert output_file.exists()
    content = output_file.read_text()
    assert "could not be verified" in content
