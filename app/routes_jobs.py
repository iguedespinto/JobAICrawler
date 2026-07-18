"""Routes for listing and retrieving jobs."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

import nh3
from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from . import get_db

# Rich-text descriptions are stored as sanitised HTML. Only these formatting
# tags/attributes survive; scripts, styles, event handlers and unknown tags are
# stripped so pasted content from arbitrary sites is safe to render back.
_ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li",
    "a", "h1", "h2", "h3", "h4", "blockquote", "code", "pre",
}
_ALLOWED_ATTRS = {"a": {"href", "title"}}
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_BOUNDARY_RE = re.compile(r"(?i)<\s*/?(br|/?p|/?li|/?h[1-6]|/?div|/?ul|/?ol)\s*/?>")

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs")

# The user's relationship to an opportunity, held in a single ``user_status``
# field. The values are mutually exclusive — setting one replaces the last — and
# unset means untriaged.
USER_STATUS_SAVED = "saved"
USER_STATUS_APPLIED = "applied"
USER_STATUSES = (USER_STATUS_SAVED, USER_STATUS_APPLIED)

# "On my radar" is NOT a stored value: it is the umbrella for every mark that
# means the user is tracking an opportunity at all, i.e. saved or applied. It
# exists only as a filter, so the list can answer "what am I watching?" without
# a third mark to keep in step. A URL queued on /import arrives saved, and so
# lands on the radar; see ``routes_import.stage_urls``.
RADAR_FILTER = "radar"
RADAR_STATUSES = (USER_STATUS_SAVED, USER_STATUS_APPLIED)

# The list's Status filter, in display order. Keyed by query-string value, which
# is why the radar umbrella sits alongside the stored marks.
STATUS_FILTERS = (RADAR_FILTER,) + USER_STATUSES
STATUS_FILTER_LABELS = {
    RADAR_FILTER: "On my radar",
    USER_STATUS_SAVED: "Saved",
    USER_STATUS_APPLIED: "Applied",
}

# Grouping the list by company. Grouped mode is ordered by company rather than
# by date — that is what puts a company's roles together — so it offers its own
# order: "recent" keeps the ungrouped list's newest-first feel at the group
# level, "name" is for finding a company you can name, "count" for seeing where
# the options are concentrated. Within a company, newest still wins.
GROUP_COMPANY = "company"
GROUP_ORDER_RECENT = "recent"
GROUP_ORDER_NAME = "name"
GROUP_ORDER_COUNT = "count"
GROUP_ORDERS = (GROUP_ORDER_RECENT, GROUP_ORDER_NAME, GROUP_ORDER_COUNT)
GROUP_ORDER_LABELS = {
    GROUP_ORDER_RECENT: "Most recent",
    GROUP_ORDER_NAME: "A→Z",
    GROUP_ORDER_COUNT: "Most jobs",
}

# Jobs with no company still have to land somewhere; they bucket together and
# sort last whatever order is chosen, being a gap in the data rather than an
# employer competing for the top of the list.
NO_COMPANY_LABEL = "(No company)"


def _parse_pagination() -> Tuple[int, int]:
    """Parse pagination params from the query string."""
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 20))
    except ValueError:
        per_page = 20
    page = max(page, 1)
    per_page = max(1, min(per_page, 100))
    return page, per_page


def _build_filters() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build Mongo filters and echo-able filter values."""
    filters: Dict[str, Any] = {}
    echo: Dict[str, Any] = {}

    # Single free-text search: case-insensitive substring match across the
    # title, keywords, and description. The term is escaped so special
    # characters (e.g. "C++", ".NET") are matched literally.
    query = request.args.get("q", "").strip()
    if query:
        regex = {"$regex": re.escape(query), "$options": "i"}
        filters["$or"] = [
            {"title": regex},
            {"keywords": regex},
            {"description_text": regex},
        ]
        echo["q"] = query

    # Free-text exclusion, the mirror of the search above: drop any posting that
    # mentions a term in its title, keywords, or description. Comma-separated so
    # several can be ruled out at once, and a job is excluded if it matches ANY
    # of them — the $nor spans every (field, term) pair. Terms are escaped so
    # special characters match literally, as with the search.
    exclude = request.args.get("exclude", "").strip()
    if exclude:
        terms = [part.strip() for part in exclude.split(",") if part.strip()]
        if terms:
            clauses = []
            for term in terms:
                regex = {"$regex": re.escape(term), "$options": "i"}
                clauses.append({"title": regex})
                clauses.append({"keywords": regex})
                clauses.append({"description_text": regex})
            filters["$nor"] = clauses
            echo["exclude"] = exclude

    # Exact (case-insensitive) keyword match against the keywords array, used by
    # the dashboard "Opportunities" links to show the jobs behind a count.
    # If the keyword belongs to a group, match any of the group's variants.
    keyword = request.args.get("keyword", "").strip()
    if keyword:
        from .routes_keywords import expand_keyword
        db = get_db()
        variants = expand_keyword(db, keyword) if db is not None else [keyword]
        if len(variants) > 1:
            pattern = "|".join(f"^{re.escape(v)}$" for v in variants)
        else:
            pattern = f"^{re.escape(keyword)}$"
        filters["keywords"] = {"$regex": pattern, "$options": "i"}
        echo["keyword"] = keyword

    # Exact (case-insensitive) company match, used by the company link on each
    # card to show every opportunity at that company. Anchored so a company is
    # not matched by another whose name merely contains it.
    company = request.args.get("company", "").strip()
    if company:
        filters["company"] = {"$regex": f"^{re.escape(company)}$", "$options": "i"}
        echo["company"] = company

    # All jobs by default; ?state=open or ?state=closed narrows to one state.
    # A job with no stored state counts as open.
    state = request.args.get("state", "").strip().lower()
    if state == "open":
        filters["state"] = {"$ne": "closed"}
        echo["state"] = "open"
    elif state == "closed":
        filters["state"] = "closed"
        echo["state"] = "closed"

    # Narrow by how the user marked the job. ``radar`` is the umbrella rather
    # than a stored value, so it matches every tracked mark. An unrecognised
    # value is ignored rather than matched literally, so a stray query parameter
    # shows the full list instead of silently emptying it.
    user_status = request.args.get("user_status", "").strip().lower()
    if user_status == RADAR_FILTER:
        filters["user_status"] = {"$in": list(RADAR_STATUSES)}
        echo["user_status"] = RADAR_FILTER
    elif user_status in USER_STATUSES:
        filters["user_status"] = user_status
        echo["user_status"] = user_status

    return filters, echo


