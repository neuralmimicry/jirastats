"""
Discovery utilities to probe Confluence and Jira to build a hierarchy of interest
based on configured keywords (e.g., CTO, DNP, DNT, Digital Network Products).

This module is designed to minimize data transfer by constructing a refined JQL
that targets only relevant Projects and Epics discovered from Confluence pages and
Jira metadata, so the subsequent search_issues call can fetch minimal results.

It uses:
- Jira Python client already used by the project (for Jira project and issue discovery)
- requests for Confluence CQL search (no new heavy dependency)

All network calls are best-effort and failures will be handled gracefully by
returning an empty discovery result, allowing the pipeline to fall back to the
user-provided base JQL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import os
import time
import json
import re
import requests

DEFAULT_CACHE_FILE = ".discovery_cache.json"


@dataclass
class DiscoveryConfig:
    enabled: bool = True
    keywords: List[str] = field(default_factory=lambda: [
        "CTO", "DNP", "DNT", "Digital Network Products"
    ])
    # Optionally constrain by known space or project keys if provided
    confluence_space_keys: List[str] = field(default_factory=list)
    jira_project_keys: List[str] = field(default_factory=list)
    cache_ttl_minutes: int = 120


@dataclass
class DiscoveryResult:
    projects: List[str] = field(default_factory=list)  # Jira project keys
    epics: List[str] = field(default_factory=list)     # Epic issue keys
    spaces: List[str] = field(default_factory=list)    # Confluence space keys
    pages: List[Dict[str, Any]] = field(default_factory=list)  # Confluence pages metadata
    # Discovered Jira fields of interest by semantic role. Values are field IDs/names.
    fields: Dict[str, Any] = field(default_factory=dict)


ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _now_epoch() -> int:
    return int(time.time())


def load_discovery_config(config: Dict[str, Any]) -> DiscoveryConfig:
    raw = config.get("discovery", {}) if isinstance(config, dict) else {}
    enabled_env = os.getenv("DISCOVERY_DISABLE")
    enabled = not (str(enabled_env).lower() in ("1", "true", "yes")) if enabled_env is not None else raw.get("enabled", True)
    dc = DiscoveryConfig(
        enabled=enabled,
        keywords=raw.get("keywords", DiscoveryConfig().keywords),
        confluence_space_keys=raw.get("confluence_space_keys", []),
        jira_project_keys=raw.get("jira_project_keys", []),
        cache_ttl_minutes=int(raw.get("cache_ttl_minutes", 120)),
    )
    # Allow CSV env override for keywords
    kw_env = os.getenv("DISCOVERY_KEYWORDS")
    if kw_env:
        dc.keywords = [k.strip() for k in kw_env.split(",") if k.strip()]
    return dc


def _read_cache(cache_file: str) -> Optional[Tuple[int, DiscoveryResult]]:
    try:
        with open(cache_file, "r") as f:
            data = json.load(f)
        ts = data.get("timestamp")
        res = DiscoveryResult(**data.get("result", {}))
        return ts, res
    except Exception:
        return None


def _write_cache(cache_file: str, result: DiscoveryResult) -> None:
    try:
        with open(cache_file, "w") as f:
            json.dump({"timestamp": _now_epoch(), "result": result.__dict__}, f, indent=2)
    except Exception:
        pass


def _keywords_cql(keywords: List[str]) -> str:
    # Build Confluence CQL matching title or text by keywords
    terms = [f'title ~ "{k}" or text ~ "{k}"' for k in keywords]
    return " or ".join(terms)


def _probe_confluence(jira_base_url: str, auth: Tuple[str, str], cfg: DiscoveryConfig) -> Tuple[List[str], List[Dict[str, Any]]]:
    # Atlassian Cloud Confluence typically lives under the same base with /wiki
    base = jira_base_url.rstrip("/") + "/wiki"
    search_url = f"{base}/rest/api/search"
    cql = _keywords_cql(cfg.keywords)
    params = {"cql": cql, "limit": 50}
    spaces: List[str] = []
    pages: List[Dict[str, Any]] = []
    try:
        resp = requests.get(search_url, params=params, auth=auth, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for r in data.get("results", []):
            container = r.get("content", {}).get("_expandable", {})
            # Fallback: some shapes include space key at r["resultGlobalContainer"]["spaceKey"]
            space_key = None
            try:
                space_key = r.get("content", {}).get("space", {}).get("key")
            except Exception:
                pass
            if not space_key:
                space_key = r.get("resultGlobalContainer", {}).get("spaceKey")
            if space_key:
                spaces.append(space_key)
            pages.append({
                "id": r.get("content", {}).get("id"),
                "title": r.get("content", {}).get("title"),
                "url": r.get("url"),
                "extract": r.get("extract"),
                "spaceKey": space_key,
            })
    except Exception:
        # Swallow errors and return empty results
        return [], []

    # Apply optional filters
    if cfg.confluence_space_keys:
        spaces = [s for s in spaces if s in cfg.confluence_space_keys]
        pages = [p for p in pages if p.get("spaceKey") in cfg.confluence_space_keys]

    # De-duplicate
    spaces = sorted(list({s for s in spaces if s}))
    return spaces, pages


def _extract_issue_keys_from_pages(pages: List[Dict[str, Any]]) -> List[str]:
    keys = []
    for p in pages:
        for field in ("title", "extract"):
            val = p.get(field) or ""
            keys.extend(m.group(1) for m in ISSUE_KEY_RE.finditer(val))
    # Deduplicate while preserving order
    seen = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _probe_jira(jira_client, cfg: DiscoveryConfig) -> Tuple[List[str], List[str]]:
    project_keys: List[str] = []
    epic_keys: List[str] = []
    try:
        projects = jira_client.projects()
        for p in projects:
            name = getattr(p, "name", "") or ""
            key = getattr(p, "key", "") or ""
            if any(k.lower() in name.lower() for k in cfg.keywords) or (cfg.jira_project_keys and key in cfg.jira_project_keys):
                project_keys.append(key)
    except Exception:
        # ignore
        pass

    # If we found project keys, search for epics matching keywords within those projects
    try:
        if project_keys:
            jql = f"project in ({','.join(project_keys)}) AND issuetype = Epic AND (" + " OR ".join(
                [f"summary ~ '{k}' or 'Epic Name' ~ '{k}'" for k in cfg.keywords]
            ) + ")"
        else:
            jql = "issuetype = Epic AND (" + " OR ".join([f"summary ~ '{k}' or 'Epic Name' ~ '{k}'" for k in cfg.keywords]) + ")"
        issues = jira_client.search_issues(jql, maxResults=200)
        for i in issues:
            epic_keys.append(i.key)
    except Exception:
        # ignore
        pass

    # Apply optional explicit project filter override
    if cfg.jira_project_keys:
        project_keys = [k for k in project_keys if k in cfg.jira_project_keys] or cfg.jira_project_keys

    # De-duplicate
    project_keys = sorted(list({k for k in project_keys if k}))
    epic_keys = sorted(list({k for k in epic_keys if k}))
    return project_keys, epic_keys


def _discover_fields(jira_client) -> Dict[str, Any]:
    """
    Inspect Jira fields and pick likely candidates for timeline/progress analysis.
    Returns a mapping from semantic role to a list of field IDs (or special tokens for system fields).
    """
    try:
        fields = jira_client.fields()
    except Exception:
        fields = []

    def match(name: str, *patterns: str) -> bool:
        n = (name or "").lower()
        return any(p in n for p in patterns)

    role_map: Dict[str, list] = {
        "start_date": [],
        "end_date": [],
        "due_date": [],
        "updated": ["updated"],  # system
        "created": ["created"],  # system
        "resolutiondate": ["resolutiondate"],  # system
        "progress": ["progress", "aggregateprogress"],  # system-derived
        "statuscategorychangedate": ["statuscategorychangedate"],
        "assignee": ["assignee"],
        "epic_link": ["Epic Link", "epicLink", "parentEpic"],
    }

    for f in fields or []:
        fid = f.get("id") or ""
        fname = f.get("name") or ""
        # Start-like
        if match(fname, "start date", "start", "planned start", "target start", "begin"):
            role_map["start_date"].append(fid)
        # End-like
        if match(fname, "end date", "finish", "planned end", "target end"):
            role_map["end_date"].append(fid)
        if match(fname, "due"):
            role_map["due_date"].append(fid)
        if match(fname, "epic link"):
            if fid not in role_map["epic_link"]:
                role_map["epic_link"].append(fid)
        if match(fname, "progress") and fid not in role_map["progress"]:
            role_map["progress"].append(fid)

    # De-duplicate values
    for k, v in role_map.items():
        seen = []
        for x in v:
            if x not in seen:
                seen.append(x)
        role_map[k] = seen
    return role_map


def discover_hierarchy(jira_client, jira_base_url: str, auth: Tuple[str, str], config: Dict[str, Any]) -> DiscoveryResult:
    """
    Perform best-effort discovery across Confluence and Jira.

    Returns DiscoveryResult with lists of candidate project keys and epic keys. This can be
    turned into a refined JQL to limit the subsequent fetch.
    """
    dcfg = load_discovery_config(config)
    if not dcfg.enabled:
        return DiscoveryResult()

    # Basic file cache to avoid repeated probing
    cache = _read_cache(DEFAULT_CACHE_FILE)
    if cache:
        ts, res = cache
        if (_now_epoch() - int(ts)) <= dcfg.cache_ttl_minutes * 60:
            return res

    spaces, pages = _probe_confluence(jira_base_url, auth, dcfg)
    project_keys, epic_keys = _probe_jira(jira_client, dcfg)

    # Try to extract any issue keys mentioned on pages, keep only Epic-like if possible
    page_issue_keys = _extract_issue_keys_from_pages(pages)
    # Combine epic keys
    all_epics = sorted(list({*epic_keys, *page_issue_keys}))

    # Discover fields
    fields_map = _discover_fields(jira_client)

    result = DiscoveryResult(projects=project_keys, epics=all_epics, spaces=spaces, pages=pages, fields=fields_map)
    _write_cache(DEFAULT_CACHE_FILE, result)
    return result


def build_refined_jql(base_jql: str, discovery: DiscoveryResult) -> str:
    """
    Build a refined JQL using discovered projects and epics. If discovery is empty,
    return the base_jql unchanged.

    We attempt to support both classic "Epic Link" and the newer parentEpic fields.
    """
    has_projects = bool(discovery.projects)
    has_epics = bool(discovery.epics)
    if not (has_projects or has_epics):
        return base_jql

    filters = []
    if has_projects:
        proj_list = ",".join(discovery.projects)
        filters.append(f"project in ({proj_list})")
    if has_epics:
        epic_list = ",".join(discovery.epics)
        # Try both fields to maximize compatibility
        epic_filter = f"('Epic Link' in ({epic_list}) OR parentEpic in ({epic_list}))"
        # Also include epics themselves
        epic_self = f"(issuetype = Epic AND key in ({epic_list}))"
        filters.append(f"({epic_filter} OR {epic_self})")

    refined = " AND ".join(filters)
    if base_jql:
        return f"({base_jql}) AND ({refined})"
    return refined
