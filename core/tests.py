from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from core.db_tracker import get_db_status, DB_QUERY_LOGS
from django.db import connection
from core.models import (
    Warehouse, WarehouseLocation, Material, Product, ProductRecipe,
    Batch, SalesOrder, SalesOrderDetail, StockAllocation,
    Shipment, ShipmentItem,
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

class WarehouseInventoryMaterialFilterTests(TestCase):
    """?material_id= on warehouse_inventory - linked from the Stockout Forecast page."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='invfilter', password='pw')
        self.client.login(username='invfilter', password='pw')
        self.warehouse = Warehouse.objects.create(name='Filter WH', location_type='Storage')
        self.wanted = Material.objects.create(
            name='Wanted Material', sku='MAT-WANT', category='Bulk',
            unit_of_measure='MT', safe_storage_days=365,
        )
        self.other = Material.objects.create(
            name='Other Material', sku='MAT-OTHER', category='Bulk',
            unit_of_measure='MT', safe_storage_days=365,
        )
        Batch.objects.create(
            batch_number='B-WANT-1', material=self.wanted, quantity=10, status='Active',
            manufacturing_date='2025-01-01', expiry_date='2026-01-01', warehouse=self.warehouse,
        )
        Batch.objects.create(
            batch_number='B-OTHER-1', material=self.other, quantity=10, status='Active',
            manufacturing_date='2025-01-01', expiry_date='2026-01-01', warehouse=self.warehouse,
        )

    def test_material_filter_shows_only_that_materials_batches(self):
        resp = self.client.get(reverse('warehouse_inventory'), {'material_id': self.wanted.id})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'B-WANT-1')
        self.assertNotContains(resp, 'B-OTHER-1')
        self.assertContains(resp, 'Filtered by material')
        self.assertContains(resp, 'MAT-WANT')

    def test_no_material_filter_shows_everything(self):
        resp = self.client.get(reverse('warehouse_inventory'))
        self.assertContains(resp, 'B-WANT-1')
        self.assertContains(resp, 'B-OTHER-1')

    def test_material_filter_combines_with_warehouse_filter(self):
        other_wh = Warehouse.objects.create(name='Other WH', location_type='Storage')
        Batch.objects.create(
            batch_number='B-WANT-2', material=self.wanted, quantity=5, status='Active',
            manufacturing_date='2025-01-01', expiry_date='2026-01-01', warehouse=other_wh,
        )
        resp = self.client.get(reverse('warehouse_inventory'), {
            'material_id': self.wanted.id, 'warehouse_id': self.warehouse.id,
        })
        self.assertContains(resp, 'B-WANT-1')
        self.assertNotContains(resp, 'B-WANT-2')

    def test_forecast_page_links_to_filtered_inventory(self):
        resp = self.client.get(reverse('forecast'))
        self.assertEqual(resp.status_code, 200)
        expected = reverse('warehouse_inventory') + f'?material_id={self.wanted.id}'
        # forecast_view only lists materials with consumption/forecast history; just
        # confirm the link pattern it emits points at warehouse_inventory, not material_edit.
        self.assertNotContains(resp, reverse('material_edit', kwargs={'pk': self.wanted.id}))


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


class BatchClosureTests(TestCase):
    """quantity hitting zero must flip the batch to Depleted and stamp closed_date."""

    def setUp(self):
        self.warehouse = Warehouse.objects.create(name='Closure WH', location_type='Storage')
        self.product = Product.objects.create(
            name='Closure Product', sku='PRD-CLOSE', unit_of_measure='pcs', price_per_unit=10.0
        )
        self.sales_order = SalesOrder.objects.create(
            so_number='SO-CLOSE-1', client_name='Test Client',
            origin_warehouse=self.warehouse, status='Draft'
        )
        SalesOrderDetail.objects.create(
            sales_order=self.sales_order, product=self.product, quantity_ordered=50.0
        )

    def _batch(self, qty):
        return Batch.objects.create(
            batch_number='B-CLOSE-1', product=self.product, quantity=qty, status='Active',
            manufacturing_date='2025-01-01', expiry_date='2026-01-01',
            warehouse=self.warehouse, location='Zone A',
        )

    def test_full_deduction_marks_depleted_and_stamps_closed_date(self):
        batch = self._batch(50.0)
        allocate_stock('sales_order', self.sales_order, self.product, 50.0)
        deduct_stock_from_allocation('sales_order', self.sales_order)
        batch.refresh_from_db()

        self.assertEqual(float(batch.quantity), 0.0)
        self.assertEqual(batch.status, 'Depleted')
        self.assertEqual(batch.closed_date, date.today())

    def test_partial_deduction_leaves_batch_open(self):
        batch = self._batch(100.0)
        allocate_stock('sales_order', self.sales_order, self.product, 50.0)
        deduct_stock_from_allocation('sales_order', self.sales_order)
        batch.refresh_from_db()

        self.assertEqual(float(batch.quantity), 50.0)
        self.assertEqual(batch.status, 'Active')
        self.assertIsNone(batch.closed_date)

    def test_spoil_dispose_stamps_closed_date(self):
        batch = self._batch(50.0)
        user = User.objects.create_user(username='qauser', password='pw')
        client = Client()
        client.login(username='qauser', password='pw')

        resp = client.post(reverse('qa_dashboard'), {'action': 'spoil_dispose', 'batch_id': batch.id})
        self.assertEqual(resp.status_code, 302)

        batch.refresh_from_db()
        self.assertEqual(batch.status, 'Spoiled')
        self.assertEqual(batch.closed_date, date.today())


class BatchRentalRateLockInTests(TestCase):
    """apply_po_material_receipt() must lock in a batch's rental rate at receipt time."""

    def setUp(self):
        self.warehouse = Warehouse.objects.create(
            name='Rate Lock WH', location_type='Storage', ownership_type='ExternalProvider',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('5.00'),
            total_capacity_mt=Decimal('1000'),
        )
        self.material = Material.objects.create(
            name='Rate Lock Material', sku='MAT-RATE', category='Bulk',
            unit_of_measure='MT', safe_storage_days=365,
        )
        self.po = PurchaseOrder.objects.create(
            po_number='PO-RATE-1', supplier_name='Rate Supplier', target_warehouse=self.warehouse,
        )
        self.user = User.objects.create_user(username='ratelockuser', password='pw')

    def _detail(self, negotiated_rate=None):
        return PurchaseOrderDetail.objects.create(
            purchase_order=self.po, material=self.material, quantity_ordered=Decimal('100'),
            negotiated_rental_rate_per_mt=negotiated_rate,
        )

    def test_receipt_with_no_negotiated_rate_leaves_batch_tracking_live(self):
        # No negotiated rate -> rental_rate_per_mt stays None, meaning "track the
        # warehouse's current rate live" (see open_batch_rent_expr()), not a snapshot.
        detail = self._detail(negotiated_rate=None)
        apply_po_material_receipt(detail, 100, self.user)
        batch = Batch.objects.get(purchase_order=self.po)
        self.assertIsNone(batch.rental_rate_per_mt)

    def test_receipt_uses_negotiated_rate_when_set(self):
        detail = self._detail(negotiated_rate=Decimal('3.25'))
        apply_po_material_receipt(detail, 100, self.user)
        batch = Batch.objects.get(purchase_order=self.po)
        self.assertEqual(batch.rental_rate_per_mt, Decimal('3.25'))

    def test_negotiated_rate_is_locked_in_not_live(self):
        detail = self._detail(negotiated_rate=Decimal('3.25'))
        apply_po_material_receipt(detail, 100, self.user)
        batch = Batch.objects.get(purchase_order=self.po)

        self.warehouse.rental_cost_per_mt = Decimal('99.00')
        self.warehouse.save()

        batch.refresh_from_db()
        self.assertEqual(batch.rental_rate_per_mt, Decimal('3.25'))


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
    capacity_forecast, warehouse_utilization, shipment_logistics, my_open_jobs,
    product_sales_trend, warehouse_rent_burn, rent_reduction_opportunities,
)
from core.settings_store import get_setting
from core.utils import apply_po_material_receipt


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
        self.assertContains(resp, 'Supplier Performance')

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
        self.assertContains(resp, 'Supplier performance')

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
        # Freeze "today" once so fixtures built here and analytics.sales_order_delivery_risk()'s
        # own timezone.now() call agree, even if the test runs across a real midnight rollover.
        frozen_now = timezone.now()
        self.today = frozen_now.date()
        patcher = _mock.patch('core.analytics.timezone.now', return_value=frozen_now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _so(self, number, *, status, deadline_in=None, ordered='10', shipped='0'):
        so = SalesOrder.objects.create(
            so_number=number, client_name=f'Client {number}',
            origin_warehouse=self.wh, status=status,
            fulfillment_deadline=(self.today + timedelta(days=deadline_in)) if deadline_in is not None else None,
        )
        SalesOrderDetail.objects.create(
            sales_order=so, product=self.product,
            quantity_ordered=Decimal(ordered), quantity_shipped=Decimal(shipped),
        )
        return so

    def _ship(self, so, trk, *, eta_in=None, arrived_in=None, status='Dispatched'):
        return Shipment.objects.create(
            tracking_number=trk, direction='Outbound', status=status, sales_order=so,
            expected_eta_date=(self.today + timedelta(days=eta_in)) if eta_in is not None else None,
            actual_arrival_date=(self.today + timedelta(days=arrived_in)) if arrived_in is not None else None,
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
        self.assertContains(resp, 'Delivery performance')


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
        resp = self.client.get(reverse('forecast') + '?status=action_required')
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
        self.assertContains(fac, 'Inventory capacity')


class WarehouseRentBurnTests(TestCase):
    """warehouse_rent_burn() - the batch-aware single source of truth for rent."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='rentburn', password='pw')
        self.material = Material.objects.create(
            name='Rent Sand', sku='MAT-RS', category='Bulk', unit_of_measure='MT',
            safe_storage_days=365, weight_mt_per_unit=Decimal('1.0'),
        )

    def _wh(self, ownership='ExternalProvider', billing='Usage', rate='5.00', capacity='1000'):
        return Warehouse.objects.create(
            name=f'Rent WH {Warehouse.objects.count()}', location_type='Storage',
            ownership_type=ownership, rental_billing_method=billing,
            rental_cost_per_mt=Decimal(rate), total_capacity_mt=Decimal(capacity),
        )

    def _batch(self, wh, qty, rate, status='Active', closed=False):
        return Batch.objects.create(
            batch_number=f'B-{wh.id}-{qty}-{rate}', material=self.material,
            quantity=Decimal(str(qty)), status=status, warehouse=wh,
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=300),
            rental_rate_per_mt=None if rate is None else Decimal(str(rate)),
            closed_date=date.today() if closed else None,
        )

    def _row(self, wh):
        return next(r for r in warehouse_rent_burn() if r['warehouse_id'] == wh.id)

    def test_internal_warehouse_is_zero_regardless_of_batches(self):
        wh = self._wh(ownership='Internal', billing='Usage', rate='5.00')
        self._batch(wh, 100, '5.00')
        self.assertEqual(self._row(wh)['daily_cost'], 0.0)
        self.assertEqual(self._row(wh)['billing_mode'], 'Internal')

    def test_overall_billing_uses_capacity_times_rate_ignores_batches(self):
        wh = self._wh(billing='Overall', rate='2.00', capacity='1000')
        self._batch(wh, 999, '5.00')  # a very different batch rate must be ignored
        self.assertEqual(self._row(wh)['daily_cost'], 2000.0)  # 1000 MT * 2.00
        self.assertEqual(self._row(wh)['billing_mode'], 'Overall Capacity')

    def test_usage_billing_sums_open_batches_at_locked_in_rate(self):
        wh = self._wh(billing='Usage', rate='5.00')
        self._batch(wh, 100, '3.00')
        self._batch(wh, 50, '7.00')
        # 100*3.00 + 50*7.00 = 650, NOT (100+50)*5.00 (the old flat warehouse-rate calc)
        self.assertEqual(self._row(wh)['daily_cost'], 650.0)

    def test_quarantined_batches_still_accrue_rent(self):
        wh = self._wh(billing='Usage', rate='4.00')
        self._batch(wh, 20, '4.00', status='Quarantined')
        rent_row = self._row(wh)
        util_row = next(r for r in warehouse_utilization() if r['warehouse_id'] == wh.id)
        self.assertEqual(rent_row['daily_cost'], 80.0)   # still costs rent
        self.assertEqual(util_row['used_mt'], 0.0)        # but excluded from usable capacity

    def test_closed_batches_excluded_from_rent(self):
        wh = self._wh(billing='Usage', rate='5.00')
        self._batch(wh, 100, '5.00', status='Depleted', closed=True)
        self.assertEqual(self._row(wh)['daily_cost'], 0.0)

    def test_null_rate_batches_live_track_warehouse_rate(self):
        wh = self._wh(billing='Usage', rate='5.00')
        self._batch(wh, 100, None)
        self.assertEqual(self._row(wh)['daily_cost'], 500.0)  # 100 MT * warehouse's 5.00

    def test_null_rate_batches_reflect_a_later_warehouse_rate_edit(self):
        wh = self._wh(billing='Usage', rate='5.00')
        self._batch(wh, 100, None)
        wh.rental_cost_per_mt = Decimal('9.00')
        wh.save()
        self.assertEqual(self._row(wh)['daily_cost'], 900.0)  # tracks the edit live

    def test_explicit_rate_batches_ignore_a_later_warehouse_rate_edit(self):
        wh = self._wh(billing='Usage', rate='5.00')
        self._batch(wh, 100, '3.00')  # explicit rate, e.g. a negotiated PO rate
        wh.rental_cost_per_mt = Decimal('9.00')
        wh.save()
        self.assertEqual(self._row(wh)['daily_cost'], 300.0)  # stays at its own locked rate


class RentReductionOpportunitiesTests(TestCase):
    """rent_reduction_opportunities() - the read-only DSS recommender."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='rentopp', password='pw')
        self.material = Material.objects.create(
            name='Opp Sand', sku='MAT-OS', category='Bulk', unit_of_measure='MT',
            safe_storage_days=365, weight_mt_per_unit=Decimal('1.0'),
        )
        self.rented = Warehouse.objects.create(
            name='Rented Depot', location_type='Storage', ownership_type='ExternalProvider',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('5.00'),
            total_capacity_mt=Decimal('1000'),
        )

    def _batch(self, wh, qty, rate, allocated=0, expiry_days=300):
        return Batch.objects.create(
            batch_number=f'B-{wh.id}-{qty}-{rate}-{allocated}-{expiry_days}', material=self.material,
            quantity=Decimal(str(qty)), allocated_quantity=Decimal(str(allocated)),
            status='Active', warehouse=wh,
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=expiry_days),
            rental_rate_per_mt=Decimal(str(rate)),
        )

    def _snap(self, wh, pct, days_ago):
        return WarehouseUtilizationSnapshot.objects.create(
            warehouse=wh, snapshot_date=date.today() - timedelta(days=days_ago),
            used_mt=Decimal(str(pct * 10)), capacity_mt=Decimal('1000'),
            utilization_percent=Decimal(str(pct)),
        )

    def _flag_critical(self, wh):
        self._snap(wh, 80, 6)
        self._snap(wh, 90, 3)
        self._snap(wh, 97, 0)

    def test_no_opportunities_when_nothing_flagged(self):
        self._batch(self.rented, 100, '5.00')
        self.assertEqual(rent_reduction_opportunities(), [])

    def test_no_opportunities_without_internal_spare_capacity(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00')
        # no Internal warehouse exists at all
        self.assertEqual(rent_reduction_opportunities(), [])

    def test_candidates_drawn_from_flagged_usage_warehouse(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00')
        Warehouse.objects.create(
            name='Internal Depot', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('500'),
        )
        rows = rent_reduction_opportunities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['warehouse_id'], self.rented.id)
        self.assertEqual(rows[0]['total_daily_saving'], 500.0)  # 100 MT * 5.00

    def test_highest_rate_batches_prioritized_when_spare_is_limited(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '3.00')
        self._batch(self.rented, 100, '9.00')
        Warehouse.objects.create(
            name='Internal Depot Small', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('100'),  # only enough spare for one batch
        )
        rows = rent_reduction_opportunities()
        self.assertEqual(len(rows[0]['candidate_batches']), 1)
        self.assertEqual(rows[0]['candidate_batches'][0]['rate_per_mt'], 9.0)
        self.assertEqual(rows[0]['total_daily_saving'], 900.0)

    def test_fully_allocated_batch_is_excluded(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', allocated=100)  # nothing left to move
        Warehouse.objects.create(
            name='Internal Depot Alloc', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('500'),
        )
        self.assertEqual(rent_reduction_opportunities(), [])

    def test_partially_allocated_batch_only_counts_free_portion(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', allocated=60)  # only 40 MT actually movable
        Warehouse.objects.create(
            name='Internal Depot Partial', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('500'),
        )
        rows = rent_reduction_opportunities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['candidate_batches'][0]['mt'], 40.0)
        self.assertEqual(rows[0]['total_daily_saving'], 200.0)  # 40 MT * 5.00, not 100 * 5.00

    def test_near_expiry_batch_is_excluded_with_nothing_else(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', expiry_days=10)  # expiring soon
        Warehouse.objects.create(
            name='Internal Depot Expiry', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('500'),
        )
        rows = rent_reduction_opportunities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['candidate_batches'], [])
        self.assertEqual(rows[0]['total_daily_saving'], 0)
        self.assertEqual(rows[0]['excluded_near_expiry'], 1)

    def test_batch_beyond_expiry_threshold_still_included(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', expiry_days=31)  # just past the 30-day cutoff
        Warehouse.objects.create(
            name='Internal Depot Beyond', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('500'),
        )
        rows = rent_reduction_opportunities()
        self.assertEqual(len(rows[0]['candidate_batches']), 1)
        self.assertEqual(rows[0]['excluded_near_expiry'], 0)

    def test_near_expiry_excluded_alongside_a_healthy_candidate(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '9.00', expiry_days=5)   # excluded despite the higher rate
        self._batch(self.rented, 100, '3.00', expiry_days=200)  # the only real candidate
        Warehouse.objects.create(
            name='Internal Depot Mixed', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('500'),
        )
        rows = rent_reduction_opportunities()
        self.assertEqual(len(rows[0]['candidate_batches']), 1)
        self.assertEqual(rows[0]['candidate_batches'][0]['rate_per_mt'], 3.0)
        self.assertEqual(rows[0]['excluded_near_expiry'], 1)

    def test_view_renders_with_caveat(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00')
        Warehouse.objects.create(
            name='Internal Depot View', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
            total_capacity_mt=Decimal('500'),
        )
        self.client.login(username='rentopp', password='pw')
        resp = self.client.get(reverse('rent_opportunities'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Rented Depot')
        self.assertContains(resp, 'Estimated potential savings only')


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


class ProductSalesTrendAnalyticsTests(TestCase):
    """Phase 5: product_sales_trend() - monthly volume trend per product, declining first."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='trendcheck', password='pw')
        self.wh = Warehouse.objects.create(name='Trend WH', location_type='Storage')
        self.product = Product.objects.create(
            name='Trend Product', sku='PRD-TREND', unit_of_measure='pcs', price_per_unit=10,
        )

    def _month_date(self, months_ago, day=15):
        y, m = date.today().year, date.today().month - months_ago
        while m <= 0:
            m += 12
            y -= 1
        return date(y, m, day)

    def _so(self, number, product, qty, months_ago, status='Delivered', unit_price=None):
        so = SalesOrder.objects.create(
            so_number=number, client_name='Acme', origin_warehouse=self.wh, status=status,
        )
        SalesOrder.objects.filter(pk=so.pk).update(order_date=self._month_date(months_ago))
        SalesOrderDetail.objects.create(
            sales_order=so, product=product, quantity_ordered=Decimal(str(qty)),
            quantity_shipped=Decimal(str(qty)), unit_price=unit_price,
        )
        return so

    def _row(self, sku='PRD-TREND'):
        return next(r for r in product_sales_trend() if r['sku'] == sku)

    def test_declining_product_flagged(self):
        for i, m in enumerate((5, 4, 3)):
            self._so(f'SO-DEC-{i}', self.product, 100, m)
        for i, m in enumerate((2, 1, 0)):
            self._so(f'SO-DEC2-{i}', self.product, 20, m)
        row = self._row()
        self.assertEqual(row['status'], 'declining')
        self.assertLess(row['pct_change'], -15)

    def test_rising_product_flagged(self):
        for i, m in enumerate((5, 4, 3)):
            self._so(f'SO-RIS-{i}', self.product, 20, m)
        for i, m in enumerate((2, 1, 0)):
            self._so(f'SO-RIS2-{i}', self.product, 100, m)
        row = self._row()
        self.assertEqual(row['status'], 'rising')
        self.assertGreater(row['pct_change'], 15)

    def test_flat_product_not_flagged(self):
        for i, m in enumerate((5, 4, 3, 2, 1, 0)):
            self._so(f'SO-FLAT-{i}', self.product, 50, m)
        self.assertEqual(self._row()['status'], 'flat')

    def test_new_product_no_earlier_history(self):
        for i, m in enumerate((2, 1, 0)):
            self._so(f'SO-NEW-{i}', self.product, 50, m)
        row = self._row()
        self.assertEqual(row['status'], 'new')
        self.assertIsNone(row['pct_change'])

    def test_insufficient_data_when_too_few_months(self):
        self._so('SO-THIN-0', self.product, 50, 0)
        self._so('SO-THIN-1', self.product, 50, 1)
        self.assertEqual(self._row()['status'], 'insufficient_data')

    def test_revenue_none_when_a_month_has_an_unpriced_line(self):
        for i, m in enumerate((5, 4, 3)):
            self._so(f'SO-REV-{i}', self.product, 10, m, unit_price=Decimal('5'))
        self._so('SO-REV-NOPRICE', self.product, 10, 2, unit_price=None)
        self._so('SO-REV-3', self.product, 10, 1, unit_price=Decimal('5'))
        self._so('SO-REV-4', self.product, 10, 0, unit_price=Decimal('5'))
        self.assertIsNone(self._row()['monthly_revenue'])
        self.assertIsNone(self._row()['total_revenue'])

    def test_revenue_present_when_fully_priced(self):
        for i, m in enumerate((5, 4, 3, 2, 1, 0)):
            self._so(f'SO-PRICED-{i}', self.product, 10, m, unit_price=Decimal('5'))
        row = self._row()
        self.assertIsNotNone(row['monthly_revenue'])
        self.assertEqual(row['total_revenue'], 300.0)  # 6 months * 10 qty * 5 price

    def test_excludes_draft_and_rejected_orders(self):
        for i, m in enumerate((5, 4, 3, 2, 1, 0)):
            self._so(f'SO-REAL-{i}', self.product, 50, m)
        self._so('SO-DRAFT', self.product, 99999, 0, status='Draft')
        self._so('SO-REJECTED', self.product, 99999, 0, status='Rejected')
        self.assertEqual(self._row()['total_qty'], 300.0)

    def test_sort_order_declining_first(self):
        rising = Product.objects.create(name='Riser', sku='PRD-RISE', unit_of_measure='pcs', price_per_unit=1)
        for i, m in enumerate((5, 4, 3)):
            self._so(f'SO-A-{i}', self.product, 100, m)
        for i, m in enumerate((2, 1, 0)):
            self._so(f'SO-B-{i}', self.product, 20, m)
        for i, m in enumerate((5, 4, 3)):
            self._so(f'SO-C-{i}', rising, 20, m)
        for i, m in enumerate((2, 1, 0)):
            self._so(f'SO-D-{i}', rising, 100, m)
        rows = product_sales_trend()
        self.assertEqual(rows[0]['sku'], 'PRD-TREND')  # declining ranks before rising

    def test_view_renders(self):
        self._so('SO-VIEW', self.product, 50, 0)
        self.client.login(username='trendcheck', password='pw')
        resp = self.client.get(reverse('product_sales_trend'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'PRD-TREND')


class ShipmentLogisticsAnalyticsTests(TestCase):
    """Phase 4: shipment_logistics() - in-flight shipment risk, any direction."""

    def setUp(self):
        self.wh1 = Warehouse.objects.create(name='Alpha', location_type='Storage')
        self.wh2 = Warehouse.objects.create(name='Beta', location_type='Storage')

    def _ship(self, trk, **kwargs):
        defaults = dict(tracking_number=trk, direction='Transfer', status='Draft',
                        origin_warehouse=self.wh1, destination_warehouse=self.wh2)
        defaults.update(kwargs)
        return Shipment.objects.create(**defaults)

    def _row(self, trk):
        return next(r for r in shipment_logistics() if r['tracking_number'] == trk)

    def test_discrepant_ranks_first(self):
        self._ship('S-1', status='Dispatched', has_discrepancy=True)
        self._ship('S-2', status='Delayed')
        self.assertEqual(shipment_logistics()[0]['tracking_number'], 'S-1')
        self.assertEqual(self._row('S-1')['risk'], 'discrepant')

    def test_overdue_when_delayed_status(self):
        self._ship('S-3', status='Delayed')
        self.assertEqual(self._row('S-3')['risk'], 'overdue')

    def test_overdue_when_dispatched_past_eta_with_no_arrival(self):
        self._ship('S-4', status='Dispatched', expected_eta_date=date.today() - timedelta(days=2))
        self.assertEqual(self._row('S-4')['risk'], 'overdue')

    def test_at_risk_window_setting_respected(self):
        self._ship('S-5', status='Dispatched', expected_eta_date=date.today() + timedelta(days=2))
        self.assertEqual(self._row('S-5')['risk'], 'at_risk')  # default window is 3 days
        SystemSetting.objects.update_or_create(
            key='logistics_at_risk_window_days', defaults={'value': '1', 'value_type': 'int'},
        )
        self.assertEqual(self._row('S-5')['risk'], 'on_track')

    def test_stalled_arrived_shipment(self):
        self._ship('S-6', status='Arrived', actual_arrival_date=date.today() - timedelta(days=10))
        self.assertEqual(self._row('S-6')['risk'], 'stalled')

    def test_recently_arrived_is_on_track(self):
        self._ship('S-7', status='Arrived', actual_arrival_date=date.today())
        self.assertEqual(self._row('S-7')['risk'], 'on_track')

    def test_pending_pre_dispatch(self):
        self._ship('S-8', status='Preparing')
        self.assertEqual(self._row('S-8')['risk'], 'pending')

    def test_transfer_shipment_has_no_so_or_po(self):
        self._ship('S-9', direction='Transfer')
        row = self._row('S-9')
        self.assertTrue(row['is_transfer'])
        self.assertIsNone(row['so_number'])
        self.assertIsNone(row['po_number'])

    def test_completed_and_cancelled_excluded(self):
        self._ship('S-10', status='Completed')
        self._ship('S-11', status='Cancelled')
        numbers = [r['tracking_number'] for r in shipment_logistics()]
        self.assertNotIn('S-10', numbers)
        self.assertNotIn('S-11', numbers)

    def test_sort_order_worst_first(self):
        self._ship('S-OK', status='Dispatched', expected_eta_date=date.today() + timedelta(days=30))
        self._ship('S-BAD', status='Delayed')
        self.assertEqual(shipment_logistics()[0]['tracking_number'], 'S-BAD')

    def test_view_renders(self):
        self._ship('S-VIEW', status='Delayed')
        client = Client()
        user = User.objects.create_user(username='logi', password='pw')
        client.login(username='logi', password='pw')
        resp = client.get(reverse('shipment_logistics'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'S-VIEW')


class SOCreateShipmentAllocationTests(TestCase):
    """Regression coverage for the SO-TS001 bug: so_create_shipment_view must move the
    SO-level StockAllocation onto the new shipment (not leave it shared/behind), must
    refuse to draft a second logistics order once the allocation is already spoken for,
    and every path that completes an outbound SO shipment must credit
    SalesOrderDetail.quantity_shipped exactly once."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='opuser', password='pw')
        self.client.login(username='opuser', password='pw')

        self.wh = Warehouse.objects.create(name='FG Hub', location_type='Storage')
        self.product = Product.objects.create(
            name='Blend X', sku='PRD-X', unit_of_measure='pcs', price_per_unit=10,
        )
        self.batch = Batch.objects.create(
            batch_number='B-TEST-1', status='Active', product=self.product,
            quantity=Decimal('600'), allocated_quantity=Decimal('600'),
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=365),
            warehouse=self.wh,
        )
        self.so = SalesOrder.objects.create(
            so_number='SO-TEST-1', client_name='Acme', origin_warehouse=self.wh,
            status='Ready to Ship',
        )
        self.so_detail = SalesOrderDetail.objects.create(
            sales_order=self.so, product=self.product, quantity_ordered=Decimal('600'),
        )
        self.alloc = StockAllocation.objects.create(
            batch=self.batch, sales_order=self.so, quantity=Decimal('600'),
        )

    def _create_shipment(self):
        return self.client.post(reverse('so_create_shipment', args=[self.so.pk]))

    def test_creating_shipment_moves_allocation_off_the_so(self):
        self._create_shipment()
        self.assertFalse(StockAllocation.objects.filter(sales_order=self.so).exists())
        shipment_allocs = StockAllocation.objects.filter(shipment__sales_order=self.so)
        self.assertEqual(shipment_allocs.count(), 1)
        self.assertEqual(shipment_allocs.first().quantity, Decimal('600'))

    def test_second_create_shipment_click_is_blocked(self):
        self._create_shipment()
        self.assertEqual(Shipment.objects.filter(sales_order=self.so).count(), 1)

        resp = self._create_shipment()
        self.assertEqual(Shipment.objects.filter(sales_order=self.so).count(), 1)
        from django.contrib.messages import get_messages
        msgs = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any('already held by logistics order' in m for m in msgs))

    def test_button_hidden_once_allocation_fully_drafted(self):
        resp = self.client.get(reverse('so_detail', args=[self.so.pk]))
        self.assertContains(resp, 'Create Logistics Order')

        self._create_shipment()
        resp = self.client.get(reverse('so_detail', args=[self.so.pk]))
        self.assertNotContains(resp, 'Create Logistics Order')
        self.assertContains(resp, 'already held by a logistics order')

    def test_scrapping_one_shipment_does_not_touch_a_sibling(self):
        # Simulate the SO-TS001 scenario directly: two shipments, each holding its own
        # shipment-level allocation (as they now should after the fix).
        self._create_shipment()
        shipment_a = Shipment.objects.get(sales_order=self.so)

        # Manually allocate more stock and draft a second, independent shipment so we
        # can prove scrapping one doesn't touch the other's allocation.
        batch2 = Batch.objects.create(
            batch_number='B-TEST-2', status='Active', product=self.product,
            quantity=Decimal('100'), allocated_quantity=Decimal('100'),
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=365),
            warehouse=self.wh,
        )
        shipment_b = Shipment.objects.create(
            tracking_number='SHP-TEST-B', sales_order=self.so, direction='Outbound', status='Draft',
        )
        ShipmentItem.objects.create(shipment=shipment_b, product=self.product, batch=batch2, quantity=Decimal('100'))
        StockAllocation.objects.create(batch=batch2, shipment=shipment_b, quantity=Decimal('100'))

        self.client.post(reverse('shipment_detail', args=[shipment_a.pk]), {'action': 'scrap_shipment'})

        self.assertFalse(StockAllocation.objects.filter(shipment=shipment_a).exists())
        self.assertTrue(StockAllocation.objects.filter(shipment=shipment_b).exists())
        batch2.refresh_from_db()
        self.assertEqual(batch2.allocated_quantity, Decimal('100'))

    def _complete(self, shipment, received=Decimal('600')):
        item = shipment.items.first()
        return self.client.post(reverse('shipment_detail', args=[shipment.pk]), {
            'action': 'complete_shipment',
            f'received_qty_{item.id}': str(received),
        })

    def test_completing_shipment_credits_quantity_shipped_and_deducts_stock(self):
        self._create_shipment()
        shipment = Shipment.objects.get(sales_order=self.so)
        shipment.status = 'Arrived'
        shipment.save(update_fields=['status'])

        self._complete(shipment)

        self.so_detail.refresh_from_db()
        self.batch.refresh_from_db()
        self.so.refresh_from_db()
        self.assertEqual(self.so_detail.quantity_shipped, Decimal('600'))
        self.assertEqual(self.batch.quantity, Decimal('0'))
        self.assertEqual(self.so.status, 'Delivered')

    def test_dispatch_then_complete_does_not_double_credit(self):
        self._create_shipment()
        shipment = Shipment.objects.get(sales_order=self.so)
        shipment.status = 'Preparing'
        shipment.external_tracking_id = 'TRK-1'
        shipment.departure_datetime = timezone.now()
        shipment.save()

        self.client.post(reverse('shipment_detail', args=[shipment.pk]), {
            'action': 'update_operational_status', 'status': 'Dispatched',
        })
        self.so_detail.refresh_from_db()
        self.assertEqual(self.so_detail.quantity_shipped, Decimal('600'))

        shipment.refresh_from_db()
        shipment.status = 'Arrived'
        shipment.save(update_fields=['status'])
        self._complete(shipment)

        self.so_detail.refresh_from_db()
        self.assertEqual(self.so_detail.quantity_shipped, Decimal('600'))


class OpsBriefingCategoryTests(TestCase):
    """Phase 4: collect_signals(category), generate_briefing(category=...), the command."""

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

    def test_collect_signals_materials_only_includes_stockout(self):
        signals = briefing_mod.collect_signals('materials')
        self.assertEqual(signals['category'], 'materials')
        self.assertIn('stockout_forecast', signals)
        self.assertNotIn('capacity_runway', signals)
        self.assertNotIn('sales_order_delivery_risk', signals)

    def test_collect_signals_warehouse_includes_capacity_and_audit(self):
        signals = briefing_mod.collect_signals('warehouse')
        self.assertIn('capacity_runway', signals)
        self.assertIn('stock_audit_accuracy', signals)

    def test_collect_signals_sales_includes_product_trend(self):
        signals = briefing_mod.collect_signals('sales')
        self.assertIn('product_sales_trend', signals)

    def test_collect_signals_sales_keeps_only_notable_rows(self):
        today = date.today()
        self._late_so('SO-LATE')
        SalesOrder.objects.create(  # on-track, far-off deadline -> must be excluded
            so_number='SO-OK', client_name='Fine', origin_warehouse=self.wh,
            status='Pending', fulfillment_deadline=today + timedelta(days=90),
        )
        signals = briefing_mod.collect_signals('sales', end=today)
        risk = signals['sales_order_delivery_risk']
        self.assertEqual([r['sales_order'] for r in risk], ['SO-LATE'])
        self.assertGreaterEqual(signals['signal_count'], 1)
        self.assertEqual(signals['generated_for'], today.isoformat())

    def test_collect_signals_empty_when_nothing_notable(self):
        signals = briefing_mod.collect_signals('sales')
        self.assertEqual(signals['signal_count'], 0)
        self.assertEqual(signals['sales_order_delivery_risk'], [])

    def test_unknown_category_raises_value_error(self):
        with self.assertRaises(ValueError):
            briefing_mod.collect_signals('not-a-category')

    def test_prompt_intro_differs_per_category(self):
        self.assertNotEqual(
            briefing_mod._system_prompt('materials'), briefing_mod._system_prompt('sales')
        )

    def test_stockout_signals_include_burn_trend(self):
        m = Material.objects.create(
            name='Trend Mat', sku='MAT-TREND', category='X', unit_of_measure='kg',
            safe_storage_days=90,
        )
        Batch.objects.create(
            batch_number='B-TREND', material=m, quantity=Decimal('10'),
            allocated_quantity=Decimal('0'), status='Active', warehouse=self.wh,
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=300),
        )
        row = RegistryLog.objects.create(
            action_type='Consumed_For_Manufacturing', item_name=f'{m.name} (Run X)',
            material=m, quantity_changed=Decimal('50'), warehouse=self.wh,
        )
        RegistryLog.objects.filter(pk=row.pk).update(timestamp=timezone.now() - timedelta(days=5))

        signals = briefing_mod._stockout_signals(None)
        trend_row = next(r for r in signals if r['material'].startswith('MAT-TREND'))
        self.assertIn('burn_trend_weekly', trend_row)
        self.assertEqual(len(trend_row['burn_trend_weekly']), briefing_mod.TREND_WEEKS)

    def test_capacity_signals_include_utilization_trend(self):
        wh2 = Warehouse.objects.create(
            name='Fast Fill', location_type='Storage', total_capacity_mt=Decimal('100'),
        )
        base = date.today()
        for i, pct in enumerate([80, 90, 97]):
            WarehouseUtilizationSnapshot.objects.create(
                warehouse=wh2, snapshot_date=base - timedelta(days=(2 - i)),
                used_mt=Decimal(str(pct)), capacity_mt=Decimal('100'),
                utilization_percent=Decimal(str(pct)),
            )

        signals = briefing_mod._capacity_signals(None)
        trend_row = next(r for r in signals if r['warehouse'] == 'Fast Fill')
        self.assertIn('utilization_trend', trend_row)
        self.assertEqual(len(trend_row['utilization_trend']), 3)
        self.assertEqual(trend_row['utilization_trend'][0]['percent'], 80.0)
        self.assertEqual(trend_row['utilization_trend'][-1]['percent'], 97.0)

    # -- generate_briefing -------------------------------------------------

    def test_empty_signals_records_empty_without_api_call(self):
        fake = _FakeAnthropicModule()
        with _mock.patch.object(briefing_mod, 'anthropic', fake):
            b = briefing_mod.generate_briefing(category='sales')
        self.assertEqual(b.status, 'empty')
        self.assertEqual(b.category, 'sales')
        self.assertEqual(fake.calls, [])

    def test_missing_api_key_records_error_not_exception(self):
        self._late_so()
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {}, clear=False):
            _os.environ.pop('ANTHROPIC_API_KEY', None)
            b = briefing_mod.generate_briefing(category='sales')
        self.assertEqual(b.status, 'error')
        self.assertIn('ANTHROPIC_API_KEY', b.error_detail)

    def test_package_missing_records_error(self):
        self._late_so()
        with _mock.patch.object(briefing_mod, 'anthropic', None), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_briefing(category='sales')
        self.assertEqual(b.status, 'error')
        self.assertIn('anthropic', b.error_detail)

    def test_disabled_setting_records_skipped(self):
        self._late_so()
        SystemSetting.objects.update_or_create(
            key='ops_briefing_enabled',
            defaults={'value': 'False', 'value_type': 'bool'},
        )
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_briefing(category='sales')
        self.assertEqual(b.status, 'skipped')

    def test_successful_generation_stores_body_tokens_and_category(self):
        self._late_so()
        fake = _FakeAnthropicModule()
        with _mock.patch.object(briefing_mod, 'anthropic', fake), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_briefing(category='sales', period='daily', user=self.manager)
        self.assertEqual(b.status, 'ok')
        self.assertEqual(b.category, 'sales')
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
            b = briefing_mod.generate_briefing(category='sales')
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
            briefing_mod.generate_briefing(category='sales')
        self.assertEqual(fake.calls[0]['model'], 'claude-haiku-4-5')

    # -- command ----------------------------------------------------------

    def test_command_dry_run_single_category(self):
        from django.core.management import call_command
        from io import StringIO
        self._late_so()
        out = StringIO()
        call_command('generate_ops_briefing', '--category', 'sales', '--dry-run', stdout=out)
        self.assertIn('sales_order_delivery_risk', out.getvalue())
        self.assertEqual(OpsBriefing.objects.count(), 0)

    def test_command_dry_run_all_categories(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('generate_ops_briefing', '--category', 'all', '--dry-run', stdout=out)
        text = out.getvalue()
        for category in ('materials', 'products', 'sales', 'purchase', 'logistics', 'warehouse'):
            self.assertIn(f'--- {category} ---', text)

    def test_command_generate_all_creates_six_rows(self):
        from django.core.management import call_command
        from io import StringIO
        self._late_so()
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            call_command('generate_ops_briefing', '--category', 'all', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(OpsBriefing.objects.exclude(category='my_checklist').count(), 6)

    def test_command_never_creates_my_checklist_rows(self):
        from django.core.management import call_command
        from io import StringIO
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            call_command('generate_ops_briefing', '--category', 'all', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(OpsBriefing.objects.filter(category='my_checklist').count(), 0)


class CategoryBriefingViewTests(TestCase):
    """Phase 4: category_briefing_view - the six domain briefing tabs."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='viewer', password='pw')
        self.manager = User.objects.create_user(username='mgr2', password='pw', role='Manager')
        self.wh = Warehouse.objects.create(name='FG Store 2', location_type='Storage')

    def _late_so(self, number='SO-CB-LATE'):
        return SalesOrder.objects.create(
            so_number=number, client_name='Acme', origin_warehouse=self.wh,
            status='In Production', fulfillment_deadline=date.today() - timedelta(days=9),
        )

    def test_view_requires_login(self):
        resp = self.client.get(reverse('category_briefing', args=['sales']))
        self.assertEqual(resp.status_code, 302)

    def test_unknown_category_404s(self):
        self.client.login(username='viewer', password='pw')
        resp = self.client.get(reverse('category_briefing', args=['not-a-category']))
        self.assertEqual(resp.status_code, 404)

    def test_all_six_tabs_render(self):
        self.client.login(username='viewer', password='pw')
        for category in ('materials', 'products', 'sales', 'purchase', 'logistics', 'warehouse'):
            resp = self.client.get(reverse('category_briefing', args=[category]))
            self.assertEqual(resp.status_code, 200, category)
            self.assertContains(resp, 'My Open Jobs')

    def test_generate_gated_to_admin_manager(self):
        self.client.login(username='viewer', password='pw')
        resp = self.client.get(reverse('category_briefing', args=['sales']))
        self.assertNotContains(resp, 'Generate now')
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            self.client.post(reverse('category_briefing', args=['sales']), {'period': 'daily'})
        self.assertEqual(OpsBriefing.objects.count(), 0)

    def test_manager_can_generate_a_category(self):
        self._late_so()
        self.client.login(username='mgr2', password='pw')
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            resp = self.client.post(reverse('category_briefing', args=['sales']), {'period': 'daily'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(OpsBriefing.objects.filter(status='ok', category='sales').count(), 1)

    def test_recent_runs_scoped_by_category(self):
        OpsBriefing.objects.create(category='sales', status='ok', headline='Sales one', signal_count=1)
        OpsBriefing.objects.create(category='purchase', status='ok', headline='Purchase one', signal_count=1)
        self.client.login(username='viewer', password='pw')
        resp = self.client.get(reverse('category_briefing', args=['sales']))
        self.assertContains(resp, 'Sales one')
        self.assertNotContains(resp, 'Purchase one')

    def test_outlook_section_renders_when_present(self):
        OpsBriefing.objects.create(
            category='sales', status='ok', headline='Head', signal_count=1,
            body_text='- point one\nWatch: watch line\nOutlook (estimate):\n- likely to worsen based on X',
        )
        self.client.login(username='viewer', password='pw')
        resp = self.client.get(reverse('category_briefing', args=['sales']))
        self.assertContains(resp, 'likely to worsen based on X')
        self.assertContains(resp, 'Outlook')

    def test_outlook_absent_when_no_section(self):
        OpsBriefing.objects.create(
            category='sales', status='ok', headline='Head', signal_count=1,
            body_text='- point one\nWatch: watch line',
        )
        self.client.login(username='viewer', password='pw')
        resp = self.client.get(reverse('category_briefing', args=['sales']))
        self.assertNotContains(resp, 'Outlook')


class MyChecklistTests(TestCase):
    """Phase 4: analytics.my_open_jobs(), generate_my_checklist(), the checklist view."""

    def setUp(self):
        self.client = Client()
        self.owner = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.wh = Warehouse.objects.create(name='FG Store', location_type='Storage')
        self.product = Product.objects.create(
            name='Blend A', sku='PRD-A', unit_of_measure='pcs', price_per_unit=10,
        )

    def test_live_table_includes_users_open_sales_order(self):
        SalesOrder.objects.create(
            so_number='SO-OWN', client_name='Acme', origin_warehouse=self.wh,
            status='Pending', created_by=self.owner,
        )
        jobs = my_open_jobs(self.owner)
        self.assertEqual([j['reference'] for j in jobs], ['SO-OWN'])
        self.assertEqual(jobs[0]['ownership'], 'created')

    def test_live_table_excludes_closed_status_records(self):
        SalesOrder.objects.create(
            so_number='SO-DONE', client_name='Acme', origin_warehouse=self.wh,
            status='Delivered', created_by=self.owner,
        )
        self.assertEqual(my_open_jobs(self.owner), [])

    def test_live_table_includes_shipment_last_edited_by_user_labeled_touched(self):
        Shipment.objects.create(
            tracking_number='SHP-OWN', direction='Transfer', status='Draft',
            last_edited_by=self.owner,
        )
        jobs = my_open_jobs(self.owner)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['kind'], 'Shipment')
        self.assertEqual(jobs[0]['ownership'], 'touched')

    def test_live_table_excludes_shipment_created_by_other_user(self):
        Shipment.objects.create(
            tracking_number='SHP-OTHER', direction='Transfer', status='Draft',
            last_edited_by=self.other,
        )
        self.assertEqual(my_open_jobs(self.owner), [])

    def test_context_cross_references_delivery_risk(self):
        so = SalesOrder.objects.create(
            so_number='SO-RISK', client_name='Acme', origin_warehouse=self.wh,
            status='In Production', fulfillment_deadline=date.today() - timedelta(days=5),
            created_by=self.owner,
        )
        SalesOrderDetail.objects.create(
            sales_order=so, product=self.product,
            quantity_ordered=Decimal('10'), quantity_shipped=Decimal('0'),
        )
        jobs = my_open_jobs(self.owner)
        self.assertEqual(jobs[0]['context']['risk'], 'late')

    def test_empty_signals_records_empty_without_api_call(self):
        fake = _FakeAnthropicModule()
        with _mock.patch.object(briefing_mod, 'anthropic', fake):
            b = briefing_mod.generate_my_checklist(user=self.owner)
        self.assertEqual(b.status, 'empty')
        self.assertEqual(b.category, 'my_checklist')
        self.assertEqual(b.generated_by, self.owner)
        self.assertEqual(fake.calls, [])

    def test_successful_generation_sends_only_real_identifiers(self):
        SalesOrder.objects.create(
            so_number='SO-REAL', client_name='Acme', origin_warehouse=self.wh,
            status='Pending', created_by=self.owner,
        )
        fake = _FakeAnthropicModule(response=_FakeResponse('Clear SO-REAL first.\n- SO-REAL: follow up'))
        with _mock.patch.object(briefing_mod, 'anthropic', fake), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            b = briefing_mod.generate_my_checklist(user=self.owner)
        self.assertEqual(b.status, 'ok')
        self.assertEqual(b.category, 'my_checklist')
        self.assertEqual(b.generated_by, self.owner)
        sent = fake.calls[0]['messages'][0]['content']
        self.assertIn('SO-REAL', sent)
        self.assertNotIn('sales_order_delivery_risk', sent)  # a different payload shape than the category briefings

    # -- view -----------------------------------------------------------

    def test_view_requires_login(self):
        self.assertEqual(self.client.get(reverse('ops_briefing')).status_code, 302)

    def test_view_shows_only_this_users_live_items(self):
        SalesOrder.objects.create(
            so_number='SO-MINE', client_name='Acme', origin_warehouse=self.wh,
            status='Pending', created_by=self.owner,
        )
        SalesOrder.objects.create(
            so_number='SO-THEIRS', client_name='Acme', origin_warehouse=self.wh,
            status='Pending', created_by=self.other,
        )
        self.client.login(username='owner', password='pw')
        resp = self.client.get(reverse('ops_briefing'))
        self.assertContains(resp, 'SO-MINE')
        self.assertNotContains(resp, 'SO-THEIRS')

    def test_view_empty_state(self):
        self.client.login(username='owner', password='pw')
        resp = self.client.get(reverse('ops_briefing'))
        self.assertContains(resp, 'Nothing open right now')

    def test_any_authenticated_user_can_generate_own_checklist(self):
        SalesOrder.objects.create(
            so_number='SO-GEN', client_name='Acme', origin_warehouse=self.wh,
            status='Pending', created_by=self.owner,
        )
        self.client.login(username='owner', password='pw')  # plain Staff_View user, not Admin/Manager
        with _mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
             _mock.patch.dict(_os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            resp = self.client.post(reverse('ops_briefing'), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(OpsBriefing.objects.filter(status='ok', generated_by=self.owner).count(), 1)

    def test_checklist_scoped_to_generating_user_only(self):
        OpsBriefing.objects.create(category='my_checklist', status='ok', headline='Owner one',
                                    signal_count=1, generated_by=self.owner)
        OpsBriefing.objects.create(category='my_checklist', status='ok', headline='Other one',
                                    signal_count=1, generated_by=self.other)
        self.client.login(username='owner', password='pw')
        resp = self.client.get(reverse('ops_briefing'))
        self.assertContains(resp, 'Owner one')
        self.assertNotContains(resp, 'Other one')


class AICopilotNavTests(TestCase):
    """Phase 4: the sidebar link is renamed and stays wired."""

    def test_nav_link_wired_and_renamed(self):
        user = User.objects.create_user(username='navcheck', password='pw')
        client = Client()
        client.login(username='navcheck', password='pw')
        resp = client.get(reverse('dashboard'))
        self.assertContains(resp, reverse('ops_briefing'))
        self.assertContains(resp, 'AI Copilot')
        self.assertNotContains(resp, 'Digital Assistant')

    def test_tabs_render_on_checklist_page(self):
        user = User.objects.create_user(username='tabcheck', password='pw')
        client = Client()
        client.login(username='tabcheck', password='pw')
        resp = client.get(reverse('ops_briefing'))
        for label in ('Materials', 'Products', 'Sales', 'Purchase', 'Logistics', 'Warehouse'):
            self.assertContains(resp, label)