def _get_db_or_error():
    """Return the database or a 503 response."""
    db = get_db()
    if db is None:
        return None, (jsonify({"error": "database not configured"}), 503)
    return db, None


def _parse_job_id(job_id: str):
    """Parse a job ObjectId or return an error response."""
    try:
        return ObjectId(job_id), None
    except InvalidId:
        return None, (jsonify({"error": "invalid job id"}), 400)


def _normalize_user_status(raw_value: str) -> Optional[str]:
    """Normalize user_status input into stored values."""
    normalized = raw_value.strip().lower()
    if normalized not in set(USER_STATUSES) | {"none"}:
        normalized = USER_STATUS_SAVED
    if normalized == "none":
        return None
    return normalized


def _format_date(value: Any) -> Optional[str]:
    """Format a stored datetime as YYYY-MM-DD, or None."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return None


def _company_key(value: Any) -> Tuple[int, str]:
    """Grouping key for a company: case/space-insensitive, blanks bucketed last.

    The leading flag is what sorts company-less jobs to the end: it leads every
    comparison, so it wins before the chosen order is even consulted.
    """
    name = " ".join(str(value or "").split()).lower()
    return (1, "") if not name else (0, name)


def _epoch(value: Any) -> float:
    """A stored datetime as a sortable number; missing dates sort oldest."""
    return value.timestamp() if isinstance(value, datetime) else 0.0


def _company_groups(docs: List[Dict[str, Any]]) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """Per-company display label, job count and newest timestamp."""
    groups: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for doc in docs:
        key = _company_key(doc.get("company"))
        group = groups.get(key)
        if group is None:
            label = str(doc.get("company") or "").strip() or NO_COMPANY_LABEL
            group = groups[key] = {"label": label, "count": 0, "newest": 0.0}
        group["count"] += 1
        group["newest"] = max(group["newest"], _epoch(doc.get("created_at")))
    return groups


def _company_ordered(
    db, filters: Dict[str, Any], order: str
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[int, str], Dict[str, Any]]]:
    """Every matching job, ordered so each company's jobs are contiguous.

    Grouping needs the whole result set: where a company belongs depends on all
    of its jobs — its newest, its size — which a single page cannot know. So this
    reads the matches and orders them in Python, as the dashboard already does
    for keywords, rather than growing an aggregation pipeline the test fake would
    then have to mimic. Fine at this list's size; revisit if it grows a lot.
    """
    docs = list(db.jobs.find(filters))
    groups = _company_groups(docs)

    # Stable sorts, least significant first: inside a company the newest job
    # wins (with the same _id tie-break the flat list needs), then each company
    # moves as a block to its place in the chosen order.
    docs.sort(key=lambda doc: str(doc.get("_id")), reverse=True)
    docs.sort(key=lambda doc: _epoch(doc.get("created_at")), reverse=True)

    if order == GROUP_ORDER_NAME:
        def rank(key):
            return (key[0], key[1])
    elif order == GROUP_ORDER_COUNT:
        def rank(key):
            return (key[0], -groups[key]["count"], key[1])
    else:
        def rank(key):
            return (key[0], -groups[key]["newest"], key[1])

    docs.sort(key=lambda doc: rank(_company_key(doc.get("company"))))
    return docs, groups


@jobs_bp.route("", methods=["GET"])
def list_jobs():
    """A page of jobs ordered by record creation date (newest first).

    A page is the unit the browser fetches as it scrolls. ``?partial=1`` returns
    that page's cards on their own, which the infinite-scroll script appends to
    the grid; without it the whole page is rendered around the first batch.
    """
    db, error = _get_db_or_error()
    if error:
        return error

    page, per_page = _parse_pagination()
    filters, echo = _build_filters()

    grouped = request.args.get("group", "").strip().lower() == GROUP_COMPANY
    group_order = request.args.get("group_order", "").strip().lower()
    if group_order not in GROUP_ORDERS:
        group_order = GROUP_ORDER_RECENT
    if grouped:
        # Echoed so every link and the scroll's next-page fetch stay grouped the
        # same way; appending cards ordered differently would be nonsense.
        echo["group"] = GROUP_COMPANY
        echo["group_order"] = group_order

    skip = (page - 1) * per_page
    previous: Optional[Dict[str, Any]] = None
    groups: Dict[Tuple[int, str], Dict[str, Any]] = {}

    if grouped:
        ordered, groups = _company_ordered(db, filters, group_order)
        total = len(ordered)
        window: Any = ordered[skip : skip + per_page]
        # The card just before this page decides whether the first one opens a
        # group: without it, a company split by a page boundary would announce
        # itself a second time in the batch the scroll appends.
        previous = ordered[skip - 1] if 0 < skip <= len(ordered) else None
    else:
        # Tie-break on _id so the ordering is a stable total order across pages.
        # A whole import batch shares one created_at (commit stamps a single
        # now), so sorting on that alone leaves tied jobs in an arbitrary order
        # and skip/limit can repeat one job on the next page while dropping
        # another entirely. The MCP server sorts the same way for the same reason.
        sort = [("created_at", -1), ("_id", -1)]
        window = db.jobs.find(filters).sort(sort).skip(skip).limit(per_page)
        total = db.jobs.count_documents(filters)

    total_pages = max(1, (total + per_page - 1) // per_page)

    jobs = []
    previous_key = _company_key(previous.get("company")) if previous else None
    for job in window:
        card = {
            "id": str(job["_id"]),
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "url": job.get("url"),
            "salary": job.get("salary"),
            "keywords": job.get("keywords", []),
            "status": job.get("status"),
            "user_status": job.get("user_status"),
            "state": job.get("state") or "open",
            "created_at": _format_date(job.get("created_at")),
        }
        if grouped:
            key = _company_key(job.get("company"))
            card["start_group"] = key != previous_key
            card["group_label"] = groups[key]["label"]
            card["group_count"] = groups[key]["count"]
            previous_key = key
        jobs.append(card)

    # The scroll script asks for one page's cards at a time; everything else on
    # the page (nav, filters, the script itself) would only be appended twice.
    if request.args.get("partial"):
        return render_template(
            "_job_cards.html",
            jobs=jobs,
            filters=echo,
            status_filter_labels=STATUS_FILTER_LABELS,
        )

    return render_template(
        "jobs_list.html",
        jobs=jobs,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        filters=echo,
        state_filter=echo.get("state", "all"),
        user_status_filter=echo.get("user_status", "all"),
        status_filters=STATUS_FILTERS,
        status_filter_labels=STATUS_FILTER_LABELS,
        grouped=grouped,
        group_order=group_order,
        group_orders=GROUP_ORDERS,
        group_order_labels=GROUP_ORDER_LABELS,
    )


@jobs_bp.route("/<job_id>", methods=["GET"])
def get_job(job_id: str):
    """Show a single job by MongoDB ObjectId."""
    db, error = _get_db_or_error()
    if error:
        return error

    oid, error = _parse_job_id(job_id)
    if error:
        return error

    job = db.jobs.find_one({"_id": oid})
    if job is None:
        return jsonify({"error": "job not found"}), 404

    job["id"] = str(job["_id"])
    job.pop("_id", None)

    return render_template("job_detail.html", job=job)


@jobs_bp.route("/<job_id>/save", methods=["POST"])
def save_job(job_id: str):
    """Mark a job as saved or applied."""
    db, error = _get_db_or_error()
    if error:
        return error

    oid, error = _parse_job_id(job_id)
    if error:
        return error

    update_value = _normalize_user_status(request.form.get("user_status", ""))

    result = db.jobs.update_one({"_id": oid}, {"$set": {"user_status": update_value}})
    if result.matched_count == 0:
        return jsonify({"error": "job not found"}), 404

    return redirect(url_for("jobs.get_job", job_id=job_id))


@jobs_bp.route("/<job_id>/state", methods=["POST"])
def set_job_state(job_id: str):
    """Set a job's state: close it (mark no longer open) or reopen it."""
    db, error = _get_db_or_error()
    if error:
        return error

    oid, error = _parse_job_id(job_id)
    if error:
        return error

    state = "closed" if request.form.get("state") == "closed" else "open"
    result = db.jobs.update_one({"_id": oid}, {"$set": {"state": state}})
    if result.matched_count == 0:
        return jsonify({"error": "job not found"}), 404

    return redirect(url_for("jobs.get_job", job_id=job_id))


