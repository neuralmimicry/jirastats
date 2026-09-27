import pytest
import json
from unittest.mock import MagicMock, patch
from topic_researcher import TopicResearcher

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    # Mock context window and estimate_tokens
    llm.get_context_window.return_value = 8192
    llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    
    def predict_side_effect(messages, system=None, **kwargs):
        system_str = (system or "").lower()
        # Handle different roles based on system prompt
        if "identify all specific individuals" in system_str:
            return MagicMock(text='NONE')
        if "identify all individual person names" in system_str:
            return MagicMock(text='NONE')
        if "expert technical researcher" in system_str:
            return MagicMock(text='{"jql": "", "cql": "", "search_queries": [], "llm_questions": []}')
        if "professional british technical writer" in system_str:
            # Check if it's a targeted expansion
            if "TARGET SECTION TO EXPAND" in messages[0]['content']:
                return MagicMock(text='## Section 1\n\nThis is the detailed content for Section 1, fetched from multiple sources.')
            return MagicMock(text='Initial draft.')
        if "critical reviewer" in system_str:
            return MagicMock(text='Improvement: add more context.')
        if "professional british editor" in system_str:
            # Both refinement and sanity check use this role
            if "sanity check" in system_str:
                return MagicMock(text=messages[0]['content'] + " Polished.")
            return MagicMock(text='Refined draft with context.')
        if "evaluate if the provided document is comprehensive" in system_str:
            return MagicMock(text='YES')
        if "technical research assistant" in system_str:
            # Relevance check for resumption or search results
            return MagicMock(text='YES')
        
        return MagicMock(text='Default mock response')

    llm.predict.side_effect = predict_side_effect
    return llm

def test_sanitize_reversed_in(researcher):
    # The reported problematic JQL
    jql = '("Das Wijesundera" OR "Jozsef Tamasi") IN (assignee, reporter) AND status = "Done"'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    expected = '(assignee IN ("Das Wijesundera", "Jozsef Tamasi") OR reporter IN ("Das Wijesundera", "Jozsef Tamasi")) AND status = "Done"'
    assert sanitized["jql"] == expected

def test_sanitize_multi_field_in(researcher):
    jql = '(assignee, reporter, watcher) IN ("User A", "User B")'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    expected = '(assignee IN ("User A", "User B") OR reporter IN ("User A", "User B") OR watcher IN ("User A", "User B"))'
    assert sanitized["jql"] == expected

def test_sanitize_in_with_or(researcher):
    jql = 'assignee IN ("User A" OR "User B")'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    expected = 'assignee IN ("User A", "User B")'
    assert sanitized["jql"] == expected

def test_sanitize_in_preservation(researcher):
    # Standard JQL should remain unchanged
    jql = 'assignee IN ("User A", "User B")'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    assert sanitized["jql"] == jql
    
    # Wrapped field should also be preserved (valid though uncommon)
    jql2 = '(assignee) IN ("User A", "User B")'
    queries2 = {"jql": jql2}
    sanitized2 = researcher._sanitize_queries(queries2)
    assert sanitized2["jql"] == jql2

def test_sanitize_cql_reversed_in(researcher):
    cql = '("User A" OR "User B") IN (creator, contributor)'
    queries = {"cql": cql}
    sanitized = researcher._sanitize_queries(queries)
    
    # creator and contributor are in person_fields
    expected = '(creator IN ("User A", "User B") OR contributor IN ("User A", "User B"))'
    assert sanitized["cql"] == expected

@patch('topic_researcher.get_provider')
@patch('topic_researcher.requests.Session')
def test_topic_researcher_with_context(mock_session_class, mock_get_provider, mock_llm, tmp_path):
    mock_get_provider.return_value = mock_llm
    
    # Mock context fetching from URL
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session
    mock_resp = MagicMock()
    mock_resp.text = "Additional context from URL"
    mock_resp.content = b"Additional context from URL"
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "text/plain"}
    mock_session.get.return_value = mock_resp
    
    # Local context file
    local_context = tmp_path / "local_context.txt"
    local_context.write_text("Local context info")
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    source_file = tmp_path / "source.txt"
    source_file.write_text("Topic: Test\nRequirements: Test reqs")
    
    output_file = tmp_path / "output.md"
    
    researcher.run(
        str(source_file), 
        str(output_file), 
        max_iterations=1, 
        context_sources=["https://example.com/context", str(local_context)]
    )
    
    assert output_file.exists()
    content = output_file.read_text()
    assert "Refined draft with context." in content
    
    # Verify context was passed to LLM (check calls to predict)
    # identify_subjects is the first call, extract_names is the second, generate_queries is the third
    call_args = mock_llm.predict.call_args_list[2]
    user_content = call_args[0][0][0]['content']
    assert "Additional Context:" in user_content
    assert "Additional context from URL" in user_content
    assert "Local context info" in user_content

