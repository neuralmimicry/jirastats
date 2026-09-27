from __future__ import annotations

import os
import json
import requests
import re
import logging
import hashlib
import time
import datetime as dt
from typing import List, Dict, Any, Optional, Tuple, Union
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm_providers import get_provider, LLMProvider, LLMResponse, LLMQuotaError, LLMError
from jira_analysis import fetch_issues as jira_fetch_issues, _jira_get, _extract_confluence_ids
from confluence_analysis import _conf_get, PageInfo
from file_converter import FileConverter

# Setup logging
logger = logging.getLogger(__name__)

RESEARCH_CACHE_ROOT = ".research_cache"

# Common placeholders that LLMs might hallucinate when they don't know the actual keys
COMMON_PLACEHOLDERS = {
    "PROJECT_KEY_HERE", "YOUR_PROJECT_KEY", "ENTER_PROJECT_KEY", "ANY_PROJECT_KEY",
    "SPACE_KEY_HERE", "YOUR_SPACE_KEY", "ENTER_SPACE_KEY", "ANY_SPACE_KEY",
    "YOUR_SPACE_NAME", "PROJECT_NAME_HERE", "YOUR_DOMAIN_HERE", "DOMAIN_KEY_HERE"
}

def status_update(msg: str):
    """Prints a status update to the console and logs it."""
    print(f"[*] {msg}", flush=True)
    logger.info(msg)

class SearchEngine:
    def search(self, query: str) -> List[Dict[str, str]]:
        raise NotImplementedError

    def verify(self) -> Tuple[bool, str]:
        """Verifies if the search engine is correctly configured and reachable."""
        return True, "Success"

class MockSearchEngine(SearchEngine):
    """
    A mock search engine that uses an LLM to simulate web search results.
    In a production environment, this could be replaced with a real search API 
    (e.g., Google Custom Search, Bing Search API).
    """
    def __init__(self, llm: Optional[LLMProvider] = None, llm_params: Optional[Dict[str, Any]] = None, fallback_llm: Optional[LLMProvider] = None):
        self.llm = llm
        self.llm_params = llm_params or {}
        self.fallback_llm = fallback_llm
        self._quota_reached = False

    def search(self, query: str) -> List[Dict[str, str]]:
        """Simulates search results using the LLM's internal knowledge."""
        if not self.llm:
            return [{"title": "No search engine", "snippet": "No LLM provided to simulate search.", "url": "#"}]
            
        logger.info(f"Simulating search for: {query}")
        prompt = (
            f"Simulate a web search for the following query and provide 3-5 relevant snippets. "
            f"Each snippet should include a title, a brief summary, and a plausible URL.\n"
            f"Query: {query}\n"
            f"Format the output as a simple list of snippets in British English."
        )
        
        provider_to_use = self.llm
        if self._quota_reached and self.fallback_llm:
            provider_to_use = self.fallback_llm

        try:
            resp = provider_to_use.predict(
                [{"role": "user", "content": prompt}], 
                system="You are a conservative, reserved British professional technical assistant providing simulated search results based on your training data. Maintain a formal, non-sycophantic tone and prioritize factual accuracy.",
                **self.llm_params
            )
            return [{"title": f"Search result for: {query}", "snippet": resp.text, "url": "https://simulated-search.com"}]
        except LLMQuotaError as e:
            self._quota_reached = True
            if self.fallback_llm and provider_to_use != self.fallback_llm:
                logger.warning(f"Mock search primary LLM quota hit: {e}. Trying fallback.")
                try:
                    resp = self.fallback_llm.predict(
                        [{"role": "user", "content": prompt}],
                        system="You are a conservative, reserved British professional technical assistant providing simulated search results based on your training data. Maintain a formal, non-sycophantic tone and prioritize factual accuracy.",
                        **self.llm_params
                    )
                    return [{"title": f"Search result for: {query}", "snippet": resp.text, "url": "https://simulated-search.com"}]
                except Exception as e2:
                    logger.error(f"Mock search fallback also failed: {e2}")
            
            logger.error(f"Search simulation failed due to quota: {e}")
            return [{"title": "Search Error", "snippet": f"Quota exceeded: {str(e)}", "url": "#"}]
        except Exception as e:
            logger.error(f"Search simulation failed: {e}")
            return [{"title": "Search Error", "snippet": f"Failed to simulate search: {str(e)}", "url": "#"}]

class GoogleSearchEngine(SearchEngine):
    """
    Real Google Search engine integration using Google Custom Search JSON API.
    """
    def __init__(self, api_key: str, cse_id: str, timeout: Optional[int] = None):
        self.api_key = api_key
        self.cse_id = cse_id
        self.timeout = timeout or 10

    def search(self, query: str) -> List[Dict[str, str]]:
        if not self.api_key or not self.cse_id:
            logger.warning("Google Search credentials missing. Falling back.")
            return []

        logger.info(f"Performing Google Search for: {query}")
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.api_key,
            "cx": self.cse_id,
            "q": query,
            "num": 5 # Fetch top 5 results
        }
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("items", []):
                results.append({
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "url": item.get("link", "")
                })
            return results
        except Exception as e:
            logger.error(f"Google Search failed: {e}")
            return []

    def verify(self) -> Tuple[bool, str]:
        """Verifies if the Google Search credentials are valid."""
        if not self.api_key or not self.cse_id:
            return False, "Google Search credentials (API key or CSE ID) are missing."

        logger.info("Verifying Google Search connection...")
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.api_key,
            "cx": self.cse_id,
            "q": "health_check",
            "num": 1
        }
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return True, "Success"
        except Exception as e:
            msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_data = e.response.json()
                    msg = error_data.get("error", {}).get("message", msg)
                except Exception:
                    pass
            return False, msg