def _parse_keywords(raw: str) -> List[str]:
    """Split a comma-separated keywords input into a clean list."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _sanitize_description_html(raw: str) -> str:
    """Sanitise pasted rich-text HTML down to a safe formatting allowlist."""
    cleaned = nh3.clean(raw or "", tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS)
    # Formatting-only markup (a lone <br>, empty <p>, …) carries no content.
    return cleaned if _html_to_text(cleaned) else ""


def _html_to_text(html: str) -> str:
    """Flatten HTML to plain text for search / import matching (no tags)."""
    if not html:
        return ""
    # Turn block boundaries into spaces so words/bullets don't run together.
    text = _BLOCK_BOUNDARY_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


@jobs_bp.route("/<job_id>/edit", methods=["POST"])
def edit_job(job_id: str):
    """Update an opportunity's editable fields. Works for any state.

    The description is rich text: the submitted HTML is sanitised and stored in
    ``description_html``; a tag-stripped copy is kept in ``description_text`` so
    search, matching and the MCP API stay plain-text.
    """
    db, error = _get_db_or_error()
    if error:
        return error

    oid, error = _parse_job_id(job_id)
    if error:
        return error

    description_html = _sanitize_description_html(request.form.get("description_html", ""))
    update: Dict[str, Any] = {
        "title": request.form.get("title", "").strip(),
        "company": request.form.get("company", "").strip(),
        "location": request.form.get("location", "").strip(),
        "url": request.form.get("url", "").strip(),
        "salary": request.form.get("salary", "").strip(),
        "description_html": description_html,
        "description_text": _html_to_text(description_html),
        "keywords": _parse_keywords(request.form.get("keywords", "")),
        "updated_at": datetime.now(timezone.utc),
    }
    result = db.jobs.update_one({"_id": oid}, {"$set": update})
    if result.matched_count == 0:
        return jsonify({"error": "job not found"}), 404

    return redirect(url_for("jobs.get_job", job_id=job_id))