@patch('topic_researcher.get_provider')
def test_agentic_debate_logic(mock_get_provider, mock_llm, tmp_path):
    mock_get_provider.return_value = mock_llm
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # Test _agentic_debate_and_refine directly
    mock_llm.predict.side_effect = [
        MagicMock(text='Critic feedback'),
        MagicMock(text='Edited content')
    ]
    
    result = researcher._agentic_debate_and_refine("topic", "reqs", "initial draft", "context")
    assert result == "Edited content"
    
    # Check that Critic received the context
    critic_call = mock_llm.predict.call_args_list[0]
    assert "context" in critic_call[0][0][0]['content']
    assert "Critical Reviewer" in critic_call[1]['system']
    
    # Check that Editor received feedback
    editor_call = mock_llm.predict.call_args_list[1]
    assert "Critic feedback" in editor_call[0][0][0]['content']
    assert "Professional British Editor" in editor_call[1]['system']

@patch('topic_researcher.get_provider')
def test_identify_thin_sections(mock_get_provider):
    mock_llm = MagicMock()
    mock_get_provider.return_value = mock_llm
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    draft = """# Report
## Thin 1
## Substantial
Line 1 of substantial content that is long enough. It needs to be much longer to pass the character count threshold.
Line 2 of substantial content that is long enough. It needs to be much longer to pass the character count threshold.
Line 3 of substantial content that is long enough. It needs to be much longer to pass the character count threshold.
Line 4 of substantial content that is long enough. It needs to be much longer to pass the character count threshold.
Line 5 of substantial content that is long enough. It needs to be much longer to pass the character count threshold.
Line 6 of substantial content that is long enough. It needs to be much longer to pass the character count threshold.
## Thin 2
Short.
"""
    thin = researcher._identify_thin_sections(draft)
    assert "Thin 1" in thin
    assert "Thin 2" in thin
    assert "Substantial" not in thin
    assert "Report" not in thin # Main title should be excluded

