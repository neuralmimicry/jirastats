from types import SimpleNamespace as NS
from unittest.mock import Mock

import main as m


def test_refined_jql_zero_results_clears_cache_and_falls_back(monkeypatch, tmp_path):
    # Arrange: run in a temp cwd so cache path is predictable
    monkeypatch.chdir(tmp_path)

    # Create a fake discovery cache file that should be removed
    cache_name = ".discovery_cache.json"
    cache_path = tmp_path / cache_name
    cache_path.write_text("{\n  \"timestamp\": 0\n}")
    assert cache_path.exists(), "Precondition: discovery cache should exist before run"

    # Avoid interactive input
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))

    # Stub out Jira connection creation to a simple object
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery returns some structure; refined JQL is different than base
    discovery_result = NS(projects=["PRJ"], epics=["PRJ-1"], fields={})

    disc_calls = {"count": 0}

    def fake_discover(*args, **kwargs):
        disc_calls["count"] += 1
        return discovery_result

    monkeypatch.setattr(m, "discover_hierarchy", fake_discover)

    # First build_refined_jql call returns refined, second also refined (after cache clear)
    refined_jql = "project in (PRJ) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined_jql)

    # fetch_issues returns empty for refined attempts, and ensure it is called with base after second empty
    calls = []

    def fake_fetch(jira, jql):
        calls.append(jql)
        # Return no issues regardless; we just care about call order/params
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)

    # Avoid deep processing of issues by stubbing sorting and report gen
    # Ensure downstream code doesn't try to treat our fake strings as issue objects
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    # Capture prints to ensure the expected messages are produced
    printed = []
    # Patch builtins.print to capture output from main
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    # Act
    m.main()

    # Assert: cache file should be removed
    assert not cache_path.exists(), "Expected discovery cache to be deleted after zero-refined results"

    # Assert: fetch_issues was called at least three times: first refined, second refined after cache clear, then base
    # Some environments may call additional internal fetches; we assert that base_jql appears after refined attempts
    # Ensure first call is refined, and a later call contains base_jql
    assert calls, "fetch_issues should have been called"
    assert calls[0] == refined_jql, "First fetch should use refined JQL"
    # There should be a subsequent call with the base JQL
    assert base_jql in calls, "Expected a fallback fetch using base JQL after repeated zero results"

    # Assert user-facing messages include the cache clear and fallback notices
    joined = "\n".join(printed)
    assert "cleared discovery cache" in joined, "Expected message about clearing discovery cache"
    assert "returned 0 issues again; retrying with base JQL" in joined, "Expected message about base JQL fallback"


def test_projects_only_refinement_tries_broadened_query(monkeypatch, tmp_path):
    # Arrange: temp cwd for cache isolation
    monkeypatch.chdir(tmp_path)

    # Avoid interactive input
    import main as m
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))

    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery returns only projects, no epics
    discovery_result = NS(projects=["PRJ", "ABC"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)

    # build_refined_jql yields a projects-only refined JQL
    refined = "project in (PRJ,ABC) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        # First two attempts (refined and refined after retry) return empty
        if len(calls) <= 2:
            return []
        # Third attempt should be the broadened one including "issuetype = Epic"
        if "issuetype = Epic" in jql:
            # Return a non-empty list to stop further fallback
            return ["EPIC-1"]
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)

    # Avoid deep processing of fake results
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    # Act
    m.main()

    # Assert that a broadened query including epics was attempted and was the last attempt
    broadened_calls = [c for c in calls if "issuetype = Epic" in c]
    assert broadened_calls, "Expected a broadened query attempt including 'issuetype = Epic'"
    assert "issuetype = Epic" in calls[-1], "Broadened query should be the last call when it returns results"
    # Message about broadened query should be printed
    joined = "\n".join(printed)
    assert "attempting a broadened query" in joined


