import os
import django
from datetime import date, timedelta

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TerraHub.settings')
django.setup()

from core.models import (
    Warehouse, WarehouseLocation, Material, Product, Batch,
    SalesOrder, Shipment, RegistryLog, CustomUser
)

def seed_database():
    print("Initializing Database Seeding...")

    # Clear existing data to prevent duplicate entries on re-run
    RegistryLog.objects.all().delete()
    Shipment.objects.all().delete()
    SalesOrder.objects.all().delete()
    Batch.objects.all().delete()
    WarehouseLocation.objects.all().delete()
    Material.objects.all().delete()
    Product.objects.all().delete()
    Warehouse.objects.all().delete()

    # 1. Create Warehouses
    wh1 = Warehouse.objects.create(
        name="Primary Storage HQ",
        location_type="Storage",
        ownership_type="Internal",
        rental_billing_method="Usage",
        rental_cost_per_mt=0.00,
        total_capacity_mt=1500.00
    )
    wh2 = Warehouse.objects.create(
        name="Eastern Distribution Hub",
        location_type="Storage",
        ownership_type="ExternalProvider",
        rental_billing_method="Usage",
        rental_cost_per_mt=12.50,
        total_capacity_mt=1000.00
    )
    wh3 = Warehouse.objects.create(
        name="Manufacturing Site Alpha",
        location_type="Manufacturing",
        ownership_type="Internal",
        rental_billing_method="Overall",
        rental_cost_per_mt=0.00,
        total_capacity_mt=500.00
    )
    wh4 = Warehouse.objects.create(
        name="Supplier Storage Bay",
        location_type="Storage",
        ownership_type="SupplierStorage",
        rental_billing_method="Overall",
        rental_cost_per_mt=8.75,
        total_capacity_mt=800.00
    )

    # 2. Create Warehouse Locations
    loc1 = WarehouseLocation.objects.create(warehouse=wh1, zone_name="Alpha", aisle="01")
    loc2 = WarehouseLocation.objects.create(warehouse=wh1, zone_name="Beta", aisle="02")
    loc3 = WarehouseLocation.objects.create(warehouse=wh2, zone_name="Gamma", aisle="03")
    loc4 = WarehouseLocation.objects.create(warehouse=wh3, zone_name="Delta", aisle="04")
    loc5 = WarehouseLocation.objects.create(warehouse=wh4, zone_name="Epsilon", aisle="05")

    # 3. Create Materials
    mat1 = Material.objects.create(
        name="Silicon Wafer Raw",
        sku="MAT-SIL-01",
        category="Semiconductors",
        unit_of_measure="pcs",
        safe_storage_days=180,
        weight_mt_per_unit=0.0500,
        cost_per_unit=120.00
    )
    mat2 = Material.objects.create(
        name="Lithium Oxide Powder",
        sku="MAT-LI-99",
        category="Chemicals",
        unit_of_measure="kg",
        safe_storage_days=25,
        weight_mt_per_unit=0.1000,
        cost_per_unit=45.00
    )
    mat3 = Material.objects.create(
        name="High-Grade Cobalt",
        sku="MAT-COB-42",
        category="Minerals",
        unit_of_measure="MT",
        safe_storage_days=90,
        weight_mt_per_unit=1.0000,
        cost_per_unit=3500.00
    )

    # 4. Create Products
    prod1 = Product.objects.create(
        name="Enterprise Server Core",
        sku="PROD-SRV-X",
        description="High-performance server processors",
        unit_of_measure="pcs",
        weight_mt_per_unit=0.5000,
        price_per_unit=1250.00
    )
    prod2 = Product.objects.create(
        name="PowerCell Battery Pack",
        sku="PROD-BAT-Y",
        description="High-capacity storage cells",
        unit_of_measure="pcs",
        weight_mt_per_unit=0.1500,
        price_per_unit=350.00
    )

    # 5. Create Batches (Active)
    # Silicon: 8000 units * 0.05 MT = 400 MT
    Batch.objects.create(
        batch_number="B-SIL-101",
        status="Active",
        material=mat1,
        quantity=8000,
        manufacturing_date=date.today() - timedelta(days=20),
        expiry_date=date.today() + timedelta(days=160),
        location=loc1
    )
    # Lithium: 1500 units * 0.1 MT = 150 MT (expiring soon - in 10 days, threshold is 25)
    Batch.objects.create(
        batch_number="B-LITH-202",
        status="Active",
        material=mat2,
        quantity=1500,
        manufacturing_date=date.today() - timedelta(days=15),
        expiry_date=date.today() + timedelta(days=10),
        location=loc3
    )
    # Server: 400 units * 0.5 MT = 200 MT (expiring in 18 days, threshold is 30)
    Batch.objects.create(
        batch_number="B-SRV-303",
        status="Active",
        product=prod1,
        quantity=400,
        manufacturing_date=date.today() - timedelta(days=12),
        expiry_date=date.today() + timedelta(days=18),
        location=loc4
    )
    # Battery: 2500 units * 0.15 MT = 375 MT
    Batch.objects.create(
        batch_number="B-BAT-404",
        status="Active",
        product=prod2,
        quantity=2500,
        manufacturing_date=date.today() - timedelta(days=5),
        expiry_date=date.today() + timedelta(days=340),
        location=loc2
    )

    # 6. Create Sales Orders
    SalesOrder.objects.create(so_number="SO-1001", client_name="Neotech Industries", origin_warehouse=wh1, status="Pending")
    SalesOrder.objects.create(so_number="SO-1002", client_name="Global Logistics Corp", origin_warehouse=wh3, status="In Production")
    SalesOrder.objects.create(so_number="SO-1003", client_name="AeroSystems Inc", origin_warehouse=wh1, status="Ready to Ship")
    SalesOrder.objects.create(so_number="SO-1004", client_name="EcoEnergy Systems", origin_warehouse=wh2, status="Shipped")
    SalesOrder.objects.create(so_number="SO-1005", client_name="Quantum Dev Labs", origin_warehouse=wh1, status="Draft")
    SalesOrder.objects.create(so_number="SO-1006", client_name="Alpha Centauri Inc", origin_warehouse=wh2, status="Pending Approval")

    # 7. Create Shipments
    Shipment.objects.create(
        tracking_number="FLEET-TX-901",
        direction="Inbound",
        status="Dispatched",
        expected_eta_date=date.today() + timedelta(days=3),
        material=mat1,
        quantity=2000,
        destination_warehouse=wh1
    )
    Shipment.objects.create(
        tracking_number="FLEET-TX-440",
        direction="Outbound",
        status="Preparing",
        expected_eta_date=date.today() + timedelta(days=5),
        product=prod2,
        quantity=500,
        origin_warehouse=wh1
    )
    Shipment.objects.create(
        tracking_number="DHL-EX-7789",
        direction="Outbound",
        status="Delayed",
        expected_eta_date=date.today() + timedelta(days=1),
        product=prod1,
        quantity=100,
        origin_warehouse=wh3
    )

    # 8. Create Registry Logs
    RegistryLog.objects.create(action_type="Inbound", item_name="Silicon Wafer Raw", quantity_changed=8000.0, warehouse=wh1)
    RegistryLog.objects.create(action_type="Produced", item_name="Enterprise Server Core", quantity_changed=400.0, warehouse=wh3)
    RegistryLog.objects.create(action_type="Outbound", item_name="PowerCell Battery Pack", quantity_changed=150.0, warehouse=wh1)
    RegistryLog.objects.create(action_type="Spoiled_Disposal", item_name="Lithium Oxide Powder", quantity_changed=50.0, warehouse=wh2)
    RegistryLog.objects.create(action_type="Inbound", item_name="High-Grade Cobalt", quantity_changed=1000.0, warehouse=wh4)

    print("Database seeding completed successfully.")

if __name__ == '__main__':
    seed_database()