@patch('topic_researcher.get_provider')
@patch('topic_researcher.jira_fetch_issues')
@patch('topic_researcher._conf_get')
def test_detect_and_expand_outline(mock_conf_get, mock_jira_fetch, mock_get_provider, mock_llm, tmp_path):
    mock_get_provider.return_value = mock_llm
    
    mock_llm.predict.side_effect = [
        # NEW: Identify subjects
        MagicMock(text="NONE"),
        # NEW: Identify names
        MagicMock(text="NONE"),
        # Iteration 1: _generate_queries
        MagicMock(text='{"jql": "project = TEST", "cql": "text ~ TEST", "search_queries": ["test topic"], "llm_questions": ["What is test?"]}'),
        # Iteration 1: search result simulation
        MagicMock(text='Some search results'),
        # Iteration 1: Relevance check (Snippet)
        MagicMock(text='YES'),
        # Iteration 1: llm question answer
        MagicMock(text='Some answer'),
        # NEW: Iteration 1: follow-up query generation
        MagicMock(text='{"jql": "", "cql": ""}'),
        # Iteration 1: _formulate_document returns just an outline
        MagicMock(text='# Test Document\n\nLine 1 of intro.\nLine 2 of intro.\nLine 3 of intro.\n\n## Section 1\n\n## Section 2\n\n## Conclusion'),
        # Iteration 1: _agentic_debate_and_refine (Critic)
        MagicMock(text='No issues.'),
        # Iteration 1: _agentic_debate_and_refine (Editor)
        MagicMock(text='# Test Document\n\nLine 1 of intro.\nLine 2 of intro.\nLine 3 of intro.\n\n## Section 1\n\n## Section 2\n\n## Conclusion'),
        # NEW: Sanity check (after Iter 1)
        MagicMock(text='# Test Document\n\nLine 1 of intro.\nLine 2 of intro.\nLine 3 of intro.\n\n## Section 1\n\n## Section 2\n\n## Conclusion'),
        # Iteration 1: _is_complete
        MagicMock(text='NO. It is just an outline.'),

        # Iteration 2: _generate_queries (should target Section 1 now)
        MagicMock(text='{"jql": "text ~ \'Section 1\'", "cql": "text ~ \'Section 1\'", "search_queries": ["Section 1 detail"], "llm_questions": ["Tell me about Section 1"]}'),
        # Iteration 2: search result simulation
        MagicMock(text='Detailed info for Section 1'),
        # Iteration 2: Relevance check (Snippet)
        MagicMock(text='YES'),
        # Iteration 2: llm question answer
        MagicMock(text='Detailed answer for Section 1'),
        # NEW: Iteration 2: follow-up query generation
        MagicMock(text='{"jql": "", "cql": ""}'),
        # Iteration 2: _formulate_document (should flesh out Section 1)
        MagicMock(text='## Section 1\n\nThis is the detailed content for Section 1, fetched from multiple sources.'),
        # Iteration 2: _agentic_debate_and_refine (Critic)
        MagicMock(text='Looks better.'),
        # Iteration 2: _agentic_debate_and_refine (Editor)
        MagicMock(text='# Test Document\n\nLine 1 of intro.\nLine 2 of intro.\nLine 3 of intro.\n\n## Section 1\n\nThis is the refined detailed content for S1.\n\n## Section 2\n\n## Conclusion'),
        # NEW: Sanity check (after Iter 2)
        MagicMock(text='# Test Document\n\nLine 1 of intro.\nLine 2 of intro.\nLine 3 of intro.\n\n## Section 1\n\nThis is the refined detailed content for S1.\n\n## Section 2\n\n## Conclusion'),
        # Iteration 2: _is_complete
        MagicMock(text='YES'),
        # Final Sanity Check
        MagicMock(text='# Test Document\n\nLine 1 of intro.\nLine 2 of intro.\nLine 3 of intro.\n\n## Section 1\n\nThis is the refined detailed content for S1.\n\n## Section 2\n\n## Conclusion')
    ]
    
    mock_jira_fetch.return_value = []
    mock_conf_get.return_value = {"results": []}

    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    source_file = tmp_path / "source.txt"
    source_file.write_text("Topic: Test Outline\nRequirements: Provide detailed info.")
    
    output_file = tmp_path / "output.md"
    
    researcher.run(str(source_file), str(output_file), max_iterations=2)
    
    content = output_file.read_text()
    assert "Section 1" in content
    assert "refined detailed content for S1" in content
    assert content.count("## Section 1") == 1
    assert "Section 2" in content
    assert "# Test Document" in content

@patch('topic_researcher.get_provider')
def test_cql_page_auto_correction(mock_get_provider):
    mock_llm = MagicMock()
    mock_get_provider.return_value = mock_llm
    
    # Mock LLM returning invalid CQL with 'page'
    mock_llm.predict.return_value = MagicMock(text='{"jql": "", "cql": "space = \\"CDNP\\" AND page = \\"Test Page\\"", "search_queries": [], "llm_questions": []}')
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = researcher._generate_queries("Topic", "Requirements")
    
    assert "page =" not in queries["cql"]
    assert "title =" in queries["cql"]

@patch('topic_researcher.get_provider')
def test_cql_page_tilde_auto_correction(mock_get_provider):
    mock_llm = MagicMock()
    mock_get_provider.return_value = mock_llm
    
    # Mock LLM returning invalid CQL with 'page'
    mock_llm.predict.return_value = MagicMock(text='{"jql": "", "cql": "space = \\"CDNP\\" AND page ~ \\"Test Page\\"", "search_queries": [], "llm_questions": []}')
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    queries = researcher._generate_queries("Topic", "Requirements")
    
    assert "page ~" not in queries["cql"]
    assert "title ~" in queries["cql"]

