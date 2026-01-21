"""LLM client helpers for job enrichment and fit scoring."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests

ENV_API_KEY = "LLM_API_KEY"
ENV_BASE_URL = "LLM_BASE_URL"
ENV_MODEL = "LLM_MODEL"
MISSING_CONFIG_MESSAGE = "Missing LLM configuration."

ENRICH_SYSTEM_PROMPT = """\
You are an assistant that analyzes software job postings.
Return only valid JSON, no markdown or explanation.
"""

ENRICH_USER_PROMPT = """\
Extract structured data from this job description.
Use the exact JSON schema below and return only JSON.

Schema:
{{
  "normalized_role": "Senior Backend Engineer",
  "seniority": "senior|mid|junior|lead",
  "location": "Dublin, Ireland",
  "remote_level": "onsite|hybrid|remote",
  "skills": ["Python", "Flask", "MongoDB"],
  "salary_band": "e.g. 80-100k EUR or null",
  "canonical_company": "Acme Corp"
}}

Job description:
{job_text}
"""

SCORE_SYSTEM_PROMPT = """\
You are an assistant that analyzes software job postings.
Return only valid JSON, no markdown or explanation.
"""

SCORE_USER_PROMPT = """\
Score job fit for a single user profile.
Use the exact JSON schema below and return only JSON.

Schema:
{{
  "fit_score": 0-100,
  "summary": "Short 2-3 sentence overview of the role.",
  "pros": ["bullet point"],
  "cons": ["bullet point"],
  "missing_skills": ["skill1", "skill2"],
  "apply_recommendation": "apply|maybe|skip"
}}

Job:
{job_json}

Profile:
{profile_json}
"""

LEADS_SYSTEM_PROMPT = """\
You are an assistant that helps find software job opportunities.
Return only valid JSON, no markdown or explanation.
"""

LEADS_USER_PROMPT = """\
Based on the profile below, generate job search leads.
Use the exact JSON schema below and return only JSON.

Schema:
{{
  "search_queries": ["query string"],
  "target_companies": ["company"],
  "role_keywords": ["keyword"],
  "locations": ["location"],
  "checklist": ["what to verify in a posting"]
}}

