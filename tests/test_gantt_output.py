import os
from types import SimpleNamespace as NS

import main as m


def make_issue(project_key="PRJ", created="2025-01-01", duedate="2025-02-01", done=False):
    status = NS(statusCategory=NS(key=("done" if done else "indeterminate")))
    fields = NS(
        project=NS(key=project_key, name=f"{project_key} Name"),
        issuetype=NS(name="Story"),
        status=status,
        created=created,
        duedate=duedate,
    )
    return NS(fields=fields, changelog=NS(histories=[]))


def test_gantt_image_is_created(tmp_path, monkeypatch):
    gantt_path = tmp_path / "gantt.png"
    monkeypatch.setattr(m, "GANTT_FILE", str(gantt_path))
    timelines_path = tmp_path / "timelines.csv"
    monkeypatch.setattr(m, "TIMELINES_FILE", str(timelines_path))

    issues = [
        make_issue(project_key="PRJ", created="2025-01-01", duedate="2025-02-01"),
        make_issue(project_key="ABC", created="2025-03-01", duedate="2025-04-15", done=True),
    ]

    fields_map = {"start_date": [], "end_date": [], "due_date": ["duedate"], "epic_link": []}

    m.generate_timelines_report(issues, fields_map)

    assert os.path.exists(gantt_path), "Expected a Gantt chart image to be created"
    assert os.path.getsize(gantt_path) > 0, "Gantt image should not be empty"
