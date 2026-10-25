"""Critical-path tests: a small, fast suite covering the business rules that would hurt most if broken.

Run on its own with:  python manage.py test core.tests_critical
A test belongs here only if breaking it would lose or invent stock or money, let someone skip an approval or
permission, break a one-way status rule, or open a security hole. Each one is a copy of a test in core/tests.py.
The full suite in core/tests.py is the complete safety net; CriticalBase lives there.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Batch, Material, Product, ProductionRun, ProductRecipe, PurchaseOrder, PurchaseOrderDetail,
    RegistryLog, SalesOrder, SalesOrderDetail, Shipment, ShipmentItem, StockAllocation, StockAudit, Warehouse,
)
import ast
import io
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings

from core import qa as qa_logic
from core.models import QAApproval, QAEvent, QARequest
from core.permissions import ACTION_PERMS
from core.tests import LOCMEM, AdminEditBase, CriticalBase, _core_perm, make_user
from core.utils import allocate_stock, apply_po_material_receipt, deduct_stock_from_allocation

D = Decimal


class EndToEndFlowTests(CriticalBase):
    """The three main journeys, driven through the real views."""

    def test_sales_order_from_stock_to_delivery(self):
        stock = self.batch('CR-FG', '30')
        so, line = self.so('10')
        # Draft -> approval by a manager -> Pending
        self.as_user(self.manager2)
        self.post('so_detail', so.pk, action='request_approval', manager_id=self.manager.id)
        self.refresh(so)
        self.assertEqual(so.status, 'Pending Approval')
        self.assertEqual(self.decide(self.manager, 'sales_order', so), 'Pending')
        # Reserve stock -> Ready to Ship
        self.as_user(self.admin)
        self.allocate_so(so, line, stock, '10')
        self.refresh(so, stock)
        self.assertEqual((so.status, stock.allocated_quantity), ('Ready to Ship', D('10')))
        # Draft the delivery, route it through logistics and approval
        self.post('so_create_shipment', so.pk)
        s = Shipment.objects.get(sales_order=so, direction='Outbound')
        Shipment.objects.filter(pk=s.pk).update(client_address='Addr', client_contact_name='Bob', client_contact_phone='0123',
                                                external_tracking_id='TRK', departure_datetime=timezone.now())
        self.post('shipment_detail', s.pk, action='submit_to_logistics')
        self.post('shipment_detail', s.pk, action='submit_for_approval', approve_now='1')
        self.refresh(s)
        self.assertEqual(s.status, 'Preparing')
        # Dispatch credits the order; arrival + receipt delivers it and takes the stock out
        self.post('shipment_detail', s.pk, action='update_operational_status', status='Dispatched')
        self.refresh(so, line)
        self.assertEqual((so.status, line.quantity_shipped), ('Shipped', D('10')))
        self.post('shipment_detail', s.pk, action='update_operational_status', status='Arrived')
        self.post('shipment_detail', s.pk, action='receive_all')
        self.refresh(s, so, stock)
        self.assertEqual(s.status, 'Completed')
        self.assertEqual(so.status, 'Delivered')
        self.assertEqual((stock.quantity, stock.allocated_quantity), (D('20'), D('0')))

    def test_sales_order_through_manufacturing(self):
        mat = self.batch('CR-MAT', '100', wh=self.plant, material=True)
        so, line = self.so('10', status='Pending')
        self.post('so_detail', so.pk, action='send_to_manufacturing', manufacturing_plant_id=self.plant.id)
        run = ProductionRun.objects.get(sales_order=so)
        self.assertEqual((run.status, run.expected_yield), ('Pending Approval', D('10')))
        # Approve, allocate materials at the plant, start, complete
        self.assertEqual(self.decide(self.manager, 'production_run', run), 'Pending Allocation')
        self.as_user(self.admin)
        self.post('production_run_allocate', run.pk, action='allocate_run', **{f'batch_qty_{mat.pk}': '20'})
        self.refresh(run, mat)
        self.assertEqual((run.status, mat.allocated_quantity), ('Planned', D('20')))
        self.post('production_run_detail', run.pk, action='start_production')
        self.refresh(run)
        self.assertEqual(run.status, 'InProgress')
        self.post('production_run_detail', run.pk, action='complete_production', actual_yield='10',
                  **{f'actual_qty_{self.material.id}': '20'})
        self.refresh(run, mat, so)
        self.assertEqual(run.status, 'Completed')
        self.assertEqual((mat.quantity, mat.allocated_quantity), (D('80'), D('0')))   # materials consumed
        fg = Batch.objects.get(produced_in=run)
        self.assertEqual((fg.quantity, fg.allocated_quantity, fg.warehouse), (D('10'), D('10'), self.plant))
        self.assertEqual(so.status, 'Ready to Ship')

    def test_purchase_order_approval_and_receipt(self):
        po, line = self.po('50')
        self.as_user(self.manager2)
        self.post('po_detail', po.pk, action='request_approval', manager_id=self.manager.id)
        self.assertEqual(self.decide(self.manager, 'purchase_order', po), 'Pending')
        self.as_user(self.admin)
        self.post('po_detail', po.pk, action='mark_received', item_id=line.id, qty_received='50')
        self.refresh(po, line)
        self.assertEqual((po.status, line.quantity_received), ('Completed', D('50')))
        received = Batch.objects.get(purchase_order=po)
        self.assertEqual((received.material, received.quantity, received.warehouse), (self.material, D('50'), self.store))


class ApprovalRuleTests(CriticalBase):
    """Nothing moves on an unapproved order, and approval can't be self-granted or faked."""

    def test_unapproved_sales_order_cannot_reserve_stock(self):
        stock = self.batch('CR-UA', '20')
        for status in ('Draft', 'Pending Approval', 'Rejected'):
            so, line = self.so('10', status=status)
            self.allocate_so(so, line, stock, '10')
            self.refresh(so)
            self.assertEqual(so.status, status)
            self.assertFalse(StockAllocation.objects.filter(sales_order=so).exists())

    def test_unapproved_purchase_order_cannot_be_received(self):
        for status in ('Draft', 'Pending Approval', 'Rejected'):
            po, line = self.po('10', status=status)
            self.post('po_detail', po.pk, action='mark_received', item_id=line.id, qty_received='10')
            self.refresh(line)
            self.assertEqual(line.quantity_received, 0)
        self.assertFalse(Batch.objects.filter(material=self.material).exists())

    def test_po_status_dropdown_cannot_approve(self):
        po, _ = self.po('10')
        for status in ('Pending', 'Completed'):
            self.post('po_detail', po.pk, action='update_po_status', status=status)
        self.refresh(po)
        self.assertEqual((po.status, po.approved_by), ('Draft', None))

    def test_unapproved_order_cannot_raise_a_production_run(self):
        so, _ = self.so('5', status='Draft')
        self.post('so_detail', so.pk, action='send_to_manufacturing')
        self.assertFalse(ProductionRun.objects.filter(sales_order=so).exists())

    def test_only_the_assigned_approver_decides(self):
        so, _ = self.so('5', status='Pending Approval')
        SalesOrder.objects.filter(pk=so.pk).update(assigned_to=self.manager)
        self.assertEqual(self.decide(self.manager2, 'sales_order', so), 'Pending Approval')
        self.assertEqual(self.decide(self.manager, 'sales_order', so), 'Pending')

    def test_editing_an_approved_order_needs_reapproval(self):
        so, _ = self.so('10', status='Pending')
        SalesOrder.objects.filter(pk=so.pk).update(approved_by=self.manager)
        self.post('so_detail', so.pk, action='add_so_item', product_id=self.product.id, quantity_ordered='5')
        self.refresh(so)
        self.assertEqual((so.status, so.approved_by), ('Draft', None))


