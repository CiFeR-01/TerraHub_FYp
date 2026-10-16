"""Tests for the AI Copilot briefing hub (assistant/briefing/hub/).

Kept apart from core/tests.py on purpose; run with
`python manage.py test core.tests_briefing_hub`.
"""
import os
from unittest import mock

from django.test import TestCase, Client
from django.urls import reverse

from core.models import OpsBriefing
from core import briefing as briefing_mod
from core.tests import make_user, _FakeAnthropicModule


def _fake_signals(category, **kwargs):
    return {"category": category, "signal_count": 1, "sections": {}}


class BriefingHubTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse('briefing_hub')
        self.plain = make_user(username='hubplain', password='pw')
        self.manager = make_user(username='hubmgr', password='pw', role='Manager')

    def _ajax_post(self, data):
        return self.client.post(self.url, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_login_required(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_plain_user_sees_six_cards_without_generate_all(self):
        self.client.login(username='hubplain', password='pw')
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        for label in ('Materials', 'Products', 'Sales', 'Purchase', 'Logistics', 'Warehouse'):
            self.assertContains(resp, label)
        self.assertNotContains(resp, 'Generate all six')

    def test_manager_sees_generate_all(self):
        self.client.login(username='hubmgr', password='pw')
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Generate all six')

    def test_failed_run_after_good_one_shows_stale(self):
        OpsBriefing.objects.create(category='sales', status='ok', headline='Sales fine', signal_count=1)
        OpsBriefing.objects.create(category='sales', status='error', error_detail='Boom from API')
        self.client.login(username='hubplain', password='pw')
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Last run failed')
        self.assertContains(resp, 'Boom from API')
        self.assertContains(resp, 'Sales fine')

    def test_empty_run_shows_all_clear(self):
        OpsBriefing.objects.create(category='warehouse', status='empty', headline='All clear')
        self.client.login(username='hubplain', password='pw')
        resp = self.client.get(self.url)
        self.assertContains(resp, 'All clear')

    def test_plain_user_cannot_generate_category(self):
        self.client.login(username='hubplain', password='pw')
        resp = self._ajax_post({'category': 'sales'})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(OpsBriefing.objects.filter(category='sales').exists())

    def test_manager_generates_category_as_json(self):
        self.client.login(username='hubmgr', password='pw')
        with mock.patch.object(briefing_mod, 'anthropic', _FakeAnthropicModule()), \
                mock.patch.object(briefing_mod, 'collect_signals', _fake_signals), \
                mock.patch.object(briefing_mod, 'render_prompt', lambda s, period='daily': 'x'), \
                mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'k'}):
            resp = self._ajax_post({'category': 'sales', 'period': 'daily'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['category'], 'sales')
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(OpsBriefing.objects.filter(category='sales', status='ok').exists())

    def test_unknown_category_rejected(self):
        self.client.login(username='hubmgr', password='pw')
        resp = self._ajax_post({'category': 'nonsense'})
        self.assertEqual(resp.status_code, 400)

    def test_any_user_can_generate_own_checklist(self):
        self.client.login(username='hubplain', password='pw')
        resp = self._ajax_post({'category': 'my_checklist'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(OpsBriefing.objects.filter(category='my_checklist', generated_by=self.plain).exists())

    def test_plain_form_post_redirects_back_to_hub(self):
        self.client.login(username='hubplain', password='pw')
        resp = self.client.post(self.url, {'category': 'my_checklist'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith(self.url))

    def test_sidebar_links_to_hub(self):
        self.client.login(username='hubplain', password='pw')
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, self.url)

    def test_bogus_open_param_ignored(self):
        self.client.login(username='hubplain', password='pw')
        resp = self.client.get(self.url + '?open=<script>')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '<script>"')
        self.assertEqual(resp.context['open_key'], '')
