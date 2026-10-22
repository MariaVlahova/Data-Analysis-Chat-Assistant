import sqlite3

import pytest

from src.errors import ConfigError
from src.storage.db import MIGRATIONS_DIR, Database, turn_id


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "data" / "db.sqlite3")
    yield database
    database.close()


def tables(database: Database) -> set[str]:
    rows = database.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in rows}


def test_migrations_apply_once_and_are_idempotent(db):
    assert db.migrate() == [1]
    assert {"conversations", "schema_migrations"} <= tables(db)
    assert db.migrate() == []  # nothing new the second time
    rows = db.conn.execute("SELECT version, name FROM schema_migrations").fetchall()
    assert [(r["version"], r["name"]) for r in rows] == [(1, "conversations")]


def test_a_second_connection_sees_the_same_state(tmp_path):
    with Database(tmp_path / "db.sqlite3") as first:
        first.migrate()
        conv = first.create_conversation("alice")
    with Database(tmp_path / "db.sqlite3") as second:
        assert second.migrate() == []
        assert second.get_conversation(conv, "alice")["id"] == conv


def test_pragmas_and_row_factory(db):
    db.migrate()
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert isinstance(db.conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)


def test_a_failed_transaction_rolls_back(db):
    db.migrate()
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO conversations (id, user_id, created_at, last_active_at) VALUES ('conv-aaaa0000', 'alice', 'now', 'now')")
            raise RuntimeError("boom")
    assert db.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert not db.conn.in_transaction


def test_a_successful_transaction_commits(db):
    db.migrate()
    with db.transaction() as conn:
        conn.execute("INSERT INTO conversations (id, user_id, created_at, last_active_at) VALUES ('conv-aaaa0000', 'alice', 'now', 'now')")
    assert db.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


def test_nested_transaction_joins_the_outer_one(db):
    db.migrate()
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.create_conversation("alice")  # opens a nested transaction()
            raise RuntimeError("boom")
    assert db.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_a_failing_migration_leaves_nothing_behind(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_good.sql").write_text("CREATE TABLE good (id INTEGER);", encoding="utf-8")
    (migrations / "002_bad.sql").write_text("CREATE TABLE half (id INTEGER);\nCREATE TABLE half (id INTEGER);", encoding="utf-8")
    database = Database(tmp_path / "db.sqlite3", migrations_dir=migrations)
    try:
        with pytest.raises(ConfigError, match="002_bad"):
            database.migrate()
        assert "good" in tables(database)
        assert "half" not in tables(database)  # the whole migration rolled back
        versions = [r["version"] for r in database.conn.execute("SELECT version FROM schema_migrations")]
        assert versions == [1]
        assert not database.conn.in_transaction
    finally:
        database.close()


def test_later_migrations_apply_in_numeric_order(tmp_path):
    migrations = tmp_path / "m"
    migrations.mkdir()
    (migrations / "010_c.sql").write_text("CREATE TABLE c (x INTEGER);", encoding="utf-8")
    (migrations / "002_b.sql").write_text("CREATE TABLE b (x INTEGER);", encoding="utf-8")
    (migrations / "001_a.sql").write_text("CREATE TABLE a (x INTEGER);", encoding="utf-8")
    database = Database(tmp_path / "db.sqlite3", migrations_dir=migrations)
    try:
        assert database.migrate() == [1, 2, 10]
        # adding one more later only applies the new one
        (migrations / "011_d.sql").write_text("CREATE TABLE d (x INTEGER);", encoding="utf-8")
        assert database.migrate() == [11]
    finally:
        database.close()


def test_badly_named_migration_files_are_rejected(tmp_path):
    migrations = tmp_path / "m"
    migrations.mkdir()
    (migrations / "create_things.sql").write_text("SELECT 1;", encoding="utf-8")
    database = Database(tmp_path / "db.sqlite3", migrations_dir=migrations)
    try:
        with pytest.raises(ConfigError, match="001_name.sql"):
            database.migrate()
    finally:
        database.close()


def test_a_database_made_by_something_else_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "db.sqlite3"
    other = sqlite3.connect(path)
    other.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, username TEXT NOT NULL, started_at TEXT NOT NULL)")
    other.execute("INSERT INTO conversations VALUES ('c1', 'old', 'then')")
    other.commit()
    other.close()

    database = Database(path)
    try:
        with pytest.raises(ConfigError, match="did not create") as err:
            database.migrate()
        assert "Move or delete" in err.value.fix
    finally:
        database.close()
    check = sqlite3.connect(path)
    assert check.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    # nothing was added to the foreign database: same tables as before, no schema_migrations
    assert {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"conversations"}
    check.close()