class PermissionTests(CriticalBase):
    """Roles come from Group permissions and are checked on the server."""

    def _set_roles(self, target, *names):
        self.post('user_management', action='update_user', user_id=target.id, is_active='on',
                  roles=[Group.objects.get(name=n).id for n in names])
        return set(target.groups.values_list('name', flat=True))

    def test_manager_cannot_grant_admin(self):
        self.as_user(self.manager)
        self.assertEqual(self._set_roles(self.manager, 'Manager', 'Admin'), {'Manager'})
        self.assertEqual(self._set_roles(self.admin), {'Admin'})

    def test_manager_cannot_deactivate_an_admin(self):
        self.as_user(self.manager)
        self.post('user_management', action='update_user', user_id=self.admin.id,
                  roles=list(self.admin.groups.values_list('id', flat=True)))
        self.refresh(self.admin)
        self.assertTrue(self.admin.is_active)

    def test_viewer_cannot_create_orders_or_spoil_stock(self):
        stock = self.batch('CR-VW', '10')
        self.as_user(make_user(username='cr_viewer', password='pw', role='Staff (Viewer)'))
        self.post('so_list', action='create_so', so_number_auto='1', client_name='C', origin_warehouse_id=self.store.id)
        self.post('qa_dashboard', action='spoil_dispose', reason='expired', batch_id=stock.id)
        self.refresh(stock)
        self.assertFalse(SalesOrder.objects.exists())
        self.assertEqual(stock.status, 'Active')

    def test_only_manufacturing_roles_start_runs(self):
        run = ProductionRun.objects.create(run_number='RUN-CR-P', target_product=self.product, expected_yield=D('5'), status='Planned')
        self.as_user(make_user(username='cr_sales', password='pw', role='Sales'))
        self.post('production_run_detail', run.pk, action='start_production')
        self.refresh(run)
        self.assertEqual(run.status, 'Planned')
        self.as_user(make_user(username='cr_mfg', password='pw', role='Manufacturing'))
        self.post('production_run_detail', run.pk, action='start_production')
        self.refresh(run)
        self.assertEqual(run.status, 'InProgress')

    def test_facility_limits_hide_and_lock_other_sites(self):
        other = self.batch('CR-OTHER', '10', wh=self.plant)
        staff = make_user(username='cr_wh', password='pw', role='Warehouse')
        staff.allowed_locations.set([self.store])
        self.as_user(staff)
        resp = self.client.get(reverse('warehouse_inventory'))
        self.assertNotIn('CR-OTHER', [b.batch_number for b in resp.context['batches']])
        self.post('stock_audit', action='create', batch_id=other.id, actual_quantity='8')
        self.assertFalse(StockAudit.objects.exists())


