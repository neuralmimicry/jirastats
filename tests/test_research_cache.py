import pytest
import os
import json
import time
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher, RESEARCH_CACHE_ROOT

@pytest.fixture
def researcher():
    with patch("topic_researcher.get_provider"):
        with patch("topic_researcher.GoogleSearchEngine.verify", return_value=(True, "Success")):
            return TopicResearcher(
                jira_base_url="https://test.atlassian.net",
                jira_auth=("user", "token"),
                llm_provider="openai",
                cache_ttl_hours=1
            )

def test_cache_write_read(researcher):
    url = "https://example.com/test-cache"
    content = "Cached content for testing"
    
    # Ensure cache is empty
    import shutil
    if os.path.exists(RESEARCH_CACHE_ROOT):
        shutil.rmtree(RESEARCH_CACHE_ROOT)
        
    # Write to cache
    researcher._write_cache(url, content)
    
    # Read from cache
    retrieved = researcher._read_cache(url)
    assert retrieved["content"] == content
    
    # Verify file existence
    import hashlib
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = os.path.join(RESEARCH_CACHE_ROOT, f"{url_hash}.json")
    assert os.path.exists(cache_path)
    
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["url"] == url
        assert data["content"] == content

def test_cache_expiration(researcher):
    url = "https://example.com/expired"
    content = "Old content"
    
    researcher._write_cache(url, content)
    
    # Manually backdate the timestamp
    import hashlib
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = os.path.join(RESEARCH_CACHE_ROOT, f"{url_hash}.json")
    
    with open(cache_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    data["timestamp"] = int(time.time()) - (2 * 3600) # 2 hours ago
    
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
        
    # Should be expired (TTL is 1 hour in fixture)
    retrieved = researcher._read_cache(url)
    assert retrieved is None

@patch("topic_researcher.requests.Session")
def test_read_source_uses_cache(mock_session_class, researcher):
    url = "https://example.com/cached-source"
    content = "Fetched from cache"
    metadata = {"type": "Web", "title": "Test Title"}
    
    researcher._write_cache(url, content, metadata=metadata)
    
    # This should not trigger a network call
    result = researcher._read_source(url)
    
    assert result == content
    mock_session_class.assert_not_called()
    # Check if contribution was recorded
    assert url in researcher.contributing_web
    assert researcher.source_metadata[url]["title"] == "Test Title"

@patch("topic_researcher.requests.Session")
def test_read_source_saves_to_cache(mock_session_class, researcher):
    url = "https://example.com/new-source"
    content = "Freshly fetched content"
    
    # Mock network call
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = content
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_session.get.return_value = mock_resp
    
    # Ensure cache is clear
    import hashlib
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = os.path.join(RESEARCH_CACHE_ROOT, f"{url_hash}.json")
    if os.path.exists(cache_path):
        os.remove(cache_path)
        
    # Read source (triggers fetch and cache save)
    result = researcher._read_source(url)
    
    assert result == content
    cache_data = researcher._read_cache(url)
    assert cache_data["content"] == content
    assert cache_data["metadata"]["type"] == "Web"

@patch("topic_researcher._jira_get")
def test_read_source_jira_cache(mock_jira_get, researcher):
    url = "https://test.atlassian.net/browse/PROJ-1"
    content = "Jira Issue PROJ-1: Summary\n\nDescription"
    metadata = {"type": "Jira", "identifier": "PROJ-1", "title": "Summary"}
    
    researcher._write_cache(url, content, metadata=metadata)
    
    result = researcher._read_source(url)
    assert result == content
    mock_jira_get.assert_not_called()
    assert "PROJ-1" in researcher.contributing_jira
