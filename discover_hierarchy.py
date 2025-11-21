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
    # Caps for supplemental epic discovery to keep queries small and paginated
    max_epics_per_project: int = 20
    max_projects_for_epics: int = 30


@dataclass
class DiscoveryResult:
    projects: List[str] = field(default_factory=list)  # Jira project keys
    epics: List[str] = field(default_factory=list)     # Epic issue keys
    spaces: List[str] = field(default_factory=list)    # Confluence space keys
    pages: List[Dict[str, Any]] = field(default_factory=list)  # Confluence pages metadata
    # Discovered Jira fields of interest by semantic role. Values are field IDs/names.
    fields: Dict[str, Any] = field(default_factory=dict)
    # Discovered issue types and an inferred ranking for sorting/grouping.
    issue_types: List[str] = field(default_factory=list)
    issue_ranking: Dict[str, int] = field(default_factory=dict)
    # Optional diagnostics to aid troubleshooting (e.g., per-project epic counts)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


# Be stricter: require letters-only project keys (at least two letters), then dash and digits.
# This avoids picking up quarter-like strings such as Q1-2026 or FY24-10 as Jira issue keys.
ISSUE_KEY_RE = re.compile(r"\b([A-Z]{2,}-\d+)\b")


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
        max_epics_per_project=int(raw.get("max_epics_per_project", 20)),
        max_projects_for_epics=int(raw.get("max_projects_for_epics", 30)),
    )
    # Allow CSV env override for keywords
    kw_env = os.getenv("DISCOVERY_KEYWORDS")
    if kw_env:
        dc.keywords = [k.strip() for k in kw_env.split(",") if k.strip()]
    return dc


def _read_cache(cache_file: str) -> Optional[Tuple[int, Optional[str], DiscoveryResult]]:
    try:
        with open(cache_file, "r") as f:
            data = json.load(f)
        ts = data.get("timestamp")
        base_url = data.get("base_url")
        res = DiscoveryResult(**data.get("result", {}))
        return ts, base_url, res
    except Exception:
        return None


def _write_cache(cache_file: str, result: DiscoveryResult, base_url: Optional[str] = None) -> None:
    try:
        with open(cache_file, "w") as f:
            json.dump({"timestamp": _now_epoch(), "base_url": base_url, "result": result.__dict__}, f, indent=2)
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


def _discover_issue_types(jira_client, config: Dict[str, Any]) -> Tuple[List[str], Dict[str, int]]:
    """
    Best-effort discovery of available issue types and construction of a logical
    ranking, using configuration as guidance but adapting to what's discovered
    in the target Jira instance.

    Strategy:
    - Try jira_client.issue_types() to list types (python-jira standard).
    - Fallback: sample a few recent issues (if possible) and collect issuetype names.
    - Build an ordered list using config["issue_types"] as a guide. Preserve the
      guide's order for known types; append unknown types before any Sub-task-like
      entries so that sub-tasks remain last.
    - Produce a 1-based ranking dictionary from the final order.
    """
    guide: List[str] = []
    try:
        guide = list((config or {}).get("issue_types", []) or [])
    except Exception:
        guide = []

    discovered: List[str] = []
    # Primary: issue_types() from python-jira
    try:
        types = jira_client.issue_types()  # returns objects with .name typically
        for t in types or []:
            name = getattr(t, "name", None)
            if not name and isinstance(t, dict):
                name = t.get("name")
            if name and name not in discovered:
                discovered.append(name)
    except Exception:
        pass

    # Fallback: sample a few issues and take their issuetype names
    if not discovered:
        try:
            issues = jira_client.search_issues("order by created desc", maxResults=50)
            for iss in issues or []:
                fields = getattr(iss, "fields", None)
                it = getattr(fields, "issuetype", None) if fields else None
                name = getattr(it, "name", None)
                if name and name not in discovered:
                    discovered.append(name)
        except Exception:
            pass

    # If we still have nothing, fall back entirely to the guide
    if not discovered:
        ordered = list(guide)
    else:
        # Start with guide order but keep only those that are discovered
        ordered: List[str] = [g for g in guide if g in discovered]
        # Unknowns: anything discovered not in ordered
        unknowns = [n for n in discovered if n not in ordered]
        # Keep sub-tasks to the end
        non_sub = [n for n in unknowns if "sub-task" not in n.lower() and "subtask" not in n.lower()]
        subs = [n for n in unknowns if n not in non_sub]
        ordered.extend(non_sub)
        # Ensure a single canonical Sub-task token from guide remains last if present
        ordered.extend(subs)

    # Ensure uniqueness and stable order
    seen = set()
    final_list: List[str] = []
    for n in ordered:
        if n and n not in seen:
            seen.add(n)
            final_list.append(n)

    # Construct ranking: 1-based indices
    ranking: Dict[str, int] = {name: idx + 1 for idx, name in enumerate(final_list)}
    return final_list, ranking