@patch('topic_researcher.get_provider')
@patch('topic_researcher._conf_get')
def test_cql_400_fallback(mock_conf_get, mock_get_provider):
    mock_llm = MagicMock()
    mock_get_provider.return_value = mock_llm
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    # Simulate 400 error for invalid CQL
    from requests.exceptions import HTTPError
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.reason = "Bad Request"
    mock_resp.url = "https://test.atlassian.net/wiki/rest/api/search?cql=page=..."
    err = HTTPError("400 Client Error: Bad Request", response=mock_resp)
    mock_conf_get.side_effect = err
    
    # Mock fallback research
    mock_llm.predict.return_value = MagicMock(text='{"search_terms": "test", "jira_keywords": "test", "confluence_keywords": "test", "reasoning": "400 error"}')
    
    queries = {"cql": "page = 'bad'"}
    results = researcher._execute_queries(queries)
    
    assert "confluence_pages" in results
    

@patch('topic_researcher.get_provider')
@patch('topic_researcher.jira_fetch_issues')
@patch('topic_researcher._conf_get')
def test_topic_researcher_resume_from_existing(mock_conf_get, mock_jira_fetch, mock_get_provider, mock_llm, tmp_path):
    mock_get_provider.return_value = mock_llm
    
    # Mock sequence:
    # 1. _is_content_relevant (for existing file) -> YES
    # 2. _generate_queries
    # 3. _execute_queries (search, llm_questions)
    # 4. _formulate_document (replacement for thin section)
    # 5. _agentic_debate_and_refine (Critic)
    # 6. _agentic_debate_and_refine (Editor)
    # 7. _is_complete -> YES
    mock_llm.predict.side_effect = [
        MagicMock(text="NONE"), # identify subjects
        MagicMock(text="NONE"), # extract names
        MagicMock(text='YES'), # Relevance check for existing file
        MagicMock(text='{"jql": "", "cql": "", "search_queries": [], "llm_questions": []}'), # queries
        MagicMock(text='## Thin Section\n\nResumed and updated content for Thin Section.'), # formulation
        MagicMock(text='Critic feedback'), # refinement Critic
        MagicMock(text='# Existing Report\n\n## Substantial Section\nSubstantial...\n\n## Thin Section\nRefined content.'), # refinement Editor
        MagicMock(text='YES'), # complete
        MagicMock(text='# Existing Report\n\n## Substantial Section\nSubstantial...\n\n## Thin Section\nRefined content. Polished.') # final sanity check
    ]
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    source_file = tmp_path / "req.txt"
    source_file.write_text("Topic: Resume Test\nRequirements: Use existing file.")
    
    output_file = tmp_path / "research_report.md"
    intro = ("Substantial content line. " * 20 + "\n") * 10
    existing_content = f"# Existing Report\n{intro}\n\n## Substantial Section\n" + ("Substantial content line. " * 20 + "\n") * 10 + "\n\n## Thin Section\nShort."
    output_file.write_text(existing_content)
    
    researcher.run(str(source_file), str(output_file), max_iterations=1)
    
    # Verify it loaded the existing draft by checking if it targeted the thin section
    content = output_file.read_text()
    assert "Existing Report" in content
    assert "Substantial Section" in content
    assert "Thin Section" in content
    assert "Refined content" in content

@patch('topic_researcher.get_provider')
@patch('topic_researcher.jira_fetch_issues')
@patch('topic_researcher._conf_get')
def test_topic_researcher_no_resume_if_irrelevant(mock_conf_get, mock_jira_fetch, mock_get_provider, mock_llm, tmp_path):
    mock_get_provider.return_value = mock_llm
    
    # Mock sequence:
    # 1. _is_content_relevant (for existing file) -> NO
    # 2. _generate_queries
    # 3. _execute_queries (search, llm_questions)
    # 4. _formulate_document (fresh draft)
    # 5. _agentic_debate_and_refine (Critic)
    # 6. _agentic_debate_and_refine (Editor)
    # 7. _is_complete -> YES
    mock_llm.predict.side_effect = [
        MagicMock(text="NONE"), # identify subjects
        MagicMock(text="NONE"), # extract names
        MagicMock(text='NO'), # Relevance check for existing file
        MagicMock(text='{"jql": "", "cql": "", "search_queries": [], "llm_questions": []}'), # queries
        MagicMock(text='# Fresh Report\n\nStarting from scratch.'), # formulation
        MagicMock(text='Critic feedback'), # refinement Critic
        MagicMock(text='# Fresh Report\n\nStarting from scratch.'), # refinement Editor
        MagicMock(text='YES'), # complete
        MagicMock(text='# Fresh Report\n\nStarting from scratch. Polished.') # final sanity check
    ]
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    source_file = tmp_path / "req.txt"
    source_file.write_text("Topic: Resume Test\nRequirements: Use existing file.")
    
    output_file = tmp_path / "research_report.md"
    existing_content = "# Totally Different Topic\n\nIrrelevant content."
    output_file.write_text(existing_content)
    
    researcher.run(str(source_file), str(output_file), max_iterations=1)
    
    content = output_file.read_text()
    assert "Fresh Report" in content
    assert "Irrelevant content" not in content
    assert "Totally Different Topic" not in content

