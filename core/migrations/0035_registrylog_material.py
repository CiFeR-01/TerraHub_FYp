"""
Add RegistryLog.material (FK -> Material, nullable).

This is wrapped in SeparateDatabaseAndState because an earlier, since-abandoned
branch already added a `material_id` column (and an orphan `product_id`) to
core_registrylog on some databases. The database_operations use IF NOT EXISTS so
this migration is a no-op where the column is already present and a real column
add on a clean database; the state_operations tell Django's migration state about
the field either way.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_alter_productionrun_status_productionrunyieldlog"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="registrylog",
                    name="material",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="registry_logs",
                        to="core.material",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "ALTER TABLE core_registrylog "
                        "ADD COLUMN IF NOT EXISTS material_id bigint NULL "
                        "REFERENCES core_material(id) DEFERRABLE INITIALLY DEFERRED; "
                        "CREATE INDEX IF NOT EXISTS core_registrylog_material_id_874044a7 "
                        "ON core_registrylog (material_id);"
                    ),
                    reverse_sql=(
                        "DROP INDEX IF EXISTS core_registrylog_material_id_874044a7; "
                        "ALTER TABLE core_registrylog DROP COLUMN IF EXISTS material_id;"
                    ),
                ),
            ],
        ),
    ]
