import pytest

from jira_analysis import _extract_confluence_ids


def test_extract_ids_same_host_full_urls():
    jira_base = "https://virginmediao2.atlassian.net"
    text = (
        "See the design: https://virginmediao2.atlassian.net/wiki/spaces/ENG/pages/123456/DesignDoc "
        "and the decision record https://virginmediao2.atlassian.net/wiki/pages/789012"
    )
    ids = _extract_confluence_ids(text, jira_base)
    assert ids == ["123456", "789012"]


def test_extract_ids_path_only_links():
    jira_base = "https://virginmediao2.atlassian.net"
    text = (
        "Links kept short: /wiki/spaces/ARCH/pages/111222/ADR-42 and more at /wiki/pages/333444"
    )
    ids = _extract_confluence_ids(text, jira_base)
    assert ids == ["111222", "333444"]


def test_other_host_full_url_not_included():
    jira_base = "https://virginmediao2.atlassian.net"
    text = (
        "External ref: https://other.atlassian.net/wiki/spaces/XYZ/pages/555666/NotOurs"
    )
    ids = _extract_confluence_ids(text, jira_base)
    assert ids == []


def test_no_links():
    jira_base = "https://virginmediao2.atlassian.net"
    text = "Plain description without Confluence links."
    ids = _extract_confluence_ids(text, jira_base)
    assert ids == []
