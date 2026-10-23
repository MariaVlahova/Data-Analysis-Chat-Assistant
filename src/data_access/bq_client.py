"""The one place a `bigquery.Client` is created. Only `SafeQueryRunner` uses it (plan 03, S2).

The original client (`requirements/bq_client.py`) also had `execute_query` and `get_table_schema`. Both are gone:
`execute_query` ran SQL with no dry run, byte cap, timeout, labels or row cap, and nothing needs either method now
that `SafeQueryRunner` runs queries and reads table metadata. With the method gone it cannot be called by mistake.
`tests/test_no_direct_execute.py` keeps it from coming back and keeps the client inside `src/data_access/`.
"""
import logging
from typing import Optional

from src import gcp_env  # noqa: F401 - must come before the Google imports: it shortens the credential probe
from google.cloud import bigquery

from src.errors import crash_summary

# A module logger under `src`, not the root logger: its lines reach the masked app.log (plan 02, S6).
# A BigQuery error message can quote SQL literals (an ID, a name) that no pattern masks, so the error lines
# log the exception class and call sites (`crash_summary`), never the message.
log = logging.getLogger(__name__)


class BigQueryRunner:
    """Holds the BigQuery client. The dataset comes from `bigquery.dataset` in the settings, read by `SafeQueryRunner`
    only, so there is one source for it."""

    def __init__(self, project_id: Optional[str] = None) -> None:
        """Initialize BigQuery client.

        Args:
            project_id: Google Cloud project ID. If None, uses default credentials.
        """
        log.info("Initializing BigQuery client")
        try:
            self.client = bigquery.Client(project=project_id)
            log.info("BigQuery client initialized")
        except Exception as e:
            log.error("Failed to initialize BigQuery client: %s", crash_summary(e))
            raise
