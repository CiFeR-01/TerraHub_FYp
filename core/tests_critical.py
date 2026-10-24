"""Critical-path tests: a small, fast suite covering the business rules that would hurt most if broken.

Run on its own with:  python manage.py test core.tests_critical
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
from core.tests import CriticalBase, make_user
from core.utils import allocate_stock, deduct_stock_from_allocation

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

    def test_manager_may_send_their_own_request_to_themselves(self):
        so, _ = self.so('5')
        self.as_user(self.manager)
        self.post('so_detail', so.pk, action='request_approval', manager_id=self.manager.id)
        self.refresh(so)
        self.assertEqual((so.status, so.assigned_to), ('Pending Approval', self.manager))

    def test_only_the_assigned_approver_decides(self):
        so, _ = self.so('5', status='Pending Approval')
        SalesOrder.objects.filter(pk=so.pk).update(assigned_to=self.manager)
        self.assertEqual(self.decide(self.manager2, 'sales_order', so), 'Pending Approval')
        self.assertEqual(self.decide(self.manager, 'sales_order', so), 'Pending')

    def test_approvers_may_approve_their_own_run(self):
        mine = ProductionRun.objects.create(run_number='RUN-CR-OWN', target_product=self.product, expected_yield=D('5'),
                                            status='Pending Approval', created_by=self.manager, supervisor=self.manager)
        self.assertEqual(self.decide(self.manager, 'production_run', mine), 'Pending Allocation')
        admins = ProductionRun.objects.create(run_number='RUN-CR-ADM', target_product=self.product, expected_yield=D('5'),
                                              status='Pending Approval', created_by=self.admin, supervisor=self.admin)
        self.assertEqual(self.decide(self.admin, 'production_run', admins), 'Pending Allocation')

    def test_editing_an_approved_order_needs_reapproval(self):
        so, _ = self.so('10', status='Pending')
        SalesOrder.objects.filter(pk=so.pk).update(approved_by=self.manager)
        self.post('so_detail', so.pk, action='add_so_item', product_id=self.product.id, quantity_ordered='5')
        self.refresh(so)
        self.assertEqual((so.status, so.approved_by), ('Draft', None))

    def test_outbound_shipment_approval_needs_the_assigned_approver(self):
        s = Shipment.objects.create(tracking_number='SHP-CR-AP', direction='Outbound', status='Pending Approval',
                                    origin_warehouse=self.store, assigned_to=self.manager)
        self.as_user(self.manager2)
        self.post('shipment_detail', s.pk, action='approve')
        self.refresh(s)
        self.assertEqual(s.status, 'Pending Approval')


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

    def test_user_without_a_group_is_kept_out_of_approvals(self):
        self.as_user(make_user(username='cr_nobody', password='pw'))
        self.assertRedirects(self.client.get(reverse('approvals_inbox')), reverse('dashboard'), fetch_redirect_response=False)

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

    def test_emptied_batch_is_closed(self):
        stock = self.batch('CR-EMPTY', '30')
        so, _ = self.so('30')
        allocate_stock('sales_order', so, self.product, 30)
        deduct_stock_from_allocation('sales_order', so)
        self.refresh(stock)
        self.assertEqual((stock.status, stock.closed_date), ('Depleted', date.today()))

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

    def test_stock_on_a_dispatched_shipment_cannot_be_spoiled(self):
        stock = self.batch('CR-LOADED', '10', allocated='10')
        so, _ = self.so('10', status='Shipped')
        self.outbound(so, stock, '10', status='Dispatched')
        self.post('qa_dashboard', action='spoil_dispose', reason='expired', batch_id=stock.id)
        self.refresh(stock)
        self.assertEqual(stock.status, 'Active')


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

    def test_shipment_status_only_moves_forward(self):
        so, _ = self.so('4', status='Shipped')
        s, _ = self.outbound(so, self.batch('CR-FWD', '4', allocated='4'), '4', status='Dispatched')
        self.post('shipment_detail', s.pk, action='update_operational_status', status='Preparing')
        self.post('shipment_detail', s.pk, action='update_operational_status', status='Completed')
        self.refresh(s)
        self.assertEqual(s.status, 'Dispatched')

    def test_expired_stock_cannot_be_shipped(self):
        so, _ = self.so('5', status='Ready to Ship')
        old = self.batch('CR-OLDFG', '5', allocated='5', expires_in=-1)
        StockAllocation.objects.create(batch=old, sales_order=so, quantity=D('5'))
        self.post('so_create_shipment', so.pk)
        self.assertFalse(Shipment.objects.filter(sales_order=so).exists())

    def test_cannot_receive_before_arrival(self):
        so, line = self.so('5', status='Shipped')
        s, _ = self.outbound(so, self.batch('CR-EARLY', '5', allocated='5'), '5', status='Dispatched')
        self.post('shipment_detail', s.pk, action='receive_all')
        self.refresh(s)
        self.assertEqual(s.status, 'Dispatched')

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
        self.post('shipment_detail', s.pk, action='complete_shipment', **{f'received_qty_{item.id}': '70'})
        self.refresh(s)
        self.assertEqual(s.status, 'Discrepant')
        Shipment.objects.filter(pk=s.pk).update(assigned_manager=self.admin)
        return src, s, item

    def test_shortage_without_a_reason_moves_nothing(self):
        src, s, _ = self._short_transfer()
        self.post('shipment_detail', s.pk, action='force_close_shipment')
        self.refresh(s, src)
        self.assertEqual((s.status, src.quantity), ('Discrepant', D('200')))
        self.assertFalse(Batch.objects.filter(warehouse=self.plant).exists())

    def test_units_lost_in_transit_are_written_off(self):
        src, s, item = self._short_transfer()
        self.post('shipment_detail', s.pk, action='force_close_shipment', **{f'shortage_reason_{item.id}': 'lost'})
        self.refresh(src)
        self.assertEqual(src.quantity, D('100'))    # 200 - 70 arrived - 30 lost
        self.assertEqual(Batch.objects.get(warehouse=self.plant).quantity, D('70'))
        self.assertEqual(RegistryLog.objects.get(action_type='Lost_In_Transit').quantity_changed, D('30'))

    def test_transfer_for_a_run_keeps_the_runs_reservation(self):
        src = self.batch('CR-SRC4', '50', allocated='20', wh=self.store, material=True)
        run = ProductionRun.objects.create(run_number='RUN-CR-T', target_product=self.product, expected_yield=D('10'),
                                           status='Awaiting Materials', manufacturing_plant=self.plant)
        s, _ = self.transfer(src, '20')
        StockAllocation.objects.filter(shipment=s).update(production_run=run)
        Shipment.objects.filter(pk=s.pk).update(linked_production_run=run)
        self.post('shipment_detail', s.pk, action='receive_all')
        arrived = Batch.objects.get(warehouse=self.plant)
        self.assertEqual(arrived.allocated_quantity, D('20'))
        self.assertEqual(StockAllocation.objects.get(production_run=run).batch, arrived)


class ManufacturingTests(CriticalBase):
    """Runs take materials in FEFO order, follow their lifecycle, and respect the QA hold."""

    def _run(self, status='Pending Allocation', **kw):
        return ProductionRun.objects.create(run_number=f'RUN-CR-{ProductionRun.objects.count()}', target_product=self.product,
                                            expected_yield=D('10'), status=status, manufacturing_plant=self.plant, **kw)

    def test_run_allocation_follows_fefo_unless_a_reason_is_given(self):
        run = self._run()
        early = self.batch('CR-M-EARLY', '15', expires_in=30, wh=self.plant, material=True)
        late = self.batch('CR-M-LATE', '100', expires_in=90, wh=self.plant, material=True)
        url_post = lambda **d: self.post('production_run_allocate', run.pk, action='allocate_run', **d)
        url_post(**{f'batch_qty_{late.pk}': '20'})
        self.assertFalse(run.allocations.exists())
        url_post(**{f'batch_qty_{early.pk}': '15', f'batch_qty_{late.pk}': '5'})
        self.assertEqual(sorted(run.allocations.values_list('batch__batch_number', 'quantity')),
                         [('CR-M-EARLY', D('15.00')), ('CR-M-LATE', D('5.00'))])

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

    def test_started_run_cannot_be_scrapped(self):
        run = self._run(status='InProgress')
        self.post('production_run_detail', run.pk, action='scrap_run', reason='changed mind')
        self.refresh(run)
        self.assertEqual(run.status, 'InProgress')

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


class SecurityTests(CriticalBase):
    """The System Console must not leak live SQL (session keys, password hashes) to non-Admins."""

    def test_system_console_is_admin_only(self):
        self.as_user(make_user(username='cr_nobody', password='pw'))
        self.assertEqual(self.client.get(reverse('db_logs_api')).status_code, 403)
        self.assertRedirects(self.client.get(reverse('system')), reverse('dashboard'), fetch_redirect_response=False)
        self.as_user(self.admin)
        self.assertEqual(self.client.get(reverse('db_logs_api')).status_code, 200)
        self.assertEqual(self.client.get(reverse('system')).status_code, 200)

    def test_query_log_hides_parameter_values(self):
        from core.db_tracker import DB_QUERY_LOGS
        DB_QUERY_LOGS.clear()
        list(Material.objects.filter(sku='SECRET-VALUE-123'))
        self.assertFalse(any('SECRET-VALUE-123' in log['sql'] for log in DB_QUERY_LOGS))
