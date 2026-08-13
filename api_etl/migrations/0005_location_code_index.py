from django.db import migrations

# tblLocations is upstream (`location`) and has no index on LocationCode, which the
# import looks every record up by. See docs/error-fixes/22.
CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS ix_loc_code ON "tblLocations" ("LocationCode");
ANALYZE "tblLocations";
"""

DROP_INDEX = """
DROP INDEX IF EXISTS ix_loc_code;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("api_etl", "0004_hh_size_harvest"),
    ]

    operations = [
        migrations.RunSQL(CREATE_INDEX, DROP_INDEX),
    ]
