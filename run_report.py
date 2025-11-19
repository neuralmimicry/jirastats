"""
CLI entry point to run the full JiraStats reporting workflow.

Usage:
  - Configure via config.json and environment variables (see README.md).
  - Optionally set JQL via:
      1) config.json -> "jql_query"
      2) env var JQL_QUERY (overrides config)
  - Execute:
      python run_report.py

This script defers to jirastats.main.main(), which reads configuration and
produces output CSVs and charts as configured.
"""
from main import main as run


if __name__ == "__main__":
    run()
