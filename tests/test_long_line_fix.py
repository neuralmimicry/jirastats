import pytest
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@pytest.fixture
def researcher():
    with patch("topic_researcher.get_provider"):
        r = TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "pass"),
            llm_provider="openai"
        )
        return r

def test_sanity_check_collapses_long_table_separator(researcher):
    long_sep = "| :--- | " + ("-" * 1000) + " |"
    draft = f"Header\n\n{long_sep}\n\nRow"
    
    cleaned = researcher._sanity_check_document(draft)
    
    assert len(cleaned) < 500
    assert "| :--- | --- |" in cleaned

def test_sanity_check_collapses_long_table_row_spaces(researcher):
    long_row = "| Ref | Desc | Date | " + (" " * 1000) + " |"
    draft = f"Header\n\n{long_row}\n\nRow"
    
    cleaned = researcher._sanity_check_document(draft)
    
    assert len(cleaned) < 500
    assert "| Ref | Desc | Date |  |" in cleaned

def test_sanity_check_collapses_long_dash_lines(researcher):
    long_dash = "-" * 1000
    draft = f"Text\n{long_dash}\nMore Text"
    
    cleaned = researcher._sanity_check_document(draft)
    
    assert len(cleaned) < 500
    assert "Text\n------\nMore Text" in cleaned

def test_sanity_check_collapses_long_dash_line_at_start(researcher):
    long_dash = "-" * 1000
    draft = f"{long_dash}\nText"
    
    cleaned = researcher._sanity_check_document(draft)
    
    assert len(cleaned) < 500
    assert "------\nText" in cleaned

def test_sanity_check_removes_empty_table_rows_but_keeps_content(researcher):
    draft = """
| Key | Summary | Desc |
|:---|:---|:---|
| PROJ-1 | Something | Some desc |
| | | |
| PROJ-2 | Other | |
| - | - | - |
"""
    cleaned = researcher._sanity_check_document(draft)
    
    assert "PROJ-1" in cleaned
    assert "PROJ-2" in cleaned
    assert "| | | |" not in cleaned
    assert "| - | - | - |" not in cleaned
