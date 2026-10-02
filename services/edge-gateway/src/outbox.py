"""Persistent outgoing messages and local alarm state, using Python's SQLite library."""
import json
import sqlite3
import threading
from pathlib import Path


class Outbox:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                measured_at_ms INTEGER NOT NULL,
                topic TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")
            self.db.execute("CREATE INDEX IF NOT EXISTS outbox_time ON outbox(measured_at_ms)")
            self.db.execute("CREATE TABLE IF NOT EXISTS local_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def record(self, measured_at_ms, messages, state_key, state):
        """Commit telemetry, alert events and alarm state together before sending anything."""
        with self.lock, self.db:
            self.db.executemany(
                "INSERT INTO outbox(measured_at_ms, topic, payload) VALUES (?, ?, ?)",
                [(measured_at_ms, topic, payload) for topic, payload in messages],
            )
            self.db.execute(
                "INSERT INTO local_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (state_key, json.dumps(state)),
            )

    def state(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM local_state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else {}

    def oldest(self):
        with self.lock:
            return self.db.execute(
                "SELECT id, measured_at_ms, topic, payload FROM outbox ORDER BY id LIMIT 1"
            ).fetchone()

    def acknowledge(self, row_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM outbox WHERE id=?", (row_id,))

    def prune(self, cutoff_ms):
        """Expire unsent data outside the configured retention window; return the count."""
        with self.lock, self.db:
            return self.db.execute("DELETE FROM outbox WHERE measured_at_ms < ?", (cutoff_ms,)).rowcount

    def count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self):
        with self.lock:
            self.db.close()
