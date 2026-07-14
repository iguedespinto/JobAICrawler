"""Tests for managing target companies and roles."""

from __future__ import annotations

import pytest
from bson import ObjectId

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


def test_rename_target_updates_and_guards_duplicates():
    db = FakeDB(jobs=[], profiles=[])
    a = routes_targets.add_target(db, "company", "Acme")
    routes_targets.add_target(db, "company", "Globex")

    # Rename succeeds.
    result = routes_targets.rename_target(db, a["id"], "  Acme Corp ")
    assert result == {"found": True, "renamed": True, "duplicate": False,
                      "kind": "company", "name": "Acme Corp"}
    assert db.targets.find_one({"_id": ObjectId(a["id"])})["name"] == "Acme Corp"

    # Renaming onto an existing same-kind name is refused.
    clash = routes_targets.rename_target(db, a["id"], "globex")
    assert clash["renamed"] is False and clash["duplicate"] is True

    # Empty name raises.
    with pytest.raises(ValueError):
        routes_targets.rename_target(db, a["id"], "   ")


def test_edit_target_route(app_client, monkeypatch):
    db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_targets, "get_db", lambda: db)
    added = routes_targets.add_target(db, "role", "Dev")

    resp = app_client.post("/targets/edit", data={"id": added["id"], "name": "Senior Dev"})
    assert resp.status_code == 302
    assert db.targets.find_one({"name": "Senior Dev"}) is not None
    assert db.targets.find_one({"name": "Dev"}) is None


def test_add_suggestions_dedupes_and_validates():
    db = FakeDB(jobs=[], profiles=[])
    routes_targets.add_target(db, "company", "Google")  # already a target

    result = routes_targets.add_suggestions(
        db,
        [
            {"kind": "company", "name": "Stripe"},        # new
            {"kind": "search_site", "name": "Indeed"},    # new
            {"kind": "company", "name": "google"},        # already a target -> skip
            {"kind": "company", "name": "Stripe"},        # dup within batch -> skip
            {"kind": "bogus", "name": "X"},               # invalid kind
            {"kind": "role", "name": "   "},              # empty name
            "not-a-dict",                                  # invalid
        ],
    )
    assert result == {"added": 2, "skipped": 2, "invalid": 3}
    assert db.target_suggestions.count_documents({}) == 2

    # Re-suggesting an existing suggestion is skipped.
    again = routes_targets.add_suggestions(db, [{"kind": "company", "name": "STRIPE"}])
    assert again == {"added": 0, "skipped": 1, "invalid": 0}


def test_accept_suggestion_promotes_to_targets():
    db = FakeDB(jobs=[], profiles=[])
    routes_targets.add_suggestions(db, [{"kind": "search_site", "name": "Indeed"}])
    sug = db.target_suggestions.find_one({"name": "Indeed"})

    result = routes_targets.accept_suggestion(db, str(sug["_id"]))
    assert result == {"found": True, "kind": "search_site", "name": "Indeed", "added": True}
    # Moved out of suggestions and into the search sites target list.
    assert db.target_suggestions.count_documents({}) == 0
    assert [s["name"] for s in routes_targets.list_targets(db)["search_sites"]] == ["Indeed"]


def test_discard_suggestion():
    db = FakeDB(jobs=[], profiles=[])
    routes_targets.add_suggestions(db, [{"kind": "role", "name": "SRE"}])
    sug = db.target_suggestions.find_one({"name": "SRE"})

    assert routes_targets.discard_suggestion(db, str(sug["_id"])) is True
    assert db.target_suggestions.count_documents({}) == 0
    # No target was created.
    assert routes_targets.list_targets(db)["roles"] == []


def test_suggestion_routes_render_accept_discard(app_client, monkeypatch):
    db = FakeDB(jobs=[], profiles=[])
    monkeypatch.setattr(routes_targets, "get_db", lambda: db)
    routes_targets.add_suggestions(
        db,
        [{"kind": "company", "name": "Stripe"}, {"kind": "factor", "name": "Remote"}],
    )

    body = app_client.get("/targets").data.decode("utf-8")
    assert "Suggestions" in body and "Stripe" in body and "Remote" in body

    stripe = db.target_suggestions.find_one({"name": "Stripe"})
    assert app_client.post(
        "/targets/suggestions/accept", data={"id": str(stripe["_id"])}
    ).status_code == 302
    assert db.targets.find_one({"name": "Stripe"}) is not None
    assert db.target_suggestions.find_one({"name": "Stripe"}) is None

    remote = db.target_suggestions.find_one({"name": "Remote"})
    assert app_client.post(
        "/targets/suggestions/discard", data={"id": str(remote["_id"])}
    ).status_code == 302
    assert db.target_suggestions.count_documents({}) == 0
    assert db.targets.find_one({"name": "Remote"}) is None


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
