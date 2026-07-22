"""SQLite-backed response cache with TTL, keyed on (source, method, args, patch).

Draft select gives the user roughly 30 seconds, so a warm cache is a hard
requirement. Every provider method wrapped with `@cached` is looked up by a key that
already includes the detected patch, so a patch bump is a natural cache miss rather
than requiring an explicit invalidation pass -- `SQLiteCache.prune_stale_patch` exists
purely for housekeeping (keeping the DB from growing unbounded across patches).

Thread-safe: opened with `check_same_thread=False` and guarded by a lock, because
`OpggProvider`'s prefetch path hits this from a thread pool (a cold `suggest()` call
against a ~170-champion live roster is otherwise ~170 sequential HTTP round-trips --
unacceptably slow for a live draft). SQLite's own file-level locking would otherwise
also risk "database is locked" errors under concurrent writes from Python threads.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

from pydantic import BaseModel

DEFAULT_DB_PATH = Path.home() / ".cache" / "draftiq" / "cache.sqlite3"

P = ParamSpec("P")
T = TypeVar("T")


class SQLiteCache:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    method TEXT NOT NULL,
                    patch TEXT NOT NULL,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    def get(self, cache_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM cache_entries WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                return None
            value, expires_at = row
            if expires_at < time.time():
                self._conn.execute("DELETE FROM cache_entries WHERE cache_key = ?", (cache_key,))
                self._conn.commit()
                return None
            return cast(str, value)

    def set(
        self,
        cache_key: str,
        source: str,
        method: str,
        patch: str,
        value: str,
        ttl_seconds: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cache_entries (cache_key, source, method, patch, value, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    value = excluded.value, expires_at = excluded.expires_at
                """,
                (cache_key, source, method, patch, value, time.time() + ttl_seconds),
            )
            self._conn.commit()

    def prune_stale_patch(self, source: str, current_patch: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cache_entries WHERE source = ? AND patch != ?", (source, current_patch)
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {
            "__model__": f"{type(value).__module__}.{type(value).__qualname__}",
            "data": value.model_dump(mode="json"),
        }
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict) and "__model__" in value:
        module_name, _, cls_name = value["__model__"].rpartition(".")
        module = importlib.import_module(module_name)
        cls = getattr(module, cls_name)
        return cls.model_validate(value["data"])
    if isinstance(value, list):
        return [_from_jsonable(v) for v in value]
    return value


def _make_key(
    source: str, method: str, patch: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> str:
    payload = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"{source}:{method}:{patch}:{digest}"


def cached(
    *, ttl_seconds: float = 86400.0, keyed_by_patch: bool = True
) -> Callable[[Callable[Concatenate[Any, P], T]], Callable[Concatenate[Any, P], T]]:
    """Decorator for StatsProvider methods.

    The decorated method must live on a class exposing `self._cache: SQLiteCache` and
    `self._source: str`, and (unless `keyed_by_patch=False`, used for `get_patch`
    itself) `self.get_patch()`. Return values must be JSON round-trippable: a pydantic
    BaseModel, a list of BaseModels, or a JSON primitive.
    """

    def decorator(func: Callable[Concatenate[Any, P], T]) -> Callable[Concatenate[Any, P], T]:
        @functools.wraps(func)
        def wrapper(self: Any, *args: P.args, **kwargs: P.kwargs) -> T:
            patch = self.get_patch() if keyed_by_patch else "*"
            key = _make_key(self._source, func.__name__, patch, args, kwargs)
            cached_raw = self._cache.get(key)
            if cached_raw is not None:
                return cast(T, _from_jsonable(json.loads(cached_raw)))
            result = func(self, *args, **kwargs)
            serialized = json.dumps(_to_jsonable(result))
            self._cache.set(key, self._source, func.__name__, patch, serialized, ttl_seconds)
            return result

        return cast(Callable[Concatenate[Any, P], T], wrapper)

    return decorator