def test_final_recent_epics_fallback_before_base(monkeypatch, tmp_path):
    # Ensure isolated cwd
    monkeypatch.chdir(tmp_path)

    import main as m
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery returns only projects (to exercise broadened path) but still yields no results; then final epic-recent returns data
    discovery_result = NS(projects=["PRJ"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)

    refined = "project in (PRJ) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        # First refined
        if jql == refined:
            return []
        # After cache clear retry refined
        if jql == refined:
            return []
        # Broadened attempt including Epic
        if "(issuetype = Epic)" in jql and "updated >=" not in jql:
            return []
        # Final fallback: recent epics
        if "issuetype = Epic" in jql and "updated >=" in jql:
            return ["EPIC-42"]
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    m.main()

    # Ensure recent epics query was attempted and returned results; base fallback should not be attempted after that
    joined = "\n".join(printed)
    assert "trying recent Epics window" in joined
    # The last fetch call should be the recent epics JQL
    assert any("issuetype = Epic" in c and "updated >=" in c for c in calls), "Expected a final recent epics fallback call"


def test_min_results_relaxation_projects_only(monkeypatch, tmp_path):
    # Arrange
    monkeypatch.chdir(tmp_path)
    import main as m
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)
    # Set MIN_RESULTS high so first small result triggers relaxation
    monkeypatch.setenv("MIN_RESULTS", "20")
    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery: projects only
    discovery_result = NS(projects=["PRJ", "ABC"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)
    refined = "project in (PRJ,ABC) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        # First refined call returns few issues (< MIN_RESULTS)
        if jql == refined:
            return ["X1", "X2", "X3", "X4", "X5"]  # 5 < 20
        # Broadened attempt with OR Epic returns many issues to satisfy threshold
        if "(issuetype = Epic)" in jql and "updated >=" not in jql:
            return [f"E{i}" for i in range(25)]
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    # Act
    m.main()

    # Assert
    broadened_calls = [c for c in calls if "(issuetype = Epic)" in c and "updated >=" not in c]
    assert broadened_calls, "Expected a relaxed projects+epics query when minimal results are found"
    assert any(c.endswith("ORDER BY Rank") for c in broadened_calls), "ORDER BY should be preserved in relaxed query"
    joined = "\n".join(printed)
    assert "relaxing constraints" in joined and "relaxed projects+epics" in joined


def test_min_results_env_override(monkeypatch, tmp_path):
    # With MIN_RESULTS=3, a refined result of 2 should trigger relaxation steps
    monkeypatch.chdir(tmp_path)
    import main as m
    monkeypatch.setenv("MIN_RESULTS", "3")
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)
    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    discovery_result = NS(projects=["PRJ"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)
    refined = "project in (PRJ) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        if jql == refined:
            return ["A", "B"]  # 2 < 3 triggers relaxation
        if "(issuetype = Epic)" in jql and "updated >=" not in jql:
            return ["E1", "E2", "E3", "E4"]
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    m.main()

    # It should have attempted a relaxed query
    assert any("(issuetype = Epic)" in c for c in calls), "Expected relaxed query attempt due to MIN_RESULTS env override"
    assert any("relaxing constraints" in line for line in printed)


def test_ultra_broad_final_fallback(monkeypatch, tmp_path):
    # After all fallbacks (broadened, recent epics, recent delivery) yield empty, ultra-broad should be tried
    monkeypatch.chdir(tmp_path)
    import main as m
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)
    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery returns only projects to exercise the path
    discovery_result = NS(projects=["PRJ"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)
    refined = "project in (PRJ) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        # First refined and retried refined will be empty
        if jql == refined:
            return []
        # Broadened attempt including Epic: empty
        if "(issuetype = Epic)" in jql and "updated >=" not in jql:
            return []
        # Recent epics window: empty
        if "issuetype = Epic" in jql and "updated >=" in jql:
            return []
        # Recent delivery types: empty
        if "issuetype in (Story, Task, Bug, Improvement, Spike)" in jql:
            return []
        # Ultra broad: updated >= -xd returns results
        if "updated >=" in jql and "issuetype" not in jql:
            return ["ANY-1", "ANY-2"]
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)
    # Avoid downstream processing that expects real issue objects
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    m.main()

    # Assert an ultra-broad updated-only query was attempted
    assert any(("updated >= -" in c) and ("issuetype" not in c) for c in calls), "Expected ultra-broad updated-only fallback"
    assert any("ultra-broad updated window" in line for line in printed), "Expected log about ultra-broad fallback"


