"""Shared fixtures for the opt-in browser tests.

The ``browser`` fixture is session-scoped, so it must live here rather than in
each test module: two modules each calling ``sync_playwright().start()`` in the
same process collides on the sync API's event loop. One shared session launches
Chromium once for the whole ``tests/browser`` run.

Playwright is imported lazily inside the fixture, not at module load: this
conftest is collected even when Playwright isn't installed (CI runs only
requirements-dev), and each browser test module skips itself via importorskip
before ever requesting these fixtures, so the lazy import never fires there.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

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
