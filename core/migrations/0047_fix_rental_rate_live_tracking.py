"""
Corrects migration 0046's backfill under the (now superseded) design where every
batch permanently locked in a rate snapshot at receipt/backfill time.

The design changed: Batch.rental_rate_per_mt is now null = "track the warehouse's
current rental_cost_per_mt live", and only non-null when a batch has a genuine
per-PO negotiated rate. 0046 set a concrete snapshot value on every open batch
regardless, which now incorrectly freezes those batches at a stale rate forever -
e.g. editing a warehouse's standing rate stops showing up in its rent total for
any pre-existing stock, which reads as "the edit didn't save" even though it did.

For every currently-open batch: if its PurchaseOrder has a PurchaseOrderDetail
line for that material with a negotiated rate, keep/re-derive that exact value
(it's a genuine negotiated rate, not a stale snapshot). Otherwise null the field
out so it goes back to live-tracking the warehouse's rate.
"""
from django.db import migrations


def fix(apps, schema_editor):
    Batch = apps.get_model("core", "Batch")
    PurchaseOrderDetail = apps.get_model("core", "PurchaseOrderDetail")

    open_batches = Batch.objects.filter(status__in=["Active", "Quarantined"])

    to_update = []
    for b in open_batches:
        negotiated = None
        if b.purchase_order_id and b.material_id:
            detail = PurchaseOrderDetail.objects.filter(
                purchase_order_id=b.purchase_order_id,
                material_id=b.material_id,
                negotiated_rental_rate_per_mt__isnull=False,
            ).first()
            if detail:
                negotiated = detail.negotiated_rental_rate_per_mt

        if b.rental_rate_per_mt != negotiated:
            b.rental_rate_per_mt = negotiated
            to_update.append(b)

    if to_update:
        Batch.objects.bulk_update(to_update, ["rental_rate_per_mt"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0046_backfill_batch_rental_rate"),
    ]

    operations = [
        migrations.RunPython(fix, migrations.RunPython.noop),
    ]
