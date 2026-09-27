import pytest
import os
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    return llm

@pytest.fixture
def researcher(mock_llm):
    with patch("topic_researcher.get_provider", return_value=mock_llm):
        return TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "token"),
            llm_provider="openai"
        )

def test_bibliography_generation(researcher):
    # Manually add some sources
    researcher._record_jira_contribution("PROJ-1", "Jira Summary 1")
    researcher._record_confluence_contribution("123", "Confluence Title 1")
    researcher._record_web_contribution("https://example.com/page", "Web Title 1")
    
    bib = researcher._generate_bibliography()
    
    assert "# Bibliography and References" in bib
    assert "## Jira Sources" in bib
    assert "[PROJ-1: Jira Summary 1](https://test.atlassian.net/browse/PROJ-1)" in bib
    assert "## Confluence Sources" in bib
    assert "[Confluence Title 1](https://test.atlassian.net/wiki/pages/viewpage.action?pageId=123)" in bib
    assert "## Web Sources" in bib
    assert "[Web Title 1](https://example.com/page)" in bib

@patch("topic_researcher.jira_fetch_issues")
@patch("topic_researcher._conf_get")
def test_bibliography_saving_in_run(mock_conf_get, mock_jira_fetch, researcher, tmp_path, mock_llm):
    # Setup mocks for run
    mock_llm.predict.side_effect = [
        MagicMock(text='NONE'), # _identify_subjects
        MagicMock(text='NONE'), # _extract_names_from_text
        MagicMock(text='{"jql": "project=PROJ", "cql": "space=SPACE", "search_queries": [], "llm_questions": []}'), # _generate_queries
        MagicMock(text='YES'), # Relevance check
        MagicMock(text='{"jql": "", "cql": ""}'), # _generate_followup_queries
        MagicMock(text='# Draft\nContent'), # _formulate_document
        MagicMock(text='Critic feedback'), # _agentic_debate_and_refine (Critic)
        MagicMock(text='# Draft\nRefined content'), # _agentic_debate_and_refine (Editor)
        MagicMock(text='YES'), # _is_complete
        MagicMock(text='# Draft\nRefined content') # Final _sanity_check_document
    ]
    
    # Jira mock
    issue = MagicMock()
    issue.key = "PROJ-123"
    issue.summary = "Test Issue"
    issue.description = "Desc"
    issue.status = "Open"
    mock_jira_fetch.return_value = [issue]
    
    # Confluence mock
    mock_conf_get.return_value = {
        "results": [
            {
                "content": {
                    "id": "987",
                    "title": "Test Page",
                    "body": {"storage": {"value": "Body"}},
                    "_links": {"webui": "/wiki/spaces/S/pages/987/Test+Page"}
                }
            }
        ]
    }
    
    source_file = tmp_path / "req.txt"
    source_file.write_text("Topic: Test\nRequirements: Test")
    
    output_file = tmp_path / "report.md"
    references_file = tmp_path / "references.md"
    
    researcher.run(str(source_file), str(output_file), max_iterations=1, references_path=str(references_file))
    
    assert output_file.exists()
    assert references_file.exists()
    
    ref_content = references_file.read_text()
    assert "PROJ-123: Test Issue" in ref_content
    assert "https://test.atlassian.net/browse/PROJ-123" in ref_content
    assert "Test Page" in ref_content
    assert "https://test.atlassian.net/wiki/spaces/S/pages/987/Test+Page" in ref_content
