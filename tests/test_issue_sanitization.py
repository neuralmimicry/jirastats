import pytest
from topic_researcher import TopicResearcher

def test_jql_sanitization_quoted_grouping():
    # Setup researcher with mock data
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai" # doesn't matter for sanitization
    )
    
    # Invalid JQL from the issue
    invalid_jql = 'text ~ "(\\"People and Culture\\" OR Performance OR Engagement)"'
    
    queries = {"jql": invalid_jql, "cql": "", "search_queries": [], "llm_questions": []}
    sanitized = researcher._sanitize_queries(queries)
    
    expected_part = '(text ~ "People and Culture" OR text ~ Performance OR text ~ Engagement)'
    assert expected_part in sanitized["jql"]
    assert "text ~ \"(" not in sanitized["jql"]

def test_cql_sanitization_quoted_grouping():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    invalid_cql = 'title ~ "(\\"Strategy\\" OR \\"Roadmap\\")"'
    queries = {"jql": "", "cql": invalid_cql, "search_queries": [], "llm_questions": []}
    sanitized = researcher._sanitize_queries(queries)
    
    expected = '(title ~ "Strategy" OR title ~ "Roadmap")'
    assert expected == sanitized["cql"]

def test_reserved_words_quoting():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    jql = 'text ~ AND OR summary ~ NULL'
    queries = {"jql": jql, "cql": "", "search_queries": [], "llm_questions": []}
    sanitized = researcher._sanitize_queries(queries)
    
    assert 'text ~ "AND"' in sanitized["jql"]
    assert 'summary ~ "NULL"' in sanitized["jql"]

def test_long_query_splitting():
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # Create a long query with many ORs
    long_jql = " OR ".join([f'text ~ "term{i}"' for i in range(100)])
    
    split_queries = researcher._split_query(long_jql, max_len=500)
    assert len(split_queries) > 1
    for q in split_queries:
        assert len(q) <= 600 # allow some buffer for splitting