class TopicResearcher:
    """
    Orchestrates the iterative research and document generation process.
    Uses Jira, Confluence, LLMs, and Search Engines to compile a comprehensive document.
    """
    
    def __init__(
        self,
        jira_base_url: str,
        jira_auth: Tuple[str, str],
        llm_provider: str,
        llm_model: Optional[str] = None,
        ollama_base_url: Optional[str] = None,
        llm_temperature: float = 0.2,
        llm_max_tokens: Optional[int] = None,
        llm_timeout: Optional[int] = None,
        company_name: Optional[str] = None,
        google_api_key: Optional[str] = None,
        google_cse_id: Optional[str] = None,
        search_configs: Optional[List[Dict[str, Any]]] = None,
        llm_api_key: Optional[str] = None,
        fallback_llm_provider: Optional[str] = None,
        fallback_llm_model: Optional[str] = None,
        fallback_llm_api_key: Optional[str] = None,
        llm_inter_request_gap: float = 3.0,
        cache_ttl_hours: int = 24,
        disable_jira: bool = False,
        disable_confluence: bool = False
    ):
        self.jira_base_url = jira_base_url
        self.jira_auth = jira_auth
        self.company_name = company_name
        self.cache_ttl_hours = cache_ttl_hours
        self.disable_jira = disable_jira
        self.disable_confluence = disable_confluence
        
        # Initialize primary LLM
        p_kwargs = {}
        if llm_api_key:
            if llm_provider in ("gemini", "google") and llm_api_key.startswith("ya29."):
                p_kwargs["access_token"] = llm_api_key
            else:
                p_kwargs["api_key"] = llm_api_key
        
        self.llm = get_provider(llm_provider, model=llm_model, base_url=ollama_base_url, inter_request_gap=llm_inter_request_gap, **p_kwargs)
        
        self.fallback_llm = None
        if fallback_llm_provider:
            f_kwargs = {}
            if fallback_llm_api_key:
                if fallback_llm_provider in ("gemini", "google") and fallback_llm_api_key.startswith("ya29."):
                    f_kwargs["access_token"] = fallback_llm_api_key
                else:
                    f_kwargs["api_key"] = fallback_llm_api_key
            try:
                self.fallback_llm = get_provider(fallback_llm_provider, model=fallback_llm_model, base_url=ollama_base_url, inter_request_gap=llm_inter_request_gap, **f_kwargs)
                logger.info(f"Initialized fallback LLM provider: {fallback_llm_provider}")
            except Exception as e:
                logger.warning(f"Failed to initialize fallback LLM provider {fallback_llm_provider}: {e}")

        self.llm_params = {
            "temperature": llm_temperature,
            "max_tokens": llm_max_tokens,
            "timeout": llm_timeout
        }
        
        self.search_engines = []
        # Support multiple search engines from config
        if search_configs:
            from credentials import get_search_credentials
            for sc in search_configs:
                s_name = sc.get("name")
                s_type = sc.get("type", "google")
                if s_type == "google":
                    s_key, s_cse = get_search_credentials(s_name)
                    if s_key and s_cse:
                        engine = GoogleSearchEngine(s_key, s_cse, timeout=llm_timeout)
                        ok, msg = engine.verify()
                        if ok:
                            self.search_engines.append(engine)
                        else:
                            logger.error(f"Search engine '{s_name}' verification failed: {msg}")

        # Fallback to legacy single Google Search if provided and not already in list
        if google_api_key and google_cse_id:
            # Check if this key/cse combo is already added
            already_added = any(isinstance(e, GoogleSearchEngine) and e.api_key == google_api_key and e.cse_id == google_cse_id for e in self.search_engines)
            if not already_added:
                engine = GoogleSearchEngine(google_api_key, google_cse_id, timeout=llm_timeout)
                ok, msg = engine.verify()
                if ok:
                    self.search_engines.append(engine)
                else:
                    logger.error(f"Legacy Google Search verification failed: {msg}")

        if not self.search_engines:
            logger.info("No valid search engines found or configured. Using MockSearchEngine.")
            self.search_engines = [MockSearchEngine(self.llm, llm_params=self.llm_params, fallback_llm=self.fallback_llm)]
            
        self.file_converter = FileConverter(self.llm, llm_params=self.llm_params)
        self.contributing_jira = set()
        self.contributing_confluence = set()
        self.contributing_web = set()
        self.source_metadata = {} # url -> {type, title, url, identifier}
        self._quota_reached = False
        self._available_projects = set()
        self._available_spaces = set()
        self._containers_fetched = False
        self.subjects = []
        self.subject_evidence = {} # subject -> {"jira": set(), "confluence": set(), "richness_evals": []}
        self.name_cache = set()
        self._names_fetched = False
        self._fetch_cached_names()

    @property
    def search_engine(self):
        """Backward compatibility for single search engine access."""
        return self.search_engines[0] if self.search_engines else None

    @property
    def token_threshold(self) -> int:
        """
        Calculates a dynamic token threshold based on the LLM's context window.
        We use ~25% of the context window, capped at 10,000 tokens to balance 
        between detail and efficiency.
        """
        context_window = self.llm.get_context_window()
        # Default to 1000 if context_window is somehow very small or unknown
        return max(1000, min(10000, context_window // 4))

    @property
    def max_content_chars(self) -> int:
        """
        Calculates maximum characters to fetch/include from a single source 
        based on the context window.
        """
        context_window = self.llm.get_context_window()
        # We allow a single source to take up to ~50% of the context window
        # 1 token approx 4 chars, so window * 4 is full window.
        # window * 2 is approx 50% of the window.
        return max(4000, min(100000, context_window * 2))

    def _predict_with_fallback(self, messages: List[Dict[str, Any]], system: Optional[str] = None, **kwargs) -> LLMResponse:
        """
        Calls the primary LLM provider and falls back to the secondary one if quota is hit.
        """
        if self._quota_reached and self.fallback_llm:
            logger.info("Using fallback LLM provider due to previous quota limit.")
            return self.fallback_llm.predict(messages, system=system, **kwargs)
            
        try:
            return self.llm.predict(messages, system=system, **kwargs)
        except LLMQuotaError as e:
            self._quota_reached = True
            if self.fallback_llm:
                logger.warning(f"LLM Quota exceeded: {e}. Switching to fallback LLM provider.")
                return self.fallback_llm.predict(messages, system=system, **kwargs)
            raise e

    def _write_cache(self, url: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Writes content and optional metadata to the research cache."""
        try:
            if not os.path.exists(RESEARCH_CACHE_ROOT):
                os.makedirs(RESEARCH_CACHE_ROOT)
            
            url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
            cache_path = os.path.join(RESEARCH_CACHE_ROOT, f"{url_hash}.json")
            
            payload = {
                "url": url,
                "timestamp": int(time.time()),
                "content": content,
                "metadata": metadata
            }
            
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            
            logger.debug(f"Saved {url} to cache: {cache_path}")
        except Exception as e:
            logger.warning(f"Failed to write cache for {url}: {e}")

    def _read_cache(self, url: str) -> Optional[Dict[str, Any]]:
        """Reads content and metadata from the research cache if it exists and is not expired."""
        try:
            url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
            cache_path = os.path.join(RESEARCH_CACHE_ROOT, f"{url_hash}.json")
            
            if not os.path.exists(cache_path):
                return None
            
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Check expiration
            timestamp = data.get("timestamp", 0)
            now = int(time.time())
            if abs(now - timestamp) > self.cache_ttl_hours * 3600:
                logger.debug(f"Cache expired for {url}")
                return None
            
            logger.info(f"Retrieved {url} from research cache")
            return data
        except Exception as e:
            logger.debug(f"Failed to read cache for {url}: {e}")
            return None

    def _get_all_sections(self, draft: str, subjects: Optional[List[str]] = None) -> List[str]:
        """
        Extracts all headers from the document, excluding the main title (#).
        If subjects are provided, it prioritises subjects with less content to ensure 
        balanced expansion even when sections are not "thin".
        """
        if not draft:
            return []
        
        lines = draft.split('\n')
        current_header = None
        current_section_content = []
        section_infos = []

        def get_info(header, content_lines_list):
            content = "\n".join(content_lines_list)
            content_chars = len(content.strip())
            header_clean = header.lstrip('#').strip('*').strip()
            
            is_subject = False
            if subjects:
                for s in subjects:
                    if s.lower() in header_clean.lower():
                        is_subject = True
                        break
            
            return {
                "header": header_clean,
                "weight": content_chars,
                "is_subject": is_subject
            }

        for line in lines:
            trimmed = line.strip()
            is_main_title = trimmed.startswith('# ') or trimmed == '#'
            is_header = (trimmed.startswith('#') and not is_main_title) or \
                        (trimmed.startswith('**') and trimmed.endswith('**') and len(trimmed) > 4)
            
            if is_header:
                if current_header:
                    section_infos.append(get_info(current_header, current_section_content))
                current_header = trimmed
                current_section_content = []
            elif trimmed:
                current_section_content.append(line)
        
        if current_header:
            section_infos.append(get_info(current_header, current_section_content))
            
        # Re-balancing sort: subjects first (shortest first), then others (shortest first)
        subject_sections = [info for info in section_infos if info["is_subject"]]
        subject_sections.sort(key=lambda x: x["weight"])
        
        other_sections = [info for info in section_infos if not info["is_subject"]]
        other_sections.sort(key=lambda x: x["weight"])
        
        return [info["header"] for info in subject_sections + other_sections]

    def _get_token_efficient_context(self, draft: str, threshold: Optional[int] = None, target_section: Optional[str] = None) -> str:
        """
        Returns a token-efficient representation of the draft if it exceeds the threshold.
        Includes Table of Contents, the target section content (if provided), and a snippet of the end.
        """
        if not draft:
            return ""
        
        if threshold is None:
            threshold = self.token_threshold
            
        token_count = self.llm.estimate_tokens(draft)
        if token_count < threshold and not target_section:
            return draft
        
        # Extract headers for TOC
        headers = [
            line for line in draft.split('\n') 
            if line.strip().startswith('#') or (line.strip().startswith('**') and line.strip().endswith('**') and len(line.strip()) > 4)
        ]
        toc = "\n".join(headers)
        
        target_content = ""
        if target_section:
            # Try to find and extract the content of the target section
            lines = draft.split('\n')
            start_idx = -1
            for idx, line in enumerate(lines):
                cleaned = line.strip().lstrip('#').strip('*').strip().lower()
                if cleaned == target_section.lower():
                    start_idx = idx
                    break
            
            if start_idx != -1:
                section_lines = [lines[start_idx]]
                for idx in range(start_idx + 1, len(lines)):
                    # Stop at next header of same or higher level
                    trimmed = lines[idx].strip()
                    if trimmed.startswith('#') or (trimmed.startswith('**') and trimmed.endswith('**') and len(trimmed) > 4):
                        break
                    section_lines.append(lines[idx])
                target_content = f"\n### Current Content of '{target_section}':\n" + "\n".join(section_lines) + "\n"

        # Get last part of the draft
        last_lines = draft.split('\n')[-30:]
        last_part = "\n".join(last_lines)
        if len(last_part) > 1000:
            last_part = "..." + last_part[-1000:]
        
        msg = f"Note: The current document is substantial ({token_count} estimated tokens). "
        if token_count >= threshold:
            msg += "To conserve tokens, only an outline, the target section, and recent context are provided below.\n\n"
        else:
            msg += "Relevant context is provided below.\n\n"

        return (
            msg +
            f"### Existing Table of Contents\n{toc}\n\n" +
            (target_content if target_content else "") +
            f"### End of Current Draft\n{last_part}"
        )

    def _identify_subjects(self, requirements: str) -> List[str]:
        """
        Uses the LLM to identify specific people or entities that are the primary subjects
        of the research as listed in the requirements.
        """
        system_prompt = (
            "Identify all specific individuals (people's names) or specific technical entities "
            "(like particular squads or teams) that are listed as subjects to be researched "
            "individually in the provided requirements. \n"
            "STRICT RULES:\n"
            "1. Return them as a simple comma-separated list.\n"
            "2. If no specific individuals or entities are listed, return 'NONE'.\n"
            "3. Do not include titles like 'Tech Lead' or 'Manager', just the names.\n"
            "4. Only include names that are subjects of the report, not the author or requester."
        )
        try:
            resp = self._predict_with_fallback([{"role": "user", "content": requirements}], system=system_prompt, **self.llm_params)
            text = resp.text.strip().strip('`').strip()
            if text.upper() == "NONE" or not text:
                return []
            subjects = [s.strip() for s in text.split(",") if s.strip()]
            return subjects
        except Exception as e:
            logger.warning(f"Failed to identify subjects: {e}")
            return []

    def _identify_thin_sections(self, draft: str, subjects: Optional[List[str]] = None) -> List[str]:
        """
        Identifies headers in the document that have little to no content under them.
        Uses a combination of line count, character count, and presence of generic placeholders.
        If subjects are provided, it prioritises missing subjects and those with the least content
        to ensure balanced expansion.
        """
        if not draft:
            if subjects:
                return subjects
            return []
            
        lines = draft.split('\n')
        current_header = None
        current_section_content = []
        section_infos = [] # List of dicts
        
        def get_info(header, content_lines_list):
            content = "\n".join(content_lines_list)
            content_lines = len([l for l in content_lines_list if l.strip() and len(l.strip()) > 10])
            content_chars = len(content.strip())
            
            # Check for generic placeholders
            placeholders = [r"Domain [A-Z]", r"System [A-Z]", r"Project [A-Z]", r"\[.*\]", r"TBD", r"N/A"]
            has_placeholders = any(re.search(p, content, re.IGNORECASE) for p in placeholders)
            
            is_thin = content_lines < 6 or content_chars < 600 or has_placeholders
            
            header_clean = header.lstrip('#').strip('*').strip()
            is_subject = False
            if subjects:
                for s in subjects:
                    if s.lower() in header_clean.lower():
                        is_subject = True
                        break
            
            return {
                "header": header_clean,
                "weight": content_chars,
                "thin": is_thin,
                "is_subject": is_subject
            }

        for line in lines:
            trimmed = line.strip()
            is_main_title = trimmed.startswith('# ') or trimmed == '#'
            is_header = (trimmed.startswith('#') and not is_main_title) or \
                        (trimmed.startswith('**') and trimmed.endswith('**') and len(trimmed) > 4)
            
            if is_header:
                if current_header:
                    section_infos.append(get_info(current_header, current_section_content))
                current_header = trimmed
                current_section_content = []
            elif trimmed:
                current_section_content.append(line)
        
        if current_header:
            section_infos.append(get_info(current_header, current_section_content))
            
        # Add missing subjects as zero-weight thin sections
        if subjects:
            existing_headers_lower = [info["header"].lower() for info in section_infos]
            for s in subjects:
                found = False
                for h in existing_headers_lower:
                    if s.lower() in h:
                        found = True
                        break
                if not found:
                    section_infos.append({
                        "header": s,
                        "weight": 0,
                        "thin": True,
                        "is_subject": True
                    })

        # Filter to only thin sections
        thin_infos = [info for info in section_infos if info["thin"]]
        
        # Sort logic for re-balancing:
        # 1. Subjects first, sorted by weight (content length) ascending.
        # 2. Others next, sorted by weight ascending.
        
        subject_thin = [info for info in thin_infos if info["is_subject"]]
        subject_thin.sort(key=lambda x: x["weight"])
        
        other_thin = [info for info in thin_infos if not info["is_subject"]]
        other_thin.sort(key=lambda x: x["weight"])
        
        return [info["header"] for info in subject_thin + other_thin]

    def _record_jira_contribution(self, key: str, summary: Optional[str] = None):
        """Records a Jira issue as a contributor to the research."""
        if key not in self.contributing_jira:
            self.contributing_jira.add(key)
            url = f"{self.jira_base_url.rstrip('/')}/browse/{key}"
            self.source_metadata[url] = {
                "type": "Jira",
                "identifier": key,
                "title": summary or key,
                "url": url
            }
            logger.info(f"Tracked Jira contribution: {key}")

    def _track_issue_evidence(self, issue: Any, richness: Optional[Dict[str, Any]] = None):
        """Tracks evidence per subject for Jira issues."""
        if not self.subjects:
            return
            
        involved = set()
        if hasattr(issue, "assignee") and issue.assignee: involved.add(issue.assignee.lower())
        if hasattr(issue, "reporter") and issue.reporter: involved.add(issue.reporter.lower())
        if hasattr(issue, "commenters") and issue.commenters:
            for c in issue.commenters:
                if c: involved.add(c.lower())
        
        # Case for dict-based issues from fallbacks or cache
        if isinstance(issue, dict):
            if issue.get("assignee"): involved.add(issue["assignee"].lower())
            if issue.get("reporter"): involved.add(issue["reporter"].lower())
            for c in issue.get("commenters", []):
                if c: involved.add(c.lower())

        for s in self.subjects:
            if s.lower() in involved:
                if s not in self.subject_evidence:
                    self.subject_evidence[s] = {"jira": set(), "confluence": set(), "richness_evals": [], "relevance_evals": []}
                
                key = getattr(issue, "key", None) or issue.get("key")
                if key and key not in self.subject_evidence[s]["jira"]:
                    self.subject_evidence[s]["jira"].add(key)
                    if richness:
                        self.subject_evidence[s]["richness_evals"].append({
                            "key": key,
                            "richness": richness["richness"],
                            "score": richness["score"]
                        })
                    
                    relevance = self._evaluate_subject_relevance(s, issue)
                    self.subject_evidence[s]["relevance_evals"].append({
                        "key": key,
                        "relevance": relevance["relevance"],
                        "score": relevance["score"],
                        "observations": relevance["observations"],
                        "type": "Jira"
                    })

    def _track_page_evidence(self, page_id: str, title: str, creator: Optional[str], contributor: Optional[str], body: str, content_type: str = "page"):
        """Tracks evidence per subject for Confluence pages with relevance weighting."""
        if not self.subjects:
            return
            
        for s in self.subjects:
            s_lower = s.lower()
            
            # Check if mentioned in body or title (to capture participants)
            in_title = s_lower in title.lower()
            in_body = s_lower in body.lower()
            
            involved = False
            if creator and s_lower == creator.lower(): involved = True
            if contributor and s_lower == contributor.lower(): involved = True
            if in_title or in_body: involved = True
            
            if involved:
                if s not in self.subject_evidence:
                    self.subject_evidence[s] = {"jira": set(), "confluence": set(), "richness_evals": [], "relevance_evals": []}
                
                if page_id not in self.subject_evidence[s]["confluence"]:
                    self.subject_evidence[s]["confluence"].add(page_id)
                    
                    relevance = self._evaluate_confluence_relevance(s, creator, contributor, title, body, content_type)
                    self.subject_evidence[s]["relevance_evals"].append({
                        "key": page_id,
                        "relevance": relevance["relevance"],
                        "score": relevance["score"],
                        "observations": relevance["observations"],
                        "type": "Confluence"
                    })

    def _get_evidence_summary(self) -> str:
        """Generates a summary of evidence density and relevance per subject for LLM context."""
        if not self.subject_evidence:
            return "No specific evidence density recorded for subjects."
            
        lines = ["Evidence Density and Credibility per Subject:"]
        for s, data in sorted(self.subject_evidence.items()):
            jira_count = len(data["jira"])
            conf_count = len(data["confluence"])
            
            summary_parts = []
            
            if data["richness_evals"]:
                counts = {"High": 0, "Medium": 0, "Low": 0}
                for r in data["richness_evals"]:
                    counts[r["richness"]] = counts.get(r["richness"], 0) + 1
                
                parts = []
                if counts["High"]: parts.append(f"{counts['High']} High richness")
                if counts["Medium"]: parts.append(f"{counts['Medium']} Medium richness")
                if counts["Low"]: parts.append(f"{counts['Low']} Low richness")
                summary_parts.append(f"Jira richness: {', '.join(parts)}")
            
            if data["relevance_evals"]:
                jira_rel = [r for r in data["relevance_evals"] if r.get("type", "Jira") == "Jira"]
                conf_rel = [r for r in data["relevance_evals"] if r.get("type") == "Confluence"]
                
                if jira_rel:
                    counts = {"High": 0, "Medium": 0, "Low": 0}
                    for r in jira_rel:
                        counts[r["relevance"]] = counts.get(r["relevance"], 0) + 1
                    
                    parts = []
                    if counts["High"]: parts.append(f"{counts['High']} High (Key Contributor)")
                    if counts["Medium"]: parts.append(f"{counts['Medium']} Medium")
                    if counts["Low"]: parts.append(f"{counts['Low']} Low (Passive Oversight)")
                    summary_parts.append(f"Jira relevance: {', '.join(parts)}")

                if conf_rel:
                    counts = {"High": 0, "Medium": 0, "Low": 0}
                    for r in conf_rel:
                        counts[r["relevance"]] = counts.get(r["relevance"], 0) + 1
                    
                    parts = []
                    if counts["High"]: parts.append(f"{counts['High']} High (Author/Owner)")
                    if counts["Medium"]: parts.append(f"{counts['Medium']} Medium (Contributor/Editor)")
                    if counts["Low"]: parts.append(f"{counts['Low']} Low (Participant only)")
                    summary_parts.append(f"Confluence relevance: {', '.join(parts)}")
            
            details = f" ({'; '.join(summary_parts)})" if summary_parts else ""
            line = f"- {s}: {jira_count} Jira issues, {conf_count} Confluence pages.{details}"
            
            # Add a warning if evidence is thin or low relevance
            total_sources = jira_count + conf_count
            low_rel_count = sum(1 for r in data["relevance_evals"] if r["relevance"] == "Low")
            high_rel_count = sum(1 for r in data["relevance_evals"] if r["relevance"] == "High")
            
            if total_sources == 0:
                line += " [CRITICAL: No direct evidence found in research data]"
            elif total_sources == 1:
                if low_rel_count == 1:
                    line += " [CRITICAL: Only one source found and it has LOW relevance. DO NOT overstate contributions.]"
                else:
                    line += " [CAUTION: Limited evidence - avoid overselling achievements based on this single source]"
            elif high_rel_count == 0 and total_sources > 0:
                line += " [CAUTION: No high-relevance tasks found. Subject appears to have primarily oversight or passive roles. Reflect this accurately.]"
                
            lines.append(line)
            
        return "\n".join(lines)

    def _record_confluence_contribution(self, page_id: Optional[str], title: str, url: Optional[str] = None):
        """Records a Confluence page as a contributor to the research."""
        entry = f"{title} ({page_id})" if page_id else title
        if entry not in self.contributing_confluence:
            self.contributing_confluence.add(entry)
            if not url and page_id:
                url = f"{self.jira_base_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={page_id}"
            
            if url:
                self.source_metadata[url] = {
                    "type": "Confluence",
                    "identifier": page_id,
                    "title": title,
                    "url": url
                }
            logger.info(f"Tracked Confluence contribution: {entry}")

    def _record_web_contribution(self, url: str, title: str):
        """Records a web page as a contributor to the research."""
        if url and url != "#" and url not in self.contributing_web:
            self.contributing_web.add(url)
            self.source_metadata[url] = {
                "type": "Web",
                "title": title,
                "url": url
            }
            logger.info(f"Tracked Web contribution: {url}")

    def _fetch_available_containers(self) -> None:
        """
        Fetches all available Jira projects and Confluence spaces and caches them.
        """
        if self._containers_fetched:
            return

        # 1. Try to load from persistent cache first
        cache_file = os.path.join(RESEARCH_CACHE_ROOT, "containers.json")
        try:
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Use a 24-hour TTL for container cache
                timestamp = data.get("timestamp", 0)
                if abs(time.time() - timestamp) < 24 * 3600:
                    self._available_projects = set(data.get("projects", []))
                    self._available_spaces = set(data.get("spaces", []))
                    if self._available_projects or self._available_spaces:
                        logger.info(f"Loaded {len(self._available_projects)} projects and {len(self._available_spaces)} spaces from cache")
                        self._containers_fetched = True
                        return
        except Exception as e:
            logger.debug(f"Failed to load container cache: {e}")

        # 2. Fetch from APIs
        logger.info("Fetching available Jira projects and Confluence spaces...")
        
        # Jira Projects
        if not self.disable_jira:
            try:
                projects = _jira_get(self.jira_base_url, self.jira_auth, "/rest/api/3/project")
                if isinstance(projects, list):
                    self._available_projects = {p.get("key") for p in projects if p.get("key")}
            except Exception as e:
                logger.warning(f"Failed to fetch Jira projects: {e}")

        # Confluence Spaces
        if not self.disable_confluence:
            try:
                # We might need to handle pagination if there are many spaces
                start = 0
                limit = 50
                while True:
                    spaces_data = _conf_get(self.jira_base_url, self.jira_auth, "/rest/api/space", params={"start": start, "limit": limit})
                    results = spaces_data.get("results", [])
                    if not results:
                        break
                    for s in results:
                        key = s.get("key")
                        if key:
                            self._available_spaces.add(key)
                    
                    if len(results) < limit:
                        break
                    start += limit
            except Exception as e:
                logger.warning(f"Failed to fetch Confluence spaces: {e}")

        # 3. Save to cache
        try:
            if not os.path.exists(RESEARCH_CACHE_ROOT):
                os.makedirs(RESEARCH_CACHE_ROOT)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamp": int(time.time()),
                    "projects": list(self._available_projects),
                    "spaces": list(self._available_spaces)
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to save container cache: {e}")

        self._containers_fetched = True
        logger.info(f"Initialized with {len(self._available_projects)} projects and {len(self._available_spaces)} spaces")

    def _fetch_cached_names(self) -> None:
        """Loads person names from the cache file."""
        if self._names_fetched:
            return
            
        cache_file = os.path.join(RESEARCH_CACHE_ROOT, "names.json")
        try:
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                # Use a 7-day TTL for names cache as people don't change that often
                timestamp = data.get("timestamp", 0)
                if abs(time.time() - timestamp) < 7 * 24 * 3600:
                    self.name_cache.update(data.get("names", []))
                    if self.name_cache:
                        logger.info(f"Loaded {len(self.name_cache)} names from cache")
                        self._names_fetched = True
                        return
        except Exception as e:
            logger.debug(f"Failed to load names cache: {e}")
        
        self._names_fetched = True

    def _save_names_to_cache(self) -> None:
        """Saves current name cache to disk."""
        cache_file = os.path.join(RESEARCH_CACHE_ROOT, "names.json")
        try:
            if not os.path.exists(RESEARCH_CACHE_ROOT):
                os.makedirs(RESEARCH_CACHE_ROOT)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamp": int(time.time()),
                    "names": sorted(list(self.name_cache))
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to save names cache: {e}")

    def _extract_names_from_text(self, text: str) -> List[str]:
        """
        Uses the LLM to extract person names from a block of text.
        This is used for requirements and other context files.
        """
        if not text or len(text.strip()) < 10:
            return []
            
        system_prompt = (
            "Identify all individual person names mentioned in the provided text. \n"
            "STRICT RULES:\n"
            "1. Return them as a simple comma-separated list.\n"
            "2. If no names are found, return 'NONE'.\n"
            "3. Only include actual person names, not titles or group names.\n"
            "4. Return ONLY the list, no other text."
        )
        try:
            # We use a short snippet to avoid token waste, but enough to catch names
            resp = self._predict_with_fallback([{"role": "user", "content": text[:10000]}], system=system_prompt, **self.llm_params)
            names_text = resp.text.strip().strip('`').strip()
            if names_text.upper() == "NONE" or not names_text:
                return []
            return [n.strip() for n in names_text.split(",") if n.strip()]
        except Exception as e:
            logger.warning(f"Failed to extract names from text via LLM: {e}")
            return []

    def _update_name_cache(self, names: Union[str, List[str]]) -> None:
        """Updates the name cache with new names."""
        if isinstance(names, str):
            self.name_cache.add(names)
        else:
            self.name_cache.update(names)
        
        if names:
            # Periodically save? For now just save when updated.
            # In a real app we might want to debounce this.
            self._save_names_to_cache()

    def _extract_references(self, draft: str) -> Tuple[List[str], List[str], List[str]]:
        """
        Extracts Jira keys, Confluence IDs, and potential titles from the draft.
        """
        if not draft:
            return [], [], []
            
        # Jira Keys: [A-Z][A-Z0-9]+-[0-9]+
        jira_keys = re.findall(r'\b([A-Z][A-Z0-9]+-[0-9]+)\b', draft)
        jira_keys = sorted(list(set(jira_keys)))
        
        # Confluence IDs via URLs
        conf_ids = _extract_confluence_ids(draft, self.jira_base_url)
        
        # Potential titles in backticks (e.g. `TECH_HUB: Advanced Kafka...`)
        potential_titles = re.findall(r'`([^`]+)`', draft)
        
        # Add parenthesized ones if they look like titles (start with uppercase, reasonable length, not IDs)
        paren_matches = re.findall(r'\(([^)]+)\)', draft)
        for pm in paren_matches:
            pm_strip = pm.strip()
            if not pm_strip: continue
            # Avoid matching URLs or Jira keys or numeric IDs
            if pm_strip.startswith('http'): continue
            if re.match(r'^[A-Z]+-\d+$', pm_strip): continue
            if re.match(r'^\d+$', pm_strip): continue
            
            # Heuristic: Title-like if starts with Upper, has spaces or punctuation, and > 5 chars
            if pm_strip[0].isupper() and len(pm_strip) > 5:
                potential_titles.append(pm_strip)
        
        potential_titles = sorted(list(set(potential_titles)))
        
        return jira_keys, conf_ids, potential_titles

    def _validate_reference_existence(self, jira_keys: List[str], conf_ids: List[str], titles: Optional[List[str]] = None, research_data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """
        Validates if the references exist in research_data or via API.
        Returns a mapping of reference -> summary or "INVALID (Not Found)".
        """
        validation_results = {}
        
        # 1. Check Jira keys
        known_jira = {}
        if not self.disable_jira:
            if research_data and "jira_issues" in research_data:
                for issue in research_data["jira_issues"]:
                    key = issue.get("key")
                    if key:
                        summary = issue.get("summary") or key
                        itype = issue.get("issuetype")
                        status = issue.get("status")
                        info = f"{summary}"
                        if itype: info += f" (Type: {itype})"
                        if status: info += f" (Status: {status})"
                        known_jira[key] = info
            
            for key in jira_keys:
                if key in known_jira:
                    validation_results[key] = known_jira[key]
                else:
                    # Try to fetch via API
                    try:
                        logger.debug(f"Probing Jira for reference existence: {key}")
                        data = _jira_get(self.jira_base_url, self.jira_auth, f"/rest/api/3/issue/{key}")
                        fields = data.get("fields", {})
                        summary = fields.get("summary", key)
                        itype = fields.get("issuetype", {}).get("name")
                        status = fields.get("status", {}).get("name")
                        info = f"{summary}"
                        if itype: info += f" (Type: {itype})"
                        if status: info += f" (Status: {status})"
                        validation_results[key] = info
                        # Record it so we know it's valid for future iterations
                        self._record_jira_contribution(key, summary)
                    except Exception:
                        validation_results[key] = "INVALID (Not Found)"
        else:
            for key in jira_keys:
                validation_results[key] = "SKIPPED (Jira Disabled)"
                    
        # 2. Check Confluence IDs
        known_conf = {}
        if not self.disable_confluence:
            if research_data and "confluence_pages" in research_data:
                for page in research_data["confluence_pages"]:
                    pid = str(page.get("id"))
                    if pid:
                        title = page.get("title") or pid
                        snippet = page.get("snippet")
                        info = f"{title}"
                        if snippet:
                            # Take a short snippet
                            short_snippet = snippet[:150].strip().replace("\n", " ")
                            if len(snippet) > 150: short_snippet += "..."
                            info += f" - Snippet: {short_snippet}"
                        known_conf[pid] = info
                    
            for cid in conf_ids:
                if cid in known_conf:
                    validation_results[cid] = known_conf[cid]
                else:
                    try:
                        logger.debug(f"Probing Confluence for reference existence: {cid}")
                        data = _conf_get(self.jira_base_url, self.jira_auth, f"/rest/api/content/{cid}")
                        title = data.get("title", "Untitled")
                        validation_results[cid] = title
                        self._record_confluence_contribution(cid, title)
                    except Exception:
                        validation_results[cid] = "INVALID (Not Found)"
        else:
            for cid in conf_ids:
                validation_results[cid] = "SKIPPED (Confluence Disabled)"

        # 3. Check Titles (Jira summaries or Confluence page titles)
        if titles:
            all_known_titles = set()
            if research_data:
                for issue in research_data.get("jira_issues", []):
                    if issue.get("summary"): 
                        all_known_titles.add(issue["summary"].strip().lower())
                for page in research_data.get("confluence_pages", []):
                    if page.get("title"): 
                        all_known_titles.add(page["title"].strip().lower())
            
            for t in titles:
                # If we already validated this as a key or ID, skip it
                if t in validation_results:
                    continue
                    
                if t.strip().lower() in all_known_titles:
                    validation_results[t] = "VALID (Known Title)"
                else:
                    # Potential hallucination if not found in research data
                    # and doesn't look like a standard acronym or very short term
                    if len(t) > 10:
                        validation_results[t] = "INVALID (Not Found in research data)"
                    
        return validation_results

    def _get_existing_context_hints(self) -> str:
        """
        Returns hints about existing Jira projects and Confluence spaces 
        to help the LLM avoid hallucinating non-existent keys.
        """
        self._fetch_available_containers()
        
        hints = []
        if self._available_projects:
            # Show a sample of 15 projects
            p_list = sorted(list(self._available_projects))[:15]
            hints.append(f"Available Jira Projects (Sample): {', '.join(p_list)}")
        
        if self._available_spaces:
            # Show a sample of 15 spaces
            s_list = sorted(list(self._available_spaces))[:15]
            hints.append(f"Available Confluence Spaces (Sample): {', '.join(s_list)}")

        if not hints:
            return ""
        return "CONTEXT HINTS (Real Data - ONLY use these if applicable, otherwise search broadly using 'text ~'):\n" + "\n".join(hints)

    def _generate_bibliography(self) -> str:
        """
        Generates a Markdown bibliography from the tracked sources.
        """
        if not self.source_metadata:
            return ""
            
        lines = ["# Bibliography and References\n"]
        
        # Group by type
        by_type = {}
        for meta in self.source_metadata.values():
            t = meta["type"]
            if t not in by_type:
                by_type[t] = []
            by_type[t].append(meta)
            
        # Sort types
        for t in sorted(by_type.keys()):
            lines.append(f"## {t} Sources")
            # Sort by title
            sorted_sources = sorted(by_type[t], key=lambda x: x["title"].lower())
            for src in sorted_sources:
                title = src["title"]
                url = src["url"]
                if t == "Jira":
                    key = src.get("identifier")
                    lines.append(f"* [{key}: {title}]({url})")
                else:
                    lines.append(f"* [{title}]({url})")
            lines.append("")
            
        return "\n".join(lines).strip()

    def _fetch_url(self, url: str) -> requests.Response:
        """
        Fetches a URL with browser-like headers and handles common blocks (403, 405).
        """
        headers_list = [
            # Chrome on Windows
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "en-GB,en;q=0.9,en-US;q=0.8",
                "Referer": "https://www.google.com/",
                "DNT": "1",
            },
            # Firefox on Linux
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.5",
                "Referer": "https://www.bing.com/",
            },
            # Safari on macOS
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        ]
        
        last_error = None
        session = requests.Session()
        for i, headers in enumerate(headers_list):
            try:
                headers.update({
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                })
                resp = session.get(url, headers=headers, timeout=self.llm_params.get("timeout", 30) or 30)
                if resp.status_code < 400:
                    return resp
                
                last_error = f"HTTP {resp.status_code}: {resp.reason}"
                if resp.status_code not in [403, 405]:
                    # For other errors, maybe don't bother retrying with different headers
                    break
            except Exception as e:
                last_error = str(e)
                continue
        
        # If all standard attempts fail and it's a 403/405, try one last time with LLM advice if possible
        if last_error and ("403" in last_error or "405" in last_error) and not self._quota_reached:
            logger.info(f"Standard fetch failed for {url} ({last_error}). Asking LLM for advice on bypassing this block.")
            advice = self._get_fetch_advice(url, last_error)
            if advice:
                try:
                    headers = advice.get("headers", headers_list[0])
                    cookies = advice.get("cookies", {})
                    params = advice.get("params", {})
                    reasoning = advice.get("reasoning", "No reasoning provided.")
                    
                    logger.info(f"Retrying {url} with LLM suggested strategy. Reasoning: {reasoning}")
                    resp = session.get(url, headers=headers, cookies=cookies, params=params, timeout=30)
                    if resp.status_code < 400:
                        logger.info(f"Successfully fetched {url} using LLM advice!")
                        return resp
                    last_error = f"HTTP {resp.status_code}: {resp.reason} (after LLM advice)"
                except Exception as e:
                    last_error = f"{str(e)} (after LLM advice)"

        # If we got here, all attempts failed. Re-raise or return the last failed response
        if 'resp' in locals() and resp is not None:
            resp.raise_for_status()
        raise requests.exceptions.RequestException(last_error)

    def _get_fetch_advice(self, url: str, error_msg: str) -> Dict[str, Any]:
        """
        Consults the LLM for advice on how to bypass a 403/405 error for a specific URL.
        """
        system_prompt = (
            "You are a web scraping expert. A request to a URL has failed with a 403 Forbidden or 405 Method Not Allowed error. "
            "This is often due to bot detection or a requirement for a browser-like environment (cookie consent, etc.).\n"
            "Suggest specific HTTP headers (like User-Agent, Referer, etc.) or cookies that might circumvent this block for this specific domain.\n"
            "MANDATORY: Return your response ONLY as a JSON object with these keys: "
            "'headers' (dict), 'cookies' (dict), 'params' (dict), 'reasoning' (string)."
        )
        user_content = f"URL: {url}\nError: {error_msg}"
        try:
            resp = self._predict_with_fallback([{"role": "user", "content": user_content}], system=system_prompt, **self.llm_params)
            return self._extract_json(resp.text)
        except Exception as e:
            logger.warning(f"Failed to get fetch advice from LLM: {e}")
            return {}

    def _read_source(self, path_or_url: str) -> str:
        """
        Reads and converts input content from either a local file or a remote URL.
        """
        import tempfile
        import os
        
        logger.info(f"Reading from: {path_or_url}")
        if path_or_url.startswith(("http://", "https://")):
            # 1. Try cache first
            cache_data = self._read_cache(path_or_url)
            if cache_data:
                content = cache_data.get("content", "")
                metadata = cache_data.get("metadata")
                if metadata:
                    m_type = metadata.get("type")
                    if m_type == "Jira":
                        self._record_jira_contribution(metadata.get("identifier"), metadata.get("title"))
                    elif m_type == "Confluence":
                        self._record_confluence_contribution(metadata.get("identifier"), metadata.get("title"), url=path_or_url)
                    elif m_type == "Web":
                        self._record_web_contribution(path_or_url, metadata.get("title"))
                return content

            # Check if it's a Jira or Confluence URL from our own instance
            is_jira_confluence = self.jira_base_url.rstrip("/") in path_or_url
            
            content = ""
            metadata = None
            try:
                if is_jira_confluence:
                    # Attempt authenticated API fetch first for our own instance
                    parsed = urlparse(path_or_url)
                    # Jira issue?
                    if "/browse/" in parsed.path:
                        issue_key = parsed.path.split("/browse/")[1].split("/")[0]
                        logger.info(f"Detected Jira issue URL, fetching via API: {issue_key}")
                        data = _jira_get(self.jira_base_url, self.jira_auth, f"/rest/api/3/issue/{issue_key}")
                        from jira_analysis import _map_issue
                        issue = _map_issue(self.jira_base_url, data)
                        self._record_jira_contribution(issue.key, issue.summary)
                        content = f"Jira Issue {issue.key}: {issue.summary}\n\n{issue.description}"
                        metadata = {"type": "Jira", "identifier": issue.key, "title": issue.summary}
                    
                    # Confluence page?
                    elif "/wiki/spaces/" in parsed.path or "/wiki/pages/" in parsed.path or "/wiki/content/" in parsed.path:
                        from jira_analysis import _extract_confluence_ids
                        cids = _extract_confluence_ids(path_or_url, self.jira_base_url)
                        if cids:
                            logger.info(f"Detected Confluence page URL, fetching via API: {cids[0]}")
                            data = _conf_get(self.jira_base_url, self.jira_auth, f"/rest/api/content/{cids[0]}", params={"expand": "body.storage"})
                            title = data.get("title", "Untitled")
                            body = data.get("body", {}).get("storage", {}).get("value", "")
                            self._record_confluence_contribution(cids[0], title, url=path_or_url)
                            content = f"Confluence Page: {title}\n\n{body}"
                            metadata = {"type": "Confluence", "identifier": cids[0], "title": title}

                if not content:
                    # Standard fetch
                    resp = self._fetch_url(path_or_url)
                    
                    # Check if it's text-based or binary
                    content_type = resp.headers.get("Content-Type", "")
                    is_likely_text = any(t in content_type for t in ["text/", "json", "javascript", "xml"])
                    
                    if is_likely_text and not any(ext in path_or_url.lower() for ext in [".pdf", ".docx", ".odf", ".odt", ".jpg", ".png", ".mp3", ".mp4"]):
                        content = resp.text
                    else:
                        # Binary or complex format, save to temp and convert
                        suffix = os.path.splitext(urlparse(path_or_url).path)[1]
                        import tempfile
                        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                            tmp.write(resp.content)
                            tmp_path = tmp.name
                        try:
                            content = self.file_converter.convert(tmp_path, mime_type=content_type)
                        finally:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                    
                    # For web content, we don't always have a title here, but we can try to guess it later
                    # or the caller (e.g. _execute_queries) will record it.
                    # However, if context_sources contains a URL, we should record it here.
                    metadata = {"type": "Web", "title": path_or_url}
                
                # Save to cache if we got content
                if content:
                    self._write_cache(path_or_url, content, metadata=metadata)
                return content

            except Exception as e:
                logger.warning(f"Failed to fetch {path_or_url}: {e}. Attempting fallback research.")
                fallback_data = self._fallback_url_research(path_or_url, str(e))
                if fallback_data:
                    content = f"Note: The source at {path_or_url} was unreachable ({str(e)}). " \
                              f"Relevant information found via fallback:\n{json.dumps(fallback_data, indent=2)}"
                    metadata = {"type": "Web", "title": f"Fallback for {path_or_url}"}
                    self._write_cache(path_or_url, content, metadata=metadata)
                    return content
                raise # Re-raise if no fallback data found
        else:
            return self.file_converter.convert(path_or_url)

    def _fetch_context(self, context_sources: List[str]) -> str:
        """
        Fetches and aggregates content from additional context sources.
        """
        if not context_sources:
            return ""
        
        aggregated_context = []
        for source in context_sources:
            try:
                content = self._read_source(source)
                aggregated_context.append(f"--- Context Source: {source} ---\n{content}")
            except Exception as e:
                logger.error(f"Failed to fetch context from {source}: {e}")
                aggregated_context.append(f"--- Context Source: {source} ---\nError: {str(e)}")
        
        return "\n\n".join(aggregated_context)

    def _balance_parentheses(self, s: str) -> str:
        """Ensures parentheses are balanced in the query and removes excessive nesting."""
        if not s:
            return s
            
        # 1. Remove excessive nesting like (((field = val))) or ((field = val AND field2 = val2))
        # We only remove if it is at least double-wrapped, to preserve single wrapping which might be intended for clarity.
        while True:
            trimmed = s.strip()
            if trimmed.startswith('((') and trimmed.endswith('))'):
                inner = trimmed[1:-1].strip()
                # Verify if the outer parens were a matching pair
                depth = 0
                is_pair = True
                for char in inner:
                    if char == '(': depth += 1
                    elif char == ')': depth -= 1
                    if depth < 0:
                        is_pair = False
                        break
                if is_pair and depth == 0:
                    s = inner
                    continue
            break

        # 2. Balance remaining
        open_count = s.count('(')
        close_count = s.count(')')
        
        if open_count == close_count:
            return s
            
        if open_count > close_count:
            # Add missing closing at the end
            return s + ')' * (open_count - close_count)
        else:
            # Try to remove extra closing at the end if they are clearly dangling
            while s.count(')') > s.count('(') and s.endswith(')'):
                temp_s = s[:-1].strip()
                # Check if we didn't just break a balance somewhere else (unlikely if endswith ')')
                if temp_s.count(')') >= temp_s.count('('):
                    s = temp_s
                else:
                    break
            
            # If still unbalanced, try to add opening at the beginning
            open_count = s.count('(')
            close_count = s.count(')')
            if close_count > open_count:
                return '(' * (close_count - open_count) + s
        
        return s

    def _fix_query_with_llm(self, query: str, error_msg: str, is_cql: bool = False) -> Optional[str]:
        """
        Consults the LLM to fix a syntactically invalid JQL or CQL query.
        """
        q_type = "Confluence CQL" if is_cql else "Jira JQL"
        system_prompt = (
            f"You are an expert in {q_type}. A query has failed with an error. "
            "Fix the query to be syntactically correct and valid according to official documentation.\n"
            "STRICT RULES:\n"
            "1. Return ONLY the fixed query string.\n"
            "2. Do NOT include any preamble, explanation, or conversational filler.\n"
            "3. Ensure parentheses are balanced.\n"
            "4. For CQL, remember: 'space' and 'title' are valid, 'page' is not a field. 'lastmodified' instead of 'updated'.\n"
            "5. For JQL, remember: 'project', 'summary', 'text', 'assignee' are valid.\n"
            "6. Keep the core intent and keywords of the original query."
        )
        
        user_content = f"Broken Query: {query}\nError Message: {error_msg}"
        
        if self._quota_reached:
            return None
            
        try:
            logger.info(f"Asking LLM to fix broken {q_type} query.")
            resp = self._predict_with_fallback(
                [{"role": "user", "content": user_content}],
                system=system_prompt,
                **self.llm_params
            )
            fixed_query = resp.text.strip().strip('`').strip()
            if fixed_query:
                # Sanitize the LLM output just in case
                fixed_query = self._balance_parentheses(fixed_query)
                logger.info(f"LLM suggested fixed query: {fixed_query}")
                return fixed_query
        except Exception as e:
            logger.warning(f"LLM failed to fix query: {e}")
            
        return None

    def _sanitize_queries(self, queries: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitizes JQL and CQL queries to fix common mistakes.
        """
        def sanitize_string(s: str, is_cql: bool = False) -> str:
            # 0. Preliminary cleanup of mis-hallucinated backslashes before quotes
            # LLMs often return \" when they should return " in JQL/CQL strings
            s = re.sub(r'(?<!\\)\\(["\'])', r'\1', s)

            # 0.5. Fix incorrectly wrapped or truncated queries
            # Remove redundant wrapping quotes often added by LLMs
            s = s.strip()
            if (s.startswith('"') and s.endswith('"')):
                # Only remove if it's a single pair of wrapping quotes and not a single quoted word
                if len(re.findall(r'(?<!\\)"', s)) == 2:
                    if not re.match(r'^"[\w\s-]+"$', s):
                        s = s[1:-1].strip()
            elif (s.startswith("'") and s.endswith("'")):
                if len(re.findall(r"(?<!\\)'", s)) == 2:
                    if not re.match(r"^'[\w\s-]+'$", s):
                        s = s[1:-1].strip()
            
            # 0.7 Fix quoted field names like "project" = "PROJ" -> project = "PROJ"
            # Some LLMs quote field names which is invalid in JQL/CQL
            s = re.sub(r'["\'](\w+)["\']\s*([=~><!]=?|!~|(?:\b(?:NOT\s+)?(?:IN|IS|WAS|CHANGED)\b))', r'\1 \2', s, flags=re.IGNORECASE)

            # Remove trailing dangling operators or templates like 'AND (' or 'AND ("'
            s = re.sub(r'\s+(?:AND|OR)\s+\(\s*["\']?\s*$', '', s, flags=re.IGNORECASE)

            # 1. Fix common syntax mistakes: == instead of =, =~ instead of ~
            s = re.sub(r"==+", "=", s)
            s = re.sub(r"=\s*~", "~", s)

            # 1.5 Fix now() hallucinations (e.g. now("-6M") or "now(\"-6M\")")
            def replace_now(match):
                inner = match.group(1).strip('"\'\\ ')
                m_offset = re.match(r'(-?\d+)([a-zA-Z]+)', inner)
                
                # Default if no offset or unparseable
                if not m_offset:
                    if is_cql:
                        return f'"{dt.datetime.now().strftime("%Y-%m-%d")}"'
                    return "now()"
                
                val = int(m_offset.group(1))
                unit = m_offset.group(2)
                
                if is_cql:
                    # CQL absolute date conversion
                    target_date = dt.datetime.now()
                    if unit.lower() == 'm': # Months
                        target_date += dt.timedelta(days=val * 30)
                    elif unit.lower() == 'w':
                        target_date += dt.timedelta(weeks=val)
                    elif unit.lower() == 'd':
                        target_date += dt.timedelta(days=val)
                    return f'"{target_date.strftime("%Y-%m-%d")}"'
                else:
                    # JQL relative date
                    if unit == 'M': # Months
                        return f"{val * 4}w"
                    return f"{val}{unit}"

            pattern_now = r'["\']?now\(([^)]*)\)["\']?'
            s = re.sub(pattern_now, replace_now, s)
            
            # 2. Fix common mistakes: using 'page' or 'title' in JQL/CQL and hallucinated date fields
            if is_cql:
                s = re.sub(r"\bpage(\s*(?:[=~]|(?:NOT\s+)?IN))", r"title\1", s, flags=re.IGNORECASE)
                # CQL common field hallucinations
                s = re.sub(r"\b(?:comment|description|body)(\s*(?:[=~]|(?:NOT\s+)?IN))", r"text\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\bsummary(\s*(?:[=~]|(?:NOT\s+)?IN))", r"title\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\bupdatedBy(\s*(?:[=~]|(?:NOT\s+)?IN))", r"contributor\1", s, flags=re.IGNORECASE)

                # Fix hallucinations: updatedDate, createdDate, commentDate, etc. -> lastmodified or created
                s = re.sub(r"\b(?:updated|modified|lastModified|updatedDate|lastmodifiedDate)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"lastmodified\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\b(?:createdDate|createdAt|created_at)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"created\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\b(?:contentId|pageId|id_at)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"id\1", s, flags=re.IGNORECASE)
                s = re.sub(r"ORDER\s+BY\s+(?:updated|modified|lastModified|updatedDate|lastmodifiedDate)\b", "ORDER BY lastmodified", s, flags=re.IGNORECASE)
                s = re.sub(r"ORDER\s+BY\s+(?:createdDate|createdAt|created_at)\b", "ORDER BY created", s, flags=re.IGNORECASE)
                
                # Fix 'space is not EMPTY' or 'space is not null' hallucinations (Confluence CQL doesn't support this for space)
                s = re.sub(r"\bspace\s+is\s+not\s+(?:EMPTY|null)\s+AND\s+", "", s, flags=re.IGNORECASE)
                s = re.sub(r"\s+AND\s+space\s+is\s+not\s+(?:EMPTY|null)\b", "", s, flags=re.IGNORECASE)
                s = re.sub(r"\bspace\s+is\s+not\s+(?:EMPTY|null)\b", "", s, flags=re.IGNORECASE)
            else:
                s = re.sub(r"\b(?:page|body)(\s*(?:[=~]|(?:NOT\s+)?IN))", r"text\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\btitle(\s*(?:[=~]|(?:NOT\s+)?IN))", r"summary\1", s, flags=re.IGNORECASE)
                # Fix 'comment by' or 'commented by' hallucinations -> updatedBy in
                s = re.sub(r"\bcomment(?:ed)?\s+by\s+(?:in\s+)?", "updatedBy in ", s, flags=re.IGNORECASE)
                # JQL hallucinations: updatedDate, resolvedDate, commentDate, etc.
                s = re.sub(r"\b(?:updatedDate|updated_at)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"updated\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\b(?:createdDate|created_at)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"created\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\b(?:resolvedDate|resolutionDate|resolved_at)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"resolutiondate\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\b(?:commentDate|commented)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"updated\1", s, flags=re.IGNORECASE)
                s = re.sub(r"\b(?:issueKey|issue_key)(\s*(?:[=~]|(?:NOT\s+)?IN|[><]=?))", r"key\1", s, flags=re.IGNORECASE)
                
                # Fix 'id = "..."' -> 'key = "..."' if value is not numeric
                def fix_id_field(match):
                    op = match.group(1)
                    val = match.group(2).strip("\"'")
                    if not val.isdigit():
                        return f"key {op} \"{val}\""
                    return match.group(0)
                s = re.sub(r"\bid\s*([=~]|!=)\s*(\"[^\"]*\"|'[^']*')", fix_id_field, s, flags=re.IGNORECASE)

                s = re.sub(r"ORDER\s+BY\s+(?:updatedDate|updated_at)\b", "ORDER BY updated", s, flags=re.IGNORECASE)
                s = re.sub(r"ORDER\s+BY\s+(?:createdDate|created_at)\b", "ORDER BY created", s, flags=re.IGNORECASE)
                s = re.sub(r"ORDER\s+BY\s+(?:resolvedDate|resolutionDate|resolved_at|resolvedDate|commentDate|commented)\b", "ORDER BY resolutiondate", s, flags=re.IGNORECASE)

            # 3. Fix invalid 'field ~ (val1 OR val2)' or 'field ~ (val1 val2)' syntax
            # Standard JQL/CQL does not support list-of-values with the contains operator (~)
            # We transform 'text ~ ("a" OR "b")' or 'text ~ "(a OR b)"' to '(text ~ "a" OR text ~ "b")'
            def replace_contains_list(match):
                field = match.group(1)
                # group 2 is inside quoted parens, group 3 is inside unquoted parens
                values_str = match.group(2) or match.group(3)
                if not values_str:
                    return match.group(0)
                # Extract quoted strings or words, ignoring 'OR' and commas
                # We handle escaped quotes inside
                values = re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[\w.-]+', values_str)
                values = [v for v in values if v.upper() != 'OR']
                if not values:
                    return match.group(0)
                # If it's just one value, keep it simple
                if len(values) == 1:
                    return f"{field} ~ {values[0]}"
                new_parts = [f'{field} ~ {v}' for v in values]
                return "(" + " OR ".join(new_parts) + ")"

            # Pattern: field ~ (val1 OR val2 ...) or field ~ "(val1 OR val2 ...)"
            # Group 1: field, Group 2: inner if quoted, Group 3: inner if unquoted
            pattern = r'(\b\w+)\s*~\s*(?:["\']\s*\(([^)]+)\)\s*["\']|\(([^)]+)\))'
            for _ in range(5):
                temp_s = re.sub(pattern, replace_contains_list, s, flags=re.IGNORECASE)
                if temp_s == s:
                    break
                s = temp_s

            # 3.5 Fix unquoted reserved words in text searches (e.g. text ~ AND -> text ~ "AND")
            reserved_jql_words = {"AND", "OR", "NOT", "IN", "IS", "WAS", "BY", "ORDER", "GROUP", "NULL", "EMPTY"}
            def quote_reserved(match):
                field = match.group(1)
                op = match.group(2)
                val = match.group(3)
                if val.upper() in reserved_jql_words:
                    return f'{field} {op} "{val}"'
                return match.group(0)
            
            s = re.sub(r'(\b\w+)\s*([=~])\s*(\b[a-zA-Z]+\b)', quote_reserved, s)

            # 3.7 Fix backwards IN syntax: "Value" IN (field1, field2) -> (field1 = "Value" OR field2 = "Value")
            known_fields = {"creator", "contributor", "assignee", "reporter", "project", "space", "priority", "status", "issuetype", "type"}
            def fix_backwards_in(match):
                val = match.group(1)
                fields_str = match.group(2)
                fields = [f.strip().strip('"\'') for f in re.split(r',\s*', fields_str)]
                if any(f.lower() in known_fields for f in fields):
                    return "(" + " OR ".join([f"{f} = {val}" for f in fields]) + ")"
                return match.group(0)
            
            s = re.sub(r'((?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'))\s+IN\s+\(([^)]+)\)', fix_backwards_in, s, flags=re.IGNORECASE)

            # 4. Fix incorrectly escaped quotes within values
            # Use a greedy approach to find the start of a quoted value and its "true" end
            def fix_quotes(query: str) -> str:
                # Find all field assignments that start with a quote
                # Group 1: prefix, Group 2: field, Group 3: operator, Group 4: quote char, Group 5: remainder
                pattern = r'(.*?)\b(\w+)\s*([=~><!]=?|!~|(?:\b(?:NOT\s+)?(?:IN|IS|WAS)\b))\s*(["\'])(.*)'
                match = re.search(pattern, query, re.IGNORECASE | re.DOTALL)
                if not match:
                    return query
                    
                prefix = match.group(1)
                field = match.group(2)
                op = match.group(3)
                quote_char = match.group(4)
                remainder = match.group(5)
                
                # Find all unescaped occurrences of the same quote character in remainder
                quotes = [i for i, char in enumerate(remainder) if char == quote_char and (i == 0 or remainder[i-1] != '\\')]
                
                best_closing_idx = -1
                for q_idx in quotes:
                    after_quote = remainder[q_idx+1:].strip()
                    if not after_quote or after_quote.startswith(')'):
                        best_closing_idx = q_idx
                        break
                    
                    # Check if followed by a keyword
                    keyword_match = re.match(r'^(?:AND|OR|ORDER BY|GROUP BY|LIMIT)\b', after_quote, re.IGNORECASE)
                    if keyword_match:
                        kw = keyword_match.group(0).upper()
                        if kw in ["ORDER BY", "GROUP BY", "LIMIT"]:
                            # These are terminal or followed by field lists, so they are strong markers of a closing quote
                            best_closing_idx = q_idx
                            break
                            
                        after_keyword = after_quote[keyword_match.end():].strip()
                        # For AND/OR, a valid next part is either another keyword, a field assignment, or end of string
                        if not after_keyword or \
                           re.match(r'\(*\s*\b\w+\s*(?:[=~><!]=?|!~|(?:\b(?:NOT\s+)?(?:IN|IS|WAS|CHANGED)\b))', after_keyword, re.IGNORECASE) or \
                           re.match(r'^(?:AND|OR|ORDER BY|GROUP BY|LIMIT)\b', after_keyword, re.IGNORECASE):
                            best_closing_idx = q_idx
                            break
                
                if best_closing_idx == -1:
                    if not quotes:
                        # Missing closing quote entirely? Add one at the end.
                        value = remainder
                        post_value = ""
                    else:
                        # Fallback to the last quote if no better terminator found
                        best_closing_idx = quotes[-1]
                        value = remainder[:best_closing_idx]
                        post_value = remainder[best_closing_idx+1:]
                else:
                    value = remainder[:best_closing_idx]
                    post_value = remainder[best_closing_idx+1:]
                
                # Escape any unescaped quotes of the same type in the value
                escaped_value = ""
                for i, char in enumerate(value):
                    if char == quote_char:
                        if i == 0 or value[i-1] != '\\':
                            escaped_value += '\\' + quote_char
                        else:
                            escaped_value += char
                    else:
                        escaped_value += char
                        
                sanitized_part = f'{field} {op} {quote_char}{escaped_value}{quote_char}'
                return prefix + sanitized_part + fix_quotes(post_value)

            s = fix_quotes(s)

            # 5. Handle concatenated queries with multiple ORDER BY
            if "ORDER BY" in s.upper() and " AND " in s.upper():
                parts = []
                temp_s = s
                while True:
                    m_order = re.search(r'\bORDER BY\b', temp_s, re.IGNORECASE)
                    if not m_order:
                        if temp_s.strip(): parts.append(temp_s.strip())
                        break
                    m_and = re.search(r'\bAND\b', temp_s[m_order.end():], re.IGNORECASE)
                    if m_and:
                        split_at = m_order.end() + m_and.start()
                        parts.append(temp_s[:split_at].strip())
                        temp_s = temp_s[m_order.end() + m_and.end():].strip()
                    else:
                        if temp_s.strip(): parts.append(temp_s.strip())
                        break
                if len(parts) > 1:
                    return [p for p in parts if p]

            # 6. Fix multiple ORDER BY clauses in a single query (if not split)
            # This handles cases where LLM repeats ORDER BY without a joining AND
            order_matches = list(re.finditer(r'\bORDER BY\b', s, re.IGNORECASE))
            if len(order_matches) > 1:
                last_match = order_matches[-1]
                body = s[:last_match.start()]
                # Remove extra ORDER BY and their field lists until the next likely clause or end of body
                # A clause start is a field followed by =, ~, or IN.
                body_cleaned = re.sub(r'\bORDER BY\b.*?(?=\b\w+\s*(?:[=~]|(?:NOT\s+)?IN\b)|$)', '', body, flags=re.IGNORECASE | re.DOTALL)
                s = body_cleaned + s[last_match.start():]

            # 7. Fix invalid CQL types
            if is_cql:
                valid_types = {"page", "blogpost", "attachment", "comment", "space", "user"}
                
                # Case 1: type = "val" or type ~ "val"
                def replace_type(match):
                    field = match.group(1)
                    op = match.group(2)
                    quote = match.group(3)
                    val = match.group(4)
                    
                    if field.lower() == "type":
                        # Force valid operator for type
                        if op not in ("=", "!="):
                            op = "="
                            
                        if val.lower() not in valid_types:
                            return f"type {op} {quote}page{quote}"
                        return f"type {op} {quote}{val}{quote}"
                    else:
                        # For creator/contributor, force = if ~ was used
                        if field.lower() in ("creator", "contributor") and op == "~":
                            return f"{field} = {quote}{val}{quote}"
                    return match.group(0)

                pattern_type = r'\b(type|creator|contributor)\s*([=~]|!=)\s*(["\'])(.*?)\3'
                s = re.sub(pattern_type, replace_type, s, flags=re.IGNORECASE)
                
                # Case 2: type = unquoted_val
                def replace_type_unquoted(match):
                    field = match.group(1)
                    op = match.group(2)
                    val = match.group(3)
                    
                    if field.lower() == "type":
                        if op not in ("=", "!="):
                            op = "="
                            
                        if val.lower() not in valid_types:
                            return f"type {op} page"
                        return f"type {op} {val}"
                    else:
                        if field.lower() in ("creator", "contributor") and op == "~":
                            return f"{field} = {val}"
                    return match.group(0)
                pattern_type_unquoted = r'\b(type|creator|contributor)\s*([=~]|!=)\s*([^"\'\s)]+)'
                s = re.sub(pattern_type_unquoted, replace_type_unquoted, s, flags=re.IGNORECASE)

                # Case 3: type IN ("val1", "val2")
                def replace_type_in(match):
                    field = match.group(1)
                    op = match.group(2)
                    values_str = match.group(3)
                    # Extract quoted or unquoted values
                    vals = re.findall(r'["\'](.*?)["\']|([^,\s]+)', values_str)
                    cleaned_vals = []
                    for v_quoted, v_unquoted in vals:
                        v = v_quoted or v_unquoted
                        if not v: continue
                        if v.lower() in valid_types:
                            cleaned_vals.append(f'"{v}"')
                        else:
                            cleaned_vals.append('"page"')
                    
                    # Deduplicate
                    seen = set()
                    final_vals = []
                    for v in cleaned_vals:
                        if v.lower() not in seen:
                            final_vals.append(v)
                            seen.add(v.lower())
                            
                    if not final_vals:
                        return f'type {op} ("page")'
                    return f'type {op} ({", ".join(final_vals)})'

                pattern_type_in = r'\b(type)\s+(IN|NOT\s+IN)\s*\(([^)]+)\)'
                s = re.sub(pattern_type_in, replace_type_in, s, flags=re.IGNORECASE)

                # Case 4: Fix invalid 'ancestor' or 'parent' syntax (must be numeric ID)
                # LLMs often hallucinate names or fields like 'creator' inside ancestor/parent
                def fix_hierarchy_id(match):
                    field = match.group(1)
                    op = match.group(2)
                    remainder = match.group(3).strip()
                    
                    if op.upper() in ("IN", "NOT IN"):
                        # Extract all numbers
                        ids = re.findall(r'\b\d+\b', remainder)
                        if ids:
                            return f"{field} {op} ({', '.join(ids)})"
                        # No numbers? Check if there are names that we should perhaps convert to other fields
                        # e.g. ancestor in (creator, "Sandy Cox") -> (creator = "Sandy Cox")
                        names = re.findall(r'["\'](.*?)["\']', remainder)
                        if names:
                            clauses = [f'creator = "{n}" OR contributor = "{n}"' for n in names]
                            return "(" + " OR ".join(clauses) + ")"
                        return "" # Remove invalid clause
                    else:
                        # Single value
                        if re.match(r'^\d+$', remainder):
                            return f"{field} {op} {remainder}"
                        # If it's a name, try to salvage
                        name_match = re.match(r'^["\']?(.*?)["\']?$', remainder)
                        if name_match:
                            n = name_match.group(1)
                            if n and not n.isdigit():
                                return f'(creator = "{n}" OR contributor = "{n}")'
                        return "" # Remove invalid clause

                # Catch ancestor = ID, parent = ID, ancestor IN (IDs), etc.
                s = re.sub(r'\b(ancestor|parent)\s*([=~]|!=|(?:NOT\s+)?IN)\s*(\(.*?\)|[^)\s]+)', fix_hierarchy_id, s, flags=re.IGNORECASE)
                # Cleanup double spaces or OR OR that might result from removal
                s = re.sub(r'\s+OR\s+\(\s*OR\s+', ' OR (', s, flags=re.IGNORECASE)
                s = re.sub(r'\s+AND\s+\(\s*AND\s+', ' AND (', s, flags=re.IGNORECASE)
                s = re.sub(r'\(\s*(?:OR|AND)\s+', '(', s, flags=re.IGNORECASE)
                s = re.sub(r'\s+(?:OR|AND)\s+\)', ')', s, flags=re.IGNORECASE)
                s = re.sub(r'^\s*(?:OR|AND)\s+', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\s+(?:OR|AND)\s*$', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\b(?:OR|AND)\s+\(\s*\)', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\(\s*\)\s+\b(?:OR|AND)', '', s, flags=re.IGNORECASE)
                s = s.replace("()", "").strip()

            # 8. Fix invalid 'field IN (val1 OR val2)' syntax
            # JQL/CQL IN clause expects comma-separated values, NOT 'OR'
            def fix_in_clause_or(match):
                field = match.group(1)
                op = match.group(2)
                values_str = match.group(3)
                if " OR " in values_str.upper():
                    # Extract values, ignoring OR
                    values = re.findall(r'"[^"]*"|\'[^\']*\'|\w+', values_str)
                    values = [v for v in values if v.upper() != 'OR' and v != ',']
                    return f"{field} {op} ({', '.join(values)})"
                return match.group(0)

            s = re.sub(r'(\b\w+)\s+(IN|NOT\s+IN)\s*\(([^)]+)\)', fix_in_clause_or, s, flags=re.IGNORECASE)

            # 8.5 Fix 'issue in (condition)' hallucinations
            def fix_issue_in_condition(match):
                inner = match.group(1).strip()
                # If it looks like a field assignment or function call, it's not a valid list of keys
                if re.search(r'\b\w+\s*(?:[=~><!]|(?:NOT\s+)?IN\b|\()', inner, re.IGNORECASE):
                    return f"({inner})"
                return match.group(0)
            s = re.sub(r'\bissue\s+in\s*\(([^)]+)\)', fix_issue_in_condition, s, flags=re.IGNORECASE)

            # 9. Fix Reversed or Multi-field IN clauses
            def replace_multi_in(match):
                p1_inside = match.group(1)
                p1_word = match.group(2)
                p2_inside = match.group(3)
                p2_word = match.group(4)
                
                part1 = (p1_inside or p1_word).strip()
                part2 = (p2_inside or p2_word).strip()
                
                person_fields = {"assignee", "reporter", "watcher", "voter", "creator", "contributor", "author"}
                
                # Extract words to identify fields
                words1 = re.findall(r'\w+', part1)
                words2 = re.findall(r'\w+', part2)
                
                # Identify person fields
                fields1 = [w for w in words1 if w.lower() in person_fields]
                fields2 = [w for w in words2 if w.lower() in person_fields]
                
                # Identify values (look for OR, quotes, or just not being standard fields)
                has_vals1 = " OR " in part1.upper() or '"' in part1 or "'" in part1
                has_vals2 = " OR " in part2.upper() or '"' in part2 or "'" in part2
                
                # Case A: (vals) IN (fields) -> Reversed
                if has_vals1 and fields2 and not fields1:
                    # part1 is vals, part2 is fields
                    values = re.findall(r'"[^"]*"|\'[^\']*\'|\w+', part1)
                    values = [v for v in values if v.upper() != 'OR' and v != ',']
                    val_list = ", ".join(values)
                    
                    target_fields = fields2
                    if not target_fields: target_fields = words2
                    
                    res = " OR ".join([f"{f} IN ({val_list})" for f in target_fields])
                    return f"({res})" if len(target_fields) > 1 else res

                # Case B: (fields) IN (vals) -> Multi-field (invalid JQL)
                if len(fields1) > 1 and not has_vals1:
                    # (assignee, reporter) IN ("User A")
                    res = " OR ".join([f"{f} IN ({part2})" for f in fields1])
                    return f"({res})"
                    
                return match.group(0)

            # Regex to catch (part1) IN (part2) or field IN (part2) or (part1) IN field
            pattern_in = r'(?:\(([^)]+)\)|(\b\w+))\s+IN\s+(?:\(([^)]+)\)|(\b\w+))'
            s = re.sub(pattern_in, replace_multi_in, s, flags=re.IGNORECASE)

            # 10. Fix spaces in labels (Jira labels cannot have spaces)
            def fix_labels(match):
                field = match.group(1)
                op = match.group(2)
                values_str = match.group(3)
                # Split and clean
                values = re.findall(r'"[^"]*"|\'[^\']*\'|[\w&.-]+', values_str)
                cleaned = []
                for v in values:
                    if v.upper() == 'OR' or v == ',': continue
                    unquoted = v.strip('"\'')
                    if ' ' in unquoted:
                        # Replace spaces with dashes as it is the most common convention
                        cleaned.append(f'"{unquoted.replace(" ", "-")}"')
                    else:
                        cleaned.append(v)
                return f"{field} {op} ({', '.join(cleaned)})"

            s = re.sub(r'\b(labels?)\s+(IN|NOT\s+IN)\s*\(([^)]+)\)', fix_labels, s, flags=re.IGNORECASE)

            # 11. Filter out hallucinated projects/spaces
            self._fetch_available_containers()
            if self._containers_fetched and (self._available_projects or self._available_spaces):
                def filter_containers(match):
                    field = match.group(1)
                    op = match.group(2)
                    values_str = match.group(3)
                    
                    field_lower = field.lower()
                    is_project = field_lower == "project"
                    available = self._available_projects if is_project else self._available_spaces
                    
                    if not available:
                        return match.group(0)
                        
                    # Extract values
                    raw_vals = re.findall(r'"([^"]*)"|\'([^\']*)\'|([\w-]+)', values_str)
                    cleaned = []
                    for v_double, v_single, v_word in raw_vals:
                        v = v_double or v_single or v_word
                        if not v or v.upper() == 'OR' or v == ',': continue
                        if v.upper() in available:
                            # Always quote identifiers for CQL safety
                            cleaned.append(f'"{v}"')
                        else:
                            logger.info(f"Filtering out hallucinated {field_lower}: {v}")
                    
                    if not cleaned:
                        # If all filtered, return an empty string if it was just placeholders, otherwise a dummy search
                        has_placeholder = any(v.upper() in COMMON_PLACEHOLDERS for v_double, v_single, v_word in raw_vals for v in [v_double or v_single or v_word] if v)
                        if has_placeholder:
                            return ""
                        return f"text ~ \"{field_lower}\""
                    
                    if len(cleaned) == 1 and op.upper() in ("IN", "NOT IN"):
                        new_op = "=" if op.upper() == "IN" else "!="
                        return f"{field} {new_op} \"{cleaned[0].strip('\"')}\""
                        
                    return f"{field} {op} ({', '.join(cleaned)})"

                s = re.sub(r'\b(project|space)\s+(IN|NOT\s+IN)\s*\(([^)]+)\)', filter_containers, s, flags=re.IGNORECASE)
                
                def filter_single_container(match):
                    groups = match.groups()
                    field = groups[0]
                    op = groups[1]
                    
                    if len(groups) == 4:
                        # Quoted case
                        val = groups[3]
                    else:
                        # Unquoted case
                        val = groups[2]
                    
                    field_lower = field.lower()
                    available = self._available_projects if field_lower == "project" else self._available_spaces
                    
                    if not available:
                        return match.group(0)
                        
                    if val.upper() in available:
                        # Always ensure it is quoted for CQL/JQL safety
                        return f"{field} {op} \"{val}\""
                    else:
                        if val.upper() in COMMON_PLACEHOLDERS:
                            logger.info(f"Removing placeholder {field_lower}: {val}")
                            return ""
                        logger.info(f"Filtering out hallucinated {field_lower}: {val}")
                        return f"text ~ \"{val}\""

                s = re.sub(r'\b(project|space)\s*([=~]|!=)\s*(["\'])(.*?)\3', filter_single_container, s, flags=re.IGNORECASE)
                # Unquoted single word
                s = re.sub(r'\b(project|space)\s*([=~]|!=)\s*\b([\w-]+)\b', filter_single_container, s, flags=re.IGNORECASE)

            # 12. Filter out hallucinated person names
            person_fields = {"assignee", "reporter", "creator", "contributor", "updatedby"}
            self._fetch_cached_names()
            # Valid names = subjects + cached names
            valid_names = set(self.subjects)
            if self.name_cache:
                valid_names.update(self.name_cache)
            
            if valid_names:
                def filter_names(match):
                    field = match.group(1)
                    op = match.group(2)
                    values_str = match.group(3)
                    
                    if field.lower() not in person_fields:
                        return match.group(0)
                        
                    # Extract values
                    raw_vals = re.findall(r'"([^"]*)"|\'([^\']*)\'|([\w\s.-]+)', values_str)
                    cleaned = []
                    for v_double, v_single, v_word in raw_vals:
                        v = v_double or v_single or v_word
                        if not v or v.upper() == 'OR' or v == ',': continue
                        
                        v_strip = v.strip()
                        # Check if name is in valid_names (case-insensitive check)
                        is_valid = False
                        v_lower = v_strip.lower()
                        for vn in valid_names:
                            if v_lower == vn.lower():
                                is_valid = True
                                break
                        
                        if is_valid:
                            cleaned.append(f'"{v_strip}"')
                        else:
                            logger.info(f"Filtering out hallucinated person name in {field}: {v_strip}")
                    
                    if not cleaned:
                        return "" # Remove entire clause if no valid names
                    
                    if len(cleaned) == 1 and op.upper() in ("IN", "NOT IN"):
                        new_op = "=" if op.upper() == "IN" else "!="
                        return f"{field} {new_op} \"{cleaned[0].strip('\"')}\""
                        
                    return f"{field} {op} ({', '.join(cleaned)})"

                s = re.sub(r'\b(assignee|reporter|creator|contributor|updatedBy)\s+(IN|NOT\s+IN)\s*\(([^)]+)\)', filter_names, s, flags=re.IGNORECASE)
                
                def filter_single_name(match):
                    groups = match.groups()
                    field = groups[0]
                    op = groups[1]
                    
                    if len(groups) == 4:
                        # Quoted case
                        val = groups[3]
                    else:
                        # Unquoted case
                        val = groups[2]
                    
                    if field.lower() not in person_fields:
                        return match.group(0)
                        
                    v_strip = val.strip()
                    v_lower = v_strip.lower()
                    is_valid = False
                    for vn in valid_names:
                        if v_lower == vn.lower():
                            is_valid = True
                            break
                    
                    if is_valid:
                        return f'{field} {op} "{v_strip}"'
                    else:
                        logger.info(f"Filtering out hallucinated single person name in {field}: {v_strip}")
                        return ""

                # Quoted case: field = "Name"
                s = re.sub(r'\b(assignee|reporter|creator|contributor|updatedBy)\s*([=~]|!=)\s*(["\'])(.*?)\3', filter_single_name, s, flags=re.IGNORECASE)
                # Unquoted case: field = Name
                s = re.sub(r'\b(assignee|reporter|creator|contributor|updatedBy)\s*([=~]|!=)\s*([^"\'\s)]+)', filter_single_name, s, flags=re.IGNORECASE)
                
                # Cleanup leftover operators and empty groups
                s = re.sub(r'\s+(?:AND|OR)\s+\(\s*["\']?\s*$', '', s, flags=re.IGNORECASE)
                s = re.sub(r'^\s*(?:AND|OR)\s+', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\s+(?:AND|OR)\s*$', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\b(?:OR|AND)\s+\(\s*\)', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\(\s*\)\s+\b(?:OR|AND)', '', s, flags=re.IGNORECASE)
                s = s.replace("()", "").strip()

            # 13. Fix unquoted reserved words as values for contains (~) or equals (=)
            # This handles cases like 'text ~ AND' or 'summary ~ OR' which are invalid JQL
            # We target specific common fields to avoid false positives with operators
            s = re.sub(
                r'(\b(?:text|summary|title|description|comment|creator|contributor|assignee|reporter))\s*([=~])\s*\b(AND|OR|NOT|IN|IS|WAS|ORDER|BY|DESC|ASC|GROUP|LIMIT|NULL)\b', 
                r'\1 \2 "\3"', 
                s, 
                flags=re.IGNORECASE
            )

            # 14. Escape parentheses in contains (~) values if not already escaped
            # This prevents 400 errors when searching for terms with parentheses like "Payment (Basic)"
            def escape_parens_in_contains(match):
                prefix = match.group(1)
                field = match.group(2)
                op = match.group(3)
                quote = match.group(4)
                value = match.group(5)
                # Escape ( and ) if they are not preceded by \
                val_escaped = re.sub(r'(?<!\\)\(', r'\\(', value)
                val_escaped = re.sub(r'(?<!\\)\)', r'\\)', val_escaped)
                return f"{prefix}{field} {op} {quote}{val_escaped}{quote}"

            s = re.sub(r'(^|[\s(])(\w+)\s*(!?~)\s*(["\'])(.*?)\4', escape_parens_in_contains, s, flags=re.DOTALL)

            # 12.5. Remove any clauses containing generic placeholders
            for placeholder in COMMON_PLACEHOLDERS:
                # Matches field op "placeholder", field op placeholder, or placeholder in an IN list
                # This is a broad stroke to clean up hallucinated placeholders
                pattern = rf'\b\w+\s*(?:[=~]|!=|(?:NOT\s+)?IN)\s*(?:\(\s*)?["\']?{re.escape(placeholder)}["\']?(?:\s*\))?'
                s = re.sub(pattern, '', s, flags=re.IGNORECASE)

            # 13. Final Parentheses Balance
            s = self._balance_parentheses(s)

            # 14. Final operator and whitespace cleanup to handle removed placeholders
            if isinstance(s, str):
                # Fix double operators like 'AND AND' or 'AND OR'
                s = re.sub(r'\s+(?:AND|OR)\s+(?:AND|OR)\s+', ' AND ', s, flags=re.IGNORECASE)
                # Remove dangling operators inside parentheses
                s = re.sub(r'\(\s*(?:AND|OR)\s+', '(', s, flags=re.IGNORECASE)
                s = re.sub(r'\s+(?:AND|OR)\s*\)', ')', s, flags=re.IGNORECASE)
                # Remove dangling operators at start/end
                s = re.sub(r'^\s*(?:AND|OR)\s+', '', s, flags=re.IGNORECASE)
                s = re.sub(r'\s+(?:AND|OR)\s*$', '', s, flags=re.IGNORECASE)
                # Fix empty parens
                s = re.sub(r'\(\s*\)', '', s)
                # Final trim
                s = s.strip()

            return s

        if "cql" in queries:
            val = queries["cql"]
            if isinstance(val, str):
                queries["cql"] = sanitize_string(val, is_cql=True)
            elif isinstance(val, list):
                new_list = []
                for item in val:
                    res = sanitize_string(str(item), is_cql=True)
                    if isinstance(res, list):
                        new_list.extend(res)
                    else:
                        new_list.append(res)
                queries["cql"] = new_list
        
        if "jql" in queries:
            val = queries["jql"]
            if isinstance(val, str):
                queries["jql"] = sanitize_string(val, is_cql=False)
            elif isinstance(val, list):
                new_list = []
                for item in val:
                    res = sanitize_string(str(item), is_cql=False)
                    if isinstance(res, list):
                        new_list.extend(res)
                    else:
                        new_list.append(res)
                queries["jql"] = new_list
            
        return queries

    def _try_fix_truncated_json(self, text: str) -> Optional[str]:
        """
        Attempts to fix truncated JSON by adding missing closing braces/brackets and quotes.
        """
        if not text:
            return None
            
        fixed = text.strip()
        
        # If it ends with a comma, remove it
        if fixed.endswith(','):
            fixed = fixed[:-1].strip()

        # Handle unclosed quotes
        if fixed.count('"') % 2 != 0:
            fixed += '"'
        
        # Count opening vs closing
        braces = fixed.count('{') - fixed.count('}')
        brackets = fixed.count('[') - fixed.count(']')
        
        # Close brackets first (likely nested inside braces)
        if brackets > 0:
            fixed += ']' * brackets
        if braces > 0:
            fixed += '}' * braces
        
        try:
            data = json.loads(fixed)
            if isinstance(data, dict):
                return fixed
        except:
            return None

    def _extract_queries_from_text(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Attempts to extract queries from Markdown-style text if JSON extraction fails.
        """
        if not text:
            return None
            
        queries = {
            "jql": "",
            "cql": "",
            "search_queries": [],
            "llm_questions": []
        }
        
        # 1. Extract JQL and CQL (often in backticks)
        # Use more restrictive regex to avoid capturing noise from subsequent sections
        jql_match = re.search(r"(?:JQL|Jira [Qq]uery)[^`\n]{0,100}[:\s]*`([^`\n]+)`", text, re.IGNORECASE)
        if not jql_match:
            jql_match = re.search(r"(?:JQL|Jira [Qq]uery)[^:\n]*?[:\s]+([^\n\*]{5,})", text, re.IGNORECASE)
        if jql_match:
            queries["jql"] = jql_match.group(1).strip()
            
        cql_match = re.search(r"(?:CQL|Confluence [Qq]uery)[^`\n]{0,100}[:\s]*`([^`\n]+)`", text, re.IGNORECASE)
        if not cql_match:
            cql_match = re.search(r"(?:CQL|Confluence [Qq]uery)[^:\n]*?[:\s]+([^\n\*]{5,})", text, re.IGNORECASE)
        if cql_match:
            queries["cql"] = cql_match.group(1).strip()
            
        # 2. Extract lists for search and LLM questions
        lines = text.split('\n')
        current_section = None
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            
            # Identify section headers
            l_lower = line_stripped.lower()
            if any(k in l_lower for k in ["search queries", "web search", "keywords"]):
                current_section = "search"
                continue
            elif any(k in l_lower for k in ["llm questions", "specific questions"]):
                current_section = "llm"
                continue
            elif any(k in l_lower for k in ["jql", "jira query", "cql", "confluence query"]):
                # Reset if we hit a new major section header we likely already handled or want to skip
                if line_stripped.startswith(('**', '##', '#')):
                    current_section = None
                continue
            
            # If we are in a section, try to extract items
            if current_section == "search":
                # Only add if it looks like a list item and not a header
                if line_stripped.startswith(('*', '-', '1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.')):
                    item = re.sub(r"^[*-]\s*|^[0-9]+\.\s*", "", line_stripped).strip('` ')
                    if len(item) >= 2:
                        queries["search_queries"].append(item)
            elif current_section == "llm":
                if line_stripped.startswith(('*', '-', '1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.')):
                    item = re.sub(r"^[*-]\s*|^[0-9]+\.\s*", "", line_stripped).strip('` ')
                    if len(item) >= 2:
                        queries["llm_questions"].append(item)

        # If we found at least something substantial, return it
        if queries["jql"] or queries["cql"] or queries["search_queries"] or queries["llm_questions"]:
            return queries
            
        return None

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Robustly extracts the first valid JSON object from a string.
        """
        if not text:
            return None
            
        # Try backticks first as they are a strong indicator
        for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
            content = match.group(1).strip()
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    return data
            except:
                # Try to fix truncated JSON inside backticks
                fixed = self._try_fix_truncated_json(content)
                if fixed:
                    try:
                        data = json.loads(fixed)
                        if isinstance(data, dict):
                            return data
                    except:
                        pass
                continue
        
        # Search for any { ... } block
        start_indices = [i for i, char in enumerate(text) if char == '{']
        for start in start_indices:
            for end in range(len(text) - 1, start, -1):
                if text[end] == '}':
                    content = text[start:end+1]
                    try:
                        data = json.loads(content)
                        if isinstance(data, dict):
                            return data
                    except:
                        continue
        
        # Try to fix truncated JSON that might not have a closing brace
        first_brace = text.find('{')
        if first_brace != -1:
            fixed = self._try_fix_truncated_json(text[first_brace:])
            if fixed:
                try:
                    data = json.loads(fixed)
                    if isinstance(data, dict):
                        return data
                except:
                    pass

        return None

    def _generate_queries(self, topic: str, requirements: str, current_draft: str = "", context: str = "", target_section: Optional[str] = None, completeness_feedback: Optional[str] = None) -> Dict[str, Any]:
        """
        Consults the LLM to determine what information is needed next.
        Generates targeted queries for various data sources.
        """
        system_prompt = (
            "You are an expert technical researcher. Based on the topic, requirements, and additional context, "
            "generate specific queries for Jira (JQL), Confluence (CQL), "
            "Web Search Engines (keywords), and specific questions for an LLM.\n"
            "STRICT JQL/CQL RULES:\n"
            "1. JQL and CQL MUST be single valid query strings, NOT URL parameters.\n"
            "2. Use 'AND' and 'OR' operators. Do NOT use '&' to separate fields.\n"
            "3. Example JQL: 'project = \"PROJ\" AND text ~ \"topic\"'.\n"
            "4. Example CQL: 'space = \"SPACE\" AND title = \"topic\"'.\n"
            "5. VALID CQL FIELDS: title, text, space, type, label, created, lastmodified, creator, contributor, parent, ancestor.\n"
            "6. VALID CQL TYPES: page, blogpost, attachment, comment.\n"
            "7. DO NOT use 'page' as a field name in CQL; use 'title' instead. DO NOT use 'updated' in CQL; use 'lastmodified' instead.\n"
            "8. OPERATORS: Use '=' for exact match and '~' for contains. DO NOT use '==' or '=~'.\n"
            "9. ANCESTOR/PARENT: These fields ONLY accept numeric IDs. Do NOT use names or 'creator' inside them.\n"
            "10. STRINGS: Always wrap strings in double quotes. If a string contains a single quote (e.g. O'Reilly), double quotes are MANDATORY.\n"
            "11. DATES: Use absolute dates in 'YYYY-MM-DD' format whenever possible (e.g., 'created >= \"2025-01-01\"'). "
            "Avoid using 'now()' in CQL. In JQL, you may use relative offsets like '-1w' or '-30d'.\n"
            "12. DO NOT put Jira/Confluence URLs in 'search_queries'; use JQL/CQL or specific LLM questions instead.\n"
            "13. DO NOT use generic placeholders like 'PROJECT_KEY_HERE', 'YOUR_PROJECT_KEY', or 'SPACE_KEY_HERE'. "
            "If you don't know the specific project/space, search using keywords in the 'text' or 'summary' fields instead, "
            "or use the provided list of available containers. DO NOT use 'space is not EMPTY' or 'space is not null' in CQL; if you want to search all spaces, simply do not include the 'space' field in your query.\n"
            "14. PERSON NAMES: If searching for individuals, PRIORITISE using the names listed in the requirements. "
            "If you need to search for other people, ONLY use names that have been identified in previous research iterations (see provided Context). "
            "ALWAYS use correct field order: 'field = \"value\"', NOT '\"value\" = field' or '\"value\" IN (field1, field2)'.\n"
            "Focus queries on gathering INTERNAL business context from Jira and Confluence. "
            "If a current draft exists, identify gaps, generic placeholders (like 'Domain A'), or missing detail, "
            "and focus queries on finding specific names, systems, and procedures to replace them.\n"
            "If completeness feedback from a previous iteration is provided, PRIORITISE addressing the missing areas "
            "identified in that feedback by generating queries that target those gaps specifically.\n"
            "If multiple individuals or entities (subjects) are listed in the requirements, ensure that "
            "queries are balanced to gather information about ALL of them equally, so that no one person "
            "is given more attention than another in the report.\n"
            "Use British English for all generated text.\n"
            "MANDATORY: Return your response ONLY as a JSON object with these keys: "
            "'jql', 'cql', 'search_queries', 'llm_questions'.\n"
            "DO NOT include any preamble, introduction, or conversational filler. "
            "If you cannot provide JSON, use clear Markdown headers like '**Jira (JQL)**' and '**Confluence (CQL)**'."
        )
        
        self._fetch_available_containers()
        available_info = ""
        if self._available_projects:
            projs = sorted(list(self._available_projects))[:50]
            available_info += f"\nAvailable Jira projects: {', '.join(projs)}"
            if len(self._available_projects) > 50:
                available_info += " (and more...)"
        if self._available_spaces:
            spaces = sorted(list(self._available_spaces))[:50]
            available_info += f"\nAvailable Confluence spaces: {', '.join(spaces)}"
            if len(self._available_spaces) > 50:
                available_info += " (and more...)"
        
        self._fetch_cached_names()
        if self.name_cache:
            # Show a sample of identified names
            names = sorted(list(self.name_cache))[:50]
            available_info += f"\nIdentified Person Names: {', '.join(names)}"
            if len(self.name_cache) > 50:
                available_info += " (and more...)"

        user_content = f"Topic: {topic}\nRequirements: {requirements}\n"
        if available_info:
            user_content += f"\nContext - Available Infrastructure:{available_info}\n"
        if self.company_name:
            user_content = f"Company: {self.company_name}\n" + user_content
        if context:
            user_content += f"\nAdditional Context:\n{context}\n"
        
        if completeness_feedback:
            user_content += f"\nFEEDBACK ON MISSING AREAS (PRIORITISE THESE): {completeness_feedback}\n"
        
        if target_section:
            user_content += f"\nTARGET SECTION TO FLESH OUT: {target_section}\n"
            user_content += f"Focus your queries specifically on gathering detailed information for the '{target_section}' section.\n"
        
        if current_draft:
            efficient_draft = self._get_token_efficient_context(current_draft, target_section=target_section)
            user_content += f"\nCurrent Draft (Outline/Partial):\n{efficient_draft}\n"
            
            if not target_section:
                thin_sections = self._identify_thin_sections(current_draft)
                if thin_sections:
                    user_content += f"\nNote: The following sections are currently just outlines and need detail: {', '.join(thin_sections[:5])}\n"
        
        logger.info("Generating research queries via LLM")
        try:
            resp = self._predict_with_fallback(
                [{"role": "user", "content": user_content}],
                system=system_prompt,
                **self.llm_params
            )
        except (LLMQuotaError, LLMError) as e:
            logger.warning(f"LLM failure during query generation: {e}. Using fallback queries.")
            # Return fallback_queries defined later
            escaped_topic = topic.replace('"', '\\"')
            req_keywords = " ".join(requirements.split()[:5])
            escaped_reqs = req_keywords.replace('"', '\\"')
            return {
                "jql": f"text ~ \"{escaped_topic}\" OR text ~ \"{escaped_reqs}\"",
                "cql": f"text ~ \"{escaped_topic}\" OR text ~ \"{escaped_reqs}\"",
                "search_queries": [topic, f"{topic} {req_keywords}"],
                "llm_questions": [f"Provide more comprehensive information about {topic}."]
            }
        
        # Better fallback: use both topic and some keywords from requirements
        # Escape quotes for JQL/CQL
        escaped_topic = topic.replace('"', '\\"')
        req_keywords = " ".join(requirements.split()[:5])
        escaped_reqs = req_keywords.replace('"', '\\"')
        
        fallback_queries = {
            "jql": f"text ~ \"{escaped_topic}\" OR text ~ \"{escaped_reqs}\"",
            "cql": f"text ~ \"{escaped_topic}\" OR text ~ \"{escaped_reqs}\"",
            "search_queries": [topic, f"{topic} {req_keywords}"],
            "llm_questions": [f"Provide more comprehensive information about {topic}."]
        }

        if not resp.text or not resp.text.strip():
            logger.warning("LLM returned an empty response for queries. Using fallbacks.")
            return fallback_queries

        try:
            queries = self._extract_json(resp.text)
            if not queries:
                # Try Markdown extraction as a fallback before giving up
                queries = self._extract_queries_from_text(resp.text)
                
            if not queries:
                raise ValueError("No valid JSON or query list found in response")
            
            # Ensure all required keys are present
            required_keys = ["jql", "cql", "search_queries", "llm_questions"]
            for key in required_keys:
                if key not in queries:
                    queries[key] = fallback_queries[key]
            
            return self._sanitize_queries(queries)
        except Exception as e:
            logger.warning(f"Failed to parse LLM queries: {e}. Response was: {resp.text[:200]}... Using fallback queries.")
            return fallback_queries

    def _generate_followup_queries(self, insights: List[Dict[str, str]]) -> Dict[str, Any]:
        """
        Generates follow-up JQL and CQL queries based on LLM insights to ground them in internal data.
        """
        system_prompt = (
            "You are an expert technical researcher. You have just received answers to some general research "
            "questions. Now, you must generate targeted Jira (JQL) and Confluence (CQL) queries to find "
            "actual internal documentation, projects, or tickets that relate to these answers.\n"
            "STRICT JQL/CQL RULES:\n"
            "1. JQL and CQL MUST be single valid query strings, NOT URL parameters.\n"
            "2. Use 'AND' and 'OR' operators. Do NOT use '&' to separate fields.\n"
            "3. Example JQL: 'project = \"PROJ\" AND text ~ \"topic\"'.\n"
            "4. Example CQL: 'space = \"SPACE\" AND title = \"topic\"'.\n"
            "5. VALID CQL FIELDS: title, text, space, type, label, created, lastmodified, creator, contributor, parent, ancestor.\n"
            "6. VALID CQL TYPES: page, blogpost, attachment, comment.\n"
            "7. DO NOT use 'page' as a field name in CQL; use 'title' instead. DO NOT use 'updated' in CQL; use 'lastmodified' instead.\n"
            "8. OPERATORS: Use '=' for exact match and '~' for contains. DO NOT use '==' or '=~'.\n"
            "9. ANCESTOR/PARENT: These fields ONLY accept numeric IDs. Do NOT use names or 'creator' inside them.\n"
            "10. STRINGS: Always wrap strings in double quotes. If a string contains a single quote (e.g. O'Reilly), double quotes are MANDATORY.\n"
            "11. Focus on finding SPECIFIC proof or detail mentioned in the LLM answers.\n"
            "MANDATORY: Return your response ONLY as a JSON object with keys 'jql' and 'cql'."
        )

        insights_text = "\n\n".join([f"Q: {i['question']}\nA: {i['answer']}" for i in insights])
        user_content = f"LLM Insights gathered:\n{insights_text}"
        
        hints = self._get_existing_context_hints()
        if hints:
            user_content = f"{hints}\n\n" + user_content

        logger.info("Generating follow-up research queries based on LLM insights")
        try:
            resp = self._predict_with_fallback(
                [{"role": "user", "content": user_content}],
                system=system_prompt,
                **self.llm_params
            )
            if not resp.text or not resp.text.strip():
                return {}
            
            queries = self._extract_json(resp.text)
            if not queries:
                queries = self._extract_queries_from_text(resp.text)
            
            if queries:
                return self._sanitize_queries(queries)
            return {}
        except Exception as e:
            logger.warning(f"Failed to generate follow-up queries: {e}")
            return {}

    def _adjust_queries(self, original_queries: Dict[str, Any], fail_reason: str) -> Dict[str, Any]:
        """
        Asks the LLM to adjust or broaden queries that produced zero results.
        """
        system_prompt = (
            "You are an expert technical researcher. Some Jira/Confluence queries you generated "
            "produced ZERO results. This is often because the project keys or space names you guessed "
            "do not exist in this system, or are too restrictive.\n"
            "Your task is to provide ADJUSTED queries. \n"
            "STRICT RULES:\n"
            "1. If you used 'project IN (...)' or 'space IN (...)', try removing them or using different keys.\n"
            "2. Ensure you use 'text ~ \"keyword\"' for broad contains search if specific fields failed.\n"
            "3. For Confluence (CQL), use 'lastmodified' instead of 'updated'.\n"
            "4. ANCESTOR/PARENT: These fields ONLY accept numeric IDs. Do NOT use names or 'creator' inside them.\n"
            "5. STRINGS: Always wrap strings in double quotes. If a string contains a single quote (e.g. O'Reilly), double quotes are MANDATORY.\n"
            "6. Maintain the same JSON structure: 'jql' and 'cql'.\n"
            "7. Use the provided context hints about existing projects/spaces if available.\n"
            "MANDATORY: Return your response ONLY as a JSON object with keys 'jql' and 'cql'."
        )
        
        hints = self._get_existing_context_hints()
        user_content = f"Original Queries: {json.dumps(original_queries)}\nReason for adjustment: {fail_reason}\n"
        if hints:
            user_content += f"\n{hints}\n"
            
        logger.info(f"Asking LLM to adjust queries due to: {fail_reason}")
        try:
            resp = self._predict_with_fallback(
                [{"role": "user", "content": user_content}],
                system=system_prompt,
                **self.llm_params
            )
            if not resp.text or not resp.text.strip():
                return {}
            
            queries = self._extract_json(resp.text)
            if not queries:
                queries = self._extract_queries_from_text(resp.text)
            
            if queries:
                return self._sanitize_queries(queries)
            return {}
        except Exception as e:
            logger.warning(f"Failed to adjust queries: {e}")
            return {}

    def _heuristic_relevance_check(self, content: str, topic: str, requirements: str) -> bool:
        """
        A simple keyword-based relevance check as a fallback for LLM when quota is hit.
        Returns True if any significant keywords from topic or requirements are found in content.
        """
        if not content or not topic:
            return False
            
        # Clean and tokenize topic/requirements
        def get_keywords(text: str) -> set[str]:
            # Remove punctuation and common words
            text = re.sub(r'[^\w\s]', ' ', text.lower())
            words = set(text.split())
            stop_words = {'a', 'an', 'the', 'and', 'or', 'but', 'if', 'then', 'else', 'is', 'are', 'to', 'for', 'of', 'in', 'on', 'with'}
            return {w for w in words if len(w) > 3 and w not in stop_words}
            
        topic_keywords = get_keywords(topic)
        req_keywords = get_keywords(requirements)
        combined_keywords = topic_keywords.union(req_keywords)
        
        if not combined_keywords:
            return True # Fallback to True if no good keywords found
            
        content_lower = content.lower()
        
        # Check for any keyword matches
        matches = [kw for kw in combined_keywords if kw in content_lower]
        
        # Heuristic: if at least 2 unique keywords match, or one keyword matches multiple times
        # Or if the topic is mentioned exactly
        if topic.lower() in content_lower:
            return True
            
        return len(matches) >= 2

    def _is_content_relevant(self, content: str, topic: str, requirements: str, is_resumption: bool = False) -> bool:
        """
        Uses the LLM to determine if a piece of content is relevant to the research topic and requirements.
        """
        if not content or not topic:
            return False
            
        if is_resumption:
            system_prompt = (
                "You are a technical research assistant. Your task is to determine if an EXISTING research report "
                "is relevant to the specified research topic and requirements. This report may have been generated "
                "in a previous session and we want to determine if we can continue building upon it.\n"
                "RELEVANCE CRITERIA FOR RESUMPTION:\n"
                "1. If the document discusses the same or a very similar topic, it is relevant.\n"
                "2. Even if it is incomplete or an early draft, if it provides a foundation aligned with the requirements, it is relevant.\n"
                "3. Only say NO if the document is about a completely different subject matter.\n"
                "4. Respond ONLY with 'YES' or 'NO'."
            )
        else:
            system_prompt = (
                "You are a technical research assistant. Your task is to determine if a given piece of content "
                "is relevant to a specific research topic and set of requirements.\n"
                "STRICT RULES:\n"
                "1. A document is relevant if it provides information that can be used to satisfy or inform the research requirements.\n"
                "2. If the document is purely marketing material, unrelated news, generic templates, or completely different technical context, it is NOT relevant.\n"
                "3. LinkedIn profiles or posts are NOT relevant unless explicitly requested in the research requirements.\n"
                "4. Respond ONLY with 'YES' or 'NO'."
            )
        
        if self._quota_reached:
            logger.info(f"Quota previously reached. Using heuristic relevance check for topic '{topic}'")
            return self._heuristic_relevance_check(content, topic, requirements)

        user_content = f"Topic: {topic}\nRequirements: {requirements}\n\nContent to evaluate (Snippet):\n{content[:self.max_content_chars]}"
        
        try:
            resp = self._predict_with_fallback([{"role": "user", "content": user_content}], system=system_prompt, **self.llm_params)
            is_relevant = resp.text.strip().upper().startswith("YES")
            if not is_relevant:
                logger.info(f"Content excluded as {'irrelevant for resumption' if is_resumption else 'irrelevant to topic'} '{topic}'")
            return is_relevant
        except LLMQuotaError as e:
            logger.warning(f"LLM Quota exceeded after fallback: {e}. Switching to heuristic relevance check.")
            return self._heuristic_relevance_check(content, topic, requirements)
        except Exception as e:
            logger.warning(f"Relevance check failed: {e}. Defaulting to relevant.")
            return True

    def _split_query(self, query: str, max_len: int = 800) -> List[str]:
        """
        Recursively splits a JQL or CQL query into smaller chunks if it exceeds max_len.
        Prioritises splitting at top-level OR operators.
        """
        if not query or len(query) <= max_len:
            return [query]

        def find_top_level_or(q: str) -> List[int]:
            indices = []
            depth = 0
            in_quote = False
            quote_char = None
            i = 0
            while i < len(q):
                char = q[i]
                if char == '\\':
                    i += 2
                    continue
                if (char == '"' or char == "'") and not in_quote:
                    in_quote = True
                    quote_char = char
                elif char == quote_char and in_quote:
                    in_quote = False
                elif not in_quote:
                    if char == '(':
                        depth += 1
                    elif char == ')':
                        depth -= 1
                    elif depth == 0:
                        if q[i:i+4].upper() == ' OR ':
                            indices.append(i)
                i += 1
            return indices

        # 1. Try splitting at top-level OR
        or_indices = find_top_level_or(query)
        if or_indices:
            parts = []
            last_idx = 0
            for idx in or_indices:
                parts.append(query[last_idx:idx].strip())
                last_idx = idx + 4
            parts.append(query[last_idx:].strip())
            
            split_queries = []
            current_query = ""
            for p in parts:
                test_query = p if not current_query else current_query + " OR " + p
                if len(test_query) <= max_len or not current_query:
                    current_query = test_query
                else:
                    split_queries.append(current_query)
                    current_query = p
            if current_query:
                split_queries.append(current_query)
            
            if len(split_queries) > 1:
                logger.info(f"Split long query into {len(split_queries)} chunks via top-level OR")
                # Recursively split chunks if they are still too long
                final_queries = []
                for sq in split_queries:
                    final_queries.extend(self._split_query(sq, max_len))
                return final_queries

        # 2. Try splitting a large nested OR group
        depth = 0
        in_quote = False
        quote_char = None
        or_groups = [] # (start, end)
        
        i = 0
        start_idx = -1
        while i < len(query):
            char = query[i]
            if char == '\\':
                i += 2
                continue
            if (char == '"' or char == "'") and not in_quote:
                in_quote = True
                quote_char = char
            elif char == quote_char and in_quote:
                in_quote = False
            elif not in_quote:
                if char == '(':
                    if depth == 0:
                        start_idx = i
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if depth == 0 and start_idx != -1:
                        content = query[start_idx+1:i]
                        if ' OR ' in content.upper():
                            or_groups.append((start_idx, i))
                        start_idx = -1
            i += 1
            
        if or_groups:
            # Pick the largest OR group
            or_groups.sort(key=lambda x: x[1] - x[0], reverse=True)
            start, end = or_groups[0]
            
            prefix = query[:start].strip()
            suffix = query[end+1:].strip()
            inner = query[start+1:end].strip()
            
            inner_parts_indices = find_top_level_or(inner)
            if inner_parts_indices:
                inner_parts = []
                last_idx = 0
                for idx in inner_parts_indices:
                    inner_parts.append(inner[last_idx:idx].strip())
                    last_idx = idx + 4
                inner_parts.append(inner[last_idx:].strip())
                
                split_queries = []
                current_inner = ""
                for p in inner_parts:
                    test_inner = p if not current_inner else current_inner + " OR " + p
                    test_query = ""
                    if prefix: test_query += prefix + " "
                    test_query += f"({test_inner})"
                    if suffix: test_query += " " + suffix
                    
                    if len(test_query) <= max_len or not current_inner:
                        current_inner = test_inner
                    else:
                        final_q = ""
                        if prefix: final_q += prefix + " "
                        final_q += f"({current_inner})"
                        if suffix: final_q += " " + suffix
                        split_queries.append(final_q)
                        current_inner = p
                
                if current_inner:
                    final_q = ""
                    if prefix: final_q += prefix + " "
                    final_q += f"({current_inner})"
                    if suffix: final_q += " " + suffix
                    split_queries.append(final_q)
                
                if len(split_queries) > 1:
                    logger.info(f"Split long query into {len(split_queries)} chunks via nested OR group")
                    # Recursively split chunks if they are still too long
                    final_queries = []
                    for sq in split_queries:
                        final_queries.extend(self._split_query(sq, max_len))
                    return final_queries

        return [query]

    def _evaluate_issue_richness(self, issue: Any) -> Dict[str, Any]:
        """
        Evaluates the 'richness' or value of an issue based on its metadata.
        """
        desc_len = len(getattr(issue, "description", "") or "")
        comment_count = getattr(issue, "comment_count", 0)
        commenters = getattr(issue, "commenters", [])
        updated = getattr(issue, "updated", None)
        
        score = 0
        reasons = []
        
        if desc_len > 500:
            score += 2
            reasons.append("Detailed description")
        elif desc_len > 100:
            score += 1
            reasons.append("Moderate description")
        else:
            reasons.append("Minimal information")
            
        if comment_count > 5:
            score += 2
            reasons.append("High engagement (many comments)")
        elif comment_count > 0:
            score += 1
            reasons.append("Active discussion")
            
        unique_commenters = len(set(commenters))
        if unique_commenters > 2:
            score += 1
            reasons.append("Collaborative (multiple people involved)")
            
        # Recency check
        if updated:
            if isinstance(updated, str):
                try:
                    from atlassian_utils import parse_atlassian_datetime
                    updated = parse_atlassian_datetime(updated)
                except Exception:
                    updated = None
            
            if updated:
                now = dt.datetime.now(dt.timezone.utc)
                delta = now - updated
                if delta.days < 30:
                    score += 1
                    reasons.append("Recently active")
                elif delta.days > 180:
                    reasons.append("Potentially stale")
                
        richness = "High" if score >= 5 else "Medium" if score >= 2 else "Low"
        
        return {
            "richness": richness,
            "score": score,
            "observations": reasons
        }

    def _evaluate_subject_relevance(self, subject: str, issue: Any) -> Dict[str, Any]:
        """
        Evaluates the relevance of a Jira issue to a specific subject.
        High: Subject is assignee and active commenter.
        Low: Subject is reporter only and low commenter.
        """
        s_lower = subject.lower()
        
        assignee = getattr(issue, "assignee", "").lower() if hasattr(issue, "assignee") and issue.assignee else ""
        reporter = getattr(issue, "reporter", "").lower() if hasattr(issue, "reporter") and issue.reporter else ""
        commenters = getattr(issue, "commenters", [])
        
        # Handle dict case (from cache or fallbacks)
        if isinstance(issue, dict):
            assignee = (issue.get("assignee") or "").lower()
            reporter = (issue.get("reporter") or "").lower()
            commenters = issue.get("commenters", [])

        s_comments = [c for c in commenters if c and c.lower() == s_lower]
        s_comment_count = len(s_comments)
        total_comments = len(commenters)
        
        is_assignee = (s_lower == assignee)
        is_reporter = (s_lower == reporter)
        
        score = 0
        reasons = []
        
        if is_assignee:
            score += 2
            reasons.append("Subject is Assignee")
            if s_comment_count > 0:
                score += 1
                reasons.append(f"Subject provided {s_comment_count} update(s)")
        
        if is_reporter:
            if not is_assignee:
                reasons.append("Subject is Reporter (Requestor)")
                if s_comment_count == 0:
                    score -= 1
                    reasons.append("No contributions in comments")
                elif s_comment_count < 2 and total_comments > 2:
                    score -= 1
                    reasons.append("Minimal contributions (likely just oversight or responding to questions)")
            else:
                reasons.append("Subject is both Reporter and Assignee")
                score += 1

        if not is_assignee and not is_reporter:
            if s_comment_count >= 2:
                score += 1
                reasons.append(f"Subject is active contributor ({s_comment_count} comments)")
            elif s_comment_count == 1:
                reasons.append("Subject made a single comment")
        
        relevance = "High" if score >= 2 else "Low" if score < 0 else "Medium"
        
        return {
            "relevance": relevance,
            "score": score,
            "observations": reasons
        }

    def _evaluate_confluence_relevance(self, subject: str, creator: Optional[str], contributor: Optional[str], title: str, body: str, content_type: str = "page") -> Dict[str, Any]:
        """
        Evaluates the relevance of a Confluence page to a specific subject.
        High: Subject is Creator or Contributor (Editor/Commentator).
        Medium: Subject mentioned in Title.
        Low: Subject only mentioned in Body (e.g. as meeting participant).
        """
        s_lower = subject.lower()
        creator_lower = creator.lower() if creator else ""
        contributor_lower = contributor.lower() if contributor else ""
        
        is_creator = (s_lower == creator_lower)
        is_contributor = (s_lower == contributor_lower)
        
        in_title = s_lower in title.lower()
        in_body = s_lower in body.lower()
        
        score = 0
        reasons = []
        
        if is_creator:
            score += 2
            if content_type == "comment":
                reasons.append("Subject is Comment Author")
            else:
                reasons.append("Subject is Page Creator")
        
        if is_contributor:
            score += 1
            reasons.append("Subject is Page Contributor/Editor")
            
        if not is_creator and not is_contributor:
            if in_title:
                score += 1
                reasons.append("Subject mentioned in Title")
            
            if in_body:
                reasons.append("Subject mentioned in Body (e.g. as participant)")

        # Determine relevance
        if is_creator or is_contributor:
            relevance = "High" if is_creator else "Medium"
        elif in_title:
            relevance = "Medium"
        elif in_body:
            # "If a data-subject is listed ... as a participant ... and is not the owner/commentator/editor ... then the value ... has less weight."
            relevance = "Low"
        else:
            relevance = "Low"

        return {
            "relevance": relevance,
            "score": score,
            "observations": reasons
        }

    def _fetch_issue_tree(self, issue_key: str, parent_key: Optional[str] = None, depth: int = 0, max_depth: int = 2) -> Dict[str, Any]:
        """
        Recursively fetches parent and sibling information for a given issue.
        """
        if depth >= max_depth:
            return {}

        tree = {}
        
        # 1. Fetch parent if available
        if parent_key:
            status_update(f"Walking up tree from {issue_key} to parent {parent_key}")
            try:
                parent_issues = jira_fetch_issues(self.jira_base_url, self.jira_auth, projects=None, jql=f"key = {parent_key}", limit=1)
                if parent_issues:
                    parent = parent_issues[0]
                    tree["parent"] = {
                        "key": parent.key,
                        "summary": parent.summary,
                        "status": parent.status,
                        "issuetype": parent.issuetype,
                        "description": parent.description[:500] + "..." if len(parent.description) > 500 else parent.description
                    }
                    # Recursive call for parent's parent
                    if parent.parent_key:
                        tree["parent_context"] = self._fetch_issue_tree(parent.key, parent.parent_key, depth + 1, max_depth)
                else:
                    logger.debug(f"Parent {parent_key} not found via API")
            except Exception as e:
                logger.debug(f"Failed to fetch parent {parent_key}: {e}")

        # 2. Fetch siblings if it has a parent
        if parent_key:
            try:
                # Fetch siblings (other issues with same parent)
                sibling_jql = f"parent = {parent_key} AND key != {issue_key}"
                siblings = jira_fetch_issues(self.jira_base_url, self.jira_auth, projects=None, jql=sibling_jql, limit=5)
                if siblings:
                    tree["siblings"] = [
                        {"key": s.key, "summary": s.summary, "status": s.status}
                        for s in siblings
                    ]
            except Exception as e:
                logger.debug(f"Failed to fetch siblings for {issue_key}: {e}")
                
        return tree

    def _execute_jql(self, jql: Union[str, List[str]], results_list: List[Dict[str, Any]], limit: int = 5) -> int:
        """Helper to execute JQL and update results list. Returns number of new results."""
        if not jql:
            return 0
        
        jql_list = jql if isinstance(jql, list) else [jql]
        
        # Split long queries
        final_jql_list = []
        for q in jql_list:
            final_jql_list.extend(self._split_query(q, max_len=800))
        jql_list = final_jql_list
        
        total_new = 0
        
        for query in jql_list:
            if not query: continue
            status_update(f"Executing Jira query: {query}")
            existing_keys = {it.get("key") for it in results_list if it.get("key")}
            try:
                issues = jira_fetch_issues(self.jira_base_url, self.jira_auth, projects=None, jql=query, limit=limit)
                count = 0
                for i in issues:
                    if i.key not in existing_keys:
                        self._record_jira_contribution(i.key, i.summary)
                        
                        richness = self._evaluate_issue_richness(i)
                        self._track_issue_evidence(i, richness=richness)
                        tree = self._fetch_issue_tree(i.key, i.parent_key)
                        
                        # Extract linked Confluence IDs
                        linked_cids = _extract_confluence_ids(i.description, self.jira_base_url)
                        for comm in getattr(i, "comments", []):
                            linked_cids.extend(_extract_confluence_ids(comm.get("body", ""), self.jira_base_url))
                        linked_cids = sorted(list(set(linked_cids)))
                        
                        results_list.append({
                            "key": i.key, 
                            "summary": i.summary, 
                            "description": i.description, 
                            "status": i.status,
                            "issuetype": i.issuetype,
                            "assignee": i.assignee,
                            "reporter": i.reporter,
                            "comment_count": i.comment_count,
                            "commenters": i.commenters,
                            "created": i.created.isoformat() if i.created else None,
                            "updated": i.updated.isoformat() if i.updated else None,
                            "richness_evaluation": richness,
                            "context_tree": tree,
                            "linked_confluence_ids": linked_cids
                        })
                        
                        # Update name cache
                        if i.assignee: self._update_name_cache(i.assignee)
                        if i.reporter: self._update_name_cache(i.reporter)
                        if i.commenters: self._update_name_cache(i.commenters)
                        existing_keys.add(i.key)
                        count += 1
                total_new += count
            except Exception as e:
                logger.error(f"Jira fetch failed: {e}")
                
                # Check for 400 (Bad Request) or 403 (Forbidden) - try to fix with LLM
                error_str = str(e)
                if ("400" in error_str or "403" in error_str) and not self._quota_reached:
                    fixed_jql = self._fix_query_with_llm(query, error_str, is_cql=False)
                    if fixed_jql and fixed_jql != query:
                        status_update(f"Retrying fixed Jira query: {fixed_jql}")
                        try:
                            issues = jira_fetch_issues(self.jira_base_url, self.jira_auth, projects=None, jql=fixed_jql, limit=limit)
                            count = 0
                            for i in issues:
                                if i.key not in existing_keys:
                                    self._record_jira_contribution(i.key, i.summary)
                                    
                                    richness = self._evaluate_issue_richness(i)
                                    self._track_issue_evidence(i, richness=richness)
                                    tree = self._fetch_issue_tree(i.key, i.parent_key)
                                    
                                    # Extract linked Confluence IDs
                                    linked_cids = _extract_confluence_ids(i.description, self.jira_base_url)
                                    for comm in getattr(i, "comments", []):
                                        linked_cids.extend(_extract_confluence_ids(comm.get("body", ""), self.jira_base_url))
                                    linked_cids = sorted(list(set(linked_cids)))
                                    
                                    results_list.append({
                                        "key": i.key, 
                                        "summary": i.summary, 
                                        "description": i.description, 
                                        "status": i.status,
                                        "issuetype": i.issuetype,
                                        "assignee": i.assignee,
                                        "reporter": i.reporter,
                                        "comment_count": i.comment_count,
                                        "commenters": i.commenters,
                                        "created": i.created.isoformat() if i.created else None,
                                        "updated": i.updated.isoformat() if i.updated else None,
                                        "richness_evaluation": richness,
                                        "context_tree": tree,
                                        "linked_confluence_ids": linked_cids
                                    })
                                    
                                    # Update name cache
                                    if i.assignee: self._update_name_cache(i.assignee)
                                    if i.reporter: self._update_name_cache(i.reporter)
                                    if i.commenters: self._update_name_cache(i.commenters)
                                    existing_keys.add(i.key)
                                    count += 1
                            total_new += count
                            continue # Successfully handled
                        except Exception as e2:
                            logger.error(f"Fixed Jira query also failed: {e2}")
                
                # Fallback: analyze the failure (likely URL-based in the backend)
                failed_url = f"{self.jira_base_url}/rest/api/3/search?jql={query}"
                fallback_data = self._fallback_url_research(failed_url, str(e))
                if fallback_data:
                    results_list.extend(fallback_data)
                    total_new += len(fallback_data)
                else:
                    results_list.append({"error": str(e)})
        
        return total_new

    def _execute_cql(self, cql: Union[str, List[str]], results_list: List[Dict[str, Any]], limit: int = 3) -> int:
        """Helper to execute CQL and update results list. Returns number of new results."""
        if not cql:
            return 0
        
        cql_list = cql if isinstance(cql, list) else [cql]
        
        # Split long queries (CQL is more sensitive to length because of GET)
        final_cql_list = []
        for q in cql_list:
            final_cql_list.extend(self._split_query(q, max_len=800))
        cql_list = final_cql_list
        
        total_new = 0
        
        for query in cql_list:
            if not query: continue
            status_update(f"Executing Confluence query: {query}")
            existing_ids = {it.get("id") for it in results_list if it.get("id")}
            try:
                data = _conf_get(self.jira_base_url, self.jira_auth, "/rest/api/search", params={
                    "cql": query,
                    "limit": limit,
                    "expand": "content.body.storage,content.history,content.version"
                })
                count = 0
                for r in data.get("results", []):
                    content = r.get("content", {})
                    cid = content.get("id")
                    if cid not in existing_ids:
                        body = content.get("body", {}).get("storage", {}).get("value", "")
                        
                        # Extract person names
                        history = content.get("history", {})
                        creator = history.get("createdBy", {}).get("displayName")
                        if creator: self._update_name_cache(creator)
                        
                        version = content.get("version", {})
                        contributor = version.get("by", {}).get("displayName")
                        if contributor: self._update_name_cache(contributor)

                        # Try to get a web URL if possible
                        web_url = None
                        links = content.get("_links", {})
                        if "webui" in links:
                            web_url = self.jira_base_url.rstrip("/") + links["webui"]
                        elif "self" in links:
                            web_url = f"{self.jira_base_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={cid}"
                        self._record_confluence_contribution(cid, content.get("title"), url=web_url)
                        self._track_page_evidence(cid, content.get("title"), creator, contributor, body, content.get("type", "page"))
                        
                        # Extract linked Jira keys
                        linked_jira = re.findall(r'\b([A-Z][A-Z0-9]+-[0-9]+)\b', body)
                        linked_jira = sorted(list(set(linked_jira)))

                        results_list.append({
                            "id": cid,
                            "title": content.get("title"),
                            "snippet": body[:self.max_content_chars // 2],
                            "linked_jira_keys": linked_jira
                        })
                        existing_ids.add(cid)
                        count += 1
                total_new += count
            except Exception as e:
                logger.error(f"Confluence search failed: {e}")
                
                # Check for 400 (Bad Request) or 403 (Forbidden) - try to fix with LLM
                error_str = str(e)
                if ("400" in error_str or "403" in error_str) and not self._quota_reached:
                    fixed_cql = self._fix_query_with_llm(query, error_str, is_cql=True)
                    if fixed_cql and fixed_cql != query:
                        status_update(f"Retrying fixed Confluence query: {fixed_cql}")
                        try:
                            data = _conf_get(self.jira_base_url, self.jira_auth, "/rest/api/search", params={
                                "cql": fixed_cql,
                                "limit": limit,
                                "expand": "content.body.storage,content.history,content.version"
                            })
                            count = 0
                            for r in data.get("results", []):
                                content = r.get("content", {})
                                cid = content.get("id")
                                if cid not in existing_ids:
                                    body = content.get("body", {}).get("storage", {}).get("value", "")
                                    
                                    # Extract person names
                                    history = content.get("history", {})
                                    creator = history.get("createdBy", {}).get("displayName")
                                    if creator: self._update_name_cache(creator)
                                    
                                    version = content.get("version", {})
                                    contributor = version.get("by", {}).get("displayName")
                                    if contributor: self._update_name_cache(contributor)

                                    web_url = None
                                    links = content.get("_links", {})
                                    if "webui" in links:
                                        web_url = self.jira_base_url.rstrip("/") + links["webui"]
                                    elif "self" in links:
                                        web_url = f"{self.jira_base_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={cid}"
                                    self._record_confluence_contribution(cid, content.get("title"), url=web_url)
                                    self._track_page_evidence(cid, content.get("title"), creator, contributor, body, content.get("type", "page"))
                                    
                                    # Extract linked Jira keys
                                    linked_jira = re.findall(r'\b([A-Z][A-Z0-9]+-[0-9]+)\b', body)
                                    linked_jira = sorted(list(set(linked_jira)))

                                    results_list.append({
                                        "id": cid,
                                        "title": content.get("title"),
                                        "snippet": body[:self.max_content_chars // 2],
                                        "linked_jira_keys": linked_jira
                                    })
                                    existing_ids.add(cid)
                                    count += 1
                            total_new += count
                            continue # Successfully handled
                        except Exception as e2:
                            logger.error(f"Fixed Confluence query also failed: {e2}")
                
                # Fallback: analyze the failure
                failed_url = f"{self.jira_base_url}/wiki/rest/api/search?cql={query}"
                fallback_data = self._fallback_url_research(failed_url, str(e))
                if fallback_data:
                    results_list.extend(fallback_data)
                    total_new += len(fallback_data)
                else:
                    results_list.append({"error": str(e)})
        
        return total_new

    def _execute_queries(self, queries: Dict[str, Any], topic: Optional[str] = None, requirements: Optional[str] = None) -> Dict[str, Any]:
        """
        Executes research queries against Jira, Confluence, Search, and LLM.
        """
        queries = self._sanitize_queries(queries)
        results = {
            "jira_issues": [],
            "confluence_pages": [],
            "search_results": [],
            "llm_insights": []
        }

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = []

            # 1. Jira Research
            jql = queries.get("jql")
            if jql and not self.disable_jira:
                futures.append(executor.submit(self._execute_jql, jql, results["jira_issues"], limit=5))

            # 2. Confluence Research
            cql = queries.get("cql")
            if cql and not self.disable_confluence:
                futures.append(executor.submit(self._execute_cql, cql, results["confluence_pages"], limit=3))

            # 3. Web Search Research
            def _proc_search(sq):
                try:
                    # If the search query looks like a direct URL, fetch it instead of searching
                    if sq.startswith(("http://", "https://")):
                        status_update(f"Detected direct URL in search queries, fetching: {sq}")
                        try:
                            content = self._read_source(sq)
                            if topic and requirements and content:
                                if not self._is_content_relevant(content, topic, requirements):
                                    return None
                            title = f"Direct fetch: {sq}"
                            self._record_web_contribution(sq, title)
                            return {
                                "title": title,
                                "url": sq,
                                "snippet": content[:500] + "...",
                                "full_content": content[:self.max_content_chars]
                            }
                        except Exception as e:
                            logger.warning(f"Failed to fetch direct URL {sq}: {e}")
                        return None

                    search_results = []
                    for engine in self.search_engines:
                        try:
                            results = engine.search(sq)
                            if results:
                                search_results.extend(results)
                        except Exception as e:
                            logger.warning(f"Search engine {engine} failed for query '{sq}': {e}")

                    # Deduplicate search results by URL
                    seen_urls = set()
                    unique_results = []
                    for res in search_results:
                        url = res.get("url")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            unique_results.append(res)
                    search_results = unique_results

                    local_results = []
                    for res in search_results:
                        url = res.get("url", "")
                        snippet = res.get("snippet", "")
                        if "linkedin.com" in url.lower():
                            if not requirements or "linkedin" not in requirements.lower():
                                continue
                        if topic and requirements and snippet:
                            if not self._is_content_relevant(snippet, topic, requirements):
                                continue
                        if url and url.startswith("http") and "simulated-search.com" not in url and url != "#":
                            status_update(f"Fetching further content from: {url}")
                            try:
                                content = self._read_source(url)
                                if topic and requirements and content:
                                    if not self._is_content_relevant(content, topic, requirements):
                                        continue
                                res["full_content"] = content[:self.max_content_chars]
                                self._record_web_contribution(url, res.get("title", url))
                            except Exception as e:
                                logger.warning(f"Failed to fetch further content from {url}: {e}")
                        
                        if "full_content" not in res and url and url != "#" and "simulated-search.com" not in url:
                            self._record_web_contribution(url, res.get("title", url))
                        local_results.append(res)
                    return local_results
                except Exception as e:
                    logger.error(f"Search query execution failed for '{sq}': {e}")
                    return None

            for sq in queries.get("search_queries", []):
                futures.append(executor.submit(_proc_search, sq))

            # 4. LLM Research
            def _proc_llm(lq):
                if self._quota_reached and not self.fallback_llm:
                    return None
                try:
                    status_update(f"Asking LLM question: {lq}")
                    resp = self._predict_with_fallback([{"role": "user", "content": lq}], **self.llm_params)
                    return {"question": lq, "answer": resp.text}
                except Exception as e:
                    logger.error(f"LLM insight query failed: {e}")
                    return None

            for lq in queries.get("llm_questions", []):
                futures.append(executor.submit(_proc_llm, lq))

            # Wait for all main research tasks
            for f in as_completed(futures):
                res = f.result()
                if isinstance(res, list):
                    results["search_results"].extend(res)
                elif isinstance(res, dict):
                    if "answer" in res:
                        results["llm_insights"].append(res)
                    elif "url" in res:
                        results["search_results"].append(res)

        # 5. Follow-up Research grounded in LLM insights (sequential as it depends on results)
        if results["llm_insights"] and not self._quota_reached:
            followup_queries = self._generate_followup_queries(results["llm_insights"])
            
            # Initial pass for follow-up
            fjql = followup_queries.get("jql")
            fcql = followup_queries.get("cql")
            
            jira_count = self._execute_jql(fjql, results["jira_issues"], limit=3) if fjql else 0
            conf_count = self._execute_cql(fcql, results["confluence_pages"], limit=2) if fcql else 0
            
            # Retry logic if insufficient results
            # "Insufficient" means we HAD queries for Jira/Conf but got ZERO results.
            has_followup_jql = bool(fjql)
            has_followup_cql = bool(fcql)
            
            if (has_followup_jql or has_followup_cql) and jira_count == 0 and conf_count == 0:
                adjusted = self._adjust_queries(followup_queries, "Original follow-up queries produced zero results.")
                if adjusted:
                    status_update("Retrying adjusted follow-up queries...")
                    self._execute_jql(adjusted.get("jql"), results["jira_issues"], limit=3)
                    self._execute_cql(adjusted.get("cql"), results["confluence_pages"], limit=2)

        # 6. Follow Links for cross-context (Jira -> Confluence, Confluence -> Jira)
        for _ in range(2): # Up to 2 levels deep
            if self._follow_links(results) == 0:
                break

        return results

    def _follow_links(self, results: Dict[str, Any]) -> int:
        """
        Iteratively follows links between Jira issues and Confluence pages.
        Returns the total number of new items fetched.
        """
        if self.disable_jira and self.disable_confluence:
            return 0
            
        status_update("Following links between Jira and Confluence for added context...")
        total_new = 0
        
        # 1. Discover linked items from current results
        linked_cids = set()
        for issue in results.get("jira_issues", []):
            if "linked_confluence_ids" in issue:
                linked_cids.update(issue["linked_confluence_ids"])
                
        linked_jira_keys = set()
        for page in results.get("confluence_pages", []):
            if "linked_jira_keys" in page:
                linked_jira_keys.update(page["linked_jira_keys"])
                
        # 2. Filter out already fetched items
        existing_keys = {it.get("key") for it in results.get("jira_issues", []) if it.get("key")}
        existing_ids = {str(it.get("id")) for it in results.get("confluence_pages", []) if it.get("id")}
        
        to_fetch_cids = [cid for cid in linked_cids if str(cid) not in existing_ids]
        to_fetch_jira = [key for key in linked_jira_keys if key not in existing_keys]
        
        if not to_fetch_cids and not to_fetch_jira:
            logger.info("No new linked items discovered to follow.")
            return 0
            
        # 3. Fetch missing items (limit to avoid explosion)
        if to_fetch_cids and not self.disable_confluence:
            # Construct a CQL to fetch multiple by ID
            cid_list = ", ".join([f'"{cid}"' for cid in to_fetch_cids[:5]])
            cql = f"id in ({cid_list})"
            fetched = self._execute_cql(cql, results["confluence_pages"], limit=5)
            total_new += fetched
            status_update(f"Fetched {fetched} linked Confluence pages.")
            
        if to_fetch_jira and not self.disable_jira:
            # Construct a JQL: key in (KEY-1, KEY-2)
            key_list = ", ".join([f'"{key}"' for key in to_fetch_jira[:5]])
            jql = f"key in ({key_list})"
            fetched = self._execute_jql(jql, results["jira_issues"], limit=5)
            total_new += fetched
            status_update(f"Fetched {fetched} linked Jira issues.")
            
        return total_new

    def _fallback_url_research(self, failed_url: str, error_msg: str) -> List[Dict[str, Any]]:
        """
        Examines a failed URL and attempts to find relevant information through alternative means.
        """
        logger.info(f"Attempting fallback research for failed URL: {failed_url}")
        
        system_prompt = (
            "You are a technical research assistant. A URL fetch or search query has failed. "
            "Examine the URL and the error message to understand what was being sought. "
            "Then, provide alternative search terms or strategies to find that information.\n"
            "If the URL was for Jira or Confluence, suggest KEYWORDS for a simple text search "
            "instead of complex JQL/CQL.\n"
            "MANDATORY: Return your response ONLY as a JSON object with these keys: "
            "'search_terms' (string for web search), 'jira_keywords' (string for simple Jira search), "
            "'confluence_keywords' (string for simple Confluence search), 'reasoning' (briefly explain why)."
        )
        
        user_content = f"Failed URL: {failed_url}\nError Message: {error_msg}"
        
        if self._quota_reached and not self.fallback_llm:
            return []
            
        try:
            resp = self._predict_with_fallback(
                [{"role": "user", "content": user_content}],
                system=system_prompt,
                **self.llm_params
            )
            
            if not resp.text or not resp.text.strip():
                logger.warning("LLM returned an empty response for fallback research.")
                return []

            fallback_plan = {}
            match = re.search(r"(\{.*\})", resp.text, re.DOTALL)
            if match:
                fallback_plan = json.loads(match.group(1))
            else:
                fallback_plan = json.loads(resp.text)
                
            fallback_results = []
            
            # 1. Try web search with suggested terms
            search_terms = fallback_plan.get("search_terms")
            if search_terms:
                results = self.search_engine.search(search_terms)
                fallback_results.extend([{"source": "web_fallback", "content": r} for r in results])
                
            # 2. Try simple Jira search
            jira_keywords = fallback_plan.get("jira_keywords")
            if jira_keywords:
                try:
                    # Use a simpler JQL based on keywords
                    jql = f"text ~ \"{jira_keywords}\""
                    issues = jira_fetch_issues(self.jira_base_url, self.jira_auth, projects=None, jql=jql, limit=3)
                    for i in issues:
                        self._record_jira_contribution(i.key, i.summary)
                    fallback_results.extend([
                        {"source": "jira_fallback", "key": i.key, "summary": i.summary, "description": i.description}
                        for i in issues
                    ])
                except Exception:
                    pass

            # 3. Try simple Confluence search
            conf_keywords = fallback_plan.get("confluence_keywords")
            if conf_keywords:
                try:
                    cql = f"text ~ \"{conf_keywords}\""
                    data = _conf_get(self.jira_base_url, self.jira_auth, "/rest/api/search", params={
                        "cql": cql, 
                        "limit": 2, 
                        "expand": "content.body.storage"
                    })
                    for r in data.get("results", []):
                        content = r.get("content", {})
                        web_url = None
                        links = content.get("_links", {})
                        if "webui" in links:
                            web_url = self.jira_base_url.rstrip("/") + links["webui"]
                        elif "self" in links:
                            web_url = f"{self.jira_base_url.rstrip('/')}/wiki/pages/viewpage.action?pageId={content.get('id')}"

                        self._record_confluence_contribution(content.get("id"), content.get("title"), url=web_url)
                        fallback_results.append({
                            "source": "confluence_fallback",
                            "title": content.get("title"),
                            "snippet": content.get("body", {}).get("storage", {}).get("value", "")[:self.max_content_chars // 4]
                        })
                except Exception:
                    pass
                    
            return fallback_results
            
        except LLMQuotaError as e:
            self._quota_reached = True
            logger.error(f"Fallback research failed due to quota: {e}")
            return []
        except Exception as e:
            logger.error(f"Fallback URL research failed: {e}. Response was: {getattr(resp, 'text', '')[:200]}...")
            return []

    def _formulate_document(self, topic: str, requirements: str, results: Dict[str, Any], current_draft: str = "", context: str = "", target_section: Optional[str] = None) -> str:
        """
        Integrates research results and context into a structured Markdown document using professional British English.
        """
        system_prompt = (
            "You are a conservative, reserved British professional technical writer. Your task is to formulate or "
            "update a comprehensive document based on a topic, requirements, additional context, and new research data.\n"
            "STRICT RULES:\n"
            "1. Use professional British English throughout (e.g., 'organisation', 'programme', 'summarise').\n"
            "2. Maintain a formal, reserved, and authoritative tone. Avoid any sycophantic, overly laudatory, or emotional language.\n"
            "3. Provide only factually-based answers grounded strictly in the provided research data. Do not speculate, embellish, or add conversational fluff.\n"
            "4. Format clearly using Markdown. Use standard headers (# for Title, ## for Sections, ### for Subsections).\n"
            "5. PRIORITISE business-specific details from Jira and Confluence over general knowledge or simulated search results. "
            "Integrate these findings seamlessly to improve depth and clarity.\n"
            "6. AVOID generic placeholders (e.g., 'Domain A', 'System X', 'Project Y') if specific names or details are available in the research data. "
            "If no specific data is found, be explicit about what is being sought instead of using fake names.\n"
            "7. PERSON NAMES: Only use individual person names that appear in the research data or identified names list. "
            "Do NOT invent people or attribute actions to names that have not been identified.\n"
            "8. EQUAL ATTENTION: If multiple individuals or entities (subjects) are listed in the requirements, ensure that "
            "the document gives equal attention to each of them. No one subject should be given significantly more or less space or detail than the others.\n"
            "9. BALANCED INVOLVEMENT ATTRIBUTION: Use the provided metadata (assignee, reporter, commenters) to accurately attribute "
            "involvement in Jira issues. A ticket has HIGH value as a reference if the subject is the ASSIGNEE and provides regular/relevant "
            "updates in the comments. Conversely, if the subject is only the REPORTER (Requestor) and does not otherwise contribute "
            "to the success of the ticket (other than to respond to the assignee's questions), then while the subject may have oversight, "
            "they are NOT a key contributor and the ticket has LOW relevance for their personal appraisal. Do NOT overstate contributions "
            "for such low-relevance oversight roles.\n"
            "10. CONFLUENCE RELEVANCE: Apply similar attribution rules to Confluence. A page has HIGH value if the subject is the AUTHOR (Owner) "
            "or an active CONTRIBUTOR (Editor/Commentator). If the subject is merely mentioned in the page body (e.g., listed as a participant "
            "in a meeting) but has not otherwise authored content on that page, the page has LOW relevance as evidence of their direct technical "
            "contribution. Do NOT overstate achievements based on meeting attendance alone.\n"
            "11. CONSOLIDATION: For reports involving multiple subjects, consolidate ALL information relevant to a specific subject "
            "into its own dedicated section for that subject. Avoid jumping between subjects or scattering information about a single "
            "subject across multiple disparate sections. This consolidation is crucial for identifying and removing any duplication or repetition.\n"
            "12. Ensure the document structure is logical and follows professional standards.\n"
            "13. DO NOT include any preamble, introduction like 'Here is the document', or meta-talk. "
            "Start immediately with the document content.\n"
            "14. STRICT GROUNDING: Every claim of fact, contribution, or specific project involvement MUST be directly supported by the provided Research Data. "
            "Do NOT invent activities, sessions, or documentation that do not appear in the research data. "
            "If citing a Confluence page or Jira ticket, use its real title or summary from the Research Data.\n"
            "15. ISSUE RELEVANCE AND RICHNESS: The research data includes a 'richness_evaluation' and a subject-specific 'relevance_evaluation'. "
            "Prioritise information from 'High' richness and 'High' relevance issues. Be explicit if an issue has 'Low' relevance for the subject, "
            "noting it as an oversight or coordination role rather than a direct technical achievement.\n"
            "16. CONTEXT TREE: Use the 'context_tree' (parent and sibling issues) to gain a better background of the tickets and how they fit into the wider project/epic. "
            "This context is crucial for understanding the 'why' behind a ticket even if the ticket itself is brief.\n"
            "17. PRONOUN USAGE: For individuals with multi-gender names (e.g., Sandy, Alex, Sam, etc.), if specific pronouns are "
            "not explicitly mentioned in the research data (Jira, Confluence, requirements), you MUST use gender-neutral "
            "pronouns (they/them/their) to refer to them.\n"
            "18. LOOKUP ERRORS: If the research data contains errors (e.g., '400 Client Error', 'lookup failed'), do NOT imply that no information exists. "
            "Instead, explicitly state that certain information could not be verified due to a technical lookup error during the research process.\n"
            "19. LINKED INFORMATION: The research data includes 'linked_confluence_ids' in Jira issues and 'linked_jira_keys' in Confluence pages. "
            "These linked items have been fetched and included in the research data to provide additional context. "
            "Use this cross-referenced information to gain a more complete understanding of the activities and contributions.\n"
            "20. ACCURATE NAME ATTRIBUTION: When generating the report, meticulously verify that every task, achievement, "
            "or contribution is correctly attributed to the specific individual who performed the work. "
            "Double-check to ensure there is no accidental substitution of one person's name for another, "
            "especially when processing lists of staff or collaborators.\n"
            "21. FORMATTING: Ensure the document has clean formatting. Avoid an excess of blank lines (more than two in a row) "
            "and do NOT use excessive horizontal rules or separators (e.g., '---' or '-------'). "
            "Standard Markdown horizontal rules should be used sparingly and never repeated in succession.\n"
            "22. EMPTY ENTRIES: Do NOT include any Jira tickets or Confluence pages in the summary tables or the report content if they do not contain meaningful information (e.g., if they have no description and no comments). Blank or 'placeholder' rows in tables are strictly prohibited. If no meaningful data exists for a subject, state this clearly in text instead of providing an empty table.\n"
        )
        
        user_content = (
            f"Topic: {topic}\n"
            f"Requirements: {requirements}\n"
        )
        evidence_summary = self._get_evidence_summary()
        if evidence_summary:
            user_content += f"\n{evidence_summary}\n"
            
        if context:
            user_content += f"Additional Context:\n{context}\n"
        
        user_content += f"Research Data: {json.dumps(results, indent=2)}\n"
        
        is_piecemeal = False
        if target_section:
            user_content += f"\nTARGET SECTION TO EXPAND: {target_section}\n"
            if current_draft:
                efficient_draft = self._get_token_efficient_context(current_draft, target_section=target_section)
                user_content += f"\nCurrent Draft (Outline/Partial):\n{efficient_draft}\n"
            
            user_content += f"The current document has content for '{target_section}'. " \
                            f"Use the research data to ENHANCE and EXPAND this section. " \
                            f"If the section is currently an outline, flesh it out with detail. " \
                            f"If it already has detail, further improve it with the new research data. " \
                            f"MANDATORY: Use the exact header '## {target_section}' (or appropriate level) for this section. " \
                            f"Return ONLY the updated and comprehensive section content (including its header)."
            is_piecemeal = True # Treat as piecemeal to avoid rewriting everything
        elif current_draft:
            token_count = self.llm.estimate_tokens(current_draft)
            if token_count >= self.token_threshold:
                is_piecemeal = True
                efficient_draft = self._get_token_efficient_context(current_draft)
                user_content += f"\nCurrent Draft (Outline/Partial):\n{efficient_draft}\n"
                user_content += "\nIMPORTANT: The document is already substantial. " \
                                "Instead of rewriting it, provide ONLY NEW sections or major expansions " \
                                "to be APPENDED to the existing document. Return ONLY the new content."
            else:
                user_content += f"\nCurrent Draft to expand/improve:\n{current_draft}\n"
                thin_sections = self._identify_thin_sections(current_draft)
                if thin_sections:
                    user_content += f"\nNote: The following sections are just outlines: {', '.join(thin_sections)}. " \
                                    f"Prioritise providing detailed content for these."
        
        logger.info("Formulating document via LLM")
        try:
            resp = self._predict_with_fallback(
                [{"role": "user", "content": user_content}],
                system=system_prompt,
                **self.llm_params
            )
        except (LLMQuotaError, LLMError) as e:
            logger.error(f"Formulating document failed: {e}")
            return current_draft
        if not resp.text or not resp.text.strip():
            logger.warning("LLM returned an empty document draft. Retaining previous draft if available.")
            return current_draft
            
        if target_section:
            logger.info(f"Targeted expansion: Updating/enhancing section '{target_section}'.")
            return self._replace_section_content(current_draft, target_section, self._strip_preamble(resp.text.strip()))

        if is_piecemeal:
            logger.info("Piecemeal update: Appending new content to draft.")
            return current_draft + "\n\n" + self._strip_preamble(resp.text.strip())
            
        return self._strip_preamble(resp.text.strip())

    def _is_complete(self, topic: str, requirements: str, draft: str) -> Tuple[bool, str]:
        """
        Determines if the document meets all requirements and is sufficiently comprehensive.
        Returns (is_complete, feedback).
        """
        system_prompt = (
            "Evaluate if the provided document is comprehensive, complete, and adheres to a "
            "conservative, reserved professional tone relative to the specified topic and requirements. "
            "Focus on the depth of detail, factual grounding, and professional standards.\n"
            "STRICT COMPLETENESS AND STYLE CRITERIA:\n"
            "1. NO generic placeholders: 'Domain A', 'System X', etc. MUST be replaced with business context or noted as missing.\n"
            "2. DEPTH: Sections must be detailed and avoid being simple bulleted lists without explanation.\n"
            "3. BUSINESS FOCUS: The document must feel specific to the company and topic, using data from Jira/Confluence.\n"
            "4. TONE: The document MUST maintain a conservative, reserved, and formal professional tone. "
            "Flag any sycophantic, emotional, or speculative language as a failure.\n"
            "5. FACTUAL GROUNDING: All information must be strictly based on the provided research data.\n"
            "6. EQUAL ATTENTION: If multiple individuals or entities (subjects) are listed in the requirements, ensure they are ALL "
            "represented with similar depth and detail. If any subject is missing or has significantly less information, mark it as incomplete.\n"
            "7. CONSOLIDATION: For reports involving multiple subjects, check that all information for each subject is consolidated "
            "into its own dedicated section. Information should not be scattered across different parts of the document.\n"
            "8. CONFLUENCE RELEVANCE: Check that the document does not overstate contributions for subjects who are only listed as participants "
            "on Confluence pages. Such pages should be treated as low-relevance references.\n"
            "9. CREDIBILITY: Check if the depth of the subject's section is proportional to their recorded evidence density. "
            "If a subject has very thin evidence but a very long and detailed section of achievements, it may be incomplete because it needs grounding or correction.\n"
            "10. ACCURATE NAME ATTRIBUTION: Verify that every task or achievement is correctly attributed to the right person. "
            "Ensure no accidental name substitution has occurred between different individuals.\n"
            "Respond ONLY with 'YES' if it is complete and stylistically correct, or 'NO' followed by a bulleted list of missing areas, "
            "tonal issues, or sections that need more depth or business context."
        )
        efficient_draft = self._get_token_efficient_context(draft)
        user_content = f"Topic: {topic}\nRequirements: {requirements}\n"
        evidence_summary = self._get_evidence_summary()
        if evidence_summary:
            user_content += f"\n{evidence_summary}\n"
        user_content += f"\nDocument Draft (Outline/Partial):\n{efficient_draft}"
        
        try:
            resp = self._predict_with_fallback(
                [{"role": "user", "content": user_content}],
                system=system_prompt,
                **self.llm_params
            )
        except (LLMQuotaError, LLMError) as e:
            logger.error(f"Completeness check failed: {e}")
            return False, f"Check failed due to LLM error: {str(e)}. Assuming incomplete for safety."
        except Exception as e:
            logger.error(f"Completeness check failed (unexpected): {e}")
            return False, f"Check failed: {str(e)}"
        
        content = resp.text.strip().upper()
        is_complete = content.startswith("YES")
        feedback = resp.text.strip()
        if not is_complete:
            logger.info(f"Document incomplete. LLM Feedback: {feedback}")
        return is_complete, feedback

    def _agentic_debate_and_refine(self, topic: str, requirements: str, draft: str, context: str = "", draft_context: str = "", target_section: Optional[str] = None) -> str:
        """
        Uses multiple agentic roles to debate and refine the document.
        """
        logger.info("Starting agentic debate and refinement")
        
        # Role 1: The Critical Reviewer
        critic_system = (
            "You are a conservative, reserved British Critical Reviewer. Your role is to find weaknesses, gaps, "
            "irrelevant content, or tonal inconsistencies in the provided document based on the topic, requirements, and context. "
            "Focus on depth, accuracy, business relevance, and strict adherence to a professional, non-sycophantic tone. "
            "Provide a concise set of 'points of improvement'.\n"
            "MANDATORY: Flag any sycophantic, overly laudatory, emotional, or speculative language as a weakness.\n"
            "MANDATORY: Flag any claim that is not strictly grounded in the provided research data.\n"
            "MANDATORY: Flag any generic placeholders like 'PROJECT_KEY_HERE', 'YOUR_PROJECT_KEY', 'Domain A', 'System X', or 'Project Y' as weaknesses "
            "that must be replaced with real business context from Jira/Confluence data.\n"
            "MANDATORY: If the requirements list multiple subjects (individuals or entities), ensure they are all given equal "
            "attention and detail. Flag any imbalance as a weakness.\n"
            "MANDATORY: Ensure involvement in Jira issues is accurately attributed based on the provided metadata (assignee, reporter, commenters). "
            "A ticket has HIGH value as a reference if the subject is the ASSIGNEE and provides regular/relevant updates in the comments. "
            "If the subject is only the REPORTER (Requestor) and does not otherwise contribute significantly (other than responding to questions), "
            "then they are NOT a key contributor and the ticket has LOW relevance for them. Flag any overstatement of contribution for oversight roles.\n"
            "MANDATORY: Flag any overstatement of contribution for Confluence pages where the subject is only a participant mentioned in the body "
            "and not the author or editor.\n"
            "MANDATORY: Flag any scattered information as a weakness.\n"
            "MANDATORY: Flag any claim or reference (e.g. specific training sessions, technical deep-dives, or named documentation) that is NOT explicitly supported "
            "by the provided Research Data or Context. Inventing facts is a critical failure.\n"
            "MANDATORY: Check for achievement 'overselling' based on evidence density. If a subject has only one source (Jira/Confluence) "
            "and the document portrays them as a major driver of many initiatives, flag this as an exaggeration. Achievements must be proportional to supporting evidence.\n"
            "MANDATORY: Ensure the document respects 'richness_evaluation'. If a section assumes too much from 'Low' richness tickets without stating the limitation, flag it. "
            "If a section ignores relevant background from the 'context_tree', flag it as a missed opportunity for depth.\n"
            "MANDATORY: Ensure the document uses gender-neutral pronouns (they/them/their) for individuals with multi-gender names (e.g., Sandy, Alex, Sam) "
            "unless specific pronouns are explicitly mentioned in the Research Data.\n"
            "MANDATORY: Ensure that lookup errors (e.g., '400 Client Error') are handled by stating that information could not be verified, "
            "rather than implying it doesn't exist.\n"
            "MANDATORY: Verify that linked information (from 'linked_confluence_ids' or 'linked_jira_keys') is used to provide added context where appropriate.\n"
            "MANDATORY: Verify that every achievement or task mentioned is correctly attributed to the person who did it. "
            "Flag any instance where a name might have been accidentally substituted for another person's name.\n"
            "MANDATORY: Flag any formatting issues, such as an excess of blank lines or excessive use of horizontal separators (e.g., '---').\n"
            "MANDATORY: Flag any 'blank rows' or 'placeholder rows' in tables. Ensure all table rows contain meaningful data and are not just listing empty tickets or pages."
        )
        if target_section:
            critic_system += f"\nNote: You are reviewing the expansion of the '{target_section}' section."

        if draft_context:
            critic_system += f"\nNote: The document is substantial. You are reviewing ONLY the LATEST ADDITION to it. " \
                             f"The existing document outline is as follows for context:\n{draft_context}"

        critic_prompt = f"Topic: {topic}\nRequirements: {requirements}\n"
        evidence_summary = self._get_evidence_summary()
        if evidence_summary:
            critic_prompt += f"\n{evidence_summary}\n"
        critic_prompt += f"Context: {context}\n\nDocument (Latest Addition):\n{draft}"
        try:
            critic_resp = self._predict_with_fallback([{"role": "user", "content": critic_prompt}], system=critic_system, **self.llm_params)
        except (LLMQuotaError, LLMError) as e:
            logger.error(f"Critic review failed: {e}")
            return draft
        
        improvement_points = critic_resp.text
        if not improvement_points or not improvement_points.strip():
            logger.info("Critic found no points of improvement. Returning original draft.")
            return draft
        logger.info(f"Critic's feedback: {improvement_points[:200]}...")

        # Role 2: The Professional Editor
        editor_system = (
            "You are a conservative, reserved British Professional Editor. Your role is to take a document and a list of "
            "improvement points, and refine the document to address those points while maintaining "
            "impeccable professional British English and a formal, reserved tone. Ensure the document is polished, "
            "factually grounded, and strictly non-sycophantic.\n"
            "STRICT RULES:\n"
            "1. DO NOT include any preamble, introduction (e.g., 'As a professional British editor...'), or meta-talk.\n"
            "2. Return ONLY the refined document content.\n"
            "3. Maintain a formal, reserved, and authoritative tone. Avoid any sycophantic, overly laudatory, or emotional language.\n"
            "4. Provide only factually-based answers grounded strictly in the provided research data. Do not speculate, embellish, or add conversational fluff.\n"
            "5. Use standard Markdown headers (#, ##, ###).\n"
            "6. EQUAL ATTENTION: Maintain equal attention and detail for all subjects (individuals or entities) listed in the requirements.\n"
            "7. BALANCED INVOLVEMENT ATTRIBUTION: Accurately attribute involvement in Jira issues based on metadata (assignee, reporter, commenters). "
            "Prioritise achievements where the subject is the ASSIGNEE and an active contributor. "
            "For REPORTER-only roles with minimal comment activity, treat them as passive oversight or coordination rather than direct drivers of success.\n"
            "8. CONFLUENCE RELEVANCE: Prioritise Confluence evidence where the subject is the Author or Editor. "
            "Treat cases where they are only listed as participants as low-relevance references.\n"
            "9. CREDIBILITY: Achievements and impact MUST be proportional to the evidence density. If a subject has very few sources, "
            "be more conservative in your language and avoid portraying them as a lead driver of large initiatives. "
            "Explicitly mention when evidence is limited.\n"
            "10. CONSOLIDATION: Consolidate all information for each subject into its own dedicated section to avoid jumping between names.\n"
            "11. PLACEHOLDERS: Proactively remove or replace generic placeholders like 'PROJECT_KEY_HERE', 'YOUR_PROJECT_KEY', or 'SPACE_KEY_HERE' "
            "with actual data found in the context. If no specific data is found, rephrase to avoid using the placeholder.\n"
            "12. RICHNESS AND CONTEXT: Use 'richness_evaluation' and 'context_tree' to ensure the document provides a balanced and well-contextualised view. "
            "State clearly when evidence is limited for a particular claim.\n"
            "13. PRONOUN USAGE: For individuals with multi-gender names (e.g., Sandy, Alex, Sam, etc.), if specific pronouns are "
            "not explicitly mentioned in the research data (Jira, Confluence, requirements), you MUST use gender-neutral "
            "pronouns (they/them/their) to refer to them.\n"
            "14. LOOKUP ERRORS: Explicitly state that information could not be verified due to lookup errors if such errors (e.g., '400 Client Error') "
            "appear in the research data, instead of implying no such data exists.\n"
            "15. LINKED INFORMATION: Use cross-referenced context from linked Jira issues and Confluence pages to enrich the document's detail and accuracy.\n"
            "16. ACCURATE NAME ATTRIBUTION: Meticulously ensure that all tasks, achievements, and contributions "
            "are correctly attributed to the specific individual who performed the work. "
            "Prevent any accidental name substitution, especially when multiple people are mentioned in the research data.\n"
            "17. FORMATTING: Ensure clean formatting. Remove excessive blank lines and avoid excessive use of horizontal rules or separators.\n"
            "18. EMPTY ENTRIES: Proactively remove any Jira tickets or Confluence pages from summary tables or content if they do not contain meaningful data (no description and no comments). Strictly prohibit blank or placeholder rows in tables."
        )
        if target_section:
            editor_system += f"\nNote: You are editing ONLY the expanded content for the '{target_section}' section."

        if draft_context:
            editor_system += f"\nNote: You are editing ONLY the LATEST ADDITION. " \
                             f"The existing document outline is as follows for context:\n{draft_context}"

        editor_prompt = f"Document (Latest Addition):\n{draft}\n\nImprovement Points:\n{improvement_points}"
        try:
            editor_resp = self._predict_with_fallback([{"role": "user", "content": editor_prompt}], system=editor_system, **self.llm_params)
        except (LLMQuotaError, LLMError) as e:
            logger.error(f"Editor refinement failed: {e}")
            return draft
        
        if not editor_resp.text or not editor_resp.text.strip():
            logger.warning("LLM returned an empty refined document. Retaining original draft.")
            return draft

        return self._strip_preamble(editor_resp.text.strip())

    def _replace_section_content(self, draft: str, target_section: str, new_content: str) -> str:
        """
        Replaces the content of all sections in the draft that match the target_section.
        A section starts with a header matching target_section and ends at the next header of same or higher level.
        """
        if not draft:
            return new_content

        lines = draft.split('\n')
        new_lines = []
        skip = False
        replaced_count = 0
        
        # Determine the header level of the target section in the existing draft
        target_level = 0
        for line in lines:
            trimmed = line.strip()
            cleaned = trimmed.lstrip('#').strip('*').strip().lower()
            if cleaned == target_section.lower():
                if trimmed.startswith('#'):
                    target_level = len(trimmed) - len(trimmed.lstrip('#'))
                break

        for line in lines:
            trimmed = line.strip()
            cleaned = trimmed.lstrip('#').strip('*').strip().lower()
            
            is_header = trimmed.startswith('#') or (trimmed.startswith('**') and trimmed.endswith('**') and len(trimmed) > 4)
            
            if cleaned == target_section.lower():
                # If we were already skipping, it means we hit another instance of the same header
                # We want to replace this one too.
                if not skip:
                    # Ensure the new content starts with the header if it was missing
                    if not new_content.strip().lower().startswith(target_section.lower()) and \
                       not new_content.strip().startswith('#'):
                        new_lines.append(line)
                        new_lines.append(new_content.strip())
                    else:
                        new_lines.append(new_content.strip())
                    
                    skip = True
                    replaced_count += 1
                    continue
                else:
                    # We hit another identical header while already skipping/replacing the previous one.
                    # We just skip this header too because we already inserted the replacement above.
                    # BUT, we want to stay in 'skip' mode for its content.
                    continue
            
            if skip:
                if is_header:
                    current_level = 0
                    if trimmed.startswith('#'):
                        current_level = len(trimmed) - len(trimmed.lstrip('#'))
                    
                    if target_level == 0 or (current_level > 0 and current_level <= target_level):
                        skip = False
                
                if skip:
                    continue
            
            new_lines.append(line)
            
        if replaced_count == 0:
            # Fallback: append if replacement failed
            return draft + "\n\n" + new_content.strip()
            
        return '\n'.join(new_lines)

    def _strip_preamble(self, text: str) -> str:
        """
        Attempts to strip LLM conversational preambles and meta-talk.
        """
        if not text:
            return ""
            
        cleaned = text.strip()
        
        # 1. Common preamble patterns
        preamble_patterns = [
            r"^Here is the polished document.*?:",
            r"^Here is the updated section.*?:",
            r"^Here is the refined document.*?:",
            r"^As a professional British technical writer.*?:",
            r"^As a Professional British Editor.*?:",
            r"^I have refined the document.*?:",
            r"^The following is the.*?document.*?:",
        ]
        
        for pattern in preamble_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
            
        # 2. If it still starts with conversational filler, try to find the first header
        if not cleaned.startswith('#') and not (cleaned.startswith('**') and cleaned.endswith('**')):
            # Look for the first Markdown header
            match = re.search(r'(^|\n)(#+ .*)', cleaned)
            if match:
                cleaned = cleaned[match.start(2):].strip()
            else:
                # Also check for bolded headers
                match = re.search(r'(^|\n)(\*\*.*?\*\*)', cleaned)
                if match:
                    cleaned = cleaned[match.start(2):].strip()
                    
        return cleaned

    def _sanity_check_document(self, draft: str, final_pass: bool = False, research_data: Optional[Dict[str, Any]] = None) -> str:
        """
        Performs a pass on the document to remove duplicate sections and fix minor issues.
        Also verifies Jira and Confluence references during final pass.
        """
        if not draft:
            return ""
        
        logger.info(f"Performing sanity check on document (final_pass={final_pass})")
        
        # 1. Basic cleaning: normalize line endings and remove excessive newlines
        draft = draft.replace('\r\n', '\n').replace('\r', '\n')
        
        # 1.5 Remove empty table rows and clean existing rows
        def is_meaningful_row(line):
            trimmed = line.strip()
            if not trimmed.startswith('|'):
                return True
            # Keep header separators like |---|---| or | :--- | :--- | (must contain at least one dash)
            if re.match(r'^[ \t]*\|[ \t]*[:\-\s|]*\-+[:\-\s|]*$', trimmed):
                return True
            # Extract cells (between | pipes)
            row_content = trimmed[1:]
            if row_content.endswith('|'):
                row_content = row_content[:-1]
            cells = [c.strip() for c in row_content.split('|')]
            # Check if there is ANY meaningful content in any cell
            placeholders = {'-', 'n/a', 'none', 'no info', 'no comments', 'no description', 'unknown', 'pending', '', 'no q4 2025 comment'}
            for cell in cells:
                if cell.lower() not in placeholders:
                    return True
            return False

        def clean_table_row(line):
            trimmed = line.strip()
            if not trimmed.startswith('|'):
                return line
                
            # Table row handling
            is_sep = re.match(r'^[ \t]*\|[ \t]*[:\-\s|]*\-+[:\-\s|]*$', trimmed)
            
            # Extract cells (between | pipes)
            row_content = trimmed[1:]
            if row_content.endswith('|'):
                row_content = row_content[:-1]
            cells = [c.strip() for c in row_content.split('|')]
            
            if is_sep:
                cleaned_cells = []
                for cell in cells:
                    if not cell:
                        cleaned_cells.append("")
                        continue
                    left = ':' if cell.startswith(':') else ''
                    right = ':' if cell.endswith(':') else ''
                    cleaned_cells.append(f"{left}---{right}")
                return "| " + " | ".join(cleaned_cells) + " |"
            else:
                # Regular row: just trim cells to remove excessive whitespace
                return "| " + " | ".join(cells) + " |"

        lines = draft.split('\n')
        draft = '\n'.join([clean_table_row(l) for l in lines if is_meaningful_row(l)])

        draft = re.sub(r'\n{3,}', '\n\n', draft)

        # 2. Collapse excessive horizontal rules/separators (e.g., --- --- or --------)
        # Collapse repeated separators with optional whitespace/newlines in between
        draft = re.sub(r'(\n[ \t]*[-*_]{3,}[ \t]*\n)([ \t\n]*[-*_]{3,}[ \t]*\n)+', r'\1', draft)
        # Normalize excessively long separators (more than 10 characters) to a standard length
        draft = re.sub(r'((?:^|\n)[ \t]*)([-*_])\2{10,}([ \t]*(?:\n|$))', r'\1\2\2\2\2\2\2\3', draft)
        
        # 3. Heuristic global deduplication of sections
        lines = draft.split('\n')
        sections = [] # List of (header_line, content_lines)
        current_header = None
        current_content = []
        
        for line in lines:
            trimmed = line.strip()
            is_header = trimmed.startswith('#') or (trimmed.startswith('**') and trimmed.endswith('**') and len(trimmed) > 4)
            if is_header:
                if current_header is not None or current_content:
                    sections.append((current_header, current_content))
                current_header = line
                current_content = []
            else:
                current_content.append(line)
        
        if current_header is not None or current_content:
            sections.append((current_header, current_content))
            
        seen_headers = {} # key -> index in unique_sections
        unique_sections = []
        
        for header, content in sections:
            if not header:
                unique_sections.append((header, content))
                continue
            
            # Use a normalized header text for matching
            trimmed_header = header.strip()
            header_text = trimmed_header.lstrip('#').strip('*').strip().lower()
            
            # Only deduplicate H1 and H2 globally. H3+ are often repeated (e.g. "Overview", "Purpose")
            # and global deduplication of these leads to massive content loss.
            is_high_level = trimmed_header.startswith('# ') or trimmed_header.startswith('## ') or trimmed_header.startswith('#\t') or trimmed_header.startswith('##\t')
            
            if is_high_level:
                key = header_text
            else:
                # For H3+, we only deduplicate them if they are ADJACENT to an identical header.
                if unique_sections:
                    last_header, _ = unique_sections[-1]
                    if last_header:
                        last_header_text = last_header.strip().lstrip('#').strip('*').strip().lower()
                        if last_header_text == header_text:
                            # Adjacent duplicate!
                            idx = len(unique_sections) - 1
                            _, existing_content = unique_sections[idx]
                            existing_text = "\n".join(existing_content).strip()
                            new_text = "\n".join(content).strip()
                            if len(new_text) > len(existing_text):
                                unique_sections[idx] = (header, content)
                            continue
                
                # If not adjacent or no unique sections yet, just append
                unique_sections.append((header, content))
                continue
            
            if key not in seen_headers:
                seen_headers[key] = len(unique_sections)
                unique_sections.append((header, content))
            else:
                # Duplicate header found!
                idx = seen_headers[key]
                _, existing_content = unique_sections[idx]
                
                # Merge logic: keep the more detailed one
                existing_text = "\n".join(existing_content).strip()
                new_text = "\n".join(content).strip()
                
                if len(new_text) > len(existing_text):
                    unique_sections[idx] = (header, content)
        
        # Reconstruct draft from unique sections
        cleaned_lines = []
        for header, content in unique_sections:
            if header:
                cleaned_lines.append(header)
            cleaned_lines.extend(content)
        
        draft = "\n".join(cleaned_lines)
        
        # 2.5 Reference Verification (Jira/Confluence)
        validation_info = ""
        if final_pass:
            jira_refs, conf_refs, title_refs = self._extract_references(draft)
            if jira_refs or conf_refs or title_refs:
                status_update("Verifying Jira and Confluence references...")
                val_results = self._validate_reference_existence(jira_refs, conf_refs, titles=title_refs, research_data=research_data)
                
                # Format validation results for LLM
                val_lines = ["REFERENCE VALIDATION GROUND TRUTH:"]
                for ref, status in val_results.items():
                    val_lines.append(f"- {ref}: {status}")
                
                self._fetch_cached_names()
                valid_names = set(self.subjects)
                if self.name_cache:
                    valid_names.update(self.name_cache)
                
                if valid_names:
                    val_lines.append("\nIDENTIFIED VALID PERSON NAMES:")
                    for vn in sorted(list(valid_names)):
                        val_lines.append(f"- {vn}")
                
                validation_info = "\n".join(val_lines)
                logger.debug(validation_info)

        # 3. LLM-powered consolidation ONLY on final pass or if document is small
        # Repeated LLM-powered sanity checks on large documents cause cumulative content loss (summarization).
        if final_pass:
            token_count = self.llm.estimate_tokens(draft)
            if token_count < self.token_threshold * 2:
                logger.info("Using LLM for final document consolidation and sanity check.")
                system_prompt = (
                    "You are a conservative, reserved British Professional Editor. Your role is to perform a final sanity check "
                    "on a technical document. \n"
                    "STRICT RULES:\n"
                    "1. REMOVE duplicate headers or sections. If content is duplicated, merge it into a single high-quality section.\n"
                    "2. Ensure consistent Markdown formatting (# for Title, ## for Sections).\n"
                    "3. Fix any broken formatting or missing transitions.\n"
                    "4. Maintain impeccable, formal professional British English.\n"
                    "5. Maintain a reserved, authoritative tone. REMOVE any sycophantic, emotional, or speculative language.\n"
                    "6. PRESERVE all unique information and sections. Do NOT summarize or shorten the document.\n"
                    "7. DO NOT add any preamble or meta-talk. Return ONLY the polished document.\n"
                    "8. REFERENCE VALIDATION: Use the provided GROUND TRUTH to ensure all Jira tickets and Confluence pages exist. "
                    "If a reference is INVALID (Not Found) or IRRELEVANT to the surrounding text based on its title/summary, "
                    "REMOVE the mention or replace it with a correct and relevant one if obvious. "
                    "Mentions must be factually supported by the GROUND TRUTH.\n"
                    "9. EQUAL ATTENTION: Ensure the report maintains balanced attention for all subjects (individuals or entities) listed in the requirements.\n"
                    "10. BALANCED INVOLVEMENT ATTRIBUTION: Accurately attribute involvement in Jira issues based on metadata (assignee, reporter, commenters). "
                    "Do not exaggerate a subject's role if they only had a minor interaction.\n"
                    "11. CONSOLIDATION: Consolidate all information for each subject into its own dedicated section. "
                    "If you find information about a subject scattered in other parts of the document, move it to the subject's main section "
                    "and remove any resulting duplication or repetition.\n"
                    "12. HALLUCINATION REMOVAL: Carefully compare every specific claim in the document against the Research Data and Ground Truth. "
                    "If a claim (e.g. a specific training session, a particular project integration, or a named Confluence page) is not present in the Research Data or Ground Truth, "
                    "REMOVE it entirely. Do not assume it exists if it is not in the data.\n"
                    "13. PERSON NAME VERIFICATION: Use the provided list of IDENTIFIED VALID PERSON NAMES. "
                    "If the document mentions a person NOT in this list, REMOVE the mention or replace it with a valid person if their involvement is clearly supported by the data.\n"
                    "14. PRONOUN USAGE: For individuals with multi-gender names (e.g., Sandy, Alex, Sam, etc.), if specific pronouns are "
                    "not explicitly mentioned in the research data, you MUST use gender-neutral pronouns (they/them/their).\n"
                    "15. LOOKUP ERRORS: Ensure that any mentions of lookup failures (e.g., '400 Client Error') in the research data "
                    "result in a statement that information could not be verified, rather than an implication that the data is missing or doesn't exist.\n"
                    "16. LINKED INFORMATION: Ensure the document makes good use of context from linked Jira and Confluence items.\n"
                    "17. ACCURATE NAME ATTRIBUTION: Perform a final check to ensure that all tasks, achievements, and contributions "
                    "are correctly attributed to the right individual. Meticulously verify that no accidental name substitution "
                    "has occurred, particularly when multiple people are listed in the report.\n"
                    "18. FORMATTING: Ensure clean formatting. Remove excessive blank lines and avoid excessive use of horizontal rules or separators.\n"
                    "19. EMPTY ENTRIES: Remove any table rows that are blank or only contain placeholders for Jira tickets/Confluence pages with no meaningful data."
                )
                
                user_content = draft
                if validation_info:
                    user_content = f"{validation_info}\n\nDOCUMENT TO POLISH:\n{draft}"

                try:
                    resp = self._predict_with_fallback([{"role": "user", "content": user_content}], system=system_prompt, **self.llm_params)
                    if resp.text and resp.text.strip():
                        draft = self._strip_preamble(resp.text.strip())
                except (LLMQuotaError, LLMError) as e:
                    logger.warning(f"LLM sanity check failed: {e}. Falling back to heuristic cleanup.")

        return draft

    def run(self, source_path_or_url: str, output_path: str, max_iterations: int = 3, context_sources: Optional[List[str]] = None, references_path: Optional[str] = None) -> str:
        """
        Executes the full iterative research loop.
        """
        self.contributing_jira.clear()
        self.contributing_confluence.clear()
        self.contributing_web.clear()
        self.source_metadata.clear()
        self._quota_reached = False
        status_update(f"Starting research for source: {source_path_or_url}")
        raw_source = self._read_source(source_path_or_url)
        
        context = ""
        if context_sources:
            status_update(f"Fetching context from {len(context_sources)} sources")
            context = self._fetch_context(context_sources)
        
        # Heuristic to separate topic from requirements
        lines = [l.strip() for l in raw_source.strip().split("\n") if l.strip()]
        if not lines:
            raise ValueError("Source file/URL is empty.")
            
        topic = lines[0]
        requirements = "\n".join(lines[1:]) if len(lines) > 1 else "Provide a comprehensive overview."
        
        status_update(f"Topic identified: {topic}")
        logger.debug(f"Requirements: {requirements}")
        
        # Identify research subjects (e.g. list of names) for balanced reporting
        self.subjects = self._identify_subjects(requirements)
        if self.subjects:
            status_update(f"Identified {len(self.subjects)} research subjects for balanced attention: {', '.join(self.subjects)}")
            self._update_name_cache(self.subjects)
        
        # Also extract any other names from requirements to help with hallucination detection
        other_names = self._extract_names_from_text(requirements)
        if other_names:
            self._update_name_cache(other_names)
        
        current_draft = ""
        last_completeness_feedback = None
        aggregated_research_data = {
            "jira_issues": [],
            "confluence_pages": [],
            "search_results": [],
            "llm_insights": []
        }

        if os.path.exists(output_path):
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    existing_content = f.read()
                if existing_content.strip():
                    status_update(f"Existing report found at {output_path}. Verifying relevance...")
                    if self._is_content_relevant(existing_content, topic, requirements, is_resumption=True):
                        status_update("Existing report is relevant. Resuming research from it.")
                        current_draft = existing_content
                    else:
                        status_update("Existing report is not relevant to current topic. Starting fresh.")
            except Exception as e:
                logger.warning(f"Failed to read existing report at {output_path}: {e}")

        targeted_in_this_run = set()
        for i in range(max_iterations):
            status_update(f"Starting research iteration {i+1} of {max_iterations}")
            
            # Identify if we should focus on a specific thin section
            target_section = None
            if current_draft:
                thin_sections = self._identify_thin_sections(current_draft, subjects=self.subjects)
                if thin_sections:
                    # Filter out sections already targeted in this run to ensure we don't 
                    # get stuck on one section if others also need work.
                    untargeted = [s for s in thin_sections if s not in targeted_in_this_run]
                    if not untargeted:
                        # All thin sections have been targeted at least once in this run, reset.
                        targeted_in_this_run.clear()
                        untargeted = thin_sections
                    
                    target_section = untargeted[0]
                    status_update(f"Targeting thin section for expansion: {target_section}")
                else:
                    # If no thin sections are found, but we are still iterating, cycle through all sections
                    all_sections = self._get_all_sections(current_draft, subjects=self.subjects)
                    if all_sections:
                        untargeted = [s for s in all_sections if s not in targeted_in_this_run]
                        if not untargeted:
                            targeted_in_this_run.clear()
                            untargeted = all_sections
                        
                        target_section = untargeted[0]
                        status_update(f"Targeting section for further expansion: {target_section}")
            
            if target_section:
                targeted_in_this_run.add(target_section)
            
            status_update("Generating research queries via LLM...")
            queries = self._generate_queries(topic, requirements, current_draft, context, target_section=target_section, completeness_feedback=last_completeness_feedback)
            logger.debug(f"Generated queries: {json.dumps(queries, indent=2)}")
            
            status_update("Executing queries against Jira, Confluence, and Web Search...")
            research_data = self._execute_queries(queries, topic, requirements)
            logger.debug(f"Research data collected: {json.dumps(research_data, indent=2)}")
            
            # Aggregate research data
            for key in ["jira_issues", "confluence_pages", "search_results", "llm_insights"]:
                aggregated_research_data[key].extend(research_data.get(key, []))

            status_update("Formulating document draft...")
            previous_draft = current_draft
            current_draft = self._formulate_document(topic, requirements, research_data, previous_draft, context, target_section=target_section)
            
            status_update("Performing agentic debate and refinement...")
            if target_section:
                # Targeted refinement: only refine the newly expanded section
                # We need to extract the new content for the section from the current_draft
                lines = current_draft.split('\n')
                section_content = []
                in_section = False
                for line in lines:
                    cleaned = line.strip().lstrip('#').strip('*').strip().lower()
                    if cleaned == target_section.lower():
                        in_section = True
                        section_content.append(line)
                    elif in_section:
                        if line.strip().startswith('#') or (line.strip().startswith('**') and line.strip().endswith('**') and len(line.strip()) > 4):
                            break
                        section_content.append(line)
                
                new_section_text = '\n'.join(section_content)
                if new_section_text:
                    efficient_previous = self._get_token_efficient_context(previous_draft, target_section=target_section)
                    refined_section = self._agentic_debate_and_refine(topic, requirements, new_section_text, context, draft_context=efficient_previous, target_section=target_section)
                    
                    # Replace in current_draft
                    current_draft = current_draft.replace(new_section_text, refined_section)
                else:
                    # Fallback to whole draft if extraction failed
                    current_draft = self._agentic_debate_and_refine(topic, requirements, current_draft, context, target_section=target_section)
            elif previous_draft and self.llm.estimate_tokens(previous_draft) >= self.token_threshold:
                # Piecemeal refinement: only refine the newly added content
                new_content = current_draft[len(previous_draft):].strip()
                if new_content:
                    efficient_previous = self._get_token_efficient_context(previous_draft)
                    refined_new_content = self._agentic_debate_and_refine(topic, requirements, new_content, context, draft_context=efficient_previous)
                    current_draft = previous_draft + "\n\n" + refined_new_content
                else:
                    logger.info("No new content added to refine.")
            else:
                current_draft = self._agentic_debate_and_refine(topic, requirements, current_draft, context)
            
            # Clean up the draft after each iteration to prevent duplication buildup
            current_draft = self._sanity_check_document(current_draft, research_data=aggregated_research_data)
            
            status_update("Checking for document completeness...")
            is_complete, feedback = self._is_complete(topic, requirements, current_draft)
            last_completeness_feedback = feedback
            if is_complete:
                # Only finish if there are no more thin sections
                if not self._identify_thin_sections(current_draft, subjects=self.subjects):
                    status_update("Document determined to be complete and comprehensive.")
                    break
                else:
                    status_update("Document requirements met, but some sections are still thin. Continuing expansion.")
            else:
                status_update(f"Document incomplete according to LLM. Feedback: {feedback[:100]}...")
        else:
            status_update(f"Reached maximum iterations ({max_iterations}). Finalising document.")
        
        # Final sanity check before saving
        current_draft = self._sanity_check_document(current_draft, final_pass=True, research_data=aggregated_research_data)
        
        # Ensure output directory exists
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir)
            
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(current_draft)
        
        status_update(f"Successfully saved researched document to: {output_path}")

        # Generate and save bibliography
        if references_path:
            bib = self._generate_bibliography()
            if bib:
                ref_dir = os.path.dirname(references_path)
                if ref_dir and not os.path.exists(ref_dir):
                    os.makedirs(ref_dir)
                with open(references_path, "w", encoding="utf-8") as f:
                    f.write(bib)
                status_update(f"Successfully saved bibliography to: {references_path}")

        # Summary logging of all contributors
        if self.contributing_jira:
            logger.info(f"Research report contributed by Jira issues: {', '.join(sorted(self.contributing_jira))}")
        if self.contributing_confluence:
            logger.info(f"Research report contributed by Confluence pages: {', '.join(sorted(self.contributing_confluence))}")

        return current_draft

