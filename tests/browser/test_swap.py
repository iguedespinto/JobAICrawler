"""End-to-end tests for in-place navigation (see app/static/js/nav.js).

These drive a real Chromium against a real Flask server, because the behaviour
under test is client-side JavaScript — a fetch-and-swap that the route tests,
which use the Flask test client, cannot exercise. They are opt-in and skip
themselves unless Playwright and a browser are installed:

    pip install -r requirements-browser.txt
    python -m playwright install chromium
    pytest tests/browser

So the default suite (and CI, which installs only requirements-dev.txt) stays
browser-free.

The reload check leans on a window sentinel: a value set on ``window`` survives
an in-place swap but is wiped by a full page reload, so its survival is the
proof that no reload happened.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta

import pytest

# Skip the whole module cleanly when Playwright isn't installed (e.g. in CI).
pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402
from bson import ObjectId  # noqa: E402

from app import create_app  # noqa: E402
from tests.conftest import FakeCollection, FakeUpdateResult, _matches_filter  # noqa: E402


class _Collection(FakeCollection):
    """A fake collection that behaves like pymongo where it matters here:
    ``find_one`` hands back a *copy*, so a route that mutates the returned
    document (``get_job`` pops ``_id``) can't corrupt the shared in-memory
    store, while ``update_one`` still edits the stored document in place.
    """

    def _find_live(self, filter_doc):
        for doc in self._docs:
            if _matches_filter(doc, filter_doc):
                return doc
        return None

    def find_one(self, filter_doc):
        live = self._find_live(filter_doc)
        return copy.deepcopy(live) if live is not None else None

    def update_one(self, filter_doc, update, upsert=False):
        doc = self._find_live(filter_doc)
        if doc is None:
            return FakeUpdateResult(matched_count=0)
        if "$set" in update:
            doc.update(update["$set"])
        return FakeUpdateResult(matched_count=1)


class _DB:
    """The handful of collections the exercised pages touch."""

    def __init__(self, jobs):
        self.jobs = _Collection(jobs)
        self.profiles = _Collection([])
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
    """Enough open jobs for the list to scroll, each with a long description so
    a detail page scrolls too. No ``url``, so no external iframe loads."""
    body = "".join(f"<p>Responsibility number {n} in this role.</p>" for n in range(40))
    base = datetime(2026, 1, 1)
    jobs = []
    for i in range(25):
        jobs.append(
            {
                "_id": ObjectId(),
                "title": f"Senior Engineer {i:02d}",
                "company": f"Company {i:02d}",
                "location": "Dublin",
                "url": "",
                "salary": "",
                "keywords": ["Python", "Flask"],
                "state": "open",
                "user_status": None,
                "description_html": body,
                "description_text": "Responsibilities.",
                "created_at": base + timedelta(days=i),
            }
        )
    return _DB(jobs)


@pytest.fixture()
def server(monkeypatch):
    """A real Flask app, wired to the fake DB, served on an ephemeral port."""
    # Empty so create_app never builds a real Mongo client; load_dotenv leaves an
    # already-present (even empty) env var alone, so .env can't refill it.
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


@pytest.fixture(scope="session")
def browser():
    playwright = sync_playwright().start()
    try:
        instance = playwright.chromium.launch()
    except Exception as exc:  # browser binary not installed
        playwright.stop()
        pytest.skip(f"Chromium unavailable ({exc}); run: python -m playwright install chromium")
    yield instance
    instance.close()
    playwright.stop()


@pytest.fixture()
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def _mark_and_probe(page, scroll_to):
    """Scroll, then plant a swap-detector and a reload-detector.

    ``.main__inner[data-pre]`` is the node a swap replaces — when it's gone, the
    swap has happened. ``window.__probe`` survives a swap but not a full reload.
    """
    page.evaluate(
        """(y) => {
            window.scrollTo(0, y);
            document.querySelector('.main__inner').dataset.pre = '1';
            window.__probe = 'kept';
        }""",
        scroll_to,
    )
    return page.evaluate("window.scrollY")


def _first_job_href(page):
    return page.get_attribute('.job a[href^="/jobs/"]', "href")


def test_action_button_keeps_scroll_without_reloading(server, page):
    """Saving a mark on a job stays exactly where you were, no reload."""
    page.goto(server + "/jobs")
    href = _first_job_href(page)
    page.goto(server + href)
    page.wait_for_selector('form[action*="/save"]')

    # Set the new value in place (no Playwright auto-scroll) and record state.
    page.evaluate(
        "() => { document.querySelector('form[action*=\\'/save\\'] select[name=user_status]').value = 'applied'; }"
    )
    y = _mark_and_probe(page, 300)

    # A real DOM click bubbles to nav.js without scrolling the button into view.
    page.eval_on_selector('form[action*="/save"] button[type=submit]', "b => b.click()")
    page.wait_for_function("() => !document.querySelector('.main__inner').dataset.pre")

    assert page.evaluate("window.__probe") == "kept"      # no full reload
    assert page.evaluate("window.scrollY") == y           # position kept
    assert page.eval_on_selector("select[name=user_status]", "el => el.value") == "applied"
    assert page.url.endswith(href)                        # same page


def test_filter_keeps_scroll_without_reloading(server, page):
    """A filter facet (same path) updates the list in place, scroll intact."""
    page.goto(server + "/jobs")
    y = _mark_and_probe(page, 250)
    assert y > 0

    # The list defaults to open, so "All" is the live facet link; the seed is all
    # open, so it swaps in the same jobs at the same height — scroll must hold.
    page.eval_on_selector('.facet a[href*="state=all"]', "a => a.click()")
    page.wait_for_function("() => !document.querySelector('.main__inner').dataset.pre")

    assert page.evaluate("window.__probe") == "kept"      # no full reload
    assert page.evaluate("window.scrollY") == y           # position kept
    assert "state=all" in page.url


def test_page_styles_travel_with_an_in_place_navigation(server, page):
    """A page's own <head> CSS must arrive with its content.

    Without it the destination renders wearing the previous page's styles —
    invisible on a plain page, disfiguring on the job edit form, whose labels
    fall back to inline and cram every field onto one line.
    """
    page.goto(server + "/jobs")
    page.eval_on_selector('.job a[href^="/jobs/"]', "a => a.click()")
    page.wait_for_function(r"() => /^\/jobs\/[a-f0-9]{24}$/.test(location.pathname)")

    # Styled by the detail page's own block; "inline" is the unstyled fallback.
    display = page.eval_on_selector(".edit-form label", "el => getComputedStyle(el).display")
    assert display == "block"


def test_opening_a_job_goes_to_the_top(server, page):
    """Following a link to a different page swaps in place but starts at the top."""
    page.goto(server + "/jobs")
    y = _mark_and_probe(page, 400)
    assert y > 0

    page.eval_on_selector('.job a[href^="/jobs/"]', "a => a.click()")
    page.wait_for_function(
        r"() => /^\/jobs\/[a-f0-9]{24}$/.test(location.pathname)"
    )

    assert page.evaluate("window.__probe") == "kept"      # in-place, not a reload
    assert page.evaluate("window.scrollY") == 0           # new page → top
