# JiraStats

## Sponsor NeuralMimicry

JiraStats is an open-source reporting toolkit for Jira and Confluence — discovery-driven JQL refinement, throughput analysis, timeline reporting, and LLM-backed insights, all configuration-driven and reusable across organisations. NeuralMimicry is an independent open-source initiative and we rely on community support to sustain this work.

**[☕ Support us on Crowdfunder](https://www.crowdfunder.co.uk/p/qr/aWggxwPW?utm_campaign=sharemodal&utm_medium=referral&utm_source=shortlink)**

---

A lightweight reporting toolkit for Jira that discovers relevant scope (projects, epics) from Jira/Confluence, fetches only the necessary data, and produces CSV reports and charts on throughput, timelines, and resource usage.

Key aspects:
- Configuration-driven: company names, Jira URL, custom field IDs, engineer lists, and issue schemas live outside the code in config.json and environment variables.
- Minimal downloads: optional discovery step narrows JQL so Jira does most of the filtering server-side.
- Reusable core: algorithms are data-agnostic so the tool can be reused across orgs and schemas.
 - Small-chunk fetching with local cache: issue searches are paginated (page_size configurable) to reduce per-request load. A lightweight JSONL cache accumulates fetched pages and can be used as a last-resort data source when servers return little/no data.


## Quickstart
1) Install dependencies
- pip install -r requirements.txt
- Optional package install for CLI: pip install -e .

2) Configure
- Copy or edit config.json to match your Jira instance.
- Optionally export environment variables (see below) to supply credentials and overrides.

3) Run
- CLI (after editable install): jirastats
- From source: python -m jirastats.cli
- Legacy entry point: python run_report.py

The workflow will (optionally) run discovery, refine your JQL, fetch issues, generate monthly CSVs and a leaderboard, and write a consolidated timelines.csv.
If the refined JQL returns no results, the tool automatically retries with your base JQL to avoid empty runs due to over-filtering.


## Configuration
This repository ships with a default config.json at the project root. You can tailor it per company or environment without changing code.

Example config.json
{
  "instances": [
    {
      "name": "Instance A",
      "jira_url": "https://instance-a.atlassian.net",
      "confluence_url": "https://instance-a.atlassian.net/wiki"
    },
    {
      "name": "Instance B",
      "jira_url": "https://instance-b.atlassian.net"
    }
  ],
  "data_files": {
    "engineer_names": "engineer_names.csv",
    "leaderboard": "leaderboard.csv",
    "monthly_csv_prefix": "monthly_subtask_summary_data",
    "timelines": "timelines.csv",
    "gantt_projects": "gantt_projects.png"
  },
  "issue_types": ["Bug", "Improvement", "New Feature", "Spike", "Epic", "Story", "Task", "Sub-task"],
  "priority_ranking": {"Highest": 1, "High": 2, "Medium": 3, "Low": 4, "Lowest": 5},
  "issue_ranking": {"Epic": 1, "Bug": 2, "Spike": 3, "New Feature": 4, "Improvement": 5, "Story": 6, "Task": 7, "Sub-task": 8},
  "custom_fields": {
    "skills_field": "customfield_10900",
    "workstream_field": "customfield_10952",
    "universe_skill_name": "UniVerse"
  },
  "office_hours": {
    "start_hour": 9,
    "end_hour": 17,
    "country": "GB"
  },
  "jql_query": "ORDER BY Rank",
  "discovery": {
    "enabled": true,
    "keywords": ["CTO", "DNP", "DNT", "Digital Network Products"],
    "confluence_space_keys": [],
    "jira_project_keys": [],
    "cache_ttl_minutes": 120
  }
}

