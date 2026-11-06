Database import optimizations are active for Survey Solutions ZIP imports.

Measured on 2026-10-13 in an isolated PostgreSQL database containing the application
schema and synthetic fixtures. No source household data was copied, and every fixture
transaction rolled back. The temporary database and application container were removed
after verification. Backend and three workers were restarted after checking that
all workers were idle; no production import, migration, dependency installation, or
container image rebuild was performed. The frontend import page remains available.

| Component | Before | After | Input |
| --- | ---: | ---: | --- |
| Household linking | 8.432s / 13,810 queries | 0.622s / 15 queries | 300 households, 900 members |
| Staging | 9.475s | 4.428s | 45,328 members |
| Optimized database import | Not measured on this synthetic full dataset | 57.008s | 19,214 households, 45,328 members, 23 batches |

These component comparisons use the same synthetic inputs before and after. The full
import includes staging, existing workflow header validation, SQL insertion, household
linking, and history. It excludes Survey Solutions downloads, CSV generation from the
adapter, PMT calculation, approval task creation, and external reporting indexing. The
full synthetic import uses fewer fields than real interviews and runs on an empty
reference schema. Its time is not a prediction or a direct comparison with the previous
Tunduru production run. Reporting batching is verified with unit tests; index-service
latency is not benchmarked. Rolled-back fixtures are vacuumed in the isolated database
before the full-size test to prevent previous test runs from distorting measurements.

Implementation:

- `openimis-be-api_etl-direct/api_etl/batching.py` groups interleaved rows by household
  and produces batches of up to 2,000 members without splitting a household. Missing
  identifiers stay independent; an oversized household stays together. The service's
  explicit batch size takes precedence over `sink_batch_size` configuration.
- `api_etl/services/survey_solution_service.py` opens a scoped optimized import inside
  the existing transaction, applies household batches, and stores phase timings in the
  import summary. Both source modes share this loading path.
- `api_etl/sinks/individual_import_sink.py` forwards configuration, collects timing data,
  and recognizes promoted external IDs so existing individuals reach the update path.
  Maker-checker still stages records and creates approval tasks without executing SQL.
- `openimis-be-individual_py/individual/import_performance.py` uses context variables
  to isolate concurrent imports, writes existing staging columns with PostgreSQL COPY,
  preserves model defaults and user audit fields, and synchronizes reporting once after
  all synchronous uploads succeed. Other backends and unscoped imports retain ORM
  staging. COPY errors propagate for rollback; reporting failures are logged.
- `individual/services.py` serializes the dataframe once, bulk creates households with
  history, bulk updates summaries with history, and loads household members in one
  query. It aligns members from older uploads when the group's location changes.
  UUID subqueries replace broad joins and DISTINCT over complete individual JSON.
  The existing single-group alignment interface remains usable by other callers.
- `individual/workflows/individual_upload_valid.py` maps candidate staging UUIDs to
  new individual UUIDs instead of joining whole JSON payloads. Original required-field,
  duplicate, active-location, and approved-subset predicates remain intact. Both full
  and partial-approval SQL paths use this mapping.
- `individual/workflows/utils.py` records header-validation and SQL execution timings.
  Existing normalized external-ID and active group-code indexes were verified in the
  database; no new indexes or migrations were needed.
- `openimis-dist_dkr/compose.dev.yml` mounts the five affected individual-module files
  into backend and workers. `openimis-be_py/Dockerfile` includes the same files in future
  builds. Runtime relationship mappings and group-task payload fixes were preserved
  because the installed individual module differed from the local checkout.

Verification: 36 focused unit tests passed (including existing source, location seed,
and service tests). Isolated SQL checks verified staging-only approval boundaries,
invalid-row holdback and PARTIAL_SUCCESS, updating individuals without duplicates,
renamed-member summaries, approved subsets, older members' location alignment, and
history creation. Full-size counts match expected households, members, and heads.
Django system checks passed. Runtime verification confirmed both sources use batch
size 2,000 and `group_code`, and UUID linking is loaded. Frontend `/front/imports`
returned HTTP 200.

Timings are nested: staging writes belong to staging, staging belongs to preparation,
SQL/linking belong to workflow, and aggregation belongs to linking. Do not add all
phase values. Raw results are in `etl-import-benchmark.json` and
`etl-import-edge-tests.json`. `benchmark_etl_import.py` and
`test_etl_import_edges.py` contain the synthetic fixtures and assertions; they require
an isolated schema-only database and the captured pre-change services at
`/tmp/baseline_services.py` in the disposable application container. They enforce the
`tasaf_etl_benchmark_` database prefix and never initiate an external API import.

Further SQL and external-ID lookup optimization was applied afterward. See
`ETL_SQL_OPTIMIZATION.md` for its measured batch improvements and verification.
