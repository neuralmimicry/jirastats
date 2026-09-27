import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher
from discover_hierarchy import discover_hierarchy

def test_topic_researcher_disable_jira():
    with patch('topic_researcher._jira_get') as mock_jira_get:
        researcher = TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "pass"),
            llm_provider="ollama",
            disable_jira=True
        )
        researcher._fetch_available_containers()
        mock_jira_get.assert_not_called()

def test_topic_researcher_disable_confluence():
    with patch('topic_researcher._conf_get') as mock_conf_get:
        researcher = TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "pass"),
            llm_provider="ollama",
            disable_confluence=True
        )
        researcher._fetch_available_containers()
        mock_conf_get.assert_not_called()

def test_discover_hierarchy_disable_all():
    mock_jira_client = MagicMock()
    with patch('discover_hierarchy._probe_confluence') as mock_probe_conf, \
         patch('discover_hierarchy._probe_jira') as mock_probe_jira:
        
        config = {"discovery": {"enabled": True}}
        discover_hierarchy(
            mock_jira_client,
            "https://test.atlassian.net",
            ("user", "pass"),
            config,
            disable_jira=True,
            disable_confluence=True
        )
        
        mock_probe_conf.assert_not_called()
        mock_probe_jira.assert_not_called()

def test_topic_researcher_execute_queries_respects_flags():
    with patch('topic_researcher._jira_get') as mock_jira_get, \
         patch('topic_researcher._conf_get') as mock_conf_get:
        
        researcher = TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "pass"),
            llm_provider="ollama",
            disable_jira=True,
            disable_confluence=True
        )
        
        queries = {
            "jql": "project = TEST",
            "cql": "text ~ 'test'",
            "search_queries": []
        }
        
        results = researcher._execute_queries(queries)
        
        assert results["jira_issues"] == []
        assert results["confluence_pages"] == []
        mock_jira_get.assert_not_called()
        mock_conf_get.assert_not_called()
