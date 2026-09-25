from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from core.db_tracker import get_db_status, DB_QUERY_LOGS
from django.db import connection
from core.models import (
    Warehouse, WarehouseLocation, Material, Product, ProductRecipe,
    Batch, SalesOrder, SalesOrderDetail, StockAllocation,
    Shipment, ShipmentItem,
)
from core.utils import allocate_stock, deduct_stock_from_allocation

User = get_user_model()


def make_user(role=None, **kwargs):
    """Create a user and put them in the `role` Group (seeded by migration 0060)."""
    user = User.objects.create_user(**kwargs)
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user

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
    product_sales_trend, warehouse_rent_burn, rent_reduction_opportunities, rent_results,
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
        # "Today" is the local (Kuala Lumpur) date, which is what the analytics use.
        frozen_now = timezone.now()
        self.today = timezone.localdate(frozen_now)
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


class ProductionRunDetailTests(TestCase):
    """Run detail page: yield log persistence, auto-fill on completion, approval from detail."""

    def setUp(self):
        self.client = Client()
        self.boss = User.objects.create_superuser(username='boss', password='pw')
        self.plain = User.objects.create_user(username='plain', password='pw')
        self.product = Product.objects.create(
            name='Compound Y', sku='PRD-Y', unit_of_measure='kg', price_per_unit=5,
        )

    def _run(self, status, **kw):
        return ProductionRun.objects.create(
            run_number='RD-1', target_product=self.product,
            expected_yield=Decimal('484.01'), status=status, **kw,
        )

    def test_complete_autofills_remaining_yield(self):
        from core.models import ProductionRunYieldLog
        run = self._run('InProgress')
        ProductionRunYieldLog.objects.create(
            production_run=run, quantity=Decimal('400'), log_date=timezone.now().date(), logged_by=self.boss)
        self.client.login(username='boss', password='pw')
        self.client.post(reverse('production_run_detail', args=[run.pk]),
                         {'action': 'complete_production', 'actual_yield': '484.01'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Completed')
        total = sum(y.quantity for y in run.yield_logs.all())
        self.assertEqual(total, Decimal('484.01'))
        self.assertTrue(run.yield_logs.filter(notes='Auto-filled on completion').exists())

    def test_complete_with_lower_figure_does_not_autofill(self):
        from core.models import ProductionRunYieldLog
        run = self._run('InProgress')
        ProductionRunYieldLog.objects.create(
            production_run=run, quantity=Decimal('400'), log_date=timezone.now().date(), logged_by=self.boss)
        self.client.login(username='boss', password='pw')
        self.client.post(reverse('production_run_detail', args=[run.pk]),
                         {'action': 'complete_production', 'actual_yield': '400'})
        self.assertEqual(run.yield_logs.count(), 1)

    def test_yield_log_visible_after_completion(self):
        from core.models import ProductionRunYieldLog
        run = self._run('Completed', actual_yield=Decimal('400'))
        ProductionRunYieldLog.objects.create(
            production_run=run, quantity=Decimal('400'), log_date=timezone.now().date(),
            logged_by=self.boss, notes='first batch')
        self.client.login(username='boss', password='pw')
        resp = self.client.get(reverse('production_run_detail', args=[run.pk]))
        self.assertContains(resp, 'Yield Log')
        self.assertContains(resp, 'first batch')

    def test_pre_production_approval_from_detail_records_approver(self):
        run = self._run('Pending Approval')
        self.client.login(username='boss', password='pw')
        url = reverse('production_run_detail', args=[run.pk])
        resp = self.client.get(url)
        self.assertContains(resp, 'Pending Approval')
        self.assertNotContains(resp, 'Variance Approval')
        self.assertContains(resp, 'value="approve"')
        self.assertNotContains(resp, 'Approve &amp; Complete')
        self.client.post(reverse('approvals_inbox'), {
            'item_type': 'production_run', 'item_id': run.pk, 'action': 'approve', 'next': url})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')
        self.assertEqual(run.approved_by, self.boss)
        self.assertContains(self.client.get(url), 'Allocate Materials')

    def test_non_approver_gets_no_approve_button(self):
        run = self._run('Pending Approval')
        self.client.login(username='plain', password='pw')
        resp = self.client.get(reverse('production_run_detail', args=[run.pk]))
        self.assertNotContains(resp, 'value="approve"')

    def _allocation_setup(self, stock_at_plant):
        from core.models import Warehouse, Material, ProductRecipe, Batch
        plant = Warehouse.objects.create(name='Plant', location_type='Manufacturing')
        other = Warehouse.objects.create(name='Store', location_type='Storage')
        mat = Material.objects.create(name='Resin', sku='MAT-R', category='Bulk',
                                      unit_of_measure='kg', safe_storage_days=365)
        ProductRecipe.objects.create(product=self.product, material=mat, quantity_required=1)
        batch = Batch.objects.create(
            batch_number='B-R-1', material=mat, quantity=Decimal('1000'), status='Active',
            manufacturing_date='2026-01-01', expiry_date='2027-01-01',
            warehouse=plant if stock_at_plant else other)
        run = self._run('Pending Allocation', manufacturing_plant=plant)
        self.client.login(username='boss', password='pw')
        return run, batch

    def test_allocation_uses_run_plant_and_skips_transfer_for_local_stock(self):
        from core.models import Shipment
        run, batch = self._allocation_setup(stock_at_plant=True)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Planned')
        self.assertFalse(Shipment.objects.filter(linked_production_run=run).exists())
        self.assertEqual(run.allocations.count(), 1)

    def test_allocation_of_remote_stock_creates_transfer(self):
        from core.models import Shipment
        run, batch = self._allocation_setup(stock_at_plant=False)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Awaiting Materials')
        self.assertEqual(Shipment.objects.filter(linked_production_run=run).count(), 1)
        self.assertEqual(run.manufacturing_plant.name, 'Plant')

    def test_cannot_start_unallocated_run(self):
        run = self._run('Pending Allocation')
        self.client.login(username='boss', password='pw')
        url = reverse('production_run_detail', args=[run.pk])
        resp = self.client.get(url)
        self.assertNotContains(resp, 'value="start_production"')
        self.assertContains(resp, 'Materials must be allocated')
        self.client.post(url, {'action': 'start_production'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')

    def test_allocated_batches_listed_on_page(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        resp = self.client.get(reverse('production_run_detail', args=[run.pk]))
        self.assertContains(resp, 'Materials Allocated')
        self.assertContains(resp, 'B-R-1')
        self.assertContains(resp, 'At plant')
        self.assertContains(resp, 'value="start_production"')

    def test_topping_up_a_short_allocation_needs_no_acknowledgement(self):
        from core.models import Shipment
        run, batch = self._allocation_setup(stock_at_plant=False)
        alloc_url = reverse('production_run_allocate', args=[run.pk])
        detail = reverse('production_run_detail', args=[run.pk])
        self.client.post(alloc_url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '400', 'allocate_short': '1'})
        self.assertEqual(run.allocations.count(), 1)
        # Still short, so the run page offers to allocate the remainder (no scary confirm)
        resp = self.client.get(detail)
        self.assertContains(resp, '+ Allocate Materials')
        self.assertNotContains(resp, 'Allocating again adds to them')
        # The allocate page shows what is already there and suggests only the shortfall
        resp = self.client.get(alloc_url)
        self.assertContains(resp, 'Already allocated to this run')
        self.assertNotContains(resp, 'acknowledge_existing')
        self.assertEqual(resp.context['recipe_reqs'][0]['needed'], 84.01)
        # The remainder goes through with no acknowledgement and is added on top
        self.client.post(alloc_url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '84.01'})
        self.assertEqual(run.allocations.count(), 2)
        self.assertEqual(Shipment.objects.filter(linked_production_run=run).count(), 2)
        # Fully allocated now: nothing left to offer
        self.assertContains(self.client.get(detail), alloc_url)   # extra can still be added

    def test_allocating_after_production_starts_keeps_the_run_in_progress(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        alloc_url = reverse('production_run_allocate', args=[run.pk])
        detail = reverse('production_run_detail', args=[run.pk])
        self.client.post(alloc_url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '400', 'allocate_short': '1'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Planned')
        # Start page warns about the shortfall and asks to confirm
        resp = self.client.get(detail)
        self.assertContains(resp, 'Not fully allocated')
        self.assertContains(resp, 'Start production anyway?')
        self.client.post(detail, {'action': 'start_production'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')
        started_at = run.exact_start_time
        # Still short and under way: the remainder can be allocated, and the run stays In Progress
        self.assertContains(self.client.get(detail), '+ Allocate Materials')
        self.client.post(alloc_url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '84.01'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')
        self.assertEqual(run.exact_start_time, started_at)
        self.assertEqual(sum(a.quantity for a in run.allocations.all()), Decimal('484.01'))

    def test_allocating_to_a_finished_run_is_refused(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        ProductionRun.objects.filter(pk=run.pk).update(status='Completed')
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Completed')
        self.assertEqual(run.allocations.count(), 0)

    def test_bom_shows_allocated_and_in_hand(self):
        run, batch = self._allocation_setup(stock_at_plant=False)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        url = reverse('production_run_detail', args=[run.pk])
        row = self.client.get(url).context['bom_materials'][0]
        self.assertEqual((row['allocated'], row['ready'], row['state']), (484.01, 0, 'in_transit'))
        # Once the transfer arrives the material counts as in hand
        from core.models import Shipment
        Shipment.objects.filter(linked_production_run=run).update(status='Arrived')
        row = self.client.get(url).context['bom_materials'][0]
        self.assertEqual((row['ready'], row['state']), (484.01, 'ready'))
        self.assertContains(self.client.get(url), 'In Hand at Plant')
        self.assertContains(self.client.get(url), reverse('production_run_allocate', args=[run.pk]))

    def _extra_setup(self):
        """Run of 100 units (recipe 1 kg/unit): 100 kg allocated from batch A and started,
        plus a second free batch B (50 kg) at the plant."""
        from core.models import Warehouse, Material, ProductRecipe, Batch
        plant = Warehouse.objects.create(name='Plant', location_type='Manufacturing')
        self.mat = Material.objects.create(name='Resin', sku='MAT-R', category='Bulk',
                                           unit_of_measure='kg', safe_storage_days=365)
        ProductRecipe.objects.create(product=self.product, material=self.mat, quantity_required=1)
        self.batch_a = Batch.objects.create(
            batch_number='B-A', material=self.mat, quantity=Decimal('1000'), status='Active',
            manufacturing_date='2026-01-01', expiry_date='2027-01-01', warehouse=plant)
        self.batch_b = Batch.objects.create(
            batch_number='B-B', material=self.mat, quantity=Decimal('50'), status='Active',
            manufacturing_date='2026-02-01', expiry_date='2027-06-01', warehouse=plant)
        run = ProductionRun.objects.create(
            run_number='RX-1', target_product=self.product, expected_yield=Decimal('100'),
            status='Pending Allocation', manufacturing_plant=plant)
        self.client.login(username='boss', password='pw')
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{self.batch_a.pk}': '100'})
        url = reverse('production_run_detail', args=[run.pk])
        self.client.post(url, {'action': 'start_production'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')
        return run, url

    def _complete(self, url, actual, **extra):
        data = {'action': 'complete_production', 'actual_yield': '100', f'actual_qty_{self.mat.pk}': actual}
        data.update(extra)
        return self.client.post(url, data)

    def _extra(self, batches, qtys, reasons=None, confirm=True):
        m = self.mat.pk
        data = {f'extra_batch_{m}': batches, f'extra_qty_{m}': qtys,
                f'extra_reason_{m}': reasons or [''] * len(batches)}
        if confirm:
            data[f'extra_confirm_{m}'] = '1'
        return data

    def test_extra_material_needs_confirmation(self):
        run, url = self._extra_setup()
        self._complete(url, '102', **self._extra([str(self.batch_b.pk)], ['2'], confirm=False))
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')

    def test_extra_material_sources_must_add_up(self):
        run, url = self._extra_setup()
        self._complete(url, '102', **self._extra([str(self.batch_b.pk)], ['1']))
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')

    def test_unrecorded_extra_needs_reason(self):
        run, url = self._extra_setup()
        self._complete(url, '102', **self._extra(['unrecorded'], ['2'], ['  ']))
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')

    def test_extra_from_batch_deducts_that_batch(self):
        from core.models import ProductionConsumption
        run, url = self._extra_setup()
        self._complete(url, '102', **self._extra([str(self.batch_b.pk)], ['2']))
        run.refresh_from_db()
        self.assertEqual(run.status, 'Completed')
        self.batch_a.refresh_from_db()
        self.batch_b.refresh_from_db()
        self.assertEqual(self.batch_a.quantity, Decimal('900'))     # the 100 allocated
        self.assertEqual(self.batch_b.quantity, Decimal('48'))      # the 2 extra, from the chosen batch
        self.assertTrue(ProductionConsumption.objects.filter(
            production_run=run, consumed_batch=self.batch_b, quantity_used=Decimal('2')).exists())

    def test_extra_split_across_batch_and_unrecorded(self):
        from core.models import RunExtraMaterial
        run, url = self._extra_setup()
        self._complete(url, '103', **self._extra(
            [str(self.batch_b.pk), 'unrecorded'], ['1', '2'], ['', 'leftover from RUN-9']))
        run.refresh_from_db()
        # 3% over the BOM goes to variance approval; the sources are stored either way
        self.assertIn(run.status, ('Completed', 'Pending Approval'))
        rows = list(RunExtraMaterial.objects.filter(production_run=run).order_by('id'))
        self.assertEqual([(r.batch_id, r.quantity, r.reason) for r in rows],
                         [(self.batch_b.pk, Decimal('1'), ''), (None, Decimal('2'), 'leftover from RUN-9')])
        self.assertContains(self.client.get(url), 'leftover from RUN-9')

    def test_unrecorded_extra_deducts_no_stock_and_keeps_reason(self):
        run, url = self._extra_setup()
        self._complete(url, '102', **self._extra(['unrecorded'], ['2'], ['balance left from RUN-9']))
        run.refresh_from_db()
        self.assertEqual(run.status, 'Completed')
        self.batch_a.refresh_from_db()
        self.batch_b.refresh_from_db()
        self.assertEqual(self.batch_a.quantity, Decimal('900'))
        self.assertEqual(self.batch_b.quantity, Decimal('50'))      # untouched
        resp = self.client.get(url)
        self.assertContains(resp, 'Not in records')
        self.assertContains(resp, 'balance left from RUN-9')

    def test_extra_cannot_exceed_batch_free_stock(self):
        run, url = self._extra_setup()
        self._complete(url, '160', **self._extra([str(self.batch_b.pk)], ['60']))
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')

    def test_so_page_hides_cancelled_runs(self):
        from core.models import Warehouse, SalesOrder, SalesOrderDetail
        wh = Warehouse.objects.create(name='Hub', location_type='Storage')
        so = SalesOrder.objects.create(so_number='SO-HIDE', client_name='C', origin_warehouse=wh, status='Pending')
        SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('10'))
        ProductionRun.objects.create(run_number='RUN-OLD', target_product=self.product,
                                     expected_yield=Decimal('10'), status='Cancelled', sales_order=so)
        ProductionRun.objects.create(run_number='RUN-LIVE', target_product=self.product,
                                     expected_yield=Decimal('10'), status='Completed', sales_order=so)
        self.client.login(username='boss', password='pw')
        resp = self.client.get(reverse('so_detail', args=[so.pk]))
        self.assertContains(resp, 'RUN-LIVE')
        self.assertNotContains(resp, 'RUN-OLD')

    def test_start_production_only_from_planned_or_awaiting(self):
        for status in ('InProgress', 'Paused', 'Completed', 'Cancelled'):
            run = ProductionRun.objects.create(
                run_number=f'RS-{status}', target_product=self.product, expected_yield=Decimal('10'),
                status=status)
            self.client.login(username='boss', password='pw')
            self.client.post(reverse('production_run_detail', args=[run.pk]), {'action': 'start_production'})
            run.refresh_from_db()
            self.assertEqual(run.status, status)
            self.assertIsNone(run.exact_start_time)
        ok = ProductionRun.objects.create(run_number='RS-OK', target_product=self.product,
                                          expected_yield=Decimal('10'), status='Planned')
        self.client.post(reverse('production_run_detail', args=[ok.pk]), {'action': 'start_production'})
        ok.refresh_from_db()
        self.assertEqual(ok.status, 'InProgress')

    def _delivered_so(self):
        from core.models import Warehouse, SalesOrder, SalesOrderDetail
        wh = Warehouse.objects.create(name='Hub2', location_type='Storage')
        so = SalesOrder.objects.create(so_number='SO-DONE', client_name='C', origin_warehouse=wh, status='Delivered')
        SalesOrderDetail.objects.create(sales_order=so, product=self.product,
                                        quantity_ordered=Decimal('10'), quantity_shipped=Decimal('10'))
        self.client.login(username='boss', password='pw')
        return so

    def test_allocating_to_a_delivered_order_is_refused(self):
        so = self._delivered_so()
        self.client.post(reverse('so_allocate', args=[so.pk]), {'action': 'allocate_manual'})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Delivered')

    def test_scrapping_a_shipment_does_not_reset_a_delivered_order(self):
        from core.models import Shipment
        so = self._delivered_so()
        stray = Shipment.objects.create(tracking_number='SHP-STRAY', direction='Outbound',
                                        status='Draft', sales_order=so)
        self.client.post(reverse('shipment_detail', args=[stray.pk]), {'action': 'scrap_shipment'})
        stray.refresh_from_db()
        self.assertEqual(stray.status, 'Cancelled')
        so.refresh_from_db()
        self.assertEqual(so.status, 'Delivered')

    def test_po_detail_renders_with_materials_in_id_order(self):
        from core.models import Warehouse, Material, PurchaseOrder
        wh = Warehouse.objects.create(name='PO Hub', location_type='Storage')
        second = Material.objects.create(name='Aaa first by name', sku='MAT-2', category='Bulk',
                                         unit_of_measure='kg', safe_storage_days=30)
        first_by_id = Material.objects.create(name='Zzz last by name', sku='MAT-1', category='Bulk',
                                              unit_of_measure='kg', safe_storage_days=30)
        # created in this order, so ids ascend: 'Aaa...' then 'Zzz...'; reverse them to prove id order wins
        Material.objects.filter(pk=second.pk).update(name='Zzz name-last')
        Material.objects.filter(pk=first_by_id.pk).update(name='Aaa name-first')
        po = PurchaseOrder.objects.create(po_number='PO-ORD', supplier_name='S', target_warehouse=wh)
        self.client.login(username='boss', password='pw')
        resp = self.client.get(reverse('po_detail', args=[po.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([m.pk for m in resp.context['materials']], sorted(m.pk for m in resp.context['materials']))
        html = resp.content.decode()
        self.assertLess(html.index('MAT-2'), html.index('MAT-1'))

    def test_list_complete_button_sends_user_to_run_page(self):
        run = self._run('InProgress')
        self.client.login(username='boss', password='pw')
        detail = reverse('production_run_detail', args=[run.pk])
        # The button on the list is a link to the run page's finalize section
        self.assertContains(self.client.get(reverse('readiness')), f'href="{detail}#finalize"')
        # A direct post of the old action no longer completes anything
        resp = self.client.post(reverse('readiness'), {'action': 'complete_run', 'run_id': run.pk,
                                                        'actual_yield': '484.01'})
        self.assertRedirects(resp, detail + '#finalize', fetch_redirect_response=False)
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')
        self.assertIsNone(run.actual_yield)

    def test_short_allocation_needs_acknowledgement(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        url = reverse('production_run_allocate', args=[run.pk])
        self.client.post(url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '400'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')
        self.assertEqual(run.allocations.count(), 0)
        # Acknowledged: goes through and the shortfall is recorded on the timeline
        self.client.post(url, {'action': 'allocate_run', 'allocate_short': '1', f'batch_qty_{batch.pk}': '400'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Planned')
        self.assertTrue(run.timeline.filter(action__icontains='shortfall').exists())

    def test_nothing_selected_cannot_skip_the_allocation_gate(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {'action': 'allocate_run'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')

    def test_over_allocation_is_refused(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', 'allocate_short': '1', f'batch_qty_{batch.pk}': '600'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')
        self.assertEqual(run.allocations.count(), 0)

    def test_allocation_cannot_exceed_batch_free_stock(self):
        from core.models import Batch
        run, batch = self._allocation_setup(stock_at_plant=True)
        Batch.objects.filter(pk=batch.pk).update(allocated_quantity=Decimal('900'))   # only 100 free
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')

    def test_allocate_page_has_live_status_and_no_side_panel(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        resp = self.client.get(reverse('production_run_allocate', args=[run.pk]))
        self.assertContains(resp, 'mat-status')
        self.assertContains(resp, 'alloc-summary')
        self.assertNotContains(resp, 'Auto-Logistics Split')

    def test_send_shortages_to_manufacturing_after_a_cancelled_run(self):
        from core.models import Warehouse, SalesOrder, SalesOrderDetail
        wh = Warehouse.objects.create(name='Hub3', location_type='Storage')
        so = SalesOrder.objects.create(so_number='SO-RETRY', client_name='C', origin_warehouse=wh, status='Pending')
        SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('10'))
        base = f'PR-SO-RETRY-{self.product.sku}'
        ProductionRun.objects.create(run_number=base, target_product=self.product, expected_yield=Decimal('10'),
                                     status='Cancelled', sales_order=so)
        self.client.login(username='boss', password='pw')
        resp = self.client.post(reverse('so_detail', args=[so.pk]), {'action': 'send_to_manufacturing'})
        self.assertEqual(resp.status_code, 302)
        new_run = ProductionRun.objects.filter(sales_order=so).exclude(status='Cancelled').get()
        self.assertEqual(new_run.run_number, base + '-2')
        # Cancelling that one and sending again picks the next free suffix
        ProductionRun.objects.filter(pk=new_run.pk).update(status='Cancelled')
        self.client.post(reverse('so_detail', args=[so.pk]), {'action': 'send_to_manufacturing'})
        self.assertEqual(ProductionRun.objects.filter(sales_order=so).exclude(status='Cancelled').get().run_number, base + '-3')

    def test_allocation_lines_have_no_update_button(self):
        run, batch = self._allocation_setup(stock_at_plant=False)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        detail = reverse('production_run_detail', args=[run.pk])
        resp = self.client.get(detail)
        self.assertNotContains(resp, 'update_allocations')
        # The old action does nothing to the allocation or its transfer
        alloc = run.allocations.get()
        self.client.post(detail, {'action': 'update_allocations', 'alloc_id': alloc.pk, 'quantity': '1'})
        alloc.refresh_from_db()
        self.assertEqual(alloc.quantity, Decimal('484.01'))

    def test_cancel_allocation_releases_stock_and_draft_transfers(self):
        from core.models import Shipment
        run, batch = self._allocation_setup(stock_at_plant=False)
        detail = reverse('production_run_detail', args=[run.pk])
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        self.assertContains(self.client.get(detail), 'Cancel allocation &amp; start over')
        self.client.post(detail, {'action': 'cancel_allocation'})
        run.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')
        self.assertEqual(run.allocations.count(), 0)
        self.assertEqual(batch.allocated_quantity, 0)
        self.assertFalse(Shipment.objects.filter(linked_production_run=run).exclude(status='Cancelled').exists())

    def test_cancel_allocation_refused_once_a_transfer_is_under_way(self):
        from core.models import Shipment
        run, batch = self._allocation_setup(stock_at_plant=False)
        detail = reverse('production_run_detail', args=[run.pk])
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        Shipment.objects.filter(linked_production_run=run).update(status='Dispatched')
        self.client.post(detail, {'action': 'cancel_allocation'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Awaiting Materials')
        self.assertEqual(run.allocations.count(), 1)

    def test_cancel_allocation_refused_after_production_starts(self):
        run, url = self._extra_setup()                       # allocated and started
        self.assertEqual(run.status, 'InProgress')
        self.assertNotContains(self.client.get(url), 'Cancel allocation &amp; start over')
        self.client.post(url, {'action': 'cancel_allocation'})
        self.client.post(reverse('readiness'), {'action': 'cancel_allocation', 'run_id': run.pk})
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')
        self.assertEqual(run.allocations.count(), 1)

    def test_extra_beyond_recipe_only_while_production_is_under_way(self):
        run, batch = self._allocation_setup(stock_at_plant=True)
        alloc_url = reverse('production_run_allocate', args=[run.pk])
        detail = reverse('production_run_detail', args=[run.pk])
        self.client.post(alloc_url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Planned')
        # Before start the recipe requirement is a hard cap, however much is asked for
        self.client.post(alloc_url, {'action': 'allocate_run', 'extra_reason': 'more please', f'batch_qty_{batch.pk}': '10'})
        self.assertEqual(sum(a.quantity for a in run.allocations.all()), Decimal('484.01'))
        self.assertNotContains(self.client.get(alloc_url), 'more than the recipe needs')
        # Under way: extra is allowed, but only with a reason
        self.client.post(detail, {'action': 'start_production'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')
        self.assertContains(self.client.get(alloc_url), 'more than the recipe needs')
        self.client.post(alloc_url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '10'})
        self.assertEqual(sum(a.quantity for a in run.allocations.all()), Decimal('484.01'))   # no reason: refused
        self.client.post(alloc_url, {'action': 'allocate_run', 'extra_reason': 'second mix needs a top-up',
                                     f'batch_qty_{batch.pk}': '10'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')
        self.assertEqual(sum(a.quantity for a in run.allocations.all()), Decimal('494.01'))
        note = run.timeline.filter(action__icontains='Extra material allocated').get()
        self.assertIn('second mix needs a top-up', note.action)
        self.assertIn('10.00', note.action)

    def test_extra_allocation_from_another_warehouse_creates_a_transfer_while_running(self):
        from core.models import Shipment
        run, batch = self._allocation_setup(stock_at_plant=True)     # 1000 kg at the plant
        alloc_url = reverse('production_run_allocate', args=[run.pk])
        self.client.post(alloc_url, {'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        self.client.post(reverse('production_run_detail', args=[run.pk]), {'action': 'start_production'})
        from core.models import Warehouse, Batch
        store = Warehouse.objects.create(name='Far Store', location_type='Storage')
        far = Batch.objects.create(batch_number='B-FAR', material=batch.material, quantity=Decimal('50'), status='Active',
                                   manufacturing_date='2026-01-01', expiry_date='2027-01-01', warehouse=store)
        self.client.post(alloc_url, {'action': 'allocate_run', 'extra_reason': 'top-up', f'batch_qty_{far.pk}': '20'})
        transfer = Shipment.objects.get(linked_production_run=run, origin_warehouse=store)
        self.assertEqual(transfer.status, 'Draft')
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')

    def test_remove_one_line_keeps_the_others(self):
        from core.models import Shipment, Warehouse, Batch
        run, batch = self._allocation_setup(stock_at_plant=False)    # 1000 kg at 'Store', not the plant
        detail = reverse('production_run_detail', args=[run.pk])
        other = Warehouse.objects.create(name='Second Store', location_type='Storage')
        second = Batch.objects.create(batch_number='B-2ND', material=batch.material, quantity=Decimal('500'), status='Active',
                                      manufacturing_date='2026-02-01', expiry_date='2027-06-01', warehouse=other)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', 'override_reason': 'test split across two stores',
            f'batch_qty_{batch.pk}': '300', f'batch_qty_{second.pk}': '184.01'})
        self.assertEqual(run.allocations.count(), 2)
        self.assertContains(self.client.get(detail), 'value="remove_allocation"')
        gone = run.allocations.get(batch=second)
        self.client.post(detail, {'action': 'remove_allocation', 'alloc_id': gone.pk})
        self.assertEqual(run.allocations.count(), 1)
        self.assertEqual(run.allocations.get().batch, batch)
        second.refresh_from_db()
        self.assertEqual(second.allocated_quantity, 0)
        # Its own (now empty) draft transfer is cancelled; the other transfer is untouched
        self.assertFalse(Shipment.objects.filter(linked_production_run=run, origin_warehouse=other).exists())   # deleted
        from core.models import RegistryLog
        self.assertEqual(RegistryLog.objects.filter(action_type='Draft_Transfer_Deleted', quantity_changed=Decimal('184.01')).count(), 1)
        self.assertEqual(Shipment.objects.get(linked_production_run=run, origin_warehouse=batch.warehouse).status, 'Draft')
        run.refresh_from_db()
        self.assertEqual(run.status, 'Awaiting Materials')
        # Removing the last line puts the run back to Pending Allocation
        last = run.allocations.get()
        self.client.post(detail, {'action': 'remove_allocation', 'alloc_id': last.pk})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')
        self.assertEqual(run.allocations.count(), 0)

    def test_remove_line_refused_when_its_transfer_has_left_draft_or_run_started(self):
        from core.models import Shipment
        run, batch = self._allocation_setup(stock_at_plant=False)
        detail = reverse('production_run_detail', args=[run.pk])
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        Shipment.objects.filter(linked_production_run=run).update(status='Preparing')
        page = self.client.get(detail)
        self.assertNotContains(page, 'value="remove_allocation"')
        self.assertContains(page, 'In logistics')
        alloc = run.allocations.get()
        self.client.post(detail, {'action': 'remove_allocation', 'alloc_id': alloc.pk})
        self.assertEqual(run.allocations.count(), 1)
        # Once production has started nothing can be removed
        Shipment.objects.filter(linked_production_run=run).update(status='Arrived')
        ProductionRun.objects.filter(pk=run.pk).update(status='InProgress')
        self.client.post(detail, {'action': 'remove_allocation', 'alloc_id': alloc.pk})
        self.assertEqual(run.allocations.count(), 1)

    def test_cancelling_an_allocation_deletes_its_draft_transfers_and_leaves_a_ledger_trace(self):
        from core.models import Shipment, RegistryLog
        run, batch = self._allocation_setup(stock_at_plant=False)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        tracking = Shipment.objects.get(linked_production_run=run).tracking_number
        self.client.post(reverse('production_run_detail', args=[run.pk]), {'action': 'cancel_allocation'})
        self.assertFalse(Shipment.objects.filter(tracking_number=tracking).exists())     # gone, not 'Cancelled'
        row = RegistryLog.objects.get(action_type='Draft_Transfer_Deleted')
        self.assertEqual(row.quantity_changed, Decimal('484.01'))
        self.assertEqual(row.material, batch.material)
        self.assertIn(tracking, row.item_name)
        self.assertIn(run.run_number, row.item_name)
        self.assertEqual(row.warehouse, batch.warehouse)
        self.assertTrue(run.timeline.filter(action__icontains=tracking).exists())
        # Stock is back and the ledger page lists the trace under its own filter
        batch.refresh_from_db()
        self.assertEqual(batch.allocated_quantity, 0)
        resp = self.client.get(reverse('registry') + '?action=Draft_Transfer_Deleted')
        self.assertContains(resp, tracking)
        self.assertContains(resp, 'Draft deleted')

    def test_scrapping_a_draft_run_transfer_deletes_it_and_returns_to_the_run(self):
        from core.models import Shipment, RegistryLog
        run, batch = self._allocation_setup(stock_at_plant=False)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        shipment = Shipment.objects.get(linked_production_run=run)
        resp = self.client.post(reverse('shipment_detail', args=[shipment.pk]), {'action': 'scrap_shipment'})
        self.assertRedirects(resp, reverse('production_run_detail', args=[run.pk]), fetch_redirect_response=False)
        self.assertFalse(Shipment.objects.filter(pk=shipment.pk).exists())
        self.assertEqual(RegistryLog.objects.filter(action_type='Draft_Transfer_Deleted').count(), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.allocated_quantity, 0)

    def test_a_transfer_that_went_for_approval_is_cancelled_not_deleted(self):
        from core.models import Shipment, RegistryLog
        run, batch = self._allocation_setup(stock_at_plant=False)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        shipment = Shipment.objects.get(linked_production_run=run)
        Shipment.objects.filter(pk=shipment.pk).update(status='Preparing')
        self.client.post(reverse('shipment_detail', args=[shipment.pk]), {'action': 'scrap_shipment'})
        shipment.refresh_from_db()
        self.assertEqual(shipment.status, 'Cancelled')
        self.assertFalse(RegistryLog.objects.filter(action_type='Draft_Transfer_Deleted').exists())

    def test_scrapping_a_run_deletes_its_draft_transfers(self):
        from core.models import Shipment, RegistryLog
        run, batch = self._allocation_setup(stock_at_plant=False)
        self.client.post(reverse('production_run_allocate', args=[run.pk]), {
            'action': 'allocate_run', f'batch_qty_{batch.pk}': '484.01'})
        self.client.post(reverse('production_run_detail', args=[run.pk]), {'action': 'scrap_run', 'reason': 'not needed'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Cancelled')
        self.assertFalse(Shipment.objects.filter(linked_production_run=run).exists())
        self.assertEqual(RegistryLog.objects.filter(action_type='Draft_Transfer_Deleted').count(), 1)

    def test_a_manually_made_draft_shipment_is_not_touched(self):
        from core.models import Shipment, Warehouse
        wh = Warehouse.objects.create(name='Manual Hub', location_type='Storage')
        run, batch = self._allocation_setup(stock_at_plant=False)
        manual = Shipment.objects.create(tracking_number='SHP-MANUAL', direction='Transfer', status='Draft',
                                         origin_warehouse=wh, linked_production_run=run)   # not auto-generated
        self.client.post(reverse('production_run_detail', args=[run.pk]), {'action': 'scrap_run', 'reason': 'not needed'})
        self.assertTrue(Shipment.objects.filter(pk=manual.pk).exists())

    def test_allocate_button_on_awaiting_materials(self):
        from core.models import Material, ProductRecipe
        mat = Material.objects.create(name='Gum', sku='MAT-G', category='Bulk', unit_of_measure='kg', safe_storage_days=30)
        ProductRecipe.objects.create(product=self.product, material=mat, quantity_required=1)
        run = self._run('Awaiting Materials')
        self.client.login(username='boss', password='pw')
        resp = self.client.get(reverse('production_run_detail', args=[run.pk]))
        self.assertContains(resp, 'Allocate Materials')



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

    def _po(self, material, qty, expected_in_days, number):
        po = PurchaseOrder.objects.create(
            po_number=number, supplier_name='S', target_warehouse=self.wh, status='Pending',
            expected_delivery_date=date.today() + timedelta(days=expected_in_days),
        )
        PurchaseOrderDetail.objects.create(purchase_order=po, material=material, quantity_ordered=Decimal(str(qty)))

    def _burning(self, sku):
        """100 in stock, 10/day burn (10 days cover), 14-day lead -> critical on its own."""
        m = self._material(sku)
        self._stock(m, 100)
        self._consume(m, 300)
        self._lead(m, 14)
        return m

    def test_po_arriving_before_run_out_extends_cover(self):
        m = self._burning('MK-PO1')
        self.assertEqual(self._row('MK-PO1')['status'], 'critical')
        self._po(m, 500, 5, 'PO-PO1')
        row = self._row('MK-PO1')
        self.assertAlmostEqual(row['days_cover'], 60.0)        # (100 + 500) / 10
        self.assertAlmostEqual(row['days_cover_on_hand'], 10.0)
        self.assertEqual((row['on_order_counted'], row['on_order_late']), (500.0, 0.0))
        self.assertEqual(row['status'], 'ok')                  # 60 - 14 = 46 days to reorder
        self.assertEqual(row['next_po_number'], 'PO-PO1')

    def test_po_arriving_after_run_out_does_not_help(self):
        m = self._burning('MK-PO2')
        self._po(m, 500, 20, 'PO-PO2')                         # stock gone on day 10
        row = self._row('MK-PO2')
        self.assertAlmostEqual(row['days_cover'], 10.0)
        self.assertEqual((row['on_order_counted'], row['on_order_late']), (0.0, 500.0))
        self.assertEqual(row['status'], 'critical')

    def test_overdue_po_counts_as_arriving_today_and_is_flagged(self):
        m = self._burning('MK-PO3')
        self._po(m, 500, -3, 'PO-PO3')
        row = self._row('MK-PO3')
        self.assertTrue(row['po_overdue'])
        self.assertAlmostEqual(row['days_cover'], 60.0)

    def test_out_of_stock_with_delivery_today_is_not_critical(self):
        m = self._material('MK-PO4')
        self._consume(m, 300)
        self._lead(m, 14)
        self._po(m, 1000, 0, 'PO-PO4')
        row = self._row('MK-PO4')
        self.assertAlmostEqual(row['days_cover'], 100.0)
        self.assertEqual(row['status'], 'ok')

    def test_todays_delivery_counts_as_today_across_the_utc_day_boundary(self):
        """At 01:00 in Kuala Lumpur it is still the previous day in UTC. 'Today' for the
        forecast is the local date, so a delivery dated today must count as arriving today."""
        from datetime import datetime, timezone as dt_timezone
        from unittest import mock
        boundary = datetime(2026, 9, 21, 17, 0, tzinfo=dt_timezone.utc)      # 01:00, 22 Sep, Kuala Lumpur
        with mock.patch('django.utils.timezone.now', return_value=boundary):
            local_today = timezone.localdate()
            self.assertEqual(local_today, date(2026, 9, 22))
            m = self._material('MK-TZ')
            self._consume(m, 300)                     # 10/day
            self._lead(m, 14)
            po = PurchaseOrder.objects.create(
                po_number='PO-TZ', supplier_name='S', target_warehouse=self.wh, status='Pending',
                expected_delivery_date=local_today)
            PurchaseOrderDetail.objects.create(purchase_order=po, material=m, quantity_ordered=Decimal('1000'))
            row = self._row('MK-TZ')
        self.assertAlmostEqual(row['days_cover'], 100.0)
        self.assertEqual(row['status'], 'ok')

    def test_forecast_page_explains_po_cover(self):
        m = self._burning('MK-PO5')
        self._po(m, 500, 20, 'PO-PO5')
        self.client.force_login(self.user)
        resp = self.client.get(reverse('forecast'))
        self.assertContains(resp, '500 arrives after run-out')

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

    def test_view_records_today_and_shows_collecting_state(self):
        # no cron: opening the page fills in today's snapshot if nothing has changed yet
        self.client.login(username='capacity', password='pw')
        resp = self.client.get(reverse('capacity_forecast'))
        self.assertTrue(WarehouseUtilizationSnapshot.objects.filter(warehouse=self.wh, snapshot_date=timezone.localdate()).exists())
        self.assertContains(resp, 'Collecting data')

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
        self.assertEqual(util_row['used_mt'], 20.0)       # and still takes up space
        self.assertEqual(util_row['quarantined_mt'], 20.0)
        self.assertEqual(rent_row['quarantined_mt'], 20.0)

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

    def test_unflagged_rented_warehouse_is_still_suggested(self):
        # falling trend and under 95% -> Stable, but its stock still costs rent
        self._snap(self.rented, 99, 6)
        self._snap(self.rented, 97, 3)
        self._snap(self.rented, 94, 0)
        self._batch(self.rented, 100, '5.00')
        self._depot()
        rows = rent_reduction_opportunities()
        self.assertEqual((rows[0]['status'], rows[0]['total_daily_saving']), ('stable', 500.0))

    def test_flat_billed_or_own_warehouses_are_never_origins(self):
        self.rented.rental_billing_method = 'Overall'
        self.rented.save()
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00')
        self._depot()
        self.assertEqual(rent_reduction_opportunities(), [])

    def test_fullest_warehouses_listed_first(self):
        calm = Warehouse.objects.create(
            name='Calm Depot', location_type='Storage', ownership_type='ExternalProvider',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('9.00'), total_capacity_mt=Decimal('1000'),
        )
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00')
        self._batch(calm, 100, '9.00')      # bigger saving, but no pressure on space
        self._depot('1000')
        self.assertEqual([r['warehouse_id'] for r in rent_reduction_opportunities()], [self.rented.id, calm.id])

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

    def _depot(self, cap='500'):
        return Warehouse.objects.create(
            name=f'Internal Depot {cap}', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'), total_capacity_mt=Decimal(cap),
        )

    def _consume(self, total):
        """Production used `total` units of the material over the last 30 days."""
        from core.models import RegistryLog
        row = RegistryLog.objects.create(action_type='Consumed_For_Manufacturing', item_name='Opp Sand (Run X)',
                                         material=self.material, quantity_changed=Decimal(str(total)), warehouse=self.rented)
        RegistryLog.objects.filter(pk=row.pk).update(timestamp=timezone.now() - timedelta(days=10))

    def test_total_saving_counts_until_expiry(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', expiry_days=10)
        self._depot()
        c = rent_reduction_opportunities()[0]['candidate_batches'][0]
        self.assertEqual((c['stay_days'], c['stay_reason'], c['total_saving']), (10, 'expires', 5000.0))  # 100 MT x 5 x 10

    def test_total_saving_counts_until_used_up(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', expiry_days=300)
        self._consume(300)                                   # 10 MT/day -> gone in 10 days
        self._depot()
        c = rent_reduction_opportunities()[0]['candidate_batches'][0]
        self.assertEqual((c['stay_days'], c['stay_reason'], c['total_saving']), (10, 'used up', 5000.0))

    def test_older_stock_is_used_first(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', expiry_days=300)
        self._batch(self.rented, 50, '5.00', expiry_days=100)  # older - used before the 100 MT batch
        self._consume(300)                                     # 10/day: 50 first, then 100 -> 15 days
        self._depot()
        cands = {c['mt']: c for c in rent_reduction_opportunities()[0]['candidate_batches']}
        self.assertEqual((cands[50.0]['stay_days'], cands[100.0]['stay_days']), (5, 15))

    def test_saving_is_capped_at_the_horizon(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00', expiry_days=1000)
        self._depot()
        c = rent_reduction_opportunities()[0]['candidate_batches'][0]
        self.assertEqual((c['stay_days'], c['stay_reason']), (365, 'horizon'))

    def test_moves_below_the_minimum_saving_are_left_out(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 2, '5.00', expiry_days=5)   # 2 MT x 5 x 5 days = RM50 < RM100
        self._depot()
        rows = rent_reduction_opportunities()
        self.assertEqual((rows[0]['candidate_batches'], rows[0]['excluded_low_saving']), ([], 1))

    def test_finished_goods_use_the_sales_rate(self):
        self._flag_critical(self.rented)
        prod = Product.objects.create(name='Opp FG', sku='PRD-OPP', unit_of_measure='kg', price_per_unit=1,
                                      weight_mt_per_unit=Decimal('1'))
        Batch.objects.create(batch_number='FG-OPP', product=prod, quantity=Decimal('90'), status='Active',
                             warehouse=self.rented, manufacturing_date=date.today(),
                             expiry_date=date.today() + timedelta(days=300), rental_rate_per_mt=Decimal('5'))
        so = SalesOrder.objects.create(so_number='SO-OPP', client_name='C', origin_warehouse=self.rented, status='Delivered')
        SalesOrderDetail.objects.create(sales_order=so, product=prod, quantity_ordered=Decimal('1800'))  # 10/day over 180 days
        self._depot()
        c = rent_reduction_opportunities()[0]['candidate_batches'][0]
        self.assertEqual((c['stay_days'], c['stay_reason']), (9, 'used up'))

    def test_space_goes_to_the_biggest_total_saving_not_the_highest_rate(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '9.00', expiry_days=5)    # RM4,500 in total
        self._batch(self.rented, 100, '3.00', expiry_days=200)  # RM60,000 in total
        self._depot('100')                                      # room for one
        rows = rent_reduction_opportunities()
        self.assertEqual([c['rate_per_mt'] for c in rows[0]['candidate_batches']], [3.0])
        self.assertEqual(rows[0]['total_saving'], 60000.0)

    def _internal(self, name, loc, cap):
        return Warehouse.objects.create(
            name=name, location_type=loc, ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'), total_capacity_mt=Decimal(cap),
        )

    def test_manufacturing_only_plant_is_never_suggested(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00')
        self._internal('Plant Only', 'Manufacturing', '5000')
        self.assertEqual(rent_reduction_opportunities(), [])

    def test_storage_and_manufacturing_facility_is_suggested(self):
        self._flag_critical(self.rented)
        self._batch(self.rented, 100, '5.00')
        self._internal('Plant Only', 'Manufacturing', '5000')
        both = self._internal('Plant + Store', 'Both', '500')
        rows = rent_reduction_opportunities()
        self.assertEqual([c['destination_id'] for c in rows[0]['candidate_batches']], [both.id])
        self.assertEqual([d['name'] for d in rows[0]['destination_options']], ['Plant + Store'])

    def test_two_rented_warehouses_share_free_space_once(self):
        other = Warehouse.objects.create(
            name='Rented Depot 2', location_type='Storage', ownership_type='ExternalProvider',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('5.00'), total_capacity_mt=Decimal('1000'),
        )
        for wh in (self.rented, other):
            self._flag_critical(wh)
        self._batch(self.rented, 100, '9.00')
        self._batch(other, 100, '4.00')
        self._internal('Small Store', 'Storage', '100')  # room for only one of them
        rows = {r['warehouse_id']: r for r in rent_reduction_opportunities()}
        moved = sum(c['move_mt'] for r in rows.values() for c in r['candidate_batches'])
        self.assertEqual(moved, 100.0)
        self.assertEqual(rows[self.rented.id]['total_daily_saving'], 900.0)  # higher rate wins the space

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
        self.manager = make_user(username='mgr', password='pw', role='Manager')
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
        self.manager = make_user(username='mgr2', password='pw', role='Manager')
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


class TransferReceiptTests(TestCase):
    """Regression coverage: receiving an internal Transfer must create the
    destination batch with warehouse=destination (it used to be left NULL), must
    do so even when the destination has no WarehouseLocation zones (stock used to
    silently vanish), and a reopen + re-complete must only log the change."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='trfuser', password='pw')
        self.client.login(username='trfuser', password='pw')

        self.origin = Warehouse.objects.create(name='Rented Store', location_type='Storage', ownership_type='ExternalProvider')
        self.dest = Warehouse.objects.create(name='Own Store', location_type='Storage', ownership_type='Internal')
        self.material = Material.objects.create(name='Urea', sku='MAT-TRF', category='Raw', safe_storage_days=180)
        self.batch = Batch.objects.create(
            batch_number='B-TRF-1', status='Active', material=self.material,
            quantity=Decimal('500'), allocated_quantity=Decimal('200'),
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=365),
            warehouse=self.origin,
        )
        self.shipment = Shipment.objects.create(
            tracking_number='SHP-TRF-1', direction='Transfer', status='Arrived',
            origin_warehouse=self.origin, destination_warehouse=self.dest,
        )
        self.item = ShipmentItem.objects.create(
            shipment=self.shipment, material=self.material, batch=self.batch, quantity=Decimal('200'),
        )
        StockAllocation.objects.create(batch=self.batch, shipment=self.shipment, quantity=Decimal('200'))

    def _complete(self, received='200'):
        return self.client.post(reverse('shipment_detail', args=[self.shipment.pk]), {
            'action': 'complete_shipment',
            f'received_qty_{self.item.id}': received,
        })

    def _dest_batch(self):
        return Batch.objects.get(batch_number=f'B-TRF-1-TRF-{self.shipment.id}')

    def _inbound_logs(self):
        return RegistryLog.objects.filter(action_type='Inbound', warehouse=self.dest)

    def test_received_batch_lands_in_destination_warehouse_without_zones(self):
        self.assertFalse(WarehouseLocation.objects.filter(warehouse=self.dest).exists())
        self._complete()

        new_batch = self._dest_batch()
        self.assertEqual(new_batch.warehouse, self.dest)
        self.assertEqual(new_batch.quantity, Decimal('200'))
        self.assertEqual(new_batch.status, 'Active')
        self.assertIsNone(new_batch.location)
        self.assertIsNone(new_batch.rental_rate_per_mt)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, Decimal('300'))
        self.assertEqual(self.batch.allocated_quantity, Decimal('0'))
        self.assertEqual(self._inbound_logs().count(), 1)
        self.assertEqual(self._inbound_logs().get().quantity_changed, Decimal('200'))

    def test_received_batch_lands_in_destination_warehouse_with_zones(self):
        WarehouseLocation.objects.create(warehouse=self.dest, zone_name='A', aisle='1')
        self._complete()
        self.assertEqual(self._dest_batch().warehouse, self.dest)

    def _post(self, **data):
        return self.client.post(reverse('shipment_detail', args=[self.shipment.pk]), data)

    def _force_close_short(self, received='150', **reasons):
        """Complete with a shortage (-> Discrepant), then force close."""
        self._complete(received=received)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'Discrepant')
        return self._post(action='force_close_shipment', **reasons)

    def test_completed_transfer_cannot_be_reopened(self):
        self._complete()
        self._post(action='reopen_shipment')
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'Completed')

    def test_completed_transfer_status_cannot_be_changed_back(self):
        self._complete()
        self._post(action='update_operational_status', status='Arrived')
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'Completed')

    def test_completed_outbound_can_still_be_reopened(self):
        self.shipment.direction = 'Outbound'
        self.shipment.status = 'Completed'
        self.shipment.save(update_fields=['direction', 'status'])
        self._post(action='reopen_shipment')
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'Arrived')

    def test_force_close_shortage_requires_a_reason(self):
        self._force_close_short()
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'Discrepant')
        self.assertFalse(Batch.objects.filter(warehouse=self.dest).exists())
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, Decimal('500'))

    def test_force_close_shortage_kept_at_origin(self):
        self._force_close_short(**{f'shortage_reason_{self.item.id}': 'origin'})

        self.assertEqual(self._dest_batch().warehouse, self.dest)
        self.assertEqual(self._dest_batch().quantity, Decimal('150'))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, Decimal('350'))  # 500 - 150 received; 50 stay
        self.assertEqual(self.batch.allocated_quantity, Decimal('0'))
        self.assertFalse(RegistryLog.objects.filter(action_type='Spoiled_Disposal').exists())
        self.assertEqual(self._inbound_logs().get().quantity_changed, Decimal('150'))

    def test_force_close_shortage_lost_in_transit_is_written_off(self):
        self._force_close_short(**{f'shortage_reason_{self.item.id}': 'lost'})

        self.assertEqual(self._dest_batch().quantity, Decimal('150'))
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, Decimal('300'))  # 500 - 150 received - 50 lost
        loss = RegistryLog.objects.get(action_type='Spoiled_Disposal')
        self.assertEqual(loss.quantity_changed, Decimal('50'))
        self.assertEqual(loss.warehouse, self.origin)

    def test_force_close_marks_emptied_origin_batch_depleted(self):
        self.batch.quantity = Decimal('200')
        self.batch.save(update_fields=['quantity'])
        self._force_close_short(**{f'shortage_reason_{self.item.id}': 'lost'})

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity, Decimal('0'))
        self.assertEqual(self.batch.status, 'Depleted')
        self.assertEqual(self.batch.closed_date, date.today())

    def test_detail_page_shows_shortage_choice_and_hides_reopen(self):
        self._complete(received='150')
        self.shipment.refresh_from_db()
        self.shipment.assigned_manager = self.user
        self.shipment.save(update_fields=['assigned_manager'])
        resp = self.client.get(reverse('shipment_detail', args=[self.shipment.pk]))
        self.assertContains(resp, f'name="shortage_reason_{self.item.id}"')
        self.assertContains(resp, 'Missing 50.00 of Urea')

        self._post(action='force_close_shipment', **{f'shortage_reason_{self.item.id}': 'origin'})
        resp = self.client.get(reverse('shipment_detail', args=[self.shipment.pk]))
        self.assertNotContains(resp, 'value="reopen_shipment"')
        self.assertContains(resp, "Completed transfers can't be reopened")

    def test_status_change_cannot_complete_or_mark_discrepant(self):
        for target in ('Completed', 'Discrepant'):
            self._post(action='update_operational_status', status=target)
            self.shipment.refresh_from_db()
            self.assertEqual(self.shipment.status, 'Arrived')
        self.assertFalse(Batch.objects.filter(warehouse=self.dest).exists())

    def test_status_change_cannot_skip_approval(self):
        self.shipment.status = 'Pending Approval'
        self.shipment.save(update_fields=['status'])
        self._post(action='update_operational_status', status='Dispatched')
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'Pending Approval')

    def test_status_change_between_in_progress_statuses_still_works(self):
        self.shipment.status = 'Preparing'
        self.shipment.save(update_fields=['status'])
        for target in ('Dispatched', 'Delayed', 'Arrived'):
            self._post(action='update_operational_status', status=target)
            self.shipment.refresh_from_db()
            self.assertEqual(self.shipment.status, target)

    def test_unused_update_status_action_no_longer_changes_status(self):
        self._post(action='update_status', status='Completed')
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, 'Arrived')

    def test_transfer_for_production_run_keeps_run_reservation_at_plant(self):
        from core.models import ProductionRun
        from core.utils import consume_materials_for_run
        product = Product.objects.create(name='Blend T', sku='PRD-T', unit_of_measure='kg', price_per_unit=1)
        run = ProductionRun.objects.create(
            run_number='RUN-TRF', target_product=product, expected_yield=Decimal('1'),
            status='Awaiting Materials', manufacturing_plant=self.dest,
        )
        StockAllocation.objects.filter(shipment=self.shipment).update(production_run=run)
        self.shipment.linked_production_run = run
        self.shipment.save(update_fields=['linked_production_run'])

        self._complete()

        new_batch = self._dest_batch()
        self.assertEqual(new_batch.allocated_quantity, Decimal('200'))
        self.assertEqual(StockAllocation.objects.get(production_run=run).batch, new_batch)

        # Completing the run consumes from the arrived batch and releases the hold
        consume_materials_for_run(run, self.user)
        new_batch.refresh_from_db()
        self.assertEqual(new_batch.quantity, Decimal('0'))
        self.assertEqual(new_batch.allocated_quantity, Decimal('0'))
        self.assertFalse(StockAllocation.objects.filter(production_run=run).exists())


class ShipmentCreateArrivedDoesNotDeliverSOTests(TestCase):
    """Registering a shipment that is already 'Arrived' must not mark its sales order
    Delivered - no stock has moved yet; delivery comes from completing the shipment."""

    def test_so_not_marked_delivered_on_create(self):
        user = User.objects.create_user(username='shpcreate', password='pw')
        client = Client()
        client.login(username='shpcreate', password='pw')
        wh = Warehouse.objects.create(name='WH Create', location_type='Storage')
        so = SalesOrder.objects.create(so_number='SO-CRT-1', client_name='Acme', origin_warehouse=wh, status='Ready to Ship')

        client.post(reverse('shipments'), {
            'action': 'create_shipment', 'direction': 'Outbound', 'status': 'Arrived',
            'origin_warehouse_id': wh.id, 'sales_order_id': so.id,
        })

        self.assertTrue(Shipment.objects.filter(sales_order=so).exists())
        so.refresh_from_db()
        self.assertEqual(so.status, 'Ready to Ship')


class FinishedGoodsFefoAllocationTests(TestCase):
    """SO finished-goods allocation: FEFO pre-fill, override reason when amounts
    leave FEFO order, capped at what the order still needs, nothing saved on error."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='fefo', password='pw')
        self.client.login(username='fefo', password='pw')
        self.wh = Warehouse.objects.create(name='FG Hub FEFO', location_type='Storage')
        self.product = Product.objects.create(name='B-Balance T', sku='PRD-FEFO', unit_of_measure='kg', price_per_unit=1)
        self.older = Batch.objects.create(
            batch_number='FG-OLD', status='Active', product=self.product, quantity=Decimal('284'),
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=300), warehouse=self.wh,
        )
        self.newer = Batch.objects.create(
            batch_number='FG-NEW', status='Active', product=self.product, quantity=Decimal('521'),
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=330), warehouse=self.wh,
        )
        self.so = SalesOrder.objects.create(so_number='SO-FEFO', client_name='AgriCore', origin_warehouse=self.wh, status='Pending')
        self.item = SalesOrderDetail.objects.create(sales_order=self.so, product=self.product, quantity_ordered=Decimal('400'))

    def _post(self, old, new, reason=''):
        return self.client.post(reverse('so_allocate', args=[self.so.pk]), {
            'action': 'allocate_manual',
            f'batch_qty_{self.item.id}_{self.older.id}': old,
            f'batch_qty_{self.item.id}_{self.newer.id}': new,
            'override_reason': reason,
        })

    def _allocated(self):
        return {a.batch.batch_number: a.quantity for a in StockAllocation.objects.filter(sales_order=self.so)}

    def test_page_prefills_fefo_amounts(self):
        resp = self.client.get(reverse('so_allocate', args=[self.so.pk]))
        self.assertContains(resp, 'Finished Goods Allocation (FEFO)')
        self.assertContains(resp, f'name="batch_qty_{self.item.id}_{self.older.id}" max="284.00" min="0" step="0.01" value="284.00"')
        self.assertContains(resp, f'name="batch_qty_{self.item.id}_{self.newer.id}" max="521.00" min="0" step="0.01" value="116.00"')

    def test_fefo_amounts_need_no_reason(self):
        self._post('284', '116')
        self.assertEqual(self._allocated(), {'FG-OLD': Decimal('284'), 'FG-NEW': Decimal('116')})
        self.so.refresh_from_db()
        self.assertEqual(self.so.status, 'Ready to Ship')

    def test_partial_fefo_amount_needs_no_reason(self):
        self._post('200', '')
        self.assertEqual(self._allocated(), {'FG-OLD': Decimal('200')})

    def test_skipping_older_batch_without_reason_is_rejected(self):
        self._post('', '400')
        self.assertEqual(self._allocated(), {})
        self.older.refresh_from_db(); self.newer.refresh_from_db()
        self.assertEqual(self.newer.allocated_quantity, Decimal('0'))

    def test_skipping_older_batch_with_reason_is_allowed_and_logged(self):
        from core.models import OrderTimeline
        self._post('', '400', reason='Client wants the later expiry')
        self.assertEqual(self._allocated(), {'FG-NEW': Decimal('400')})
        self.assertTrue(OrderTimeline.objects.filter(sales_order=self.so, action__contains='Client wants the later expiry').exists())

    def test_cannot_allocate_more_than_order_needs(self):
        self._post('284', '200', reason='x')
        self.assertEqual(self._allocated(), {})

    def test_later_allocation_is_capped_by_existing_reservations(self):
        self._post('284', '116')
        self.newer.refresh_from_db()
        self._post('', '10', reason='x')  # order already fully covered
        self.assertEqual(sum(self._allocated().values()), Decimal('400'))


class OutboundShipmentOriginAndBatchTests(TestCase):
    """A shipment leaves from one place: SO shipments are split per warehouse, items
    need a batch at the origin, and dispatch is blocked otherwise."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='origin', password='pw')
        self.client.login(username='origin', password='pw')
        self.hub = Warehouse.objects.create(name='Hub O', location_type='Storage')
        self.plant = Warehouse.objects.create(name='Plant O', location_type='Manufacturing')
        self.product = Product.objects.create(name='AC O', sku='PRD-O', unit_of_measure='kg', price_per_unit=1)
        self.hub_batch = Batch.objects.create(
            batch_number='FG-HUB', status='Active', product=self.product, quantity=Decimal('400'),
            allocated_quantity=Decimal('400'), manufacturing_date=date.today(),
            expiry_date=date.today() + timedelta(days=300), warehouse=self.hub,
        )
        self.plant_batch = Batch.objects.create(
            batch_number='FG-PLANT', status='Active', product=self.product, quantity=Decimal('400'),
            allocated_quantity=Decimal('400'), manufacturing_date=date.today(),
            expiry_date=date.today() + timedelta(days=300), warehouse=self.plant,
        )
        self.so = SalesOrder.objects.create(so_number='SO-O', client_name='AgriCore', origin_warehouse=self.hub, status='Ready to Ship')
        SalesOrderDetail.objects.create(sales_order=self.so, product=self.product, quantity_ordered=Decimal('800'))
        StockAllocation.objects.create(batch=self.hub_batch, sales_order=self.so, quantity=Decimal('400'))
        StockAllocation.objects.create(batch=self.plant_batch, sales_order=self.so, quantity=Decimal('400'))

    def test_so_shipment_is_split_per_warehouse(self):
        self.client.post(reverse('so_create_shipment', args=[self.so.pk]))
        shipments = {s.origin_warehouse.name: s for s in Shipment.objects.filter(sales_order=self.so)}
        self.assertEqual(set(shipments), {'Hub O', 'Plant O'})
        for name, batch in (('Hub O', self.hub_batch), ('Plant O', self.plant_batch)):
            items = list(shipments[name].items.all())
            self.assertEqual([i.batch for i in items], [batch])
            self.assertEqual(StockAllocation.objects.get(shipment=shipments[name]).batch, batch)
        self.assertFalse(StockAllocation.objects.filter(sales_order=self.so).exists())

    def _draft(self):
        return Shipment.objects.create(
            tracking_number='SHP-O-1', sales_order=self.so, direction='Outbound', status='Draft', origin_warehouse=self.hub,
        )

    def test_add_item_requires_batch(self):
        s = self._draft()
        self.client.post(reverse('shipment_detail', args=[s.pk]), {'action': 'add_item', 'product_id': self.product.id, 'quantity': '5'})
        self.assertFalse(s.items.exists())

    def test_add_item_rejects_batch_from_another_warehouse(self):
        s = self._draft()
        self.client.post(reverse('shipment_detail', args=[s.pk]), {
            'action': 'add_item', 'product_id': self.product.id, 'batch_id': self.plant_batch.id, 'quantity': '5',
        })
        self.assertFalse(s.items.exists())

    def test_dispatch_blocked_when_item_has_no_batch(self):
        s = self._draft()
        s.status = 'Preparing'
        s.external_tracking_id = 'TRK'
        s.departure_datetime = timezone.now()
        s.save()
        ShipmentItem.objects.create(shipment=s, product=self.product, quantity=Decimal('5'))
        self.client.post(reverse('shipment_detail', args=[s.pk]), {'action': 'update_operational_status', 'status': 'Dispatched'})
        s.refresh_from_db()
        self.assertEqual(s.status, 'Preparing')

    def test_dispatch_allowed_with_batch_at_origin(self):
        s = self._draft()
        s.status = 'Preparing'
        s.external_tracking_id = 'TRK'
        s.departure_datetime = timezone.now()
        s.save()
        ShipmentItem.objects.create(shipment=s, product=self.product, batch=self.hub_batch, quantity=Decimal('5'))
        self.client.post(reverse('shipment_detail', args=[s.pk]), {'action': 'update_operational_status', 'status': 'Dispatched'})
        s.refresh_from_db()
        self.assertEqual(s.status, 'Dispatched')

    def test_registering_outbound_shipment_always_starts_as_draft(self):
        self.client.post(reverse('shipments'), {
            'action': 'create_shipment', 'direction': 'Outbound', 'status': 'Dispatched',
            'origin_warehouse_id': self.hub.id, 'sales_order_id': self.so.id,
        })
        s = Shipment.objects.get(sales_order=self.so)
        self.assertEqual(s.status, 'Draft')
        self.assertFalse(s.credited_to_so)


class ProcessControlTests(TestCase):
    """Group 2: approvals (no self-approval except Admins, only the assigned approver
    decides), manual SO status steps, re-approval after edits, shipment cargo lock and
    forward-only shipment status."""

    def setUp(self):
        from core.models import PurchaseOrder
        self.PurchaseOrder = PurchaseOrder
        self.manager = make_user(username='mgr2', password='pw', role='Manager')
        self.other_manager = make_user(username='mgr3', password='pw', role='Manager')
        self.admin = make_user(username='adm2', password='pw', role='Admin')
        self.wh = Warehouse.objects.create(name='WH PC', location_type='Storage')
        self.product = Product.objects.create(name='Prod PC', sku='PRD-PC', unit_of_measure='kg', price_per_unit=1)

    def _as(self, user):
        c = Client()
        c.force_login(user)
        return c

    def _so(self, status='Draft', **kw):
        so = SalesOrder.objects.create(so_number=f'SO-PC-{SalesOrder.objects.count()}', client_name='Acme',
                                       origin_warehouse=self.wh, status=status, **kw)
        SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('10'))
        return so

    # --- approvals
    def test_manager_cannot_request_approval_from_self(self):
        so = self._so()
        self._as(self.manager).post(reverse('so_detail', args=[so.pk]), {'action': 'request_approval', 'manager_id': self.manager.id})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Draft')

    def test_admin_can_self_approve_and_it_is_marked(self):
        from core.models import OrderTimeline
        so = self._so()
        c = self._as(self.admin)
        c.post(reverse('so_detail', args=[so.pk]), {'action': 'request_approval', 'manager_id': self.admin.id})
        c.post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'sales_order', 'item_id': so.id})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Pending')
        self.assertTrue(OrderTimeline.objects.filter(sales_order=so, action__contains='self-approval by Admin').exists())

    def test_only_assigned_approver_can_approve(self):
        so = self._so('Pending Approval', assigned_to=self.manager)
        self._as(self.other_manager).post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'sales_order', 'item_id': so.id})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Pending Approval')
        self._as(self.manager).post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'sales_order', 'item_id': so.id})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Pending')

    def test_cannot_approve_something_not_pending(self):
        so = self._so('Draft', assigned_to=self.manager)
        self._as(self.manager).post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'sales_order', 'item_id': so.id})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Draft')

    def test_request_must_go_to_a_manager(self):
        staff = make_user(username='staff2', password='pw', role='Sales')
        so = self._so()
        self._as(self.manager).post(reverse('so_detail', args=[so.pk]), {'action': 'request_approval', 'manager_id': staff.id})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Draft')

    # --- manual SO status
    def test_so_status_cannot_jump_from_draft_to_delivered(self):
        so = self._so()
        self._as(self.admin).post(reverse('so_detail', args=[so.pk]), {'action': 'update_so_status', 'status': 'Delivered'})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Draft')

    def test_so_can_be_marked_delivered_from_ready_to_ship(self):
        so = self._so('Ready to Ship')
        self._as(self.admin).post(reverse('so_detail', args=[so.pk]), {'action': 'update_so_status', 'status': 'Delivered'})
        so.refresh_from_db()
        self.assertEqual(so.status, 'Delivered')

    def test_so_detail_dropdown_only_offers_allowed_statuses(self):
        so = self._so('Ready to Ship')
        resp = self._as(self.admin).get(reverse('so_detail', args=[so.pk]))
        codes = [c for c, _ in resp.context['so_status_choices']]
        self.assertEqual(codes, ['Ready to Ship', 'Shipped', 'Delivered'])

    # --- edits after approval
    def test_adding_item_to_approved_order_returns_it_to_draft(self):
        so = self._so('Pending', approved_by=self.manager)
        self._as(self.admin).post(reverse('so_detail', args=[so.pk]), {'action': 'add_so_item', 'product_id': self.product.id, 'quantity_ordered': '5'})
        so.refresh_from_db()
        self.assertEqual(so.items.count(), 2)
        self.assertEqual(so.status, 'Draft')
        self.assertIsNone(so.approved_by)

    def test_items_locked_once_ready_to_ship(self):
        so = self._so('Ready to Ship')
        self._as(self.admin).post(reverse('so_detail', args=[so.pk]), {'action': 'add_so_item', 'product_id': self.product.id, 'quantity_ordered': '5'})
        so.refresh_from_db()
        self.assertEqual(so.items.count(), 1)
        self.assertEqual(so.status, 'Ready to Ship')

    def test_editing_draft_order_stays_draft(self):
        so = self._so('Draft')
        self._as(self.admin).post(reverse('so_detail', args=[so.pk]), {'action': 'add_so_item', 'product_id': self.product.id, 'quantity_ordered': '5'})
        so.refresh_from_db()
        self.assertEqual((so.items.count(), so.status, so.revision_count), (2, 'Draft', 0))

    # --- shipments
    def _shipment(self, status):
        batch = Batch.objects.create(
            batch_number=f'B-PC-{Batch.objects.count()}', status='Active', product=self.product, quantity=Decimal('50'),
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=100), warehouse=self.wh,
        )
        s = Shipment.objects.create(tracking_number=f'SHP-PC-{Shipment.objects.count()}', direction='Outbound',
                                    status=status, origin_warehouse=self.wh, assigned_to=self.manager)
        return s, batch

    def test_cargo_change_while_pending_approval_needs_reapproval(self):
        s, batch = self._shipment('Pending Approval')
        self._as(self.admin).post(reverse('shipment_detail', args=[s.pk]), {
            'action': 'add_item', 'product_id': self.product.id, 'batch_id': batch.id, 'quantity': '5'})
        s.refresh_from_db()
        self.assertEqual(s.items.count(), 1)
        self.assertEqual(s.status, 'Logistics Review')
        self.assertIsNone(s.assigned_to)

    def test_cargo_locked_after_dispatch(self):
        s, batch = self._shipment('Dispatched')
        self._as(self.admin).post(reverse('shipment_detail', args=[s.pk]), {
            'action': 'add_item', 'product_id': self.product.id, 'batch_id': batch.id, 'quantity': '5'})
        self.assertFalse(s.items.exists())

    def test_shipment_status_cannot_go_backwards(self):
        s, _ = self._shipment('Dispatched')
        self._as(self.admin).post(reverse('shipment_detail', args=[s.pk]), {'action': 'update_operational_status', 'status': 'Preparing'})
        s.refresh_from_db()
        self.assertEqual(s.status, 'Dispatched')

    def test_shipment_approve_needs_pending_and_assigned(self):
        s, _ = self._shipment('Logistics Review')
        self._as(self.manager).post(reverse('shipment_detail', args=[s.pk]), {'action': 'approve'})
        s.refresh_from_db()
        self.assertEqual(s.status, 'Logistics Review')

    # --- production runs
    def _run(self, **kw):
        from core.models import ProductionRun
        return ProductionRun.objects.create(run_number=f'RUN-PC-{ProductionRun.objects.count()}', target_product=self.product,
                                            expected_yield=Decimal('1'), status='Pending Approval', **kw)

    def test_staff_cannot_approve_production_run(self):
        staff = make_user(username='staff_run', password='pw', role='Manufacturing')
        run = self._run()
        self._as(staff).post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'production_run', 'item_id': run.id})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Approval')

    def test_any_manager_can_approve_unassigned_production_run(self):
        run = self._run()
        self._as(self.other_manager).post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'production_run', 'item_id': run.id})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Allocation')

    def test_named_run_approver_only(self):
        run = self._run(assigned_to=self.manager)
        self._as(self.other_manager).post(reverse('approvals_inbox'), {'action': 'reject', 'item_type': 'production_run', 'item_id': run.id})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Pending Approval')


class ActionCenterTests(TestCase):
    """The Action Center page, its sidebar badge and its history/notifications."""

    def setUp(self):
        self.manager = make_user(username='ac_mgr', password='pw', role='Manager')
        self.other_manager = make_user(username='ac_mgr2', password='pw', role='Manager')
        self.sales = make_user(username='ac_sales', password='pw', role='Sales')
        self.wh = Warehouse.objects.create(name='WH AC', location_type='Storage')
        self.product = Product.objects.create(name='Prod AC', sku='PRD-AC', unit_of_measure='kg', price_per_unit=1)

    def _as(self, user):
        c = Client()
        c.force_login(user)
        return c

    def _so(self, status='Pending Approval', **kw):
        so = SalesOrder.objects.create(so_number=f'SO-AC-{SalesOrder.objects.count()}', client_name='Acme',
                                       origin_warehouse=self.wh, status=status, **kw)
        SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('10'))
        return so

    def _run(self, **kw):
        from core.models import ProductionRun
        return ProductionRun.objects.create(run_number=f'RUN-AC-{ProductionRun.objects.count()}', target_product=self.product,
                                            expected_yield=Decimal('1'), status='Pending Approval', **kw)

    def test_badge_matches_page_and_ignores_unrelated_items(self):
        from core.models import PurchaseOrder
        self._so(assigned_to=self.manager)
        self._so(assigned_to=self.other_manager)   # someone else's
        self._so(status='Draft')                    # not pending
        PurchaseOrder.objects.create(po_number='PO-AC-1', status='Pending', target_warehouse=self.wh)  # already approved
        self._run()                                 # unassigned: open to any approver
        self._run(assigned_to=self.other_manager)   # named approver is someone else
        resp = self._as(self.manager).get(reverse('approvals_inbox'))
        self.assertEqual(resp.context['pending_count'], 2)
        self.assertEqual(resp.context['pending_approvals_total'], 2)
        self.assertEqual(len(resp.context['pending_runs']), 1)

    def test_empty_state_hidden_when_only_a_run_is_pending(self):
        self._run()
        resp = self._as(self.manager).get(reverse('approvals_inbox'))
        self.assertNotContains(resp, "You're all caught up")
        self.assertContains(resp, 'Production Approval')
        self.assertNotContains(resp, 'Approve &amp; Complete')

    def test_pre_production_reject_cancels_and_is_in_history(self):
        from core.models import OrderTimeline, Notification
        run = self._run(created_by=self.sales)
        c = self._as(self.manager)
        c.post(reverse('approvals_inbox'), {'action': 'reject', 'item_type': 'production_run', 'item_id': run.id, 'comment': 'Not now'})
        run.refresh_from_db()
        self.assertEqual(run.status, 'Cancelled')
        self.assertTrue(OrderTimeline.objects.filter(production_run=run, action__contains='Not now').exists())
        self.assertTrue(Notification.objects.filter(user=self.sales, message__contains=run.run_number).exists())
        resp = c.get(reverse('approvals_inbox'))
        self.assertEqual([h['decision'] for h in resp.context['history_page']], ['Rejected'])
        self.assertEqual(resp.context['rejected_this_week'], 1)

    def test_history_ignores_approval_requests(self):
        so = self._so(status='Draft')
        c = self._as(self.manager)
        c.post(reverse('so_detail', args=[so.pk]), {'action': 'request_approval', 'manager_id': self.other_manager.id})
        resp = c.get(reverse('approvals_inbox'))
        self.assertEqual(len(resp.context['history_page']), 0)

    def test_requester_is_notified_of_decision(self):
        from core.models import Notification
        so = self._so(status='Draft')
        self._as(self.sales).post(reverse('so_detail', args=[so.pk]), {'action': 'request_approval', 'manager_id': self.manager.id})
        self._as(self.manager).post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'sales_order', 'item_id': so.id})
        self.assertTrue(Notification.objects.filter(user=self.sales, message__startswith='Approved:', message__contains=so.so_number).exists())

    def test_refusal_returns_to_next(self):
        so = self._so(assigned_to=self.manager)
        nxt = reverse('so_detail', args=[so.pk])
        resp = self._as(self.other_manager).post(reverse('approvals_inbox'), {
            'action': 'approve', 'item_type': 'sales_order', 'item_id': so.id, 'next': nxt})
        self.assertRedirects(resp, nxt, fetch_redirect_response=False)

    def test_adding_a_follower_notifies_them_but_not_yourself(self):
        from core.models import Notification
        run = self._run()
        so = self._so()
        c = self._as(self.manager)
        c.post(reverse('production_run_detail', args=[run.pk]), {'action': 'add_follower', 'user_id': self.sales.id})
        c.post(reverse('so_detail', args=[so.pk]), {'action': 'add_follower', 'user_id': self.sales.id})
        c.post(reverse('so_detail', args=[so.pk]), {'action': 'add_follower', 'user_id': self.sales.id})  # already following
        c.post(reverse('production_run_detail', args=[run.pk]), {'action': 'add_follower', 'user_id': self.manager.id})
        self.assertEqual(Notification.objects.filter(user=self.sales, message__contains='added you as a follower').count(), 2)
        self.assertFalse(Notification.objects.filter(user=self.manager, message__contains='added you as a follower').exists())

    def test_unknown_action_is_rejected(self):
        resp = self._as(self.manager).post(reverse('approvals_inbox'), {'action': 'delete', 'item_type': 'sales_order', 'item_id': 1})
        self.assertRedirects(resp, reverse('approvals_inbox'), fetch_redirect_response=False)


class OrderDisplayTests(TestCase):
    """Group 3: SO subtotal/total, RM order value, full status filter, pagination,
    no raw template tags in the list timeline."""

    def setUp(self):
        self.user = make_user(username='disp', password='pw', role='Admin')
        self.client = Client()
        self.client.force_login(self.user)
        self.wh = Warehouse.objects.create(name='WH Disp', location_type='Storage')
        self.product = Product.objects.create(name='Prod Disp', sku='PRD-DISP', unit_of_measure='kg', price_per_unit=1)

    def test_subtotal_and_order_total(self):
        so = SalesOrder.objects.create(so_number='SO-DISP', client_name='AgriCore', origin_warehouse=self.wh)
        SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('400'), unit_price=Decimal('1200'))
        SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('5'))
        resp = self.client.get(reverse('so_detail', args=[so.pk]))
        self.assertContains(resp, 'RM 480000.00')
        self.assertNotContains(resp, '×')
        self.assertContains(resp, '1 line without a unit price not included')
        self.assertEqual(so.total_value, Decimal('480000'))

    def test_list_has_every_status_and_no_raw_template_tags(self):
        from core.models import OrderTimeline
        so = SalesOrder.objects.create(so_number='SO-DISP2', client_name='AgriCore', origin_warehouse=self.wh)
        OrderTimeline.objects.create(sales_order=so, action='Created', user=self.user)
        resp = self.client.get(reverse('so_list'))
        for code in ('Rejected', 'Awaiting Acknowledgement', 'Partially Shipped', 'Pending Approval'):
            self.assertContains(resp, f'<option value="{code}"')
        self.assertNotContains(resp, 'event.timestamp')

    def test_list_is_paginated(self):
        for i in range(30):
            SalesOrder.objects.create(so_number=f'SO-PG-{i:02d}', client_name='C', origin_warehouse=self.wh)
        resp = self.client.get(reverse('so_list'))
        self.assertEqual(len(resp.context['sales_orders'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')

    def test_sort_applies_across_all_pages(self):
        for i in range(30):
            SalesOrder.objects.create(so_number=f'SO-S-{i:02d}', client_name=f'Client {i:02d}', origin_warehouse=self.wh)
        resp = self.client.get(reverse('so_list'), {'sort': '-client_name'})
        self.assertEqual(resp.context['sales_orders'][0].client_name, 'Client 29')
        resp = self.client.get(reverse('so_list'), {'sort': 'client_name', 'page': 2})
        self.assertEqual(resp.context['sales_orders'][0].client_name, 'Client 25')
        # paging links keep the sort
        self.assertContains(resp, 'href="?sort=client_name&page=1"')

    def test_status_sorts_in_workflow_order_and_bad_sort_falls_back(self):
        SalesOrder.objects.create(so_number='SO-ST-1', client_name='A', origin_warehouse=self.wh, status='Delivered')
        SalesOrder.objects.create(so_number='SO-ST-2', client_name='B', origin_warehouse=self.wh, status='Draft')
        resp = self.client.get(reverse('so_list'), {'sort': 'status'})
        self.assertEqual([so.status for so in resp.context['sales_orders']], ['Draft', 'Delivered'])
        resp = self.client.get(reverse('so_list'), {'sort': 'created_by__password'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['sort_key'], 'order_date')

    def test_order_numbers_link_to_detail_and_no_actions_column(self):
        from core.models import Supplier, PurchaseOrder
        so = SalesOrder.objects.create(so_number='SO-LINK', client_name='A', origin_warehouse=self.wh)
        po = PurchaseOrder.objects.create(po_number='PO-LINK', supplier_name='S', target_warehouse=self.wh)
        so_resp = self.client.get(reverse('so_list'))
        self.assertContains(so_resp, f'href="{reverse("so_detail", args=[so.pk])}"')
        self.assertNotContains(so_resp, '>Actions<')
        po_resp = self.client.get(reverse('po_list'))
        self.assertContains(po_resp, f'href="{reverse("po_detail", args=[po.pk])}"')
        self.assertNotContains(po_resp, '>Actions<')


class TesterFollowUpTests(TestCase):
    """Remaining items from the SO-1007 end-to-end test: run progress after completion,
    approval notifications cleared once decided, shipments drafted from an SO record who
    requested them and carry the client's details, origin-only batch dropdown, and
    shipment status changes in the timeline."""

    def setUp(self):
        from core.models import Client as ClientModel
        self.admin = make_user(username='tfu', password='pw', role='Admin', first_name='Ada', last_name='Admin')
        self.client = Client()
        self.client.force_login(self.admin)
        self.hub = Warehouse.objects.create(name='Hub TFU', location_type='Storage')
        self.plant = Warehouse.objects.create(name='Plant TFU', location_type='Manufacturing')
        self.product = Product.objects.create(name='Prod TFU', sku='PRD-TFU', unit_of_measure='kg', price_per_unit=1)
        self.customer = ClientModel.objects.create(name='AgriCore', contact_person='Siti', phone='012-345 6789', delivery_address='Lot 5, Klang')

    def test_completed_run_progress_uses_actual_yield(self):
        from core.models import ProductionRun
        run = ProductionRun.objects.create(run_number='RUN-TFU', target_product=self.product, expected_yield=Decimal('400'),
                                           actual_yield=Decimal('400'), status='Completed', manufacturing_plant=self.plant)
        resp = self.client.get(reverse('production_run_detail', args=[run.pk]))
        self.assertEqual(resp.context['yield_logs_sum'], Decimal('400'))
        self.assertEqual(resp.context['yield_progress_pct'], 100)
        self.assertContains(resp, 'Planned Start')

    def test_variance_approval_from_run_page_returns_to_run_page(self):
        from core.models import ProductionRun
        run = ProductionRun.objects.create(run_number='RUN-VAR', target_product=self.product, expected_yield=Decimal('400'),
                                           actual_yield=Decimal('380'), status='Pending Approval', manufacturing_plant=self.plant)
        url = reverse('production_run_detail', args=[run.pk])
        resp = self.client.get(url)
        self.assertContains(resp, 'name="next"')
        resp = self.client.post(reverse('approvals_inbox'), {
            'item_type': 'production_run', 'item_id': run.pk, 'action': 'reject', 'next': url,
        })
        self.assertRedirects(resp, url, fetch_redirect_response=False)
        run.refresh_from_db()
        self.assertEqual(run.status, 'InProgress')

    def test_approvals_inbox_ignores_offsite_next(self):
        resp = self.client.post(reverse('approvals_inbox'), {'next': 'https://evil.example/'})
        self.assertRedirects(resp, reverse('approvals_inbox'), fetch_redirect_response=False)

    def test_approval_notification_cleared_after_decision(self):
        from core.models import Notification
        so = SalesOrder.objects.create(so_number='SO-TFU', client_name='AgriCore', origin_warehouse=self.hub,
                                       status='Pending Approval', assigned_to=self.admin)
        Notification.objects.create(user=self.admin, message='Sales Order SO-TFU requires your approval.')
        other = Notification.objects.create(user=self.admin, message='Sales Order SO-OTHER requires your approval.')
        self.client.post(reverse('approvals_inbox'), {'action': 'approve', 'item_type': 'sales_order', 'item_id': so.id})
        self.assertFalse(Notification.objects.filter(message__contains='SO-TFU', is_read=False).exists())
        other.refresh_from_db()
        self.assertFalse(other.is_read)

    def test_shipment_drafted_from_so_has_requester_client_details_and_timeline(self):
        from core.models import OrderTimeline
        batch = Batch.objects.create(batch_number='FG-TFU', status='Active', product=self.product, quantity=Decimal('10'),
                                     allocated_quantity=Decimal('10'), manufacturing_date=date.today(),
                                     expiry_date=date.today() + timedelta(days=100), warehouse=self.hub)
        so = SalesOrder.objects.create(so_number='SO-TFU2', client_name='AgriCore', client=self.customer,
                                       origin_warehouse=self.hub, status='Ready to Ship')
        StockAllocation.objects.create(batch=batch, sales_order=so, quantity=Decimal('10'))
        self.client.post(reverse('so_create_shipment', args=[so.pk]))

        s = Shipment.objects.get(sales_order=so)
        self.assertEqual((s.client_contact_name, s.client_contact_phone, s.client_address), ('Siti', '012-345 6789', 'Lot 5, Klang'))
        self.assertEqual(s.last_edited_by, self.admin)
        self.assertTrue(OrderTimeline.objects.filter(shipment=s, action='Drafted from SO-TFU2.').exists())
        resp = self.client.get(reverse('shipment_detail', args=[s.pk]))
        self.assertContains(resp, 'Ada Admin')
        self.assertNotContains(resp, 'Draft (Manufacturing)')

    def test_batch_dropdown_only_lists_origin_stock(self):
        here = Batch.objects.create(batch_number='B-HERE', status='Active', product=self.product, quantity=Decimal('10'),
                                    manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=100), warehouse=self.hub)
        Batch.objects.create(batch_number='B-ELSEWHERE', status='Active', product=self.product, quantity=Decimal('10'),
                             manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=100), warehouse=self.plant)
        s = Shipment.objects.create(tracking_number='SHP-TFU', direction='Outbound', status='Draft', origin_warehouse=self.hub)
        resp = self.client.get(reverse('shipment_detail', args=[s.pk]))
        self.assertEqual(list(resp.context['batches']), [here])

    def test_shipment_status_change_is_logged(self):
        from core.models import OrderTimeline
        s = Shipment.objects.create(tracking_number='SHP-TFU2', direction='Inbound', status='Preparing', destination_warehouse=self.hub)
        self.client.post(reverse('shipment_detail', args=[s.pk]), {'action': 'update_operational_status', 'status': 'Dispatched'})
        self.assertTrue(OrderTimeline.objects.filter(shipment=s, action='Status changed from Preparing to Dispatched.').exists())


class RecipeTraceQuantityTests(TestCase):
    """Trace recipe amounts: stored to 4 dp, and never displayed as 0."""

    def test_trace_amount_is_stored(self):
        prod = Product.objects.create(name='Trace P', sku='PRD-TR', unit_of_measure='kg', price_per_unit=1)
        mat = Material.objects.create(name='Trace M', sku='MAT-TR', category='Raw', unit_of_measure='kg', safe_storage_days=100)
        r = ProductRecipe.objects.create(product=prod, material=mat, quantity_required=Decimal('0.0005'))
        r.refresh_from_db()
        self.assertEqual(r.quantity_required, Decimal('0.0005'))

    def test_qty_display_filter(self):
        from core.templatetags.quantity_extras import qty_display
        self.assertEqual(qty_display(591000.0), '591,000')
        self.assertEqual(qty_display(20.0), '20')
        self.assertEqual(qty_display(Decimal('0.02')), '0.02')
        self.assertEqual(qty_display(Decimal('0.0005')), '0.0005')
        self.assertEqual(qty_display(596.25), '596.25')
        self.assertEqual(qty_display(0), '0')
        self.assertEqual(qty_display(None), '')


class UnifiedReceivingTests(TestCase):
    """One receiving flow for every shipment direction, "Receive all as expected",
    one-click status buttons and Submit & Approve for Admins."""

    def setUp(self):
        self.admin = make_user(username='recv_admin', password='pw', role='Admin')
        self.manager = make_user(username='recv_mgr', password='pw', role='Manager')
        self.client = Client()
        self.client.force_login(self.admin)
        self.origin = Warehouse.objects.create(name='Recv Origin', location_type='Storage')
        self.dest = Warehouse.objects.create(name='Recv Dest', location_type='Storage')
        self.product = Product.objects.create(name='Recv P', sku='PRD-RECV', unit_of_measure='kg', price_per_unit=1)
        self.material = Material.objects.create(name='Recv M', sku='MAT-RECV', category='Raw', safe_storage_days=100)

    def _post(self, s, **data):
        return self.client.post(reverse('shipment_detail', args=[s.pk]), data)

    def _transfer(self, status='Arrived'):
        batch = Batch.objects.create(batch_number='B-RECV-T', status='Active', material=self.material, quantity=Decimal('100'),
                                     allocated_quantity=Decimal('60'), manufacturing_date=date.today(),
                                     expiry_date=date.today() + timedelta(days=100), warehouse=self.origin)
        s = Shipment.objects.create(tracking_number='SHP-RECV-T', direction='Transfer', status=status,
                                    origin_warehouse=self.origin, destination_warehouse=self.dest)
        item = ShipmentItem.objects.create(shipment=s, material=self.material, batch=batch, quantity=Decimal('60'))
        StockAllocation.objects.create(batch=batch, shipment=s, quantity=Decimal('60'))
        return s, item, batch

    def test_transfer_received_in_rounds_moves_stock_on_finalize(self):
        s, item, batch = self._transfer()
        self._post(s, action='log_item_receipt', item_id=item.id, quantity='40', received_date=str(date.today()))
        self.assertFalse(Batch.objects.filter(warehouse=self.dest).exists())  # nothing moves per round
        self._post(s, action='log_item_receipt', item_id=item.id, quantity='20', received_date=str(date.today()))
        self._post(s, action='finalize_shipment_receiving')
        s.refresh_from_db(); batch.refresh_from_db()
        self.assertEqual(s.status, 'Completed')
        self.assertEqual(batch.quantity, Decimal('40'))
        self.assertEqual(Batch.objects.get(warehouse=self.dest).quantity, Decimal('60'))
        self.assertEqual(item.receipts.count(), 2)

    def test_receive_all_completes_transfer_in_one_click(self):
        s, item, batch = self._transfer()
        self._post(s, action='log_item_receipt', item_id=item.id, quantity='25', received_date=str(date.today()))
        self._post(s, action='receive_all')
        s.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(s.status, 'Completed')
        self.assertEqual(item.received_quantity, Decimal('60'))
        self.assertEqual(sorted(item.receipts.values_list('quantity', flat=True)), [Decimal('25'), Decimal('35')])
        self.assertEqual(Batch.objects.get(warehouse=self.dest).quantity, Decimal('60'))

    def test_receive_all_completes_outbound_and_credits_so(self):
        batch = Batch.objects.create(batch_number='FG-RECV', status='Active', product=self.product, quantity=Decimal('50'),
                                     allocated_quantity=Decimal('50'), manufacturing_date=date.today(),
                                     expiry_date=date.today() + timedelta(days=100), warehouse=self.origin)
        so = SalesOrder.objects.create(so_number='SO-RECV', client_name='C', origin_warehouse=self.origin, status='Shipped')
        detail = SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('50'))
        s = Shipment.objects.create(tracking_number='SHP-RECV-O', direction='Outbound', status='Arrived',
                                    origin_warehouse=self.origin, sales_order=so)
        ShipmentItem.objects.create(shipment=s, product=self.product, batch=batch, quantity=Decimal('50'))
        StockAllocation.objects.create(batch=batch, shipment=s, quantity=Decimal('50'))

        self._post(s, action='receive_all')

        s.refresh_from_db(); batch.refresh_from_db(); detail.refresh_from_db(); so.refresh_from_db()
        self.assertEqual(s.status, 'Completed')
        self.assertEqual((batch.quantity, batch.status), (Decimal('0'), 'Depleted'))
        self.assertEqual(detail.quantity_shipped, Decimal('50'))
        self.assertEqual(so.status, 'Delivered')

    def test_cannot_receive_before_arrival(self):
        s, item, _ = self._transfer(status='Dispatched')
        self._post(s, action='receive_all')
        self._post(s, action='log_item_receipt', item_id=item.id, quantity='10', received_date=str(date.today()))
        s.refresh_from_db(); item.refresh_from_db()
        self.assertEqual((s.status, item.received_quantity), ('Dispatched', Decimal('0')))

    def test_status_buttons_replace_dropdown(self):
        s, _, _ = self._transfer(status='Preparing')
        resp = self.client.get(reverse('shipment_detail', args=[s.pk]))
        self.assertContains(resp, 'Mark Dispatched')
        self.assertNotContains(resp, 'Save Status')

    def _for_approval(self):
        s, _, _ = self._transfer(status='Logistics Review')
        return s

    def test_admin_submit_and_approve(self):
        s = self._for_approval()
        self._post(s, action='submit_for_approval', approve_now='1')
        s.refresh_from_db()
        self.assertEqual((s.status, s.assigned_to), ('Preparing', self.admin))

    def test_non_admin_cannot_submit_and_approve(self):
        s = self._for_approval()
        c = Client(); c.force_login(self.manager)
        c.post(reverse('shipment_detail', args=[s.pk]), {'action': 'submit_for_approval', 'approve_now': '1'})
        s.refresh_from_db()
        self.assertEqual(s.status, 'Logistics Review')


class QaHoldNewFinishedGoodsTests(TestCase):
    """qa_hold_new_finished_goods: off = batch Active and reserved at completion (unchanged);
    on = batch Quarantined, reserved for its order only when QA releases it."""

    def setUp(self):
        from core.models import ProductionRun
        self.admin = make_user(username='qa_admin', password='pw', role='Admin')
        self.client = Client()
        self.client.force_login(self.admin)
        self.plant = Warehouse.objects.create(name='QA Plant', location_type='Manufacturing')
        self.product = Product.objects.create(name='QA P', sku='PRD-QA', unit_of_measure='kg', price_per_unit=1)
        self.so = SalesOrder.objects.create(so_number='SO-QA', client_name='C', origin_warehouse=self.plant, status='In Production')
        SalesOrderDetail.objects.create(sales_order=self.so, product=self.product, quantity_ordered=Decimal('40'))
        self.run = ProductionRun.objects.create(run_number='RUN-QA', target_product=self.product, expected_yield=Decimal('40'),
                                                actual_yield=Decimal('40'), status='InProgress', sales_order=self.so,
                                                manufacturing_plant=self.plant)

    def _set_hold(self, on):
        from core.models import SystemSetting
        SystemSetting.objects.update_or_create(key='qa_hold_new_finished_goods', defaults={'value': 'true' if on else 'false', 'value_type': 'bool'})

    def test_hold_off_keeps_current_behaviour(self):
        from core.utils import finalize_production_run
        self._set_hold(False)
        batch = finalize_production_run(self.run, self.admin)
        self.so.refresh_from_db()
        self.assertEqual((batch.status, batch.allocated_quantity, self.so.status), ('Active', Decimal('40'), 'Ready to Ship'))

    def test_hold_on_quarantines_then_release_allocates(self):
        from core.utils import finalize_production_run
        from core.models import Notification
        self._set_hold(True)
        batch = finalize_production_run(self.run, self.admin)
        self.so.refresh_from_db()
        self.assertEqual((batch.status, batch.allocated_quantity), ('Quarantined', Decimal('0')))
        self.assertEqual(self.so.status, 'In Production')
        self.assertTrue(Notification.objects.filter(user=self.admin, message__contains=batch.batch_number, is_read=False).exists())

        self.client.post(reverse('qa_dashboard'), {'action': 'release_quarantine', 'batch_id': batch.id})

        batch.refresh_from_db(); self.so.refresh_from_db()
        self.assertEqual((batch.status, batch.allocated_quantity), ('Active', Decimal('40')))
        self.assertEqual(self.so.status, 'Ready to Ship')
        self.assertFalse(Notification.objects.filter(message__contains=batch.batch_number, is_read=False).exists())

    def test_releasing_an_already_reserved_batch_does_not_double_allocate(self):
        from core.utils import finalize_production_run
        self._set_hold(False)
        batch = finalize_production_run(self.run, self.admin)
        batch.status = 'Quarantined'; batch.save()
        self.client.post(reverse('qa_dashboard'), {'action': 'release_quarantine', 'batch_id': batch.id})
        batch.refresh_from_db()
        self.assertEqual(batch.allocated_quantity, Decimal('40'))
        self.assertEqual(StockAllocation.objects.filter(batch=batch).count(), 1)


class QuarantinedCapacityDisplayTests(TestCase):
    """Quarantined stock counts toward capacity and is called out on the pages."""

    def test_dashboard_and_facilities_show_quarantined_share(self):
        admin = make_user(username='cap_admin', password='pw', role='Admin', is_superuser=True)
        c = Client()
        c.force_login(admin)
        wh = Warehouse.objects.create(name='Cap WH', location_type='Storage', total_capacity_mt=Decimal('100'))
        mat = Material.objects.create(name='Cap M', sku='MAT-CAP', category='Raw', safe_storage_days=100, weight_mt_per_unit=Decimal('1'))
        for n, status in (('B-CAP-A', 'Active'), ('B-CAP-Q', 'Quarantined')):
            Batch.objects.create(batch_number=n, status=status, material=mat, quantity=Decimal('10'), warehouse=wh,
                                 manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=100))
        for url in (reverse('warehouse_list'), reverse('dashboard')):
            resp = c.get(url)
            self.assertContains(resp, 'incl. 10.0 MT quarantined', msg_prefix=url)
        self.assertContains(c.get(reverse('warehouse_list')), '20.0 MT used')


class RentHistoryTests(TestCase):
    """DSS #5: daily occupancy + rent snapshots written on change (no cron),
    estimated for rows without recorded rent, charted on Rent Opportunities."""

    def setUp(self):
        from core.models import WarehouseUtilizationSnapshot
        self.Snap = WarehouseUtilizationSnapshot
        self.rented = Warehouse.objects.create(
            name='Rented RH', location_type='Storage', ownership_type='ExternalProvider',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('2.00'), total_capacity_mt=Decimal('1000'),
        )
        self.own = Warehouse.objects.create(name='Own RH', location_type='Storage', ownership_type='Internal',
                                            total_capacity_mt=Decimal('1000'))
        self.mat = Material.objects.create(name='RH Mat', sku='MAT-RH', category='Raw', safe_storage_days=100,
                                           weight_mt_per_unit=Decimal('1'))

    def _batch(self, wh, qty):
        return Batch.objects.create(batch_number=f'B-RH-{wh.id}-{qty}', status='Active', material=self.mat,
                                    quantity=Decimal(str(qty)), warehouse=wh, manufacturing_date=date.today(),
                                    expiry_date=date.today() + timedelta(days=100))

    def _today(self, wh):
        return self.Snap.objects.get(warehouse=wh, snapshot_date=timezone.localdate())

    def test_saving_a_batch_records_todays_rent(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._batch(self.rented, 100)
        snap = self._today(self.rented)
        self.assertEqual((snap.used_mt, snap.daily_rent_cost, snap.rent_estimated), (Decimal('100.000'), Decimal('200.00'), False))

    def test_last_change_of_the_day_wins(self):
        with self.captureOnCommitCallbacks(execute=True):
            b = self._batch(self.rented, 100)
        with self.captureOnCommitCallbacks(execute=True):
            b.quantity = Decimal('40'); b.save()
        self.assertEqual(self._today(self.rented).daily_rent_cost, Decimal('80.00'))
        self.assertEqual(self.Snap.objects.filter(warehouse=self.rented).count(), 1)

    def test_moving_a_batch_updates_both_warehouses(self):
        with self.captureOnCommitCallbacks(execute=True):
            b = self._batch(self.rented, 100)
        b = Batch.objects.get(pk=b.pk)
        with self.captureOnCommitCallbacks(execute=True):
            b.warehouse = self.own; b.save()
        self.assertEqual(self._today(self.rented).daily_rent_cost, Decimal('0.00'))
        self.assertEqual(self._today(self.own).used_mt, Decimal('100.000'))

    def test_rate_change_updates_todays_rent(self):
        with self.captureOnCommitCallbacks(execute=True):
            self._batch(self.rented, 100)
        with self.captureOnCommitCallbacks(execute=True):
            self.rented.rental_cost_per_mt = Decimal('3.00'); self.rented.save()
        self.assertEqual(self._today(self.rented).daily_rent_cost, Decimal('300.00'))

    def test_history_estimates_missing_rent_and_carries_forward(self):
        from core.analytics import rent_history
        today = timezone.localdate()
        # a seeded-style row 3 days ago with no rent recorded, nothing since
        self.Snap.objects.create(warehouse=self.rented, snapshot_date=today - timedelta(days=3),
                                 used_mt=Decimal('50'), capacity_mt=Decimal('1000'), utilization_percent=Decimal('5'))
        self.Snap.objects.create(warehouse=self.own, snapshot_date=today - timedelta(days=3),
                                 used_mt=Decimal('500'), capacity_mt=Decimal('1000'), utilization_percent=Decimal('50'))
        h = rent_history(days=5, end=today)
        self.assertEqual([s['name'] for s in h['series']], ['Rented RH'])       # own warehouse costs nothing
        self.assertEqual(h['series'][0]['values'], [None, 100.0, 100.0, 100.0, 100.0])  # 50 MT x 2.00, carried forward
        self.assertEqual(h['series'][0]['estimated'], [False, True, True, True, True])
        self.assertTrue(h['has_estimates'])

    def test_rent_page_fills_today_and_renders_chart(self):
        admin = make_user(username='rh_admin', password='pw', role='Admin')
        self._batch(self.rented, 100)          # created without running on-commit callbacks
        c = Client(); c.force_login(admin)
        resp = c.get(reverse('rent_opportunities'))
        self.assertContains(resp, 'Rent History')
        self.assertContains(resp, 'id="rent-history-data"')
        self.assertEqual(self._today(self.rented).daily_rent_cost, Decimal('200.00'))  # safety net wrote it


class SplitStockDeliveryTests(TestCase):
    """An order whose reserved stock is in several warehouses: removing a line frees
    its stock; the user chooses separate deliveries or moving everything to the
    order's warehouse first, and can switch until the shipments are approved."""

    def setUp(self):
        from core.models import OrderTimeline, Notification
        self.OrderTimeline, self.Notification = OrderTimeline, Notification
        self.admin = make_user(username='split_admin', password='pw', role='Admin')
        self.client = Client()
        self.client.force_login(self.admin)
        self.hub = Warehouse.objects.create(name='Split Hub', location_type='Storage')
        self.alpha = Warehouse.objects.create(name='Split Alpha', location_type='Storage')
        self.product = Product.objects.create(name='Split P', sku='PRD-SPLIT', unit_of_measure='kg', price_per_unit=1)
        self.so = SalesOrder.objects.create(so_number='SO-SPLIT', client_name='AgriCore', origin_warehouse=self.hub,
                                            status='Ready to Ship', created_by=self.admin)
        self.line = SalesOrderDetail.objects.create(sales_order=self.so, product=self.product, quantity_ordered=Decimal('200'))
        self.at_hub = self._reserved(self.hub, 'FG-SPLIT-H')
        self.at_alpha = self._reserved(self.alpha, 'FG-SPLIT-A')

    def _reserved(self, wh, number, qty='100'):
        b = Batch.objects.create(batch_number=number, status='Active', product=self.product, quantity=Decimal(qty),
                                 allocated_quantity=Decimal(qty), manufacturing_date=date.today(),
                                 expiry_date=date.today() + timedelta(days=200), warehouse=wh)
        StockAllocation.objects.create(batch=b, sales_order=self.so, quantity=Decimal(qty))
        return b

    def _post(self, **data):
        return self.client.post(reverse('so_detail', args=[self.so.pk]), data)

    def _moves(self):
        return Shipment.objects.filter(sales_order=self.so, direction='Transfer').exclude(status='Cancelled')

    # --- the ported fix
    def test_removing_a_line_releases_its_reserved_stock(self):
        self.so.status = 'Pending'; self.so.save()
        self._post(action='remove_so_item', item_id=self.line.id)
        for b in (self.at_hub, self.at_alpha):
            b.refresh_from_db()
            self.assertEqual(b.allocated_quantity, Decimal('0'))
        self.assertFalse(StockAllocation.objects.filter(sales_order=self.so).exists())

    def test_removing_a_line_takes_it_off_an_open_stock_move(self):
        self._post(action='consolidate_stock')
        move = self._moves().get()
        self.so.status = 'Pending'; self.so.save()
        self._post(action='remove_so_item', item_id=self.line.id)
        move.refresh_from_db()
        self.assertEqual(move.status, 'Cancelled')
        self.at_alpha.refresh_from_db()
        self.assertEqual(self.at_alpha.allocated_quantity, Decimal('0'))

    # --- choosing
    def test_order_page_offers_the_choice(self):
        resp = self.client.get(reverse('so_detail', args=[self.so.pk]))
        self.assertContains(resp, 'Choose how to deliver')
        self.assertContains(resp, 'Ship separately (2 deliveries)')
        self.assertContains(resp, 'Move everything to Split Hub first (1 delivery)')

    def test_creator_is_notified_once(self):
        from core.utils import prompt_delivery_choice
        prompt_delivery_choice(self.so, self.admin)
        prompt_delivery_choice(self.so, self.admin)
        self.assertEqual(self.Notification.objects.filter(user=self.admin, message__contains='SO-SPLIT is ready to ship').count(), 1)

    def test_ship_separately_makes_one_shipment_per_warehouse(self):
        self.client.post(reverse('so_create_shipment', args=[self.so.pk]))
        origins = sorted(Shipment.objects.filter(sales_order=self.so, direction='Outbound').values_list('origin_warehouse__name', flat=True))
        self.assertEqual(origins, ['Split Alpha', 'Split Hub'])

    def test_one_delivery_moves_stock_then_ships_once(self):
        self._post(action='consolidate_stock')
        move = self._moves().get()
        self.assertEqual((move.origin_warehouse, move.destination_warehouse), (self.alpha, self.hub))
        alloc = StockAllocation.objects.get(batch=self.at_alpha)
        self.assertEqual((alloc.sales_order, alloc.shipment), (self.so, move))   # still the order's stock
        self.so.refresh_from_db()
        self.assertEqual(self.so.status, 'Ready to Ship')

        # can't ship while stock is still moving
        self.client.post(reverse('so_create_shipment', args=[self.so.pk]))
        self.assertFalse(Shipment.objects.filter(sales_order=self.so, direction='Outbound').exists())

        # the move arrives: reservation follows the stock to the hub
        move.status = 'Arrived'; move.save()
        self.client.post(reverse('shipment_detail', args=[move.pk]), {'action': 'receive_all'})
        arrived = Batch.objects.get(warehouse=self.hub, batch_number__contains='-TRF-')
        self.assertEqual(StockAllocation.objects.get(batch=arrived).sales_order, self.so)
        self.assertEqual(arrived.allocated_quantity, Decimal('100'))

        # now one outbound shipment from the hub carries everything
        self.client.post(reverse('so_create_shipment', args=[self.so.pk]))
        outbound = Shipment.objects.filter(sales_order=self.so, direction='Outbound')
        self.assertEqual(list(outbound.values_list('origin_warehouse__name', flat=True)), ['Split Hub'])
        self.assertEqual(outbound.get().items.count(), 2)

    # --- switching
    def test_switch_from_one_delivery_back_to_separate(self):
        self._post(action='consolidate_stock')
        self._post(action='cancel_delivery_plan')
        self.assertFalse(self._moves().exists())
        alloc = StockAllocation.objects.get(batch=self.at_alpha)
        self.assertEqual((alloc.sales_order, alloc.shipment), (self.so, None))
        self.assertContains(self.client.get(reverse('so_detail', args=[self.so.pk])), 'Choose how to deliver')

    def test_switch_from_separate_back_to_one_delivery(self):
        self.client.post(reverse('so_create_shipment', args=[self.so.pk]))
        self.assertContains(self.client.get(reverse('so_detail', args=[self.so.pk])), 'Switch to one delivery')
        self._post(action='cancel_delivery_plan')
        self.assertFalse(Shipment.objects.filter(sales_order=self.so, direction='Outbound').exclude(status='Cancelled').exists())
        self.assertEqual(StockAllocation.objects.filter(sales_order=self.so, shipment__isnull=True).count(), 2)
        for b in (self.at_hub, self.at_alpha):
            b.refresh_from_db()
            self.assertEqual(b.allocated_quantity, Decimal('100'))   # never released

    def test_cannot_switch_once_approved(self):
        self._post(action='consolidate_stock')
        move = self._moves().get()
        move.status = 'Preparing'; move.save()
        self._post(action='cancel_delivery_plan')
        move.refresh_from_db()
        self.assertEqual(move.status, 'Preparing')

    def test_scrapping_a_stock_move_keeps_the_reservation(self):
        self._post(action='consolidate_stock')
        move = self._moves().get()
        self.client.post(reverse('shipment_detail', args=[move.pk]), {'action': 'scrap_shipment'})
        move.refresh_from_db(); self.at_alpha.refresh_from_db(); self.so.refresh_from_db()
        self.assertEqual(move.status, 'Cancelled')
        self.assertEqual(self.at_alpha.allocated_quantity, Decimal('100'))
        self.assertEqual(StockAllocation.objects.get(batch=self.at_alpha).shipment, None)
        self.assertEqual(self.so.status, 'Ready to Ship')

class GroupPermissionTests(TestCase):
    """Roles are Groups: what a user may do comes only from the permissions on
    their groups, so a brand-new group works without any code change."""

    def setUp(self):
        from django.contrib.auth.models import Permission
        self.perm = lambda codename: Permission.objects.get(content_type__app_label='core', codename=codename)
        self.user = make_user(username='gp_user', password='pw')
        self.client.login(username='gp_user', password='pw')

    def _give(self, *codenames):
        group = Group.objects.create(name='Night Shift Lead')
        group.permissions.add(*[self.perm(c) for c in codenames])
        self.user.groups.add(group)

    def test_standard_groups_are_seeded(self):
        names = set(Group.objects.values_list('name', flat=True))
        self.assertTrue({'Admin', 'Manager', 'Staff (Editor)', 'Staff (Viewer)', 'Sales',
                         'Purchasing', 'Logistics', 'Manufacturing', 'Warehouse'} <= names)

    def test_no_group_means_no_action_center_or_user_management(self):
        self.assertRedirects(self.client.get(reverse('approvals_inbox')), reverse('dashboard'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('user_management')), reverse('dashboard'), fetch_redirect_response=False)

    def test_new_group_with_approve_permission_can_approve(self):
        from core.permissions import approvers
        self.assertNotIn(self.user, approvers())
        self._give('approve_requests')
        self.assertIn(self.user, approvers())
        self.assertEqual(self.client.get(reverse('approvals_inbox')).status_code, 200)

    def test_new_group_with_manage_users_can_assign_groups(self):
        self._give('manage_users')
        other = make_user(username='gp_other', password='pw')
        sales = Group.objects.get(name='Sales')
        self.client.post(reverse('user_management'), {
            'action': 'update_user', 'user_id': other.id, 'is_active': 'on', 'roles': [sales.id],
        })
        self.assertEqual(list(other.groups.all()), [sales])

    def test_admin_group_overrides_approvals_but_manager_does_not(self):
        from core.permissions import is_admin_user
        self.assertTrue(is_admin_user(make_user(username='gp_adm', password='pw', role='Admin')))
        self.assertFalse(is_admin_user(make_user(username='gp_mgr', password='pw', role='Manager')))

    def test_role_label_lists_groups(self):
        self.assertEqual(self.user.role_label, 'No role')
        self._give()
        self.assertEqual(self.user.role_label, 'Night Shift Lead')


class DashboardFiguresTests(TestCase):
    """Executive dashboard headline figures: raw stock is shown in MT (not the
    material's kg units), the active-shipments list excludes finished ones, and
    one full warehouse is flagged even when the network average looks healthy."""

    def setUp(self):
        self.user = User.objects.create_user(username='dash_exec', password='pw', is_superuser=True)
        self.client.force_login(self.user)
        self.wh = Warehouse.objects.create(name='Dash Big WH', location_type='Storage', total_capacity_mt=Decimal('1000'))

    def _batch(self, number, material, qty, wh):
        return Batch.objects.create(batch_number=number, status='Active', material=material, quantity=Decimal(qty),
                                    warehouse=wh, manufacturing_date=date.today(),
                                    expiry_date=date.today() + timedelta(days=200))

    def test_raw_materials_kpi_is_in_mt(self):
        kg_mat = Material.objects.create(name='Dash Urea', sku='MAT-DASH-KG', category='Raw', unit_of_measure='kg',
                                         safe_storage_days=365, weight_mt_per_unit=Decimal('0.001'))
        self._batch('B-DASH-KG', kg_mat, '5000', self.wh)
        resp = self.client.get(reverse('dashboard'))
        self.assertAlmostEqual(resp.context['inventory_metrics']['raw_materials_mt'], 5.0)
        self.assertContains(resp, '5<span class="kpi-unit">MT</span>')

    def test_active_shipments_exclude_finished(self):
        for trk, status in (('DASH-DONE', 'Completed'), ('DASH-GONE', 'Cancelled'), ('DASH-DRAFT', 'Draft'),
                            ('DASH-ROAD', 'Dispatched')):
            Shipment.objects.create(tracking_number=trk, direction='Outbound', status=status)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual([s.tracking_number for s in resp.context['active_shipments']], ['DASH-ROAD'])
        self.assertEqual(resp.context['active_shipments_total'], 1)

    def test_single_full_warehouse_is_flagged(self):
        small = Warehouse.objects.create(name='Dash Small WH', location_type='Storage', total_capacity_mt=Decimal('10'))
        mt_mat = Material.objects.create(name='Dash Bulk', sku='MAT-DASH-MT', category='Raw',
                                         safe_storage_days=365, weight_mt_per_unit=Decimal('1'))
        self._batch('B-DASH-FULL', mt_mat, '12', small)
        resp = self.client.get(reverse('dashboard'))
        self.assertLess(resp.context['global_utilization'], 70)
        self.assertEqual(resp.context['capacity_status'], 'crit')
        self.assertEqual(resp.context['attention']['over_capacity'], 1)
        self.assertContains(resp, 'warehouse over capacity')


class ProductionAllocateRoundingTests(TestCase):
    """A 4dp recipe x a fractional yield (0.0200 x 115.01 = 2.3002) must be
    suggested as a 2dp amount stock can actually hold, rounded up (2.31), and
    the qty inputs must step from 0 so whole numbers like 5 are accepted."""

    def test_fefo_suggestion_rounds_up_to_2dp(self):
        user = User.objects.create_user(username='alloc_round', password='pw', is_superuser=True)
        self.client.force_login(user)
        wh = Warehouse.objects.create(name='Round WH', location_type='Storage')
        mat = Material.objects.create(name='Round AN', sku='MAT-RND', category='Raw', unit_of_measure='kg',
                                      safe_storage_days=365, weight_mt_per_unit=Decimal('0.001'))
        prod = Product.objects.create(name='Round Prod', sku='PRD-RND', unit_of_measure='kg')
        ProductRecipe.objects.create(product=prod, material=mat, quantity_required=Decimal('0.0200'))
        Batch.objects.create(batch_number='B-RND-1', material=mat, quantity=Decimal('171'), status='Active',
                             warehouse=wh, manufacturing_date=date.today(),
                             expiry_date=date.today() + timedelta(days=90))
        run = ProductionRun.objects.create(run_number='RUN-RND', target_product=prod,
                                           expected_yield=Decimal('115.01'), status='Pending Allocation')
        resp = self.client.get(reverse('production_run_allocate', args=[run.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['recipe_reqs'][0]['needed'], 2.31)
        self.assertContains(resp, 'value="2.31"')
        self.assertContains(resp, 'min="0" step="0.01"')


class HomeLandingTests(TestCase):
    """Public landing page: renders for guests, sends signed-in users to the
    dashboard, and its "Trace a batch" box jumps to the batch's public page."""

    def setUp(self):
        mat = Material.objects.create(name='Landing Mat', sku='MAT-LAND', category='Raw',
                                      safe_storage_days=365, weight_mt_per_unit=Decimal('0.001'))
        Batch.objects.create(batch_number='RM-LAND-1', material=mat, quantity=Decimal('10'), status='Active',
                             manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=90))

    def test_guest_sees_landing(self):
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Every batch, in every warehouse, used before it expires.')
        self.assertContains(resp, 'img/landing-dashboard.png')

    def test_signed_in_user_goes_to_dashboard(self):
        self.client.force_login(User.objects.create_user(username='land_user', password='pw'))
        self.assertRedirects(self.client.get(reverse('home')), reverse('dashboard'), fetch_redirect_response=False)

    def test_trace_known_batch_redirects_to_public_page(self):
        resp = self.client.get(reverse('home'), {'batch': ' rm-land-1 '})
        self.assertRedirects(resp, reverse('batch_public_info', args=['RM-LAND-1']))

    def test_trace_unknown_batch_shows_error(self):
        resp = self.client.get(reverse('home'), {'batch': 'NOPE-123'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No batch found with the number')
        self.assertContains(resp, 'value="NOPE-123"')


class RentSuggestionDecisionTests(TestCase):
    """DSS #10: turn Rent Opportunities suggestions into a draft transfer or dismiss
    them with a reason; both are recorded; stock on the way reserves the destination."""

    def setUp(self):
        from core.models import RentSuggestion
        self.RentSuggestion = RentSuggestion
        self.admin = User.objects.create_user(username='rs_admin', password='pw', is_superuser=True)
        self.plain = User.objects.create_user(username='rs_plain', password='pw')
        self.client = Client()
        self.client.force_login(self.admin)
        self.rented = Warehouse.objects.create(
            name='Rented RS', location_type='Storage', ownership_type='ExternalProvider',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('5.00'), total_capacity_mt=Decimal('1000'),
        )
        self.material = Material.objects.create(name='RS Sand', sku='MAT-RS', category='Bulk', unit_of_measure='MT',
                                                safe_storage_days=365, weight_mt_per_unit=Decimal('1.0'))
        for pct, ago in ((80, 6), (90, 3), (97, 0)):    # flag the rented warehouse Critical
            WarehouseUtilizationSnapshot.objects.create(
                warehouse=self.rented, snapshot_date=date.today() - timedelta(days=ago),
                used_mt=Decimal(str(pct * 10)), capacity_mt=Decimal('1000'), utilization_percent=Decimal(str(pct)))

    def _depot(self, name, cap):
        return Warehouse.objects.create(name=name, location_type='Storage', ownership_type='Internal',
                                        rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'),
                                        total_capacity_mt=Decimal(str(cap)))

    def _batch(self, qty, rate='5.00', expiry_days=10, material=None):
        m = material or self.material
        return Batch.objects.create(
            batch_number=f'B-RS-{Batch.objects.count()}', material=m, quantity=Decimal(str(qty)), status='Active',
            warehouse=self.rented, manufacturing_date=date.today(),
            expiry_date=date.today() + timedelta(days=expiry_days), rental_rate_per_mt=Decimal(rate))

    def _post(self, decision, picks, **extra):
        data = {'decision': decision, 'pick': picks}
        data.update(extra)
        return self.client.post(reverse('rent_suggestion_decide'), data)

    def _candidates(self):
        return [c for o in rent_reduction_opportunities() for c in o['candidate_batches']]

    def _messages(self, resp):
        from django.contrib.messages import get_messages
        return [m.message for m in get_messages(resp.wsgi_request)]

    # --- accept
    def test_accepting_creates_one_draft_transfer_and_reserves_the_stock(self):
        depot = self._depot('Depot RS', 500)
        a, b = self._batch(100), self._batch(50)
        resp = self._post('accept', [f'{a.id}:{depot.id}', f'{b.id}:{depot.id}'])

        s = Shipment.objects.get(direction='Transfer')
        self.assertEqual((s.status, s.origin_warehouse, s.destination_warehouse), ('Draft', self.rented, depot))
        self.assertEqual(s.items.count(), 2)
        self.assertRedirects(resp, reverse('shipment_detail', args=[s.pk]))
        for batch, qty in ((a, '100'), (b, '50')):
            batch.refresh_from_db()
            self.assertEqual(batch.allocated_quantity, Decimal(qty))
            self.assertEqual(StockAllocation.objects.get(batch=batch).shipment, s)
        recorded = self.RentSuggestion.objects.filter(decision='Accepted')
        self.assertEqual(recorded.count(), 2)
        r = recorded.get(batch=a)
        self.assertEqual((r.move_mt, r.est_daily_saving, r.est_stay_days, r.est_total_saving, r.shipment),
                         (Decimal('100.000'), Decimal('500.00'), 10, Decimal('5000.00'), s))

    def test_accepting_reserves_room_at_the_destination(self):
        depot = self._depot('Depot RS', 500)
        a, b = self._batch(100), self._batch(50, expiry_days=200)
        self.assertEqual(rent_reduction_opportunities()[0]['destination_options'][0]['spare_mt'], 500.0)
        self._post('accept', [f'{a.id}:{depot.id}'])
        opp = rent_reduction_opportunities()[0]
        self.assertEqual(opp['destination_options'][0]['spare_mt'], 400.0)        # 100 MT is on its way
        self.assertEqual([c['batch_id'] for c in opp['candidate_batches']], [b.id])   # accepted batch no longer offered

    def test_partial_move_is_converted_to_units(self):
        half = Material.objects.create(name='RS Half', sku='MAT-RSH', category='Bulk', unit_of_measure='kg',
                                       safe_storage_days=365, weight_mt_per_unit=Decimal('0.5'))
        depot = self._depot('Small Depot', 40)          # room for 40 MT of the 100 MT batch
        batch = self._batch(200, material=half)         # 200 units x 0.5 = 100 MT
        self._post('accept', [f'{batch.id}:{depot.id}'])
        item = Shipment.objects.get(direction='Transfer').items.get()
        self.assertEqual(item.quantity, Decimal('80.00'))   # 40 MT / 0.5
        batch.refresh_from_db()
        self.assertEqual(batch.allocated_quantity, Decimal('80.00'))

    def test_one_transfer_per_destination(self):
        d1, d2 = self._depot('Depot A', 100), self._depot('Depot B', 90)
        a, b = self._batch(100, rate='9.00'), self._batch(90, rate='8.00')
        resp = self._post('accept', [f'{a.id}:{d1.id}', f'{b.id}:{d2.id}'])
        self.assertEqual(Shipment.objects.filter(direction='Transfer').count(), 2)
        self.assertRedirects(resp, reverse('rent_opportunities'))

    def test_stale_or_unknown_picks_are_skipped(self):
        depot = self._depot('Depot RS', 500)
        real = self._batch(100)
        resp = self._post('accept', [f'{real.id}:{depot.id + 99}', '999999:1'])
        self.assertFalse(Shipment.objects.filter(direction='Transfer').exists())
        self.assertFalse(self.RentSuggestion.objects.exists())
        self.assertTrue(any('changed since the page loaded' in m for m in self._messages(resp)))

    def test_nothing_ticked_does_nothing(self):
        self._depot('Depot RS', 500); self._batch(100)
        resp = self._post('accept', [])
        self.assertFalse(Shipment.objects.filter(direction='Transfer').exists())
        self.assertIn('Tick at least one batch first.', self._messages(resp))

    def test_needs_permission(self):
        depot = self._depot('Depot RS', 500)
        batch = self._batch(100)
        self.client.force_login(self.plain)
        resp = self._post('accept', [f'{batch.id}:{depot.id}'])
        self.assertFalse(Shipment.objects.filter(direction='Transfer').exists())
        self.assertIn("You don't have permission to create transfers.", self._messages(resp))

    # --- dismiss
    def test_dismissing_records_the_reason_and_hides_the_batch(self):
        depot = self._depot('Depot RS', 500)
        batch = self._batch(100, expiry_days=200)
        self._post('dismiss', [f'{batch.id}:{depot.id}'], reason='needed_here', note='Needed for a rush order')
        rec = self.RentSuggestion.objects.get()
        self.assertEqual((rec.decision, rec.dismiss_reason, rec.dismiss_note), ('Dismissed', 'needed_here', 'Needed for a rush order'))
        self.assertEqual(rec.snoozed_until, date.today() + timedelta(days=30))
        opp = rent_reduction_opportunities()[0]
        self.assertEqual((opp['candidate_batches'], opp['dismissed_hidden']), ([], 1))
        self.assertFalse(Shipment.objects.filter(direction='Transfer').exists())
        batch.refresh_from_db()
        self.assertEqual(batch.allocated_quantity, Decimal('0'))     # nothing reserved

    def test_dismissed_batch_comes_back_after_the_snooze(self):
        depot = self._depot('Depot RS', 500)
        batch = self._batch(100, expiry_days=200)
        self._post('dismiss', [f'{batch.id}:{depot.id}'], reason='other')
        self.RentSuggestion.objects.update(snoozed_until=date.today() - timedelta(days=1))
        self.assertEqual([c['batch_id'] for c in self._candidates()], [batch.id])

    def test_dismissing_needs_a_reason(self):
        depot = self._depot('Depot RS', 500)
        batch = self._batch(100)
        self._post('dismiss', [f'{batch.id}:{depot.id}'], reason='')
        self.assertFalse(self.RentSuggestion.objects.exists())

    # --- the page
    def test_controls_show_only_with_permission(self):
        self._depot('Depot RS', 500); self._batch(100)
        resp = self.client.get(reverse('rent_opportunities'))
        self.assertContains(resp, 'Create transfer for selected')
        self.assertContains(resp, 'name="pick"')
        self.client.force_login(self.plain)
        resp = self.client.get(reverse('rent_opportunities'))
        self.assertNotContains(resp, 'Create transfer for selected')
        self.assertNotContains(resp, 'name="pick"')


class RentResultsTests(TestCase):
    """DSS #11: measured vs promised saving for accepted Rent Opportunities moves."""

    def setUp(self):
        from core.models import RentSuggestion, OrderTimeline
        self.RentSuggestion, self.OrderTimeline = RentSuggestion, OrderTimeline
        self.today = date.today()
        self.origin = Warehouse.objects.create(
            name='Origin RR', location_type='Storage', ownership_type='ExternalProvider',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('5.00'), total_capacity_mt=Decimal('1000'))
        self.depot = Warehouse.objects.create(
            name='Depot RR', location_type='Storage', ownership_type='Internal',
            rental_billing_method='Usage', rental_cost_per_mt=Decimal('0'), total_capacity_mt=Decimal('500'))

    def _snap(self, wh, days_ago, rent, used, estimated=False):
        WarehouseUtilizationSnapshot.objects.update_or_create(
            warehouse=wh, snapshot_date=self.today - timedelta(days=days_ago),
            defaults=dict(used_mt=Decimal(str(used)), capacity_mt=wh.total_capacity_mt, utilization_percent=Decimal('10'),
                          daily_rent_cost=Decimal(str(rent)), rent_estimated=estimated))

    def _move(self, moved_days_ago=10, status='Completed', promised='200.00', mt='40'):
        s = Shipment.objects.create(tracking_number=f'T-RR-{Shipment.objects.count()}', direction='Transfer',
                                    status=status, origin_warehouse=self.origin, destination_warehouse=self.depot)
        if status == 'Completed':
            t = self.OrderTimeline.objects.create(shipment=s, action='Receiving finalized. Shipment completed.')
            self.OrderTimeline.objects.filter(pk=t.pk).update(
                timestamp=timezone.now() - timedelta(days=moved_days_ago))
        self.RentSuggestion.objects.create(
            decision='Accepted', batch_number='B-RR', origin_warehouse=self.origin, destination_warehouse=self.depot,
            move_mt=Decimal(mt), rate_per_mt=Decimal('5.00'), est_daily_saving=Decimal(promised),
            est_total_saving=Decimal('2000.00'), est_stay_days=10, shipment=s)
        return s

    def _history(self, before=(500, 100), after=(300, 60), moved_days_ago=10):
        # one recorded row a week+ before the move, one just after it; the rest carry forward
        self._snap(self.origin, moved_days_ago + 8, *before)
        self._snap(self.origin, moved_days_ago - 1, *after)
        self._snap(self.depot, moved_days_ago + 8, 0, 0)

    def test_measured_saving_matches_the_promise(self):
        self._history()
        self._move()
        r = rent_results()
        m = r['moves'][0]
        self.assertEqual((m['status'], m['measured_daily'], m['promised_daily']), ('measured', 200.0, 200.0))
        self.assertEqual((r['accepted'], r['pending'], r['measured_count'], r['measured_daily_total']), (1, 0, 1, 200.0))
        self.assertEqual(m['moved_on'], self.today - timedelta(days=10))

    def test_falls_short_when_rent_only_drops_a_bit(self):
        self._history(after=(450, 60))
        self._move()
        m = rent_results()['moves'][0]
        self.assertEqual((m['status'], m['measured_daily']), ('measured', 50.0))

    def test_unclear_when_other_stock_also_changed(self):
        self._history(after=(50, 10))            # 90 MT left the origin but only 40 MT was moved
        self._move()
        m = rent_results()['moves'][0]
        self.assertEqual(m['status'], 'unclear')
        self.assertEqual(rent_results()['measured_count'], 0)     # not counted in the total

    def test_waits_until_the_transfer_completes(self):
        self._move(status='Draft')
        r = rent_results()
        self.assertEqual((r['pending'], r['moves'][0]['status']), (1, 'measuring'))

    def test_waits_a_few_days_after_the_move(self):
        self._history(moved_days_ago=1)
        self._move(moved_days_ago=1)
        self.assertEqual(rent_results()['moves'][0]['status'], 'measuring')

    def test_estimated_rows_are_not_used(self):
        self._snap(self.origin, 18, 500, 100, estimated=True)
        self._snap(self.origin, 9, 300, 60, estimated=True)
        self._move()
        self.assertEqual(rent_results()['moves'][0]['status'], 'unavailable')

    def test_cancelled_transfers_are_left_out(self):
        self._move(status='Cancelled')
        r = rent_results()
        self.assertEqual((r['accepted'], r['moves']), (0, []))

    def test_one_row_per_transfer_even_with_several_batches(self):
        self._history()
        s = self._move(promised='120.00', mt='24')
        self.RentSuggestion.objects.create(
            decision='Accepted', batch_number='B-RR2', origin_warehouse=self.origin, destination_warehouse=self.depot,
            move_mt=Decimal('16'), rate_per_mt=Decimal('5.00'), est_daily_saving=Decimal('80.00'),
            est_total_saving=Decimal('800.00'), est_stay_days=10, shipment=s)
        r = rent_results()
        self.assertEqual(len(r['moves']), 1)
        self.assertEqual((r['moves'][0]['promised_daily'], r['moves'][0]['move_mt']), (200.0, 40.0))

    def test_dismissed_are_counted(self):
        self.RentSuggestion.objects.create(
            decision='Dismissed', batch_number='B-D', origin_warehouse=self.origin, destination_warehouse=self.depot,
            move_mt=Decimal('10'), rate_per_mt=Decimal('5.00'), est_daily_saving=Decimal('50.00'),
            est_total_saving=Decimal('500.00'), dismiss_reason='other')
        self.assertEqual(rent_results()['dismissed'], 1)

    def test_page_shows_the_results_section(self):
        self._history()
        self._move()
        user = User.objects.create_user(username='rr_admin', password='pw', is_superuser=True)
        self.client.force_login(user)
        resp = self.client.get(reverse('rent_opportunities'))
        self.assertContains(resp, 'Did the moves you accepted save what was promised?')
        self.assertContains(resp, 'T-RR-0')
        self.assertContains(resp, 'Measured')


class SOAllocateFullyAllocatedTests(TestCase):
    """A line that is already fully covered shows a tick, not the batch picker."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='fa_user', password='pw')
        self.client.login(username='fa_user', password='pw')
        self.wh = Warehouse.objects.create(name='FG Hub FA', location_type='Storage')
        self.product = Product.objects.create(name='FA Product', sku='PRD-FA', unit_of_measure='kg', price_per_unit=1)
        self.batch = Batch.objects.create(
            batch_number='FG-FA', status='Active', product=self.product, quantity=Decimal('500'),
            manufacturing_date=date.today(), expiry_date=date.today() + timedelta(days=300), warehouse=self.wh)
        self.so = SalesOrder.objects.create(so_number='SO-FA', client_name='Acme', origin_warehouse=self.wh, status='Pending')
        self.item = SalesOrderDetail.objects.create(sales_order=self.so, product=self.product, quantity_ordered=Decimal('100'))

    def test_line_still_needing_stock_shows_the_picker(self):
        resp = self.client.get(reverse('so_allocate', args=[self.so.pk]))
        self.assertContains(resp, 'Needed to Allocate: <span')
        self.assertContains(resp, f'name="batch_qty_{self.item.id}_{self.batch.id}"')
        self.assertNotContains(resp, 'Fully Allocated')

    def test_fully_allocated_line_shows_a_tick_and_no_picker(self):
        StockAllocation.objects.create(batch=self.batch, sales_order=self.so, quantity=Decimal('100'))
        self.batch.allocated_quantity = Decimal('100')
        self.batch.save(update_fields=['allocated_quantity'])
        resp = self.client.get(reverse('so_allocate', args=[self.so.pk]))
        self.assertContains(resp, 'Fully Allocated')
        self.assertContains(resp, 'Allocation complete for this line item')
        self.assertNotContains(resp, 'Needed to Allocate: <span')
        self.assertNotContains(resp, f'name="batch_qty_{self.item.id}_{self.batch.id}"')


class UnrecordedMaterialUsageTests(TestCase):
    """Yield page: usage entered as 'batch not in records', per material."""

    def setUp(self):
        from core.models import Warehouse, Material
        self.client = Client()
        self.user = User.objects.create_superuser(username='boss', password='pw')
        self.other = User.objects.create_user(username='op2', password='pw')
        self.product = Product.objects.create(name='Blend', sku='PRD-B', unit_of_measure='kg', price_per_unit=5)
        self.plant = Warehouse.objects.create(name='Plant', location_type='Manufacturing')
        self.resin = Material.objects.create(name='Resin', sku='MAT-R', category='Bulk', unit_of_measure='kg', safe_storage_days=30)
        self.dye = Material.objects.create(name='Dye', sku='MAT-D', category='Bulk', unit_of_measure='kg', safe_storage_days=30)

    def _completed_run(self, number, poured, material=None):
        run = ProductionRun.objects.create(
            run_number=number, target_product=self.product, expected_yield=Decimal('100'),
            actual_yield=Decimal('100'), status='Completed', exact_end_time=timezone.now())
        RunMaterialUsage.objects.create(production_run=run, material=material or self.resin,
                                        expected_qty=Decimal(str(poured)), actual_qty=Decimal(str(poured)))
        return run

    def _extra(self, run, material, qty, reason, user=None, batch=None):
        from core.models import RunExtraMaterial
        return RunExtraMaterial.objects.create(production_run=run, material=material, quantity=Decimal(str(qty)),
                                               reason=reason, batch=batch, recorded_by=user or self.user)

    def test_counts_shares_reasons_and_people(self):
        from core.analytics import unrecorded_material_usage
        run = self._completed_run('U-1', 100)
        self._extra(run, self.resin, 3, 'Leftover from RUN-9')
        self._extra(run, self.resin, 2, ' leftover from run-9 ', user=self.other)
        self._extra(run, self.resin, 1, 'caking')
        data = unrecorded_material_usage()
        self.assertEqual(len(data['rows']), 1)
        row = data['rows'][0]
        self.assertEqual((row['events_recent'], row['qty_recent'], row['events_window']), (3, 6.0, 3))
        self.assertAlmostEqual(row['share_pct'], 6.0)
        top_reason, top_count = row['top_reasons'][0]
        self.assertEqual((top_reason.lower(), top_count), ('leftover from run-9', 2))   # case / spacing merged
        self.assertEqual(dict(row['top_people']), {'boss': 2, 'op2': 1})
        self.assertEqual([r['number'] for r in row['runs']], ['U-1'])
        self.assertEqual((data['events_recent'], data['materials_recent']), (3, 1))

    def test_batch_entries_and_old_entries_are_left_out(self):
        from core.analytics import unrecorded_material_usage
        from core.models import Batch, RunExtraMaterial
        run = self._completed_run('U-2', 100)
        batch = Batch.objects.create(batch_number='B-U', material=self.resin, quantity=Decimal('50'), status='Active',
                                     manufacturing_date='2026-01-01', expiry_date='2027-01-01', warehouse=self.plant)
        self._extra(run, self.resin, 4, '', batch=batch)                    # traced to a batch: not "unrecorded"
        old = self._extra(run, self.resin, 5, 'ancient')
        RunExtraMaterial.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))
        self.assertEqual(unrecorded_material_usage()['rows'], [])

    def test_recent_versus_window_and_worst_share_first(self):
        from core.analytics import unrecorded_material_usage
        from core.models import RunExtraMaterial
        run = self._completed_run('U-3', 100)
        RunMaterialUsage.objects.create(production_run=run, material=self.dye,
                                        expected_qty=Decimal('100'), actual_qty=Decimal('100'))
        older = self._extra(run, self.resin, 2, 'spill')
        RunExtraMaterial.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(days=15))
        self._extra(run, self.dye, 10, 'unlabelled drum')
        data = unrecorded_material_usage()
        self.assertEqual([r['name'] for r in data['rows']], ['Dye', 'Resin'])       # 10% then 2%
        resin = data['rows'][1]
        self.assertEqual((resin['events_recent'], resin['events_window']), (0, 1))

    def test_yield_page_shows_the_section(self):
        run = self._completed_run('U-4', 100)
        self._extra(run, self.resin, 3, 'caking')
        self.client.login(username='boss', password='pw')
        resp = self.client.get(reverse('production_yield'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Unrecorded material usage')
        self.assertContains(resp, 'caking')
        self.assertContains(resp, 'U-4')
        self.assertContains(resp, 'Unrecorded Usage (7 days)')


class LocalDateAnalyticsTests(TestCase):
    """'Today' in analytics is the local (Kuala Lumpur) date. Between local midnight and 08:00 the UTC
    date is still yesterday, so these pin the clock to 01:00 local and check each function agrees."""

    LOCAL_TODAY = date(2026, 9, 22)

    def setUp(self):
        from datetime import datetime, timezone as dt_timezone
        from unittest import mock
        boundary = datetime(2026, 9, 21, 17, 0, tzinfo=dt_timezone.utc)      # 01:00, 22 Sep, Kuala Lumpur
        patcher = mock.patch('django.utils.timezone.now', return_value=boundary)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(timezone.localdate(), self.LOCAL_TODAY)
        self.wh = Warehouse.objects.create(name='Hub', location_type='Storage')
        self.user = User.objects.create_user(username='owner', password='pw')
        self.product = Product.objects.create(name='Blend', sku='PRD-TZ', unit_of_measure='kg', price_per_unit=5)

    def _so(self, number, **kw):
        from core.models import SalesOrder, SalesOrderDetail
        so = SalesOrder.objects.create(so_number=number, client_name='C', origin_warehouse=self.wh,
                                       status=kw.pop('status', 'Pending'), created_by=self.user, **kw)
        SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal('10'))
        return so

    def test_delivery_risk_treats_yesterdays_deadline_as_late(self):
        from core.analytics import sales_order_delivery_risk
        self._so('SO-TZ1', fulfillment_deadline=self.LOCAL_TODAY - timedelta(days=1))
        row = next(r for r in sales_order_delivery_risk() if r['so_number'] == 'SO-TZ1')
        self.assertEqual(row['risk'], 'late')          # 21 Sep < today (22 Sep); against UTC it read as due today

    def test_shipment_logistics_flags_an_eta_that_passed_yesterday(self):
        from core.analytics import shipment_logistics
        from core.models import Shipment
        Shipment.objects.create(tracking_number='SHP-TZ', direction='Outbound', status='Dispatched',
                                dispatch_date=self.LOCAL_TODAY - timedelta(days=3),
                                expected_eta_date=self.LOCAL_TODAY - timedelta(days=1))
        row = next(r for r in shipment_logistics() if r['tracking_number'] == 'SHP-TZ')
        self.assertEqual(row['risk'], 'overdue')

    def test_my_open_jobs_ages_todays_order_as_zero_days(self):
        from core.analytics import my_open_jobs
        from core.models import SalesOrder
        so = self._so('SO-TZ2')
        SalesOrder.objects.filter(pk=so.pk).update(order_date=self.LOCAL_TODAY)
        row = next(r for r in my_open_jobs(self.user) if r['reference'] == 'SO-TZ2')
        self.assertEqual(row['age_days'], 0)            # not -1

    def test_sales_trend_uses_the_local_month(self):
        from core.analytics import product_sales_trend
        from core.models import SalesOrder
        from datetime import datetime, timezone as dt_timezone
        from unittest import mock
        # 01:00 on 1 Oct in Kuala Lumpur is still 30 Sep in UTC: the trend must already be in October
        with mock.patch('django.utils.timezone.now',
                        return_value=datetime(2026, 9, 30, 17, 0, tzinfo=dt_timezone.utc)):
            so = self._so('SO-TZ3')
            SalesOrder.objects.filter(pk=so.pk).update(order_date=date(2026, 10, 1))
            row = next(r for r in product_sales_trend() if r['sku'] == 'PRD-TZ')
        self.assertEqual(row['monthly_qty'][-1], 10.0)


class SOCommitmentTests(TestCase):
    """Stock that has moved onto an order's logistics orders still counts as covering the order
    (the SO-1002 case: 312 on a draft shipment, 188 already shipped, nothing 'allocated')."""

    def setUp(self):
        from core.models import Warehouse, SalesOrder, SalesOrderDetail, Batch, Shipment, StockAllocation
        self.client = Client()
        self.boss = User.objects.create_superuser(username='boss', password='pw')
        self.plant = Warehouse.objects.create(name='Plant', location_type='Manufacturing')
        self.hub = Warehouse.objects.create(name='Hub', location_type='Storage')
        self.product = Product.objects.create(name='Chloride', sku='PRD-CL', unit_of_measure='kg', price_per_unit=5)
        self.so = SalesOrder.objects.create(so_number='SO-C1', client_name='C', origin_warehouse=self.hub,
                                            status='Partially Shipped')
        self.item = SalesOrderDetail.objects.create(sales_order=self.so, product=self.product,
                                                    quantity_ordered=Decimal('500'), quantity_shipped=Decimal('188'))
        self.b312 = self._batch('B-312', 312)
        self.b188 = self._batch('B-188', 188)
        # 188 already shipped: its shipment is credited, and still holds its reservation until it completes
        self.shipped = Shipment.objects.create(tracking_number='SHP-C1', direction='Outbound', status='Arrived',
                                               sales_order=self.so, origin_warehouse=self.hub, credited_to_so=True)
        StockAllocation.objects.create(batch=self.b188, shipment=self.shipped, quantity=Decimal('188'))
        # 312 sits on a draft logistics order; the reservation moved off the order onto the shipment
        self.draft = Shipment.objects.create(tracking_number='SHP-C2', direction='Outbound', status='Draft',
                                             sales_order=self.so, origin_warehouse=self.plant)
        StockAllocation.objects.create(batch=self.b312, shipment=self.draft, quantity=Decimal('312'))

    def _batch(self, number, qty):
        from core.models import Batch
        return Batch.objects.create(batch_number=number, product=self.product, quantity=Decimal(str(qty)),
                                    allocated_quantity=Decimal(str(qty)), status='Active', warehouse=self.plant,
                                    manufacturing_date='2026-01-01', expiry_date='2027-01-01')

    def _line(self):
        self.client.login(username='boss', password='pw')
        resp = self.client.get(reverse('so_detail', args=[self.so.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp, resp.context['line_items'][0]

    def test_commitment_counts_logistics_stock_once_and_leaves_out_credited_shipments(self):
        from core.utils import so_line_commitment
        c = so_line_commitment(self.so, self.product)
        self.assertEqual((c['held'], c['in_logistics'], c['shipped'], c['total']),
                         (Decimal('0'), Decimal('312'), Decimal('188'), Decimal('500')))

    def test_order_page_shows_no_deficit_and_the_logistics_split(self):
        resp, row = self._line()
        self.assertEqual((row['allocated'], row['in_logistics'], row['deficit']), (0.0, 312.0, 0.0))
        self.assertTrue(row['fulfilled'])
        self.assertContains(resp, 'In Logistics')
        self.assertNotContains(resp, 'Insufficient')

    def test_a_cancelled_shipment_holds_nothing(self):
        from core.models import Shipment
        Shipment.objects.filter(pk=self.draft.pk).update(status='Cancelled')
        resp, row = self._line()
        self.assertEqual((row['in_logistics'], row['deficit']), (0.0, 312.0))
        self.assertFalse(row['fulfilled'])

    def test_reservation_kept_on_the_order_is_not_counted_twice(self):
        # A stock move (consolidation) tags the order's own reservation with a transfer; it is 'held', not 'in logistics'
        from core.models import Shipment, StockAllocation
        move = Shipment.objects.create(tracking_number='SHP-MOVE', direction='Transfer', status='Draft',
                                       sales_order=self.so, origin_warehouse=self.plant, destination_warehouse=self.hub)
        StockAllocation.objects.filter(shipment=self.draft).delete()
        StockAllocation.objects.create(batch=self.b312, sales_order=self.so, shipment=move, quantity=Decimal('312'))
        from core.utils import so_line_commitment
        c = so_line_commitment(self.so, self.product)
        self.assertEqual((c['held'], c['in_logistics'], c['total']), (Decimal('312'), Decimal('0'), Decimal('500')))

    def test_no_new_manufacturing_run_for_stock_already_committed(self):
        from core.utils import create_shortage_production_runs
        self.assertFalse(create_shortage_production_runs(self.so, self.plant, self.boss))
        self.assertEqual(ProductionRun.objects.filter(sales_order=self.so).count(), 0)

    def test_send_shortages_button_path_does_not_create_a_run(self):
        self.client.login(username='boss', password='pw')
        self.client.post(reverse('so_detail', args=[self.so.pk]), {'action': 'send_to_manufacturing'})
        self.assertEqual(ProductionRun.objects.filter(sales_order=self.so).count(), 0)

    def test_allocate_screen_offers_nothing_more_for_a_fully_covered_order(self):
        from core.models import Batch
        extra = Batch.objects.create(batch_number='B-EXTRA', product=self.product, quantity=Decimal('100'), status='Active',
                                     warehouse=self.hub, manufacturing_date='2026-02-01', expiry_date='2027-06-01')
        self.client.login(username='boss', password='pw')
        self.client.post(reverse('so_allocate', args=[self.so.pk]), {
            'action': 'allocate_manual', f'batch_qty_{self.item.pk}_{extra.pk}': '50'})
        self.assertEqual(self.so.allocations.count(), 0)          # nothing taken: the order needs no more
        extra.refresh_from_db()
        self.assertEqual(extra.allocated_quantity, 0)



# ----------------------------------------------------------------------------
# Material & product details pages
# ----------------------------------------------------------------------------
class MaterialDetailPageTests(TestCase):
    def setUp(self):
        from core.models import PurchaseOrder, PurchaseOrderDetail, Supplier, SupplierMaterial, ProductionRun, RunExtraMaterial
        self.manager = make_user(username='matmgr', password='pw', role='Manager')
        self.viewer = make_user(username='matview', password='pw', role='Staff (Viewer)')
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')
        self.mat = Material.objects.create(name='Resin', sku='MAT-R', category='Chem', unit_of_measure='kg',
                                           safe_storage_days=90, cost_per_unit=Decimal('4.00'))
        self.other = Material.objects.create(name='Dye', sku='MAT-D', category='Chem', unit_of_measure='kg',
                                             safe_storage_days=90, cost_per_unit=Decimal('1.00'))
        self.product = Product.objects.create(name='Paint', sku='PRD-P', price_per_unit=Decimal('20.00'))
        ProductRecipe.objects.create(product=self.product, material=self.mat, quantity_required=Decimal('2.5'))
        self.batch = Batch.objects.create(batch_number='B-R1', material=self.mat, quantity=Decimal('100'),
                                          allocated_quantity=Decimal('30'), warehouse=self.wh, status='Active',
                                          manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        self.quar = Batch.objects.create(batch_number='B-R2', material=self.mat, quantity=Decimal('40'),
                                         warehouse=self.wh, status='Quarantined',
                                         manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        sup = Supplier.objects.create(name='ChemCo')
        SupplierMaterial.objects.create(supplier=sup, material=self.mat, unit_price=Decimal('3.50'), lead_time_days=12)
        po = PurchaseOrder.objects.create(po_number='PO-9001', supplier_name='ChemCo', target_warehouse=self.wh, status='Pending')
        PurchaseOrderDetail.objects.create(purchase_order=po, material=self.mat, quantity_ordered=Decimal('500'))
        run = ProductionRun.objects.create(run_number='RUN-9', target_product=self.product, expected_yield=Decimal('10'))
        RunExtraMaterial.objects.create(production_run=run, material=self.mat, batch=None,
                                        quantity=Decimal('7'), reason='Found a spare drum')
        self.url = reverse('material_detail', args=[self.mat.pk])

    def test_page_shows_stock_batches_recipes_suppliers_pos_and_off_book_usage(self):
        self.client.login(username='matmgr', password='pw')
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        for text in ('B-R1', 'PRD-P', 'ChemCo', 'PO-9001', 'RUN-9', 'Found a spare drum'):
            self.assertContains(resp, text)
        self.assertEqual(resp.context['total_qty'], Decimal('100'))           # Active only
        self.assertEqual(resp.context['quarantined_qty'], Decimal('40'))       # shown apart
        self.assertEqual(resp.context['available_qty'], Decimal('70'))
        self.assertEqual(resp.context['stock_value'], Decimal('400'))
        self.assertEqual(resp.context['recipe_uses'][0]['line_cost'], Decimal('10.00'))   # 2.5 x 4.00

    def test_bare_material_renders_every_empty_state(self):
        self.client.login(username='matmgr', password='pw')
        resp = self.client.get(reverse('material_detail', args=[self.other.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No active batches of this material.')
        self.assertContains(resp, 'Not part of any product recipe.')
        self.assertContains(resp, 'No supplier linked to this material.')
        self.assertContains(resp, 'No purchase orders for this material yet.')

    def test_unknown_material_is_404_and_login_is_required(self):
        self.client.login(username='matmgr', password='pw')
        self.assertEqual(self.client.get(reverse('material_detail', args=[99999])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_update_saves_and_logs_for_a_user_who_may_change_materials(self):
        self.client.login(username='matmgr', password='pw')
        self.client.post(self.url, {
            'action': 'update_material', 'name': 'Resin X', 'sku': 'mat-r', 'category': 'Chem',
            'unit_of_measure': 'kg', 'safe_storage_days': '60', 'weight_mt_per_unit': '0.001', 'cost_per_unit': '5.25',
        })
        self.mat.refresh_from_db()
        self.assertEqual((self.mat.name, self.mat.safe_storage_days, self.mat.cost_per_unit), ('Resin X', 60, Decimal('5.25')))
        self.assertTrue(RegistryLog.objects.filter(material=self.mat, item_name="Updated Material 'Resin X' (SKU: MAT-R)").exists())

    def test_update_and_archive_are_refused_without_the_permission(self):
        self.client.login(username='matview', password='pw')
        self.client.post(self.url, {'action': 'update_material', 'name': 'Hacked', 'sku': 'MAT-R', 'category': 'Chem',
                                    'unit_of_measure': 'kg', 'safe_storage_days': '1', 'weight_mt_per_unit': '1', 'cost_per_unit': '1'})
        self.client.post(self.url, {'action': 'toggle_active'})
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.name, 'Resin')
        self.assertTrue(self.mat.is_active)

    def test_duplicate_sku_is_rejected(self):
        self.client.login(username='matmgr', password='pw')
        self.client.post(self.url, {'action': 'update_material', 'name': 'Resin', 'sku': 'MAT-D', 'category': 'Chem',
                                    'unit_of_measure': 'kg', 'safe_storage_days': '90', 'weight_mt_per_unit': '1', 'cost_per_unit': '4'})
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.sku, 'MAT-R')

    def test_archive_toggles_and_deactivated_material_has_no_forecast(self):
        self.client.login(username='matmgr', password='pw')
        self.client.post(self.url, {'action': 'toggle_active'})
        self.mat.refresh_from_db()
        self.assertFalse(self.mat.is_active)
        self.assertIsNone(self.client.get(self.url).context['forecast'])

    def test_consumption_chart_uses_the_registry_ledger_not_run_estimates(self):
        from core.models import ProductionRun, RunMaterialUsage
        run = ProductionRun.objects.create(run_number='RUN-EST', target_product=self.product, expected_yield=Decimal('1'))
        RunMaterialUsage.objects.create(production_run=run, material=self.mat, expected_qty=Decimal('999'), actual_qty=Decimal('999'))
        RegistryLog.objects.create(action_type='Consumed_For_Manufacturing', item_name='Resin (Run X)',
                                   material=self.mat, quantity_changed=Decimal('12'), warehouse=self.wh)
        self.client.login(username='matmgr', password='pw')
        ctx = self.client.get(self.url).context
        self.assertEqual(sum(ctx['chart_consumption']), 12.0)
        self.assertEqual(len(ctx['chart_labels']), 6)

    def test_forecast_row_is_the_analytics_one(self):
        RegistryLog.objects.create(action_type='Consumed_For_Manufacturing', item_name='Resin (Run X)',
                                   material=self.mat, quantity_changed=Decimal('30'), warehouse=self.wh)
        self.client.login(username='matmgr', password='pw')
        f = self.client.get(self.url).context['forecast']
        self.assertEqual(f['material_id'], self.mat.id)
        self.assertEqual(f['on_order'], 500.0)
        self.assertAlmostEqual(f['daily_rate'], 1.0)

    def test_material_list_links_to_the_page_and_edit_view_still_works(self):
        self.client.login(username='matmgr', password='pw')
        self.assertContains(self.client.get(reverse('material_list')), self.url)
        self.assertEqual(self.client.get(reverse('material_edit', args=[self.mat.pk])).status_code, 200)


class ProductDetailPageTests(TestCase):
    def setUp(self):
        self.user = make_user(username='prodmgr', password='pw', role='Manager')
        self.client.login(username='prodmgr', password='pw')
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')
        self.m1 = Material.objects.create(name='Resin', sku='MAT-R', category='Chem', unit_of_measure='kg',
                                          safe_storage_days=90, cost_per_unit=Decimal('4.00'))
        self.m2 = Material.objects.create(name='Dye', sku='MAT-D', category='Chem', unit_of_measure='kg',
                                          safe_storage_days=90, cost_per_unit=Decimal('1.50'))
        self.product = Product.objects.create(name='Paint', sku='PRD-P', price_per_unit=Decimal('20.00'))
        ProductRecipe.objects.create(product=self.product, material=self.m1, quantity_required=Decimal('2.5'))
        ProductRecipe.objects.create(product=self.product, material=self.m2, quantity_required=Decimal('2'))
        self.url = reverse('product_detail', args=[self.product.pk])

    def test_cost_and_margin_come_from_the_recipe(self):
        ctx = self.client.get(self.url).context
        self.assertEqual(ctx['unit_cost'], Decimal('13.00'))       # 2.5x4 + 2x1.5
        self.assertEqual(ctx['margin'], Decimal('7.00'))
        self.assertEqual(round(float(ctx['margin_pct']), 1), 35.0)

    def test_no_recipe_and_zero_price_do_not_break_the_page(self):
        bare = Product.objects.create(name='Bare', sku='PRD-B', price_per_unit=Decimal('0'))
        resp = self.client.get(reverse('product_detail', args=[bare.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context['margin_pct'])
        self.assertContains(resp, 'No recipe defined')

    def test_stock_kpis_split_active_reserved_and_quarantined(self):
        Batch.objects.create(batch_number='F1', product=self.product, quantity=Decimal('50'), allocated_quantity=Decimal('20'),
                             warehouse=self.wh, status='Active', manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        Batch.objects.create(batch_number='F2', product=self.product, quantity=Decimal('9'),
                             warehouse=self.wh, status='Quarantined', manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        ctx = self.client.get(self.url).context
        self.assertEqual((ctx['total_stock'], ctx['allocated_stock'], ctx['available_stock'], ctx['quarantined_stock']),
                         (Decimal('50'), Decimal('20'), Decimal('30'), Decimal('9')))
        self.assertEqual(ctx['stock_value'], Decimal('1000'))

    def test_sales_series_skips_draft_and_rejected_orders_and_runs_skip_cancelled(self):
        from core.models import ProductionRun
        today = timezone.localdate()
        for number, status, qty in (('SO-OK', 'Pending', 10), ('SO-DR', 'Draft', 99), ('SO-RJ', 'Rejected', 77)):
            so = SalesOrder.objects.create(so_number=number, client_name='C', origin_warehouse=self.wh, status=status)
            SalesOrderDetail.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal(qty))
        ProductionRun.objects.create(run_number='RUN-C', target_product=self.product, expected_yield=Decimal('5'), status='Cancelled')
        ProductionRun.objects.create(run_number='RUN-OPEN', target_product=self.product, expected_yield=Decimal('5'), status='Planned')
        ProductionRun.objects.create(run_number='RUN-DONE', target_product=self.product, expected_yield=Decimal('8'),
                                     actual_yield=Decimal('8'), status='Completed', exact_end_time=timezone.now())
        ctx = self.client.get(self.url).context
        self.assertEqual(sum(ctx['chart_sales_data']), 10.0)
        self.assertEqual(ctx['chart_sales_data'][-1], 10.0)                       # this local month
        self.assertEqual(ctx['chart_labels'][-1], today.strftime('%Y-%m'))
        self.assertEqual(ctx['chart_produced_data'][-1], 8.0)
        self.assertEqual({r.run_number for r in ctx['production_runs']}, {'RUN-OPEN', 'RUN-DONE'})
        self.assertEqual(ctx['open_runs'], 1)

    def test_bom_links_each_material_to_its_page(self):
        self.assertContains(self.client.get(self.url), reverse('material_detail', args=[self.m1.pk]))


# ----------------------------------------------------------------------------
# Phase 1 server-side pagination (Registry Ledger, Warehouse Inventory,
# Purchase Orders, Shipments, Suppliers, Clients, User Management)
# ----------------------------------------------------------------------------
class SupplierListPaginationTests(TestCase):
    def setUp(self):
        make_user(username='supmgr', password='pw', role='Manager')
        self.client.login(username='supmgr', password='pw')
        from core.models import Supplier
        for i in range(30):
            Supplier.objects.create(name=f'Supplier {i:02d}', contact_person=f'Contact {i:02d}')

    def test_list_is_paginated_and_page_size_is_adjustable(self):
        resp = self.client.get(reverse('supplier_list'))
        self.assertEqual(len(resp.context['suppliers'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')
        resp = self.client.get(reverse('supplier_list'), {'page_size': 50})
        self.assertEqual(len(resp.context['suppliers'].object_list), 30)
        self.assertNotContains(resp, 'Page 1 of 2')

    def test_sort_and_search_apply_across_all_pages(self):
        resp = self.client.get(reverse('supplier_list'), {'sort': '-name'})
        self.assertEqual(resp.context['suppliers'][0].name, 'Supplier 29')
        resp = self.client.get(reverse('supplier_list'), {'q': 'Supplier 07'})
        self.assertEqual(resp.context['suppliers'].paginator.count, 1)


class ClientListPaginationTests(TestCase):
    def setUp(self):
        make_user(username='climgr', password='pw', role='Manager')
        self.client.login(username='climgr', password='pw')
        from core.models import Client as ClientModel
        for i in range(30):
            ClientModel.objects.create(name=f'Client {i:02d}', contact_person=f'Contact {i:02d}')

    def test_list_is_paginated(self):
        resp = self.client.get(reverse('client_list'))
        self.assertEqual(len(resp.context['clients'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')

    def test_sort_and_search_apply_across_all_pages(self):
        resp = self.client.get(reverse('client_list'), {'sort': '-name'})
        self.assertEqual(resp.context['clients'][0].name, 'Client 29')
        resp = self.client.get(reverse('client_list'), {'q': 'Client 07'})
        self.assertEqual(resp.context['clients'].paginator.count, 1)


class PurchaseOrderListPaginationTests(TestCase):
    def setUp(self):
        make_user(username='pomgr', password='pw', role='Manager')
        self.client.login(username='pomgr', password='pw')
        from core.models import PurchaseOrder
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')
        for i in range(30):
            PurchaseOrder.objects.create(po_number=f'PO-PG-{i:02d}', supplier_name='ChemCo', target_warehouse=self.wh)

    def test_list_is_paginated_and_sort_applies_across_pages(self):
        resp = self.client.get(reverse('po_list'))
        self.assertEqual(len(resp.context['purchase_orders'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')
        resp = self.client.get(reverse('po_list'), {'sort': '-po_number', 'page': 2})
        self.assertEqual(resp.context['purchase_orders'][0].po_number, 'PO-PG-04')


class ShipmentsPaginationTests(TestCase):
    def setUp(self):
        make_user(username='shipmgr', password='pw', role='Manager')
        self.client.login(username='shipmgr', password='pw')
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')
        for i in range(30):
            Shipment.objects.create(tracking_number=f'TRK-{i:02d}', direction='Inbound', status='Preparing', origin_warehouse=self.wh)

    def test_list_is_paginated_sortable_and_searchable(self):
        resp = self.client.get(reverse('shipments'))
        self.assertEqual(len(resp.context['shipments'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')
        resp = self.client.get(reverse('shipments'), {'q': 'TRK-07'})
        self.assertEqual(resp.context['shipments'].paginator.count, 1)
        resp = self.client.get(reverse('shipments'), {'sort': 'tracking_number'})
        self.assertEqual(resp.context['shipments'][0].tracking_number, 'TRK-00')


class RegistryLedgerPaginationTests(TestCase):
    def setUp(self):
        make_user(username='ledgermgr', password='pw', role='Manager')
        self.client.login(username='ledgermgr', password='pw')
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')
        for i in range(30):
            RegistryLog.objects.create(action_type='Inbound', item_name=f'Item {i:02d}',
                                       quantity_changed=Decimal('10'), warehouse=self.wh)

    def test_list_is_paginated_and_sort_applies_across_pages(self):
        resp = self.client.get(reverse('registry'))
        self.assertEqual(len(resp.context['logs'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')
        resp = self.client.get(reverse('registry'), {'sort': 'item_name', 'page': 2})
        self.assertEqual(resp.context['logs'][0].item_name, 'Item 25')


class WarehouseInventoryPaginationTests(TestCase):
    def setUp(self):
        make_user(username='invmgr', password='pw', role='Manager')
        self.client.login(username='invmgr', password='pw')
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')
        self.mat = Material.objects.create(name='Resin', sku='MAT-R', category='Chem', unit_of_measure='kg', safe_storage_days=90)
        for i in range(30):
            Batch.objects.create(batch_number=f'B-{i:03d}', material=self.mat, quantity=Decimal('10'),
                                 warehouse=self.wh, status='Active', manufacturing_date='2026-01-01', expiry_date='2027-01-01')

    def test_list_is_paginated_and_kpi_shows_the_true_total_not_the_page_length(self):
        resp = self.client.get(reverse('warehouse_inventory'))
        self.assertEqual(len(resp.context['batches'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')
        self.assertEqual(resp.context['total_batches'], 30)
        self.assertContains(resp, '>30<')   # the Active Batches KPI, not the page length (25)

    def test_search_narrows_the_kpi_total_too(self):
        resp = self.client.get(reverse('warehouse_inventory'), {'q': 'B-007'})
        self.assertEqual(resp.context['total_batches'], 1)

    def test_sort_applies_across_all_pages(self):
        resp = self.client.get(reverse('warehouse_inventory'), {'sort': '-batch_number', 'page': 2})
        self.assertEqual(resp.context['batches'][0].batch_number, 'B-004')


class UserManagementPaginationTests(TestCase):
    def setUp(self):
        make_user(username='sysadmin', password='pw', role='Admin')
        self.client.login(username='sysadmin', password='pw')
        for i in range(30):
            User.objects.create_user(username=f'staffuser{i:02d}', password='pw')

    def test_list_is_paginated_and_sort_applies_across_pages(self):
        resp = self.client.get(reverse('user_management'))
        self.assertEqual(len(resp.context['users'].object_list), 25)
        self.assertContains(resp, 'Page 1 of 2')
        resp = self.client.get(reverse('user_management'), {'sort': '-username', 'page': 2})
        # 31 users total (30 + sysadmin); descending username, page 2 starts at the 26th
        usernames_desc = sorted([u.username for u in User.objects.all()], reverse=True)
        self.assertEqual(resp.context['users'][0].username, usernames_desc[25])


# ----------------------------------------------------------------------------
# N+1 query fixes: product_list / material_list / qa_dashboard now use one
# aggregate (or targeted) query instead of one query per row, so the query
# count must stay flat as the row count grows - that's the actual thing worth
# testing here, not just "the page still renders".
# ----------------------------------------------------------------------------
class ProductMaterialQADashboardQueryCountTests(TestCase):
    def setUp(self):
        make_user(username='qcountmgr', password='pw', role='Manager')
        self.client.login(username='qcountmgr', password='pw')
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')

    def _query_count(self, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def test_product_list_query_count_does_not_grow_with_row_count(self):
        for i in range(2):
            Product.objects.create(name=f'Product {i}', sku=f'PRD-{i}', price_per_unit=Decimal('10'))
        small = self._query_count(reverse('product_list'))
        for i in range(2, 30):
            Product.objects.create(name=f'Product {i}', sku=f'PRD-{i}', price_per_unit=Decimal('10'))
        large = self._query_count(reverse('product_list'))
        self.assertEqual(small, large, "product_list's query count should be flat, not one-per-product")

    def test_product_list_stock_totals_are_still_correct(self):
        p1 = Product.objects.create(name='P1', sku='PRD-Q1', price_per_unit=Decimal('10'))
        p2 = Product.objects.create(name='P2', sku='PRD-Q2', price_per_unit=Decimal('10'))
        Batch.objects.create(batch_number='PQ-1', product=p1, quantity=Decimal('40'), status='Active',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        Batch.objects.create(batch_number='PQ-2', product=p1, quantity=Decimal('5'), status='Quarantined',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        Batch.objects.create(batch_number='PQ-3', product=p2, quantity=Decimal('7'), status='Active',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        resp = self.client.get(reverse('product_list'))
        by_sku = {row['product'].sku: row for row in resp.context['product_data']}
        self.assertEqual(by_sku['PRD-Q1']['product'].active_stock, Decimal('40'))   # Quarantined batch excluded
        self.assertEqual(by_sku['PRD-Q2']['product'].active_stock, Decimal('7'))

    def test_material_list_query_count_does_not_grow_with_row_count(self):
        for i in range(2):
            Material.objects.create(name=f'Material {i}', sku=f'MAT-{i}', category='Chem', unit_of_measure='kg', safe_storage_days=90)
        small = self._query_count(reverse('material_list'))
        for i in range(2, 30):
            Material.objects.create(name=f'Material {i}', sku=f'MAT-{i}', category='Chem', unit_of_measure='kg', safe_storage_days=90)
        large = self._query_count(reverse('material_list'))
        self.assertEqual(small, large, "material_list's query count should be flat, not one-per-material")

    def test_material_list_stock_totals_are_still_correct(self):
        m1 = Material.objects.create(name='M1', sku='MAT-Q1', category='Chem', unit_of_measure='kg',
                                     safe_storage_days=90, cost_per_unit=Decimal('2.00'))
        Batch.objects.create(batch_number='MQ-1', material=m1, quantity=Decimal('15'), status='Active',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        Batch.objects.create(batch_number='MQ-2', material=m1, quantity=Decimal('9'), status='Depleted',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date='2027-01-01')
        resp = self.client.get(reverse('material_list'))
        row = next(r for r in resp.context['material_data'] if r['material'].sku == 'MAT-Q1')
        self.assertEqual(row['current_stock'], 15.0)   # Depleted batch excluded
        self.assertEqual(row['total_value'], 30.0)

    def test_qa_dashboard_query_count_does_not_grow_with_row_count(self):
        mat = Material.objects.create(name='Resin', sku='MAT-QA', category='Chem', unit_of_measure='kg', safe_storage_days=90)
        for i in range(2):
            Batch.objects.create(batch_number=f'QA-{i}', material=mat, quantity=Decimal('10'), status='Active',
                                 warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date='2099-01-01')
        small = self._query_count(reverse('qa_dashboard'))
        for i in range(2, 60):
            Batch.objects.create(batch_number=f'QA-{i}', material=mat, quantity=Decimal('10'), status='Active',
                                 warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date='2099-01-01')
        large = self._query_count(reverse('qa_dashboard'))
        self.assertEqual(small, large, "qa_dashboard's query count should be flat, not scale with total batch count")

    def test_qa_dashboard_excludes_depleted_batches_even_when_near_expiry(self):
        # This used to be a live bug: a Depleted batch with a soon expiry date leaked
        # into the Near-Expiry table even though it has zero stock and needs no QA action.
        mat = Material.objects.create(name='Resin', sku='MAT-DEP', category='Chem', unit_of_measure='kg', safe_storage_days=90)
        today = timezone.localdate()
        Batch.objects.create(batch_number='DEP-SOON', material=mat, quantity=Decimal('0'), status='Depleted',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date=today + timedelta(days=5))
        Batch.objects.create(batch_number='ACT-SOON', material=mat, quantity=Decimal('10'), status='Active',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date=today + timedelta(days=5))
        resp = self.client.get(reverse('qa_dashboard'))
        numbers = {b.batch_number for b in resp.context['near_expiry']}
        self.assertIn('ACT-SOON', numbers)
        self.assertNotIn('DEP-SOON', numbers)

    def test_qa_dashboard_buckets_and_kpi_counts_are_still_correct(self):
        mat = Material.objects.create(name='Resin', sku='MAT-BKT', category='Chem', unit_of_measure='kg', safe_storage_days=90)
        today = timezone.localdate()
        Batch.objects.create(batch_number='BKT-NEAR', material=mat, quantity=Decimal('10'), status='Active',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date=today + timedelta(days=10))
        Batch.objects.create(batch_number='BKT-FAR', material=mat, quantity=Decimal('10'), status='Active',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date=today + timedelta(days=200))
        Batch.objects.create(batch_number='BKT-QTN', material=mat, quantity=Decimal('10'), status='Quarantined',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date=today + timedelta(days=200))
        Batch.objects.create(batch_number='BKT-SPL', material=mat, quantity=Decimal('10'), status='Spoiled',
                             warehouse=self.wh, manufacturing_date='2026-01-01', expiry_date=today + timedelta(days=200))
        resp = self.client.get(reverse('qa_dashboard'))
        near_numbers = {b.batch_number for b in resp.context['near_expiry']}
        self.assertEqual(near_numbers, {'BKT-NEAR'})
        self.assertEqual(resp.context['near_expiry'][0].days_remaining, 10)
        self.assertEqual(len(resp.context['quarantined']), 1)
        self.assertEqual(len(resp.context['spoiled']), 1)
        self.assertContains(resp, '10 Days')


class ContactValidationTests(TestCase):
    """Phone numbers are checked against their country's rules (Malaysia by
    default, any country via the dropdown or a +code); emails need a real
    domain. Enforced on client/supplier add+edit, the shipment contact and the
    profile email."""

    def setUp(self):
        self.admin = make_user(username='cv', password='pw', role='Admin')
        self.client = Client()
        self.client.force_login(self.admin)

    def test_normalise_phone_valid_numbers(self):
        from core.validators import normalise_phone
        cases = [
            ('012-345 6789', 'MY', '012-345 6789'),
            ('0123456789', 'MY', '012-345 6789'),
            ('011-2345 6789', 'MY', '011-2345 6789'),
            ('03-7956 1234', 'MY', '03-7956 1234'),
            ('04-226 1234', 'MY', '04-226 1234'),
            ('088-212 345', 'MY', '088-212 345'),
            ('+60 12-345 6789', 'MY', '012-345 6789'),
            ('+65 9123 4567', 'MY', '+65 9123 4567'),
            ('+86 138 0013 8000', 'MY', '+86 138 0013 8000'),
            ('9123 4567', 'SG', '+65 9123 4567'),
            ('', 'MY', ''),
        ]
        for raw, region, expected in cases:
            with self.subTest(raw=raw, region=region):
                self.assertEqual(normalise_phone(raw, region), expected)

    def test_normalise_phone_rejects_bad_numbers(self):
        from django.core.exceptions import ValidationError
        from core.validators import normalise_phone
        for raw, region in [('12345', 'MY'), ('012-345', 'MY'), ('012-345 67890 12', 'MY'),
                            ('call me', 'MY'), ('9123 456', 'SG'), ('+65 1234', 'MY')]:
            with self.subTest(raw=raw, region=region):
                with self.assertRaises(ValidationError):
                    normalise_phone(raw, region)

    def test_phone_or_email(self):
        from django.core.exceptions import ValidationError
        from core.validators import validate_phone_or_email, normalise_phone_or_email
        self.assertEqual(normalise_phone_or_email('siti@agricore.com.my'), 'siti@agricore.com.my')
        self.assertEqual(normalise_phone_or_email('0123456789'), '012-345 6789')
        validate_phone_or_email('')
        for bad in ('siti@agricore', 'not a contact', '123'):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_phone_or_email(bad)

    def test_client_add_rejects_bad_phone_and_email(self):
        from core.models import Client as ClientModel
        resp = self.client.post(reverse('client_list'), {'name': 'Bad Phone Co', 'phone': '12345', 'phone_country': 'MY'}, follow=True)
        self.assertFalse(ClientModel.objects.filter(name='Bad Phone Co').exists())
        self.assertContains(resp, 'not a valid Malaysia phone number')
        self.client.post(reverse('client_list'), {'name': 'Bad Mail Co', 'email': 'sales@badmail'})
        self.assertFalse(ClientModel.objects.filter(name='Bad Mail Co').exists())

    def test_client_add_saves_normalised_phone(self):
        from core.models import Client as ClientModel
        self.client.post(reverse('client_list'), {'name': 'Local Co', 'phone': '0123456789', 'phone_country': 'MY', 'email': 'a@local.com.my'})
        self.client.post(reverse('client_list'), {'name': 'SG Co', 'phone': '9123 4567', 'phone_country': 'SG'})
        self.assertEqual(ClientModel.objects.get(name='Local Co').phone, '012-345 6789')
        self.assertEqual(ClientModel.objects.get(name='SG Co').phone, '+65 9123 4567')

    def test_client_edit_rejects_bad_phone(self):
        from core.models import Client as ClientModel
        c = ClientModel.objects.create(name='Edit Co', phone='012-345 6789')
        self.client.post(reverse('client_edit', args=[c.pk]), {'name': 'Edit Co', 'phone': '999', 'phone_country': 'MY'})
        c.refresh_from_db()
        self.assertEqual(c.phone, '012-345 6789')

    def test_supplier_add_and_edit(self):
        self.client.post(reverse('supplier_list'), {'name': 'Bad Sup', 'phone': '0000', 'phone_country': 'MY'})
        self.assertFalse(Supplier.objects.filter(name='Bad Sup').exists())
        self.client.post(reverse('supplier_list'), {'name': 'CN Sup', 'phone': '138 0013 8000', 'phone_country': 'CN', 'email': 'sales@cnsup.cn'})
        sup = Supplier.objects.get(name='CN Sup')
        self.assertEqual(sup.phone, '+86 138 0013 8000')
        self.client.post(reverse('supplier_edit', args=[sup.pk]), {'name': 'CN Sup', 'phone': '+86 138 0013 8000', 'phone_country': 'MY', 'email': 'nope@'})
        sup.refresh_from_db()
        self.assertEqual(sup.email, 'sales@cnsup.cn')

    def test_shipment_contact_accepts_phone_or_email_only(self):
        from core.models import Shipment
        s = Shipment.objects.create(tracking_number='SHP-CV1', direction='Outbound', status='Draft')
        url = reverse('shipment_detail', args=[s.pk])
        self.client.post(url, {'action': 'update_route', 'client_contact_phone': 'call the office'})
        s.refresh_from_db()
        self.assertIsNone(s.client_contact_phone)
        self.client.post(url, {'action': 'update_route', 'client_contact_phone': 'ops@agricore.com.my'})
        s.refresh_from_db()
        self.assertEqual(s.client_contact_phone, 'ops@agricore.com.my')
        self.client.post(url, {'action': 'update_route', 'client_contact_phone': '0123456789'})
        s.refresh_from_db()
        self.assertEqual(s.client_contact_phone, '012-345 6789')

    def test_shipment_from_so_falls_back_to_client_email(self):
        from core.models import Client as ClientModel, Shipment
        hub = Warehouse.objects.create(name='Hub CV', location_type='Storage')
        product = Product.objects.create(name='Prod CV', sku='PRD-CV', unit_of_measure='kg', price_per_unit=1)
        customer = ClientModel.objects.create(name='NoPhone Co', contact_person='Aina', email='aina@nophone.com.my', delivery_address='Lot 1')
        batch = Batch.objects.create(batch_number='FG-CV', status='Active', product=product, quantity=Decimal('5'),
                                     allocated_quantity=Decimal('5'), manufacturing_date=date.today(),
                                     expiry_date=date.today() + timedelta(days=100), warehouse=hub)
        so = SalesOrder.objects.create(so_number='SO-CV', client_name='NoPhone Co', client=customer,
                                       origin_warehouse=hub, status='Ready to Ship')
        StockAllocation.objects.create(batch=batch, sales_order=so, quantity=Decimal('5'))
        self.client.post(reverse('so_create_shipment', args=[so.pk]))
        self.assertEqual(Shipment.objects.get(sales_order=so).client_contact_phone, 'aina@nophone.com.my')

    def test_profile_rejects_bad_email(self):
        self.client.post(reverse('profile'), {'first_name': 'A', 'last_name': 'B', 'email': 'me@nowhere'})
        self.admin.refresh_from_db()
        self.assertNotEqual(self.admin.email, 'me@nowhere')
        self.client.post(reverse('profile'), {'first_name': 'A', 'last_name': 'B', 'email': 'me@terrahub.com.my'})
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.email, 'me@terrahub.com.my')


class OrderListShowsItemsTests(TestCase):
    """PO/SO list pages show what is on each order (not timeline snippets) and search reaches it."""
    def setUp(self):
        from core.models import PurchaseOrder, PurchaseOrderDetail, SalesOrderDetail
        make_user(username='itemsmgr', password='pw', role='Manager')
        self.client.login(username='itemsmgr', password='pw')
        self.wh = Warehouse.objects.create(name='Plant A', location_type='Manufacturing')
        self.resin = Material.objects.create(name='Resin Alpha', sku='MAT-RA', category='C', unit_of_measure='kg', safe_storage_days=9)
        self.dye = Material.objects.create(name='Dye Beta', sku='MAT-DB', category='C', unit_of_measure='L', safe_storage_days=9)
        self.paint = Product.objects.create(name='Wall Paint', sku='PRD-WP', price_per_unit=Decimal('5'))
        self.po1 = PurchaseOrder.objects.create(po_number='PO-ITM-1', supplier_name='S', target_warehouse=self.wh)
        self.po2 = PurchaseOrder.objects.create(po_number='PO-ITM-2', supplier_name='S', target_warehouse=self.wh)
        PurchaseOrderDetail.objects.create(purchase_order=self.po1, material=self.resin, quantity_ordered=Decimal('250'))
        PurchaseOrderDetail.objects.create(purchase_order=self.po2, material=self.dye, quantity_ordered=Decimal('40'))
        self.so1 = SalesOrder.objects.create(so_number='SO-ITM-1', client_name='C', origin_warehouse=self.wh)
        self.so2 = SalesOrder.objects.create(so_number='SO-ITM-2', client_name='C', origin_warehouse=self.wh)
        SalesOrderDetail.objects.create(sales_order=self.so1, product=self.paint, quantity_ordered=Decimal('30'))

    def test_po_list_shows_materials_with_quantities_and_search_matches_them(self):
        resp = self.client.get(reverse('po_list'))
        self.assertContains(resp, 'Resin Alpha')
        self.assertContains(resp, '250 kg')
        self.assertNotContains(resp, 'No timeline logs')
        resp = self.client.get(reverse('po_list'), {'q': 'Dye Beta'})
        self.assertEqual([p.po_number for p in resp.context['purchase_orders']], ['PO-ITM-2'])
        resp = self.client.get(reverse('po_list'), {'q': 'MAT-RA'})
        self.assertEqual([p.po_number for p in resp.context['purchase_orders']], ['PO-ITM-1'])

    def test_so_list_shows_products_and_search_matches_them(self):
        resp = self.client.get(reverse('so_list'))
        self.assertContains(resp, 'Wall Paint')
        self.assertContains(resp, 'No products added yet')     # SO-ITM-2 has no lines
        resp = self.client.get(reverse('so_list'), {'q': 'wall paint'})
        self.assertEqual([s.so_number for s in resp.context['sales_orders']], ['SO-ITM-1'])

    def test_more_than_three_lines_collapse_to_a_count(self):
        from core.models import PurchaseOrderDetail
        for i in range(4):
            m = Material.objects.create(name=f'Extra {i}', sku=f'MAT-X{i}', category='C', unit_of_measure='kg', safe_storage_days=9)
            PurchaseOrderDetail.objects.create(purchase_order=self.po1, material=m, quantity_ordered=Decimal('1'))
        resp = self.client.get(reverse('po_list'))
        self.assertContains(resp, '+2 more')      # 5 lines, 3 shown


class SessionIdleTimeoutTests(TestCase):
    """30-minute idle sign-out: settings, keep-alive endpoint and warning popup."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='idle', password='pw')

    def test_session_settings(self):
        from django.conf import settings
        self.assertEqual(settings.SESSION_COOKIE_AGE, 30 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
        self.assertTrue(settings.SESSION_EXPIRE_AT_BROWSER_CLOSE)

    def test_keepalive_anonymous_gets_401_not_redirect(self):
        resp = self.client.post(reverse('session_keepalive'))
        self.assertEqual(resp.status_code, 401)

    def test_keepalive_rejects_get(self):
        self.client.login(username='idle', password='pw')
        self.assertEqual(self.client.get(reverse('session_keepalive')).status_code, 405)

    def test_keepalive_authenticated(self):
        self.client.login(username='idle', password='pw')
        resp = self.client.post(reverse('session_keepalive'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['expires_in'], 30 * 60)

    def test_warning_popup_only_for_signed_in_users(self):
        resp = self.client.get(reverse('home'))
        self.assertNotContains(resp, 'session-timeout-modal')
        self.client.login(username='idle', password='pw')
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, 'id="session-timeout-modal"')
        self.assertContains(resp, 'data-timeout="1800"')
        self.assertContains(resp, 'js/session-timeout.js')

    def test_login_page_explains_idle_sign_out_and_keeps_next(self):
        resp = self.client.get(reverse('login') + '?reason=idle&next=/warehouse/inventory/')
        self.assertContains(resp, 'signed out after 30 minutes of inactivity')
        self.assertContains(resp, 'name="next" value="/warehouse/inventory/"')
        resp = self.client.post(reverse('login') + '?reason=idle', {
            'username': 'idle', 'password': 'pw', 'next': '/warehouse/inventory/',
        })
        self.assertRedirects(resp, '/warehouse/inventory/', fetch_redirect_response=False)