def discover_hierarchy(jira_client, jira_base_url: str, auth: Tuple[str, str], config: Dict[str, Any]) -> DiscoveryResult:
    """
    Perform best-effort discovery across Confluence and Jira.

    Returns DiscoveryResult with lists of candidate project keys and epic keys. This can be
    turned into a refined JQL to limit the subsequent fetch.
    """
    dcfg = load_discovery_config(config)
    if not dcfg.enabled:
        return DiscoveryResult()

    # Basic file cache to avoid repeated probing; only reuse if base URL matches
    cache = _read_cache(DEFAULT_CACHE_FILE)
    if cache:
        ts, cached_base, res = cache
        if (cached_base == jira_base_url) and ((_now_epoch() - int(ts)) <= dcfg.cache_ttl_minutes * 60):
            return res

    spaces, pages = _probe_confluence(jira_base_url, auth, dcfg)
    project_keys, epic_keys = _probe_jira(jira_client, dcfg)

    # Supplemental epic discovery: if few/no epics found via keyword probing, fetch a small
    # capped set of recent Epics per discovered project to avoid undercounting epics.
    try:
        need_supplement = len(epic_keys) == 0 and len(project_keys) > 0
    except Exception:
        need_supplement = False
    supplemental_counts: Dict[str, int] = {}
    child_link_counts: Dict[str, int] = {}
    if need_supplement:
        try:
            per_proj_limit = max(1, int(dcfg.max_epics_per_project))
            proj_limit = max(1, int(dcfg.max_projects_for_epics))
            supplemental: List[str] = []
            for pkey in project_keys[:proj_limit]:
                start_at = 0
                fetched = 0
                before_count = 0
                while fetched < per_proj_limit:
                    # Order by updated desc to get the most relevant epics first
                    jql = f"project = {pkey} AND issuetype = Epic ORDER BY updated DESC"
                    try:
                        batch = jira_client.search_issues(jql, startAt=start_at, maxResults=min(50, per_proj_limit - fetched))
                    except Exception:
                        batch = []
                    if not batch:
                        break
                    for issue in batch:
                        k = getattr(issue, 'key', None)
                        if k and k not in supplemental:
                            supplemental.append(k)
                    got = len(batch)
                    fetched += got
                    if got < min(50, per_proj_limit - (fetched - got)):
                        # fewer results than requested → no more pages
                        break
                    start_at += got
                # Track per-project supplemental epic count
                supplemental_counts[pkey] = sum(1 for k in supplemental if k.startswith(f"{pkey}-")) if supplemental else 0
            if supplemental:
                # Merge with existing set
                merged = sorted(list({*epic_keys, *supplemental}))
                epic_keys = merged
            # If still no epics found via explicit Epic search, try deriving epics from recent child issues
            if not epic_keys:
                derived: List[str] = []
                for pkey in project_keys[:proj_limit]:
                    start_at = 0
                    fetched = 0
                    while fetched < per_proj_limit:
                        jql = (
                            f"project = {pkey} AND issuetype in (Story, Task, Bug, Improvement, Spike) ORDER BY updated DESC"
                        )
                        try:
                            batch = jira_client.search_issues(jql, startAt=start_at, maxResults=min(50, per_proj_limit - fetched))
                        except Exception:
                            batch = []
                        if not batch:
                            break
                        for issue in batch:
                            try:
                                fields = getattr(issue, 'fields', None)
                                epic_key = None
                                if fields is not None:
                                    cf = getattr(fields, 'customfield_10014', None)
                                    if isinstance(cf, str):
                                        epic_key = cf
                                    elif cf is not None:
                                        epic_key = getattr(cf, 'key', None)
                                    if not epic_key:
                                        pe = getattr(fields, 'parentEpic', None)
                                        if isinstance(pe, str):
                                            epic_key = pe
                                        elif pe is not None:
                                            epic_key = getattr(pe, 'key', None)
                                    # Some instances expose only a generic parent object
                                    if not epic_key:
                                        parent = getattr(fields, 'parent', None)
                                        if parent is not None:
                                            epic_key = getattr(parent, 'key', None)
                                if epic_key and epic_key not in derived:
                                    derived.append(epic_key)
                            except Exception:
                                continue
                        got = len(batch)
                        fetched += got
                        if got < min(50, per_proj_limit - (fetched - got)):
                            break
                        start_at += got
                    # Track per-project counts for derived epics
                    child_link_counts[pkey] = sum(1 for k in derived if k.startswith(f"{pkey}-")) if derived else 0
                if derived:
                    epic_keys = sorted(list({*epic_keys, *derived}))
        except Exception:
            # best-effort only
            pass

    # Try to extract any issue keys mentioned on pages, keep only Epic-like if possible
    page_issue_keys = _extract_issue_keys_from_pages(pages)
    # Combine epic keys (keyword/bulk + supplemental + page extracted)
    all_epics = sorted(list({*epic_keys, *page_issue_keys}))

    # Validate epic keys against Jira to avoid bogus keys extracted from Confluence text (e.g., Q1-2026)
    valid_epics: List[str] = []
    if all_epics:
        try:
            # Query in small batches to respect URL length and server limits
            batch_size = 50
            for idx in range(0, len(all_epics), batch_size):
                batch = all_epics[idx: idx + batch_size]
                jql = f"issuetype = Epic AND key in ({','.join(batch)})"
                try:
                    res = jira_client.search_issues(jql, maxResults=len(batch))
                except Exception:
                    res = []
                for issue in res or []:
                    k = getattr(issue, 'key', None)
                    if k and k not in valid_epics:
                        valid_epics.append(k)
        except Exception:
            # If validation fails entirely, fall back to unvalidated list
            valid_epics = list(all_epics)
    else:
        valid_epics = []

    # Discover fields
    fields_map = _discover_fields(jira_client)
    # Discover issue types and an inferred ranking
    issue_types, issue_ranking = _discover_issue_types(jira_client, config)

    result = DiscoveryResult(
        projects=project_keys,
        epics=valid_epics,
        spaces=spaces,
        pages=pages,
        fields=fields_map,
        issue_types=issue_types,
        issue_ranking=issue_ranking,
    )
    # Attach diagnostics if we have any supplemental probing info
    try:
        if supplemental_counts:
            result.diagnostics["supplemental_epic_counts"] = supplemental_counts
            result.diagnostics["supplemental_probed_projects"] = list(supplemental_counts.keys())
        if child_link_counts:
            result.diagnostics["child_issue_epic_counts"] = child_link_counts
    except Exception:
        # do not fail discovery due to diagnostics
        pass
    _write_cache(DEFAULT_CACHE_FILE, result, base_url=jira_base_url)
    return result


