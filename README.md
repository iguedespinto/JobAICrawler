## Job AI Crawler

Personal Flask app for collecting and reviewing job opportunities, stored in
MongoDB Atlas. Opportunities are prepared externally and brought in through a
JSON import flow.

### Run locally

1. Create and activate a virtual environment.
2. Install dependencies:
   - App only: `pip install -r requirements.txt`
   - App + tests + MCP server (for development): `pip install -r requirements-dev.txt`
3. Export required env vars (see below).
4. Run the app:
   - `flask --app wsgi run`
5. Run the tests:
   - `pytest`

`requirements.txt` holds only what running the app needs; test tooling lives in
`requirements-dev.txt` so the two stay separable.

### Importing opportunities

1. Open `/import`.
2. Upload a JSON file. The file is an array of objects (a single object is also
   accepted) with these fields:
   - `name` (job title), `company`, `url`, `salary`, `description`, `keywords`,
     and an optional `state` of `open` (default) or `closed`.
3. Review the preview. Each opportunity is matched against what is already in the
   database: first by URL, then by title + company. New opportunities are
   pre-selected; matches are not.
4. Adjust the selection and click **Import selected** to create them.

Opportunities can be imported whether **open** or **closed** (missing `state`
counts as open):

- **Open + new** → created as an open opportunity.
- **Open + matched** → left as a match; not selected for import.
- **Closed + matched** (by URL or title + company) → the existing job is updated
  to `closed` (a status update); no new record is created.
- **Closed + no match** → imported as a `closed` record, kept for
  statistical/keyword analysis.

### Queuing URLs for processing

Bare job URLs can be queued for enrichment before they become importable
opportunities:

1. On `/import`, paste one URL per line into **Pending URLs** and click
   **Queue URLs**. Each URL is checked against active imported jobs and all
   staged records; only new, single-posting URLs are kept (search/results pages
   are rejected). They are stored as `unprocessed` records, hidden from the
   staging table below, and marked **Saved** (see below) — queuing a URL by hand
   counts as an expression of interest, and the mark rides through to the
   imported opportunity, putting it on your radar.
2. An MCP client retrieves them with `find_pending_urls`, validates each URL,
   confirms the job is still open, and writes a JSON import file with the full
   fields.
3. Importing that file (via the upload form or the MCP `import_file` tool) fills
   in the matching queued URL in place and promotes it to a viewable, importable
   opportunity — so it no longer appears in the pending queue.

Use **Clear pending URLs** to drop URLs that never produced a record (e.g. a job
that has since closed).

### Tracking opportunities

Each opportunity carries a single `user_status` marking your relationship to it:
**Saved** (`saved`), **Applied** (`applied`), or unset. The two are mutually
exclusive — setting one replaces the last — and are set from the job page or the
MCP `update_job_status` tool.

**On my radar** is not a mark of its own: it is the umbrella over both, meaning
you are tracking the opportunity at all. It exists only as a filter — the
**Status** row on `/jobs` offers *On my radar / Saved / Applied*, and the MCP
`find_jobs` tool takes the same values in its `user_status` argument, where
`radar` matches saved or applied.

A URL queued on `/import` arrives **Saved**, and so on your radar. The mark is
applied only when the opportunity is first created, so re-importing a job you
have since marked **Applied** never knocks it back.

### Environment variables

Required:
- `MONGODB_URI` - MongoDB Atlas connection string.

Optional:
- `MONGO_DB_NAME` - Database name (default: `jobs_db`).
- `SECRET_KEY` - Flask session signing key for flashed messages
  (default: a development value).

### Deployment

There is none: the app runs locally (see **Run locally**) against MongoDB
Atlas, so a merge to `main` ships nothing. CI runs the test suite and stops
there.

It used to deploy to Heroku on every green `main`. That app is gone — the last
successful deploy was 30 June 2026, and each of the 17 merges after it failed
authenticating against an app that no longer answers — so the deploy job has
been removed rather than left to cry wolf on every merge. `Procfile` and
`gunicorn.conf.py` are leftovers from that setup, kept in case it is ever
revived; nothing reads them today.

To bring hosting back, restore the deploy job in `.github/workflows/ci.yml`,
recreate the app with `MONGODB_URI` set, and add a fresh `HEROKU_API_KEY`
repository secret.
