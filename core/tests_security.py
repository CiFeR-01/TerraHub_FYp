"""Security hardening tests (review items 4-9): settings guard, login throttling, deny-by-default actions,
public batch page and the notifications redirect.

Run on its own with:  python manage.py test core.tests_security
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core.permissions import ACTION_PERMS, action_denied
from core.tests import make_user
from core.tests_critical import CriticalBase

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'login-throttle-tests'}}


class SettingsGuardTests(TestCase):
    def test_production_refuses_to_start_without_secret_key(self):
        env = {**os.environ, 'DEBUG': 'False', 'SECRET_KEY': ''}
        result = subprocess.run([sys.executable, '-c', 'import TerraHub.settings'], cwd=settings.BASE_DIR,
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SECRET_KEY must be set', result.stderr)


@override_settings(CACHES=LOCMEM, LOGIN_FAIL_LIMIT=3, LOGIN_LOCKOUT_SECONDS=900)
class LoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = make_user(username='victim', password='right-pw')

    def post_login(self, password, ip='10.0.0.1'):
        return self.client.post(reverse('login'), {'username': 'victim', 'password': password}, REMOTE_ADDR=ip)

    def test_locks_after_repeated_failures_even_with_right_password(self):
        for _ in range(3):
            self.post_login('wrong')
        response = self.post_login('right-pw')
        self.assertContains(response, 'Too many failed sign-in attempts')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_lock_also_applies_to_admin_login(self):
        self.user.is_staff = True
        self.user.save()
        for _ in range(3):
            self.post_login('wrong')
        response = self.client.post('/admin/login/', {'username': 'victim', 'password': 'right-pw'},
                                    REMOTE_ADDR='10.0.0.1')
        self.assertEqual(response.status_code, 200)  # form shown again, not a redirect into admin
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_other_ip_can_still_sign_in_until_username_cap(self):
        for _ in range(3):
            self.post_login('wrong', ip='10.0.0.1')
        response = self.post_login('right-pw', ip='10.0.0.2')
        self.assertEqual(response.status_code, 302)

    def test_username_cap_applies_across_ips(self):
        for i in range(12):  # 4x the limit, spread over many addresses
            self.post_login('wrong', ip=f'10.0.1.{i}')
        response = self.post_login('right-pw', ip='10.0.9.9')
        self.assertContains(response, 'Too many failed sign-in attempts')

    def test_success_clears_failures(self):
        for _ in range(2):
            self.post_login('wrong')
        self.assertEqual(self.post_login('right-pw').status_code, 302)
        self.client.logout()
        for _ in range(2):
            self.post_login('wrong')
        self.assertEqual(self.post_login('right-pw').status_code, 302)


class DenyByDefaultActionTests(CriticalBase):
    def post(self, page, action):
        request = RequestFactory().post('/', {'action': action})
        request.user = self.admin
        return action_denied(request, page)

    def test_unlisted_action_is_refused(self):
        self.assertIsNotNone(self.post('so_detail', 'some_new_action'))

    def test_listed_actions_still_allowed(self):
        self.assertIsNone(self.post('so_detail', 'add_so_item'))
        self.assertIsNone(self.post('shipment_detail', 'approve'))  # checked inside the view

    def test_posts_without_action_are_untouched(self):
        request = RequestFactory().post('/', {})
        request.user = self.admin
        self.assertIsNone(action_denied(request, 'so_detail'))

    def test_every_action_a_view_handles_is_listed(self):
        """Guards the deny-by-default list: a new `action == '...'` branch must be added to ACTION_PERMS."""
        src = (Path(settings.BASE_DIR) / 'core' / 'views.py').read_text(encoding='utf-8')
        missing = {}
        for node in ast.parse(src).body:
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.get_source_segment(src, node)
            page = re.search(r"action_denied\(request, '(\w+)'\)", body)
            if not page:
                continue
            handled = set()
            for cmp in ast.walk(node):
                if isinstance(cmp, ast.Compare) and 'action' in ast.get_source_segment(src, cmp.left):
                    for c in cmp.comparators:
                        elts = c.elts if isinstance(c, (ast.Tuple, ast.List, ast.Set)) else [c]
                        handled |= {e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            unlisted = handled - set(ACTION_PERMS.get(page.group(1), {}))
            if unlisted:
                missing[page.group(1)] = sorted(unlisted)
        self.assertEqual(missing, {})

    def test_own_check_actions_are_not_hidden(self):
        viewer = make_user(username='sec_viewer', password='pw')
        self.as_user(viewer)
        so, _ = self.so(1)
        response = self.client.get(reverse('so_detail', args=[so.pk]))
        self.assertNotIn('add_follower', response.context['denied_actions'])


class PublicBatchPageTests(CriticalBase):
    def setUp(self):
        super().setUp()
        po, _ = self.po(5, status='Received')
        po.supplier_name = 'Secret Supplier Sdn Bhd'
        po.save()
        self.lot = self.batch('BATCH-SEC-1', 5, material=True)
        self.lot.purchase_order = po
        self.lot.location = 'Aisle 7 Bin 3'
        self.lot.save()
        self.url = reverse('batch_public_info', args=[self.lot.batch_number])

    def test_anonymous_sees_no_supplier_or_bin(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Secret Supplier')
        self.assertNotContains(response, 'Aisle 7 Bin 3')
        self.assertContains(response, 'CR Store')

    def test_signed_in_staff_still_see_everything(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'Secret Supplier')
        self.assertContains(response, 'Aisle 7 Bin 3')


class NotificationRedirectTests(CriticalBase):
    def test_external_referer_is_not_followed(self):
        response = self.client.post(reverse('mark_notifications_read'), HTTP_REFERER='https://evil.example/phish')
        self.assertEqual(response.url, reverse('home'))

    def test_same_site_referer_is_followed(self):
        response = self.client.post(reverse('mark_notifications_read'), HTTP_REFERER='http://testserver/dashboard/')
        self.assertEqual(response.url, 'http://testserver/dashboard/')
