"""End-to-end tests for the profile skill board (see app/templates/profile.html).

The board's move-a-card behaviour is HTML5 drag-and-drop wired to a background
fetch — client-side JavaScript the Flask-client route tests can't exercise. Like
the in-place navigation tests, these drive a real Chromium against a real Flask
server and are opt-in, skipping themselves unless Playwright and a browser are
installed:

    pip install -r requirements-browser.txt
    python -m playwright install chromium
    pytest tests/browser

The no-reload check uses the same window sentinel as test_swap: a value set on
``window`` survives the drag's in-place DOM move but is wiped by a full reload,
so its survival proves the save didn't reload the page.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("playwright.sync_api")
from werkzeug.serving import make_server  # noqa: E402
from bson import ObjectId  # noqa: E402

from app import create_app  # noqa: E402
from tests.browser.test_swap import _Collection  # noqa: E402


class _DB:
    """Jobs to make cards, and a seeded default profile so the board's upsert
    lands on an existing document (the fake ``update_one`` doesn't insert)."""

    def __init__(self, jobs, profiles):
        self.jobs = _Collection(jobs)
        self.profiles = _Collection(profiles)
        self.import_staging = _Collection([])
        self.keyword_groups = _Collection([])
        self.targets = _Collection([])
        self.target_suggestions = _Collection([])


class _Client:
    def __init__(self, db):
        self._db = db

    def __getitem__(self, _name):
        return self._db


def _seed_db():
    jobs = [
        {"_id": ObjectId(), "title": "A", "keywords": ["Python", "Flask"], "state": "open"},
        {"_id": ObjectId(), "title": "B", "keywords": ["Python", "React"], "state": "open"},
    ]
    profiles = [{"_id": "default", "skill_categories": {}}]
    return _DB(jobs, profiles)


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "")
    app = create_app()
    app.extensions["mongo_client"] = _Client(_seed_db())

    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_dragging_a_card_moves_it_saves_it_and_does_not_reload(server, page):
    page.goto(server + "/profile")
    page.wait_for_selector('.skill-card[data-keyword="Python"]')

    # Python starts in the default column, carrying its open-job count of 2.
    assert page.eval_on_selector(
        '.skill-card[data-keyword="Python"]', "el => el.dataset.count"
    ) == "2"
    assert page.eval_on_selector(
        '[data-drop="not_categorised"] .skill-card[data-keyword="Python"]',
        "el => !!el",
    )

    # Plant the reload sentinel, then drag Python into the Strong column.
    page.evaluate("window.__probe = 'kept'")
    page.locator('.skill-card[data-keyword="Python"]').drag_to(
        page.locator('[data-drop="strong"]')
    )

    # The card lands in Strong and the column count follows it.
    page.wait_for_selector('[data-drop="strong"] .skill-card[data-keyword="Python"]')
    assert page.eval_on_selector(
        '.kanban-col--strong [data-col-count]', "el => el.textContent"
    ) == "1"
    # The save was a background fetch, not a reload.
    assert page.evaluate("window.__probe") == "kept"

    # It persisted: a full reload still finds Python in Strong.
    page.reload()
    page.wait_for_selector('.skill-card[data-keyword="Python"]')
    assert page.eval_on_selector(
        '[data-drop="strong"] .skill-card[data-keyword="Python"]', "el => !!el"
    )
    assert page.evaluate("window.__probe") is None  # the reload really happened
