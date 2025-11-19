import csv
from pathlib import Path

import main as m


def test_leaderboard_output_writes_csv(tmp_path: Path, monkeypatch):
    out = tmp_path / "leaderboard.csv"
    # Redirect output path
    monkeypatch.setattr(m, "LEADERBOARD_FILE", str(out))

    leaderboard = [
        ("Alice", {"total_time": 8*3600, "qa_returns": 1, "tasks_completed": 2, "throughput": 2.0}),
        ("Bob",   {"total_time": 4*3600, "qa_returns": 0, "tasks_completed": 1, "throughput": 1.0}),
    ]

    m.leaderboard_output(leaderboard)

    assert out.exists()
    rows = list(csv.reader(out.open()))
    # Header plus two rows
    assert rows[0][:3] == ["Name", "Total Coding Duration", "QA Returns"]
    assert rows[1][0] == "Alice"
    assert rows[2][0] == "Bob"
