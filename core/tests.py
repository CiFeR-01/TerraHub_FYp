from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from core.db_tracker import get_db_status, DB_QUERY_LOGS
from django.db import connection

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



