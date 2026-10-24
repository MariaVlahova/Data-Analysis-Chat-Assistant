"""Generated sample data for running the whole app without Google Cloud (a development aid, not a test of BigQuery).

`schema.py` is the one description of the four tables, shared by the generator and by `data_access/local_bq.py`.
`generate.py` builds a DuckDB file of fake `thelook_ecommerce` data, the same every time for the same seed.
`reports.py` puts a few saved reports into the local app database, for trying the delete and undo flow.
`info.py` reads the file back and prints what is in it.

Nothing here imports the BigQuery library: `tests/test_no_direct_execute.py` keeps that inside `src/data_access/`.
Every person in the data is invented, and every e-mail ends in `@example.com`.
"""
