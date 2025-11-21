from types import SimpleNamespace as NS

import main as m


def test_project_access_probe_filters_projects(monkeypatch, tmp_path):
    # Isolate any caches
    monkeypatch.chdir(tmp_path)

    # Ensure probe is enabled
    monkeypatch.setattr(m, "PROBE_ACCESSIBLE_PROJECTS", True, raising=False)

    # Avoid interactive creds and real network
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))

    # Fake Jira client: PRJ returns 1 issue, ABC returns 0
    class FakeJira:
        def search_issues(self, jql, startAt=0, maxResults=50, expand=None):
            if jql.startswith("project = PRJ"):
                return [NS(key="PRJ-1")]
            if jql.startswith("project = ABC"):
                return []
            return []

    fake_jira = FakeJira()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery returns two projects
    discovery_result = NS(projects=["PRJ", "ABC"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)

    # Capture projects passed to build_refined_jql and return a benign refined jql
    seen_projects = {}

    def fake_build_refined_jql(base, res):
        # Record the projects used for refined jql
        seen_projects["projects"] = list(getattr(res, "projects", []) or [])
        return "project in (" + ",".join(seen_projects["projects"]) + ") ORDER BY Rank" if seen_projects.get("projects") else base

    monkeypatch.setattr(m, "build_refined_jql", fake_build_refined_jql)

    # Avoid deep processing
    monkeypatch.setattr(m, "fetch_issues", lambda *_a, **_k: [])
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    # Act
    m.main()

    # Assert: only PRJ should remain after probe
    assert seen_projects.get("projects") == ["PRJ"], "Expected project accessibility probe to filter out ABC"
    # Diagnostic should mention 1 of 2 accessible
    assert any("Project accessibility probe: 1 of 2 projects accessible" in line for line in printed)


def test_project_access_probe_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Disable the probe via env/flag
    monkeypatch.setattr(m, "PROBE_ACCESSIBLE_PROJECTS", False, raising=False)
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))

    class FakeJira:
        def search_issues(self, jql, startAt=0, maxResults=50, expand=None):
            # Even though ABC would be inaccessible, disabling probe should keep both
            if jql.startswith("project = PRJ"):
                return [NS(key="PRJ-1")]
            if jql.startswith("project = ABC"):
                return []
            return []

    fake_jira = FakeJira()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    discovery_result = NS(projects=["PRJ", "ABC"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)

    seen_projects = {}

    def fake_build_refined_jql(base, res):
        seen_projects["projects"] = list(getattr(res, "projects", []) or [])
        return base

    monkeypatch.setattr(m, "build_refined_jql", fake_build_refined_jql)
    # Avoid network and processing
    monkeypatch.setattr(m, "fetch_issues", lambda *_a, **_k: [])
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    # Act
    m.main()

    # Assert no filtering occurred: both projects remain
    assert seen_projects.get("projects") == ["PRJ", "ABC"], "Expected no filtering when probe is disabled"
    # No accessibility message should be printed
    assert not any("Project accessibility probe:" in line for line in printed)
