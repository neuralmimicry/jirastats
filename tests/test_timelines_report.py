import csv
from types import SimpleNamespace as NS

import main as m


def make_issue(project_key="PRJ", epic_link=None, status_name="In Progress", done=False,
               assignee="Alice", created="2025-01-01", updated="2025-01-02",
               resolutiondate=None, duedate=None, progress=None, updater="Bob"):
    # fields
    status = NS(name=status_name, statusCategory=NS(key=("done" if done else "indeterminate")))
    project = NS(key=project_key, name=f"{project_key} Name")
    assignee_obj = NS(displayName=assignee)
    fields = NS(
        project=project,
        issuetype=NS(name="Story"),
        summary="Some story",
        status=status,
        assignee=assignee_obj,
        created=created,
        updated=updated,
        resolutiondate=resolutiondate,
        duedate=duedate,
        progress=progress,
    )
    # changelog last history
    last_hist = NS(created=updated, author=NS(displayName=updater))
    changelog = NS(histories=[last_hist])
    issue = NS(fields=fields, changelog=changelog)
    return issue


def test_generate_timelines_report_writes_csv(tmp_path, monkeypatch):
    out = tmp_path / "timelines.csv"
    monkeypatch.setattr(m, "TIMELINES_FILE", str(out))

    issues = [
        make_issue(project_key="PRJ", done=False, duedate="2025-02-01"),
        make_issue(project_key="PRJ", done=True, duedate="2025-03-01", assignee="Charlie", updater="Dana"),
        make_issue(project_key="ABC", done=False, duedate="2025-04-01"),
    ]

    # minimal fields_map
    fields_map = {"start_date": [], "end_date": [], "due_date": ["duedate"], "epic_link": []}

    m.generate_timelines_report(issues, fields_map)

    rows = list(csv.reader(out.open()))
    # Header exists
    assert rows[0][0] == "ScopeType"
    # Expect at least one Project row
    project_rows = [r for r in rows[1:] if r[0] == "Project"]
    assert project_rows, "Expected project aggregation rows in timelines.csv"
    # Ensure assignees count and updaters count columns parse as integers
    # Columns: 0.. 8 UniqueAssigneesCount, 10 UpdatersCount
    for r in project_rows:
        int(r[8])
        int(r[10])


def test_generate_timelines_uses_transitions_when_no_dates(tmp_path, monkeypatch):
    out = tmp_path / "timelines.csv"
    monkeypatch.setattr(m, "TIMELINES_FILE", str(out))

    # Issue with no explicit start/end/due/resolution but with changelog transitions
    issue = make_issue(project_key="PRJ", done=True, duedate=None, updated="2025-02-27T10:00:00.000+0000")
    # Override changelog histories to include To Do -> In Progress -> Done transitions
    from types import SimpleNamespace as NS
    histories = [
        NS(created="2025-02-26T08:00:56.598+0000", items=[NS(field='status', fromString='To Do', toString='In Progress')]),
        NS(created="2025-08-11T18:48:46.227+0100", items=[NS(field='status', fromString='In Test', toString='Done')]),
    ]
    issue.changelog = NS(histories=histories)

    fields_map = {"start_date": [], "end_date": [], "due_date": []}
    m.generate_timelines_report([issue], fields_map)

    rows = list(csv.reader(out.open()))
    # Find first project row
    proj_rows = [r for r in rows[1:] if r[0] == "Project" and r[1] == "PRJ"]
    assert proj_rows, "Expected project row from transitions-inferred dates"
    # Columns: [ScopeType, ScopeKey, Name, Start, End, LastUpdated, ...]
    start_val = proj_rows[0][3]
    end_val = proj_rows[0][4]
    assert start_val != "" and end_val != "", "Start/End should be inferred from transitions"
