"""
RegistryLog.quantity_changed is meant to always store a positive magnitude -
direction is derived from action_type by callers/templates, not the sign.
Every writer follows that convention except deduct_stock_from_allocation()
(core/utils.py) and the shipment force-close path (core/views.py), which
historically stored Outbound rows as a negative Decimal. Combined with
templates that already prefix Outbound/Consumed_For_Manufacturing/
Spoiled_Disposal rows with a literal "-", those rows rendered as a doubled
"--123.45" instead of "-123.45" (see templates/dashboard.html's
"Recent Registry Movements" table).

Both writers are fixed going forward in this same change; this migration
flips the sign on any already-existing negative rows so historical data
matches the positive-magnitude convention too.
"""
from django.db import migrations


def normalize_negative_quantities(apps, schema_editor):
    RegistryLog = apps.get_model("core", "RegistryLog")
    for log in RegistryLog.objects.filter(quantity_changed__lt=0):
        log.quantity_changed = -log.quantity_changed
        log.save(update_fields=["quantity_changed"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0051_shipment_credited_to_so"),
    ]

    operations = [
        migrations.RunPython(normalize_negative_quantities, migrations.RunPython.noop),
    ]
