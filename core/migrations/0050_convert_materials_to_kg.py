"""
One-time correction: every Material moves from "1 unit = 1 MT" (or, for the
one broken row MAT-1005, a 'kg'-labeled unit that was secretly computing as
1 MT each) to a consistent "1 unit = 1 kg = 0.001 MT" convention.

Same shape of change as 0049_rescale_products_and_randomize_bom.py did for
Product, but reaches a different, parallel set of tables - materials flow
through purchasing/production/consumption rather than sales. Confirmed with
the user before writing this migration (see memory: mat-1005-weight-anomaly-
deferred.md, no-minimum-billing-floors.md session).

For every material that was on the old 1.0 MT/unit scale (every material
EXCEPT MAT-1005), every related quantity field is multiplied by 1000x
(1.0 / 0.001) so the *real* physical quantities represented stay the same,
just re-expressed in the new smaller unit. Reaches real historical Purchase
Orders (not just seeded/test data - e.g. PO-5001/5002/5003), not just
current-state tables.

MAT-1005 is NOT rescaled: its existing quantity numbers were recorded under
a broken weight_mt_per_unit=1.0 alongside a 'kg' label that never made
sense together (that's the same broken pattern PROD-2007 had, and the user
explicitly chose the same resolution: fix the weight, leave quantities
alone). Its weight becomes meaningful for the first time rather than being
rescaled from a number that was never coherent.

Per-unit PRICE fields (Material.cost_per_unit, SupplierMaterial.unit_price,
PurchaseOrderDetail.unit_price) are inverse-scaled (/1000) for rescaled
materials, so quantity x price keeps meaning the same real money - the
DB fields are only 2dp, so sub-cent-per-kg prices round to the nearest
cent; this is an accepted, pre-existing precision ceiling of those fields,
not something this migration can widen.

Per the user's explicit choice, RegistryLog.quantity_changed for
material-referencing rows IS rescaled here (unlike 0049's Product side,
where there was no reliable material FK to filter by - RegistryLog.material
is a real FK, so this can be done correctly this time).

WarehouseUtilizationSnapshot is NOT touched: it's a value-preserving
rescale (real tonnage doesn't change, only what unit it's expressed in),
so historical snapshot MT figures stay numerically correct without
regeneration - same as 0049.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations

NEW_WEIGHT = Decimal("0.0010")
SCALE = Decimal("1000")
Q2 = Decimal("0.01")
EXCLUDE_FROM_RESCALE = {"MAT-1005"}


def _scale(value):
    if value is None:
        return None
    return (Decimal(value) * SCALE).quantize(Q2, rounding=ROUND_HALF_UP)


def _inverse_scale(value):
    if value is None:
        return None
    return (Decimal(value) / SCALE).quantize(Q2, rounding=ROUND_HALF_UP)


def convert_materials_to_kg(apps, schema_editor):
    Material = apps.get_model("core", "Material")
    Batch = apps.get_model("core", "Batch")
    StockAllocation = apps.get_model("core", "StockAllocation")
    PurchaseOrderDetail = apps.get_model("core", "PurchaseOrderDetail")
    ShipmentItem = apps.get_model("core", "ShipmentItem")
    ShipmentItemReceipt = apps.get_model("core", "ShipmentItemReceipt")
    RunMaterialUsage = apps.get_model("core", "RunMaterialUsage")
    ProductionConsumption = apps.get_model("core", "ProductionConsumption")
    StockAudit = apps.get_model("core", "StockAudit")
    RegistryLog = apps.get_model("core", "RegistryLog")
    ProductRecipe = apps.get_model("core", "ProductRecipe")
    SupplierMaterial = apps.get_model("core", "SupplierMaterial")

    materials = list(Material.objects.all())
    rescale_ids = [m.id for m in materials
                   if m.weight_mt_per_unit == Decimal("1.0000") and m.sku not in EXCLUDE_FROM_RESCALE]

    # --- rescale every quantity field that references a rescaled material ---
    for b in Batch.objects.filter(material_id__in=rescale_ids):
        b.quantity = _scale(b.quantity)
        b.allocated_quantity = _scale(b.allocated_quantity)
        b.save(update_fields=["quantity", "allocated_quantity"])

    for sa in StockAllocation.objects.filter(batch__material_id__in=rescale_ids):
        sa.quantity = _scale(sa.quantity)
        sa.save(update_fields=["quantity"])

    for pod in PurchaseOrderDetail.objects.filter(material_id__in=rescale_ids):
        pod.quantity_ordered = _scale(pod.quantity_ordered)
        pod.quantity_received = _scale(pod.quantity_received)
        pod.unit_price = _inverse_scale(pod.unit_price)
        pod.save(update_fields=["quantity_ordered", "quantity_received", "unit_price"])

    for si in ShipmentItem.objects.filter(material_id__in=rescale_ids):
        si.quantity = _scale(si.quantity)
        si.received_quantity = _scale(si.received_quantity)
        si.save(update_fields=["quantity", "received_quantity"])

    for sir in ShipmentItemReceipt.objects.filter(shipment_item__material_id__in=rescale_ids):
        sir.quantity = _scale(sir.quantity)
        sir.save(update_fields=["quantity"])

    for rmu in RunMaterialUsage.objects.filter(material_id__in=rescale_ids):
        rmu.expected_qty = _scale(rmu.expected_qty)
        rmu.actual_qty = _scale(rmu.actual_qty)
        # variance_pct is a ratio of the two - scale-invariant, left as-is.
        rmu.save(update_fields=["expected_qty", "actual_qty"])

    for pc in ProductionConsumption.objects.filter(consumed_batch__material_id__in=rescale_ids):
        pc.quantity_used = _scale(pc.quantity_used)
        pc.save(update_fields=["quantity_used"])

    for sat in StockAudit.objects.filter(batch__material_id__in=rescale_ids):
        sat.expected_quantity = _scale(sat.expected_quantity)
        sat.actual_quantity = _scale(sat.actual_quantity)
        sat.save(update_fields=["expected_quantity", "actual_quantity"])

    for log in RegistryLog.objects.filter(material_id__in=rescale_ids):
        log.quantity_changed = _scale(log.quantity_changed)
        log.save(update_fields=["quantity_changed"])

    for pr in ProductRecipe.objects.filter(material_id__in=rescale_ids):
        pr.quantity_required = _scale(pr.quantity_required)
        pr.save(update_fields=["quantity_required"])

    for sm in SupplierMaterial.objects.filter(material_id__in=rescale_ids):
        sm.unit_price = _inverse_scale(sm.unit_price)
        sm.save(update_fields=["unit_price"])

    # --- every material (including MAT-1005) moves to the kg unit + weight ---
    for m in materials:
        m.unit_of_measure = "kg"
        m.weight_mt_per_unit = NEW_WEIGHT
        if m.id in rescale_ids:
            m.cost_per_unit = _inverse_scale(m.cost_per_unit)
        m.save(update_fields=["unit_of_measure", "weight_mt_per_unit", "cost_per_unit"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0049_rescale_products_and_randomize_bom"),
    ]

    operations = [
        migrations.RunPython(convert_materials_to_kg, migrations.RunPython.noop),
    ]
