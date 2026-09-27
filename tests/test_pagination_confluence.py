import types


def test_confluence_fetch_space_pages_paginates_beyond_50(monkeypatch):
    import confluence_analysis as ca

    # Force page size to 50 and high max_items via config
    def fake_load_config():
        return {
            "confluence": {
                "page_size": 50,
                "max_items": 10000,
            }
        }

    # Total items to simulate
    total = 120

    def fake_conf_get(base_url, auth, path, params=None):
        assert path == "/rest/api/search"
        params = params or {}
        start = int(params.get("start", 0))
        limit = int(params.get("limit", 50))
        remaining = max(0, total - start)
        size = min(limit, remaining)
        results = []
        for i in range(size):
            idx = start + i
            results.append({
                "content": {
                    "id": str(idx + 1),
                    "title": f"Page {idx+1}",
                    "space": {"key": "SPC"},
                    "version": {"when": None, "by": {}},
                    "metadata": {"labels": {"results": []}},
                }
            })
        return {"results": results, "size": size}

    monkeypatch.setattr("main.load_config", fake_load_config)
    monkeypatch.setattr(ca, "_conf_get", fake_conf_get)

    pages = ca.fetch_space_pages("https://example.atlassian.net", ("u", "p"), "SPC")
    assert len(pages) == total
    # Ensure we got expected first/last elements
    assert pages[0].title == "Page 1"
    assert pages[-1].title == f"Page {total}"
