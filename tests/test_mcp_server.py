"""Tests for the MCP import server's core logic."""

from __future__ import annotations

import json

import pytest

from tests.conftest import FakeDB

mcp_server = pytest.importorskip("mcp_server")


def test_import_file_to_staging(tmp_path):
    payload = [
        {"name": "Role A", "company": "Acme", "url": "https://example.com/a"},
        {"name": "Role B", "company": "Beta", "url": "https://example.com/b"},
    ]
    file_path = tmp_path / "offers.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    db = FakeDB(jobs=[], profiles=[])
    result = mcp_server.import_file_to_staging(str(file_path), db=db)

    assert result["parsed"] == 2
    assert result["added"] == 2
    assert result["skipped"] == 0
    assert result["staged_total"] == 2
    assert db.import_staging.count_documents({}) == 2


def test_import_file_to_staging_dedupes_on_repeat(tmp_path):
    payload = [{"name": "Role A", "company": "Acme", "url": "https://example.com/a"}]
    file_path = tmp_path / "offers.json"
    file_path.write_text(json.dumps(payload), encoding="utf-8")

    db = FakeDB(jobs=[], profiles=[])
    mcp_server.import_file_to_staging(str(file_path), db=db)
    second = mcp_server.import_file_to_staging(str(file_path), db=db)

    assert second["added"] == 0
    assert second["skipped"] == 1
    assert db.import_staging.count_documents({}) == 1
