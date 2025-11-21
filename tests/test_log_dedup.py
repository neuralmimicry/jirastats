from types import SimpleNamespace as NS

import main as m


def test_rank_auto_retry_log_prints_once_per_jql(monkeypatch):
    # Ensure AVOID_RANK_ORDER is disabled so auto-retry path is used
    monkeypatch.setattr(m, "AVOID_RANK_ORDER", False, raising=False)
    # Reset any prior dedup state from other tests
    monkeypatch.setattr(m, "_RANK_AUTO_RETRY_PRINTED", set(), raising=False)

    # Make fetch_issues always return empty to trigger the auto-retry path
    calls = []

    def fake_fetch(_jira, jql):
        calls.append(jql)
        return []

    monkeypatch.setattr(m, "fetch_issues", fake_fetch)

    # Capture prints
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))

    jira = NS()
    jql = "project = PRJ ORDER BY Rank"

    # First call should print the auto-retry notice once
    m._fetch_sanitized(jira, jql)
    first_notice_count = sum("auto-retrying without Rank ordering" in line for line in printed)
    assert first_notice_count == 1, "Expected a single auto-retry notice on first invocation"

    # Second call with the same JQL should not print the notice again due to de-duplication
    m._fetch_sanitized(jira, jql)
    second_notice_count = sum("auto-retrying without Rank ordering" in line for line in printed)
    assert second_notice_count == 1, "Expected no additional auto-retry notice on repeated identical JQL"
