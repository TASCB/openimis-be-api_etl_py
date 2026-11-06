"""Index the JSONB expression used by the sink's case-sensitive ORM lookup.

The existing normalized text index serves the SQL duplicate checks. It cannot
serve Django's JSONB -> lookup, so this small partial index complements it.
"""
from django.db import migrations

CREATE_INDEX = """
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_individual_external_id_exact
ON individual_individual (("Json_ext" -> 'external_id'))
WHERE "isDeleted" = false;
"""
DROP_INDEX = "DROP INDEX CONCURRENTLY IF EXISTS idx_individual_external_id_exact;"


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(CREATE_INDEX)
        schema_editor.execute("ANALYZE individual_individual;")


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(DROP_INDEX)


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('api_etl', '0005_location_code_index'), ('individual', '0001_initial')]
    operations = [migrations.RunPython(create_index, drop_index)]
