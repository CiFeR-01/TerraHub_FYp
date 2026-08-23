import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TerraHub.settings')
django.setup()

from core.models import Batch, PurchaseOrder, WarehouseLocation

batches = Batch.objects.filter(batch_number__startswith='B-PO-')
fixed_count = 0
for b in batches:
    if b.warehouse is None:
        # Extract PO number. Format: B-PO-5001-MAT-1011-50
        parts = b.batch_number.split('-')
        if len(parts) >= 3:
            po_num = parts[1] + '-' + parts[2] # e.g. PO-5001
            po = PurchaseOrder.objects.filter(po_number=po_num).first()
            if po:
                b.warehouse = po.target_warehouse
                b.purchase_order = po
                loc = WarehouseLocation.objects.filter(warehouse=po.target_warehouse).first()
                if loc:
                    b.location = f"Zone {loc.zone_name} Aisle {loc.aisle}"
                b.save()
                fixed_count += 1
                print(f"Fixed {b.batch_number} -> assigned to {po.target_warehouse.name}")

print(f"Total fixed: {fixed_count}")
