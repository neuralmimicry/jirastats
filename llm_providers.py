"""
Lightweight LLM provider adapters: OpenAI, Gemini, and Ollama.

No heavy SDK dependencies; uses requests to call HTTP endpoints.

Environment variables:
- OPENAI_API_KEY
- GEMINI_API_KEY
- OLLAMA_BASE_URL (optional, defaults to http://localhost:11434)

Robustness env defaults (optional):
- LLM_TIMEOUT_SECONDS (default 60)
- LLM_MAX_RETRIES (default 2)
- LLM_BACKOFF_BASE (seconds, default 0.5)
- LLM_BACKOFF_MAX (seconds, default 8)

Public factory:
- get_provider(name: str, model: str | None = None, base_url: str | None = None) -> LLMProvider
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional, List
import os
import time
import hashlib
import json
import requests
import random
import logging
import re
import threading

logger = logging.getLogger(__name__)


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


class LLMError(RuntimeError):
    pass


class LLMQuotaError(LLMError):
    """Raised when the LLM provider returns a 429 (Quota Exceeded) error."""
    pass


@dataclass
class LLMResponse:
    text: str
    raw: Dict[str, Any]
    latency_ms: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None


class LLMProvider:
    name: str
    model: str

    def __init__(self, inter_request_gap: float = 0.0):
        self.inter_request_gap = inter_request_gap
        self._last_request_time = 0.0
        self._lock = threading.Lock()

    def _wait_for_gap(self):
        """Ensures that the configured inter-request gap is respected."""
        if self.inter_request_gap <= 0:
            return

        with self._lock:
            now = time.time()
            elapsed = now - self._last_request_time
            if elapsed < self.inter_request_gap:
                delay = self.inter_request_gap - elapsed
                logger.info(f"Respecting LLM inter-request gap: sleeping {delay:.2f}s")
                time.sleep(delay)
            self._last_request_time = time.time()

    def predict(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None, temperature: float = 0.2, system: Optional[str] = None, timeout: Optional[int] = None) -> LLMResponse:
        raise NotImplementedError

    def transcribe(self, file_path: str, timeout: Optional[int] = None) -> str:
        """Transcribe audio/video to text."""
        raise NotImplementedError

    def health_check(self, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Perform a lightweight availability probe for the provider.
        Returns a dict: {"ok": bool, "status_code": int|None, "latency_ms": int|None, "message": str}
        """
        raise NotImplementedError

    def estimate_tokens(self, text: str) -> int:
        # Simple heuristic: ~1 token ≈ 4 chars in English
        return max(1, int(len(text) / 4))

    def get_context_window(self) -> int:
        """Returns the context window size for the model."""
        return 4096


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _should_retry(status: Optional[int], exc: Optional[Exception]) -> bool:
    if exc is not None:
        # Network issues/timeouts
        return True
    if status is None:
        return False
    # Retry on 429 and 5xx
    return status == 429 or (500 <= status < 600)


def _retry_after_seconds(resp: Optional[requests.Response]) -> Optional[float]:
    if not resp:
        return None
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except Exception:
        return None


