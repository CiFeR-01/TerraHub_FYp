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