@patch('topic_researcher.get_provider')
@patch('topic_researcher.jira_fetch_issues')
@patch('topic_researcher._conf_get')
def test_topic_researcher_immediate_complete_if_resumed_perfect(mock_conf_get, mock_jira_fetch, mock_get_provider, mock_llm, tmp_path):
    mock_get_provider.return_value = mock_llm
    
    # Mock sequence:
    # 1. _is_content_relevant -> YES
    # 2. _generate_queries
    # 3. _execute_queries (empty results)
    # 4. _formulate_document (returns substantial content)
    # 5. _agentic_debate_and_refine (Critic -> No issues)
    # 6. _is_complete -> YES
    mock_llm.predict.side_effect = [
        MagicMock(text="NONE"), # identify subjects
        MagicMock(text="NONE"), # extract names
        MagicMock(text='YES'), # Relevance check
        MagicMock(text='{"jql": "", "cql": "", "search_queries": [], "llm_questions": []}'), # queries
        MagicMock(text='# Perfect Report\n' + ("Substantial content line that is quite long to avoid being thin. " * 10 + "\n") * 10), # formulation
        MagicMock(text=''), # refinement Critic returns empty -> no issues
        MagicMock(text='YES'), # complete
        MagicMock(text='# Perfect Report\nPolished.') # final sanity check
    ]
    
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    
    source_file = tmp_path / "req.txt"
    source_file.write_text("Topic: Perfect Test\nRequirements: Use existing file.")
    
    output_file = tmp_path / "research_report.md"
    # Make it not thin
    intro = ("Substantial content line that is quite long to avoid being thin. " * 10 + "\n") * 10
    existing_content = f"# Perfect Report\n{intro}"
    output_file.write_text(existing_content)
    
    # max_iterations=3 but should stop after 1
    researcher.run(str(source_file), str(output_file), max_iterations=3)
        
    # predict should have been called exactly 8 times:
    # 1. Identify subjects
    # 2. Extract names
    # 3. Resumption relevance
    # 4. Generate queries
    # 5. Formulate doc
    # 6. Debate Critic (returns empty, skipping Editor)
    # 7. Is complete
    # 8. Final Sanity Check
    assert mock_llm.predict.call_count == 8
    content = output_file.read_text()
    assert "Perfect Report" in content


@pytest.fixture
def researcher():
    with patch("topic_researcher.get_provider"):
        r = TopicResearcher(
            jira_base_url="https://test.atlassian.net",
            jira_auth=("user", "token"),
            llm_provider="openai"
        )
        # Populate with names used in sanitization tests to avoid them being stripped
        r.name_cache = {"Das Wijesundera", "Jozsef Tamasi", "User A", "User B", "Sarah Chen", "David Rodriguez"}
        return r


def test_extract_queries_from_markdown(researcher):
    text = """
    Here are the queries for your research:
    
    **Jira (JQL)**
    `project = "CDNP" AND status != "Closed"`
    
    **Confluence (CQL)**
    `space = "CDNP" AND title ~ "Strategy"`
    
    **Web Search**
    * VMO2 Cross-Domain Test Strategy
    * TM Forum ODA concepts
    
    **LLM Questions**
    1. What are the quality gates?
    2. How to define canonical models?
    """
    
    queries = researcher._extract_queries_from_text(text)
    assert queries is not None
    assert queries["jql"] == 'project = "CDNP" AND status != "Closed"'
    assert queries["cql"] == 'space = "CDNP" AND title ~ "Strategy"'
    assert "VMO2 Cross-Domain Test Strategy" in queries["search_queries"]
    assert "What are the quality gates?" in queries["llm_questions"]

