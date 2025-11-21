import csv
from types import SimpleNamespace as NS

import main as m


def make_issue(project_key="PRJ", updated="2025-03-01T10:00:00.000+0000", progress=(20, 100), epic_link=None):
    status = NS(statusCategory=NS(key="indeterminate"))
    project = NS(key=project_key, name=f"{project_key} Name")
    fields = NS(
        project=project,
        issuetype=NS(name="Story"),
        status=status,
        assignee=None,
        created=None,  # no explicit created used for start
        updated=updated,
        resolutiondate=None,
        duedate=None,
        progress=NS(progress=progress[0], total=progress[1]),
    )
    # Minimal changelog with last history author only; no status transitions
    changelog = NS(histories=[NS(created=updated, author=NS(displayName="Updater"))])
    issue = NS(fields=fields, changelog=changelog)
    return issue


def test_timelines_infers_dates_from_progress_series_when_no_dates(tmp_path, monkeypatch):
    out = tmp_path / "timelines.csv"
    monkeypatch.setattr(m, "TIMELINES_FILE", str(out))

    # Two issues in same project with different updated timestamps and percent done
    i1 = make_issue(project_key="PRJ", updated="2025-03-01T10:00:00.000+0000", progress=(20, 100))
    i2 = make_issue(project_key="PRJ", updated="2025-04-01T10:00:00.000+0000", progress=(60, 100))

    fields_map = {"start_date": [], "end_date": [], "due_date": [], "epic_link": []}

    m.generate_timelines_report([i1, i2], fields_map)

    rows = list(csv.reader(out.open()))
    # Find the PRJ project row
    proj_rows = [r for r in rows[1:] if r[0] == "Project" and r[1] == "PRJ"]
    assert proj_rows, "Expected project row for PRJ"
    # Start and End should be populated via progress inference since no explicit dates or transitions were provided
    start_val = proj_rows[0][3]
    end_val = proj_rows[0][4]
    assert start_val != "" and end_val != "", "Expected inferred start and end from progress series"
