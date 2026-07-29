import os
import django
from datetime import date, timedelta

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'TerraHub.settings')
django.setup()

from core.models import (
    Warehouse, WarehouseLocation, Material, Product, ProductRecipe, Batch,
    SalesOrder, PurchaseOrder, Shipment, RegistryLog, CustomUser, ProductionRun
)

def seed_database():
    print("Initializing TerraHub Agricultural Product Database Seeding...")

    # Clear existing data to prevent duplicate entries on re-run
    RegistryLog.objects.all().delete()
    Shipment.objects.all().delete()
    ProductionRun.objects.all().delete()
    SalesOrder.objects.all().delete()
    PurchaseOrder.objects.all().delete()
    Batch.objects.all().delete()
    ProductRecipe.objects.all().delete()
    WarehouseLocation.objects.all().delete()
    Material.objects.all().delete()
    Product.objects.all().delete()
    Warehouse.objects.all().delete()

    # 1. Create Warehouses
    wh1 = Warehouse.objects.create(
        name="Primary Storage & Processing HQ",
        location_type="Storage",
        ownership_type="Internal",
        rental_billing_method="Usage",
        rental_cost_per_mt=0.00,
        total_capacity_mt=2500.00
    )
    wh2 = Warehouse.objects.create(
        name="East Coast Distribution Hub",
        location_type="Storage",
        ownership_type="ExternalProvider",
        rental_billing_method="Usage",
        rental_cost_per_mt=10.50,
        total_capacity_mt=1500.00
    )
    wh3 = Warehouse.objects.create(
        name="Acid Ammoniation Plant Alpha",
        location_type="Manufacturing",
        ownership_type="Internal",
        rental_billing_method="Overall",
        rental_cost_per_mt=0.00,
        total_capacity_mt=1200.00
    )
    wh4 = Warehouse.objects.create(
        name="Raw Import Port Bay",
        location_type="Storage",
        ownership_type="SupplierStorage",
        rental_billing_method="Overall",
        rental_cost_per_mt=8.00,
        total_capacity_mt=2000.00
    )

    # 2. Create Warehouse Locations
    loc1 = WarehouseLocation.objects.create(warehouse=wh1, zone_name="A-Solubles", aisle="01")
    loc2 = WarehouseLocation.objects.create(warehouse=wh1, zone_name="B-Granular", aisle="02")
    loc3 = WarehouseLocation.objects.create(warehouse=wh2, zone_name="C-BioOrganics", aisle="03")
    loc4 = WarehouseLocation.objects.create(warehouse=wh3, zone_name="D-Reactor", aisle="04")
    loc5 = WarehouseLocation.objects.create(warehouse=wh4, zone_name="E-BulkRaw", aisle="05")

    # 3. Create Straight & Raw Material Fertilizers (Category 4)
    mat_cirp = Material.objects.create(
        name="Christmas Island Rock Phosphate (CIRP)",
        sku="MAT-CIRP-30",
        category="Straight & Raw Materials",
        unit_of_measure="MT",
        safe_storage_days=365,
        weight_mt_per_unit=1.0000,
        cost_per_unit=450.00
    )
    mat_ams = Material.objects.create(
        name="Ammonium Sulphate (21% N + Organic Matter)",
        sku="MAT-AMS-21",
        category="Straight & Raw Materials",
        unit_of_measure="MT",
        safe_storage_days=365,
        weight_mt_per_unit=1.0000,
        cost_per_unit=620.00
    )
    mat_amc = Material.objects.create(
        name="Ammonium Chloride (25% N)",
        sku="MAT-AMC-25",
        category="Straight & Raw Materials",
        unit_of_measure="MT",
        safe_storage_days=365,
        weight_mt_per_unit=1.0000,
        cost_per_unit=580.00
    )
    mat_mop = Material.objects.create(
        name="Potassium Chloride / MOP (60% K2O)",
        sku="MAT-KCL-60",
        category="Straight & Raw Materials",
        unit_of_measure="MT",
        safe_storage_days=365,
        weight_mt_per_unit=1.0000,
        cost_per_unit=850.00
    )
    mat_te = Material.objects.create(
        name="Chelated Trace Elements Mix (TE + MgO)",
        sku="MAT-TE-CHELATE",
        category="Micro-Nutrients",
        unit_of_measure="kg",
        safe_storage_days=180,
        weight_mt_per_unit=0.0010,
        cost_per_unit=18.50
    )
    mat_bio = Material.objects.create(
        name="Active Chitin & Tea Extract Liquid",
        sku="MAT-BIO-KITIN",
        category="Bio-Extracts",
        unit_of_measure="L",
        safe_storage_days=120,
        weight_mt_per_unit=0.0010,
        cost_per_unit=14.00
    )

    # 4. Create Formulated Products (Categories 1, 2, 3)

    # Category 1: Water-Soluble Fertilizers (wsNPK & Foliar)
    p1 = Product.objects.create(
        name="wsNPK 20-20-20 + TE",
        sku="PROD-WS-202020",
        description="Balanced All-Rounder formulation for all-stage plant nutrition; ideal for Durian, Banana, and leafy crops.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=3200.00
    )
    p2 = Product.objects.create(
        name="wsPalmore 10-35-15 + Zn + Cu + TE",
        sku="PROD-WSP-103515",
        description="High Phosphate (Rooting & Flowering) - Promotes early growth, root development, and flower set (e.g. Oil Palm, Coconut).",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=3650.00
    )
    p3 = Product.objects.create(
        name="wsPalmore 10-8-35 + MgO + TE",
        sku="PROD-WSP-10835",
        description="High Potassium (Fruiting & Yield) - Enhances fruit size, weight, and sweetness in fruiting crops.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=3850.00
    )
    p4 = Product.objects.create(
        name="wsNPK 10-12-36 + 2MgO + TE",
        sku="PROD-WS-101236",
        description="Fruit Filling Formula - High-K formulation for crop maturation and fruit expansion.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=3950.00
    )
    p5 = Product.objects.create(
        name="Premium LEAF NPK 22-9-9",
        sku="PROD-LEAF-2299",
        description="High Nitrogen Vegetative Foliar - Rapid vegetative growth, leaf greening, and soil condition improvement.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=3100.00
    )

    # Category 2: Compound & NPK Fertilizers (Goldmas Series)
    p6 = Product.objects.create(
        name="Goldmas Compound Green",
        sku="PROD-GM-GREEN",
        description="Balanced Compound (15-15-15) with activated phosphate & organic chelated trace elements for general crop development.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=2850.00
    )
    p7 = Product.objects.create(
        name="Goldmas Premium Red",
        sku="PROD-GM-RED",
        description="Flowering & Fruiting Granular - Targeted nutrition for high-yield fruit trees and cash crops.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=3400.00
    )
    p8 = Product.objects.create(
        name="Goldmas Premium Yellow",
        sku="PROD-GM-YELLOW",
        description="Starter / Rooting Granular - Designed for early-stage establishment and root extension.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=2950.00
    )
    p9 = Product.objects.create(
        name="Goldmas Premium Blue",
        sku="PROD-GM-BLUE",
        description="Crop Vitality Compound - Fast-release macronutrients for high nutrient-demanding phases.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=3500.00
    )

    # Category 3: Bio-Organic & Soil Amendments
    p10 = Product.objects.create(
        name="B - Balance",
        sku="PROD-BIO-BBAL",
        description="Bio-Stimulant / Root Booster - Contains Chitin (Kitin) to repair cells, promote microbial activity, and build plant immunity.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=4200.00
    )
    p11 = Product.objects.create(
        name="T - Balance",
        sku="PROD-BIO-TBAL",
        description="Soil & Ecosystem Health - Blended with tea extracts to encourage beneficial microorganisms and root health.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=4100.00
    )
    p12 = Product.objects.create(
        name="Bio-Organic Compound Fertilizers",
        sku="PROD-BIO-COMP",
        description="Organic Soil Conditioner - Blends organic matter with essential NPK for soil structure recovery.",
        unit_of_measure="MT",
        weight_mt_per_unit=1.0000,
        price_per_unit=2600.00
    )

    # 5. Product Recipes (BOM linkages)
    ProductRecipe.objects.create(product=p1, material=mat_ams, quantity_required=0.35)
    ProductRecipe.objects.create(product=p1, material=mat_cirp, quantity_required=0.35)
    ProductRecipe.objects.create(product=p1, material=mat_mop, quantity_required=0.30)

    ProductRecipe.objects.create(product=p6, material=mat_amc, quantity_required=0.40)
    ProductRecipe.objects.create(product=p6, material=mat_cirp, quantity_required=0.40)
    ProductRecipe.objects.create(product=p6, material=mat_mop, quantity_required=0.20)

    ProductRecipe.objects.create(product=p10, material=mat_bio, quantity_required=50.0) # 50L per MT

    # 6. Active Batches
    Batch.objects.create(
        batch_number="B-CIRP-101",
        status="Active",
        material=mat_cirp,
        quantity=500,
        manufacturing_date=date.today() - timedelta(days=30),
        expiry_date=date.today() + timedelta(days=335),
        location=loc5
    )
    Batch.objects.create(
        batch_number="B-AMS-202",
        status="Active",
        material=mat_ams,
        quantity=350,
        manufacturing_date=date.today() - timedelta(days=15),
        expiry_date=date.today() + timedelta(days=350),
        location=loc5
    )
    Batch.objects.create(
        batch_number="B-WS20-303",
        status="Active",
        product=p1,
        quantity=120,
        manufacturing_date=date.today() - timedelta(days=10),
        expiry_date=date.today() + timedelta(days=180),
        location=loc1
    )
    Batch.objects.create(
        batch_number="B-GMGR-404",
        status="Active",
        product=p6,
        quantity=200,
        manufacturing_date=date.today() - timedelta(days=5),
        expiry_date=date.today() + timedelta(days=360),
        location=loc2
    )
    Batch.objects.create(
        batch_number="B-BBAL-505",
        status="Active",
        product=p10,
        quantity=45,
        manufacturing_date=date.today() - timedelta(days=12),
        expiry_date=date.today() + timedelta(days=20), # expiring soon alert
        location=loc3
    )

    # 7. Sales & Purchase Orders
    SalesOrder.objects.create(so_number="SO-FERT-1001", client_name="Durian Plantation Tech", origin_warehouse=wh1, status="In Production")
    SalesOrder.objects.create(so_number="SO-FERT-1002", client_name="Borneo Oil Palm Corp", origin_warehouse=wh2, status="Ready to Ship")
    SalesOrder.objects.create(so_number="SO-FERT-1003", client_name="Malaya Agricultural Consortium", origin_warehouse=wh1, status="Pending")

    PurchaseOrder.objects.create(po_number="PO-RAW-5001", supplier_name="Christmas Island Mining Corp", target_warehouse=wh4, status="Pending")
    PurchaseOrder.objects.create(po_number="PO-RAW-5002", supplier_name="Global Nitrogen Synthetics", target_warehouse=wh4, status="Completed")

    # 8. Shipments
    Shipment.objects.create(
        tracking_number="FREET-LOG-901",
        direction="Inbound",
        status="Dispatched",
        expected_eta_date=date.today() + timedelta(days=4),
        material=mat_cirp,
        quantity=250,
        destination_warehouse=wh4
    )
    Shipment.objects.create(
        tracking_number="FREET-LOG-440",
        direction="Outbound",
        status="Preparing",
        expected_eta_date=date.today() + timedelta(days=2),
        product=p6,
        quantity=80,
        origin_warehouse=wh1
    )

    # 9. Registry Logs
    RegistryLog.objects.create(action_type="Inbound", item_name="Christmas Island Rock Phosphate (CIRP)", quantity_changed=500.0, warehouse=wh4)
    RegistryLog.objects.create(action_type="Produced", item_name="wsNPK 20-20-20 + TE", quantity_changed=120.0, warehouse=wh3)
    RegistryLog.objects.create(action_type="Produced", item_name="Goldmas Compound Green", quantity_changed=200.0, warehouse=wh3)
    RegistryLog.objects.create(action_type="Consumed_For_Manufacturing", item_name="Ammonium Sulphate (21% N)", quantity_changed=50.0, warehouse=wh3)

    print("TerraHub Product Catalog Database Seeding completed successfully!")

if __name__ == '__main__':
    seed_database()
