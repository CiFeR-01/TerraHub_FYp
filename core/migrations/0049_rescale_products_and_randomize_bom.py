"""
One-time correction: every Product moves from "1 unit = 1 MT" (or, for the
one broken row PROD-2007, a nonsensical 50 MT/unit under a 'kg' label) to a
consistent "1 unit = 50kg = 0.05 MT" convention across the whole catalog.

Because Batch/SalesOrderDetail/etc. don't store a frozen weight_mt_per_unit
per row (weight is always looked up live from Product), simply changing
Product.weight_mt_per_unit would silently change what every existing
quantity number *means* - a batch showing "321.60 units" implicitly meant
321.60 MT before, and would mean 16.08 MT after, with nothing else in the
system telling you that happened.

For every product that was on the old 1.0 MT/unit scale (everything except
PROD-2007, which was never on a coherent scale to begin with), every related
quantity field is multiplied by 20x (1.0 / 0.05) so the *real* physical
quantities represented stay the same, just re-expressed in the new smaller
unit. This reaches real, currently-open Sales Orders with live
StockAllocation rows (not just seeded/test data) - confirmed with the user
before writing this migration.

PROD-2007 specifically is NOT rescaled: its existing quantity numbers were
recorded under a broken weight_mt_per_unit=50 and never represented anything
coherent, so there is no "real" quantity to preserve. Its weight is simply
corrected to 0.05; its already-recorded batch/order quantities become
meaningful (if modest) for the first time.

ProductRecipe (the BOM) is NOT rescaled here - it's fully regenerated with
new, randomized-but-mass-balanced quantities sized for the new 50kg unit,
for every product including PROD-2007 (see randomize_bom() below).

NOT touched: RegistryLog.quantity_changed for product-related entries.
RegistryLog only has a `material` FK (null for product/finished-goods
movements - see core/models.py's comment on that field), so there's no
reliable FK-based way to identify which historical log rows reference an
affected product without fragile item_name text matching. This is a
display-only audit trail (nothing recomputes capacity/rent from it), so a
historical log line reading "produced 40" next to a batch that now reads
"800" is a cosmetic inconsistency in old log text, not a correctness bug -
same category of accepted gap as migration 0046 not backfilling
closed_date for pre-existing zero-quantity batches.
"""
import random
from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations

NEW_WEIGHT = Decimal("0.0500")
SCALE = Decimal("20")
Q2 = Decimal("0.01")


def _scale(value):
    if value is None:
        return None
    return (Decimal(value) * SCALE).quantize(Q2, rounding=ROUND_HALF_UP)


def rescale_and_randomize(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    Batch = apps.get_model("core", "Batch")
    StockAllocation = apps.get_model("core", "StockAllocation")
    SalesOrderDetail = apps.get_model("core", "SalesOrderDetail")
    ShipmentItem = apps.get_model("core", "ShipmentItem")
    ShipmentItemReceipt = apps.get_model("core", "ShipmentItemReceipt")
    ProductionRun = apps.get_model("core", "ProductionRun")
    ProductionRunYieldLog = apps.get_model("core", "ProductionRunYieldLog")
    ProductRecipe = apps.get_model("core", "ProductRecipe")

    products = list(Product.objects.all())
    affected_ids = [p.id for p in products if p.weight_mt_per_unit == Decimal("1.0000")]

    # --- rescale every quantity field that references an affected product ---
    for b in Batch.objects.filter(product_id__in=affected_ids):
        b.quantity = _scale(b.quantity)
        b.allocated_quantity = _scale(b.allocated_quantity)
        b.save(update_fields=["quantity", "allocated_quantity"])

    for sa in StockAllocation.objects.filter(batch__product_id__in=affected_ids):
        sa.quantity = _scale(sa.quantity)
        sa.save(update_fields=["quantity"])

    for sod in SalesOrderDetail.objects.filter(product_id__in=affected_ids):
        sod.quantity_ordered = _scale(sod.quantity_ordered)
        sod.quantity_shipped = _scale(sod.quantity_shipped)
        sod.save(update_fields=["quantity_ordered", "quantity_shipped"])

    for si in ShipmentItem.objects.filter(product_id__in=affected_ids):
        si.quantity = _scale(si.quantity)
        si.received_quantity = _scale(si.received_quantity)
        si.save(update_fields=["quantity", "received_quantity"])

    for sir in ShipmentItemReceipt.objects.filter(shipment_item__product_id__in=affected_ids):
        sir.quantity = _scale(sir.quantity)
        sir.save(update_fields=["quantity"])

    for run in ProductionRun.objects.filter(target_product_id__in=affected_ids):
        run.expected_yield = _scale(run.expected_yield)
        run.actual_yield = _scale(run.actual_yield)
        run.save(update_fields=["expected_yield", "actual_yield"])

    for yl in ProductionRunYieldLog.objects.filter(production_run__target_product_id__in=affected_ids):
        yl.quantity = _scale(yl.quantity)
        yl.save(update_fields=["quantity"])

    # --- every product (including PROD-2007) moves to the 50kg unit weight ---
    Product.objects.update(weight_mt_per_unit=NEW_WEIGHT)

    # --- regenerate the BOM: random-but-mass-balanced, ~50kg of output needs
    #     roughly 50-70kg (1.0-1.4x, i.e. a plausible process-loss factor) of
    #     combined raw material, split across whatever materials that product
    #     already used. Seeded per product so this is reproducible. ---
    for p in products:
        lines = list(ProductRecipe.objects.filter(product_id=p.id))
        material_ids = [l.material_id for l in lines]
        if not material_ids:
            continue
        rnd = random.Random(f"bom-{p.sku}")
        yield_factor = Decimal(str(round(rnd.uniform(1.0, 1.4), 3)))
        target_total = (NEW_WEIGHT * yield_factor).quantize(Q2, rounding=ROUND_HALF_UP)

        weights = [rnd.random() + 0.15 for _ in material_ids]  # +0.15 floor: no near-zero shares
        weight_sum = sum(weights)
        shares = [(target_total * Decimal(str(w / weight_sum))).quantize(Q2, rounding=ROUND_HALF_UP)
                  for w in weights]
        shares = [s if s > Decimal("0.00") else Q2 for s in shares]  # never a zero-quantity line

        ProductRecipe.objects.filter(product_id=p.id).delete()
        ProductRecipe.objects.bulk_create([
            ProductRecipe(product_id=p.id, material_id=mid, quantity_required=qty)
            for mid, qty in zip(material_ids, shares)
        ])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0048_product_weight_help_text"),
    ]

    operations = [
        migrations.RunPython(rescale_and_randomize, migrations.RunPython.noop),
    ]