class StockAccuracyTests(CriticalBase):
    """Reservations, FEFO and receipts keep on-hand and reserved quantities right."""

    def test_reserve_then_deduct_moves_the_right_amounts(self):
        stock = self.batch('CR-RD', '50')
        so, _ = self.so('30')
        allocate_stock('sales_order', so, self.product, 30)
        self.refresh(stock)
        self.assertEqual(stock.allocated_quantity, D('30'))
        deduct_stock_from_allocation('sales_order', so)
        self.refresh(stock)
        self.assertEqual((stock.quantity, stock.allocated_quantity), (D('20'), D('0')))

    def test_fefo_skip_needs_a_reason(self):
        older = self.batch('CR-OLD', '10', expires_in=30)
        newer = self.batch('CR-NEW', '10', expires_in=90)
        so, line = self.so('10', status='Pending')
        self.allocate_so(so, line, newer, '10')
        self.assertFalse(StockAllocation.objects.filter(sales_order=so).exists())
        self.allocate_so(so, line, newer, '10', override_reason='Client wants later expiry')
        self.assertEqual(StockAllocation.objects.get(sales_order=so).batch, newer)
        self.refresh(older)
        self.assertEqual(older.allocated_quantity, D('0'))

    def test_cannot_reserve_more_than_the_order_needs(self):
        stock = self.batch('CR-OVER', '50')
        so, line = self.so('10', status='Pending')
        self.allocate_so(so, line, stock, '20', override_reason='x')
        self.assertFalse(StockAllocation.objects.filter(sales_order=so).exists())

    def test_expired_stock_is_never_picked(self):
        expired = self.batch('CR-EXP', '10', expires_in=-1)
        fresh = self.batch('CR-FRESH', '10')
        so, line = self.so('5', status='Pending')
        self.allocate_so(so, line, expired, '5', override_reason='x')
        self.assertFalse(StockAllocation.objects.filter(batch=expired).exists())
        allocate_stock('sales_order', so, self.product, 5)
        self.assertEqual(StockAllocation.objects.get(sales_order=so).batch, fresh)

    def test_po_receipt_rejects_over_receipt_and_junk(self):
        po, line = self.po('100', status='Pending')
        for bad in ('250', '-5', 'abc'):
            self.post('po_detail', po.pk, action='mark_received', item_id=line.id, qty_received=bad)
        self.refresh(line)
        self.assertEqual(line.quantity_received, D('0'))
        self.assertFalse(Batch.objects.filter(purchase_order=po).exists())

    def test_lowering_a_receipt_removes_the_stock_but_not_below_reservations(self):
        po, line = self.po('100', status='Pending')
        receive = lambda q: self.post('po_detail', po.pk, action='mark_received', item_id=line.id, qty_received=q)
        receive('100')
        receive('60')
        received = Batch.objects.get(purchase_order=po)
        self.refresh(po)
        self.assertEqual((received.quantity, po.status), (D('60'), 'Partially Received'))
        Batch.objects.filter(pk=received.pk).update(allocated_quantity=D('50'))
        receive('40')
        self.refresh(received)
        self.assertEqual(received.quantity, D('60'))

    def test_short_stock_count_trims_reservations(self):
        stock = self.batch('CR-AUD', '40', allocated='40')
        so, _ = self.so('40', status='Ready to Ship')
        StockAllocation.objects.create(batch=stock, sales_order=so, quantity=D('40'))
        audit = StockAudit.objects.create(batch=stock, expected_quantity=D('40'), actual_quantity=D('20'))
        self.post('stock_audit', action='resolve', audit_id=audit.id)
        self.refresh(stock, so)
        self.assertEqual((stock.quantity, stock.allocated_quantity), (D('20'), D('20')))
        self.assertEqual(so.status, 'Pending')

    def test_spoiling_a_reserved_batch_releases_the_order(self):
        stock = self.batch('CR-SPOIL', '10', allocated='10')
        so, _ = self.so('10', status='Ready to Ship')
        StockAllocation.objects.create(batch=stock, sales_order=so, quantity=D('10'))
        self.post('qa_dashboard', action='spoil_dispose', reason='expired', batch_id=stock.id)
        self.refresh(stock, so)
        self.assertEqual((stock.status, stock.allocated_quantity, so.status), ('Spoiled', D('0'), 'Pending'))
        self.assertTrue(RegistryLog.objects.filter(action_type='Spoiled_Disposal').exists())