def test_extract_queries_from_markdown_alt_format(researcher):
    text = """
    Jira Query: project = "TEST"
    Confluence Query: title ~ "Test"
    Keywords:
    - key1
    - key2
    Specific Questions:
    - q1
    - q2
    """
    queries = researcher._extract_queries_from_text(text)
    assert queries is not None
    assert queries["jql"] == 'project = "TEST"'
    assert queries["cql"] == 'title ~ "Test"'
    assert "key1" in queries["search_queries"]
    assert "q1" in queries["llm_questions"]

def test_try_fix_truncated_json_with_quote(researcher):
    truncated = '{"jql": "project = CDNP'
    fixed = researcher._try_fix_truncated_json(truncated)
    assert fixed is not None
    assert fixed == '{"jql": "project = CDNP"}'
    data = json.loads(fixed)
    assert data["jql"] == "project = CDNP"

def test_try_fix_truncated_json_with_comma(researcher):
    truncated = '{"jql": "project = PROJ",'
    fixed = researcher._try_fix_truncated_json(truncated)
    assert fixed is not None
    assert fixed == '{"jql": "project = PROJ"}'

def test_sanitize_hallucinated_backslashes(researcher):
    # LLM often over-escapes quotes in strings
    jql = 'updated <= "2025-12-31\\" AND text ~ \\"diversity" OR summary ~ "inclusion\\"'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    # Expected: 'updated <= "2025-12-31" AND text ~ "diversity" OR summary ~ "inclusion"'
    # Note: fix_quotes will also process it.
    assert '\\"' not in sanitized["jql"]
    assert 'updated <= "2025-12-31"' in sanitized["jql"]
    assert 'text ~ "diversity"' in sanitized["jql"]
    assert 'summary ~ "inclusion"' in sanitized["jql"]

def test_fix_quotes_with_multiple_parentheses(researcher):
    # The bug where ((field ~ "val")) would cause runaway string
    jql = 'updated <= "2025-12-31" AND ((text ~ "diversity" OR text ~ "inclusion"))'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    # It should correctly identify all quotes and NOT add extra backslashes
    assert sanitized["jql"] == jql
    assert '\\"' not in sanitized["jql"]

def test_fix_labels_with_spaces(researcher):
    jql = 'labels IN ("P&C-DEI", "Tech Debt: Duplication", valid_label)'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    assert '"Tech-Debt:-Duplication"' in sanitized["jql"]
    assert '"P&C-DEI"' in sanitized["jql"]
    assert 'valid_label' in sanitized["jql"]

def test_fix_label_singular_with_spaces(researcher):
    jql = 'label IN ("My Label")'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    assert '"My-Label"' in sanitized["jql"]

def test_cql_with_backslashes(researcher):
    cql = 'lastmodified <= "2025-12-31\\" AND text ~ \\"diversity"'
    queries = {"cql": cql}
    sanitized = researcher._sanitize_queries(queries)
    
    assert '\\"' not in sanitized["cql"]
    assert 'lastmodified <= "2025-12-31"' in sanitized["cql"]
    assert 'text ~ "diversity"' in sanitized["cql"]

def test_mixed_single_double_quotes_escaping(researcher):
    # Standard JQL supports single quotes too
    jql = "summary ~ 'The \"Best\" things' AND text ~ \"It's a trap\""
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    # fix_quotes should escape inner quotes of same type
    assert "summary ~ 'The \"Best\" things'" in sanitized["jql"]
    # It's a bit aggressive with escaping, let's see what it does
    # For text ~ "It's a trap", it sees single quote inside double quotes. 
    # That should remain as is.
    assert "text ~ \"It's a trap\"" in sanitized["jql"]

def test_reversed_in_with_parentheses(researcher):
    # Complex case from log
    jql = '("User A" OR "User B") IN (assignee, reporter) AND ((status = "Done"))'
    queries = {"jql": jql}
    sanitized = researcher._sanitize_queries(queries)
    
    expected = '(assignee IN ("User A", "User B") OR reporter IN ("User A", "User B")) AND ((status = "Done"))'
    assert sanitized["jql"] == expected

