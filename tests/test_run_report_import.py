def test_run_report_import_and_attr():
    # Ensure importing CLI module does not execute workflow and exposes run
    import run_report  # noqa: F401
    assert hasattr(run_report, 'run')