class DeliveryTests(CriticalBase):
    """Outbound shipments: dispatch can't be skipped and orders are only Delivered when everything arrived."""

    def test_partial_delivery_does_not_complete_the_order(self):
        so, line = self.so('20', status='Pending')
        s, _ = self.outbound(so, self.batch('CR-PART', '5', allocated='5'), '5')
        self.post('shipment_detail', s.pk, action='receive_all')
        self.refresh(so, line)
        self.assertEqual((line.quantity_shipped, so.status), (D('5'), 'Partially Shipped'))

    def test_order_cannot_be_marked_delivered_by_hand_early(self):
        so, _ = self.so('10', status='Ready to Ship')
        self.post('so_detail', so.pk, action='update_so_status', status='Delivered')
        self.refresh(so)
        self.assertEqual(so.status, 'Ready to Ship')

    def test_outbound_cannot_skip_dispatch(self):
        so, _ = self.so('4', status='Ready to Ship')
        s, _ = self.outbound(so, self.batch('CR-SKIP', '4', allocated='4'), '4', status='Preparing')
        for target in ('Arrived', 'Delayed'):
            self.post('shipment_detail', s.pk, action='update_operational_status', status=target)
        self.refresh(s)
        self.assertEqual(s.status, 'Preparing')

    def test_dispatched_goods_cannot_be_scrapped_or_pulled_back(self):
        so, _ = self.so('5', status='Shipped')
        stock = self.batch('CR-GONE', '5', allocated='5')
        s, _ = self.outbound(so, stock, '5', status='Dispatched', credited_to_so=True)
        for action in ('scrap_shipment', 'return_to_manufacturing', 'reopen_shipment'):
            self.post('shipment_detail', s.pk, action=action)
        self.refresh(s, stock)
        self.assertEqual((s.status, stock.allocated_quantity), ('Dispatched', D('5')))

    def test_scrapping_an_undispatched_delivery_gives_stock_back_to_the_order(self):
        so, _ = self.so('5', status='Ready to Ship')
        stock = self.batch('CR-BACK', '5', allocated='5')
        s, _ = self.outbound(so, stock, '5', status='Preparing')
        self.post('shipment_detail', s.pk, action='scrap_shipment')
        self.refresh(s, so, stock)
        self.assertEqual((s.status, so.status, stock.allocated_quantity), ('Cancelled', 'Ready to Ship', D('5')))
        self.assertEqual(StockAllocation.objects.get(batch=stock).sales_order, so)

    def test_force_close_needs_a_shortage_reason_and_credits_only_what_arrived(self):
        so, line = self.so('10', status='Shipped')
        SalesOrderDetail.objects.filter(pk=line.pk).update(quantity_shipped=D('10'))
        stock = self.batch('CR-FC', '10', allocated='10')
        s, item = self.outbound(so, stock, '10', status='Discrepant', credited_to_so=True, assigned_manager=self.admin)
        ShipmentItem.objects.filter(pk=item.pk).update(received_quantity=D('9'))
        self.post('shipment_detail', s.pk, action='force_close_shipment', manager_comment='one damaged')
        self.refresh(s)
        self.assertEqual(s.status, 'Discrepant')
        self.post('shipment_detail', s.pk, action='force_close_shipment', manager_comment='one damaged',
                  **{f'shortage_reason_{item.id}': 'origin'})
        self.refresh(so, line, stock)
        self.assertEqual((line.quantity_shipped, so.status), (D('9'), 'Partially Shipped'))
        self.assertEqual((stock.quantity, stock.allocated_quantity), (D('1'), D('0')))

    def test_force_close_is_only_for_the_assigned_manager(self):
        s = Shipment.objects.create(tracking_number='SHP-CR-FCM', direction='Inbound', status='Discrepant',
                                    destination_warehouse=self.store, assigned_manager=self.manager)
        self.as_user(self.manager2)
        self.post('shipment_detail', s.pk, action='force_close_shipment')
        self.refresh(s)
        self.assertEqual(s.status, 'Discrepant')


class TransferTests(CriticalBase):
    """Internal transfers move stock to the destination exactly once and account for shortages."""

    def test_receiving_a_transfer_moves_the_stock(self):
        src = self.batch('CR-SRC', '100', allocated='60', material=True)
        s, item = self.transfer(src, '60')
        self.post('shipment_detail', s.pk, action='receive_all')
        self.refresh(s, src)
        self.assertEqual(s.status, 'Completed')
        self.assertEqual((src.quantity, src.allocated_quantity), (D('40'), D('0')))
        arrived = Batch.objects.get(warehouse=self.plant)
        self.assertEqual((arrived.quantity, arrived.material), (D('60'), self.material))

    def test_completed_transfer_cannot_be_reopened_or_rolled_back(self):
        src = self.batch('CR-SRC2', '50', allocated='50', material=True)
        s, _ = self.transfer(src, '50')
        self.post('shipment_detail', s.pk, action='receive_all')
        self.post('shipment_detail', s.pk, action='reopen_shipment')
        self.post('shipment_detail', s.pk, action='update_operational_status', status='Arrived')
        self.refresh(s)
        self.assertEqual(s.status, 'Completed')
        self.assertEqual(Batch.objects.filter(warehouse=self.plant).count(), 1)

    def _short_transfer(self):
        src = self.batch('CR-SRC3', '200', allocated='100', material=True)
        s, item = self.transfer(src, '100')
        self.post('shipment_detail', s.pk, action='log_item_receipt', item_id=item.id, quantity='70')
        self.post('shipment_detail', s.pk, action='finalize_shipment_receiving')
        self.refresh(s)
        self.assertEqual(s.status, 'Discrepant')
        Shipment.objects.filter(pk=s.pk).update(assigned_manager=self.admin)
        return src, s, item

    def test_units_lost_in_transit_are_written_off(self):
        src, s, item = self._short_transfer()
        self.post('shipment_detail', s.pk, action='force_close_shipment', **{f'shortage_reason_{item.id}': 'lost'})
        self.refresh(src)
        self.assertEqual(src.quantity, D('100'))    # 200 - 70 arrived - 30 lost
        self.assertEqual(Batch.objects.get(warehouse=self.plant).quantity, D('70'))
        self.assertEqual(RegistryLog.objects.get(action_type='Lost_In_Transit').quantity_changed, D('30'))


class ManufacturingTests(CriticalBase):
    """Runs follow their lifecycle and respect the QA hold."""

    def _run(self, status='Pending Allocation', **kw):
        return ProductionRun.objects.create(run_number=f'RUN-CR-{ProductionRun.objects.count()}', target_product=self.product,
                                            expected_yield=D('10'), status=status, manufacturing_plant=self.plant, **kw)

    def test_run_cannot_start_before_allocation_or_twice(self):
        run = self._run()
        self.post('production_run_detail', run.pk, action='start_production')
        self.refresh(run)
        self.assertEqual(run.status, 'Pending Allocation')
        done = self._run(status='Completed')
        self.post('production_run_detail', done.pk, action='start_production')
        self.refresh(done)
        self.assertEqual(done.status, 'Completed')

    def test_only_a_running_run_can_be_completed(self):
        run = self._run(status='Planned')
        self.post('production_run_detail', run.pk, action='complete_production', actual_yield='10')
        self.refresh(run)
        self.assertEqual(run.status, 'Planned')
        self.assertFalse(Batch.objects.filter(produced_in=run).exists())

    def test_qa_hold_quarantines_output_until_released(self):
        from core.models import SystemSetting
        from core.utils import finalize_production_run
        SystemSetting.objects.update_or_create(key='qa_hold_new_finished_goods', defaults={'value': 'true', 'value_type': 'bool'})
        so, _ = self.so('10', status='In Production')
        run = self._run(status='InProgress', sales_order=so, actual_yield=D('10'))
        fg = finalize_production_run(run, self.admin)
        self.refresh(so)
        self.assertEqual((fg.status, fg.allocated_quantity, so.status), ('Quarantined', D('0'), 'In Production'))
        self.post('so_create_shipment', so.pk)
        self.assertFalse(Shipment.objects.filter(sales_order=so).exists())
        self.post('qa_dashboard', action='release_quarantine', batch_id=fg.id)
        self.refresh(fg, so)
        self.assertEqual((fg.status, fg.allocated_quantity, so.status), ('Active', D('10'), 'Ready to Ship'))


