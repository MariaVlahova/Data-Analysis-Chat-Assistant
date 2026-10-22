# Plan 03: BigQuery access
Status: not started

**Goal:** Wrap `requirements/bq_client.py` (brought into the prototype package) so every query goes through a dry-run, a byte cap, a timeout, labels and a row cap with masked error logs, add schema lookup for questions about the data structure, and check the configured brand mapping against the real brand list at startup so a typo never silently produces an empty scope.
