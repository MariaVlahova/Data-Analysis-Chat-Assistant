"""Write the sample data as Parquet files, one per table (`local-data export`), so the same rows can be loaded into a real BigQuery
dataset with `bq load` and the app compared against BigQuery on data whose right answers are known.

Loading is the user's step, with their own project; nothing here talks to Google Cloud. Types carry over (BIGINT to INT64, DOUBLE to
FLOAT64, TIMESTAMPTZ to TIMESTAMP) except `users.user_geom`, which is text in the file and loads as STRING (a blocked column either way).
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from src.errors import ConfigError
from src.localdata import schema


def export_parquet(db_path: Path, out_dir: Path) -> dict[str, int]:
    """Write `<table>.parquet` for each table into `out_dir` (created if needed). Returns the rows written per table."""
    if not db_path.is_file():
        raise ConfigError(f"The local sample data was not found: {db_path}", fix="Run: python -m src.cli.main local-data build")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error:
        raise ConfigError(f"The local sample data could not be read: {db_path}", fix="Run: python -m src.cli.main local-data build") from None
    try:
        for table in schema.TABLE_NAMES:
            target = (out_dir / f"{table}.parquet").resolve().as_posix().replace("'", "''")  # COPY takes a literal, not a parameter
            con.execute(f"COPY \"{table}\" TO '{target}' (FORMAT PARQUET)")
            written[table] = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        con.close()
    return written
