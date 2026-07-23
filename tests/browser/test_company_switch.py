"""End-to-end tests for the Similarity cell's suggestion/company switch.

Flipping the cell and hovering a company role are client-side behaviours the
Flask-client route tests can't exercise, so these drive a real Chromium against
a real Flask server. Opt-in like the other browser tests:

    pip install -r requirements-browser.txt
    python -m playwright install chromium
    pytest tests/browser

The point worth proving is that a role in the company list reveals the *same*
card the suggestion link does — both come from one Jinja macro, and these tests
hold that together.
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
    """Version 1 has three open roles and a closed one; Globex has just the one.

    The staged row matches Version 1's "AI Engineer", so the switch should offer
    the other two open roles — not the match, not the closed role.
    """
    db = _DB(
        [
            {"_id": ObjectId(), "title": "AI Engineer", "company": "Version 1",
             "url": "https://example.com/jobs/v1-ai", "location": "Dublin",
             "salary": "€45,000-€55,000", "keywords": ["LLM", "Python"],
             "description_text": "Build agentic systems.", "state": "open"},
            {"_id": ObjectId(), "title": "Data Engineer", "company": "Version 1",
             "url": "https://example.com/jobs/v1-data", "location": "Cork",
             "salary": "€60,000", "keywords": ["Spark"],
             "description_text": "Pipelines and warehousing.", "state": "open"},
            {"_id": ObjectId(), "title": "Platform Engineer", "company": "Version 1",
             "url": "https://example.com/jobs/v1-plat", "state": "open"},
            {"_id": ObjectId(), "title": "Retired Role", "company": "Version 1",
             "url": "https://example.com/jobs/v1-old", "state": "closed"},
            {"_id": ObjectId(), "title": "Solo Role", "company": "Globex",
             "url": "https://example.com/jobs/gx-1", "state": "open"},
        ]
    )
    routes_import.stage_jobs(
        db,
        [
            {"title": "AI Engineer", "company": "Version 1",
             "url": "https://example.com/jobs/v1-ai"},
            {"title": "Solo Role", "company": "Globex",
             "url": "https://example.com/jobs/gx-1"},
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


def _row(page, title):
    """The staging row whose title cell reads ``title``."""
    return page.locator("#staging-table tbody tr", has_text=title).first


def test_switch_flips_between_the_suggestion_and_the_company_roles(server, page):
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    row = _row(page, "AI Engineer")
    # The suggestion is what you see first.
    assert row.locator('[data-view="suggestion"]').is_visible()
    assert row.locator('[data-view="company"]').is_hidden()
    assert "AI Engineer @ Version 1" in row.locator('[data-view="suggestion"]').inner_text()

    row.locator(".sim-switch").click()

    # Now the company's other open roles — and only those.
    assert row.locator('[data-view="suggestion"]').is_hidden()
    company = row.locator('[data-view="company"]')
    assert company.is_visible()
    assert company.locator(".match-link").all_inner_texts() == [
        "Data Engineer", "Platform Engineer",
    ]
    assert "2 other open roles" in company.inner_text()

    # And back again.
    row.locator(".sim-switch").click()
    assert row.locator('[data-view="suggestion"]').is_visible()
    assert row.locator('[data-view="company"]').is_hidden()


def test_a_company_role_reveals_the_same_card_as_a_suggestion(server, page):
    """Both links go through the same delegated hover handler and the same macro."""
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    row = _row(page, "AI Engineer")

    # The card the suggestion link shows, for comparison.
    row.locator('[data-view="suggestion"] .match-link').hover()
    page.wait_for_selector(".match-card.is-shown")
    suggestion_fields = page.eval_on_selector(
        ".match-card.is-shown",
        "c => [c.querySelector('.match-card__title').tagName,"
        " !!c.querySelector('.match-card__company'),"
        " !!c.querySelector('.match-card__meta'),"
        " !!c.querySelector('.match-card__keywords'),"
        " !!c.querySelector('.match-card__desc')]",
    )

    row.locator(".sim-switch").click()
    row.locator('[data-view="company"] .match-link').first.hover()
    page.wait_for_selector(".match-card.is-shown")

    shown = page.eval_on_selector(
        ".match-card.is-shown",
        "c => ({title: c.querySelector('.match-card__title').textContent.trim(),"
        " href: c.querySelector('.match-card__title').getAttribute('href'),"
        " company: c.querySelector('.match-card__company').textContent.trim(),"
        " desc: c.querySelector('.match-card__desc').textContent.trim(),"
        " fields: [c.querySelector('.match-card__title').tagName,"
        "  !!c.querySelector('.match-card__company'),"
        "  !!c.querySelector('.match-card__meta'),"
        "  !!c.querySelector('.match-card__keywords'),"
        "  !!c.querySelector('.match-card__desc')]})",
    )

    # Same shape as the suggestion's card, filled with this role's details...
    assert shown["fields"] == suggestion_fields
    assert shown["title"] == "Data Engineer"
    assert shown["company"] == "Version 1"
    assert shown["desc"] == "Pipelines and warehousing."
    # ...and, like the suggestion, its title links through to the detail page.
    assert shown["href"].startswith("/jobs/")


def test_no_switch_when_the_company_has_no_other_open_role(server, page):
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    assert _row(page, "Solo Role").locator(".sim-switch").count() == 0
    assert _row(page, "AI Engineer").locator(".sim-switch").count() == 1


def test_each_row_switches_on_its_own(server, page):
    """Flipping one row leaves the rest showing their suggestion."""
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    _row(page, "AI Engineer").locator(".sim-switch").click()

    assert _row(page, "AI Engineer").locator('[data-view="company"]').is_visible()
    assert _row(page, "Solo Role").locator('[data-view="suggestion"]').is_visible()


def _crowded_seed():
    """A company with far more open roles than the list can show at once."""
    jobs = [
        {"_id": ObjectId(), "title": "AI Engineer", "company": "Version 1",
         "url": "https://example.com/jobs/v1-ai", "state": "open"},
    ]
    jobs += [
        {"_id": ObjectId(), "title": f"Role {n:02d}", "company": "Version 1",
         "url": f"https://example.com/jobs/v1-{n}", "state": "open"}
        for n in range(12)
    ]
    db = _DB(jobs)
    routes_import.stage_jobs(
        db,
        [{"title": "AI Engineer", "company": "Version 1",
          "url": "https://example.com/jobs/v1-ai"}],
    )
    return db


@pytest.fixture()
def crowded_server(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "")
    app = create_app()
    app.extensions["mongo_client"] = _Client(_crowded_seed())

    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_the_company_list_scrolls_rather_than_stretching_the_row(crowded_server, page):
    """Twelve roles must scroll inside the cell, not stretch the row to fit."""
    page.goto(crowded_server + "/import")
    page.wait_for_selector("#staging-table")

    row = _row(page, "AI Engineer")
    row.locator(".sim-switch").click()

    assert row.locator('[data-view="company"] .match-link').count() == 12

    box = page.eval_on_selector(
        '[data-view="company"] .company-list',
        "el => ({overflowY: getComputedStyle(el).overflowY,"
        " clientH: el.clientHeight, scrollH: el.scrollHeight})",
    )
    assert box["overflowY"] in ("auto", "scroll")
    assert box["scrollH"] > box["clientH"]      # there is genuinely more to reach

    # And it really moves.
    moved = page.eval_on_selector(
        '[data-view="company"] .company-list',
        "el => { el.scrollTop = 9999; return el.scrollTop; }",
    )
    assert moved > 0


def test_the_switch_keeps_clear_of_the_cell_content(server, page):
    """The switch is absolutely positioned, so the cell must reserve a gutter.

    ``table.data td`` sets the padding and outranks a bare ``.sim-cell`` rule,
    so the reservation has to be specific enough to win — otherwise the icon
    lands on top of the first line of text.
    """
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    row = _row(page, "AI Engineer")
    row.locator(".sim-switch").click()

    geometry = page.eval_on_selector(
        "td.sim-cell",
        "cell => ({switchRight: cell.querySelector('.sim-switch').getBoundingClientRect().right,"
        " headLeft: cell.querySelector('.company-view__head').getBoundingClientRect().left,"
        " linkLeft: cell.querySelector('.company-list .match-link').getBoundingClientRect().left})",
    )

    assert geometry["switchRight"] <= geometry["headLeft"]
    assert geometry["switchRight"] <= geometry["linkLeft"]


def test_the_company_roles_are_not_crowded_together(server, page):
    """Stacked links need room to read as separate rows."""
    page.goto(server + "/import")
    page.wait_for_selector("#staging-table")

    row = _row(page, "AI Engineer")
    row.locator(".sim-switch").click()

    links = page.eval_on_selector_all(
        '[data-view="company"] .company-list li',
        "els => els.map(el => el.getBoundingClientRect().top)",
    )
    assert len(links) >= 2
    # Consecutive roles sit clearly apart, not stacked line-on-line.
    spacing = links[1] - links[0]
    assert spacing >= 20, f"roles only {spacing}px apart"