def test_a_file_that_is_not_a_database_is_a_startup_error(tmp_path):
    path = tmp_path / "db.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 20)
    database = Database(path)
    try:
        with pytest.raises(ConfigError):
            database.migrate()
    finally:
        database.close()


def test_conversation_ids_and_ownership(db):
    db.migrate()
    conv = db.create_conversation("alice")
    assert conv.startswith("conv-") and len(conv) == len("conv-") + 8
    assert conv != db.create_conversation("alice")
    row = db.get_conversation(conv, "alice")
    assert row["user_id"] == "alice" and row["created_at"] == row["last_active_at"]
    # someone else's conversation looks the same as a missing one
    assert db.get_conversation(conv, "bob") is None
    assert db.get_conversation("conv-00000000", "alice") is None


def test_next_turn_counts_per_conversation_and_updates_last_active(db):
    db.migrate()
    first, second = db.create_conversation("alice"), db.create_conversation("alice")
    assert db.get_conversation(first, "alice")["turn_count"] == 0
    db.conn.execute("UPDATE conversations SET last_active_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (first,))

    assert [db.next_turn(first), db.next_turn(first), db.next_turn(second)] == [1, 2, 1]
    row = db.get_conversation(first, "alice")
    assert row["turn_count"] == 2 and row["last_active_at"] > "2000-01-01"


def test_the_turn_count_survives_a_restart(tmp_path):
    with Database(tmp_path / "db.sqlite3") as first:
        first.migrate()
        conv = first.create_conversation("alice")
        first.next_turn(conv)
        first.next_turn(conv)
    with Database(tmp_path / "db.sqlite3") as second:
        second.migrate()
        assert second.get_conversation(conv, "alice")["turn_count"] == 2
        assert second.next_turn(conv) == 3  # turn IDs never restart inside a conversation


def test_next_turn_for_an_unknown_conversation_changes_nothing(db):
    db.migrate()
    with pytest.raises(LookupError):
        db.next_turn("conv-00000000")
    assert not db.conn.in_transaction


def test_a_failed_open_does_not_leave_the_file_locked(tmp_path):
    path = tmp_path / "not-a-db.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 20)
    database = Database(path)
    with pytest.raises(ConfigError):
        database.conn
    path.unlink()  # on Windows this fails if the connection was left open


def test_turn_ids():
    assert turn_id(3) == "t-3"


def test_shipped_migration_files_are_numbered_from_one():
    names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    assert names[0] == "001_conversations.sql"


def test_001_creates_the_turn_counter_so_plan_08_can_keep_002(db):
    db.migrate()
    columns = [r["name"] for r in db.conn.execute("PRAGMA table_info(conversations)")]
    assert columns == ["id", "user_id", "created_at", "last_active_at", "turn_count"]
    assert [p.name for p in MIGRATIONS_DIR.glob("*.sql")] == ["001_conversations.sql"]


def make_migrations(folder, **files):
    folder.mkdir()
    for name, sql in files.items():
        (folder / f"{name}.sql").write_text(sql, encoding="utf-8")
    return folder


def test_a_reused_migration_number_is_refused_instead_of_silently_skipped(tmp_path):
    old = make_migrations(tmp_path / "old", **{"001_a": "CREATE TABLE a (x);", "002_turns": "CREATE TABLE turns (x);"})
    with Database(tmp_path / "db.sqlite3", migrations_dir=old) as database:
        assert database.migrate() == [1, 2]

    # later the number 002 is used for a different migration
    new = make_migrations(tmp_path / "new", **{"001_a": "CREATE TABLE a (x);", "002_reports": "CREATE TABLE reports (x);"})
    with Database(tmp_path / "db.sqlite3", migrations_dir=new) as database:
        with pytest.raises(ConfigError, match="002_turns.*002_reports") as err:
            database.migrate()
        assert "reports" not in tables(database)  # nothing was applied
        assert "move or delete" in err.value.fix


def test_a_database_newer_than_the_code_is_refused(tmp_path):
    full = make_migrations(tmp_path / "full", **{"001_a": "CREATE TABLE a (x);", "002_b": "CREATE TABLE b (x);"})
    with Database(tmp_path / "db.sqlite3", migrations_dir=full) as database:
        database.migrate()
    fewer = make_migrations(tmp_path / "fewer", **{"001_a": "CREATE TABLE a (x);"})
    with Database(tmp_path / "db.sqlite3", migrations_dir=fewer) as database:
        with pytest.raises(ConfigError, match="002_b.*no migration file with that number"):
            database.migrate()
