"""The Golden Bucket in miniature (plan 10): solved questions the agent can learn from, and a glossary.

A **trio** is a question, the SQL that answered it and the analyst's report (`schema.py`). The production design keeps trios in
Cloud Storage and searches them with Postgres and pgvector; the prototype reads `golden/seed/*.yaml` and searches in memory
(`index.py`). The `Retriever` interface is the same, so a vector or hybrid retriever swaps in without touching the graph.

What is real here: the trio format, the status and brand rules (a trio of another brand never reaches a prompt, S5), the per-step
views, the checks that the Ingest Job would run (`validate.py`), and the retrieval. What is design only: the Cloud Storage bucket,
the Ingest Job, embeddings and hybrid search, the analyst queue and the nightly re-check (plan 11).
"""