class AdminIntegrityTests(AdminEditBase):
    """The Django admin can't move a batch out from under a reservation, shipment or QA request, or edit stock totals."""

    def test_facility_move_refused_when_reserved_on_shipment_or_in_qa(self):
        Batch.objects.filter(pk=self.batch.pk).update(allocated_quantity=D('1'))
        self.assertContains(self._move(self.b), 'This batch is reserved')
        Batch.objects.filter(pk=self.batch.pk).update(allocated_quantity=0)

        shp = Shipment.objects.create(tracking_number='SHP-ADM', direction='Transfer', origin_warehouse=self.a)
        ShipmentItem.objects.create(shipment=shp, batch=self.batch, material=self.batch.material, quantity=D('5'))
        self.assertContains(self._move(self.b), 'open shipment SHP-ADM')
        shp.status = 'Completed'
        shp.save()

        QARequest.objects.create(batch=self.batch, reason='moisture')
        self.assertContains(self._move(self.b), 'open QA request')

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.warehouse, self.a)
        self.assertFalse(RegistryLog.objects.exists())

    def test_batch_quantity_is_read_only(self):
        r = self.client.get(reverse('admin:core_batch_change', args=[self.batch.pk]))
        self.assertNotContains(r, 'name="quantity"')
        self.assertNotContains(r, 'name="allocated_quantity"')

    def test_staff_without_superuser_cannot_open_admin(self):
        staff = get_user_model().objects.create_user('staff', password='pw', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse('admin:index')).status_code, 302)


class PurchaseOrderCancelTests(TestCase):
    """A PO can't be cancelled once goods arrived, and a cancelled PO takes no receipts."""

    def setUp(self):
        self.client = Client()
        self.user = make_user(username='pocancel', password='pw', role='Manager')
        self.client.force_login(self.user)
        wh = Warehouse.objects.create(name='Cancel WH', location_type='Storage')
        mat = Material.objects.create(name='Cancel Mat', sku='MAT-CANCEL', unit_of_measure='kg', safe_storage_days=365)
        self.po = PurchaseOrder.objects.create(po_number='PO-CANCEL', supplier_name='S', target_warehouse=wh, status='Pending')
        self.line = PurchaseOrderDetail.objects.create(purchase_order=self.po, material=mat, quantity_ordered=Decimal('10'))
        self.ship = Shipment.objects.create(tracking_number='SHP-CANCEL', direction='Inbound', status='Draft',
                                            purchase_order=self.po, destination_warehouse=wh)

    def _cancel(self, reason='Supplier backed out'):
        return self.client.post(reverse('po_detail', args=[self.po.pk]), {'action': 'cancel_po', 'reason': reason})

    def test_refused_once_goods_are_received(self):
        self.line.quantity_received = Decimal('2')
        self.line.save()
        self._cancel()
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'Pending')

    def test_cancelled_po_takes_no_receipts(self):
        self._cancel()
        self.client.post(reverse('po_detail', args=[self.po.pk]), {'action': 'mark_received', 'item_id': self.line.id, 'qty_received': '5'})
        self.line.refresh_from_db()
        self.assertEqual(self.line.quantity_received, Decimal('0'))


