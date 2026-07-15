"""Shared test helpers and fixtures."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import pytest
from bson import ObjectId

from app import create_app


class FakeUpdateResult:
    def __init__(self, upserted_id=None, matched_count: int = 0) -> None:
        self.upserted_id = upserted_id
        self.matched_count = matched_count


class FakeDeleteResult:
    def __init__(self, deleted_count: int = 0) -> None:
        self.deleted_count = deleted_count


def _get_nested(doc: Dict[str, Any], dotted_key: str) -> Any:
    current = doc
    for part in dotted_key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _regex_matches(target: Any, pattern: str, options: str) -> bool:
    """Mimic Mongo $regex: match a string, or any element of a list."""
    import re

    flags = re.IGNORECASE if "i" in options else 0
    compiled = re.compile(pattern, flags)
    if isinstance(target, list):
        return any(isinstance(item, str) and compiled.search(item) for item in target)
    if isinstance(target, str):
        return bool(compiled.search(target))
    return False


def _matches_filter(doc: Dict[str, Any], filter_doc: Dict[str, Any]) -> bool:
    for key, value in filter_doc.items():
        if key == "$or":
            if not any(_matches_filter(doc, sub) for sub in value):
                return False
        elif isinstance(value, dict):
            if "$gte" in value:
                target = _get_nested(doc, key)
                if target is None or target < value["$gte"]:
                    return False
            elif "$exists" in value:
                exists = _get_nested(doc, key) is not None
                if value["$exists"] != exists:
                    return False
            elif "$ne" in value:
                if _get_nested(doc, key) == value["$ne"]:
                    return False
            elif "$in" in value:
                # Mimic Mongo $in: the value matches, or (for a list field) any
                # element does. A missing field is None, so it only matches an
                # $in that lists None.
                target = _get_nested(doc, key)
                options = value["$in"]
                if isinstance(target, list):
                    if not any(item in options for item in target):
                        return False
                elif target not in options:
                    return False
            elif "$regex" in value:
                if not _regex_matches(
                    _get_nested(doc, key), value["$regex"], value.get("$options", "")
                ):
                    return False
            else:
                return False
        else:
            target = _get_nested(doc, key)
            if isinstance(target, list):
                if value not in target:
                    return False
            elif target != value:
                return False
    return True


class FakeCursor:
    def __init__(self, docs: List[Dict[str, Any]]) -> None:
        self._docs = docs
        self._skip = 0
        self._limit: Optional[int] = None

    def sort(self, sort_spec: Iterable) -> "FakeCursor":
        for key, direction in reversed(list(sort_spec)):
            self._docs.sort(
                key=lambda doc: _get_nested(doc, key) or 0,
                reverse=direction == -1,
            )
        return self

    def skip(self, count: int) -> "FakeCursor":
        self._skip = count
        return self

    def limit(self, count: int) -> "FakeCursor":
        self._limit = count
        return self

    def __iter__(self):
        start = self._skip
        end = None if self._limit is None else start + self._limit
        return iter(self._docs[start:end])


class FakeCollection:
    def __init__(self, docs: Optional[List[Dict[str, Any]]] = None) -> None:
        self._docs = docs or []

    def create_index(self, *args, **kwargs) -> None:
        return None

    def find(self, filter_doc: Optional[Dict[str, Any]] = None) -> FakeCursor:
        filter_doc = filter_doc or {}
        filtered = [doc for doc in self._docs if _matches_filter(doc, filter_doc)]
        return FakeCursor(filtered)

    def count_documents(self, filter_doc: Optional[Dict[str, Any]] = None) -> int:
        filter_doc = filter_doc or {}
        return sum(1 for doc in self._docs if _matches_filter(doc, filter_doc))

    def find_one(self, filter_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for doc in self._docs:
            if _matches_filter(doc, filter_doc):
                return doc
        return None

    def update_one(self, filter_doc: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> FakeUpdateResult:
        doc = self.find_one(filter_doc)
        if doc is None:
            if not upsert:
                return FakeUpdateResult(matched_count=0)
            doc = dict(filter_doc)
            if "_id" not in doc:
                doc["_id"] = ObjectId()
            self._docs.append(doc)
            if "$setOnInsert" in update:
                doc.update(update["$setOnInsert"])
            if "$set" in update:
                doc.update(update["$set"])
            return FakeUpdateResult(upserted_id=doc["_id"], matched_count=1)

        if "$set" in update:
            doc.update(update["$set"])
        return FakeUpdateResult(matched_count=1)

    def insert_one(self, doc: Dict[str, Any]) -> FakeUpdateResult:
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        self._docs.append(doc)
        return FakeUpdateResult(upserted_id=doc["_id"], matched_count=1)

    def insert_many(self, docs: List[Dict[str, Any]]) -> None:
        for doc in docs:
            self.insert_one(doc)

    def delete_one(self, filter_doc: Dict[str, Any]) -> FakeDeleteResult:
        for index, doc in enumerate(self._docs):
            if _matches_filter(doc, filter_doc):
                del self._docs[index]
                return FakeDeleteResult(deleted_count=1)
        return FakeDeleteResult(deleted_count=0)

    def delete_many(self, filter_doc: Dict[str, Any]) -> FakeDeleteResult:
        filter_doc = filter_doc or {}
        kept = [doc for doc in self._docs if not _matches_filter(doc, filter_doc)]
        removed = len(self._docs) - len(kept)
        self._docs[:] = kept
        return FakeDeleteResult(deleted_count=removed)


class FakeDB:
    def __init__(
        self,
        jobs: Optional[List[Dict[str, Any]]] = None,
        profiles: Optional[List[Dict[str, Any]]] = None,
        import_staging: Optional[List[Dict[str, Any]]] = None,
        keyword_groups: Optional[List[Dict[str, Any]]] = None,
        targets: Optional[List[Dict[str, Any]]] = None,
        target_suggestions: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.jobs = FakeCollection(jobs)
        self.profiles = FakeCollection(profiles)
        self.import_staging = FakeCollection(import_staging)
        self.keyword_groups = FakeCollection(keyword_groups)
        self.targets = FakeCollection(targets)
        self.target_suggestions = FakeCollection(target_suggestions)


class FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def app_client():
    app = create_app()
    app.testing = True
    return app.test_client()