def test_sanitize_hallucinated_containers(researcher):
    # Mock available containers
    researcher._available_projects = {"PROJ", "EXIST"}
    researcher._available_spaces = {"SPACE", "WIKI"}
    researcher._containers_fetched = True
    
    # JQL with hallucinated project
    queries = {
        "jql": 'project IN ("PROJ", "GHOST") AND status = "Done"',
        "cql": 'space IN ("SPACE", "PHANTOM") AND title ~ "Test"'
    }
    sanitized = researcher._sanitize_queries(queries)
    
    # GHOST should be filtered out
    assert 'project = "PROJ"' in sanitized["jql"] or 'project IN ("PROJ")' in sanitized["jql"]
    assert 'GHOST' not in sanitized["jql"]
    
    # PHANTOM should be filtered out
    assert 'space = "SPACE"' in sanitized["cql"] or 'space IN ("SPACE")' in sanitized["cql"]
    assert 'PHANTOM' not in sanitized["cql"]

def test_sanitize_all_hallucinated_containers(researcher):
    # Mock available containers
    researcher._available_projects = {"PROJ", "EXIST"}
    researcher._available_spaces = {"SPACE", "WIKI"}
    researcher._containers_fetched = True
    
    queries = {
        "jql": 'project = "GHOST"',
        "cql": 'space = "PHANTOM"'
    }
    sanitized = researcher._sanitize_queries(queries)
    
    # Should fallback to broad text search using the hallucinated name
    assert 'text ~ "GHOST"' in sanitized["jql"]
    assert 'text ~ "PHANTOM"' in sanitized["cql"]

@patch("topic_researcher.get_provider")
@patch("topic_researcher.jira_fetch_issues")
@patch("topic_researcher._conf_get")
def test_completeness_feedback_integration(mock_conf_get, mock_jira_fetch, mock_get_provider, tmp_path):
    mock_llm = MagicMock()
    mock_llm.get_context_window.return_value = 8192
    mock_llm.estimate_tokens.side_effect = lambda x: len(x) // 4
    mock_get_provider.return_value = mock_llm
    
    feedback_text = "Missing individual factual summaries for Das Wijesundera."
    
    mock_llm.predict.side_effect = [
        MagicMock(text="NONE"), # identify subjects
        MagicMock(text="NONE"), # extract names
        # Iter 1
        MagicMock(text='{"jql": "", "cql": "", "search_queries": [], "llm_questions": []}'), # queries
        MagicMock(text='# Draft 1'), # formulation
        MagicMock(text='Critic'), # Critic
        MagicMock(text='# Draft 1'), # Editor
        MagicMock(text=f'NO\n* {feedback_text}'), # is_complete
        
        # Iter 2
        MagicMock(text='{"jql": "", "cql": "", "search_queries": [], "llm_questions": []}'), # queries
        MagicMock(text='# Draft 2'), # formulation
        MagicMock(text='Critic'), # Critic
        MagicMock(text='# Draft 2'), # Editor
        MagicMock(text='YES'), # is_complete
        
        # Final sanity check
        MagicMock(text='# Draft 2 Polished')
    ]
    
    from topic_researcher import TopicResearcher
    researcher = TopicResearcher(
        jira_base_url="https://test.atlassian.net",
        jira_auth=("user", "pass"),
        llm_provider="openai"
    )
    researcher._containers_fetched = True # Avoid API calls for hints
    
    source_file = tmp_path / "req.txt"
    source_file.write_text("Topic: Test\nRequirements: Test reqs")
    output_file = tmp_path / "out.md"
    
    researcher.run(str(source_file), str(output_file), max_iterations=2)
    
    # Check if Iter 2 _generate_queries received the feedback
    # predict.call_args_list[7] -> Iter 2 queries
    iter2_queries_call = mock_llm.predict.call_args_list[7]
    user_content = iter2_queries_call[0][0][0]['content']
    assert "FEEDBACK ON MISSING AREAS" in user_content
    assert feedback_text in user_content
    
    # Also check if Iter 2 queries system prompt was updated
    system_prompt = iter2_queries_call[1]['system']
    assert "PRIORITISE addressing the missing areas" in system_prompt