class QAWriteOffTests(TestCase):
    """Big write-offs wait for a manager, only managers undo them, expired stock can't be released,
    and material checked on arrival is received on hold."""

    def setUp(self):
        self.wh = Warehouse.objects.create(name='QA Store', location_type='Both')
        self.mat = Material.objects.create(name='QA Humic', sku='QA-1', category='Raw', unit_of_measure='kg',
                                           safe_storage_days=200, weight_mt_per_unit=Decimal('0.001'),
                                           cost_per_unit=Decimal('10'))
        self.manager = make_user(username='qa_mgr', password='pw', role='Manager')
        self.editor = make_user(username='qa_ed', password='pw', role='Staff (Editor)')
        self.client = Client()
        self.client.force_login(self.editor)
        self.today = timezone.localdate()

    def batch(self, number, qty='10', expires_in=100, made_ago=100, **extra):
        return Batch.objects.create(batch_number=number, material=self.mat, quantity=Decimal(qty), status='Active',
                                    warehouse=self.wh, manufacturing_date=self.today - timedelta(days=made_ago),
                                    expiry_date=self.today + timedelta(days=expires_in), **extra)

    def qa_post(self, **data):
        return self.client.post(reverse('qa_dashboard'), data)

    def test_large_write_off_waits_for_a_manager(self):
        b = self.batch('QA-BIG', qty='1000')  # RM 10,000, over the RM 5,000 default
        self.qa_post(action='spoil_dispose', batch_id=b.id, reason='contamination')
        b.refresh_from_db()
        approval = QAApproval.objects.get()
        self.assertEqual((b.status, approval.status, approval.value_rm), ('Quarantined', 'Pending', Decimal('10000.00')))
        self.client.force_login(self.manager)
        self.client.post(reverse('qa_dashboard'), {'action': 'approve_qa', 'approval_id': approval.id})
        b.refresh_from_db(); approval.refresh_from_db()
        self.assertEqual((b.status, approval.status), ('Spoiled', 'Approved'))

    def test_editor_cannot_approve_a_write_off(self):
        b = self.batch('QA-BIG2', qty='1000')
        qa_logic.dispose_batch(b, self.editor, 'contamination')
        approval = QAApproval.objects.get()
        self.qa_post(action='approve_qa', approval_id=approval.id)
        approval.refresh_from_db()
        self.assertEqual(approval.status, 'Pending')

    def test_manager_can_undo_a_write_off_within_the_window(self):
        from core import analytics
        b = self.batch('QA-UNDO')
        qa_logic.dispose_batch(b, self.editor, 'other')
        b.refresh_from_db()
        # Editors can write off but not undo
        self.qa_post(action='undo_write_off', batch_id=b.id)
        b.refresh_from_db()
        self.assertEqual(b.status, 'Spoiled')
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(reverse('batch_detail', args=[b.batch_number])), 'Undo write-off')
        self.qa_post(action='undo_write_off', batch_id=b.id)
        b.refresh_from_db()
        self.assertEqual((b.status, b.closed_date, b.quantity), ('Active', None, Decimal('10')))
        self.assertEqual(list(b.qa_events.filter(action__in=('disposed_undone', 'writeoff_undone')).values_list('action', flat=True).order_by('id')),
                         ['disposed_undone', 'writeoff_undone'])
        self.assertTrue(RegistryLog.objects.filter(action_type='Adjusted', item_name__icontains='write-off undone',
                                                   quantity_changed=Decimal('10')).exists())
        self.assertEqual(analytics.spoilage_report()['totals']['count'], 0)

    def test_expired_stock_is_held_by_the_sweep_and_cannot_be_released(self):
        b = self.batch('QA-EXP', expires_in=-1)
        so = SalesOrder.objects.create(so_number='SO-QA-EXP', client_name='C', origin_warehouse=self.wh, status='Ready to Ship')
        StockAllocation.objects.create(batch=b, sales_order=so, quantity=Decimal('5'))
        Batch.objects.filter(pk=b.pk).update(allocated_quantity=Decimal('5'))
        counts = qa_logic.daily_sweep()
        b.refresh_from_db()
        self.assertEqual(counts['auto_held'], 1)
        self.assertEqual((b.status, b.allocated_quantity), ('Quarantined', Decimal('0')))
        self.assertTrue(QAEvent.objects.filter(batch=b, action='auto_hold', reason='expired').exists())
        self.qa_post(action='release_quarantine', batch_id=b.id)
        b.refresh_from_db()
        self.assertEqual(b.status, 'Quarantined')
        self.assertEqual(qa_logic.daily_sweep()['auto_held'], 0)

    def test_material_checked_on_arrival_is_received_on_hold_with_supplier_dates(self):
        self.mat.inspect_on_receipt = True
        self.mat.save()
        po = PurchaseOrder.objects.create(po_number='PO-QA-IN', supplier_name='S', target_warehouse=self.wh, status='Pending')
        detail = PurchaseOrderDetail.objects.create(purchase_order=po, material=self.mat, quantity_ordered=Decimal('10'))
        expiry = self.today + timedelta(days=60)
        apply_po_material_receipt(detail, Decimal('10'), self.manager, expiry_date=expiry)
        b = Batch.objects.get(purchase_order=po)
        self.assertEqual((b.status, b.expiry_date, b.original_expiry_date), ('Quarantined', expiry, expiry))
        # Waiting for its check: listed under QA Check, not On hold
        req = QARequest.objects.get(batch=b)
        self.assertEqual((req.status, req.reason, req.source), ('Open', 'inbound_check', 'PO-QA-IN'))
        self.client.force_login(self.manager)
        resp = self.client.get(reverse('qa_dashboard'))
        self.assertIn(req, resp.context['requests_open'])
        self.assertNotIn(b, resp.context['quarantined'])
        self.assertEqual(resp.context['tab_list'][0][:2], ('requests', 'QA Check'))


