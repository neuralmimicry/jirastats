def test_jira_fetch_issues_rest_pagination(monkeypatch):
    import jira_analysis as ja

    # Force REST path with page_size=100
    monkeypatch.setattr(ja, "_get_search_config", lambda: (False, 100))

    total = 230

    def fake_rest(base_url, auth, q, start_at, max_results, fields):
        # simulate pages of 100, 100, 30
        remaining = max(0, total - start_at)
        size = min(max_results, remaining)
        issues = [{"key": f"KEY-{start_at + i + 1}", "fields": {}} for i in range(size)]
        return {"issues": issues, "total": total}

    monkeypatch.setattr(ja, "_search_via_rest", fake_rest)

    issues = ja.fetch_issues(
        base_url="https://example.atlassian.net",
        auth=("u", "p"),
        projects=None,
        jql="order by updated",
        limit=10000,
    )

    assert len(issues) == total
    assert issues[0].key == "KEY-1"
    assert issues[-1].key == f"KEY-{total}"

    # Now test cap works
    issues_capped = ja.fetch_issues(
        base_url="https://example.atlassian.net",
        auth=("u", "p"),
        projects=None,
        jql="order by updated",
        limit=150,
    )
    assert len(issues_capped) == 150
