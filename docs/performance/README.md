# Survey Solutions import optimization

Deploy `feature/survey-solutions-import-optimization` from both repositories:

- [ETL module](https://github.com/TASCB/openimis-be-api_etl_py/tree/feature/survey-solutions-import-optimization), based on `feature/direct-survey-api`.
- [Individual module](https://github.com/TASCB/openimis-be-individual_py/tree/feature/survey-solutions-import-optimization), based on `tz-imf`.

The local ETL checkout is called `openimis-be-api_etl-direct`; its remote is
`openimis-be-api_etl_py`. The similarly named Survey Solutions repository is
not the runtime package containing these changes. Install both branches together.

## Code improvements

- Household batches default to 2,000 members (`sink_batch_size`). Explicit service
  batch size takes precedence; values <= 1 retain single-batch behavior.
  Interleaved households stay together, including households larger than the limit.
  Set `sink_group_aggregation_column` to `group_code`.
- Scoped PostgreSQL COPY accelerates staging; other imports and database vendors
  retain ORM staging. Existing validation and maker-checker boundaries remain.
  Failed synchronous batches roll back the whole synchronous import.
- Bulk household creation, linking, location alignment and summary updates retain
  history. Reporting synchronizes once after successful synchronous uploads;
  reporting failures are logged. Summaries include phase timings, which overlap.
- Full and partial approval SQL carries UUID/location mappings through candidate
  results and retrieves JSON by staging primary key, preserving validation,
  normalized duplicate checks, approved subsets, and original payloads.
- Active external-ID lookup uses the exact JSONB expression index below; promoted
  external IDs reach the update path. Modified services also preserve relationship
  mappings, group approval payload fixes, and flat/nested PMT reads.
- Current imports use ZIP exports only (`source_mode: export_api`). Direct API
  requests are rejected. Credentials and site configuration are not packaged.

## Database requirements and deployment

Install both module branches into the backend, migration runner and all workers.
For image installations rebuild with both modules. For development mounts cover
all ETL code plus `individual/services.py`, `individual/import_performance.py`,
`individual/relationship_roles.py`, `individual/workflows/utils.py`, and
`individual/workflows/individual_upload_valid.py`. A checkout alone does not
update installed packages. No frontend change is required by this optimization.

Review the plan in the configured backend environment:

```sh
python manage.py migrate --plan
python manage.py showmigrations api_etl individual
```

| Migration | Index | Purpose |
| --- | --- | --- |
| `api_etl.0005_location_code_index` (existing) | `ix_loc_code` | Location code lookup |
| `individual.0028_add_import_lookup_indexes` (existing) | `idx_individual_group_code` | Active household code lookup |
| `individual.0028_add_import_lookup_indexes` (existing) | `idx_individual_external_id_norm` | Normalized text duplicate matching |
| `api_etl.0006_individual_external_id_exact_index` (new) | `idx_individual_external_id_exact` | Case-sensitive JSONB sink lookup |

Apply the reviewed plan with `python manage.py migrate`. Inspect unrelated pending
migrations before executing. New migration 0006 creates its index concurrently
outside an atomic migration and runs ANALYZE; other vendors skip its operation.
Existing 0005 uses a regular index build that may block writes when first applied.
No new columns or data backfill are needed. Existing indexes do not replace 0006.

Verify the definitions and valid/ready status in PostgreSQL:

```sql
SELECT c.relname, i.indisvalid, i.indisready, pg_get_indexdef(i.indexrelid)
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE c.relname IN ('ix_loc_code', 'idx_individual_group_code',
                   'idx_individual_external_id_norm',
                   'idx_individual_external_id_exact');
```

`IF NOT EXISTS` cannot repair a same-named invalid or incorrectly defined index.
Resolve an interrupted concurrent build before retrying. Keep both normalized
text and exact JSONB indexes: their expressions serve different lookups.
Restart backend and workers after deployment and run `python manage.py check`.
Verify the next fresh district import's counts, approvals and phase timings.
Preparing these commits does not apply database changes.

For rollback restore both previous module versions together. The extra index can
remain safely. To remove it, review `python manage.py migrate api_etl 0005 --plan`
before executing that reverse migration. It drops the exact index concurrently
without deleting imported records.

## Recorded measurements

| Component | Before | After |
| --- | ---: | ---: |
| Linking 300 synthetic households / 900 members | 8.432 s, 13,810 queries | 0.622 s, 15 queries |
| Staging 45,328 synthetic members | 9.475 s | 4.428 s |
| Exact-ID lookup, same 1,999-row batch | 5.359 s | 0.055 s |
| SQL, same batch in temporary tables | 22.684 s | 1.383 s |

These earlier component measurements do not predict whole-district duration.
Synthetic full imports exclude downloads, PMT, approval creation and reporting
latency; temporary-table SQL does not reproduce foreign-key trigger costs.
See [import results](ETL_IMPORT_PERFORMANCE.md), [SQL results](ETL_SQL_OPTIMIZATION.md)
and the sanitized JSON summaries here. Historical reports describe their runtime
at measurement time; current source support is ZIP only. Benchmark scripts and
baseline snapshots referenced there remain workspace artifacts, not packaged tools.

## Branch preparation checks (2026-11-03)

Sixteen focused ETL tests passed (household batching, approval boundaries, service
and GraphQL). Five individual-module reporting-scope and PMT tests passed against
the running backend code. Django system checks passed during the ETL test run.
The broader existing `test_hq_source` suite had one failure in questionnaire
ordering (`test_list_questionnaires_success`); that test/source was not changed
by this commit. Database-backed linking regressions were not rerun during branch
preparation; the historical isolated SQL results above record earlier verification.