def _http_post(url: str, *, headers: Dict[str, str], json_payload: Dict[str, Any], timeout: Optional[int] = None) -> requests.Response:
    """
    POST with exponential backoff + jitter on transient errors.
    Honors Retry-After when present. Raises LLMError after exhausting retries.
    """
    max_retries = _env_int("LLM_MAX_RETRIES", 2)
    base = _env_float("LLM_BACKOFF_BASE", 0.5)
    backoff_max = _env_float("LLM_BACKOFF_MAX", 8.0)
    tmt = timeout or _env_int("LLM_TIMEOUT_SECONDS", 60)

    logger.debug(f"HTTP POST to {url} (timeout={tmt})")
    if logger.isEnabledFor(logging.DEBUG):
        # Avoid serializing large payloads if not in debug
        logger.debug(f"Payload: {json.dumps(json_payload)[:1000]}...")

    attempt = 0
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    last_resp: Optional[requests.Response] = None
    while True:
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(json_payload), timeout=tmt)
            last_resp = resp
            last_status = resp.status_code
            if resp.status_code < 300:
                logger.debug(f"HTTP POST success (status={resp.status_code})")
                return resp
            if not _should_retry(resp.status_code, None):
                # Non-retryable status
                logger.debug(f"HTTP POST failed with non-retriable status {resp.status_code}")
                return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            resp = None
            last_status = None

        if attempt >= max_retries:
            if last_exc:
                raise LLMError(f"HTTP POST failed after {attempt+1} attempts: {last_exc}")
            else:
                text = (last_resp.text[:200] if last_resp is not None else "")
                if last_status == 429:
                    raise LLMQuotaError(f"HTTP POST failed after {attempt+1} attempts, status 429: {text}")
                raise LLMError(f"HTTP POST failed after {attempt+1} attempts, status {last_status}: {text}")

        # Compute sleep seconds from either Retry-After or exponential backoff
        ra = _retry_after_seconds(last_resp)
        if ra is not None:
            delay = min(backoff_max, ra)
        else:
            delay = min(backoff_max, base * (2 ** attempt))
            # add jitter 0-200ms
            delay += random.uniform(0, 0.2)
        
        logger.info(f"Retrying LLM request in {delay:.2f}s (attempt {attempt+1}/{max_retries+1}) due to {last_exc or f'HTTP {last_status}'}")
        time.sleep(delay)
        attempt += 1


class OpenAIProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None, inter_request_gap: float = 0.0, api_key: Optional[str] = None):
        super().__init__(inter_request_gap=inter_request_gap)
        self.name = "openai"
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY not set")

    def predict(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None, temperature: float = 0.2, system: Optional[str] = None, timeout: Optional[int] = None) -> LLMResponse:
        self._wait_for_gap()
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if system:
            messages = [{"role": "system", "content": system}] + messages
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        start = time.time()
        resp = _http_post(url, headers=headers, json_payload=payload, timeout=timeout)
        latency_ms = int((time.time() - start) * 1000)
        if resp.status_code >= 300:
            raise LLMError(f"OpenAI error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        # Guard against missing fields
        text = ""
        try:
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                text = msg.get("content") or ""
        except Exception:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        
        logger.debug(f"OpenAI Response: {text[:500]}...")
        return LLMResponse(text=text, raw=data, latency_ms=latency_ms, provider=self.name, model=self.model)

    def transcribe(self, file_path: str, timeout: Optional[int] = None) -> str:
        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        # Note: requests handles multipart/form-data when files is provided
        tmt = timeout or _env_int("LLM_TIMEOUT_SECONDS", 60)
        try:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f)}
                data = {"model": "whisper-1"}
                resp = requests.post(url, headers=headers, files=files, data=data, timeout=tmt)
                resp.raise_for_status()
                return resp.json().get("text", "")
        except Exception as e:
            raise LLMError(f"OpenAI transcription failed: {e}")

    def health_check(self, timeout: Optional[int] = None) -> Dict[str, Any]:
        url = "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {}
        start = time.time()
        try:
            resp = _http_post(url, headers=headers, json_payload=payload, timeout=timeout or _env_int("LLM_TIMEOUT_SECONDS", 60))
            latency_ms = int((time.time() - start) * 1000)
            ok = resp.status_code < 300
            return {"ok": ok, "status_code": resp.status_code, "latency_ms": latency_ms, "message": "ok" if ok else resp.text[:200]}
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return {"ok": False, "status_code": None, "latency_ms": latency_ms, "message": str(e)}

    def get_context_window(self) -> int:
        model = self.model.lower()
        if "gpt-4o" in model: # covers gpt-4o, gpt-4o-mini
            return 128000
        if "gpt-4-turbo" in model:
            return 128000
        if "gpt-4" in model:
            if "32k" in model:
                return 32768
            return 8192
        if "gpt-3.5-turbo" in model:
            if "16k" in model:
                return 16385
            return 4096
        if "o1-" in model:
            return 128000
        return super().get_context_window()


class GeminiProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None, inter_request_gap: float = 0.0, api_key: Optional[str] = None, access_token: Optional[str] = None):
        super().__init__(inter_request_gap=inter_request_gap)
        self.name = "gemini"
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.access_token = access_token or os.getenv("GEMINI_ACCESS_TOKEN") or os.getenv("GOOGLE_ACCESS_TOKEN")
        if not self.api_key and not self.access_token:
            raise LLMError("Neither GEMINI_API_KEY nor GEMINI_ACCESS_TOKEN (or GOOGLE_ACCESS_TOKEN) is set")

    def predict(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None, temperature: float = 0.2, system: Optional[str] = None, timeout: Optional[int] = None) -> LLMResponse:
        self._wait_for_gap()
        # Gemini generateContent expects a contents list of role/parts
        # We use v1beta as it supports newer models and reasoning features
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        
        # Priority: API Key (Preferred) then Access Token (OAuth 2.0)
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        elif self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            
        contents = []
        if system:
            contents.append({"role": "user", "parts": [{"text": f"System instruction: {system}"}]})
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, str):
                contents.append({"role": role, "parts": [{"text": content}]})
            elif isinstance(content, list):
                # Handle multimodal content if provided
                parts = []
                for part in content:
                    if part.get("type") == "text":
                        parts.append({"text": part.get("text")})
                    elif part.get("type") == "image_url":
                        # Gemini expects base64 in a slightly different way
                        # For simplicity, we'll assume the URL is a data URL
                        url_val = part.get("image_url", {}).get("url", "")
                        if url_val.startswith("data:"):
                            mime, b64 = url_val.split(";base64,")
                            mime = mime.replace("data:", "")
                            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                contents.append({"role": role, "parts": parts})

        payload = {"contents": contents, "generationConfig": {"temperature": temperature}}
        if max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = max_tokens
        start = time.time()
        resp = _http_post(url, headers=headers, json_payload=payload, timeout=timeout)
        latency_ms = int((time.time() - start) * 1000)
        if resp.status_code >= 300:
            raise LLMError(f"Gemini error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        candidates = data.get("candidates", [])
        text = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        if not isinstance(text, str):
            text = str(text)
        
        logger.debug(f"Gemini Response: {text[:500]}...")
        return LLMResponse(text=text, raw=data, latency_ms=latency_ms, provider=self.name, model=self.model)

    def transcribe(self, file_path: str, timeout: Optional[int] = None) -> str:
        # Gemini also supports audio/video, but via standard generateContent with file upload/data.
        # For now, we'll implement it as a prediction with a specific prompt.
        import base64
        import mimetypes
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "audio/mpeg"
        
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Please transcribe this audio/video file exactly."},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}} # Re-using image_url structure for simplicity
            ]
        }]
        # We need to adjust predict to handle this generic structure
        resp = self.predict(messages, timeout=timeout)
        return resp.text

    def health_check(self, timeout: Optional[int] = None) -> Dict[str, Any]:
        # call models:list style endpoint
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = {}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        elif self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            
        start = time.time()
        try:
            resp = requests.get(url, headers=headers, timeout=timeout or _env_int("LLM_TIMEOUT_SECONDS", 60))
            latency_ms = int((time.time() - start) * 1000)
            ok = resp.status_code < 300
            return {"ok": ok, "status_code": resp.status_code, "latency_ms": latency_ms, "message": "ok" if ok else resp.text[:200]}
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return {"ok": False, "status_code": None, "latency_ms": latency_ms, "message": str(e)}

    def get_context_window(self) -> int:
        model = self.model.lower()
        if "pro" in model:
            if "1.5" in model or "2.0" in model or "2.5" in model:
                return 2000000
            if "1.0" in model:
                return 32768
        if "flash" in model:
            return 1000000
        # Default fallbacks
        if "gemini-1.5" in model or "gemini-2.0" in model or "gemini-2.5" in model:
            return 1000000
        return super().get_context_window()


