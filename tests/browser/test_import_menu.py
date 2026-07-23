"""End-to-end tests for the staging list's show/hide of 100% matches.

The toggle is client-side JavaScript — a right-click menu plus a CSS class that
takes rows off screen — so the Flask-client route tests can't exercise it. Like
the other browser tests these drive a real Chromium against a real Flask server
and are opt-in:

    pip install -r requirements-browser.txt
    python -m playwright install chromium
    pytest tests/browser

The load-bearing guarantee here is that hiding is *visual only*: a hidden row
keeps its checkbox, and its checked state, so "Import selected" commits exactly
what it would have committed with the rows on screen.
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("playwright.sync_api")
from werkzeug.serving import make_server  # noqa: E402
from bson import ObjectId  # noqa: E402

from app import create_app  # noqa: E402
from app import routes_import  # noqa: E402
from tests.browser.test_swap import _Collection, _Client  # noqa: E402


class _DB:
    def __init__(self, jobs):
        self.jobs = _Collection(jobs)
        self.profiles = _Collection([])
        self.import_staging = _Collection([])
        self.keyword_groups = _Collection([])
        self.targets = _Collection([])
        self.target_suggestions = _Collection([])


def _seed():
    """One existing job, and three staged rows: two 100% matches and one new.

    The second 100% match is *closed*, so it arrives pre-selected — that is the
    row that proves hiding doesn't quietly drop work from the commit.
    """
    db = _DB(
        [
            {
                "_id": ObjectId(),
                "title": "Existing Role",
                "company": "Acme",
                "url": "https://example.com/jobs/existing",
                "state": "open",
            },
            {
                "_id": ObjectId(),
                "title": "Second Role",
                "company": "Beta",
                "url": "https://example.com/jobs/second",
                "state": "open",
            },
        ]
    )
    routes_import.stage_jobs(
        db,
        [
            # 100% on URL, open -> not pre-selected.
            {"title": "Existing Role", "company": "Acme",
             "url": "https://example.com/jobs/existing"},
            # 100% on URL, closed -> pre-selected (closes the existing job).
            {"title": "Second Role", "company": "Beta",
             "url": "https://example.com/jobs/second", "state": "closed"},
            # Brand new -> pre-selected, must stay visible.
            {"title": "Fresh Role", "company": "Startup",
             "url": "https://example.com/jobs/fresh"},
        ],
    )
    return db


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "")
    app = create_app()
    app.extensions["mongo_client"] = _Client(_seed())

    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _visible_titles(page):
    return page.eval_on_selector_all(
        "#staging-table tbody tr",
        "rows => rows.filter(r => r.offsetParent !== null)"
        ".map(r => r.querySelector('.row-title').textContent.trim())",
    )


def test_right_click_menu_toggles_the_full_matches(server, page):
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    # All three rows start on screen, and the menu is out of the way.
    assert sorted(_visible_titles(page)) == ["Existing Role", "Fresh Role", "Second Role"]
    assert page.is_hidden("#staging-menu")

    # Right-clicking the list summons the menu.
    page.click("#staging-list", button="right")
    page.wait_for_selector("#staging-menu:not([hidden])")
    assert "Hide 100% matches (2)" in page.inner_text("#staging-menu")

    # Choosing the option hides the two 100% matches and dismisses the menu.
    page.click('#staging-menu [data-action="toggle-full-matches"]')
    page.wait_for_selector("#staging-menu", state="hidden")
    assert _visible_titles(page) == ["Fresh Role"]

    # The option now offers the way back, and says so.
    page.click("#staging-list", button="right")
    page.wait_for_selector("#staging-menu:not([hidden])")
    assert "Show 100% matches (2)" in page.inner_text("#staging-menu")
    page.click('#staging-menu [data-action="toggle-full-matches"]')
    assert sorted(_visible_titles(page)) == ["Existing Role", "Fresh Role", "Second Role"]


def test_hiding_is_visual_only_and_leaves_the_commit_set_alone(server, page):
    """A hidden row keeps its checkbox and its ticked state.

    The closed 100% match is pre-selected because importing it closes the job it
    matches. Hiding it must not quietly cancel that.
    """
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    def checked_ids():
        return set(page.eval_on_selector_all(
            'input[name="select"]', "els => els.filter(e => e.checked).map(e => e.value)"
        ))

    before = checked_ids()
    assert len(before) == 2  # the closed 100% match and the new row

    page.click('.staging-tools [data-action="toggle-full-matches"]')
    assert _visible_titles(page) == ["Fresh Role"]

    # Same checkboxes, same values, still ticked — just not on screen.
    assert checked_ids() == before
    assert page.eval_on_selector_all('input[name="select"]', "els => els.length") == 3


def test_the_choice_survives_a_reload(server, page):
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    page.click('.staging-tools [data-action="toggle-full-matches"]')
    assert _visible_titles(page) == ["Fresh Role"]

    page.reload()
    page.wait_for_selector("#staging-table")

    assert _visible_titles(page) == ["Fresh Role"]
    assert "Show 100% matches (2)" in page.inner_text(".staging-tools")


def test_escape_dismisses_the_menu_without_toggling(server, page):
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    page.click("#staging-list", button="right")
    page.wait_for_selector("#staging-menu:not([hidden])")
    page.keyboard.press("Escape")

    page.wait_for_selector("#staging-menu", state="hidden")
    assert len(_visible_titles(page)) == 3  # nothing was hidden
