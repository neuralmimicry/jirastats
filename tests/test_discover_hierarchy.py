from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import discover_hierarchy as dh


def test_keywords_cql_builds_query():
    cql = dh._keywords_cql(["CTO", "DNP"])  # noqa: SLF001 accessing internal for test
    assert 'title ~ "CTO"' in cql and 'text ~ "DNP"' in cql


def test_extract_issue_keys_from_pages():
    pages = [
        {"title": "Work on ABC-123 and XYZ-9", "extract": ""},
        {"title": "No keys", "extract": "See PRJ-1 details"},
    ]
    keys = dh._extract_issue_keys_from_pages(pages)  # noqa: SLF001
    assert keys == ["ABC-123", "XYZ-9", "PRJ-1"]


def test_build_refined_jql_combines_filters():
    res = dh.DiscoveryResult(projects=["PRJ", "ABC"], epics=["PRJ-1", "ABC-2"])
    out = dh.build_refined_jql("issuetype in (Story, Bug)", res)
    assert "project in (PRJ,ABC)" in out
    assert "key in (PRJ-1,ABC-2)" in out


def test_load_discovery_config_env_overrides(monkeypatch):
    cfg = {"discovery": {"enabled": True, "keywords": ["A"], "cache_ttl_minutes": 1}}
    monkeypatch.setenv("DISCOVERY_KEYWORDS", "X,Y")
    dc = dh.load_discovery_config(cfg)
    assert dc.enabled is True and dc.keywords == ["X", "Y"]


@patch("discover_hierarchy.requests.get")
def test_discover_hierarchy_smoke(requests_get):
    # Mock Confluence response
    requests_get.return_value = Mock(
        **{
            "raise_for_status.return_value": None,
            "json.return_value": {"results": []},
        }
    )

    # Mock Jira client
    jira = Mock()
    jira.projects.return_value = [NS(name="CTO Projects", key="PRJ")]
    jira.search_issues.return_value = [NS(key="PRJ-1"), NS(key="PRJ-2")]
    jira.fields.return_value = [
        {"id": "customfield_1", "name": "Start date"},
        {"id": "customfield_2", "name": "End date"},
        {"id": "duedate", "name": "Due date"},
    ]

    res = dh.discover_hierarchy(jira, "https://example.atlassian.net", ("u", "p"), {"discovery": {"enabled": True}})
    assert "start_date" in res.fields and res.projects == ["PRJ"] and "PRJ-1" in res.epics