Notes
- instances: List of Jira/Confluence instances to query. Each instance needs `name` and `jira_url`, and optionally `confluence_url`.
- data_files.* control input/output filenames (including timelines.csv and gantt_projects.png).
- custom_fields.* let you map instance-specific field IDs once, instead of changing code.
- office_hours define the workday and holiday region. Supported codes include GB/UK, US, CA, DE, FR; unknown codes fall back to GB.
- jql_query provides a base JQL that discovery can refine at runtime.
- search: controls search behavior. Keys (defaults shown):
  - prefer_client (bool, default: true): when true or env PREFER_CLIENT_SEARCH=1, use the python-jira client directly (most compatible with Atlassian Cloud). Set to false to prefer HTTP /search/jql.
  - page_size (int, default: 100): pagination size for HTTP/client explicit pagination.
  - fail_fast_http (bool, default: true): after first 4xx from /search/jql, immediately fall back to client search.
  - allow_alt_shapes (bool, default: true): try alternative JSON shapes for /search/jql for broader compatibility.
  - debug (bool, default: false): enable verbose diagnostics for search; can also use env DEBUG_SEARCH=1.
  - recent_days (int, default: 180): time window for bounded fallbacks when refined/base JQL yield no or minimal results.
  - min_results (int, default: 20): if a refined query returns fewer than this number (but more than zero), constraints are relaxed and retried to broaden selection.
  - force_ultra_broad (bool, default: false): when true (or env FORCE_ULTRA_BROAD=1), bypass discovery and run an ultra-broad query first: updated >= -recent_days, preserving any ORDER BY.
  - allow_extreme_broad (bool, default: true): when all refined/base and fallback queries (including ultra-broad) return 0, perform one last bounded attempt with no WHERE clause, i.e., "ORDER BY created DESC" to fetch the most recent issues available. Can be disabled via env ALLOW_EXTREME_BROAD=0.
  - enable_user_scoped_fallback (bool, default: true): when broader instance-level queries still yield no results, try a user-scoped recent activity query: (assignee = currentUser() OR reporter = currentUser()) AND updated >= -recent_days.
  - try_created_window (bool, default: true): in addition to updated-based windows, also try created >= -recent_days to catch old-but-recently-created issues where updated field may not reflect activity.
  - avoid_rank_order (bool, default: false): when true (or env AVOID_RANK_ORDER=1), replace any trailing "ORDER BY Rank" with a safer, portable sort to avoid Rank-related permission/index issues on some Jira instances.
  - rank_fallback (string, default: "created"): the field to use when replacing Rank; accepted values: "created", "updated". Sorting direction is DESC.
  - enable_cache (bool, default: true): enable lightweight JSONL caching of fetched pages to progressively build a local dataset.
  - issues_cache (string, default: ".issues_cache.jsonl"): path to the JSONL cache file.
  - prefer_cache_for_fallbacks (bool, default: true): when all remote attempts return 0, fall back to using cached issues (within cache_max_age_days) to generate reports.
  - cache_max_age_days (int, default: 7): only use cached issues fetched within the last N days.
  - iterate_per_project (bool, default: false): when true (or env ITERATE_PER_PROJECT=1), refined queries that target many projects will be executed per project (project = KEY) in small chunks and merged locally. This reduces server load and avoids overly broad project-in filters that may return zero results. ORDER BY is preserved per sub-query.
  - probe_accessible_projects (bool, default: true): when enabled (or env PROBE_ACCESSIBLE_PROJECTS=1), after discovery the tool probes each discovered project with a tiny query (max 1 result) to ensure the project actually returns at least one visible issue for the current user. Only accessible projects are kept when building the refined JQL. Prints a diagnostic summary like "Project accessibility probe: X of Y projects accessible".
  The fetch order is: client path if prefer_client=true → otherwise try `/rest/api/3/search/jql` (top-level payload) → if 4xx and fail_fast_http=true, go straight to python-jira client; otherwise retry once with explicit `fields`/`expand` in the body → then client fallback → optional batch payload retry.

Sorting configuration
- custom_fields.priority_index_field: Optional custom field id (e.g., "customfield_10104") used to sort issues alphanumerically when generating reports.
  - If absent on an issue, the tool falls back to Jira's native priority (mapped via priority_ranking) and finally to the issue key for a stable order.


