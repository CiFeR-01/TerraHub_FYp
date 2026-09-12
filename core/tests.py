from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from core.db_tracker import get_db_status, DB_QUERY_LOGS
from django.db import connection
from core.models import (
    Warehouse, WarehouseLocation, Material, Product, ProductRecipe,
    Batch, SalesOrder, SalesOrderDetail, StockAllocation,
)
from core.utils import allocate_stock, deduct_stock_from_allocation

User = get_user_model()

class DatabaseConsoleTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testadmin', password='password123')
        
    def test_db_status_keys(self):
        status = get_db_status()
        self.assertIn('status', status)
        self.assertIn('engine', status)
        self.assertIn('name', status)
        self.assertIn('is_live', status)
        
    def test_db_query_interception(self):
        # Clear buffer
        DB_QUERY_LOGS.clear()
        
        # Run a query
        list(User.objects.all())
        
        # Verify a query was logged
        self.assertTrue(len(DB_QUERY_LOGS) > 0)
        last_log = DB_QUERY_LOGS[-1]
        self.assertIn('sql', last_log)
        self.assertIn('type', last_log)
        self.assertEqual(last_log['type'], 'READ')
        
    def test_db_logs_api_requires_login(self):
        url = reverse('db_logs_api')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302) # Redirect to login
        
    def test_db_logs_api_authenticated(self):
        self.client.login(username='testadmin', password='password123')
        url = reverse('db_logs_api')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('logs', data)
        self.assertIn('db_status', data)
        
    def test_db_clear_logs(self):
        self.client.login(username='testadmin', password='password123')
        # Insert a dummy query in buffer
        DB_QUERY_LOGS.append({'sql': 'SELECT 1', 'type': 'READ', 'timestamp': '12:00:00', 'duration': '0.1ms', 'success': True})
        
        url = reverse('db_clear_logs')
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(DB_QUERY_LOGS), 0)

    def test_db_test_op_read(self):
        self.client.login(username='testadmin', password='password123')
        url = reverse('db_test_op') + '?type=read'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Read query', response.json()['message'])

    def test_db_test_op_write(self):
        self.client.login(username='testadmin', password='password123')
        url = reverse('db_test_op') + '?type=write'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Write query', response.json()['message'])


from core.models import Warehouse

class FacilityEditTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='teststaff', password='password123')
        self.warehouse = Warehouse.objects.create(
            name="Test facility A",
            location_type="Storage",
            ownership_type="Internal",
            rental_billing_method="Usage",
            rental_cost_per_mt=0.00,
            total_capacity_mt=500.00
        )
        
    def test_warehouse_edit_requires_login(self):
        url = reverse('warehouse_edit', kwargs={'pk': self.warehouse.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        
    def test_warehouse_edit_permission_denied(self):
        self.client.login(username='teststaff', password='password123')
        url = reverse('warehouse_edit', kwargs={'pk': self.warehouse.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('warehouse_list'))
        
    def test_warehouse_edit_get_success(self):
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        content_type = ContentType.objects.get_for_model(Warehouse)
        permission = Permission.objects.get(codename='change_warehouse', content_type=content_type)
        self.user.user_permissions.add(permission)
        
        self.client.login(username='teststaff', password='password123')
        url = reverse('warehouse_edit', kwargs={'pk': self.warehouse.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test facility A')
        self.assertContains(response, 'Save Changes')
        
    def test_warehouse_edit_post_success(self):
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        content_type = ContentType.objects.get_for_model(Warehouse)
        permission = Permission.objects.get(codename='change_warehouse', content_type=content_type)
        self.user.user_permissions.add(permission)
        
        self.client.login(username='teststaff', password='password123')
        url = reverse('warehouse_edit', kwargs={'pk': self.warehouse.pk})
        post_data = {
            'name': 'Updated Facility name',
            'location_type': 'Manufacturing',
            'ownership_type': 'ExternalProvider',
            'rental_billing_method': 'Overall',
            'rental_cost_per_mt': '15.50',
            'total_capacity_mt': '750.00'
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('warehouse_list'))
        
        self.warehouse.refresh_from_db()
        self.assertEqual(self.warehouse.name, 'Updated Facility name')
        self.assertEqual(self.warehouse.location_type, 'Manufacturing')
        self.assertEqual(self.warehouse.ownership_type, 'ExternalProvider')
        self.assertEqual(self.warehouse.rental_billing_method, 'Overall')
        self.assertEqual(float(self.warehouse.rental_cost_per_mt), 15.50)
        self.assertEqual(float(self.warehouse.total_capacity_mt), 750.00)


from core.models import Product, Material, ProductRecipe
import io

class BulkImportExportTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='catalogmanager', password='password123')
        self.client.login(username='catalogmanager', password='password123')

        self.p1 = Product.objects.create(
            name="Existing Product Alpha",
            sku="PROD0001",
            description="Alpha product desc",
            unit_of_measure="pcs",
            weight_mt_per_unit=0.5,
            price_per_unit=100.00
        )
        self.m1 = Material.objects.create(
            name="Existing Material Alpha",
            sku="MAT0001",
            category="Chemicals",
            unit_of_measure="MT",
            safe_storage_days=90,
            weight_mt_per_unit=1.0,
            cost_per_unit=50.00
        )

    def test_export_product_template(self):
        url = reverse('export_product_template')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertContains(response, 'name,sku,description,unit_of_measure,weight_mt_per_unit,price_per_unit')

    def test_export_products_csv(self):
        url = reverse('export_products_csv')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Existing Product Alpha')
        self.assertContains(response, 'PROD0001')

    def test_import_products_create_and_skip_duplicates(self):
        url = reverse('import_products')
        csv_data = (
            "name,sku,description,unit_of_measure,weight_mt_per_unit,price_per_unit\n"
            "Existing Product Alpha,PROD0001,Dup test,pcs,0.5,100.00\n" # DB Dup (Skip)
            "Brand New Product Beta,,New product desc,kg,0.25,250.00\n" # New Auto SKU
        )
        file = io.BytesIO(csv_data.encode('utf-8'))
        file.name = 'products.csv'
        
        response = self.client.post(url, {'csv_file': file, 'duplicate_mode': 'skip'}, follow=True)
        self.assertEqual(response.status_code, 200)
        
        # New product created
        self.assertTrue(Product.objects.filter(name="Brand New Product Beta").exists())
        # Total products count = 2
        self.assertEqual(Product.objects.count(), 2)

    def test_import_products_update_mode(self):
        url = reverse('import_products')
        csv_data = (
            "name,sku,description,unit_of_measure,weight_mt_per_unit,price_per_unit\n"
            "Existing Product Alpha Updated,PROD0001,Updated desc,pcs,0.75,199.99\n"
        )
        file = io.BytesIO(csv_data.encode('utf-8'))
        file.name = 'products_update.csv'

        response = self.client.post(url, {'csv_file': file, 'duplicate_mode': 'update'}, follow=True)
        self.assertEqual(response.status_code, 200)
        
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.name, "Existing Product Alpha Updated")
        self.assertEqual(float(self.p1.price_per_unit), 199.99)

    def test_export_material_template(self):
        url = reverse('export_material_template')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name,sku,category,unit_of_measure,safe_storage_days,weight_mt_per_unit,cost_per_unit')

    def test_export_materials_csv(self):
        url = reverse('export_materials_csv')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Existing Material Alpha')

    def test_import_materials_create(self):
        url = reverse('import_materials')
        csv_data = (
            "name,sku,category,unit_of_measure,safe_storage_days,weight_mt_per_unit,cost_per_unit\n"
            "Solvent Fluid Beta,MAT0002,Solvents,L,60,0.001,15.00\n"
        )
        file = io.BytesIO(csv_data.encode('utf-8'))
        file.name = 'materials.csv'

        response = self.client.post(url, {'csv_file': file, 'duplicate_mode': 'skip'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Material.objects.filter(sku="MAT0002").exists())

    def test_export_product_recipes_csv(self):
        ProductRecipe.objects.create(product=self.p1, material=self.m1, quantity_required=5.0)
        url = reverse('export_recipes_csv')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'PROD0001')
        self.assertContains(response, 'MAT0001')

    def test_get_product_recipe_api(self):
        ProductRecipe.objects.create(product=self.p1, material=self.m1, quantity_required=3.5)
        url = reverse('get_product_recipe_api', kwargs={'product_id': self.p1.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['product_sku'], 'PROD0001')
        self.assertEqual(len(data['recipe_items']), 1)
        self.assertEqual(data['recipe_items'][0]['quantity_required'], 3.5)

    def test_save_product_recipe_api_batch_save(self):
        url = reverse('save_product_recipe_api')
        response = self.client.post(url, {
            'action': 'save_batch',
            'product_id': self.p1.id,
            'material_ids[]': [self.m1.id],
            'quantities[]': ['4.25']
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ProductRecipe.objects.filter(product=self.p1, material=self.m1, quantity_required=4.25).exists())

    def test_save_product_recipe_api_delete_and_clone(self):
        recipe = ProductRecipe.objects.create(product=self.p1, material=self.m1, quantity_required=10.0)
        p2 = Product.objects.create(name="Target Product Beta", sku="PROD0002")

        # Clone
        url = reverse('save_product_recipe_api')
        clone_res = self.client.post(url, {
            'action': 'clone_recipe',
            'source_product_id': self.p1.id,
            'target_product_id': p2.id
        })
        self.assertEqual(clone_res.status_code, 200)
        self.assertTrue(ProductRecipe.objects.filter(product=p2, material=self.m1, quantity_required=10.0).exists())

        # Delete
        del_res = self.client.post(url, {
            'action': 'delete_item',
            'recipe_id': recipe.id
        })
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(ProductRecipe.objects.filter(id=recipe.id).exists())

    def test_material_edit_view(self):
        url = reverse('material_edit', kwargs={'pk': self.m1.id})
        get_res = self.client.get(url)
        self.assertEqual(get_res.status_code, 200)

        post_res = self.client.post(url, {
            'name': 'Updated Titanium Pigment',
            'sku': 'MAT0001',
            'category': 'Chemicals Advanced',
            'unit_of_measure': 'kg',
            'safe_storage_days': '120',
            'weight_mt_per_unit': '0.0010',
            'cost_per_unit': '85.00'
        }, follow=True)
        self.assertEqual(post_res.status_code, 200)

        self.m1.refresh_from_db()
        self.assertEqual(self.m1.name, 'Updated Titanium Pigment')
        self.assertEqual(self.m1.category, 'Chemicals Advanced')
        self.assertEqual(self.m1.unit_of_measure, 'kg')
        self.assertEqual(self.m1.safe_storage_days, 120)
        self.assertEqual(float(self.m1.cost_per_unit), 85.00)


class StockAllocationTests(TestCase):
    def setUp(self):
        self.warehouse = Warehouse.objects.create(name='Test Warehouse', location_type='Storage')
        self.location = WarehouseLocation.objects.create(
            warehouse=self.warehouse, zone_name='A', aisle='1'
        )
        self.material = Material.objects.create(
            name='Test Material X', sku='MAT-X', category='Test', unit_of_measure='kg',
            safe_storage_days=365,
        )
        self.product = Product.objects.create(
            name='Test Product Y', sku='PRD-Y', unit_of_measure='pcs', price_per_unit=10.0
        )
        ProductRecipe.objects.create(product=self.product, material=self.material, quantity_required=2.0)

        self.batch = Batch.objects.create(
            batch_number='B-PRD-Y-1', product=self.product, quantity=50.0, status='Active',
            manufacturing_date='2025-01-01', expiry_date='2026-01-01',
            warehouse=self.warehouse, location='Zone A Aisle 1',
        )

        self.sales_order = SalesOrder.objects.create(
            so_number='SO-TEST-1', client_name='Test Client',
            origin_warehouse=self.warehouse, status='Draft'
        )
        SalesOrderDetail.objects.create(
            sales_order=self.sales_order, product=self.product, quantity_ordered=30.0
        )

    def test_allocate_stock_reserves_batch_quantity(self):
        allocated = allocate_stock('sales_order', self.sales_order, self.product, 30.0)
        self.batch.refresh_from_db()

        self.assertEqual(float(allocated), 30.0)
        self.assertEqual(float(self.batch.allocated_quantity), 30.0)
        self.assertEqual(StockAllocation.objects.filter(sales_order=self.sales_order).count(), 1)

    def test_deduct_stock_from_allocation_reduces_batch_quantity(self):
        allocate_stock('sales_order', self.sales_order, self.product, 30.0)
        deduct_stock_from_allocation('sales_order', self.sales_order)
        self.batch.refresh_from_db()

        self.assertEqual(float(self.batch.quantity), 20.0)
        self.assertEqual(float(self.batch.allocated_quantity), 0.0)
        self.assertFalse(StockAllocation.objects.filter(sales_order=self.sales_order).exists())


from datetime import timedelta
from decimal import Decimal
from django.utils import timezone
from core.models import RegistryLog
from core.analytics import consumption_rate, daily_consumption, resolve_material_from_label


class RegistryLogMaterialSpineTests(TestCase):
    """Phase 0: RegistryLog.material FK + consumption-rate analytics."""

    def setUp(self):
        self.warehouse = Warehouse.objects.create(name='Plant 1', location_type='Manufacturing')
        self.material = Material.objects.create(
            name='Citric Acid', sku='MAT-CIT', category='Acids',
            unit_of_measure='kg', safe_storage_days=180,
        )
        self.other = Material.objects.create(
            name='Sodium Nitrate', sku='MAT-SN', category='Salts',
            unit_of_measure='kg', safe_storage_days=365,
        )

    def _log(self, qty, days_ago, material=None, action='Consumed_For_Manufacturing'):
        row = RegistryLog.objects.create(
            action_type=action,
            item_name=f"{(material or self.material).name} (Run RUN-X)",
            material=material or self.material,
            quantity_changed=Decimal(str(qty)),
            warehouse=self.warehouse,
        )
        # timestamp has auto_now_add=True, so set it explicitly afterwards
        RegistryLog.objects.filter(pk=row.pk).update(
            timestamp=timezone.now() - timedelta(days=days_ago)
        )
        return row

    def test_material_fk_persists(self):
        row = self._log(10, 1)
        row.refresh_from_db()
        self.assertEqual(row.material, self.material)
        self.assertIn(row, self.material.registry_logs.all())

    def test_daily_consumption_buckets_by_day_and_material(self):
        self._log(4, 1)
        self._log(6, 1)          # same day -> summed
        self._log(5, 3)
        self._log(99, 3, material=self.other)   # different material -> excluded
        series = daily_consumption(self.material, window_days=7)
        self.assertEqual(sorted(series.values()), [Decimal('5'), Decimal('10')])
        self.assertEqual(sum(series.values()), Decimal('15'))

    def test_daily_consumption_respects_window(self):
        self._log(10, 2)
        self._log(50, 40)        # outside a 30-day window
        series = daily_consumption(self.material, window_days=30)
        self.assertEqual(sum(series.values()), Decimal('10'))

    def test_consumption_rate_divides_by_full_window(self):
        self._log(30, 1)
        self._log(30, 5)         # 60 total over a 30-day window -> 2.0/day
        self.assertEqual(consumption_rate(self.material, window_days=30), Decimal('2'))

    def test_consumption_rate_zero_when_no_activity(self):
        self.assertEqual(consumption_rate(self.material, window_days=30), Decimal('0'))

    def test_non_consumption_actions_are_ignored(self):
        self._log(100, 1, action='Inbound')
        self.assertEqual(consumption_rate(self.material, window_days=30), Decimal('0'))


class RegistryLogBackfillResolverTests(TestCase):
    """The item_name -> Material parser used by migration 0036."""

    def setUp(self):
        self.by_name = {
            'citric acid': 'CIT',
            'sodium nitrate': 'SN',
        }

    def _r(self, s):
        return resolve_material_from_label(s, self.by_name)

    def test_run_suffix(self):
        self.assertEqual(self._r('Citric Acid (Run RUN-2010)'), 'CIT')

    def test_batch_suffix(self):
        self.assertEqual(self._r('Sodium Nitrate (Batch B-PO-1-9D5040)'), 'SN')

    def test_force_closed_outbound(self):
        self.assertEqual(self._r('Citric Acid (Batch X) - force closed'), 'CIT')

    def test_manual_receipt(self):
        self.assertEqual(self._r('Manual Receipt of Sodium Nitrate'), 'SN')

    def test_material_edit(self):
        self.assertEqual(self._r("Updated Material 'Citric Acid' (SKU: MAT-CIT)"), 'CIT')

    def test_audit_resolve(self):
        self.assertEqual(self._r('Batch B-1 (MAT-SN - Sodium Nitrate)'), 'SN')

    def test_unknown_returns_none(self):
        self.assertIsNone(self._r('Bulk Import Products (3 created)'))
        self.assertIsNone(self._r('wsNPK 20-20-20 + TE (Batch FG-1)'))


from datetime import date
from core.models import (
    Supplier, SupplierMaterial, PurchaseOrder, PurchaseOrderDetail, Shipment,
    SystemSetting, ProductionRun, RunMaterialUsage, StockAudit,
    WarehouseUtilizationSnapshot,
)
from core.analytics import (
    supplier_reliability, sales_order_delivery_risk,
    audit_accuracy, production_yield_variance, stockout_forecast,
    capacity_forecast, warehouse_utilization,
)
from core.settings_store import get_setting


class SupplierScorecardTests(TestCase):
    """Phase 1: supplier_reliability() roll-up and the scorecard view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='scorecard', password='pw')
        self.wh = Warehouse.objects.create(name='RM Store', location_type='Storage')
        self.material = Material.objects.create(
            name='Potash', sku='MAT-K', category='Salts',
            unit_of_measure='MT', safe_storage_days=365,
        )
        self.good = Supplier.objects.create(name='Reliable Chemicals')
        self.poor = Supplier.objects.create(name='Laggard Supplies')
        today = date.today()

        # Reliable: on time (arrived a day early), fully filled, completed.
        po_g = self._po('PO-G1', self.good, expected=today, status='Completed')
        self._detail(po_g, ordered='100', received='100')
        self._shipment('TRK-G1', po_g, arrival=today - timedelta(days=1))

        # Laggard: 10 days late, only 80% filled, still partially received.
        po_p = self._po('PO-P1', self.poor, expected=today - timedelta(days=10),
                        status='Partially Received')
        self._detail(po_p, ordered='100', received='80')
        self._shipment('TRK-P1', po_p, arrival=today)

    def _po(self, number, supplier, expected, status):
        return PurchaseOrder.objects.create(
            po_number=number, supplier_name=supplier.name, supplier=supplier,
            target_warehouse=self.wh, expected_delivery_date=expected, status=status,
        )

    def _detail(self, po, ordered, received):
        return PurchaseOrderDetail.objects.create(
            purchase_order=po, material=self.material,
            quantity_ordered=Decimal(ordered), quantity_received=Decimal(received),
        )

    def _shipment(self, trk, po, arrival):
        return Shipment.objects.create(
            tracking_number=trk, direction='Inbound', status='Arrived',
            purchase_order=po, actual_arrival_date=arrival,
        )

    def _row(self, rows, name):
        return next(r for r in rows if r['supplier_name'] == name)

    def test_on_time_full_supplier_rated_good(self):
        row = self._row(supplier_reliability(), 'Reliable Chemicals')
        self.assertEqual(row['on_time_rate'], 1.0)
        self.assertEqual(row['fill_rate'], 1.0)
        self.assertLessEqual(row['avg_delay_days'], 0)
        self.assertEqual(row['rating'], 'good')

    def test_late_partial_supplier_rated_poor(self):
        row = self._row(supplier_reliability(), 'Laggard Supplies')
        self.assertEqual(row['on_time_rate'], 0.0)
        self.assertEqual(row['fill_rate'], 0.8)
        self.assertEqual(row['avg_delay_days'], 10)
        self.assertEqual(row['late_count'], 1)
        self.assertEqual(row['rating'], 'poor')

    def test_sorted_worst_first(self):
        rows = supplier_reliability()
        self.assertEqual(rows[0]['supplier_name'], 'Laggard Supplies')

    def test_arrival_falls_back_to_received_batch(self):
        s = Supplier.objects.create(name='Batch Only Co')
        po = self._po('PO-B1', s, expected=date.today(), status='Completed')
        self._detail(po, ordered='50', received='50')
        Batch.objects.create(
            batch_number='B-PO-B1-1', material=self.material, quantity=Decimal('50'),
            purchase_order=po, warehouse=self.wh,
            manufacturing_date=date.today() - timedelta(days=2),
            expiry_date=date.today() + timedelta(days=200),
        )
        row = self._row(supplier_reliability(), 'Batch Only Co')
        self.assertEqual(row['assessable_count'], 1)
        self.assertEqual(row['on_time_count'], 1)

    def test_window_filters_by_order_date(self):
        old = self._po('PO-OLD', self.good, expected=date.today(), status='Completed')
        PurchaseOrder.objects.filter(pk=old.pk).update(
            order_date=date.today() - timedelta(days=400)
        )
        recent = supplier_reliability(since=date.today() - timedelta(days=90))
        row = self._row(recent, 'Reliable Chemicals')
        self.assertEqual(row['po_count'], 1)  # PO-OLD excluded

    def test_draft_po_excluded(self):
        s = Supplier.objects.create(name='Draft Only Co')
        self._po('PO-D1', s, expected=date.today(), status='Draft')
        names = [r['supplier_name'] for r in supplier_reliability()]
        self.assertNotIn('Draft Only Co', names)

    def test_view_renders(self):
        self.client.login(username='scorecard', password='pw')
        resp = self.client.get(reverse('supplier_scorecard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Laggard Supplies')
        self.assertContains(resp, 'Supplier Scorecard')

    def test_view_requires_login(self):
        resp = self.client.get(reverse('supplier_scorecard'))
        self.assertEqual(resp.status_code, 302)


class SystemSettingTests(TestCase):
    def test_default_when_no_row(self):
        SystemSetting.objects.filter(key='po_default_lead_time_days').delete()
        self.assertEqual(get_setting('po_default_lead_time_days'), 14)

    def test_db_override_is_typed(self):
        SystemSetting.objects.update_or_create(
            key='po_default_lead_time_days',
            defaults={'value': '21', 'value_type': 'int'},
        )
        self.assertEqual(get_setting('po_default_lead_time_days'), 21)

    def test_invalid_value_falls_back_to_default(self):
        SystemSetting.objects.update_or_create(
            key='po_default_lead_time_days',
            defaults={'value': 'not-a-number', 'value_type': 'int'},
        )
        self.assertEqual(get_setting('po_default_lead_time_days'), 14)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            get_setting('no_such_setting')

    def test_seeded_by_migration(self):
        self.assertTrue(SystemSetting.objects.filter(key='po_default_lead_time_days').exists())


class SupplierScorecardExpectedDateTests(TestCase):
    """Phase 1: lead-time-derived expected date + completed_date arrival."""

    def setUp(self):
        self.wh = Warehouse.objects.create(name='RM2', location_type='Storage')
        self.material = Material.objects.create(
            name='Urea', sku='MAT-U', category='N', unit_of_measure='MT', safe_storage_days=365,
        )
        self.supplier = Supplier.objects.create(name='LeadTime Co')

    def _po(self, number, *, expected=None, status='Completed', order_days_ago=0,
            completed_days_ago=None):
        po = PurchaseOrder.objects.create(
            po_number=number, supplier_name=self.supplier.name, supplier=self.supplier,
            target_warehouse=self.wh, expected_delivery_date=expected, status=status,
        )
        updates = {}
        if order_days_ago:
            updates['order_date'] = date.today() - timedelta(days=order_days_ago)
        if completed_days_ago is not None:
            updates['completed_date'] = date.today() - timedelta(days=completed_days_ago)
        if updates:
            PurchaseOrder.objects.filter(pk=po.pk).update(**updates)
        PurchaseOrderDetail.objects.create(
            purchase_order=po, material=self.material,
            quantity_ordered=Decimal('10'), quantity_received=Decimal('10'),
        )
        return PurchaseOrder.objects.get(pk=po.pk)

    def _row(self):
        return next(r for r in supplier_reliability() if r['supplier_name'] == 'LeadTime Co')

    def test_no_expected_date_uses_default_lead_and_is_estimated(self):
        # ordered 30d ago, default lead 14 -> due today-16; completed today-12 -> late 4d
        self._po('PO-E1', expected=None, order_days_ago=30, completed_days_ago=12)
        row = self._row()
        self.assertEqual(row['assessable_count'], 1)
        self.assertEqual(row['estimated_count'], 1)
        self.assertEqual(row['estimated_share'], 1.0)
        self.assertEqual(row['late_count'], 1)
        self.assertEqual(row['avg_delay_days'], 4)

    def test_supplier_material_lead_time_overrides_default(self):
        SupplierMaterial.objects.create(
            supplier=self.supplier, material=self.material, lead_time_days=40,
        )
        # ordered 30d ago, lead 40 -> due 10d in the FUTURE; completed 20d ago -> early
        self._po('PO-E2', expected=None, order_days_ago=30, completed_days_ago=20)
        row = self._row()
        self.assertEqual(row['on_time_count'], 1)
        self.assertEqual(row['estimated_count'], 1)

    def test_explicit_expected_date_not_estimated(self):
        self._po('PO-E3', expected=date.today(), order_days_ago=5, completed_days_ago=1)
        row = self._row()
        self.assertEqual(row['estimated_count'], 0)
        self.assertEqual(row['on_time_count'], 1)

    def test_completed_date_is_the_arrival_signal(self):
        po = self._po('PO-E4', expected=date.today() - timedelta(days=3),
                      order_days_ago=10, completed_days_ago=1)
        # completed_date (today-1) is after expected (today-3) -> late by 2
        row = self._row()
        self.assertEqual(row['late_count'], 1)
        self.assertEqual(row['avg_delay_days'], 2)
        self.assertEqual(row['last_delivery'], po.completed_date)


class SupplierHubNavTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='hubnav', password='pw')

    def test_directory_page_links_to_scorecard(self):
        self.client.login(username='hubnav', password='pw')
        resp = self.client.get(reverse('supplier_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse('supplier_scorecard'))
        self.assertContains(resp, 'Supplier scorecard')

    def test_scorecard_lives_under_suppliers_path(self):
        self.assertEqual(reverse('supplier_scorecard'), '/catalog/suppliers/scorecard/')


class SystemSettingsViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.super = User.objects.create_superuser('root', 'root@x.com', 'pw')
        self.plain = User.objects.create_user(username='plain', password='pw')

    def test_requires_superuser(self):
        self.client.login(username='plain', password='pw')
        resp = self.client.get(reverse('system_settings'))
        self.assertRedirects(resp, reverse('dashboard'))

    def test_superuser_sees_registered_settings(self):
        self.client.login(username='root', password='pw')
        resp = self.client.get(reverse('system_settings'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'po_default_lead_time_days')
        self.assertContains(resp, 'Operational Settings')

    def test_post_updates_setting(self):
        self.client.login(username='root', password='pw')
        resp = self.client.post(reverse('system_settings'),
                                {'po_default_lead_time_days': '21'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(get_setting('po_default_lead_time_days'), 21)
        row = SystemSetting.objects.get(key='po_default_lead_time_days')
        self.assertEqual(row.updated_by, self.super)

    def test_post_rejects_invalid_value(self):
        self.client.login(username='root', password='pw')
        self.client.post(reverse('system_settings'),
                         {'po_default_lead_time_days': 'abc'}, follow=True)
        self.assertEqual(get_setting('po_default_lead_time_days'), 14)


class SalesOrderDeliveryRiskTests(TestCase):
    """Phase 1: sales_order_delivery_risk() classification and board."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='risk', password='pw')
        self.wh = Warehouse.objects.create(name='FG Store', location_type='Storage')
        self.product = Product.objects.create(
            name='Blend A', sku='PRD-A', unit_of_measure='pcs', price_per_unit=10,
        )

    def _so(self, number, *, status, deadline_in=None, ordered='10', shipped='0'):
        so = SalesOrder.objects.create(
            so_number=number, client_name=f'Client {number}',
            origin_warehouse=self.wh, status=status,
            fulfillment_deadline=(date.today() + timedelta(days=deadline_in)) if deadline_in is not None else None,
        )
        SalesOrderDetail.objects.create(
            sales_order=so, product=self.product,
            quantity_ordered=Decimal(ordered), quantity_shipped=Decimal(shipped),
        )
        return so

    def _ship(self, so, trk, *, eta_in=None, arrived_in=None, status='Dispatched'):
        return Shipment.objects.create(
            tracking_number=trk, direction='Outbound', status=status, sales_order=so,
            expected_eta_date=(date.today() + timedelta(days=eta_in)) if eta_in is not None else None,
            actual_arrival_date=(date.today() + timedelta(days=arrived_in)) if arrived_in is not None else None,
        )

    def _risk(self, so_number):
        return next(r for r in sales_order_delivery_risk() if r['so_number'] == so_number)

    def test_late_when_deadline_passed_and_nothing_shipped(self):
        self._so('SO-1', status='In Production', deadline_in=-5)
        row = self._risk('SO-1')
        self.assertEqual(row['risk'], 'late')
        self.assertEqual(row['days_slack'], -5)

    def test_at_risk_when_no_shipment_and_deadline_within_window(self):
        self._so('SO-2', status='In Production', deadline_in=3)
        self.assertEqual(self._risk('SO-2')['risk'], 'at_risk')

    def test_on_track_when_deadline_far_and_no_shipment(self):
        self._so('SO-3', status='Pending', deadline_in=30)
        self.assertEqual(self._risk('SO-3')['risk'], 'on_track')

    def test_on_track_when_eta_beats_deadline(self):
        so = self._so('SO-4', status='Shipped', deadline_in=10, shipped='10')
        self._ship(so, 'T-4', eta_in=5)
        self.assertEqual(self._risk('SO-4')['risk'], 'on_track')

    def test_late_when_fully_shipped_but_eta_misses_deadline(self):
        so = self._so('SO-5', status='Shipped', deadline_in=2, shipped='10')
        self._ship(so, 'T-5', eta_in=6)
        self.assertEqual(self._risk('SO-5')['risk'], 'late')

    def test_at_risk_when_partial_shipment_eta_misses_deadline(self):
        so = self._so('SO-6', status='Partially Shipped', deadline_in=2, ordered='10', shipped='4')
        self._ship(so, 'T-6', eta_in=6)
        self.assertEqual(self._risk('SO-6')['risk'], 'at_risk')

    def test_no_deadline(self):
        self._so('SO-7', status='In Production', deadline_in=None)
        self.assertEqual(self._risk('SO-7')['risk'], 'no_deadline')

    def test_delivered_and_draft_excluded(self):
        self._so('SO-8', status='Delivered', deadline_in=1)
        self._so('SO-9', status='Draft', deadline_in=1)
        numbers = [r['so_number'] for r in sales_order_delivery_risk()]
        self.assertNotIn('SO-8', numbers)
        self.assertNotIn('SO-9', numbers)

    def test_sorted_worst_first(self):
        self._so('SO-A', status='Pending', deadline_in=30)      # on_track
        self._so('SO-B', status='In Production', deadline_in=-2)  # late
        self.assertEqual(sales_order_delivery_risk()[0]['so_number'], 'SO-B')

    def test_production_summary(self):
        so = self._so('SO-C', status='In Production', deadline_in=10)
        ProductionRun.objects.create(run_number='R-C1', target_product=self.product,
                                     expected_yield=Decimal('10'), status='Completed', sales_order=so)
        ProductionRun.objects.create(run_number='R-C2', target_product=self.product,
                                     expected_yield=Decimal('5'), status='InProgress', sales_order=so)
        self.assertEqual(self._risk('SO-C')['production_summary'], '2 runs, 1 completed')

    def test_at_risk_window_setting_changes_classification(self):
        self._so('SO-D', status='In Production', deadline_in=10)
        self.assertEqual(self._risk('SO-D')['risk'], 'on_track')  # 10 > default window 7
        SystemSetting.objects.update_or_create(
            key='so_at_risk_window_days', defaults={'value': '14', 'value_type': 'int'})
        self.assertEqual(self._risk('SO-D')['risk'], 'at_risk')   # 10 <= 14

    def test_view_and_filter(self):
        self._so('SO-E', status='In Production', deadline_in=-1)  # late
        self._so('SO-F', status='Pending', deadline_in=40)        # on_track
        self.client.login(username='risk', password='pw')
        resp = self.client.get(reverse('so_delivery_risk'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'SO-E')
        self.assertContains(resp, 'SO-F')
        resp = self.client.get(reverse('so_delivery_risk') + '?risk=late')
        self.assertContains(resp, 'SO-E')
        self.assertNotContains(resp, 'SO-F')

    def test_so_list_links_to_delivery_risk(self):
        self.client.login(username='risk', password='pw')
        resp = self.client.get(reverse('so_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse('so_delivery_risk'))
        self.assertContains(resp, 'Delivery risk board')


class AuditAccuracyTests(TestCase):
    """Phase 1: audit_accuracy() roll-up and board."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='auditor', password='pw')
        self.wh1 = Warehouse.objects.create(name='WH North', location_type='Storage')
        self.wh2 = Warehouse.objects.create(name='WH South', location_type='Storage')
        self.material = Material.objects.create(
            name='Zinc', sku='MAT-Z', category='Metals', unit_of_measure='kg', safe_storage_days=365,
        )

    def _batch(self, num, wh, qty='100'):
        return Batch.objects.create(
            batch_number=num, material=self.material, quantity=Decimal(qty), status='Active',
            warehouse=wh, manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=200),
        )

    def _audit(self, batch, expected, actual, months_ago=0):
        a = StockAudit.objects.create(
            batch=batch, expected_quantity=Decimal(str(expected)),
            actual_quantity=Decimal(str(actual)), auditor=self.user, status='Resolved',
        )
        if months_ago:
            when = timezone.now() - timedelta(days=30 * months_ago)
            StockAudit.objects.filter(pk=a.pk).update(audit_date=when)
        return a

    def _wh_row(self, name):
        return next(r for r in audit_accuracy()['by_warehouse'] if r['name'] == name)

    def test_exact_match_counts_toward_accuracy(self):
        b = self._batch('BZ-1', self.wh1)
        self._audit(b, 100, 100)
        self._audit(b, 100, 95)
        row = self._wh_row('WH North')
        self.assertEqual(row['audit_count'], 2)
        self.assertEqual(row['exact_count'], 1)
        self.assertEqual(row['accuracy_rate'], 0.5)

    def test_shrinkage_and_overage_split(self):
        b = self._batch('BZ-2', self.wh1)
        self._audit(b, 100, 90)   # -10 shrink
        self._audit(b, 100, 105)  # +5 overage
        row = self._wh_row('WH North')
        self.assertEqual(row['shrinkage'], -10.0)
        self.assertEqual(row['overage'], 5.0)
        self.assertEqual(row['net_variance'], -5.0)

    def test_grouped_by_warehouse(self):
        self._audit(self._batch('BZ-3', self.wh1), 100, 100)
        self._audit(self._batch('BZ-4', self.wh2), 100, 80)
        names = {r['name'] for r in audit_accuracy()['by_warehouse']}
        self.assertEqual(names, {'WH North', 'WH South'})

    def test_chronic_shrinkage_flag(self):
        b = self._batch('BZ-5', self.wh2)
        for _ in range(3):
            self._audit(b, 100, 90)
        self.assertTrue(self._wh_row('WH South')['chronic_shrinkage'])

    def test_trend_buckets_by_month(self):
        b = self._batch('BZ-6', self.wh1)
        self._audit(b, 100, 90, months_ago=0)
        self._audit(b, 100, 80, months_ago=2)
        buckets = [t['bucket'] for t in audit_accuracy()['trend']]
        self.assertEqual(len(buckets), 2)
        self.assertEqual(buckets, sorted(buckets))

    def test_sorted_worst_accuracy_first(self):
        self._audit(self._batch('BZ-7', self.wh1), 100, 100)   # WH North 100%
        b2 = self._batch('BZ-8', self.wh2)
        self._audit(b2, 100, 70)
        self._audit(b2, 100, 100)                              # WH South 50%
        self.assertEqual(audit_accuracy()['by_warehouse'][0]['name'], 'WH South')

    def test_window_filter(self):
        b = self._batch('BZ-9', self.wh1)
        self._audit(b, 100, 90, months_ago=0)
        self._audit(b, 100, 50, months_ago=6)
        recent = audit_accuracy(since=date.today() - timedelta(days=90))
        self.assertEqual(recent['by_warehouse'][0]['audit_count'], 1)

    def test_view_renders(self):
        self._audit(self._batch('BZ-10', self.wh1), 100, 88)
        self.client.login(username='auditor', password='pw')
        resp = self.client.get(reverse('audit_accuracy'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'WH North')
        self.assertContains(resp, 'MAT-Z')


class ProductionYieldVarianceTests(TestCase):
    """Phase 1: production_yield_variance() roll-up and board."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='sup', password='pw')
        self.sup2 = User.objects.create_user(username='sup2', password='pw')
        self.product = Product.objects.create(
            name='Compound X', sku='PRD-X', unit_of_measure='kg', price_per_unit=5,
        )
        self.material = Material.objects.create(
            name='Filler', sku='MAT-F', category='Bulk', unit_of_measure='kg', safe_storage_days=365,
        )

    def _run(self, num, *, expected, actual, status='Completed', supervisor=None,
             mat_expected=None, mat_actual=None):
        run = ProductionRun.objects.create(
            run_number=num, target_product=self.product,
            expected_yield=Decimal(str(expected)),
            actual_yield=Decimal(str(actual)) if actual is not None else None,
            status=status, supervisor=supervisor,
        )
        if mat_expected is not None:
            RunMaterialUsage.objects.create(
                production_run=run, material=self.material,
                expected_qty=Decimal(str(mat_expected)), actual_qty=Decimal(str(mat_actual)),
            )
        return run

    def _prod_row(self):
        return production_yield_variance()['by_product'][0]

    def test_under_yield_rated_poor(self):
        self._run('RY-1', expected=100, actual=83, supervisor=self.user)
        row = self._prod_row()
        self.assertAlmostEqual(row['mean_yield_variance_pct'], -17.0)
        self.assertEqual(row['rating'], 'poor')

    def test_on_plan_rated_good(self):
        self._run('RY-2', expected=100, actual=100, supervisor=self.user,
                  mat_expected=50, mat_actual=50)
        self.assertEqual(self._prod_row()['rating'], 'good')

    def test_material_overuse_rated_poor(self):
        self._run('RY-3', expected=100, actual=100, supervisor=self.user,
                  mat_expected=100, mat_actual=125)  # +25% variance_pct
        row = self._prod_row()
        self.assertAlmostEqual(row['mean_material_overuse_pct'], 25.0)
        self.assertEqual(row['rating'], 'poor')

    def test_grouped_by_supervisor_including_unassigned(self):
        self._run('RY-4', expected=100, actual=95, supervisor=self.user)
        self._run('RY-5', expected=100, actual=95, supervisor=None)
        names = {r['name'] for r in production_yield_variance()['by_supervisor']}
        self.assertEqual(names, {'sup', 'Unassigned'})

    def test_excludes_incomplete_and_missing_yield(self):
        self._run('RY-6', expected=100, actual=None, status='Completed', supervisor=self.user)
        self._run('RY-7', expected=100, actual=90, status='InProgress', supervisor=self.user)
        self.assertEqual(production_yield_variance()['by_product'], [])

    def test_sorted_worst_first(self):
        self._run('RY-8', expected=100, actual=100, supervisor=self.user)   # product row aggregates
        data = production_yield_variance()['by_supervisor']
        self._run('RY-9', expected=100, actual=70, supervisor=self.sup2)
        data = production_yield_variance()['by_supervisor']
        self.assertEqual(data[0]['name'], 'sup2')

    def test_view_renders(self):
        self._run('RY-10', expected=100, actual=92, supervisor=self.user)
        self.client.login(username='sup', password='pw')
        resp = self.client.get(reverse('production_yield'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'PRD-X')
        self.assertContains(resp, 'By supervisor')




class StockoutForecastTests(TestCase):
    """Phase 2a: stockout_forecast() days-of-cover + reorder-by."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='planner', password='pw')
        self.wh = Warehouse.objects.create(name='RM Depot', location_type='Storage')

    def _material(self, sku, safe_days=90):
        return Material.objects.create(
            name=f'Mat {sku}', sku=sku, category='X', unit_of_measure='kg',
            safe_storage_days=safe_days,
        )

    def _stock(self, material, qty, allocated=0):
        return Batch.objects.create(
            batch_number=f'B-{material.sku}-{qty}', material=material, quantity=Decimal(str(qty)),
            allocated_quantity=Decimal(str(allocated)), status='Active', warehouse=self.wh,
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=300),
        )

    def _consume(self, material, total, window_days=30):
        """One consumption RegistryLog of `total`, dated inside the window."""
        row = RegistryLog.objects.create(
            action_type='Consumed_For_Manufacturing', item_name=f'{material.name} (Run X)',
            material=material, quantity_changed=Decimal(str(total)), warehouse=self.wh,
        )
        RegistryLog.objects.filter(pk=row.pk).update(
            timestamp=timezone.now() - timedelta(days=window_days // 2)
        )

    def _lead(self, material, days):
        s = Supplier.objects.create(name=f'Sup {material.sku}')
        SupplierMaterial.objects.create(supplier=s, material=material, lead_time_days=days)

    def _row(self, sku, **kw):
        return next(r for r in stockout_forecast(**kw) if r['sku'] == sku)

    def test_no_consumption_is_no_usage(self):
        m = self._material('MK-1')
        self._stock(m, 500)
        row = self._row('MK-1')
        self.assertEqual(row['status'], 'no_usage')
        self.assertIsNone(row['days_cover'])
        self.assertIsNone(row['reorder_by_date'])

    def test_burn_rate_and_days_cover(self):
        m = self._material('MK-2')
        self._stock(m, 200)
        self._consume(m, 300)          # 300 / 30 = 10 per day
        row = self._row('MK-2')
        self.assertAlmostEqual(row['daily_rate'], 10.0)
        self.assertAlmostEqual(row['days_cover'], 20.0)

    def test_critical_when_cover_below_lead_time(self):
        m = self._material('MK-3')
        self._stock(m, 80)
        self._consume(m, 300)          # 10/day -> 8 days cover
        self._lead(m, 14)
        row = self._row('MK-3')
        self.assertEqual(row['status'], 'critical')
        self.assertFalse(row['lead_time_estimated'])

    def test_watch_band(self):
        m = self._material('MK-4')
        self._stock(m, 200)
        self._consume(m, 300)          # 10/day -> 20 days cover; lead 10 -> reorder in 10d
        self._lead(m, 10)
        self.assertEqual(self._row('MK-4')['status'], 'watch')

    def test_ok_when_far_out(self):
        m = self._material('MK-5')
        self._stock(m, 1000)
        self._consume(m, 300)          # 10/day -> 100 days cover
        self._lead(m, 10)
        self.assertEqual(self._row('MK-5')['status'], 'ok')

    def test_already_out_is_critical(self):
        m = self._material('MK-6')
        self._stock(m, 100, allocated=100)   # available 0
        self._consume(m, 300)
        row = self._row('MK-6')
        self.assertEqual(row['status'], 'critical')
        self.assertEqual(row['days_cover'], 0.0)

    def test_lead_time_estimated_flag_and_default(self):
        m = self._material('MK-7')
        self._stock(m, 500)
        self._consume(m, 300)
        row = self._row('MK-7')
        self.assertTrue(row['lead_time_estimated'])
        self.assertEqual(row['lead_time_days'], get_setting('po_default_lead_time_days'))

    def test_allocated_reduces_available(self):
        m = self._material('MK-8')
        self._stock(m, 300, allocated=100)
        self._consume(m, 300)          # 10/day, available 200 -> 20 days cover
        self.assertAlmostEqual(self._row('MK-8')['days_cover'], 20.0)

    def test_on_order_from_open_pos(self):
        m = self._material('MK-9')
        self._stock(m, 500)
        po = PurchaseOrder.objects.create(
            po_number='PO-MK9', supplier_name='S', target_warehouse=self.wh, status='Pending',
        )
        PurchaseOrderDetail.objects.create(
            purchase_order=po, material=m, quantity_ordered=Decimal('400'),
            quantity_received=Decimal('150'),
        )
        self.assertEqual(self._row('MK-9')['on_order'], 250.0)

    def test_sorted_worst_first(self):
        crit = self._material('MK-C'); self._stock(crit, 20); self._consume(crit, 300); self._lead(crit, 14)
        idle = self._material('MK-I'); self._stock(idle, 500)
        rows = stockout_forecast()
        self.assertEqual(rows[0]['sku'], 'MK-C')
        self.assertEqual(rows[-1]['status'], 'no_usage')

    def test_view_renders_and_filters(self):
        crit = self._material('MK-V'); self._stock(crit, 20); self._consume(crit, 300); self._lead(crit, 14)
        idle = self._material('MK-Z'); self._stock(idle, 500)
        self.client.login(username='planner', password='pw')
        resp = self.client.get(reverse('forecast'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'MK-V')
        self.assertContains(resp, 'MK-Z')
        resp = self.client.get(reverse('forecast') + '?status=critical')
        self.assertContains(resp, 'MK-V')
        self.assertNotContains(resp, 'MK-Z')

    def test_view_requires_login(self):
        self.assertEqual(self.client.get(reverse('forecast')).status_code, 302)


from datetime import date, timedelta
from core.models import RegistryLog


class BatchDetailEditTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='warehouseop', password='password123')
        self.client.login(username='warehouseop', password='password123')

        self.warehouse = Warehouse.objects.create(
            name="Batch Edit Facility",
            location_type="Storage",
            ownership_type="Internal",
            rental_billing_method="Usage",
            rental_cost_per_mt=0.00,
            total_capacity_mt=1000.00,
        )
        self.material = Material.objects.create(
            name="Edit Test Material",
            sku="MATEDIT1",
            category="Chemicals",
            unit_of_measure="MT",
            safe_storage_days=90,
            weight_mt_per_unit=1.0,
            cost_per_unit=50.00,
        )
        self.batch = Batch.objects.create(
            batch_number="BATCH-EDIT-001",
            status="Active",
            material=self.material,
            quantity=100,
            manufacturing_date=date.today() - timedelta(days=10),
            expiry_date=date.today() + timedelta(days=80),
            warehouse=self.warehouse,
            location="Zone A",
        )

    def _post_update(self, **overrides):
        data = {
            'action': 'update_batch',
            'status': self.batch.status,
            'expiry_date': self.batch.expiry_date.strftime('%Y-%m-%d'),
            'location': self.batch.location,
        }
        data.update(overrides)
        url = reverse('batch_detail', kwargs={'batch_number': self.batch.batch_number})
        return self.client.post(url, data)

    def test_update_batch_status_logs_change_without_error(self):
        response = self._post_update(status='Quarantined')
        self.assertEqual(response.status_code, 302)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 'Quarantined')

        log = RegistryLog.objects.get(action_type='Adjusted', item_name__icontains='BATCH-EDIT-001')
        self.assertIn('Status changed to Quarantined', log.item_name)
        self.assertEqual(log.warehouse, self.warehouse)
        self.assertEqual(log.user, self.user)

    def test_update_batch_multiple_fields_logged_and_visible_in_history(self):
        new_expiry = (self.batch.expiry_date + timedelta(days=30)).strftime('%Y-%m-%d')
        response = self._post_update(status='Quarantined', expiry_date=new_expiry, location='Zone B')
        self.assertEqual(response.status_code, 302)

        log = RegistryLog.objects.get(action_type='Adjusted', item_name__icontains='BATCH-EDIT-001')
        self.assertIn('Status changed to Quarantined', log.item_name)
        self.assertIn('Expiry updated to', log.item_name)
        self.assertIn('Location updated', log.item_name)

        # The batch detail page filters its history by item_name__icontains=batch_number,
        # so the folded summary must still surface there.
        history_url = reverse('batch_detail', kwargs={'batch_number': self.batch.batch_number})
        page = self.client.get(history_url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Status changed to Quarantined')

    def test_update_batch_no_changes_writes_no_log(self):
        response = self._post_update()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(RegistryLog.objects.filter(item_name__icontains='BATCH-EDIT-001').exists())


class CapacityForecastTests(TestCase):
    """Phase 2b: warehouse_utilization(), snapshot_utilization command, capacity_forecast()."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='capacity', password='pw')
        self.wh = Warehouse.objects.create(
            name='Depot 1', location_type='Storage', total_capacity_mt=Decimal('1000'),
        )
        self.material = Material.objects.create(
            name='Sand', sku='MAT-S', category='Bulk', unit_of_measure='MT',
            safe_storage_days=365, weight_mt_per_unit=Decimal('1.0'),
        )

    def _batch(self, qty):
        return Batch.objects.create(
            batch_number=f'B-{self.wh.id}-{qty}', material=self.material,
            quantity=Decimal(str(qty)), status='Active', warehouse=self.wh,
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=300),
        )

    def _snap(self, pct, days_ago):
        return WarehouseUtilizationSnapshot.objects.create(
            warehouse=self.wh, snapshot_date=date.today() - timedelta(days=days_ago),
            used_mt=Decimal(str(pct * 10)), capacity_mt=Decimal('1000'),
            utilization_percent=Decimal(str(pct)),
        )

    def _row(self):
        return next(r for r in capacity_forecast() if r['warehouse_id'] == self.wh.id)

    def test_warehouse_utilization_weighted_by_unit_weight(self):
        self._batch(300)  # 300 MT into a 1000 MT warehouse -> 30%
        row = next(r for r in warehouse_utilization() if r['warehouse_id'] == self.wh.id)
        self.assertAlmostEqual(row['used_mt'], 300.0)
        self.assertAlmostEqual(row['utilization_percent'], 30.0)

    def test_snapshot_command_is_idempotent_per_day(self):
        from django.core.management import call_command
        self._batch(400)
        call_command('snapshot_utilization')
        call_command('snapshot_utilization')
        rows = WarehouseUtilizationSnapshot.objects.filter(warehouse=self.wh)
        self.assertEqual(rows.count(), 1)
        self.assertAlmostEqual(float(rows.first().utilization_percent), 40.0)

    def test_no_data_below_three_snapshots(self):
        self._snap(50, 2)
        self._snap(55, 1)
        self.assertEqual(self._row()['status'], 'no_data')

    def test_filling_trend_projects_full_date(self):
        # 60 -> 70 -> 80 over 20 days: ~1 pp/day, 20 days to 100
        self._snap(60, 20)
        self._snap(70, 10)
        self._snap(80, 0)
        row = self._row()
        self.assertEqual(row['current_percent'], 80.0)
        self.assertAlmostEqual(row['weekly_rate_pp'], 7.0, places=1)
        self.assertAlmostEqual(row['days_to_full'], 20.0, places=0)
        self.assertIsNotNone(row['projected_full_date'])
        self.assertEqual(row['status'], 'watch')  # full in ~20d -> within 60, past 14

    def test_fast_fill_is_critical(self):
        self._snap(80, 6)
        self._snap(90, 3)
        self._snap(97, 0)  # already >= 95
        self.assertEqual(self._row()['status'], 'critical')

    def test_flat_trend_is_stable(self):
        self._snap(50, 20)
        self._snap(50, 10)
        self._snap(50, 0)
        row = self._row()
        self.assertEqual(row['status'], 'stable')
        self.assertIsNone(row['projected_full_date'])

    def test_view_renders_with_history_banner(self):
        self._snap(60, 2)
        self._snap(65, 1)
        self._snap(70, 0)
        self.client.login(username='capacity', password='pw')
        resp = self.client.get(reverse('capacity_forecast'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Depot 1')
        self.assertContains(resp, 'Collecting data')  # 3 days < 7

    def test_view_empty_state(self):
        self.client.login(username='capacity', password='pw')
        resp = self.client.get(reverse('capacity_forecast'))
        self.assertContains(resp, 'No snapshots yet')

    def test_view_requires_login(self):
        self.assertEqual(self.client.get(reverse('capacity_forecast')).status_code, 302)

    def test_nav_and_facility_callout(self):
        self.client.login(username='capacity', password='pw')
        # facility page links to it
        fac = self.client.get(reverse('warehouse_list'))
        self.assertContains(fac, reverse('capacity_forecast'))
        self.assertContains(fac, 'Capacity runway')


import os as _os
import types as _types
from unittest import mock as _mock

from core.models import OpsBriefing
from core import briefing as briefing_mod


class _FakeUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text, model="claude-opus-5", i=1234, o=210):
        self.content = [_FakeTextBlock(text)]
        self.usage = _FakeUsage(i, o)
        self.model = model


class _FakeAnthropicModule:
    """Stand-in for the top-level ``anthropic`` module used by core.briefing."""

    def __init__(self, response=None, boom=None):
        self._response = response or _FakeResponse(
            "Depot 1 fills in 12 days.\n- Depot 1 at 92% and rising\nWatch: SO-9 slack -1d"
        )
        self._boom = boom
        self.calls = []

    def Anthropic(self, *args, **kwargs):
        module = self

        class _Client:
            def __init__(self):
                self.messages = _Messages()

        class _Messages:
            def create(self, **kw):
                module.calls.append(kw)
                if module._boom is not None:
                    raise module._boom
                return module._response

        return _Client()


class OpsBriefingTests(TestCase):
    """Phase 3: collect_signals(), generate_briefing(), the command and the view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='briefer', password='pw')
        self.manager = User.objects.create_user(username='mgr', password='pw', role='Manager')
        self.wh = Warehouse.objects.create(name='FG Store', location_type='Storage')
        self.product = Product.objects.create(
            name='Blend A', sku='PRD-A', unit_of_measure='pcs', price_per_unit=10,
        )

    def _late_so(self, number='SO-LATE'):
        so = SalesOrder.objects.create(
            so_number=number, client_name='Acme', origin_warehouse=self.wh,
            status='In Production', fulfillment_deadline=date.today() - timedelta(days=9),
        )
        SalesOrderDetail.objects.create(
            sales_order=so, product=self.product,
            quantity_ordered=Decimal('10'), quantity_shipped=Decimal('0'),
        )
        return so

    # -- collect_signals -----------------------------------------------------

    def test_collect_signals_keeps_only_notable_rows(self):
        self._late_so('SO-LATE')
        SalesOrder.objects.create(  # on-track, far-off deadline -> must be excluded
            so_number='SO-OK', client_name='Fine', origin_warehouse=self.wh,
            status='Pending', fulfillment_deadline=date.today() + timedelta(days=90),
        )
        signals = briefing_mod.collect_signals()
        risk = signals['sales_order_delivery_risk']
        self.assertEqual([r['sales_order'] for r in risk], ['SO-LATE'])
        self.assertGreaterEqual(signals['signal_count'], 1)
        self.assertEqual(signals['generated_for'], date.today().isoformat())

    def test_collect_signals_empty_when_nothing_notable(self):
        signals = briefing_mod.collect_signals()
        self.assertEqual(signals['signal_count'], 0)
        self.assertEqual(signals['sales_order_delivery_risk'], [])

    # -- generate_briefing -------------------------------------------------

    def test_empty_signals_records_empty_without_api_call(self):
        fake = _FakeAnthropicModule()
        with _mock.patch.object(briefing_mod, 'anthropic', fake):
            b = briefing_mod.generate_briefing()
        self.assertEqual(b.status, 'empty')
        self.assertEqual(fake.calls, [])

    def test_missing_api_key_records_error_not_exception(self):
        self._late_so()
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {}, clear=False):
            _os.environ.pop('ANTHROPIC_API_KEY', None)
            b = briefing_mod.generate_briefing()
        self.assertEqual(b.status, 'error')
        self.assertIn('ANTHROPIC_API_KEY', b.error_detail)

    def test_package_missing_records_error(self):
        self._late_so()
        with _mock.patch.object(briefing_mod, 'anthropic', None), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_briefing()
        self.assertEqual(b.status, 'error')
        self.assertIn('anthropic', b.error_detail)

    def test_disabled_setting_records_skipped(self):
        self._late_so()
        SystemSetting.objects.update_or_create(
            key='ops_briefing_enabled',
            defaults={'value': 'False', 'value_type': 'bool'},
        )
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()) as _f, \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_briefing()
        self.assertEqual(b.status, 'skipped')

    def test_successful_generation_stores_body_and_tokens(self):
        self._late_so()
        fake = _FakeAnthropicModule()
        with _mock.patch.object(briefing_mod, 'anthropic', fake), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_briefing(period='daily', user=self.manager)
        self.assertEqual(b.status, 'ok')
        self.assertEqual(b.headline, 'Depot 1 fills in 12 days.')
        self.assertIn('Depot 1 at 92%', b.body_text)
        self.assertEqual(b.input_tokens, 1234)
        self.assertEqual(b.output_tokens, 210)
        self.assertEqual(b.model_id, 'claude-opus-5')
        self.assertEqual(b.generated_by, self.manager)
        # the model was handed the signal JSON, not a DB handle
        sent = fake.calls[0]['messages'][0]['content']
        self.assertIn('sales_order_delivery_risk', sent)
        self.assertIn('SO-LATE', sent)

    def test_api_exception_is_caught_and_recorded(self):
        self._late_so()
        fake = _FakeAnthropicModule(boom=RuntimeError('rate limited'))
        with _mock.patch.object(briefing_mod, 'anthropic', fake), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_briefing()
        self.assertEqual(b.status, 'error')
        self.assertIn('rate limited', b.error_detail)

    def test_model_id_comes_from_system_setting(self):
        self._late_so()
        SystemSetting.objects.update_or_create(
            key='ops_briefing_model',
            defaults={'value': 'claude-haiku-4-5', 'value_type': 'str'},
        )
        fake = _FakeAnthropicModule(response=_FakeResponse('Head.\n- point', model='claude-haiku-4-5'))
        with _mock.patch.object(briefing_mod, 'anthropic', fake), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            briefing_mod.generate_briefing()
        self.assertEqual(fake.calls[0]['model'], 'claude-haiku-4-5')

    # -- command ----------------------------------------------------------

    def test_command_dry_run_prints_signals_and_saves_nothing(self):
        from django.core.management import call_command
        from io import StringIO
        self._late_so()
        out = StringIO()
        call_command('generate_ops_briefing', '--dry-run', stdout=out)
        self.assertIn('sales_order_delivery_risk', out.getvalue())
        self.assertEqual(OpsBriefing.objects.count(), 0)

    def test_command_generates_row(self):
        from django.core.management import call_command
        from io import StringIO
        self._late_so()
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            call_command('generate_ops_briefing', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(OpsBriefing.objects.filter(status='ok').count(), 1)

    # -- view -----------------------------------------------------------

    def test_view_requires_login(self):
        self.assertEqual(self.client.get(reverse('ops_briefing')).status_code, 302)

    def test_view_empty_state(self):
        self.client.login(username='briefer', password='pw')
        resp = self.client.get(reverse('ops_briefing'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No briefing yet')

    def test_view_renders_latest_ok_briefing(self):
        OpsBriefing.objects.create(
            status='ok', headline='Depot 1 fills in 12 days.',
            body_text='- Depot 1 at 92% and rising\nWatch: SO-9 slack -1d',
            signal_count=3, model_id='claude-opus-5',
            signals_json={'capacity_runway': [{'warehouse': 'Depot 1'}]},
        )
        self.client.login(username='briefer', password='pw')
        resp = self.client.get(reverse('ops_briefing'))
        self.assertContains(resp, 'Depot 1 fills in 12 days.')
        self.assertContains(resp, 'Depot 1 at 92% and rising')
        self.assertContains(resp, 'SO-9 slack -1d')  # watch line
        self.assertContains(resp, reverse('capacity_forecast'))  # signal back-link

    def test_generate_button_hidden_for_plain_user_and_post_forbidden(self):
        self.client.login(username='briefer', password='pw')
        resp = self.client.get(reverse('ops_briefing'))
        self.assertNotContains(resp, 'Generate now')
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            self.client.post(reverse('ops_briefing'), {'period': 'daily'})
        self.assertEqual(OpsBriefing.objects.count(), 0)

    def test_manager_can_generate_from_view(self):
        self._late_so()
        self.client.login(username='mgr', password='pw')
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            resp = self.client.post(reverse('ops_briefing'), {'period': 'daily'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(OpsBriefing.objects.filter(status='ok', generated_by=self.manager).count(), 1)

    def test_dead_nav_link_is_now_wired(self):
        self.client.login(username='briefer', password='pw')
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, reverse('ops_briefing'))
        self.assertContains(resp, 'Digital Assistant (AI)')
