"""Tests for LLM client helpers."""

from __future__ import annotations

import json

import pytest

from app import llm_client


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _set_env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.com/llm")
    monkeypatch.setenv("LLM_MODEL", "test-model")


def test_enrich_job_success(monkeypatch, mocker):
    _set_env(monkeypatch)
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "normalized_role": "Backend Engineer",
                            "seniority": "mid",
                            "location": "Remote",
                            "remote_level": "remote",
                            "skills": ["Python", "Flask"],
                            "salary_band": "80-100k EUR",
                            "canonical_company": "Acme",
                        }
                    )
                }
            }
        ]
    }
    mocker.patch("app.llm_client.requests.post", return_value=FakeResponse(200, response_payload))
    result = llm_client.enrich_job("Example job description")

    assert result["normalized_role"] == "Backend Engineer"
    assert result["remote_level"] == "remote"
    assert result["skills"] == ["Python", "Flask"]


def test_enrich_job_invalid_json(monkeypatch, mocker):
    _set_env(monkeypatch)
    response_payload = {"choices": [{"message": {"content": "not json"}}]}
    mocker.patch("app.llm_client.requests.post", return_value=FakeResponse(200, response_payload))

    result = llm_client.enrich_job("Example job description")

    assert result["normalized_role"] is None
    assert "error" in result


def test_score_job_clamps_and_defaults(monkeypatch, mocker):
    _set_env(monkeypatch)
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "fit_score": 999,
                            "summary": "Summary text",
                            "pros": ["Strong stack"],
                            "cons": ["No onsite"],
                            "missing_skills": ["Go"],
                            "apply_recommendation": "unknown",
                        }
                    )
                }
            }
        ]
    }
    mocker.patch("app.llm_client.requests.post", return_value=FakeResponse(200, response_payload))

    result = llm_client.score_job({"title": "Role"}, {"skills": ["Python"]})

    assert result["fit_score"] == 100
    assert result["apply_recommendation"] == "maybe"
