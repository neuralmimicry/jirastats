"""
JiraStats package initializer.

Exposes a small, stable API surface for programmatic use while keeping the
existing script structure intact. This enables reuse from other tools without
relying on internal file layout.

Public API:
- main: end-to-end reporting workflow (connects to JIRA, fetches, processes, outputs)
- analyze_issue_transitions: pure function computing time-to-code and QA returns
- get_monthly_worklog_times: aggregates worklog times grouped by workstream
- seconds_to_work_units: utility conversion
- normalize_name, sorting_key: small helpers used across reporting

Note: Configuration is read in main.py at import time via load_config();
see README.md for details and config.json format.
"""
from .main import (
    main,
    analyze_issue_transitions,
    get_monthly_worklog_times,
    seconds_to_work_units,
    normalize_name,
    sorting_key,
)
