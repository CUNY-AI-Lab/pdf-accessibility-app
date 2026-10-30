"""Bounded, process-local SQLite access for reusable crop descriptions only."""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_CACHE_LOCK = threading.RLock()
MAX_ENTRIES = 2000
MAX_TEXT_BYTES = 4096
MAX_AGE_NS = 48 * 60 * 60 * 1_000_000_000


def usable_description(text: str) -> bool:
    text = text.strip()
    return bool(text) and not text.startswith("[") and text.lower() != "decorative"


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
    def _prune_expired(connection: sqlite3.Connection, now: int) -> None:
        connection.execute("DELETE FROM descriptions WHERE stored_at <= ?", (now - MAX_AGE_NS,))

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
                    connection.execute(
                        "UPDATE descriptions SET accessed = ? "
                        "WHERE image_hash = ? AND model = ? AND prompt_version = ? AND input_hash = ?",
                        (now, *key),
                    )
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
                connection.execute(
                    "INSERT OR REPLACE INTO descriptions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (image_hash, model, prompt_version, input_hash, text, now, now),
                )
                connection.execute(
                    "DELETE FROM descriptions WHERE rowid IN "
                    "(SELECT rowid FROM descriptions ORDER BY accessed DESC, rowid DESC "
                    "LIMIT -1 OFFSET ?)", (self.max_entries,),
                )
        except (OSError, sqlite3.Error):
            logger.warning("Alt-text cache write unavailable", exc_info=True)
