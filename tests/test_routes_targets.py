"""Tests for managing target companies and roles."""

from __future__ import annotations

import pytest

from app import routes_targets
from tests.conftest import FakeDB


def test_add_target_normalizes_and_dedupes():
    db = FakeDB(jobs=[], profiles=[])

    first = routes_targets.add_target(db, "company", "  Google  ")
    assert first["added"] is True
    assert first["kind"] == "company"
    assert first["name"] == "Google"  # trimmed

    # Case-insensitive duplicate (and plural kind) is skipped.
    dup = routes_targets.add_target(db, "companies", "google")
    assert dup["added"] is False
    assert db.targets.count_documents({}) == 1

    # Same name under a different kind is a distinct target.
    role = routes_targets.add_target(db, "role", "Google")
    assert role["added"] is True
    assert db.targets.count_documents({}) == 2


def test_add_target_validates_kind_and_name():
    db = FakeDB(jobs=[], profiles=[])
    with pytest.raises(ValueError):
        routes_targets.add_target(db, "bogus", "X")
    with pytest.raises(ValueError):
        routes_targets.add_target(db, "role", "   ")


def test_add_target_supports_all_kinds():
    db = FakeDB(jobs=[], profiles=[])
    # Friendly aliases normalise to the stored kind.
    assert routes_targets.add_target(db, "search site", "LinkedIn")["kind"] == "search_site"
    assert routes_targets.add_target(db, "sites", "Indeed")["kind"] == "search_site"
    assert routes_targets.add_target(db, "factors", "Remote only")["kind"] == "factor"


def test_list_targets_groups_and_sorts():
    db = FakeDB(jobs=[], profiles=[])
    routes_targets.add_target(db, "role", "Tech Lead")
    routes_targets.add_target(db, "role", "Developer")
    routes_targets.add_target(db, "company", "Stripe")
    routes_targets.add_target(db, "search_site", "LinkedIn")
    routes_targets.add_target(db, "factor", "Remote only")

    result = routes_targets.list_targets(db)
    assert set(result) == {"companies", "roles", "search_sites", "factors"}
    assert [r["name"] for r in result["roles"]] == ["Developer", "Tech Lead"]
    assert [c["name"] for c in result["companies"]] == ["Stripe"]
    assert [s["name"] for s in result["search_sites"]] == ["LinkedIn"]
    assert [f["name"] for f in result["factors"]] == ["Remote only"]
    assert all("id" in e for group in result.values() for e in group)


def test_remove_target():
    db = FakeDB(jobs=[], profiles=[])
    added = routes_targets.add_target(db, "company", "Acme")

    assert routes_targets.remove_target(db, added["id"]) is True
    assert db.targets.count_documents({}) == 0
    # Removing a now-missing (but valid) id returns False rather than raising.
    assert routes_targets.remove_target(db, added["id"]) is False


def test_targets_routes_add_render_delete(app_client, monkeypatch):
    db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_targets, "get_db", lambda: db)

    assert app_client.post(
        "/targets/add", data={"kind": "company", "name": "Google"}
    ).status_code == 302
    assert app_client.post(
        "/targets/add", data={"kind": "role", "name": "Engineering Manager"}
    ).status_code == 302
    assert app_client.post(
        "/targets/add", data={"kind": "search_site", "name": "LinkedIn"}
    ).status_code == 302
    assert app_client.post(
        "/targets/add", data={"kind": "factor", "name": "Remote only"}
    ).status_code == 302
    assert db.targets.count_documents({}) == 4

    body = app_client.get("/targets").data.decode("utf-8")
    assert "Google" in body and "Engineering Manager" in body
    assert "LinkedIn" in body and "Remote only" in body
    assert "Search sites" in body and "Other relevant factors" in body

    tid = str(db.targets.find_one({"name": "Google"})["_id"])
    assert app_client.post("/targets/delete", data={"id": tid}).status_code == 302
    assert db.targets.find_one({"name": "Google"}) is None
    assert db.targets.count_documents({}) == 3
