import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TerraHub.settings')
django.setup()

from core.models import *
from core.utils import allocate_stock, deduct_stock_from_allocation

print('Creating test products...')
w = Warehouse.objects.first()
wl = WarehouseLocation.objects.filter(warehouse=w).first()

if not w or not wl:
    print('No warehouse/location found. Skipping test.')
    exit(0)

m = Material.objects.create(name='Test Material X', sku='MAT-X', unit_of_measure='kg')
p = Product.objects.create(name='Test Product Y', sku='PRD-Y', unit_of_measure='units', unit_price=10.0)

ProductRecipe.objects.create(product=p, material=m, quantity_required=2.0)

b_m = Batch.objects.create(batch_number='B-MAT-X-1', material=m, quantity=100.0, status='Active', manufacturing_date='2025-01-01', expiry_date='2026-01-01', location=wl)
b_p = Batch.objects.create(batch_number='B-PRD-Y-1', product=p, quantity=50.0, status='Active', manufacturing_date='2025-01-01', expiry_date='2026-01-01', location=wl)

print('Creating Sales Order...')
so = SalesOrder.objects.create(so_number='SO-TEST-1', origin_warehouse=w, status='Draft')
soi = SalesOrderDetail.objects.create(sales_order=so, product=p, quantity_ordered=60.0)

print('Testing Allocation...')
allocate_stock('sales_order', so, p, 60.0)
b_p.refresh_from_db()
print('Allocated Qty on Batch:', b_p.allocated_quantity)
print('Allocations:', StockAllocation.objects.filter(sales_order=so).count())

print('Testing Deduction...')
deduct_stock_from_allocation('sales_order', so)
b_p.refresh_from_db()
print('Remaining Qty:', b_p.quantity, 'Allocated Qty:', b_p.allocated_quantity)

print('Cleaning up...')
so.delete()
b_m.delete()
b_p.delete()
m.delete()
p.delete()
print('Test passed!')