class ArchiveAndYieldTests(TestCase):
    """Archived items can't go on new order lines; logging past the expected yield needs a reason."""

    def setUp(self):
        self.client = Client()
        self.client.force_login(make_user(username='archboss', password='pw', role='Admin'))
        self.wh = Warehouse.objects.create(name='Arch WH', location_type='Storage')
        self.mat = Material.objects.create(name='Old Resin', sku='MAT-ARCH', category='Test', unit_of_measure='kg', safe_storage_days=365, is_active=False)
        self.prod = Product.objects.create(name='Old Widget', sku='PRD-ARCH', unit_of_measure='MT', price_per_unit=10, is_active=False)
        self.live = Product.objects.create(name='Live Widget', sku='PRD-LIVE', unit_of_measure='MT', price_per_unit=10)
        ProductRecipe.objects.create(product=self.live, material=self.mat, quantity_required=1)

    def test_po_and_so_lines_refuse_archived_items(self):
        po = PurchaseOrder.objects.create(po_number='PO-ARCH', supplier_name='S', target_warehouse=self.wh)
        self.client.post(reverse('po_detail', args=[po.pk]), {'action': 'add_po_item', 'material_id': self.mat.id, 'quantity_ordered': '5'})
        self.assertEqual(po.items.count(), 0)
        so = SalesOrder.objects.create(so_number='SO-ARCH', client_name='C', origin_warehouse=self.wh)
        self.client.post(reverse('so_detail', args=[so.pk]), {'action': 'add_so_item', 'product_id': self.prod.id, 'quantity_ordered': '5'})
        self.assertEqual(so.items.count(), 0)
        # An active product whose recipe uses an archived material goes on, with a warning
        r = self.client.post(reverse('so_detail', args=[so.pk]), {'action': 'add_so_item', 'product_id': self.live.id, 'quantity_ordered': '5'}, follow=True)
        self.assertEqual(so.items.count(), 1)
        self.assertContains(r, 'archived material(s): Old Resin')

    def test_logging_past_expected_yield_needs_a_reason(self):
        from core.models import OrderTimeline
        run = ProductionRun.objects.create(run_number='RD-1', target_product=self.live,
                                           expected_yield=Decimal('484.01'), status='InProgress')
        log = lambda q, notes='': self.client.post(reverse('production_run_detail', args=[run.pk]),
                                                  {'action': 'log_yield', 'quantity': q, 'notes': notes})
        log('484.01')
        log('10')
        self.assertEqual(run.yield_logs.count(), 1)
        log('10', 'Extra batch from leftover material')
        self.assertEqual(run.yield_logs.count(), 2)
        self.assertTrue(OrderTimeline.objects.filter(production_run=run, action__contains='leftover material').exists())


class OrderCancelAndAbortTests(TestCase):
    """Cancelling an order or aborting a run gives back every reservation and can't be undone."""

    def setUp(self):
        self.admin = make_user(username='sd_admin', password='pw', role='Admin')
        self.client.force_login(self.admin)
        self.wh = Warehouse.objects.create(name='SD Hub', location_type='Storage')
        self.product = Product.objects.create(name='SD Product', sku='PRD-SD', unit_of_measure='kg', price_per_unit=1)

    def _batch(self, number, qty, allocated=0, expires_in=100):
        return Batch.objects.create(
            batch_number=number, status='Active', product=self.product, quantity=Decimal(qty),
            allocated_quantity=Decimal(allocated), manufacturing_date=date.today() - timedelta(days=200),
            expiry_date=date.today() + timedelta(days=expires_in), warehouse=self.wh,
        )

    def _material_batch(self, number, qty, allocated=0):
        mat, _ = Material.objects.get_or_create(sku='MAT-SD', defaults=dict(name='SD Mat', category='Bulk', unit_of_measure='kg', safe_storage_days=365))
        return Batch.objects.create(batch_number=number, status='Active', material=mat, quantity=Decimal(qty),
                                    allocated_quantity=Decimal(allocated), manufacturing_date=date.today(),
                                    expiry_date=date.today() + timedelta(days=100), warehouse=self.wh)

    def _so(self, ordered, status='Pending', shipped=0):
        so = SalesOrder.objects.create(so_number=f'SO-SD-{SalesOrder.objects.count()}', client_name='C',
                                       origin_warehouse=self.wh, status=status)
        detail = SalesOrderDetail.objects.create(sales_order=so, product=self.product,
                                                 quantity_ordered=Decimal(ordered), quantity_shipped=Decimal(shipped))
        return so, detail

    def _outbound(self, so, batch, qty, status='Arrived', **kw):
        s = Shipment.objects.create(tracking_number=f'SHP-SD-{Shipment.objects.count()}', direction='Outbound',
                                    status=status, origin_warehouse=self.wh, sales_order=so, **kw)
        item = ShipmentItem.objects.create(shipment=s, product=self.product, batch=batch, quantity=Decimal(qty))
        StockAllocation.objects.create(batch=batch, shipment=s, quantity=Decimal(qty))
        return s, item

    def test_cancelling_an_order_releases_its_stock_runs_and_shipments(self):
        so, _ = self._so('10', status='Pending')
        fg = self._batch('SD-CX', '10', allocated='5')
        StockAllocation.objects.create(batch=fg, sales_order=so, quantity=Decimal('3'))
        s, _ = self._outbound(so, fg, '2', status='Draft')
        run = ProductionRun.objects.create(run_number='RUN-SD-CX', target_product=self.product, expected_yield=Decimal('5'),
                                           status='Planned', sales_order=so)
        mat = self._material_batch('SD-CX-M', '50', allocated='10')
        StockAllocation.objects.create(batch=mat, production_run=run, quantity=Decimal('10'))
        url = reverse('so_detail', args=[so.pk])
        self.client.post(url, {'action': 'cancel_so'})          # a reason is required
        so.refresh_from_db()
        self.assertEqual(so.status, 'Pending')
        self.client.post(url, {'action': 'cancel_so', 'reason': 'Client withdrew'})
        for obj in (so, fg, s, run, mat):
            obj.refresh_from_db()
        self.assertEqual((so.status, so.cancel_reason), ('Cancelled', 'Client withdrew'))
        self.assertEqual((fg.allocated_quantity, mat.allocated_quantity), (Decimal('0'), Decimal('0')))
        self.assertEqual((s.status, run.status), ('Cancelled', 'Cancelled'))
        self.assertFalse(StockAllocation.objects.exists())
        # Final: it can't be sent back for approval or edited
        self.client.post(url, {'action': 'request_approval', 'manager_id': self.admin.id})
        self.client.post(url, {'action': 'add_so_item', 'product_id': self.product.id, 'quantity_ordered': '1'})
        so.refresh_from_db()
        self.assertEqual((so.status, so.items.count()), ('Cancelled', 1))

    def test_aborting_a_run_refuses_more_than_allocated_and_frees_the_order(self):
        from core.models import OrderTimeline
        so, _ = self._so('5', status='In Production')
        mat = self._material_batch('SD-AB2-M', '50', allocated='10')
        ProductRecipe.objects.create(product=self.product, material=mat.material, quantity_required=2)
        run = ProductionRun.objects.create(run_number='RUN-SD-AB2', target_product=self.product, expected_yield=Decimal('5'),
                                           status='InProgress', manufacturing_plant=self.wh, sales_order=so)
        StockAllocation.objects.create(batch=mat, production_run=run, quantity=Decimal('10'))
        url = reverse('production_run_detail', args=[run.pk])
        self.client.post(url, {'action': 'abort_run', 'reason': 'x', f'used_qty_{mat.material.id}': '12'})
        run.refresh_from_db(); mat.refresh_from_db()
        self.assertEqual((run.status, mat.quantity), ('InProgress', Decimal('50')))
        self.client.post(url, {'action': 'abort_run', 'reason': 'x', f'used_qty_{mat.material.id}': '10'})
        run.refresh_from_db(); so.refresh_from_db()
        self.assertEqual((run.status, so.status), ('Cancelled', 'Pending'))
        self.assertFalse(OrderTimeline.objects.filter(sales_order=so, action__contains='completed').exists())


