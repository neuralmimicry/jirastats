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
        {"title": "Quarter plan Q1-2026 and FY24-10 should not match", "extract": ""},
    ]
    keys = dh._extract_issue_keys_from_pages(pages)  # noqa: SLF001
    assert keys == ["ABC-123", "XYZ-9", "PRJ-1"]


def test_validate_epic_keys_filters_invalid(monkeypatch):
    # Simulate that only PRJ-1 and ABC-2 exist as Epics; bogus keys should be filtered out
    jira = Mock()

    # search_issues will be called twice in _probe_jira (for epics by keywords) and in validation batch
    # We'll simulate validation call by returning only valid epics when JQL contains "key in"
    def fake_search_issues(jql, maxResults=50):
        class I:  # simple issue with key attr
            def __init__(self, k):
                self.key = k

        if "key in" in jql and "issuetype = Epic" in jql:
            return [I("PRJ-1"), I("ABC-2")]
        # For epic discovery by keywords, return empty to rely on page extraction
        return []

    jira.search_issues = Mock(side_effect=fake_search_issues)
    jira.projects = Mock(return_value=[])
    jira.fields = Mock(return_value=[])
    # Pages include mix of valid and bogus keys
    pages = [
        {"title": "Roadmap PRJ-1", "extract": "See ABC-2"},
        {"title": "Q1-2026", "extract": "FY24-10"},
    ]

    # Patch _probe_confluence to return our pages and spaces
    monkeypatch.setattr(dh, "_probe_confluence", lambda base, auth, cfg: ([], pages))

    # Use a unique base URL to avoid sharing the discovery cache with other tests
    res = dh.discover_hierarchy(jira, "https://unique-test.atlassian.net", ("u", "p"), {"discovery": {"enabled": True}})
    # Only validated epics should remain
    assert sorted(res.epics) == ["ABC-2", "PRJ-1"]


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
    # Provide issue types discovery via client
    jira.issue_types.return_value = [NS(name="Epic"), NS(name="Story"), NS(name="Task"), NS(name="Bug"), NS(name="Sub-task")]

    res = dh.discover_hierarchy(jira, "https://example.atlassian.net", ("u", "p"), {"discovery": {"enabled": True}})
    assert "start_date" in res.fields and res.projects == ["PRJ"] and "PRJ-1" in res.epics
    # Validate issue types discovery and ranking
    assert res.issue_types, "Expected discovered issue types"
    assert res.issue_ranking, "Expected inferred issue ranking"
    # Epic should rank ahead of Story/Task/Sub-task if present
    if "Epic" in res.issue_ranking and "Sub-task" in res.issue_ranking:
        assert res.issue_ranking["Epic"] < res.issue_ranking["Sub-task"]
