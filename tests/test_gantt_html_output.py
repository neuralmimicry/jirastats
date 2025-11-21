import os
from types import SimpleNamespace as NS

import main as m


def make_issue(project_key="PRJ", created="2025-01-01", duedate="2025-02-01", done=False, epic_key=None):
    status = NS(statusCategory=NS(key=("done" if done else "indeterminate")))
    fields = NS(
        project=NS(key=project_key, name=f"{project_key} Name"),
        issuetype=NS(name="Story"),
        status=status,
        created=created,
        duedate=duedate,
    )
    if epic_key is not None:
        # The discovery mapping may include customfield_10014 as Epic Link
        setattr(fields, "customfield_10014", epic_key)
    return NS(fields=fields, changelog=NS(histories=[]))


def test_gantt_html_is_created_and_clickable(tmp_path, monkeypatch):
    # Redirect output paths
    gantt_png = tmp_path / "gantt.png"
    gantt_html = tmp_path / "gantt.html"
    timelines_csv = tmp_path / "timelines.csv"
    monkeypatch.setattr(m, "GANTT_FILE", str(gantt_png))
    monkeypatch.setattr(m, "GANTT_HTML_FILE", str(gantt_html))
    monkeypatch.setattr(m, "TIMELINES_FILE", str(timelines_csv))

    # Two issues: one basic for project timeline, one linked to an epic to populate epic rows
    issues = [
        make_issue(project_key="PRJ", created="2025-01-01", duedate="2025-02-01"),
        make_issue(project_key="PRJ", created="2025-01-10", duedate="2025-02-10", epic_key="PRJ-1"),
    ]
    fields_map = {"start_date": [], "end_date": [], "due_date": ["duedate"], "epic_link": ["customfield_10014"]}

    m.generate_timelines_report(issues, fields_map)

    # HTML exists and is not empty
    assert os.path.exists(gantt_html), "Expected a Gantt HTML file to be created"
    assert os.path.getsize(gantt_html) > 0, "Gantt HTML should not be empty"

    content = gantt_html.read_text(encoding="utf-8")
    # Contains a project query link and an epic browse link
    assert "/issues/?jql=" in content and "project%20%3D%20PRJ" in content
    assert "/browse/PRJ-1" in content

    # Contains mouse-over tooltip data for at least one bar
    assert "title=\"" in content
    assert "Percent done:" in content
    assert "Start:" in content and "End:" in content

    # Sticky header and legend should be present
    assert "gantt-header" in content  # sticky header container
    # Expand/collapse controls should exist
    assert "id=\"expandAll\"" in content and "id=\"collapseAll\"" in content
    # Project toggle button and epic hierarchy markers
    assert "class=\"toggle\"" in content
    assert "data-parent=\"PRJ\"" in content or "data-parent=\"PRJ\"" in content
