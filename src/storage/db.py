"""SQLite storage for the app tables (prototype stand-in for Cloud SQL Postgres, decision §8).

One file, WAL mode. A19: one user at a time on one machine, so no locking work beyond WAL.
LangGraph's `SqliteSaver` (plan 08) creates its own checkpoint tables in the same file.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, NamedTuple

from src.config import Settings
from src.errors import ConfigError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d+)_([A-Za-z0-9_]+)\.sql$")


class Migration(NamedTuple):
    version: int
    name: str
    filename: str
    sql: str


def now_utc() -> datetime:
    """The current time as an aware UTC datetime: the one clock of the store, the audit log and the purges."""
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """ISO 8601 in UTC, to the second. One format everywhere, so a text comparison of two times is a time comparison.

    A time with no zone is refused: it would be read as local time and shifted, and every stored time and cutoff after it would be
    off by the offset of the machine."""
    if moment.tzinfo is None:
        raise ValueError("a time without a time zone cannot be stored; use an aware UTC datetime")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc_now() -> str:
    return iso(now_utc())


def open_connection(path: Path) -> sqlite3.Connection:
    """A new connection with the app's settings: autocommit (callers run their own BEGIN/COMMIT), WAL, foreign keys. It belongs to
    the thread that opened it. (LangGraph's saver needs Python's default transactions and a connection it shares between threads,
    so `agent/checkpointer.py` opens its own.) A connection is closed again if a setting cannot be applied, so a failed open never
    leaves the file locked.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def new_conversation_id() -> str:
    return "conv-" + secrets.token_hex(4)


def turn_id(n: int) -> str:
    """Turn IDs are `t-<n>` inside a conversation."""
    return f"t-{n}"


class Database:
    def __init__(self, path: Path, migrations_dir: Path = MIGRATIONS_DIR) -> None:
        self.path = Path(path)
        self.migrations_dir = Path(migrations_dir)
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "Database":
        return cls(settings.db_path)

    # connection -----------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Autocommit, so `transaction()` controls BEGIN/COMMIT itself.
                conn = open_connection(self.path)
            except (OSError, sqlite3.Error) as exc:
                raise ConfigError(
                    f"The database could not be opened: {self.path} ({exc})",
                    fix="Check that the data folder exists and is writable, and that the file is a SQLite database.",
                ) from None
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on any exception. A nested call joins the outer transaction."""
        conn = self.conn
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    # migrations -----------------------------------------------------------
    def migrate(self) -> list[int]:
        """Apply new migrations in order. Returns the versions applied now (empty if up to date)."""
        conn = self.conn
        has_history = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        recorded = (
            {row["version"]: row["name"] for row in conn.execute("SELECT version, name FROM schema_migrations")}
            if has_history
            else {}
        )
        if not recorded:
            self._refuse_unmanaged_tables()  # before writing anything into a database we did not create
        if not has_history:
            conn.execute(
                "CREATE TABLE schema_migrations ("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )

        migrations = self._migration_files()
        self._check_history_matches(recorded, migrations)
        applied_now: list[int] = []
        for migration in migrations:
            if migration.version in recorded:
                continue
            # DDL is transactional in SQLite, so a failing script leaves nothing half-applied.
            record = (
                f"INSERT INTO schema_migrations (version, name, applied_at) "
                f"VALUES ({migration.version}, '{migration.name}', '{utc_now()}');"
            )
            try:
                conn.executescript(f"BEGIN;\n{migration.sql}\n{record}\nCOMMIT;")
            except sqlite3.Error as exc:
                if conn.in_transaction:
                    conn.rollback()
                raise ConfigError(
                    f"Database migration {migration.filename} failed: {exc}",
                    fix=f"Fix {migration.filename} in {self.migrations_dir}, or remove {self.path} to start fresh.",
                ) from None
            applied_now.append(migration.version)
        return applied_now

    def _check_history_matches(self, recorded: dict[int, str], migrations: list["Migration"]) -> None:
        """Every recorded migration must still be a file with the same number and name.

        Otherwise a number reused for a different migration would be treated as already applied and
        silently skipped, leaving the database without the tables the code expects.
        """
        by_version = {m.version: m for m in migrations}
        for version, name in sorted(recorded.items()):
            known = by_version.get(version)
            if known is None or known.name != name:
                found = f"{version:03d}_{known.name}.sql" if known else "no migration file with that number"
                raise ConfigError(
                    f"{self.path.name} was built with migration {version:03d}_{name}, but the code has {found}.",
                    fix=f"Use the code version that made this database, or move or delete {self.path.name} to start fresh.",
                )

    def _migration_files(self) -> list["Migration"]:
        found = []
        for file in sorted(self.migrations_dir.glob("*.sql")):
            match = _MIGRATION_NAME.match(file.name)
            if not match:
                raise ConfigError(f"Migration file name must look like 001_name.sql: {file.name}")
            found.append(Migration(int(match.group(1)), match.group(2), file.name, file.read_text(encoding="utf-8")))
        versions = [m.version for m in found]
        if len(versions) != len(set(versions)):
            raise ConfigError(f"Two migration files share a version number in {self.migrations_dir}.")
        return sorted(found, key=lambda m: m.version)

    def _refuse_unmanaged_tables(self) -> None:
        """A database with tables but no migration history came from something else. Never build on it."""
        tables = [
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "AND name <> 'schema_migrations'"
            )
        ]
        if tables:
            raise ConfigError(
                f"{self.path} has tables this app did not create ({', '.join(sorted(tables))}) "
                "and no migration history.",
                fix=f"Move or delete {self.path.name}. A new database is created on the next start.",
            )

    # conversations --------------------------------------------------------
    def create_conversation(self, user_id: str) -> str:
        conv_id = new_conversation_id()
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO conversations (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
                (conv_id, user_id, now, now),
            )
        return conv_id

    def get_conversation(self, conv_id: str, user_id: str) -> sqlite3.Row | None:
        """The conversation, only if it belongs to `user_id`. Someone else's ID looks like a missing one."""
        return self.conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (conv_id, user_id)
        ).fetchone()

    def next_turn(self, conv_id: str) -> int:
        """Count a new turn in the conversation and return its number (1, 2, ...).

        The count is stored, so turn IDs stay unique inside a conversation that is resumed later.
        """
        with self.transaction() as conn:
            updated = conn.execute(
                "UPDATE conversations SET turn_count = turn_count + 1, last_active_at = ? WHERE id = ?",
                (utc_now(), conv_id),
            ).rowcount
            if updated != 1:
                raise LookupError("conversation not found")
            return conn.execute("SELECT turn_count FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]
