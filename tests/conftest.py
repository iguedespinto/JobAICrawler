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


def _get_nested(doc: Dict[str, Any], dotted_key: str) -> Any:
    current = doc
    for part in dotted_key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _matches_filter(doc: Dict[str, Any], filter_doc: Dict[str, Any]) -> bool:
    for key, value in filter_doc.items():
        if isinstance(value, dict):
            if "$gte" in value:
                target = _get_nested(doc, key)
                if target is None or target < value["$gte"]:
                    return False
            elif "$exists" in value:
                exists = _get_nested(doc, key) is not None
                if value["$exists"] != exists:
                    return False
            else:
                return False
        else:
            if _get_nested(doc, key) != value:
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


class FakeDB:
    def __init__(self, jobs: Optional[List[Dict[str, Any]]] = None, profiles: Optional[List[Dict[str, Any]]] = None) -> None:
        self.jobs = FakeCollection(jobs)
        self.profiles = FakeCollection(profiles)


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
