from unittest.mock import Mock
import os
import importlib

import main as m


def make_issue_with_two_status_changes():
    issue = Mock()
    issue.changelog = Mock()
    issue.changelog.histories = [
        Mock(created="2023-05-01T09:30:00.000+0000", items=[
            Mock(field='status', fromString='Ready to Develop', toString='In Progress')
        ]),
        Mock(created="2023-05-01T16:30:00.000+0000", items=[
            Mock(field='status', fromString='In Progress', toString='For Peer Review')
        ]),
    ]
    return issue


def test_debug_transitions_prints_when_enabled(capsys, monkeypatch):
    # Ensure DEBUG_TRANSITIONS enabled
    monkeypatch.setenv('DEBUG_TRANSITIONS', '1')
    importlib.reload(m)

    issue = make_issue_with_two_status_changes()
    m.analyze_issue_transitions(issue)
    out = capsys.readouterr().out
    assert "2023-05-01T09:30:00.000+0000: From: Ready to Develop, To: In Progress" in out
    assert "2023-05-01T16:30:00.000+0000: From: In Progress, To: For Peer Review" in out


def test_debug_transitions_suppressed_when_disabled(capsys, monkeypatch):
    # Disable transition logging
    monkeypatch.setenv('DEBUG_TRANSITIONS', '0')
    importlib.reload(m)

    issue = make_issue_with_two_status_changes()
    m.analyze_issue_transitions(issue)
    out = capsys.readouterr().out
    # Ensure no generic From/To lines printed
    assert "From:" not in out
    assert "To:" not in out