Profile:
{profile_json}
"""


class LLMClientError(RuntimeError):
    """Base error for LLM client failures."""


class LLMConfigError(LLMClientError):
    """Raised when required LLM configuration is missing."""


class LLMResponseError(LLMClientError):
    """Raised when the LLM response is invalid or unusable."""


@dataclass
class LLMClient:
    """Minimal client for OpenAI-compatible chat completions."""

    api_base_url: str
    api_key: str
    model: str
    timeout: int = 30

    @classmethod
    def from_env(cls) -> Optional["LLMClient"]:
        """Create a client from environment variables."""
        api_key = os.getenv(ENV_API_KEY, "")
        base_url = os.getenv(ENV_BASE_URL, "")
        model = os.getenv(ENV_MODEL, "")
        if not api_key or not base_url or not model:
            return None
        return cls(api_base_url=base_url, api_key=api_key, model=model)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """Send a chat completion request and parse a JSON response."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
        try:
            response = requests.post(
                self.api_base_url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise LLMResponseError("LLM request timed out.") from exc
        except requests.RequestException as exc:
            raise LLMResponseError("LLM request failed.") from exc

        if response.status_code != 200:
            raise LLMResponseError(
                f"LLM response status {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
        content = _extract_content(data)
        return _parse_json_content(content)


def enrich_job(job_text: str) -> Dict[str, Any]:
    """Enrich a raw job description into structured fields."""
    prompt = ENRICH_USER_PROMPT.format(job_text=job_text.strip())
    return _run_prompt(
        system_prompt=ENRICH_SYSTEM_PROMPT,
        user_prompt=prompt,
        validator=_validate_enrich_payload,
        fallback=_enrich_fallback,
    )


def score_job(job: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """Score job fit relative to a stored user profile."""
    prompt = SCORE_USER_PROMPT.format(
        job_json=json.dumps(job, ensure_ascii=True, default=str),
        profile_json=json.dumps(profile, ensure_ascii=True, default=str),
    )
    return _run_prompt(
        system_prompt=SCORE_SYSTEM_PROMPT,
        user_prompt=prompt,
        validator=_validate_score_payload,
        fallback=_score_fallback,
    )


def generate_job_leads(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Generate job search leads for a profile."""
    prompt = LEADS_USER_PROMPT.format(
        profile_json=json.dumps(profile, ensure_ascii=True, default=str),
    )
    return _run_prompt(
        system_prompt=LEADS_SYSTEM_PROMPT,
        user_prompt=prompt,
        validator=_validate_leads_payload,
        fallback=_leads_fallback,
    )


def _run_prompt(
    system_prompt: str,
    user_prompt: str,
    validator: Callable[[Dict[str, Any]], Dict[str, Any]],
    fallback: Callable[[str], Dict[str, Any]],
) -> Dict[str, Any]:
    """Execute a prompt against the LLM and normalize the response."""
    client = LLMClient.from_env()
    if client is None:
        return fallback(MISSING_CONFIG_MESSAGE)
    try:
        payload = client.chat_json(system_prompt, user_prompt)
        return validator(payload)
    except LLMClientError as exc:
        return fallback(str(exc))


def _extract_content(data: Dict[str, Any]) -> str:
    """Extract the model's text content from a response body."""
    if "choices" in data and data["choices"]:
        choice = data["choices"][0]
        if isinstance(choice, dict):
            message = choice.get("message", {})
            if isinstance(message, dict) and "content" in message:
                return str(message["content"])
            if "text" in choice:
                return str(choice["text"])
    if "content" in data:
        return str(data["content"])
    if "text" in data:
        return str(data["text"])
    raise LLMResponseError("LLM response missing content.")


def _strip_code_fences(text: str) -> str:
    """Remove markdown-style JSON fences if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return cleaned.strip()


def _parse_json_content(text: str) -> Dict[str, Any]:
    """Parse JSON content into a dictionary."""
    cleaned = _strip_code_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMResponseError("Invalid JSON returned by LLM.") from exc
    if not isinstance(data, dict):
        raise LLMResponseError("LLM JSON response must be an object.")
    return data


def _validate_enrich_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the enrich_job response."""
    return {
        "normalized_role": _as_str(payload.get("normalized_role")),
        "seniority": _as_str(payload.get("seniority")),
        "location": _as_str(payload.get("location")),
        "remote_level": _as_str(payload.get("remote_level")),
        "skills": _as_list(payload.get("skills")),
        "salary_band": payload.get("salary_band"),
        "canonical_company": _as_str(payload.get("canonical_company")),
    }


def _validate_score_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the score_job response."""
    fit_score = _as_int(payload.get("fit_score"), default=0)
    fit_score = max(0, min(100, fit_score))
    recommendation = _as_str(payload.get("apply_recommendation")) or "maybe"
    if recommendation not in {"apply", "maybe", "skip"}:
        recommendation = "maybe"

    return {
        "fit_score": fit_score,
        "summary": _as_str(payload.get("summary")),
        "pros": _as_list(payload.get("pros")),
        "cons": _as_list(payload.get("cons")),
        "missing_skills": _as_list(payload.get("missing_skills")),
        "apply_recommendation": recommendation,
    }


def _validate_leads_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the generate_job_leads response."""
    return {
        "search_queries": _as_list(payload.get("search_queries")),
        "target_companies": _as_list(payload.get("target_companies")),
        "role_keywords": _as_list(payload.get("role_keywords")),
        "locations": _as_list(payload.get("locations")),
        "checklist": _as_list(payload.get("checklist")),
    }


def _enrich_fallback(message: str) -> Dict[str, Any]:
    """Fallback payload when enrich_job fails."""
    return {
        "normalized_role": None,
        "seniority": None,
        "location": None,
        "remote_level": None,
        "skills": [],
        "salary_band": None,
        "canonical_company": None,
        "error": message,
    }


def _score_fallback(message: str) -> Dict[str, Any]:
    """Fallback payload when score_job fails."""
    return {
        "fit_score": 0,
        "summary": "",
        "pros": [],
        "cons": [],
        "missing_skills": [],
        "apply_recommendation": "maybe",
        "error": message,
    }


def _leads_fallback(message: str) -> Dict[str, Any]:
    """Fallback payload when generate_job_leads fails."""
    return {
        "search_queries": [],
        "target_companies": [],
        "role_keywords": [],
        "locations": [],
        "checklist": [],
        "error": message,
    }


def _as_str(value: Any) -> Optional[str]:
    """Normalize a value to a string if possible."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _as_list(value: Any) -> list:
    """Normalize a value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _as_int(value: Any, default: int = 0) -> int:
    """Normalize a value to an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