## Discovery and field identification
- The discovery phase probes Confluence (via CQL) and Jira to identify related spaces/pages, candidate project keys, and epic keys based on configured keywords.
- Results are cached to .discovery_cache.json for the configured TTL to avoid repeated probing.
- Jira field metadata is inspected to identify likely candidates for:
  - Start date, End date, Due date, Updated, Created, Resolution date,
  - Progress, Status category change date, Assignee, Epic Link.
- These fields drive a consolidated timelines report even if your instance uses custom field IDs; safe fallbacks are used when fields are unavailable.


## Outputs
- Leaderboard CSV → data_files.leaderboard
- Monthly summary CSV(s) → prefixed by data_files.monthly_csv_prefix
- Consolidated timelines CSV → data_files.timelines
- Programme plan Gantt chart (projects) → data_files.gantt_projects (PNG)
- Optional pie charts per month for workstream distribution
- Optional cache file accumulating fetched issues → search.issues_cache (JSON Lines)
 - engineer_names.csv is optional; if missing, the run proceeds without senior filtering and prints a concise warning.


## Environment variables
- JIRA_USERNAME: Default Jira username (email for Atlassian Cloud)
- JIRA_PASSWORD: Default Jira API token or password
- For multiple instances, you can use instance-specific overrides:
  - `JIRA_USERNAME_<INSTANCE_NAME>`
  - `JIRA_PASSWORD_<INSTANCE_NAME>`
  - (The instance name should be normalized: uppercase, spaces/dashes replaced by underscores, e.g., `JIRA_USERNAME_INSTANCE_A`)