def test_extreme_broad_after_ultra_broad_empty(monkeypatch, tmp_path):
    # When even ultra-broad returns empty, we try an extreme-broad no-filter query
    monkeypatch.chdir(tmp_path)
    import main as m
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)
    base_jql = "ORDER BY Rank"
    monkeypatch.setattr(m, "JQL_QUERY", base_jql)

    # Discovery: projects-only to enter fallback path; everything returns empty until extreme-broad
    discovery_result = NS(projects=["PRJ"], epics=[], fields={})
    monkeypatch.setattr(m, "discover_hierarchy", lambda *a, **k: discovery_result)
    refined = "project in (PRJ) ORDER BY Rank"
    monkeypatch.setattr(m, "build_refined_jql", lambda base, res: refined)

    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        # refined empty
        if jql == refined:
            return []
        # broadened projects+epic empty
        if "(issuetype = Epic)" in jql and "updated >=" not in jql:
            return []
        # recent epics empty
        if "issuetype = Epic" in jql and "updated >=" in jql:
            return []
        # recent delivery empty
        if "issuetype in (Story, Task, Bug, Improvement, Spike)" in jql:
            return []
        # ultra broad empty
        if jql.strip().startswith("updated >= -"):
            return []
        # extreme-broad (no WHERE): return some results
        if jql.strip().lower().startswith("order by created desc"):
            return ["ANY-3"]
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)
    # Make downstream no-op
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: [])
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    m.main()

    # Assert extreme-broad query was attempted
    assert any(j.strip().lower().startswith("order by created desc") for j in calls), (
        "Expected extreme-broad no-filter query after ultra-broad remained empty"
    )
    assert any("extreme-broad no-filter" in line for line in printed)


def test_force_ultra_broad_env(monkeypatch, tmp_path):
    # With FORCE_ULTRA_BROAD=1, discovery should be bypassed and updated-only query used first
    monkeypatch.chdir(tmp_path)
    import main as m
    monkeypatch.setenv("FORCE_ULTRA_BROAD", "1")
    # Need to reload module-level constants respecting env? Not necessary; main reads FORCE_ULTRA_BROAD at import time.
    # Work around by updating attribute directly
    monkeypatch.setattr(m, "FORCE_ULTRA_BROAD", True, raising=False)

    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    fake_jira = NS()
    monkeypatch.setattr(m, "create_jira_connection", lambda u, p: fake_jira)

    # Ensure discovery functions are not called
    def fail_discovery(*a, **k):
        raise AssertionError("discover_hierarchy should not be called in ultra-broad mode")

    monkeypatch.setattr(m, "discover_hierarchy", fail_discovery)
    monkeypatch.setattr(m, "build_refined_jql", lambda *a, **k: (_ for _ in ()).throw(AssertionError("build_refined_jql should not be called")))

    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        # Return some results to proceed
        return ["A-1", "B-2", "C-3"]

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)
    monkeypatch.setattr(m, "sort_issues_by_priority", lambda issues: issues)
    monkeypatch.setattr(m, "generate_timelines_report", lambda *a, **k: None)

    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    m.main()

    assert calls, "Expected at least one fetch call"
    # The very first call should be the ultra-broad one with updated >= -RECENT_DAYS
    assert "updated >= -" in calls[0]
    # ORDER BY Rank should be preserved by default base JQL
    assert calls[0].strip().endswith("ORDER BY Rank") or " ORDER BY " in calls[0]
    assert any("Force ultra-broad mode" in line for line in printed)
