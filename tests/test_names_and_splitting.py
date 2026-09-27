import pytest
import json
import os
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher, RESEARCH_CACHE_ROOT

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    return llm

@pytest.fixture
def researcher(mock_llm, tmp_path):
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        with patch("topic_researcher.GoogleSearchEngine.verify", return_value=(True, "Success")):
            # Redirect cache to tmp_path
            with patch("topic_researcher.RESEARCH_CACHE_ROOT", str(tmp_path)):
                r = TopicResearcher(
                    jira_base_url="https://test.atlassian.net",
                    jira_auth=("user", "token"),
                    llm_provider="openai"
                )
                r.subjects = ["Valid Subject"]
                r.name_cache = {"Known Colleague"}
                return r

def test_name_cache_persistence(mock_llm, tmp_path):
    cache_dir = tmp_path / ".research_cache"
    cache_dir.mkdir()
    
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        with patch("topic_researcher.GoogleSearchEngine.verify", return_value=(True, "Success")):
            with patch("topic_researcher.RESEARCH_CACHE_ROOT", str(cache_dir)):
                r = TopicResearcher(
                    jira_base_url="https://test.atlassian.net",
                    jira_auth=("user", "token"),
                    llm_provider="openai"
                )
                r._update_name_cache(["New Person"])
                
                assert "New Person" in r.name_cache
                assert os.path.exists(os.path.join(str(cache_dir), "names.json"))
                
                # Create a new researcher and see if it loads
                r2 = TopicResearcher(
                    jira_base_url="https://test.atlassian.net",
                    jira_auth=("user", "token"),
                    llm_provider="openai"
                )
                assert "New Person" in r2.name_cache

def test_query_name_filtering(researcher):
    queries = {
        "jql": 'assignee = "Valid Subject" OR reporter = "Hallucinated Name"',
        "cql": 'creator = "Known Colleague" OR contributor = "Ghost Person"'
    }
    
    sanitized = researcher._sanitize_queries(queries)
    
    # "Hallucinated Name" and "Ghost Person" should be filtered out
    assert 'assignee = "Valid Subject"' in sanitized["jql"]
    assert 'reporter = "Hallucinated Name"' not in sanitized["jql"]
    assert 'creator = "Known Colleague"' in sanitized["cql"]
    assert 'contributor = "Ghost Person"' not in sanitized["cql"]
    
    # Cleanup should handle leftover ORs
    assert 'OR' not in sanitized["jql"]
    assert 'OR' not in sanitized["cql"]

def test_query_name_filtering_in_clause(researcher):
    queries = {
        "jql": 'assignee IN ("Valid Subject", "Hallucinated Name", "Known Colleague")'
    }
    
    sanitized = researcher._sanitize_queries(queries)
    
    assert 'Valid Subject' in sanitized["jql"]
    assert 'Known Colleague' in sanitized["jql"]
    assert 'Hallucinated Name' not in sanitized["jql"]
    # Should still be an IN clause if more than 1
    assert 'IN ("Valid Subject", "Known Colleague")' in sanitized["jql"]

def test_recursive_splitting(researcher):
    # Create a very long query with many ORs
    long_query = " OR ".join([f'project = "PROJ-{i}"' for i in range(50)])
    
    # Split with a small max_len
    chunks = researcher._split_query(long_query, max_len=100)
    
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 100
        # Check that it's still a valid-looking JQL part
        assert "project =" in chunk

if __name__ == "__main__":
    pytest.main([__file__])