- JQL_QUERY: Optional base JQL; overrides config.json:jql_query
- DISCOVERY_KEYWORDS: Optional comma-separated override for discovery.keywords
- DISCOVERY_DISABLE: If set to 1/true/yes, disables discovery regardless of config
- PREFER_CLIENT_SEARCH: If set to 1/true/yes, skip HTTP /search calls and use the python-jira client directly
- DEBUG_TRANSITIONS: If set to 0/false/no, suppresses status transition debug logging. By default, each status change is logged with its timestamp to aid troubleshooting.
 - RECENT_DAYS: Overrides search.recent_days; bounds the fallback windows (e.g., updated >= -180d)
 - MIN_RESULTS: Overrides search.min_results; threshold below which the tool relaxes constraints to broaden the selection
 - FORCE_ULTRA_BROAD: If set to 1/true/yes, bypass discovery and directly run a broad query: updated >= -RECENT_DAYS (ORDER BY preserved).
 - ENABLE_USER_SCOPED_FALLBACK: If set to 0/false/no, disables the user-scoped recent activity fallback.
 - TRY_CREATED_WINDOW: If set to 0/false/no, disables the created >= -RECENT_DAYS fallback.
 - LLM_TIMEOUT_SECONDS: Override the default 60-second timeout for LLM requests (e.g. 300 for Ollama).
 - GOOGLE_API_KEY: Google Search API Key.
 - GOOGLE_CSE_ID: Google Search Engine ID (CX).
  - AVOID_RANK_ORDER: If set to 1/true/yes, replaces trailing "ORDER BY Rank" with "ORDER BY <rank_fallback> DESC" in constructed queries.
  - RANK_FALLBACK: Field name to use when replacing Rank; supports "created" or "updated". Defaults to "created".
  - ENABLE_CACHE: If set to 0/false/no, disables on-disk cache of fetched issues.
  - PREFER_CACHE_FOR_FALLBACKS: If set to 0/false/no, disables using the cache as a last-resort data source.
  - CACHE_MAX_AGE_DAYS: Override max age for using cached issues.
  - ITERATE_PER_PROJECT: If set to 1/true/yes, enable per-project iteration of refined queries as described above.
  - PROBE_ACCESSIBLE_PROJECTS: If set to 0/false/no, disables the post-discovery project accessibility probe described above.

  ### Jira insights (optional)
  If you use the Jira quality report with LLM-backed insights, you can optionally include linked Confluence content in the analysis and tune limits/concurrency via `jira_insights` in `config.json`:

  Example `jira_insights` block
  ```
  {
    "jira_insights": {
      "include_confluence": true,
      "max_confluence_pages_per_issue": 3,
      "max_confluence_chars_per_page": 5000,
      "max_parallel_confluence_fetches": 4
    }
  }
  ```

  ### Topic Research
  The tool can also perform iterative research on a specific topic and requirements, gathering data from Jira, Confluence, LLMs, and simulated web search to formulate a comprehensive document in professional British English.

  ```bash
  jirastats --topic-research topic_requirements.txt --context https://example.com/context --context local_doc.pdf --output researched_doc.md --llm-provider openai
  ```

  - `--topic-research`: Path or URL to a file containing a topic (first line) and requirements (remaining lines). Supports `.txt`, `.docx`, `.pdf`, `.odf`, `.html`, `.jpg`, `.png`, `.svg`, `.mp3`, and `.mp4`.
  - `--context`: (Optional) Additional URLs or file paths to provide context, relevance, boundaries, and focus. Supports the same formats as `--topic-research`. Can be specified multiple times.
  - `--max-iterations`: (Optional) Maximum refinement loops (default: 3).
  - `--llm-timeout`: (Optional) Timeout in seconds for LLM requests (can also be set via `LLM_TIMEOUT_SECONDS` environment variable).
  - Uses existing Jira and Confluence connectivity settings.
  - Features an agentic debate loop where LLMs act as both critic and editor to polish the final document.
  - Integration with Google Search:
    - Automatically performs real web searches if credentials are provided.
    - Fetches and analyzes the full content of relevant search result URLs.

  ### Logging and Progress Monitoring
  The tool provides detailed status updates and debug logging to monitor progress in real-time and analyze execution afterwards.

  ```bash
  # Standard run with real-time status updates
  jirastats --topic-research req.txt --output report.md

  # Verbose run (includes INFO level logs)
  jirastats --topic-research req.txt --output report.md --verbose

  # Debug run (detailed logs for all API and LLM calls)
  jirastats --topic-research req.txt --output report.md --debug --log-file my_research.log
  ```

  - `--verbose` (-v): Enables INFO level status updates on the console.
  - `--debug` (-d): Enables detailed DEBUG level logging, including truncated LLM payloads and API interactions.
  - `--log-file`: Path to the file where all logs (up to DEBUG level) are saved (default: `jirastats.log`).
  - Status updates prefixed with `[*]` are shown on the console during long-running tasks like Topic Research.

  Settings
  - `include_confluence` (bool, default: true): When true, if an issue description links to Confluence pages, their text will be fetched and appended as authoritative context for the LLM.
  - `max_confluence_pages_per_issue` (int, default: 3): Upper bound on the number of linked Confluence pages to fetch per issue.
  - `max_confluence_chars_per_page` (int, default: 5000): Per-page character cap after stripping HTML; longer pages are truncated for cost/latency control.
  - `max_parallel_confluence_fetches` (int, default: 4): Small thread-pool size used to fetch linked pages concurrently per issue to reduce wall-clock latency while keeping load bounded.

Optional inputs
- engineer_names.csv: If present (path configured via data_files.engineer_names), the report will use it to determine active seniors by time window. If absent, the tool continues normally and skips senior-based filtering with a short warning.


## Testing
- pytest
The suite uses lightweight stubs/mocks, so it runs offline without contacting Jira/Confluence.


## Packaging and CLI
- Install in editable mode: pip install -e .
- Console entry point: jirastats
- Module entry point: python -m jirastats.cli
- run_report.py remains for convenience and defers to the same workflow.


## Project structure (high level)
- __init__.py: exposes a minimal API
- main.py: orchestrates configuration, discovery, fetching, processing, and outputs
- discover_hierarchy.py: probes Confluence/Jira to refine scope and discover fields
- analyze_issue_transitions.py, get_monthly_worklog_times.py, seconds_to_work_units.py, normalize_name.py, sorting_key.py: helpers
- cli.py: thin CLI that calls main.main()
- tests/: pytest suite with offline mocks


## License
This project is licensed under the terms of the LICENSE file in this repository.

## Contributing
See CONTRIBUTING.md for guidelines.

