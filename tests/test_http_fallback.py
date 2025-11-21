from types import SimpleNamespace as NS
from unittest.mock import Mock

import main as m


class _Resp:
    def __init__(self, status=200, json_data=None, text=""):
        self._status = status
        self._json = json_data or {}
        self.text = text

    def raise_for_status(self):
        if self._status >= 400:
            raise Exception(f"HTTP {self._status}")

    def json(self):
        return self._json


def test_http_fallback_to_classic_search_when_jql_returns_empty(monkeypatch):
    # Force HTTP path
    monkeypatch.setattr(m, "PREFER_CLIENT_SEARCH", False, raising=False)
    # Provide credentials without prompting
    monkeypatch.setattr(m, "get_credentials", lambda: ("user", "token"))
    # Dummy Jira client for signature
    jira = NS()

    # Track calls to requests.post
    calls = []

    def fake_post(url, json=None, headers=None, auth=None, params=None):
        calls.append((url, json))
        # First endpoint: /search/jql returns no issues (200 OK)
        if url.endswith("/search/jql"):
            return _Resp(200, {"issues": []})
        # Classic endpoint: /search returns some issues
        if url.endswith("/search"):
            return _Resp(200, {"issues": [{"key": "PRJ-1"}, {"key": "PRJ-2"}]})
        return _Resp(404, text="not found")

    monkeypatch.setattr(m.requests, "post", fake_post)

    issues = m.fetch_issues(jira, "project = PRJ ORDER BY Rank")

    # Expect fallback provided issues
    assert isinstance(issues, list) and len(issues) == 2
    # Ensure both endpoints were attempted
    assert any(u.endswith("/search/jql") for u, _ in calls)
    assert any(u.endswith("/search") for u, _ in calls)
