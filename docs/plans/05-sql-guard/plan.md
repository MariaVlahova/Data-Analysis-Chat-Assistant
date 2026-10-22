# Plan 05: SQL Guard
Status: not started

**Goal:** Check every generated query on its sqlglot parse tree, against an approved table-and-column allow-list, so that only one SELECT or WITH runs, PII columns are blocked even through aliases, CONCAT or SELECT *, the user's brand filter is forced in, and a LIMIT is always added.
