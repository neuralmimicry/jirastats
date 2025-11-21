from types import SimpleNamespace as NS

import main as m


def test_iterate_per_project_builds_per_project_jql(monkeypatch, tmp_path):
    # Enable per-project iteration
    monkeypatch.setattr(m, "ITERATE_PER_PROJECT", True, raising=False)

    # Avoid interactive credential input and real Jira client
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    # Base JQL and refined JQL that includes a large project in () clause
    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    discovery_result = NS(projects=["PRJ", "ABC"], epics=["PRJ-1"], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)

    refined = "project in (PRJ,ABC) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    # Capture per-project JQLs executed via _fetch_sanitized
    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        # Return some sentinel issues per project to simulate results
        if jql.startswith("project = PRJ"):
            return [NS(key="PRJ-10"), NS(key="PRJ-11")]
        if jql.startswith("project = ABC"):
            return [NS(key="ABC-1")]
        return []

    monkeypatch.setattr(m, "_fetch_sanitized", fake_fetch)

    # Avoid downstream processing to keep test focused on fetch path
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: issues)
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    # Run
    m.main()

    # Assert per-project queries were executed separately and preserved ORDER BY per sub-query
    assert any(c.startswith("project = PRJ") and c.endswith("ORDER BY Rank") for c in calls), "Expected per-project JQL for PRJ"
    assert any(c.startswith("project = ABC") and c.endswith("ORDER BY Rank") for c in calls), "Expected per-project JQL for ABC"
    # Should not re-use the combined project in (...) JQL when iterating
    assert refined not in calls, "Combined project-in JQL should not be used when iterating per project"


def test_auto_per_project_retry_when_disabled(monkeypatch, tmp_path):
    # Ensure ITERATE_PER_PROJECT is disabled to exercise auto-retry path
    import main as m
    monkeypatch.setattr(m, "ITERATE_PER_PROJECT", False, raising=False)

    # Temp cwd to isolate any cache usage/logs
    monkeypatch.chdir(tmp_path)

    # Avoid interactive creds and network
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery returns multiple projects and a fields map; no need for epics for this path
    discovery_result = NS(projects=["PRJ", "ABC"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)

    refined = "project in (PRJ,ABC) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    # fetch_issues is called via _fetch_sanitized; make the combined refined return empty,
    # but per-project jqls return some results
    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        if jql.strip() == refined:
            return []  # trigger auto per-project retry
        if jql.startswith("project = PRJ"):
            return [NS(key="PRJ-1"), NS(key="PRJ-2")]
        if jql.startswith("project = ABC"):
            return [NS(key="ABC-9")]
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)
    # Downstream no-ops
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: issues)
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    # Run
    m.main()

    # Assert that per-project queries were attempted even though the flag is disabled
    assert any(c.startswith("project = PRJ") for c in calls), "Expected auto per-project JQL for PRJ"
    assert any(c.startswith("project = ABC") for c in calls), "Expected auto per-project JQL for ABC"
    # Diagnostic should mention Auto per-project retry
    assert any("Auto per-project retry" in line for line in printed), "Expected auto per-project retry diagnostic"
