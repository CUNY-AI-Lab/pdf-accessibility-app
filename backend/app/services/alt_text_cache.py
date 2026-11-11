"""Bounded, process-local SQLite access for reusable crop descriptions only.

Entry transitions, guarded by _write_entry::
    absent -> fresh (store description)
    fresh -> fresh (access/store) | absent (eviction)
    expired -> fresh (replace description) | absent (prune)
    fresh -> expired (derived after 48h; reads never extend storage lifetime)

Malformed legacy storage times are expired misses. Exact-input identity, the
entry/text/file bounds and the disable switch apply to every cache operation.
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_CACHE_LOCK = threading.RLock()
MAX_ENTRIES = 2000
MAX_TEXT_BYTES = 4096
MAX_AGE_NS = 48 * 60 * 60 * 1_000_000_000


class CacheEntryState(Enum):
    ABSENT = "absent"
    FRESH = "fresh"
    EXPIRED = "expired"


def cache_entry_state(stored_at: Any, now: int, *, present: bool = True) -> CacheEntryState:
    if not present:
        return CacheEntryState.ABSENT
    # A legacy/malformed storage time is a cache miss, never a failed read.
    if not isinstance(stored_at, int) or stored_at <= now - MAX_AGE_NS:
        return CacheEntryState.EXPIRED
    return CacheEntryState.FRESH


_CACHE_TRANSITIONS = {
    CacheEntryState.ABSENT: {CacheEntryState.FRESH},
    CacheEntryState.FRESH: {CacheEntryState.FRESH, CacheEntryState.ABSENT},
    CacheEntryState.EXPIRED: {CacheEntryState.FRESH, CacheEntryState.ABSENT},
}


def guard_cache_transition(current: CacheEntryState, target: CacheEntryState) -> None:
    if target not in _CACHE_TRANSITIONS[current]:
        raise ValueError(f"Illegal alt-text cache transition: {current.value} -> {target.value}")


def usable_description(text: str) -> bool:
    text = text.strip()
    return bool(text) and not text.startswith("[") and text.lower().rstrip(".") not in {
        "decorative", "n/a", "none", "null", "unknown", "no description",
        "no description available", "no alt text", "no alt text available",
        "unable to describe", "could not determine figure semantics",
    }


def request_input_hash(payload: dict[str, Any]) -> str | None:
    """Hash the actual request, retaining every non-image field exactly.

    Inline images become digests, so page previews and any other visual
    context must also match. Remote image URLs cannot prove identical pixels;
    skip caching those requests. No prompt or image payload is stored.
    """
    def image_digests(value: Any) -> Any:
        if isinstance(value, list):
            return [image_digests(item) for item in value]
        if isinstance(value, dict):
            if value.get("type") == "image_url":
                image = value["image_url"]
                if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                    raise ValueError("Image URL is not a string")
                url = image["url"]
                if not url.startswith("data:image/"):
                    raise ValueError("Image bytes are not available for cache identity")
                return {**value, "image_url": {
                    **image, "url": {"sha256": hashlib.sha256(url.encode("utf-8")).hexdigest()},
                }}
            return {key: image_digests(item) for key, item in value.items()}
        return value

    try:
        serialized = json.dumps(image_digests(payload), sort_keys=True, ensure_ascii=True)
    except (KeyError, TypeError, ValueError):
        return None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AltTextCache:
    def __init__(self, path: Path, *, max_entries: int = MAX_ENTRIES):
        self.path = path
        self.max_entries = max(1, max_entries)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            # FULL reclaims pages after LRU eviction; the database itself cannot
            # grow beyond 16 MiB (SQLite's default 4096-byte pages).
            connection.execute("PRAGMA auto_vacuum = FULL")
            connection.execute("PRAGMA max_page_count = 4096")
            connection.execute("PRAGMA secure_delete = ON")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(descriptions)")}
            if columns and not {"input_hash", "stored_at"}.issubset(columns):
                # Entries without exact inputs or a storage time are unusable.
                connection.execute("DROP TABLE descriptions")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS descriptions ("
                "image_hash TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL, "
                "input_hash TEXT NOT NULL, "
                "description TEXT NOT NULL, stored_at INTEGER NOT NULL, accessed INTEGER NOT NULL, "
                "PRIMARY KEY (image_hash, model, prompt_version, input_hash))"
            )
            self._prune_expired(connection, time.time_ns())
            connection.commit()
            return connection
        except sqlite3.Error:
            connection.close()
            raise

    @staticmethod
    def _write_entry(
        connection: sqlite3.Connection, key: tuple[str, str, str, str],
        target: CacheEntryState, now: int, *, text: str | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT stored_at FROM descriptions WHERE image_hash = ? AND model = ? "
            "AND prompt_version = ? AND input_hash = ?", key,
        ).fetchone()
        current = cache_entry_state(row[0] if row else None, now, present=row is not None)
        guard_cache_transition(current, target)
        if target is CacheEntryState.ABSENT:
            connection.execute(
                "DELETE FROM descriptions WHERE image_hash = ? AND model = ? "
                "AND prompt_version = ? AND input_hash = ?", key,
            )
        elif text is not None:
            connection.execute("INSERT OR REPLACE INTO descriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (*key, text, now, now))
        elif current is CacheEntryState.FRESH:
            connection.execute(
                "UPDATE descriptions SET accessed = ? WHERE image_hash = ? AND model = ? "
                "AND prompt_version = ? AND input_hash = ?", (now, *key),
            )
        else:
            raise ValueError("A cache insertion requires a description")

    @classmethod
    def _prune_expired(cls, connection: sqlite3.Connection, now: int) -> None:
        keys = connection.execute(
            "SELECT image_hash, model, prompt_version, input_hash FROM descriptions "
            "WHERE stored_at <= ? OR typeof(stored_at) != 'integer'", (now - MAX_AGE_NS,),
        ).fetchall()
        for key in keys:
            cls._write_entry(connection, tuple(key), CacheEntryState.ABSENT, now)

    def get(self, image_hash: str, model: str, prompt_version: str, input_hash: str) -> str | None:
        try:
            with _CACHE_LOCK, closing(self._connect()) as connection, connection:
                now = time.time_ns()
                self._prune_expired(connection, now)
                key = (image_hash, model, prompt_version, input_hash)
                row = connection.execute(
                    "SELECT description FROM descriptions "
                    "WHERE image_hash = ? AND model = ? AND prompt_version = ? AND input_hash = ?", key,
                ).fetchone()
                if row and usable_description(row[0]):
                    self._write_entry(connection, key, CacheEntryState.FRESH, now)
                    return row[0]
        except (OSError, sqlite3.Error):
            logger.warning("Alt-text cache read unavailable", exc_info=True)
        return None

    def put(self, image_hash: str, model: str, prompt_version: str, input_hash: str, text: str) -> None:
        text = text.strip()
        if not usable_description(text) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            return
        try:
            with _CACHE_LOCK, closing(self._connect()) as connection, connection:
                now = time.time_ns()
                self._prune_expired(connection, now)
                key = (image_hash, model, prompt_version, input_hash)
                self._write_entry(connection, key, CacheEntryState.FRESH, now, text=text)
                evicted = connection.execute(
                    "SELECT image_hash, model, prompt_version, input_hash FROM descriptions "
                    "ORDER BY accessed DESC, rowid DESC LIMIT -1 OFFSET ?", (self.max_entries,),
                ).fetchall()
                for old_key in evicted:
                    self._write_entry(connection, tuple(old_key), CacheEntryState.ABSENT, now)

        except (OSError, sqlite3.Error):
            logger.warning("Alt-text cache write unavailable", exc_info=True)
