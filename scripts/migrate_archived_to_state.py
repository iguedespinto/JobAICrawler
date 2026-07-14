"""One-off migration: collapse the ``archived`` flag into ``state``.

The app now tracks a single ``state`` field (``open`` / ``closed``) and no
longer uses ``archived``. This backfills existing jobs:

- ``archived == True``  -> ``state = "closed"`` (archived meant inactive), and
  the ``archived`` field is removed.
- any job still missing ``state`` -> ``state = "open"`` (the default).
- any leftover ``archived`` field (e.g. ``archived: False``) is removed.

Run a dry run first (prints what would change), then apply:

    python scripts/migrate_archived_to_state.py            # dry run
    python scripts/migrate_archived_to_state.py --apply    # perform the writes

Uses the same MONGODB_URI / MONGO_DB_NAME env vars as the app.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient


def _get_db():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    uri = os.getenv("MONGODB_URI", "") or os.getenv("MONGO_URI", "")
    if not uri:
        raise SystemExit("MONGODB_URI is not set.")
    db_name = os.getenv("MONGO_DB_NAME", "jobs_db")
    return MongoClient(uri, serverSelectionTimeoutMS=8000)[db_name]


def main(apply: bool) -> None:
    jobs = _get_db().jobs

    archived = jobs.count_documents({"archived": True})
    missing_state = jobs.count_documents(
        {"archived": {"$ne": True}, "state": {"$exists": False}}
    )
    has_archived_field = jobs.count_documents({"archived": {"$exists": True}})

    print(f"jobs archived=True            -> state=closed : {archived}")
    print(f"non-archived missing state    -> state=open   : {missing_state}")
    print(f"jobs carrying an archived field (to unset)    : {has_archived_field}")

    if not apply:
        print("\nDRY RUN — no changes written. Re-run with --apply to migrate.")
        return

    # 1. Archived jobs become closed (archived dominated their state).
    jobs.update_many({"archived": True}, {"$set": {"state": "closed"}})
    # 2. Everything still without a state is open.
    jobs.update_many({"state": {"$exists": False}}, {"$set": {"state": "open"}})
    # 3. Drop the archived field everywhere.
    jobs.update_many({"archived": {"$exists": True}}, {"$unset": {"archived": ""}})

    total = jobs.count_documents({})
    open_n = jobs.count_documents({"state": "open"})
    closed_n = jobs.count_documents({"state": "closed"})
    leftover = jobs.count_documents({"archived": {"$exists": True}})
    no_state = jobs.count_documents({"state": {"$exists": False}})
    print(
        f"\nDone. total={total} open={open_n} closed={closed_n} "
        f"archived-field-leftover={leftover} missing-state={no_state}"
    )


if __name__ == "__main__":
    main(apply="--apply" in sys.argv[1:])
