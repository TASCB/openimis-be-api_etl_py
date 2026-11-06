The second SQL optimization is active for ZIP imports.

The latest Tunduru ZIP import loaded 19,214 households and 45,328 members in
839.516 seconds. Its SQL phase consumed 524.829 seconds. This change targets that
phase and the sink's external-ID lookup. It does not delete or reimport records.

| Same 1,999-row batch | Before | After |
| --- | ---: | ---: |
| Exact external-ID lookup | 5.359s | 0.055s |
| Full import SQL, temporary-table comparison | 22.684s | 1.383s |

The existing `idx_individual_external_id_norm` supports the normalized text
expression used by SQL duplicate checks. Django's sink lookup uses
`Json_ext -> 'external_id'`, which returns JSONB. Previously PostgreSQL scanned
all active entries and filtered their JSON; the old plan examined approximately
47,681 active individuals for one batch. The new partial B-tree index,
`idx_individual_external_id_exact`, matches that JSONB expression and the
`isDeleted = false` predicate. EXPLAIN ANALYZE confirms an index scan returning
1,999 matches. Lookup semantics remain case-sensitive; SQL duplicate matching
continues to normalize whitespace and case with the existing index.

`api_etl/migrations/0006_individual_external_id_exact_index.py` creates the
non-unique index concurrently and analyzes the table. The migration plan contained
only this migration, and it was applied successfully. PostgreSQL confirms the
index is valid and ready. The reverse operation drops it concurrently. Other
vendors skip this PostgreSQL-specific operation.

The SQL issue appeared only after reproducing staging cardinality and statistics
from an atomic live import. Committed statistics show no pending staging rows,
while a new batch introduces about 2,000 uncommitted rows. PostgreSQL severely
underestimates the candidate count and chooses repeated scans of its materialized
result. Carrying the full interview payload in this result makes those scans
expensive. The materialized candidate result now holds only staging UUID,
prospective individual UUID, and location ID. INSERT retrieves the original
payload from staging by primary key. Full and partial-approval queries retain
all original required-field, active-location, approved-subset, and normalized
duplicate predicates. The UUID mapping and all JSON fields are preserved.

The SQL measurements use a real imported batch in session-private temporary
tables. Other rows have synthetic names/payloads while retaining cardinality and
identifier lookup context. Each test rolls back, checks the temporary namespace
before executing writes, and prints counts and timing only. Foreign-key triggers
are not cloned by LIKE, so these component timings are not a whole-district
prediction. The latest import counts and live household/member records were not
changed. These tests make no Survey Solutions or reporting calls.

Verification: 36 focused tests passed. Additional temporary-table SQL regression
checks passed for full imports, partial approval, invalid-row holdback, normalized
duplicate linking without overwriting existing names, exact payload preservation,
and new-row UUID mapping. Backend and three idle workers were restarted. Django
system checks and runtime index/query verification passed.

Files:

- `openimis-be-individual_py/individual/workflows/individual_upload_valid.py`:
  lean candidate result and primary-key payload retrieval in both approval paths.
- `openimis-be-api_etl-direct/api_etl/migrations/0006_individual_external_id_exact_index.py`:
  concurrent exact-expression index and updated statistics.
- `docs/benchmark_etl_sql_batch.py`: reproducible private-table SQL comparison.
- `docs/test_etl_sql_temp_tables.py`: synthetic approval and validation regressions.
- `docs/etl-exact-id-plan-before.json` and `docs/etl-exact-id-plan-after.json`:
  sanitized query-plan summaries without interview content or identifiers.
- `docs/etl-sql-batch-benchmark.json` and `docs/etl-sql-regression-results.json`:
  measured results.

The existing dev mounts and Dockerfile include the modified workflow; the editable
ETL package includes the new migration automatically. No dependency installation,
image rebuild, frontend change, or unrelated migration was needed. Measure the
next fresh district import to confirm the resulting end-to-end improvement.
