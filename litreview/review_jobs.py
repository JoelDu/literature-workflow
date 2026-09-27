"""Durable Web review snapshots, independent of the read-only literature database.

One Web server process owns this registry, matching the in-process worker lifecycle.
"""
import json
import sqlite3
from pathlib import Path


class ReviewJobStore:
    def __init__(self, path):
        self.path = str(path)

    def _connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute('CREATE TABLE IF NOT EXISTS web_review_jobs '
                     '(job_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)')
        return conn

    def save(self, job):
        conn = self._connect()
        try:
            with conn:
                conn.execute('INSERT OR REPLACE INTO web_review_jobs VALUES (?, ?)',
                             (job['job_id'], json.dumps(job, ensure_ascii=False)))
        finally:
            conn.close()

    def load_all(self):
        conn = self._connect()
        try:
            return [json.loads(row[0]) for row in conn.execute('SELECT snapshot FROM web_review_jobs')]
        finally:
            conn.close()