class OrderImportApprovalTests(TestCase):
    """An imported order with an approver only reaches Pending Approval, never approved."""

    def test_import_order_with_approver_goes_to_pending_approval_only(self):
        from core.models import Notification
        self.client.force_login(make_user(username='catalogmanager', password='password123', role='Manager'))
        Product.objects.create(name='Existing Product Alpha', sku='PROD0001', unit_of_measure='pcs',
                               weight_mt_per_unit=0.5, price_per_unit=100.00)
        Warehouse.objects.create(name='Main WH', location_type='Storage')
        boss = make_user(username='boss', password='pw', role='Admin')
        csv_data = (
            "so_number,client_name,origin_warehouse,product_sku,quantity_ordered,approver,approval_remarks\n"
            "SO-9100,Acme,Main WH,PROD0001,3,BOSS,urgent\n"
            "SO-9101,Acme,Main WH,PROD0001,3,nobody,\n"  # unknown approver: order rejected
        )
        f = io.BytesIO(csv_data.encode('utf-8'))
        f.name = 'orders.csv'
        self.client.post(reverse('import_so'), {'csv_file': f})
        so = SalesOrder.objects.get()
        self.assertEqual((so.so_number, so.status, so.assigned_to, so.approval_remarks, so.approved_by),
                         ('SO-9100', 'Pending Approval', boss, 'urgent', None))
        self.assertTrue(Notification.objects.filter(user=boss, message__contains='SO-9100').exists())


class RolesSecurityTests(TestCase):
    """The Roles and Users pages can't be used to change the Admin role, hand out permissions you don't hold,
    or reset the password of someone with more powers."""

    def setUp(self):
        self.admin = make_user(username='rp_admin', password='admin-pw-123', role='Admin')
        self.client.force_login(self.admin)

    def post(self, **data):
        return self.client.post(reverse('roles'), data)

    def codes(self, role):
        return set(role.permissions.values_list('codename', flat=True))

    def test_admin_role_is_locked(self):
        from core.models import UserAuditLog
        admin_role = Group.objects.get(name='Admin')
        before = self.codes(admin_role)
        self.post(action='save_permissions', role_id=admin_role.id, perms=['add_salesorder'])
        self.post(action='rename_role', role_id=admin_role.id, name='Boss')
        self.post(action='delete_role', role_id=admin_role.id)
        admin_role.refresh_from_db()
        self.assertEqual((admin_role.name, self.codes(admin_role)), ('Admin', before))
        self.assertFalse(UserAuditLog.objects.filter(action__startswith='role_').exists())

    def test_editor_cannot_give_permissions_they_lack(self):
        boss = Group.objects.create(name='Boss')
        boss.permissions.add(_core_perm('manage_users'), _core_perm('override_approvals'))
        editor = make_user(username='rp_editor', password='x-pw-123456')
        editor.groups.add(boss)
        self.client.force_login(editor)
        role = Group.objects.create(name='Temp')
        self.post(action='save_permissions', role_id=role.id, perms=['approve_sales_orders'])
        self.assertEqual(self.codes(role), set())

    def test_cannot_reset_the_password_of_someone_with_more_powers(self):
        # copy of PasswordResetTests.test_cannot_reset_someone_with_more_powers
        boss = make_user(username='pr_boss', password='boss-pw-123', is_superuser=True)
        self.client.post(reverse('user_management'), {'action': 'reset_password', 'user_id': boss.id})
        boss.refresh_from_db()
        self.assertTrue(boss.check_password('boss-pw-123'))


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


class DenyByDefaultActionTests(CriticalBase):
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


class PublicBatchPageTests(CriticalBase):
    def test_anonymous_sees_no_supplier_or_bin(self):
        po, _ = self.po(5, status='Received')
        po.supplier_name = 'Secret Supplier Sdn Bhd'
        po.save()
        lot = self.batch('BATCH-SEC-1', 5, material=True)
        lot.purchase_order = po
        lot.location = 'Aisle 7 Bin 3'
        lot.save()
        self.client.logout()
        response = self.client.get(reverse('batch_public_info', args=[lot.batch_number]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Secret Supplier')
        self.assertNotContains(response, 'Aisle 7 Bin 3')
        self.assertContains(response, 'CR Store')