class OllamaProvider(LLMProvider):
    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None, inter_request_gap: float = 0.0, **kwargs):
        super().__init__(inter_request_gap=inter_request_gap)
        self.name = "ollama"
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")

    def predict(self, messages: List[Dict[str, Any]], max_tokens: Optional[int] = None, temperature: float = 0.2, system: Optional[str] = None, timeout: Optional[int] = None) -> LLMResponse:
        self._wait_for_gap()
        # Collapse messages into a single prompt for /api/generate
        # Note: Ollama /api/chat is better for multimodal, but we are using /api/generate here
        # For simplicity, we'll keep generate but ideally it should use chat
        prompt = ""
        images = []
        for m in messages:
            content = m.get("content", "")
            role = m.get("role", "user")
            if isinstance(content, str):
                prompt += f"{role}: {content}\n"
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        prompt += f"{role}: {part.get('text')}\n"
                    elif part.get("type") == "image_url":
                        url_val = part.get("image_url", {}).get("url", "")
                        if url_val.startswith("data:"):
                            _, b64 = url_val.split(";base64,")
                            images.append(b64)
        
        if system:
            prompt = f"System: {system}\n" + prompt
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "options": {"temperature": temperature},
            "stream": False,
        }
        if images:
            payload["images"] = images
            
        # Ollama may not support explicit max_tokens uniformly across models; omit if None
        start = time.time()
        try:
            resp = _http_post(url, headers={"Content-Type": "application/json"}, json_payload=payload, timeout=timeout)
        except LLMError as e:
            raise LLMError(f"Ollama connection error: {e}")
        latency_ms = int((time.time() - start) * 1000)
        if resp.status_code >= 300:
            raise LLMError(f"Ollama error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        text = data.get("response", "")
        if not isinstance(text, str):
            text = str(text)
        
        if not text:
            logger.debug(f"Ollama returned an empty response. Full data: {data}")
        else:
            logger.debug(f"Ollama Response: {text[:500]}...")
            
        return LLMResponse(text=text, raw=data, latency_ms=latency_ms, provider=self.name, model=self.model)

    def transcribe(self, file_path: str, timeout: Optional[int] = None) -> str:
        raise NotImplementedError("Ollama transcription not yet implemented in this adapter.")

    def health_check(self, timeout: Optional[int] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tags"
        start = time.time()
        try:
            resp = requests.get(url, timeout=timeout or _env_int("LLM_TIMEOUT_SECONDS", 60))
            latency_ms = int((time.time() - start) * 1000)
            ok = resp.status_code < 300
            return {"ok": ok, "status_code": resp.status_code, "latency_ms": latency_ms, "message": "ok" if ok else resp.text[:200]}
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return {"ok": False, "status_code": None, "latency_ms": latency_ms, "message": str(e)}

    def get_context_window(self) -> int:
        url = f"{self.base_url}/api/show"
        payload = {"name": self.model}
        try:
            # We use requests directly here to avoid retries/backoff for a metadata call
            resp = requests.post(url, json=payload, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                
                # 1. Check for num_ctx in parameters
                parameters = data.get("parameters", "")
                if parameters:
                    match = re.search(r"num_ctx\s+(\d+)", parameters)
                    if match:
                        return int(match.group(1))
                
                # 2. Check model info
                model_info = data.get("model_info", {})
                if model_info:
                    ctx = model_info.get("llama.context_length") or model_info.get("context_length")
                    if ctx:
                        return int(ctx)
        except Exception:
            pass
            
        # 3. Fallbacks based on known models
        m = self.model.lower()
        if "llama3" in m:
            return 8192
        if "phi3" in m:
            return 128000
        if "mistral" in m:
            return 32768
            
        return super().get_context_window()


def get_provider(name: Optional[str], model: Optional[str] = None, base_url: Optional[str] = None, inter_request_gap: float = 0.0, **kwargs) -> Optional[LLMProvider]:
    if not name:
        return None
    name = name.lower().strip()
    if name in ("openai", "chatgpt", "gpt"):
        return OpenAIProvider(model=model, inter_request_gap=inter_request_gap, **kwargs)
    if name in ("gemini", "google"):
        return GeminiProvider(model=model, inter_request_gap=inter_request_gap, **kwargs)
    if name in ("ollama",):
        return OllamaProvider(model=model, base_url=base_url, inter_request_gap=inter_request_gap, **kwargs)
    raise LLMError(f"Unknown provider: {name}")