def build_refined_jql(base_jql: str, discovery: DiscoveryResult) -> str:
    """
    Build a refined JQL using discovered projects and epics. If discovery is empty,
    return the base_jql unchanged.

    We attempt to support both classic "Epic Link" and the newer parentEpic fields.

    Important: If the base_jql contains an ORDER BY clause, it must appear at the very end
    of the final JQL (after all filters). This function will extract any ORDER BY from the
    base_jql, combine WHERE-like parts with discovery filters, and then re-append ORDER BY
    at the end to avoid syntax errors like "Expecting ')' but got 'ORDER'".
    """
    has_projects = bool(discovery.projects)
    has_epics = bool(discovery.epics)
    if not (has_projects or has_epics):
        # Nothing to refine; return base as-is
        return base_jql

    # Split base_jql into WHERE-ish part and ORDER BY tail (case-insensitive)
    order_by_part = ""
    where_part = (base_jql or "").strip()
    if where_part:
        m = re.search(r"\border\s+by\b(.+)$", where_part, flags=re.IGNORECASE)
        if m:
            order_by_part = " ORDER BY " + m.group(1).strip()
            where_part = where_part[: m.start()].strip()

    # Build discovery filters
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

    # Use OR between project and epic filters to avoid over-constraining the
    # result set when epics may reside outside the discovered project set or
    # when epic extraction is imperfect. This yields issues that are either in
    # the discovered projects OR related to the discovered epics.
    refined = " OR ".join(filters)

    # Combine where_part and refined filters
    # Important: Using OR between base WHERE and discovery filters helps avoid
    # over-constraining the query when discovered epics span projects outside
    # the discovered project list. This yields a broader but more useful scope.
    if where_part and refined:
        combined = f"({where_part}) AND ({refined})"
    else:
        combined = where_part or refined

    # Re-append ORDER BY at the end if present
    final_jql = (combined or "").strip()
    if order_by_part:
        final_jql = f"{final_jql}{order_by_part}"
    return final_jql
