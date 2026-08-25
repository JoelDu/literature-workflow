import sqlite3

import pytest

from litreview.sqlite_runtime import Migration, apply_migrations, connect_sqlite


def test_write_policy_enables_wal_timeout_but_not_foreign_keys_by_default(tmp_path):
    path = tmp_path / "nested" / "db.sqlite"
    conn = connect_sqlite(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        conn.close()


def test_readonly_policy_does_not_create_or_mutate_database(tmp_path):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample(id INTEGER)")
    before = (path.stat().st_size, path.stat().st_mtime_ns)
    conn = connect_sqlite(str(path), readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO sample VALUES (1)")
    finally:
        conn.close()
    assert before == (path.stat().st_size, path.stat().st_mtime_ns)


def test_foreign_keys_are_opt_in(tmp_path):
    conn = connect_sqlite(str(tmp_path / "db.sqlite"), foreign_keys=True)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrations_are_ordered_and_idempotent(tmp_path):
    conn = connect_sqlite(str(tmp_path / "db.sqlite"))
    migrations = [
        Migration(2, "add value", lambda c: c.execute("ALTER TABLE item ADD COLUMN value TEXT")),
        Migration(1, "create item", lambda c: c.execute("CREATE TABLE item(id INTEGER PRIMARY KEY)")),
    ]
    try:
        assert apply_migrations(conn, "test", migrations) == [1, 2]
        assert apply_migrations(conn, "test", migrations) == []
        assert [row[1] for row in conn.execute("PRAGMA table_info(item)")] == ["id", "value"]
    finally:
        conn.close()

