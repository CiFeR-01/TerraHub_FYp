from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.contrib import messages
from django import forms
from django.db import transaction
from django.db.models import Sum, F, Case, When, Value, DecimalField, Count, Q, Avg
from django.db.models.functions import Abs, Coalesce, TruncWeek
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import csv
import io
import uuid
from django.utils import timezone
from .models import (
    CustomUser, Warehouse, WarehouseLocation, Material, Product,
    ProductRecipe, ProductionRun, ProductionRunYieldLog, RunMaterialUsage,
    ProductionConsumption, Batch,
    PurchaseOrder, PurchaseOrderDetail, SalesOrder, SalesOrderDetail,
    Shipment, ShipmentItem, ShipmentItemReceipt, StockAudit, RegistryLog, OrderTimeline, Notification,
    StockAllocation, Supplier, SupplierMaterial, Client
)
from .utils import generate_next_code, format_stock_display, unexpired, read_decimal, read_date, read_unit_weight, UNIT_WEIGHT_MT
from .validators import normalise_phone, normalise_phone_or_email, validate_email_address, validation_messages
from .decorators import permission_or_redirect
from .account import log_archive_change, log_user_change
from .permissions import (
    is_admin_user, can_approve, can_grant_group, action_denied,
    limited_warehouse_ids, may_use_warehouse, FACILITY_REFUSED, approvers, users_with_perm, pending_actions,
    ADJUST_PHYSICAL_STOCK, MANAGE_USERS, HANDLE_PURCHASING, approve_perm,
    APPROVE_SALES_ORDERS, APPROVE_PURCHASE_ORDERS, APPROVE_PRODUCTION_RUNS, APPROVE_SHIPMENTS,
)


def apply_list_sort(request, qs, fields, default):
    """Server-side column sort for paginated lists. `fields` maps a ?sort= key to an ORM expression; unknown keys use `default`.
    Returns (qs, context) with the sort state and query strings that keep filters when sorting or paging."""
    from django.db.models import F
    sort = request.GET.get('sort', default)
    key = sort.lstrip('-')
    if key not in fields:
        sort, key = default, default.lstrip('-')
    desc = sort.startswith('-')
    expr = fields[key]
    if isinstance(expr, str):
        expr = F(expr)
    # Blank values (e.g. no ETA) sit at the bottom either way, on SQLite and Postgres alike.
    qs = qs.order_by(expr.desc(nulls_last=True) if desc else expr.asc(nulls_last=True), '-id')

    keep = request.GET.copy()
    keep.pop('page', None)
    page_qs = keep.urlencode()
    keep.pop('sort', None)
    return qs, {'sort_key': key, 'sort_desc': desc, 'sort_qs': keep.urlencode(), 'page_qs': page_qs}


# Rows-per-page choices offered by partials/_pagination.html's selector.
PAGE_SIZE_CHOICES = (25, 50, 100)


def get_page_size(request, default=25, max_size=500):
    """Resolves ?page_size= for a paginated list, using `default` if missing or invalid and capping it."""
    raw = request.GET.get('page_size', default)
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return default
    if size <= 0:
        return default
    return min(size, max_size)


def approver_problem(approver, perm):
    """Why `approver` can't take an approval request needing `perm`, or None.
    Anyone holding `perm` may be picked, including the requester themselves (self-approval)."""
    if not approver or not approver.has_perm(perm):
        return "Please select a valid manager for approval."
    return None


def clear_approval_notifications(reference):
    """Marks the "requires your approval" notifications for `reference` (SO/PO number, tracking number or run number) as read."""
    Notification.objects.filter(is_read=False, message__contains=reference).filter(
        Q(message__icontains='requires your approval') | Q(message__icontains='approval required')
        | Q(message__icontains='pending variance approval') | Q(message__icontains='QA release needed')
        | Q(message__icontains='Force Close requested')
    ).update(is_read=True)


def notify_added_follower(adder, follower, label, link):
    """Let someone know they were added as a follower of `label` (e.g. "Production
    Run RUN-001"). Adding yourself needs no notification."""
    if follower == adder:
        return
    Notification.objects.create(
        user=follower,
        message=f"{adder.get_full_name() or adder.username} added you as a follower to {label}.",
        link=link,
    )


def self_approval_note(requester, approver):
    return " (self-approval)" if approver == requester else ""


def may_decide_approval(user, obj):
    """Only something actually pending approval, and only by its assigned approver while they hold the module's approve permission."""
    return obj.status == 'Pending Approval' and obj.assigned_to_id == user.id and user.has_perm(approve_perm(obj))


def may_decide_run_approval(user, run):
    """Unassigned pending runs can be decided by anyone who may approve runs, including whoever created or supervises the run;
    if an approver was named, only they."""
    if run.status != 'Pending Approval' or not user.has_perm(APPROVE_PRODUCTION_RUNS):
        return False
    return not run.assigned_to_id or run.assigned_to_id == user.id


# Sales order edit rules: locked once stock is heading out; editing an approved order sends it back to Draft.
SO_ITEMS_LOCKED = ('Ready to Ship', 'Partially Shipped', 'Shipped', 'Delivered', 'Cancelled')
SO_REAPPROVE_ON_EDIT = ('Pending Approval', 'Pending', 'Awaiting Acknowledgement', 'In Production')
# Status changes allowed from the dropdown; everything else comes from the order flow.
# Delivery plan can be switched until one of its shipments is approved.
SO_PLAN_SWITCHABLE = ('Draft', 'Logistics Review', 'Pending Approval')
SO_MANUAL_TRANSITIONS = {
    'Ready to Ship': ['Shipped', 'Delivered'],
    'Partially Shipped': ['Shipped', 'Delivered'],
    'Shipped': ['Delivered'],
}


SO_UNCANCELLABLE = ('Shipped', 'Delivered', 'Cancelled')
SHIPMENT_OUT = ('Dispatched', 'Delayed', 'Arrived', 'Discrepant')   # goods physically left; can't be undone by a cancel


def so_cancel_problem(so):
    """Why `so` can't be cancelled right now, or None. Anything already on the road or being made must be dealt with first."""
    if so.status in SO_UNCANCELLABLE:
        return f"{so.so_number} is {so.status}, so it can't be cancelled."
    out = so.shipments.filter(direction__in=['Outbound', 'Transfer'], status__in=SHIPMENT_OUT)
    if out.exists():
        return (f"{', '.join(out.values_list('tracking_number', flat=True))} has already left. "
                "Receive or force close it before cancelling the order.")
    running = so.production_runs.filter(status__in=['InProgress', 'Paused'])
    if running.exists():
        return (f"Run {', '.join(running.values_list('run_number', flat=True))} is already in production. "
                "Abort or finish it on the run page before cancelling the order.")
    return None


def cancel_sales_order(so, reason, user):
    """Cancels an order for good: frees every reservation it holds, cancels its unstarted runs and its shipments
    that haven't left. Shipped units and finished runs stay as history. Callers check so_cancel_problem first."""
    from .utils import release_production_run_allocations
    with transaction.atomic():
        for sh in so.shipments.filter(direction__in=['Outbound', 'Transfer']).exclude(status__in=['Completed', 'Cancelled']):
            for a in StockAllocation.objects.filter(shipment=sh).select_related('batch'):
                a.batch.allocated_quantity = max(a.batch.allocated_quantity - a.quantity, Decimal('0'))
                a.batch.save(update_fields=['allocated_quantity'])
                a.delete()
            sh.status = 'Cancelled'
            sh.save(update_fields=['status'])
            clear_approval_notifications(sh.tracking_number)
            OrderTimeline.objects.create(shipment=sh, action=f"Cancelled with {so.so_number}. Its stock is free again.", user=user)
        for a in StockAllocation.objects.filter(sales_order=so).select_related('batch'):
            a.batch.allocated_quantity = max(a.batch.allocated_quantity - a.quantity, Decimal('0'))
            a.batch.save(update_fields=['allocated_quantity'])
            a.delete()
        for run in so.production_runs.filter(status__in=['Pending Approval', 'Pending Allocation', 'Awaiting Materials', 'Planned']):
            release_production_run_allocations(run, user)
            run.status = 'Cancelled'
            run.save(update_fields=['status'])
            clear_approval_notifications(run.run_number)
            OrderTimeline.objects.create(production_run=run, action=f"Cancelled with {so.so_number}. Reason: {reason}", user=user)
        so.status = 'Cancelled'
        so.cancel_reason = reason
        so.assigned_to = None
        so.updated_by = user
        so.save()
        clear_approval_notifications(so.so_number)
        OrderTimeline.objects.create(sales_order=so, action=f"Order cancelled. Reason: {reason}", user=user)
        for follower in so.followers.exclude(pk=user.pk):
            Notification.objects.create(user=follower, message=f"Sales Order {so.so_number} was cancelled.", link=reverse('so_detail', args=[so.pk]))



def po_cancel_problem(po):
    """Why `po` can't be cancelled right now, or None. Like a sales order: any PO before goods arrive or leave the supplier."""
    if po.status not in ('Draft', 'Pending Approval', 'Pending'):
        return f"{po.po_number} is {po.status}, so it can't be cancelled. A part-received PO can be closed short instead."
    if any(i.quantity_received > 0 for i in po.items.all()):
        return f"Goods have already been received on {po.po_number}. Close it short instead."
    out = po.shipments.filter(direction='Inbound', status__in=SHIPMENT_OUT + ('Completed',))
    if out.exists():
        return f"{', '.join(out.values_list('tracking_number', flat=True))} has already left the supplier. Receive or scrap it first."
    return None


def cancel_purchase_order(po, reason, user):
    """Cancels a PO and its deliveries that haven't left. Callers check po_cancel_problem first."""
    with transaction.atomic():
        for sh in po.shipments.filter(direction='Inbound').exclude(status='Cancelled'):
            sh.status = 'Cancelled'
            sh.save(update_fields=['status'])
            clear_approval_notifications(sh.tracking_number)
            OrderTimeline.objects.create(shipment=sh, action=f"Cancelled with {po.po_number}.", user=user)
        po.status = 'Cancelled'
        po.assigned_to = None
        po.updated_by = user
        po.save()
        clear_approval_notifications(po.po_number)
        OrderTimeline.objects.create(purchase_order=po, action=f"PO cancelled. Reason: {reason}", user=user)
        for follower in po.followers.exclude(pk=user.pk):
            Notification.objects.create(user=follower, message=f"Purchase Order {po.po_number} was cancelled.", link=reverse('po_detail', args=[po.pk]))

def so_manual_status_problem(so, new_status):
    """Why `so` can't be set to `new_status` from the dropdown, or None.
    Shipped/Delivered by hand only once every unit has shipped and none of its shipments is still open."""
    if new_status not in SO_MANUAL_TRANSITIONS.get(so.status, []):
        return f"{so.so_number} can't be changed from {so.status} to {new_status} by hand."
    open_ships = so.shipments.filter(direction__in=['Outbound', 'Transfer']).exclude(status__in=['Completed', 'Cancelled'])
    if open_ships.exists():
        return (f"{so.so_number} still has open shipment(s) {', '.join(open_ships.values_list('tracking_number', flat=True))}. "
                f"Complete or scrap them before marking it {new_status}.")
    if any(i.quantity_shipped < i.quantity_ordered for i in so.items.all()):
        return f"{so.so_number} hasn't shipped every unit it ordered, so it can't be marked {new_status} by hand."
    return None

# Only a partly received PO can be closed by hand; other PO statuses come from approval and receiving.
PO_MANUAL_TRANSITIONS = {
    'Partially Received': ['Completed'],
    'Completed': ['Partially Received'],   # reopen, only when closed short (see po_manual_transitions)
}


def po_manual_transitions(po):
    """Statuses this PO may be set to by hand. A Completed PO can only be reopened if it was closed short."""
    allowed = PO_MANUAL_TRANSITIONS.get(po.status, [])
    if po.status == 'Completed' and all(i.quantity_received >= i.quantity_ordered for i in po.items.all()):
        return []
    return allowed

# Unapproved orders cannot be allocated, received or produced against.
UNAPPROVED_STATUSES = ('Draft', 'Pending Approval', 'Rejected')
# Orders that stock can be allocated to: approved and not covered yet, or part-shipped and still owed units.
SO_ALLOCATABLE = ('Pending', 'Partially Shipped')
# Orders whose shortages may be sent to manufacturing: approved and not yet Ready to Ship.
SO_MANUFACTURABLE = ('Pending', 'Awaiting Acknowledgement', 'In Production')


# PO statuses where lines can't be edited
PO_ITEMS_LOCKED = ('Partially Received', 'Completed', 'Cancelled')
PO_REAPPROVE_ON_EDIT = ('Pending Approval', 'Pending')


def po_return_for_reapproval(po, user, what):
    if po.status not in PO_REAPPROVE_ON_EDIT:
        return False
    po.status = 'Draft'
    po.assigned_to = None
    po.approved_by = None
    po.revision_count += 1
    po.save()
    clear_approval_notifications(po.po_number)
    OrderTimeline.objects.create(purchase_order=po, action=f"{what} after approval - returned to Draft for re-approval.", user=user)
    return True


def so_return_for_reapproval(so, user, what):
    if so.status not in SO_REAPPROVE_ON_EDIT:
        return False
    so.status = 'Draft'
    so.assigned_to = None
    so.approved_by = None
    so.revision_count += 1
    so.save()
    clear_approval_notifications(so.so_number)
    OrderTimeline.objects.create(sales_order=so, action=f"{what} after approval - returned to Draft for re-approval.", user=user)
    return True


# Outbound/Transfer cargo is locked once the truck leaves; edits while pending or approved return the shipment to Logistics Review.
SHIPMENT_CARGO_LOCKED = ('Dispatched', 'Delayed', 'Arrived', 'Completed', 'Discrepant', 'Cancelled')
# Inbound cargo is the supplier's declared load, so it stays editable on the road and locks once the truck arrives.
INBOUND_CARGO_LOCKED = ('Arrived', 'Completed', 'Discrepant', 'Cancelled')


def shipment_cargo_locked(shipment):
    return shipment.status in (INBOUND_CARGO_LOCKED if shipment.direction == 'Inbound' else SHIPMENT_CARGO_LOCKED)

# Statuses each shipment action may start from: nothing that has left can be scrapped or sent back, and only a finished one reopened.
SHIPMENT_ACTION_STATUSES = {
    'scrap_shipment': ('Draft', 'Logistics Review', 'Pending Approval', 'Preparing'),
    'submit_for_approval': ('Draft', 'Logistics Review'),
    'return_to_manufacturing': ('Logistics Review', 'Pending Approval', 'Preparing'),
    'reopen_shipment': ('Completed',),
}
# A supplier's truck still on the road holds none of our stock, so it can be scrapped too.
INBOUND_EXTRA_SCRAPPABLE = ('Dispatched', 'Delayed')


@login_required
def dashboard_view(request):
    from .analytics import warehouse_rent_burn

    warehouses_by_id = Warehouse.objects.in_bulk()

    warehouse_stats = []
    total_capacity = 0.0
    total_used = 0.0
    total_daily_cost = 0.0

    for row in warehouse_rent_burn():
        w = warehouses_by_id[row['warehouse_id']]
        used_mt = row['used_mt']
        capacity_mt = row['capacity_mt']
        daily_cost = row['daily_cost']
        total_capacity += capacity_mt
        total_used += used_mt
        total_daily_cost += daily_cost

        if capacity_mt > 0:
            utilization_percent = (used_mt / capacity_mt) * 100
        else:
            utilization_percent = 0.0

        warehouse_stats.append({
            'name': w.name,
            'type': w.get_ownership_type_display() if hasattr(w, 'get_ownership_type_display') else w.ownership_type,
            'raw_type': w.ownership_type,
            'capacity_mt': capacity_mt,
            'used_mt': used_mt,
            'quarantined_mt': row['quarantined_mt'],
            'daily_cost': daily_cost,
            'billing_mode': row['billing_mode'],
            'utilization_percent': utilization_percent,
        })

    # 2. Inventory metrics (material quantities are in kg, converted to MT for the KPI).
    raw_materials_mt = Batch.objects.filter(status='Active', material__isnull=False).aggregate(
        t=Coalesce(Sum(F('quantity') * F('material__weight_mt_per_unit'), output_field=DecimalField()),
                   Value(0, output_field=DecimalField()))
    )['t']
    finished_goods_sum = Batch.objects.filter(status='Active', product__isnull=False).aggregate(total=Sum('quantity'))['total'] or 0

    inventory_metrics = {
        'raw_materials_mt': float(raw_materials_mt),
        'finished_goods': float(finished_goods_sum),
    }

    # Global utilization percent
    if total_capacity > 0:
        global_utilization = (total_used / total_capacity) * 100
    else:
        global_utilization = 0.0

    # Headline status follows the worse of the network average and the fullest site.
    over_capacity_sites = [w for w in warehouse_stats if w['utilization_percent'] > 100]
    worst_site = max(warehouse_stats, key=lambda w: w['utilization_percent'], default=None)
    worst_pct = worst_site['utilization_percent'] if worst_site else 0.0
    if global_utilization > 85 or worst_pct > 100:
        capacity_status = 'crit'
    elif global_utilization > 70 or worst_pct > 85:
        capacity_status = 'warn'
    else:
        capacity_status = 'ok'

    # 3. Recent logs (pre-fetching related warehouse models, ordered descending by timestamp)
    recent_logs = RegistryLog.objects.select_related('warehouse', 'material').order_by('-timestamp')[:5]

    # 4. Active shipments (approved or moving), soonest ETA first, capped for the dashboard preview.
    DASH_PREVIEW_LIMIT = 8
    active_shipments_qs = Shipment.objects.exclude(
        status__in=['Draft', 'Arrived', 'Completed', 'Cancelled']
    ).select_related(
        'origin_warehouse', 'destination_warehouse'
    ).annotate(item_count=Count('items')).order_by(F('expected_eta_date').asc(nulls_last=True), 'id')
    active_shipments_total = active_shipments_qs.count()
    active_shipments = active_shipments_qs[:DASH_PREVIEW_LIMIT]

    # 5. Degrading batches (within the QA near-expiry window, qa_near_expiry_days), soonest expiry first, capped.
    from .qa import sweep_if_due
    from .settings_store import get_setting
    sweep_if_due()
    near_window = get_setting('qa_near_expiry_days')
    active_batches = Batch.objects.filter(status='Active').select_related('material', 'product', 'warehouse')
    today = date.today()
    degrading_batches = []
    for b in active_batches:
        if b.expiry_date:
            days_remaining = (b.expiry_date - today).days
            if days_remaining <= near_window:
                b.days_remaining = days_remaining
                degrading_batches.append(b)
    degrading_batches.sort(key=lambda b: b.days_remaining)
    degrading_batches_total = len(degrading_batches)
    degrading_batches = degrading_batches[:DASH_PREVIEW_LIMIT]

    # Analytics: attention panel, production progress, KPI deltas, sparklines, output trend.
    tomorrow = today + timedelta(days=1)
    prev30 = today - timedelta(days=30)
    prev60 = today - timedelta(days=60)

    def _reg_sum(action, start, end, mt=False):
        """Registry quantity for `action` in [start, end). mt=True sums only
        material movements, converted to MT."""
        logs = RegistryLog.objects.filter(
            action_type=action, timestamp__date__gte=start, timestamp__date__lt=end
        )
        if mt:
            logs = logs.filter(material__isnull=False)
            expr = Sum(F('quantity_changed') * F('material__weight_mt_per_unit'), output_field=DecimalField())
        else:
            expr = Sum('quantity_changed')
        return float(logs.aggregate(s=expr)['s'] or 0)

    def _trend(cur, prev):
        """Period-over-period change for a KPI pill; swings of 200%+ are shown neutral."""
        if not prev:
            return {'dir': 'none', 'pct': None, 'prev': prev}
        pct = (cur - prev) / prev * 100
        if abs(pct) >= 200:
            direction = 'flat'
        else:
            direction = 'up' if pct >= 0 else 'down'
        return {'dir': direction, 'pct': pct, 'prev': prev}

    # A. "Needs attention" counters (each links to a filtered work list)
    attention = {
        'approvals': (
            PurchaseOrder.objects.filter(status='Pending Approval').count()
            + SalesOrder.objects.filter(status='Pending Approval').count()
            + ProductionRun.objects.filter(status='Pending Approval').count()
            + Shipment.objects.filter(status='Pending Approval').count()
        ),
        'overdue_pos': PurchaseOrder.objects.filter(
            expected_delivery_date__lt=today
        ).exclude(status__in=['Completed', 'Rejected', 'Draft']).count(),
        'late_sos': SalesOrder.objects.filter(
            fulfillment_deadline__lt=today
        ).exclude(status__in=['Shipped', 'Delivered', 'Rejected', 'Draft']).count(),
        'delayed_shipments': Shipment.objects.filter(
            status__in=['Delayed', 'Discrepant']
        ).count(),
        'awaiting_materials': ProductionRun.objects.filter(status='Awaiting Materials').count(),
        'quarantined': Batch.objects.filter(status='Quarantined').count(),
        'pending_audits': StockAudit.objects.filter(status='Pending').count(),
        'over_capacity': len(over_capacity_sites),
    }
    attention_total = sum(attention.values())

    # B. Production in progress — % of target reached from dated yield logs
    active_runs = (
        ProductionRun.objects
        .filter(status__in=['InProgress', 'Paused', 'Awaiting Materials'])
        .select_related('target_product', 'supervisor')
        .annotate(made=Coalesce(
            Sum('yield_logs__quantity'),
            Value(0, output_field=DecimalField()),
        ))
        .order_by('status', 'run_number')
    )
    run_progress = []
    for r in active_runs:
        expected = float(r.expected_yield or 0)
        made = float(r.made or 0)
        pct = (made / expected * 100) if expected > 0 else 0
        days_running = (today - r.start_time.date()).days if r.start_time else None
        # A run stuck waiting (or paused) for a week+ is the one to chase.
        age_level = ''
        if r.status in ('Awaiting Materials', 'Paused') and days_running is not None:
            if days_running >= 14:
                age_level = 'crit'
            elif days_running >= 7:
                age_level = 'warn'
        run_progress.append({
            'run_number': r.run_number,
            'pk': r.pk,
            'product': r.target_product.name if r.target_product else '—',
            'status': r.status,
            'status_display': r.get_status_display(),
            'supervisor': r.supervisor.username if r.supervisor else 'Unassigned',
            'made': made,
            'expected': expected,
            'unit': 'units' if r.target_product else '',
            'pct': min(pct, 100),
            'pct_raw': pct,
            'days_running': days_running,
            'age_level': age_level,
        })

    # C. KPI deltas — last 30 days vs the 30 days before that
    rm_in_cur, rm_in_prev = _reg_sum('Inbound', prev30, tomorrow, mt=True), _reg_sum('Inbound', prev60, prev30, mt=True)
    fg_cur, fg_prev = _reg_sum('Produced', prev30, tomorrow), _reg_sum('Produced', prev60, prev30)

    kpi_deltas = {
        'raw_materials': _trend(rm_in_cur, rm_in_prev),
        'finished_goods': _trend(fg_cur, fg_prev),
    }

    # D. Sparklines: weekly totals for the last 6 weeks as (height%, label, value).
    def _weekly_heights(action, unit, mt=False):
        weeks = []
        for i in range(6, 0, -1):
            wk_start = today - timedelta(days=i * 7)
            wk_end = today - timedelta(days=(i - 1) * 7)
            weeks.append((wk_start, wk_end, _reg_sum(action, wk_start, wk_end, mt=mt)))
        peak = max(v for _, _, v in weeks) or 1

        def _fmt(d):
            return f"{d.strftime('%b')} {d.day}"

        return [
            {
                'height': max(round(v / peak * 100), 4),
                'label': f"{_fmt(wk_start)}–{_fmt(wk_end)}: {v:,.0f} {unit}",
            }
            for wk_start, wk_end, v in weeks
        ]

    raw_spark = _weekly_heights('Inbound', 'MT', mt=True)
    fg_spark = _weekly_heights('Produced', 'units')

    # E. Weekly finished-goods output trend (12 weeks) for a real line chart
    twelve_weeks_ago = today - timedelta(weeks=12)
    weekly_output_qs = (
        ProductionRunYieldLog.objects
        .filter(log_date__gte=twelve_weeks_ago)
        .annotate(wk=TruncWeek('log_date'))
        .values('wk')
        .annotate(qty=Sum('quantity'))
        .order_by('wk')
    )
    output_trend = {
        'labels': [row['wk'].strftime('%d %b') for row in weekly_output_qs],
        'values': [float(row['qty'] or 0) for row in weekly_output_qs],
    }

    # F. Material usage variance (completed runs), biggest deviation first; beyond 3% needs sign-off, beyond 10% is severe.
    material_variance = list(
        RunMaterialUsage.objects
        .filter(production_run__status='Completed')
        .values('material__name', 'material__sku')
        .annotate(avg_var=Avg('variance_pct'), runs=Count('id'))
        .order_by(Abs(F('avg_var')).desc())[:6]
    )
    for m in material_variance:
        v = float(m['avg_var'] or 0)
        m['level'] = 'crit' if abs(v) > 10 else 'warn' if abs(v) > 3 else 'ok'
        m['direction'] = 'over' if v > 0 else 'under'

    # G. Inventory value in RM (not just tonnage)
    rm_value = Batch.objects.filter(status='Active', material__isnull=False).aggregate(
        v=Coalesce(Sum(F('quantity') * F('material__cost_per_unit'),
                       output_field=DecimalField()), Value(0, output_field=DecimalField()))
    )['v']
    fg_value = Batch.objects.filter(status='Active', product__isnull=False).aggregate(
        v=Coalesce(Sum(F('quantity') * F('product__price_per_unit'),
                       output_field=DecimalField()), Value(0, output_field=DecimalField()))
    )['v']
    inventory_value = float(rm_value) + float(fg_value)

    def _compact_rm(amount):
        """Executive-style short money label, e.g. 34_089_550 -> 'RM 34.1M'."""
        amount = float(amount)
        for divisor, suffix in ((1_000_000_000, 'B'), (1_000_000, 'M'), (1_000, 'K')):
            if abs(amount) >= divisor:
                return f"RM {amount / divisor:.1f}{suffix}"
        return f"RM {amount:,.0f}"

    inventory_value_compact = _compact_rm(inventory_value)

    last_activity = RegistryLog.objects.order_by('-timestamp').values_list('timestamp', flat=True).first()
    # Only claim "live" when the data really is fresh.
    is_live = bool(last_activity) and timezone.now() - last_activity < timedelta(hours=1)

    # 6. Sales order pipeline: one bar per open stage, closed orders as a single count.
    so_counts = SalesOrder.objects.values('status').annotate(count=Count('id'))
    counts_dict = {item['status']: item['count'] for item in so_counts}
    so_pipeline = [
        {'label': 'Draft', 'count': counts_dict.get('Draft', 0), 'tone': 'slate'},
        {'label': 'Pending approval', 'count': counts_dict.get('Pending Approval', 0), 'tone': 'amber'},
        {'label': 'Approved', 'count': counts_dict.get('Pending', 0), 'tone': 'sky'},
        {'label': 'In production', 'count': counts_dict.get('Awaiting Acknowledgement', 0) + counts_dict.get('In Production', 0), 'tone': 'primary'},
        {'label': 'Ready to ship', 'count': counts_dict.get('Ready to Ship', 0) + counts_dict.get('Partially Shipped', 0), 'tone': 'emerald'},
    ]
    so_peak = max((s['count'] for s in so_pipeline), default=0) or 1
    for s in so_pipeline:
        s['width'] = round(s['count'] / so_peak * 100)
    so_open_total = sum(s['count'] for s in so_pipeline)
    so_closed_total = counts_dict.get('Shipped', 0) + counts_dict.get('Delivered', 0)

    context = {
        'warehouse_stats': warehouse_stats,
        'inventory_metrics': inventory_metrics,
        'global_utilization': global_utilization,
        'global_utilization_bar': min(global_utilization, 100),
        'capacity_status': capacity_status,
        'over_capacity_sites': over_capacity_sites,
        'worst_site': worst_site,
        'total_daily_cost': total_daily_cost,
        'recent_logs': recent_logs,
        'active_shipments': active_shipments,
        'active_shipments_total': active_shipments_total,
        'degrading_batches': degrading_batches,
        'degrading_batches_total': degrading_batches_total,
        'near_window': near_window,
        'so_pipeline': so_pipeline,
        'so_open_total': so_open_total,
        'so_closed_total': so_closed_total,
        'current_timestamp': date.today().strftime('%Y-%m-%d'),
        'today': today,
        # analytics upgrade
        'attention': attention,
        'attention_total': attention_total,
        'run_progress': run_progress,
        'kpi_deltas': kpi_deltas,
        'raw_spark': raw_spark,
        'fg_spark': fg_spark,
        'output_trend': output_trend,
        'material_variance': material_variance,
        'inventory_value': inventory_value,
        'inventory_value_compact': inventory_value_compact,
        'last_activity': last_activity,
        'is_live': is_live,
    }

    return render(request, 'dashboard.html', context)


def _setting_label(key):
    return key.replace('_', ' ').title()


@login_required
def system_settings_view(request):
    """In-site editor for the settings in core.settings_store, grouped by its SECTIONS. Superusers and Admins."""
    if not (request.user.is_superuser or is_admin_user(request.user)):
        messages.error(request, "Permission Denied. System settings are for Admins only.")
        return redirect('dashboard')

    from .account import log_setting_change
    from .models import SystemSetting
    from .settings_store import ALL_SETTINGS, SECTIONS, cast_value, default_value, get_setting, limits

    if request.method == 'POST':
        reset_key = request.POST.get('reset_key')
        if reset_key:
            if reset_key in ALL_SETTINGS:
                old, default = get_setting(reset_key), default_value(reset_key)
                with transaction.atomic():
                    SystemSetting.objects.filter(key=reset_key).delete()
                    if str(old) != str(default):
                        log_setting_change(request, reset_key, f"{old} -> {default} (reset to default)")
                messages.success(request, f"{_setting_label(reset_key)} is back to its default ({default}).")
            return redirect('system_settings')

        changed, failed = 0, False
        for key, (_, value_type, description) in ALL_SETTINGS.items():
            raw = request.POST.get(key)
            if raw is None:  # not on the form: leave it alone
                continue
            label = _setting_label(key)
            cast = cast_value(raw.strip(), value_type)
            if cast is None:
                messages.error(request, f"'{raw}' is not a valid {value_type} value for {label}.")
                failed = True
                continue
            low, high = limits(key)
            if (low is not None and cast < low) or (high is not None and cast > high):
                messages.error(request, f"{label} must be between {low} and {high}.")
                failed = True
                continue
            old = get_setting(key)
            if cast == old:
                continue
            with transaction.atomic():
                SystemSetting.objects.update_or_create(key=key, defaults={
                    'value': str(cast), 'value_type': value_type,
                    'description': description[:255], 'updated_by': request.user,
                })
                log_setting_change(request, key, f"{old} -> {cast}")
            changed += 1
        if changed:
            messages.success(request, f"Saved {changed} change{'s' if changed != 1 else ''}.")
        elif not failed:
            messages.info(request, "Nothing changed.")
        return redirect('system_settings')

    rows = SystemSetting.objects.select_related('updated_by').in_bulk(field_name='key')

    def item(key):
        _, value_type, description = ALL_SETTINGS[key]
        value, default = get_setting(key), default_value(key)
        low, high = limits(key)
        return {
            'key': key,
            'label': _setting_label(key),
            'value': value,
            'default': default,
            'value_type': value_type,
            'description': description,
            'min': low,
            'max': high,
            'updated_at': rows[key].updated_at if key in rows else None,
            'updated_by': rows[key].updated_by if key in rows else None,
            'is_overridden': str(value) != str(default),
        }

    sections = [{'title': title, 'settings': [item(k) for k in keys if k in ALL_SETTINGS]} for title, keys in SECTIONS]
    placed = {k for _, keys in SECTIONS for k in keys}
    leftover = [k for k in ALL_SETTINGS if k not in placed]
    if leftover:
        sections.append({'title': 'Other', 'settings': [item(k) for k in leftover]})
    return render(request, 'system_settings.html', {'sections': sections})


from django.http import JsonResponse
import datetime

def session_keepalive_view(request):
    """Keepalive ping from static/js/session-timeout.js. Returns 401 (not a redirect) if the session has already expired."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({'authenticated': False}, status=401)
    from .account import idle_timeout_seconds
    return JsonResponse({'authenticated': True,
                         'expires_in': min(request.session.get_expiry_age(), idle_timeout_seconds(request))})


def home_view(request):
    """Public landing page. ?batch=<number> jumps to that batch's public page or shows a not-found note; signed-in users go to the dashboard."""
    trace_query = request.GET.get('batch', '').strip()[:100]
    if trace_query:
        batch_number = Batch.objects.filter(batch_number__iexact=trace_query).values_list('batch_number', flat=True).first()
        if batch_number:
            return redirect('batch_public_info', batch_number=batch_number)
    elif request.user.is_authenticated:
        return redirect('dashboard')

    return render(request, 'home.html', {
        'trace_query': trace_query,
        'trace_not_found': bool(trace_query),
    })


@login_required
def warehouse_inventory_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'manual_receive':
            if not request.user.has_perm(ADJUST_PHYSICAL_STOCK):
                messages.error(request, "Permission Denied: You cannot manually adjust stock.")
                return redirect('warehouse_inventory')
            
            material_id = request.POST.get('material_id')
            product_id = request.POST.get('product_id')
            qty = request.POST.get('quantity')
            wh_id = request.POST.get('warehouse_id')
            expiry = request.POST.get('expiry_date')
            
            qty, err = read_decimal(request.POST, 'quantity', "Quantity", above=0)
            expiry_date, date_err = read_date(request.POST, 'expiry_date', "Expiry date")
            err = err or date_err
            if not err and bool(material_id) == bool(product_id):
                err = "Choose one material or one product (not both)."
            if not err and not Warehouse.objects.filter(id=wh_id or None).exists():
                err = "Choose the facility the stock was received into."
            if not err and not may_use_warehouse(request.user, wh_id):
                err = FACILITY_REFUSED
            if err:
                messages.error(request, err)
                return redirect('warehouse_inventory')
            expiry = expiry_date
            # Suffix keeps same-day batch numbers unique
            import uuid
            suffix = uuid.uuid4().hex[:4].upper()

            try:
                log_material = None
                if material_id:
                    mat = get_object_or_404(Material, id=material_id)
                    log_material = mat
                    b = Batch.objects.create(
                        batch_number=f"M-ADJ-{mat.sku}-{date.today().strftime('%Y%m%d')}-{suffix}",
                        status='Active',
                        material=mat,
                        quantity=qty,
                        manufacturing_date=date.today(),
                        expiry_date=expiry if expiry else date.today() + timedelta(days=mat.safe_storage_days or 365),
                        warehouse_id=wh_id,
                        location="Receiving Bay"
                    )
                    log_item = mat.name
                elif product_id:
                    prod = get_object_or_404(Product, id=product_id)
                    b = Batch.objects.create(
                        batch_number=f"P-ADJ-{prod.sku}-{date.today().strftime('%Y%m%d')}-{suffix}",
                        status='Active',
                        product=prod,
                        quantity=qty,
                        manufacturing_date=date.today(),
                        expiry_date=expiry if expiry else date.today() + timedelta(days=prod.shelf_life_days or 365),
                        warehouse_id=wh_id,
                        location="Receiving Bay"
                    )
                    log_item = prod.name
                else:
                    messages.error(request, "Must select either Material or Product.")
                    return redirect('warehouse_inventory')
                    
                RegistryLog.objects.create(
                    action_type='Adjusted',
                    item_name=f"Manual Receipt of {log_item}",
                    material=log_material,
                    quantity_changed=qty,
                    warehouse_id=wh_id,
                    user=request.user
                )
                messages.success(request, f"Successfully received {qty} of {log_item}.")
            except Exception as e:
                messages.error(request, f"Error receiving stock: {e}")
            return redirect('warehouse_inventory')

    warehouses = Warehouse.objects.all().order_by('name')
    limited = limited_warehouse_ids(request.user)
    if limited is not None:
        warehouses = warehouses.filter(id__in=limited)
    warehouse_id = request.GET.get('warehouse_id')
    material_id = request.GET.get('material_id')
    selected_warehouse = None
    selected_material = None
    batches = []
    global_kpis = None

    if material_id:
        try:
            selected_material = Material.objects.get(id=material_id)
        except Material.DoesNotExist:
            pass

    if warehouse_id:
        try:
            selected_warehouse = warehouses.get(id=warehouse_id)
            batches = Batch.objects.filter(warehouse=selected_warehouse,
status='Active').select_related('warehouse',
                'material', 'product'
            ).order_by('location', '-manufacturing_date')
        except Warehouse.DoesNotExist:
            pass

    if not selected_warehouse:
        batches = Batch.objects.filter(status='Active').select_related(
            'material', 'product'
        ).order_by('-manufacturing_date')
        if limited is not None:
            batches = batches.filter(warehouse_id__in=limited)

    if selected_material:
        batches = batches.filter(material=selected_material)

    search_query = request.GET.get('q', '').strip()
    if search_query:
        batches = batches.filter(
            Q(batch_number__icontains=search_query)
            | Q(material__sku__icontains=search_query) | Q(material__name__icontains=search_query)
            | Q(product__sku__icontains=search_query) | Q(product__name__icontains=search_query)
            | Q(location__icontains=search_query)
        )

    # Count after all filters, before pagination (Active Batches KPI and "Showing X of Y").
    total_batches = batches.count()

    if not selected_warehouse:
        from django.db.models import Sum
        total_cap = warehouses.aggregate(t=Sum('total_capacity_mt'))['t'] or 0
        global_kpis = {
            'total_warehouses': warehouses.count(),
            'total_capacity': total_cap,
            'total_batches': total_batches,
        }

    can_adjust = request.user.has_perm(ADJUST_PHYSICAL_STOCK)

    # days_until_expiry is a Python property; sort on expiry_date instead.
    sort_fields = {
        'location': 'location',
        'batch_number': 'batch_number',
        'quantity': 'quantity',
        'expiry_date': 'expiry_date',
        'manufacturing_date': 'manufacturing_date',
    }
    if not selected_warehouse:
        sort_fields['warehouse'] = 'warehouse__name'
    default_sort = 'location' if selected_warehouse else '-manufacturing_date'
    batches, sort_ctx = apply_list_sort(request, batches, sort_fields, default=default_sort)
    page_size = get_page_size(request)
    batches = Paginator(batches, page_size).get_page(request.GET.get('page'))

    context = {
        'warehouses': warehouses,
        'materials': Material.objects.all().order_by('name'),
        'products': Product.objects.all().order_by('name'),
        'selected_warehouse': selected_warehouse,
        'selected_material': selected_material,
        'batches': batches,
        'total_batches': total_batches,
        'global_kpis': global_kpis,
        'can_adjust': can_adjust,
        'search_query': search_query,
        'page_size': page_size,
        **sort_ctx,
    }
    return render(request, 'warehouse_inventory.html', context)

def _batch_qa_context(user, batch):
    """QA panel data for the batch page: history, inspections, open request, pending approval, trace and what the user may do."""
    from . import qa
    from .models import QA_REASON_CHOICES
    from .permissions import REQUEST_QA, PERFORM_QA, DISPOSE_STOCK, APPROVE_QA_WRITEOFFS
    is_open = batch.status in qa.OPEN_STATUSES
    undo_until = qa.undo_deadline(batch) if user.has_perm(APPROVE_QA_WRITEOFFS) else None
    return {
        'qa_events': batch.qa_events.select_related('user', 'request')[:50],
        'qa_inspections': batch.inspections.select_related('inspector')[:10],
        'qa_open_request': batch.qa_requests.filter(status__in=('Open', 'In Review')).select_related('requested_by').first(),
        'qa_pending_approval': batch.qa_approvals.filter(status='Pending').select_related('requested_by').first(),
        'qa_trace': qa.trace(batch),
        'qa_reasons': QA_REASON_CHOICES,
        'qa_checks': qa.INSPECTION_CHECKS,
        'qa_expired': qa.is_expired(batch),
        'qa_cap_days': qa.extension_cap_days(batch),
        'can_request_qa': is_open and user.has_perm(REQUEST_QA),
        'can_qa': is_open and user.has_perm(PERFORM_QA),
        'can_dispose': is_open and user.has_perm(DISPOSE_STOCK),
        'can_trace_hold': batch.material_id is not None and user.has_perm(PERFORM_QA),
        'qa_undo_until': undo_until if undo_until and undo_until >= timezone.localdate() else None,
    }


@login_required
def batch_detail_view(request, batch_number):
    from django.shortcuts import get_object_or_404, redirect
    denied = action_denied(request, 'batch_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    batch = get_object_or_404(Batch.objects.select_related(
        'material', 'product', 'purchase_order', 'produced_in', 'produced_in__manufacturing_plant'
    ), batch_number=batch_number)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_batch' and not may_use_warehouse(request.user, batch.warehouse_id):
            messages.error(request, FACILITY_REFUSED)
            return redirect('batch_detail', batch_number=batch.batch_number)
        if action == 'update_batch':
            new_status = request.POST.get('status')
            new_expiry = request.POST.get('expiry_date')
            new_location_id = request.POST.get('location_id')
            
            changes = []
            # Status only changes through the QA actions (hold, inspect/release, write-off), which record a reason.
            if new_status and new_status != batch.status:
                messages.error(request, "Change a batch's status with the QA actions on this page (Put on hold, "
                                        "Inspect / release, Write off), so the reason is recorded.")
                return redirect('batch_detail', batch_number=batch.batch_number)

            expiry_note = None
            if new_expiry:
                parsed_expiry, err = read_date(request.POST, 'expiry_date', "Expiry date")
                if err:
                    messages.error(request, err)
                    return redirect('batch_detail', batch_number=batch.batch_number)
                if batch.expiry_date != parsed_expiry:
                    if batch.expiry_date and parsed_expiry > batch.expiry_date:
                        messages.error(request, "A later expiry date is a shelf-life extension. Use Extend shelf life, "
                                                "so it's checked against the limit and the reason is recorded.")
                        return redirect('batch_detail', batch_number=batch.batch_number)
                    expiry_note = f"Expiry corrected from {batch.expiry_date} to {parsed_expiry}"
                    changes.append(f"Expiry updated to {new_expiry}")
                    batch.expiry_date = parsed_expiry

            new_location = request.POST.get('location')
            if new_location is not None:
                new_location = new_location.strip()
                if batch.location != new_location:
                    batch.location = new_location
                    changes.append("Location updated")

            if 'rental_rate_per_mt' in request.POST:
                raw_rate = request.POST.get('rental_rate_per_mt', '').strip()
                try:
                    new_rate = Decimal(raw_rate) if raw_rate else None
                except InvalidOperation:
                    new_rate = batch.rental_rate_per_mt
                    messages.error(request, "Invalid rental rate — leaving it unchanged.")
                if new_rate is not None and new_rate < 0:
                    new_rate = batch.rental_rate_per_mt
                    messages.error(request, "Rental rate can't be negative — leaving it unchanged.")
                if batch.rental_rate_per_mt != new_rate:
                    changes.append(
                        f"Rental rate set to RM{new_rate}/MT" if new_rate is not None
                        else "Rental rate cleared (now tracks warehouse rate)"
                    )
                    batch.rental_rate_per_mt = new_rate

            if changes:
                batch.save()
                RegistryLog.objects.create(
                    action_type='Adjusted',
                    item_name=f"{batch.batch_number} — {', '.join(changes)}",
                    quantity_changed=0,
                    warehouse=batch.warehouse if batch.warehouse else None,
                    user=request.user,
                )
            if expiry_note:
                from .qa import log_event
                log_event(batch, 'expiry_corrected', request.user, note=expiry_note)
            if changes:
                messages.success(request, "Batch details updated successfully.")
            return redirect('batch_detail', batch_number=batch.batch_number)

    logs = RegistryLog.objects.filter(item_name__icontains=batch.batch_number).select_related('user', 'warehouse').order_by('-timestamp')

    reservations = []
    for alloc in StockAllocation.objects.filter(batch=batch).select_related('sales_order', 'production_run', 'shipment'):
        if alloc.sales_order:
            reservations.append({'quantity': alloc.quantity, 'label': f"Sales Order {alloc.sales_order.so_number}", 'type': 'Sales Order', 'url_name': 'so_detail', 'url_pk': alloc.sales_order.pk})
        elif alloc.production_run:
            reservations.append({'quantity': alloc.quantity, 'label': f"Production Run {alloc.production_run.run_number}", 'type': 'Production', 'url_name': 'production_run_detail', 'url_pk': alloc.production_run.pk})
        elif alloc.shipment:
            reservations.append({'quantity': alloc.quantity, 'label': f"Shipment {alloc.shipment.tracking_number}", 'type': 'Shipment', 'url_name': 'shipment_detail', 'url_pk': alloc.shipment.pk})
        else:
            reservations.append({'quantity': alloc.quantity, 'label': "Unlinked reservation", 'type': '—', 'url_name': None, 'url_pk': None})

    shipment_history = []
    for si in batch.shipment_items.select_related('shipment').order_by('-shipment__dispatch_date', '-id'):
        shipment_history.append({
            'shipment': si.shipment,
            'quantity': si.quantity,
            'received_quantity': si.received_quantity,
            'date_confirmed': si.date_confirmed,
        })

    unit_cost = None
    if batch.material:
        unit_cost = batch.material.cost_per_unit
    elif batch.product:
        unit_cost = batch.product.price_per_unit
    est_value = (unit_cost * batch.quantity) if unit_cost is not None else None

    context = {
        'batch': batch,
        'logs': logs,
        'reservations': reservations,
        **_batch_qa_context(request.user, batch),
        'shipment_history': shipment_history,
        'unit_cost': unit_cost,
        'est_value': est_value,
        'available_quantity': batch.quantity - batch.allocated_quantity,
    }
    return render(request, 'batch_detail.html', context)


class WarehouseForm(forms.ModelForm):
    class Meta:
        model = Warehouse
        fields = ['name', 'location_type', 'ownership_type', 'rental_billing_method', 'rental_cost_per_mt', 'total_capacity_mt']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Port Klang Hub B', 'required': 'required'}),
            'location_type': forms.Select(attrs={'class': 'form-select', 'required': 'required'}),
            'ownership_type': forms.Select(attrs={'class': 'form-select', 'id': 'id_ownership_type', 'required': 'required'}),
            'rental_billing_method': forms.Select(attrs={'class': 'form-select', 'id': 'id_rental_billing_method'}),
            'rental_cost_per_mt': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'id': 'id_rental_cost_per_mt'}),
            'total_capacity_mt': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.1', 'required': 'required'}),
        }

    def clean_rental_cost_per_mt(self):
        rate = self.cleaned_data.get('rental_cost_per_mt')
        if rate is not None and rate < 0:
            raise forms.ValidationError("Rent can't be negative.")
        return rate

    def clean_total_capacity_mt(self):
        capacity = self.cleaned_data.get('total_capacity_mt')
        if capacity is not None and capacity <= 0:
            raise forms.ValidationError("Capacity must be more than 0.")
        return capacity


@login_required
def warehouse_create_view(request):
    if not request.user.has_perm('core.add_warehouse'):
        messages.error(request, "Permission Denied: You do not have permissions to add a new facility.")
        return redirect('warehouse_list')

    if request.method == 'POST':
        form = WarehouseForm(request.POST)
        if form.is_valid():
            warehouse = form.save()
            messages.success(request, f"Facility '{warehouse.name}' was successfully registered.")
            return redirect('warehouse_list')
        else:
            messages.error(request, "Please correct the errors in the form below.")
    else:
        form = WarehouseForm()

    return render(request, 'warehouse_form.html', {'form': form})



@login_required
def warehouse_edit_view(request, pk):
    warehouse = get_object_or_404(Warehouse, pk=pk)
    
    if not request.user.has_perm('core.change_warehouse'):
        messages.error(request, "Permission Denied: You do not have permissions to edit a facility.")
        return redirect('warehouse_list')
    if not may_use_warehouse(request.user, warehouse.id):
        messages.error(request, FACILITY_REFUSED)
        return redirect('warehouse_list')

    if request.method == 'POST':
        form = WarehouseForm(request.POST, instance=warehouse)
        if form.is_valid():
            warehouse = form.save()
            messages.success(request, f"Facility '{warehouse.name}' was successfully updated.")
            return redirect('warehouse_list')
        else:
            messages.error(request, "Please correct the errors in the form below.")
    else:
        form = WarehouseForm(instance=warehouse)

    return render(request, 'warehouse_form.html', {
        'form': form,
        'warehouse': warehouse,
        'edit_mode': True
    })



@login_required
def facility_management_view(request):
    """Facility Management — overview of all warehouse facilities with capacity, cost, and zone data."""
    from .analytics import warehouse_rent_burn

    warehouses = list(Warehouse.objects.annotate(zone_count=Count('locations', distinct=True)).order_by('name'))
    rent_by_id = {r['warehouse_id']: r for r in warehouse_rent_burn()}

    batch_counts = {
        row['warehouse_id']: row['cnt']
        for row in (Batch.objects.filter(status='Active', warehouse__isnull=False)
                    .values('warehouse_id').annotate(cnt=Count('id')))
    }

    facility_list = []
    total_capacity = 0.0
    total_used = 0.0
    total_daily_cost = 0.0

    for w in warehouses:
        rent_row = rent_by_id.get(w.id, {'used_mt': 0.0, 'daily_cost': 0.0})
        used = rent_row['used_mt']
        cap = float(w.total_capacity_mt)
        total_capacity += cap
        total_used += used
        util = (used / cap * 100) if cap > 0 else 0.0

        daily_cost = rent_row['daily_cost']
        total_daily_cost += daily_cost

        facility_list.append({
            'id': w.id,
            'name': w.name,
            'location_type': w.get_location_type_display(),
            'ownership_type': w.get_ownership_type_display(),
            'raw_ownership': w.ownership_type,
            'capacity_mt': cap,
            'used_mt': used,
            'quarantined_mt': rent_row.get('quarantined_mt', 0.0),
            'utilization': util,
            'daily_cost': daily_cost,
            'billing_method': w.get_rental_billing_method_display(),
            'cost_per_mt': float(w.rental_cost_per_mt),
            'zone_count': w.zone_count,
            'batch_count': batch_counts.get(w.id, 0),
        })

    global_util = (total_used / total_capacity * 100) if total_capacity > 0 else 0.0

    context = {
        'facility_list': facility_list,
        'total_facilities': len(facility_list),
        'total_capacity': total_capacity,
        'total_used': total_used,
        'global_utilization': global_util,
        'total_daily_cost': total_daily_cost,
    }
    return render(request, 'warehouse_list.html', context)


# Stock audit (tally)
@login_required
def stock_audit_view(request):
    denied = action_denied(request, 'stock_audit')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            batch_id = request.POST.get('batch_id')
            try:
                actual_qty, err = read_decimal(request.POST, 'actual_quantity', "Counted quantity", min_value=0)
                if err:
                    messages.error(request, err)
                    return redirect('stock_audit')
                batch = get_object_or_404(Batch, id=batch_id)
                if not may_use_warehouse(request.user, batch.warehouse_id):
                    messages.error(request, FACILITY_REFUSED)
                    return redirect('stock_audit')
                if batch.status in ('Depleted', 'Spoiled'):
                    messages.error(request, f"Batch {batch.batch_number} is {batch.get_status_display().lower()}, so it can't be counted.")
                    return redirect('stock_audit')
                open_audit = StockAudit.objects.filter(batch=batch, status='Pending').first()
                if open_audit:
                    messages.error(request, f"Batch {batch.batch_number} already has a pending count (audit #{open_audit.id}). Apply or reject it before logging another.")
                    return redirect('stock_audit')
                StockAudit.objects.create(
                    batch=batch,
                    expected_quantity=batch.quantity,
                    actual_quantity=actual_qty,
                    auditor=request.user,
                    status='Pending',
                    source='WebForm'
                )
                messages.success(request, f"Stock audit discrepancy logged for {batch.batch_number}.")
            except (ValueError, TypeError):
                messages.error(request, "Invalid quantity provided.")
        elif action == 'resolve':
            audit_id = request.POST.get('audit_id')
            audit = get_object_or_404(StockAudit, id=audit_id)
            if not may_use_warehouse(request.user, audit.batch.warehouse_id):
                messages.error(request, FACILITY_REFUSED)
                return redirect('stock_audit')
            if audit.status == 'Pending':
                from .utils import trim_batch_reservations
                with transaction.atomic():
                    b = Batch.objects.select_for_update().get(pk=audit.batch_id)
                    # Stock moved since the count, so applying it would undo that movement
                    if b.quantity != audit.expected_quantity:
                        messages.error(request, f"Batch {b.batch_number} has changed since audit #{audit.id} was counted "
                                                f"({audit.expected_quantity} then, {b.quantity} now). Reject it and count again.")
                        return redirect('stock_audit')
                    problem = trim_batch_reservations(b, audit.actual_quantity, request.user, f"stock count found only {audit.actual_quantity}")
                    if problem:
                        messages.error(request, problem)
                        return redirect('stock_audit')
                    variance = audit.variance
                    audit.status = 'Resolved'
                    audit.reviewed_by = request.user
                    audit.reviewed_at = timezone.now()
                    audit.save()

                    b.quantity = audit.actual_quantity
                    b.save()

                    wh = b.warehouse if b.warehouse else None
                    RegistryLog.objects.create(
                        action_type='Adjusted',
                        item_name=f"Batch {b.batch_number} ({b.material or b.product})",
                        material=b.material,
                        quantity_changed=variance,
                        warehouse=wh,
                        user=request.user
                    )
                messages.success(request, f"Audit #{audit.id} resolved. Physical stock updated.")
        elif action == 'reject':
            audit = get_object_or_404(StockAudit, id=request.POST.get('audit_id'))
            if not may_use_warehouse(request.user, audit.batch.warehouse_id):
                messages.error(request, FACILITY_REFUSED)
                return redirect('stock_audit')
            reason = request.POST.get('reason', '').strip()
            if audit.status != 'Pending':
                messages.error(request, f"Audit #{audit.id} is already {audit.status.lower()}.")
            elif not reason:
                messages.error(request, "Give a reason for rejecting the count.")
            else:
                # Stock is left as it is; the count is kept on record with who rejected it and why.
                audit.status = 'Rejected'
                audit.reviewed_by = request.user
                audit.reviewed_at = timezone.now()
                audit.review_note = reason
                audit.save()
                messages.success(request, f"Audit #{audit.id} rejected. Stock was not changed.")

        return redirect('stock_audit')

    audits = StockAudit.objects.select_related('batch', 'batch__material', 'batch__product', 'batch__warehouse',
                                               'auditor', 'reviewed_by').order_by('-id')
    active_batches = Batch.objects.filter(status='Active').select_related('material', 'product', 'warehouse')
    # Facility limit: users limited to some facilities only see those facilities' audits and batches
    limited = limited_warehouse_ids(request.user)
    if limited is not None:
        audits = audits.filter(batch__warehouse_id__in=limited)
        active_batches = active_batches.filter(warehouse_id__in=limited)

    pending_count = audits.filter(status='Pending').count()
    resolved_count = audits.filter(status='Resolved').count()
    pending_batch_ids = set(audits.filter(status='Pending').values_list('batch_id', flat=True))
    # Items for the filter on the form and the table: one entry per material/product, by name.
    items = {(b.material or b.product).name for b in active_batches if b.material or b.product}
    items |= {(a.batch.material or a.batch.product).name for a in audits if a.batch.material or a.batch.product}

    context = {
        'pending_batch_ids': pending_batch_ids,
        'item_names': sorted(items),
        'audits': audits,
        'active_batches': active_batches,
        'pending_count': pending_count,
        'resolved_count': resolved_count,
    }
    return render(request, 'stock_audit.html', context)


# Registry ledger
@login_required
def registry_ledger_view(request):
    action_filter = request.GET.get('action')
    search_query = request.GET.get('q', '').strip()
    range_filter = 'all' if request.GET.get('range') == 'all' else '30d'

    # Date range scopes both the table and the KPI cards; search/action only narrow the table.
    base = RegistryLog.objects.all()
    if range_filter == '30d':
        base = base.filter(timestamp__gte=timezone.now() - timedelta(days=30))

    logs = base.select_related('warehouse', 'user').order_by('-timestamp')
    if action_filter:
        logs = logs.filter(action_type=action_filter)
    if search_query:
        logs = logs.filter(Q(item_name__icontains=search_query) | Q(warehouse__name__icontains=search_query))

    logs, sort_ctx = apply_list_sort(request, logs, {
        'timestamp': 'timestamp',
        'action_type': 'action_type',
        'item_name': 'item_name',
        'quantity_changed': 'quantity_changed',
        'warehouse': 'warehouse__name',
        'user': 'user__username',
    }, default='-timestamp')
    page_size = get_page_size(request)
    logs = Paginator(logs, page_size).get_page(request.GET.get('page'))

    stats = {
        'inbound': base.filter(action_type='Inbound').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'outbound': base.filter(action_type='Outbound').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'consumed': base.filter(action_type='Consumed_For_Manufacturing').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'produced': base.filter(action_type='Produced').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'disposed': base.filter(action_type='Spoiled_Disposal').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
    }

    # Toggle links keep q/action/sort/page_size but restart at page 1.
    keep = request.GET.copy()
    keep.pop('page', None)
    keep.pop('range', None)

    context = {
        'logs': logs,
        'action_filter': action_filter,
        'search_query': search_query,
        'range_filter': range_filter,
        'range_qs': keep.urlencode(),
        'range_choices': (('30d', 'Last 30 days'), ('all', 'All time')),
        'stats': stats,
        'action_choices': RegistryLog.ACTION_CHOICES,
        'page_size': page_size,
        **sort_ctx,
    }
    return render(request, 'registry_ledger.html', context)


# Bulk import/export and reference templates

@login_required
def export_product_template(request):
    """Download reference CSV template for Products."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="product_import_template.csv"'
    writer = csv.writer(response)
    writer.writerow(['name', 'sku', 'description', 'unit_of_measure', 'weight_mt_per_unit', 'price_per_unit', 'shelf_life_days'])
    writer.writerow(['Polymer Compound Alpha', 'PROD1001', 'High density industrial resin', 'pcs', '0.5000', '150.00', '365'])
    writer.writerow(['Bio-Solvent Solution', 'PROD1002', 'Organic chemical solvent', 'L', '1.0000', '85.50', '180'])
    writer.writerow(['Composite Sheet Grade B', '', 'Standard structural panel (Auto SKU)', 'pcs', '0.2500', '45.00', ''])
    return response


@login_required
def export_products_csv(request):
    """Export all registered Products as CSV."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="products_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['name', 'sku', 'description', 'unit_of_measure', 'weight_mt_per_unit', 'price_per_unit', 'shelf_life_days'])
    for prod in Product.objects.all().order_by('sku'):
        writer.writerow([
            prod.name,
            prod.sku,
            prod.description or '',
            prod.unit_of_measure,
            f"{prod.weight_mt_per_unit:.4f}",
            f"{prod.price_per_unit:.2f}",
            prod.shelf_life_days or '',
        ])
    return response


@login_required
def import_products(request):
    """Bulk import Products from uploaded CSV with dry-run, anti-duplication, and error reporting."""
    if request.method != 'POST':
        return redirect('product_list')
    if not request.user.has_perm('core.add_product'):
        messages.error(request, "You don't have permission to import products.")
        return redirect('product_list')

    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        messages.error(request, "Please select a valid CSV file to upload.")
        return redirect('product_list')

    if not csv_file.name.endswith('.csv'):
        messages.error(request, "File format not supported. Please upload a standard .csv file.")
        return redirect('product_list')

    duplicate_mode = request.POST.get('duplicate_mode', 'skip')
    is_dry_run = request.POST.get('dry_run') == '1'

    try:
        file_data = csv_file.read().decode('utf-8-sig')
        io_string = io.StringIO(file_data)
        reader = csv.DictReader(io_string)
    except Exception as e:
        messages.error(request, f"Could not read CSV file: {e}")
        return redirect('product_list')

    if not reader.fieldnames:
        messages.error(request, "CSV file is empty or missing headers.")
        return redirect('product_list')

    headers = [h.strip().lower() for h in reader.fieldnames if h]
    if 'name' not in headers:
        messages.error(request, "CSV header missing required 'name' column.")
        return redirect('product_list')

    valid_uoms = {'mt', 'kg', 'l', 'g', 'pcs'}
    existing_skus = {p.sku.upper(): p for p in Product.objects.all()}
    existing_names = {p.name.strip().upper(): p for p in Product.objects.all()}

    seen_skus_in_file = set()
    seen_names_in_file = set()

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_rows = []
    dry_run_results = []

    rows = list(reader)

    with transaction.atomic():
        for idx, raw_row in enumerate(rows, start=2):
            row = {k.strip().lower(): (v.strip() if v else '') for k, v in raw_row.items() if k}
            
            name = row.get('name', '')
            sku = row.get('sku', '').upper()
            description = row.get('description', '')
            uom = row.get('unit_of_measure', 'pcs')
            weight_str = row.get('weight_mt_per_unit', '1.0')
            price_str = row.get('price_per_unit', '0.0')
            shelf_str = row.get('shelf_life_days', '')

            # 1. Validation
            if not name:
                err = f"Line {idx}: Missing product name."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name or '—', 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            if uom.lower() not in valid_uoms:
                err = f"Line {idx}: Invalid unit of measure '{uom}'. Valid: MT, kg, L, g, pcs."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            fixed = {k.lower(): v for k, v in UNIT_WEIGHT_MT.items()}.get(uom.lower())
            try:
                # MT, kg and g fix their own weight; pcs and L need weight_mt_per_unit in the file
                weight = float(fixed) if fixed is not None else float(weight_str)
                if weight <= 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: weight_mt_per_unit must be a number above 0 for pcs and L."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            try:
                price = float(price_str) if price_str else 0.0
                if price < 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: Price/Unit must be a non-negative number."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            try:
                shelf = int(shelf_str) if shelf_str else None
                if shelf is not None and shelf <= 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: Shelf life must be a whole number of days, or blank."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            # Intra-file duplicate check
            clean_name_key = name.strip().upper()
            if sku and sku in seen_skus_in_file:
                err = f"Line {idx}: Duplicate SKU '{sku}' within CSV file."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku, 'status': 'error', 'msg': err})
                continue

            if clean_name_key in seen_names_in_file and not sku:
                err = f"Line {idx}: Duplicate product name '{name}' within CSV file."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            # DB Duplicate check
            db_match = None
            if sku and sku in existing_skus:
                db_match = existing_skus[sku]
            elif clean_name_key in existing_names:
                db_match = existing_names[clean_name_key]

            if db_match:
                if duplicate_mode == 'skip':
                    skipped_count += 1
                    msg = f"Line {idx}: Skipped duplicate item '{name}' (SKU: {db_match.sku})."
                    dry_run_results.append({'line': idx, 'name': name, 'sku': db_match.sku, 'status': 'duplicate', 'msg': msg})
                    continue
                else: # 'update'
                    if not is_dry_run:
                        db_match.name = name
                        if description: db_match.description = description
                        db_match.unit_of_measure = uom
                        db_match.weight_mt_per_unit = weight
                        db_match.price_per_unit = price
                        if shelf is not None: db_match.shelf_life_days = shelf
                        db_match.save()
                    updated_count += 1
                    msg = f"Line {idx}: Updated existing product '{name}' (SKU: {db_match.sku})."
                    dry_run_results.append({'line': idx, 'name': name, 'sku': db_match.sku, 'status': 'updated', 'msg': msg})
                    continue

            # Create New Product
            if not sku:
                sku = generate_next_code(Product, 'sku', 'PROD', 1001, pad=4)
                while sku in existing_skus or sku in seen_skus_in_file:
                    seq = int(sku.replace('PROD', '')) + 1
                    sku = f"PROD{seq:04d}"

            if not is_dry_run:
                p = Product.objects.create(
                    name=name, sku=sku, description=description,
                    unit_of_measure=uom, weight_mt_per_unit=weight, price_per_unit=price, shelf_life_days=shelf
                )
                existing_skus[sku.upper()] = p
                existing_names[clean_name_key] = p

            if sku: seen_skus_in_file.add(sku.upper())
            seen_names_in_file.add(clean_name_key)
            created_count += 1
            msg = f"Line {idx}: Ready to create product '{name}' (SKU: {sku})."
            dry_run_results.append({'line': idx, 'name': name, 'sku': sku, 'status': 'created', 'msg': msg})

        if is_dry_run:
            transaction.set_rollback(True)

    if is_dry_run:
        return JsonResponse({
            'success': True,
            'dry_run': True,
            'created_count': created_count,
            'updated_count': updated_count,
            'skipped_count': skipped_count,
            'error_count': len(error_rows),
            'results': dry_run_results
        })

    if created_count > 0 or updated_count > 0:
        RegistryLog.objects.create(
            action_type='Adjusted',
            item_name=f"Bulk Import Products ({created_count} created, {updated_count} updated, {skipped_count} skipped)",
            quantity_changed=created_count + updated_count,
            warehouse=None,
            user=request.user if request.user.is_authenticated else None
        )

    summary_msg = f"Product import completed: {created_count} created, {updated_count} updated, {skipped_count} skipped duplicates."
    if error_rows:
        summary_msg += f" {len(error_rows)} row(s) had validation errors."
        messages.warning(request, summary_msg)
    else:
        messages.success(request, summary_msg)

    return redirect('product_list')


@login_required
def export_material_template(request):
    """Download reference CSV template for Materials."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="material_import_template.csv"'
    writer = csv.writer(response)
    writer.writerow(['name', 'sku', 'category', 'unit_of_measure', 'safe_storage_days', 'weight_mt_per_unit', 'cost_per_unit'])
    writer.writerow(['Titanium Dioxide Pigment', 'MAT1001', 'Chemicals', 'MT', '90', '1.0000', '450.00'])
    writer.writerow(['Recycled Polyethylene Pellets', 'MAT1002', 'Polymers', 'kg', '180', '0.0010', '2.50'])
    writer.writerow(['Organic Catalyst Fluid', '', 'Additives (Auto SKU)', 'L', '60', '0.0010', '12.00'])
    return response


@login_required
def export_materials_csv(request):
    """Export all registered Materials as CSV."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="materials_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['name', 'sku', 'category', 'unit_of_measure', 'safe_storage_days', 'weight_mt_per_unit', 'cost_per_unit'])
    for mat in Material.objects.all().order_by('sku'):
        writer.writerow([
            mat.name,
            mat.sku,
            mat.category,
            mat.unit_of_measure,
            mat.safe_storage_days,
            f"{mat.weight_mt_per_unit:.4f}",
            f"{mat.cost_per_unit:.2f}"
        ])
    return response


@login_required
def import_materials(request):
    """Bulk import Materials from uploaded CSV with dry-run, anti-duplication, and error reporting."""
    if request.method != 'POST':
        return redirect('material_list')
    if not request.user.has_perm('core.add_material'):
        messages.error(request, "You don't have permission to import materials.")
        return redirect('material_list')

    csv_file = request.FILES.get('csv_file')
    if not csv_file:
        messages.error(request, "Please select a valid CSV file to upload.")
        return redirect('material_list')

    if not csv_file.name.endswith('.csv'):
        messages.error(request, "File format not supported. Please upload a standard .csv file.")
        return redirect('material_list')

    duplicate_mode = request.POST.get('duplicate_mode', 'skip')
    is_dry_run = request.POST.get('dry_run') == '1'

    try:
        file_data = csv_file.read().decode('utf-8-sig')
        io_string = io.StringIO(file_data)
        reader = csv.DictReader(io_string)
    except Exception as e:
        messages.error(request, f"Could not read CSV file: {e}")
        return redirect('material_list')

    if not reader.fieldnames:
        messages.error(request, "CSV file is empty or missing headers.")
        return redirect('material_list')

    headers = [h.strip().lower() for h in reader.fieldnames if h]
    if 'name' not in headers:
        messages.error(request, "CSV header missing required 'name' column.")
        return redirect('material_list')

    valid_uoms = {'mt', 'kg', 'l', 'g', 'pcs'}
    existing_skus = {m.sku.upper(): m for m in Material.objects.all()}
    existing_names = {m.name.strip().upper(): m for m in Material.objects.all()}

    seen_skus_in_file = set()
    seen_names_in_file = set()

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_rows = []
    dry_run_results = []

    rows = list(reader)

    with transaction.atomic():
        for idx, raw_row in enumerate(rows, start=2):
            row = {k.strip().lower(): (v.strip() if v else '') for k, v in raw_row.items() if k}
            
            name = row.get('name', '')
            sku = row.get('sku', '').upper()
            category = row.get('category', 'General')
            uom = row.get('unit_of_measure', 'MT')
            days_str = row.get('safe_storage_days', '90')
            weight_str = row.get('weight_mt_per_unit', '1.0')
            cost_str = row.get('cost_per_unit', '0.0')

            # 1. Validation
            if not name:
                err = f"Line {idx}: Missing material name."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name or '—', 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            if uom.lower() not in valid_uoms:
                err = f"Line {idx}: Invalid unit of measure '{uom}'. Valid: MT, kg, L, g, pcs."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            try:
                days = int(days_str) if days_str else 90
                if days < 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: Safe storage days must be a non-negative integer."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            fixed = {k.lower(): v for k, v in UNIT_WEIGHT_MT.items()}.get(uom.lower())
            try:
                # MT, kg and g fix their own weight; pcs and L need weight_mt_per_unit in the file
                weight = float(fixed) if fixed is not None else float(weight_str)
                if weight <= 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: weight_mt_per_unit must be a number above 0 for pcs and L."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            try:
                cost = float(cost_str) if cost_str else 0.0
                if cost < 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: Cost/Unit must be a non-negative number."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            # Intra-file duplicate check
            clean_name_key = name.strip().upper()
            if sku and sku in seen_skus_in_file:
                err = f"Line {idx}: Duplicate SKU '{sku}' within CSV file."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku, 'status': 'error', 'msg': err})
                continue

            if clean_name_key in seen_names_in_file and not sku:
                err = f"Line {idx}: Duplicate material name '{name}' within CSV file."
                error_rows.append({'line': idx, 'row': raw_row, 'error': err})
                dry_run_results.append({'line': idx, 'name': name, 'sku': sku or 'Auto', 'status': 'error', 'msg': err})
                continue

            # DB Duplicate check
            db_match = None
            if sku and sku in existing_skus:
                db_match = existing_skus[sku]
            elif clean_name_key in existing_names:
                db_match = existing_names[clean_name_key]

            if db_match:
                if duplicate_mode == 'skip':
                    skipped_count += 1
                    msg = f"Line {idx}: Skipped duplicate material '{name}' (SKU: {db_match.sku})."
                    dry_run_results.append({'line': idx, 'name': name, 'sku': db_match.sku, 'status': 'duplicate', 'msg': msg})
                    continue
                else: # 'update'
                    if not is_dry_run:
                        db_match.name = name
                        db_match.category = category or db_match.category
                        db_match.unit_of_measure = uom
                        db_match.safe_storage_days = days
                        db_match.weight_mt_per_unit = weight
                        db_match.cost_per_unit = cost
                        db_match.save()
                    updated_count += 1
                    msg = f"Line {idx}: Updated existing material '{name}' (SKU: {db_match.sku})."
                    dry_run_results.append({'line': idx, 'name': name, 'sku': db_match.sku, 'status': 'updated', 'msg': msg})
                    continue

            # Create New Material
            if not sku:
                sku = generate_next_code(Material, 'sku', 'MAT', 1001, pad=4)
                while sku in existing_skus or sku in seen_skus_in_file:
                    seq = int(sku.replace('MAT', '')) + 1
                    sku = f"MAT{seq:04d}"

            if not is_dry_run:
                m = Material.objects.create(
                    name=name, sku=sku, category=category or 'General',
                    unit_of_measure=uom, safe_storage_days=days,
                    weight_mt_per_unit=weight, cost_per_unit=cost
                )
                existing_skus[sku.upper()] = m
                existing_names[clean_name_key] = m

            if sku: seen_skus_in_file.add(sku.upper())
            seen_names_in_file.add(clean_name_key)
            created_count += 1
            msg = f"Line {idx}: Ready to create material '{name}' (SKU: {sku})."
            dry_run_results.append({'line': idx, 'name': name, 'sku': sku, 'status': 'created', 'msg': msg})

        if is_dry_run:
            transaction.set_rollback(True)

    if is_dry_run:
        return JsonResponse({
            'success': True,
            'dry_run': True,
            'created_count': created_count,
            'updated_count': updated_count,
            'skipped_count': skipped_count,
            'error_count': len(error_rows),
            'results': dry_run_results
        })

    if created_count > 0 or updated_count > 0:
        RegistryLog.objects.create(
            action_type='Adjusted',
            item_name=f"Bulk Import Materials ({created_count} created, {updated_count} updated, {skipped_count} skipped)",
            quantity_changed=created_count + updated_count,
            warehouse=None,
            user=request.user if request.user.is_authenticated else None
        )

    summary_msg = f"Material import completed: {created_count} created, {updated_count} updated, {skipped_count} skipped duplicates."
    if error_rows:
        summary_msg += f" {len(error_rows)} row(s) had validation errors."
        messages.warning(request, summary_msg)
    else:
        messages.success(request, summary_msg)

    return redirect('material_list')


@login_required
def export_product_recipes_csv(request):
    """Export all Product Recipes as CSV."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="product_recipes_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['product_sku', 'product_name', 'material_sku', 'material_name', 'quantity_required', 'material_uom'])
    for r in ProductRecipe.objects.select_related('product', 'material').order_by('product__sku'):
        writer.writerow([
            r.product.sku,
            r.product.name,
            r.material.sku,
            r.material.name,
            f"{r.quantity_required.normalize():f}",
            r.material.unit_of_measure
        ])
    return response

# Recipe Bulk CSV Import disabled per user request (Recipe Studio UI used instead).


@login_required
def get_product_recipe_api(request, product_id):
    """API endpoint to get full recipe details for a product."""
    product = get_object_or_404(Product, id=product_id)
    items = ProductRecipe.objects.filter(product=product).select_related('material')
    
    recipe_list = []
    total_cost = 0.0
    total_weight = 0.0
    for r in items:
        qty = float(r.quantity_required)
        unit_cost = float(r.material.cost_per_unit)
        item_cost = qty * unit_cost
        total_cost += item_cost
        
        unit_weight = float(r.material.weight_mt_per_unit)
        item_weight = qty * unit_weight
        total_weight += item_weight

        recipe_list.append({
            'id': r.id,
            'material_id': r.material.id,
            'material_sku': r.material.sku,
            'material_name': r.material.name,
            'unit_of_measure': r.material.unit_of_measure,
            'quantity_required': qty,
            'cost_per_unit': unit_cost,
            'total_cost': item_cost,
            'weight_mt_per_unit': unit_weight,
            'total_weight': item_weight,
        })

    return JsonResponse({
        'success': True,
        'product_id': product.id,
        'product_sku': product.sku,
        'product_name': product.name,
        'recipe_items': recipe_list,
        'total_cost': round(total_cost, 2),
        'total_weight': round(total_weight, 4)
    })


def recipe_locked(product):
    """Error message if the product has a run holding materials or under way (its usage form is built from the recipe), else None."""
    runs = list(ProductionRun.objects.filter(
        target_product=product, status__in=('Planned', 'Awaiting Materials', 'InProgress', 'Paused')
    ).values_list('run_number', flat=True))
    if runs:
        return f"{product.sku}'s recipe can't change while run(s) {', '.join(runs)} hold materials or are in progress. Finish, abort or scrap them first."
    return None


def archived_recipe_materials(product):
    """Archived materials still on the product's recipe (runs and SO lines only warn about these)."""
    return [r.material for r in ProductRecipe.objects.filter(product=product, material__is_active=False).select_related('material')]


@login_required
def save_product_recipe_api(request):
    """API endpoint for live inline recipe modifications (add, update, delete, clone)."""
    denied = action_denied(request, 'product_recipe_api')
    if denied:
        return JsonResponse({'success': False, 'error': denied}, status=403)
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST method required.'}, status=400)

    action = request.POST.get('action')
    product_id = request.POST.get('product_id')

    if action == 'delete_item':
        recipe_id = request.POST.get('recipe_id')
        try:
            item = get_object_or_404(ProductRecipe, id=recipe_id)
            locked = recipe_locked(item.product)
            if locked:
                return JsonResponse({'success': False, 'error': locked}, status=400)
            p_id = item.product.id
            item.delete()
            return JsonResponse({'success': True, 'message': 'Recipe item deleted successfully.', 'product_id': p_id})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    elif action == 'clone_recipe':
        source_id = request.POST.get('source_product_id')
        target_id = request.POST.get('target_product_id')
        try:
            source_prod = get_object_or_404(Product, id=source_id)
            target_prod = get_object_or_404(Product, id=target_id)
            locked = recipe_locked(target_prod)
            if locked:
                return JsonResponse({'success': False, 'error': locked}, status=400)

            source_items = ProductRecipe.objects.filter(product=source_prod).select_related('material')
            cloned_count = skipped = 0
            for item in source_items:
                if not item.material.is_active:
                    skipped += 1
                    continue
                ProductRecipe.objects.update_or_create(
                    product=target_prod,
                    material=item.material,
                    defaults={'quantity_required': item.quantity_required}
                )
                cloned_count += 1

            return JsonResponse({
                'success': True,
                'message': f"Successfully cloned {cloned_count} requirement(s) from {source_prod.sku} to {target_prod.sku}."
                           + (f" Skipped {skipped} archived material(s)." if skipped else ""),
                'product_id': target_prod.id
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    elif action == 'save_batch':
        try:
            product = get_object_or_404(Product, id=product_id)
            locked = recipe_locked(product)
            if locked:
                return JsonResponse({'success': False, 'error': locked}, status=400)
            material_ids = request.POST.getlist('material_ids[]')
            quantities = request.POST.getlist('quantities[]')
            
            if not material_ids:
                m_id = request.POST.get('material_id')
                qty = request.POST.get('quantity_required')
                if m_id and qty:
                    material_ids = [m_id]
                    quantities = [qty]

            saved_count = 0
            for m_id, q_val in zip(material_ids, quantities):
                if not m_id or not q_val: continue
                qty = float(q_val)
                if qty <= 0: continue
                mat = get_object_or_404(Material, id=m_id)
                if not mat.is_active:
                    return JsonResponse({'success': False, 'error': f"{mat.name} is archived, so it can't be added to a recipe."}, status=400)
                ProductRecipe.objects.update_or_create(
                    product=product,
                    material=mat,
                    defaults={'quantity_required': qty}
                )
                saved_count += 1

            return JsonResponse({
                'success': True,
                'message': f"Saved {saved_count} recipe requirement(s) for {product.sku}.",
                'product_id': product.id
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    return JsonResponse({'success': False, 'error': 'Invalid action.'}, status=400)


# Products catalog
@login_required
def product_list_view(request):
    denied = action_denied(request, 'product_list')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_product':
            name = request.POST.get('name')
            sku_auto = request.POST.get('sku_auto') == '1'
            sku = request.POST.get('sku')
            if sku_auto or not sku:
                sku = generate_next_code(Product, 'sku', 'PROD', 1001, pad=4)
            description = request.POST.get('description', '')
            uom = request.POST.get('unit_of_measure', 'pcs')
            name = (name or '').strip()
            weight, err = read_unit_weight(request.POST, uom)
            price, price_err = read_decimal(request.POST, 'price_per_unit', "Price", min_value=0, required=False)
            shelf, shelf_err = read_decimal(request.POST, 'shelf_life_days', "Shelf life", above=0, required=False)
            err = err or price_err or shelf_err or (None if name else "Product name is required.")
            if err:
                messages.error(request, err)
                return redirect('product_list')
            price = price if price is not None else Decimal('0')
            try:
                Product.objects.create(
                    name=name, sku=sku, description=description,
                    unit_of_measure=uom, weight_mt_per_unit=weight, price_per_unit=price,
                    shelf_life_days=int(shelf) if shelf is not None else None,
                )
                messages.success(request, f"Product '{name}' (SKU: {sku}) added successfully.")
            except Exception as e:
                messages.error(request, f"Error creating product: {e}")

        elif action == 'add_recipe':
            product_id = request.POST.get('product_id')
            material_id = request.POST.get('material_id')
            qty = request.POST.get('quantity_required')
            try:
                prod = get_object_or_404(Product, id=product_id)
                mat = get_object_or_404(Material, id=material_id)
                qty, err = read_decimal(request.POST, 'quantity_required', "Quantity", above=0)
                err = err or recipe_locked(prod) or (None if mat.is_active else f"{mat.name} is archived, so it can't be added to a recipe.")
                if err:
                    messages.error(request, err)
                    return redirect('product_list')
                # Update the line if the material is already on the recipe
                line, created = ProductRecipe.objects.update_or_create(
                    product=prod, material=mat, defaults={'quantity_required': qty})
                if created:
                    messages.success(request, f"Recipe requirement of {qty} {mat.sku} added for {prod.sku}.")
                else:
                    messages.success(request, f"{mat.sku} was already on the {prod.sku} recipe; its amount is now {qty}.")
            except Exception as e:
                messages.error(request, f"Error adding recipe item: {e}")

        elif action == 'toggle_active':
            if request.user.has_perm('core.change_product'):
                product_id = request.POST.get('product_id')
                prod = get_object_or_404(Product, id=product_id)
                prod.is_active = not prod.is_active
                prod.save()
                messages.success(request, f"Product {prod.sku} is now {'Active' if prod.is_active else 'Archived'}.")
                log_archive_change(request, 'Product', prod)
            else:
                messages.error(request, "Permission denied. Only Managers and Admins can toggle status.")

        return redirect('product_list')

    # One aggregate query for all products' active stock.
    products = Product.objects.prefetch_related('recipe_items__material').annotate(
        active_stock=Sum('batch__quantity', filter=Q(batch__status='Active'))
    ).order_by('name')
    materials = Material.objects.all().order_by('name')

    product_data = []
    for p in products:
        stock = p.active_stock or 0
        product_data.append({'product': p, 'stock_display': format_stock_display(stock, p)})

    context = {
        'products': products,
        'product_data': product_data,
        'materials': materials,
        'next_product_sku': generate_next_code(Product, 'sku', 'PROD', 1001, pad=4),
    }
    return render(request, 'product_list.html', context)


@login_required
def product_detail_view(request, pk):
    denied = action_denied(request, 'product_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    product = get_object_or_404(Product.objects.prefetch_related('recipe_items__material'), pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_product':
            uom = request.POST.get('unit_of_measure', product.unit_of_measure)
            weight, err = read_unit_weight(request.POST, uom)
            if err:
                messages.error(request, err)
                return redirect('product_detail', pk=pk)
            try:
                product.name = request.POST.get('name', product.name)
                product.sku = request.POST.get('sku', product.sku)
                product.description = request.POST.get('description', product.description)
                product.unit_of_measure = uom
                product.weight_mt_per_unit = weight
                product.price_per_unit = float(request.POST.get('price_per_unit', product.price_per_unit))
                if 'shelf_life_days' in request.POST:
                    raw_shelf = request.POST.get('shelf_life_days', '').strip()
                    product.shelf_life_days = int(raw_shelf) if raw_shelf else None
                    if product.shelf_life_days is not None and product.shelf_life_days <= 0:
                        raise ValueError("Shelf life must be at least 1 day, or blank for the 365-day default.")
                product.save()
                messages.success(request, f"Product '{product.sku}' updated successfully.")
            except Exception as e:
                messages.error(request, f"Error updating product: {e}")
        return redirect('product_detail', pk=pk)

    # Inventory Overview
    active_batches = Batch.objects.filter(product=product, status='Active').select_related('warehouse', 'produced_in').order_by('expiry_date')
    total_stock = sum(b.quantity for b in active_batches)
    stock_weight_display = format_stock_display(total_stock, product)

    from .utils import get_batch_reservations, get_batch_produced_for
    batch_rows = []
    for b in active_batches:
        batch_rows.append({
            'batch': b,
            'available': b.quantity - b.allocated_quantity,
            'reservations': get_batch_reservations(b),
            'produced_for': get_batch_produced_for(b),
        })

    # Headline figures: active stock only; quarantined FG shown separately.
    allocated_stock = sum((b.allocated_quantity for b in active_batches), Decimal('0'))
    quarantined_stock = Batch.objects.filter(product=product, status='Quarantined').aggregate(s=Sum('quantity'))['s'] or Decimal('0')

    # Manufacturing History (cancelled runs are noise, as on the order pages)
    runs_qs = ProductionRun.objects.filter(target_product=product).exclude(status='Cancelled')
    production_runs = runs_qs.select_related('supervisor', 'sales_order').order_by('-id')[:10]
    open_runs = runs_qs.exclude(status='Completed').count()

    # Sales vs production per month, same rules as Analytics -> Demand.
    from . import analytics
    today = timezone.localdate()
    chart_labels = analytics._trailing_month_keys(today, analytics.TREND_WINDOW_MONTHS)
    trend = next((r for r in analytics.product_sales_trend(end=today) if r['product_id'] == product.id), None)
    chart_sales_data = trend['monthly_qty'] if trend else [0] * len(chart_labels)
    if trend:
        trend = {**trend, 'label': trend['status'].replace('_', ' ').capitalize()}

    first_month = date(*(int(p) for p in chart_labels[0].split('-')), 1)
    produced_by_month = {k: 0.0 for k in chart_labels}
    for end_time, yielded in runs_qs.filter(status='Completed', actual_yield__isnull=False, exact_end_time__date__gte=first_month - timedelta(days=1)).values_list('exact_end_time', 'actual_yield'):
        if end_time is None:
            continue
        key = timezone.localtime(end_time).strftime('%Y-%m')
        if key in produced_by_month:
            produced_by_month[key] += float(yielded)
    chart_produced_data = [round(produced_by_month[k], 2) for k in chart_labels]

    # Recipe cost and margin (quantity_required is per unit made; material cost is per the material's own unit).
    recipe_rows = []
    unit_cost = Decimal('0')
    for item in product.recipe_items.all():
        line_cost = item.quantity_required * item.material.cost_per_unit
        unit_cost += line_cost
        recipe_rows.append({'item': item, 'line_cost': line_cost})
    margin = product.price_per_unit - unit_cost
    margin_pct = (margin / product.price_per_unit * 100) if product.price_per_unit > 0 else None

    context = {
        'product': product,
        'products': Product.objects.all().order_by('name'),
        'materials': Material.objects.all().order_by('name'),
        'active_batches': active_batches,
        'batch_rows': batch_rows,
        'total_stock': total_stock,
        'stock_weight_display': stock_weight_display,
        'stock_value': total_stock * product.price_per_unit,
        'allocated_stock': allocated_stock,
        'available_stock': total_stock - allocated_stock,
        'quarantined_stock': quarantined_stock,
        'open_runs': open_runs,
        'production_runs': production_runs,
        'chart_labels': chart_labels,
        'chart_sales_data': chart_sales_data,
        'chart_produced_data': chart_produced_data,
        'sales_trend': trend,
        'recipe_rows': recipe_rows,
        'unit_cost': unit_cost,
        'margin': margin,
        'margin_pct': margin_pct,
        'uom_choices': Product.UNIT_CHOICES,
    }
    return render(request, 'product_detail.html', context)


# Materials hub
@login_required
def material_list_view(request):
    denied = action_denied(request, 'material_list')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle_active':
            if request.user.has_perm('core.change_material'):
                material_id = request.POST.get('material_id')
                mat = get_object_or_404(Material, id=material_id)
                mat.is_active = not mat.is_active
                mat.save()
                messages.success(request, f"Material {mat.sku} is now {'Active' if mat.is_active else 'Archived'}.")
                log_archive_change(request, 'Material', mat)
            else:
                messages.error(request, "Permission denied. Only Managers and Admins can toggle status.")
            return redirect('material_list')

        name = request.POST.get('name')
        sku_auto = request.POST.get('sku_auto') == '1'
        sku = request.POST.get('sku')
        if sku_auto or not sku:
            sku = generate_next_code(Material, 'sku', 'MAT', 1001, pad=4)
        category = request.POST.get('category')
        uom = request.POST.get('unit_of_measure', 'MT')
        reorder_point, err = read_decimal(request.POST, 'reorder_point', 'Reorder point', min_value=0, required=False)
        weight, weight_err = read_unit_weight(request.POST, uom)
        err = err or weight_err
        if err:
            messages.error(request, err)
            return redirect('material_list')
        try:
            safe_days = int(request.POST.get('safe_storage_days', 90))
            cost = float(request.POST.get('cost_per_unit', 0.0))
            Material.objects.create(
                name=name, sku=sku, category=category, unit_of_measure=uom,
                safe_storage_days=safe_days, weight_mt_per_unit=weight, cost_per_unit=cost,
                reorder_point=reorder_point, inspect_on_receipt=request.POST.get('inspect_on_receipt') == '1',
            )
            messages.success(request, f"Material '{name}' (SKU: {sku}) registered.")
        except Exception as e:
            messages.error(request, f"Error registering material: {e}")
        return redirect('material_list')

    # One aggregate query for all materials' active stock.
    materials = Material.objects.annotate(
        active_stock=Sum('batch__quantity', filter=Q(batch__status='Active'))
    ).order_by('name')

    material_data = []
    for m in materials:
        total_qty = m.active_stock or 0
        material_data.append({
            'material': m,
            'current_stock': float(total_qty),
            'stock_display': format_stock_display(total_qty, m),
            'total_value': float(total_qty * m.cost_per_unit),
        })

    context = {
        'material_data': material_data,
        'next_material_sku': generate_next_code(Material, 'sku', 'MAT', 1001, pad=4),
    }
    return render(request, 'material_list.html', context)


def _apply_material_update(request, material):
    """Validates and saves the material edit form from request.POST, queuing a message. The "Updated Material '<name>'" log text is parsed by analytics.resolve_material_from_label."""
    name = request.POST.get('name', '').strip()
    sku = request.POST.get('sku', '').strip().upper()
    category = request.POST.get('category', '').strip()
    uom = request.POST.get('unit_of_measure', 'MT')

    try:
        safe_days = int(request.POST.get('safe_storage_days', 90))
        cost = float(request.POST.get('cost_per_unit', 0.0))
        weight, weight_err = read_unit_weight(request.POST, uom)
        if weight_err:
            messages.error(request, weight_err)
            return False

        if not name:
            messages.error(request, "Material name is required.")
            return False

        reorder_point, err = read_decimal(request.POST, 'reorder_point', 'Reorder point', min_value=0, required=False)
        if err:
            messages.error(request, err)
            return False

        if not sku:
            sku = material.sku

        # Check SKU uniqueness against other materials
        if Material.objects.filter(sku=sku).exclude(pk=material.pk).exists():
            messages.error(request, f"SKU '{sku}' is already assigned to another material.")
            return False

        material.name = name
        material.sku = sku
        material.category = category or material.category
        material.unit_of_measure = uom
        material.safe_storage_days = safe_days
        material.weight_mt_per_unit = weight
        material.cost_per_unit = cost
        material.reorder_point = reorder_point
        if 'inspect_on_receipt_field' in request.POST:
            material.inspect_on_receipt = request.POST.get('inspect_on_receipt') == '1'
        material.save()

        RegistryLog.objects.create(
            action_type='Adjusted',
            item_name=f"Updated Material '{material.name}' (SKU: {material.sku})",
            material=material,
            quantity_changed=0,
            warehouse=None,
            user=request.user if request.user.is_authenticated else None
        )

        messages.success(request, f"Material '{material.name}' (SKU: {material.sku}) updated successfully.")
        return True
    except Exception as e:
        messages.error(request, f"Error updating material: {e}")
        return False


@login_required
def material_edit_view(request, pk):
    """View to edit an existing raw material record."""
    material = get_object_or_404(Material, pk=pk)

    if request.method == 'POST':
        if not request.user.has_perm('core.change_material'):
            messages.error(request, "You don't have permission to edit materials.")
            return redirect('material_list')
        _apply_material_update(request, material)
        return redirect('material_list')

    # GET request - AJAX returns JSON data for modal; standard request renders form page
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'id': material.id,
            'name': material.name,
            'sku': material.sku,
            'category': material.category,
            'unit_of_measure': material.unit_of_measure,
            'safe_storage_days': material.safe_storage_days,
            'weight_mt_per_unit': float(material.weight_mt_per_unit),
            'cost_per_unit': float(material.cost_per_unit),
            'reorder_point': float(material.reorder_point) if material.reorder_point is not None else None,
            'inspect_on_receipt': material.inspect_on_receipt,
        })

    from .utils import get_batch_reservations
    material_batches = Batch.objects.filter(material=material, status='Active').select_related('warehouse', 'purchase_order').order_by('expiry_date')
    material_batch_rows = []
    for b in material_batches:
        material_batch_rows.append({
            'batch': b,
            'available': b.quantity - b.allocated_quantity,
            'reservations': get_batch_reservations(b),
            'source_po': b.purchase_order,
        })

    return render(request, 'material_form.html', {
        'material': material, 'edit_mode': True, 'material_batch_rows': material_batch_rows,
    })


# RegistryLog action -> stock direction shown on the material page ('' = no stock movement).
_MOVEMENT_SIGN = {
    'Inbound': '+', 'Produced': '+',
    'Outbound': '-', 'Consumed_For_Manufacturing': '-', 'Spoiled_Disposal': '-',
    'Lost_In_Transit': '-',
}


@login_required
def material_detail_view(request, pk):
    """One raw material: core info, stock and batches, cover/reorder forecast, usage and suppliers (figures from core.analytics)."""
    denied = action_denied(request, 'material_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    from . import analytics
    from .models import RunExtraMaterial
    from .utils import get_batch_reservations

    material = get_object_or_404(Material, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if not request.user.has_perm('core.change_material'):
            messages.error(request, "Permission denied. Only Managers and Admins can change materials.")
        elif action == 'update_material':
            _apply_material_update(request, material)
        elif action == 'toggle_active':
            material.is_active = not material.is_active
            material.save()
            messages.success(request, f"Material {material.sku} is now {'Active' if material.is_active else 'Archived'}.")
            log_archive_change(request, 'Material', material)
        return redirect('material_detail', pk=pk)

    today = timezone.localdate()

    # --- Stock & batches (Active only, as everywhere else; Quarantined shown apart)
    active_batches = list(
        Batch.objects.filter(material=material, status='Active')
        .select_related('warehouse', 'purchase_order').order_by('expiry_date')
    )
    total_qty = sum((b.quantity for b in active_batches), Decimal('0'))
    allocated_qty = sum((b.allocated_quantity for b in active_batches), Decimal('0'))
    quarantined_qty = Batch.objects.filter(material=material, status='Quarantined').aggregate(s=Sum('quantity'))['s'] or Decimal('0')

    batch_rows = []
    warehouse_totals = {}
    for b in active_batches:
        days_left = (b.expiry_date - today).days if b.expiry_date else None
        batch_rows.append({
            'batch': b,
            'available': b.quantity - b.allocated_quantity,
            'reservations': get_batch_reservations(b),
            'days_left': days_left,
        })
        wh_name = b.warehouse.name if b.warehouse else (b.location or 'Unassigned')
        w = warehouse_totals.setdefault(wh_name, {'name': wh_name, 'quantity': Decimal('0'), 'allocated': Decimal('0'), 'batches': 0})
        w['quantity'] += b.quantity
        w['allocated'] += b.allocated_quantity
        w['batches'] += 1
    warehouse_rows = sorted(warehouse_totals.values(), key=lambda w: -w['quantity'])

    # --- Cover / reorder forecast (None for deactivated materials)
    forecast = next((r for r in analytics.stockout_forecast() if r['material_id'] == material.id), None)

    # Consumption: last 6 months of Consumed_For_Manufacturing
    month_keys = analytics._trailing_month_keys(today, 6)
    first_month = date(*(int(p) for p in month_keys[0].split('-')), 1)
    by_month = {k: Decimal('0') for k in month_keys}
    consumption_logs = material.registry_logs.filter(
        action_type=analytics.CONSUMPTION_ACTION, timestamp__date__gte=first_month,
    ).values_list('timestamp', 'quantity_changed')
    for ts, qty in consumption_logs:
        key = timezone.localtime(ts).strftime('%Y-%m')
        if key in by_month:
            by_month[key] += qty
    chart_labels = month_keys
    chart_consumption = [float(by_month[k]) for k in month_keys]

    # --- Where it is used / who supplies it / purchasing
    recipe_uses = [
        {'recipe': r, 'line_cost': r.quantity_required * material.cost_per_unit}
        for r in ProductRecipe.objects.filter(material=material).select_related('product').order_by('product__name')
    ]
    supplier_rows = SupplierMaterial.objects.filter(material=material).select_related('supplier').order_by('supplier__name')
    po_lines = (PurchaseOrderDetail.objects.filter(material=material)
                .select_related('purchase_order').order_by('-purchase_order__id')[:10])

    movements = material.registry_logs.select_related('warehouse', 'user').order_by('-timestamp')[:15]
    movement_rows = [{'log': m, 'sign': _MOVEMENT_SIGN.get(m.action_type, '')} for m in movements]

    # Poured into runs from stock the system doesn't hold - nothing was deducted for these
    off_book_usage = (RunExtraMaterial.objects.filter(material=material, batch__isnull=True)
                      .select_related('production_run').order_by('-created_at')[:10])

    context = {
        'material': material,
        'uom_choices': Material.UNIT_CHOICES,
        'total_qty': total_qty,
        'stock_display': format_stock_display(total_qty, material),
        'allocated_qty': allocated_qty,
        'available_qty': total_qty - allocated_qty,
        'quarantined_qty': quarantined_qty,
        'stock_value': total_qty * material.cost_per_unit,
        'batch_rows': batch_rows,
        'warehouse_rows': warehouse_rows,
        'forecast': forecast,
        'chart_labels': chart_labels,
        'chart_consumption': chart_consumption,
        'recipe_uses': recipe_uses,
        'supplier_rows': supplier_rows,
        'po_lines': po_lines,
        'movement_rows': movement_rows,
        'off_book_usage': off_book_usage,
    }
    return render(request, 'material_detail.html', context)


# Suppliers
@login_required
def supplier_list_view(request):
    denied = action_denied(request, 'supplier_list')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle_active':
            if request.user.has_perm('core.change_supplier'):
                supplier_id = request.POST.get('supplier_id')
                sup = get_object_or_404(Supplier, id=supplier_id)
                sup.is_active = not sup.is_active
                sup.save()
                messages.success(request, f"Supplier {sup.name} is now {'Active' if sup.is_active else 'Archived'}.")
                log_archive_change(request, 'Supplier', sup)
            else:
                messages.error(request, "Permission denied. Only Managers and Admins can toggle status.")
            return redirect('supplier_list')

        name = request.POST.get('name', '').strip()
        try:
            if not name:
                messages.error(request, "Supplier name is required.")
                return redirect('supplier_list')
            supplier = Supplier(
                name=name,
                contact_person=request.POST.get('contact_person', '').strip(),
                email=request.POST.get('email', '').strip(),
                phone=normalise_phone(request.POST.get('phone'), request.POST.get('phone_country')),
                address=request.POST.get('address', '').strip(),
            )
            supplier.full_clean()
            supplier.save()
            messages.success(request, f"Supplier '{name}' registered.")
        except ValidationError as e:
            messages.error(request, f"Supplier not saved. {validation_messages(e)}")
        except Exception as e:
            messages.error(request, f"Error registering supplier: {e}")
        return redirect('supplier_list')

    search_query = request.GET.get('q', '').strip()
    suppliers = Supplier.objects.all().order_by('name')
    if search_query:
        suppliers = suppliers.filter(
            Q(name__icontains=search_query) | Q(contact_person__icontains=search_query)
            | Q(email__icontains=search_query) | Q(phone__icontains=search_query)
        )

    suppliers, sort_ctx = apply_list_sort(request, suppliers, {
        'name': 'name',
        'contact_person': 'contact_person',
        'email': 'email',
        'phone': 'phone',
        'address': 'address',
    }, default='name')
    page_size = get_page_size(request)
    suppliers = Paginator(suppliers, page_size).get_page(request.GET.get('page'))

    context = {'suppliers': suppliers, 'search_query': search_query, 'page_size': page_size, **sort_ctx}
    return render(request, 'supplier_list.html', context)


@login_required
def supplier_edit_view(request, pk):
    supplier = get_object_or_404(Supplier, pk=pk)

    if request.method == 'POST':
        if not request.user.has_perm('core.change_supplier'):
            messages.error(request, "You don't have permission to edit suppliers.")
            return redirect('supplier_detail', pk=supplier.pk)
        name = request.POST.get('name', '').strip()
        try:
            if not name:
                messages.error(request, "Supplier name is required.")
                return redirect('supplier_detail', pk=supplier.pk)

            supplier.name = name
            supplier.contact_person = request.POST.get('contact_person', '').strip()
            supplier.email = request.POST.get('email', '').strip()
            supplier.phone = normalise_phone(request.POST.get('phone'), request.POST.get('phone_country'))
            supplier.address = request.POST.get('address', '').strip()
            supplier.full_clean()
            supplier.save()

            messages.success(request, f"Supplier '{supplier.name}' updated successfully.")
        except ValidationError as e:
            messages.error(request, f"Supplier not updated. {validation_messages(e)}")
        except Exception as e:
            messages.error(request, f"Error updating supplier: {e}")

    return redirect('supplier_detail', pk=supplier.pk)


# Clients
@login_required
def client_list_view(request):
    denied = action_denied(request, 'client_list')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle_active':
            if request.user.has_perm('core.change_client'):
                client_id = request.POST.get('client_id')
                cli = get_object_or_404(Client, id=client_id)
                cli.is_active = not cli.is_active
                cli.save()
                messages.success(request, f"Client {cli.name} is now {'Active' if cli.is_active else 'Archived'}.")
                log_archive_change(request, 'Client', cli)
            else:
                messages.error(request, "Permission denied. Only Managers and Admins can toggle status.")
            return redirect('client_list')

        name = request.POST.get('name', '').strip()
        try:
            if not name:
                messages.error(request, "Client name is required.")
                return redirect('client_list')
            client = Client(
                name=name,
                contact_person=request.POST.get('contact_person', '').strip(),
                email=request.POST.get('email', '').strip(),
                phone=normalise_phone(request.POST.get('phone'), request.POST.get('phone_country')),
                delivery_address=request.POST.get('delivery_address', '').strip(),
            )
            client.full_clean()
            client.save()
            messages.success(request, f"Client '{name}' registered.")
        except ValidationError as e:
            messages.error(request, f"Client not saved. {validation_messages(e)}")
        except Exception as e:
            messages.error(request, f"Error registering client: {e}")
        return redirect('client_list')

    search_query = request.GET.get('q', '').strip()
    clients = Client.objects.all().order_by('name')
    if search_query:
        clients = clients.filter(
            Q(name__icontains=search_query) | Q(contact_person__icontains=search_query)
            | Q(email__icontains=search_query) | Q(phone__icontains=search_query)
        )

    clients, sort_ctx = apply_list_sort(request, clients, {
        'name': 'name',
        'contact_person': 'contact_person',
        'email': 'email',
        'phone': 'phone',
        'delivery_address': 'delivery_address',
    }, default='name')
    page_size = get_page_size(request)
    clients = Paginator(clients, page_size).get_page(request.GET.get('page'))

    context = {'clients': clients, 'search_query': search_query, 'page_size': page_size, **sort_ctx}
    return render(request, 'client_list.html', context)


@login_required
def client_edit_view(request, pk):
    client = get_object_or_404(Client, pk=pk)

    if request.method == 'POST':
        if not request.user.has_perm('core.change_client'):
            messages.error(request, "You don't have permission to edit clients.")
            return redirect('client_detail', pk=client.pk)
        name = request.POST.get('name', '').strip()
        try:
            if not name:
                messages.error(request, "Client name is required.")
                return redirect('client_detail', pk=client.pk)

            client.name = name
            client.contact_person = request.POST.get('contact_person', '').strip()
            client.email = request.POST.get('email', '').strip()
            client.phone = normalise_phone(request.POST.get('phone'), request.POST.get('phone_country'))
            client.delivery_address = request.POST.get('delivery_address', '').strip()
            client.full_clean()
            client.save()

            messages.success(request, f"Client '{client.name}' updated successfully.")
        except ValidationError as e:
            messages.error(request, f"Client not updated. {validation_messages(e)}")
        except Exception as e:
            messages.error(request, f"Error updating client: {e}")

    return redirect('client_detail', pk=client.pk)

@login_required
def supplier_detail_view(request, pk):
    denied = action_denied(request, 'supplier_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    supplier = get_object_or_404(Supplier, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle_active':
            supplier.is_active = not supplier.is_active
            supplier.save(update_fields=['is_active'])
            messages.success(request, f"{supplier.name} is now {'Active' if supplier.is_active else 'Archived'}.")
            log_archive_change(request, 'Supplier', supplier)
        elif action == 'save_material':
            material = get_object_or_404(Material, pk=request.POST.get('material_id'))
            price, err = read_decimal(request.POST, 'unit_price', "Unit price", min_value=0, required=False)
            lead, lead_err = read_decimal(request.POST, 'lead_time_days', "Lead time", min_value=0, required=False)
            err = err or lead_err or (lead is not None and lead != int(lead) and "Lead time must be whole days.")
            if err:
                messages.error(request, err)
            else:
                _, created = SupplierMaterial.objects.update_or_create(
                    supplier=supplier, material=material,
                    defaults={'unit_price': price, 'lead_time_days': int(lead) if lead is not None else None})
                messages.success(request, f"{'Added' if created else 'Updated'} {material.name}.")
        elif action == 'remove_material':
            sm = get_object_or_404(SupplierMaterial, pk=request.POST.get('supplier_material_id'), supplier=supplier)
            sm.delete()
            messages.success(request, f"Removed {sm.material.name} from {supplier.name}.")
        return redirect('supplier_detail', pk=supplier.pk)

    from .analytics import _expected_delivery, _po_actual_arrival, _supplier_lead_map
    from .settings_store import get_setting
    lead_map, default_lead = _supplier_lead_map(), get_setting('po_default_lead_time_days')

    pos = (supplier.purchase_orders
           .prefetch_related('items__material', 'shipments', 'received_batches').order_by('-order_date', '-id'))
    po_rows, bought, on_time, assessed = [], {}, 0, 0
    for po in pos:
        arrival = _po_actual_arrival(po)
        expected, estimated = _expected_delivery(po, lead_map, default_lead)
        late = (arrival - expected).days if arrival and expected else None
        if late is not None:
            assessed += 1
            on_time += late <= 0
        po_rows.append({'po': po, 'arrival': arrival, 'expected': expected, 'estimated': estimated, 'late_days': late})
        if po.status in ('Draft', 'Pending Approval', 'Rejected'):
            continue
        for it in po.items.all():
            row = bought.setdefault(it.material_id, {'material': it.material, 'ordered': Decimal('0'), 'received': Decimal('0'), 'value': Decimal('0')})
            row['ordered'] += it.quantity_ordered
            row['received'] += it.quantity_received
            row['value'] += it.quantity_ordered * (it.unit_price or 0)

    supplied = SupplierMaterial.objects.filter(supplier=supplier).select_related('material').order_by('material__name')
    return render(request, 'supplier_detail.html', {
        'supplier': supplier,
        'supplied': supplied,
        'materials': Material.objects.filter(is_active=True).exclude(pk__in=supplied.values('material_id')).order_by('name'),
        'po_rows': po_rows,
        'bought': sorted(bought.values(), key=lambda r: r['material'].name),
        'total_value': sum((r['po'].total_value for r in po_rows if r['po'].status not in ('Draft', 'Pending Approval', 'Rejected')), Decimal('0')),
        'on_time_rate': round(on_time / assessed * 100) if assessed else None,
    })


@login_required
def client_detail_view(request, pk):
    denied = action_denied(request, 'client_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    client = get_object_or_404(Client, pk=pk)
    if request.method == 'POST' and request.POST.get('action') == 'toggle_active':
        client.is_active = not client.is_active
        client.save(update_fields=['is_active'])
        messages.success(request, f"{client.name} is now {'Active' if client.is_active else 'Archived'}.")
        log_archive_change(request, 'Client', client)
        return redirect('client_detail', pk=client.pk)

    sos = client.sales_orders.prefetch_related('items__product').order_by('-order_date', '-id')
    bought = {}
    for so in sos:
        if so.status in ('Draft', 'Pending Approval', 'Rejected', 'Cancelled'):
            continue
        for it in so.items.all():
            row = bought.setdefault(it.product_id, {'product': it.product, 'ordered': Decimal('0'), 'shipped': Decimal('0'), 'value': Decimal('0')})
            row['ordered'] += it.quantity_ordered
            row['shipped'] += it.quantity_shipped or 0
            row['value'] += it.quantity_ordered * (it.unit_price or 0)
    return render(request, 'client_detail.html', {
        'client': client,
        'sales_orders': sos,
        'bought': sorted(bought.values(), key=lambda r: r['product'].name),
        'total_value': sum((r['value'] for r in bought.values()), Decimal('0')),
    })


# Sales orders
@login_required
def sales_order_list_view(request):
    denied = action_denied(request, 'so_list')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_so':
            so_number_auto = request.POST.get('so_number_auto') == '1'
            so_number = request.POST.get('so_number')
            if so_number_auto or not so_number:
                so_number = generate_next_code(SalesOrder, 'so_number', 'SO', 1001)
            client_name = request.POST.get('client_name')
            client_id = request.POST.get('client_id')
            warehouse_id = request.POST.get('origin_warehouse_id')
            try:
                wh = get_object_or_404(Warehouse, id=warehouse_id)
                client_obj = Client.objects.filter(id=client_id).first() if client_id else None
                so = SalesOrder.objects.create(
                    so_number=so_number, client_name=client_name, client=client_obj,
                    origin_warehouse=wh, status='Draft', created_by=request.user
                )
                OrderTimeline.objects.create(sales_order=so, action="Sales Order Created (Draft)", user=request.user)
                messages.success(request, f"Sales Order {so_number} created.")
            except Exception as e:
                messages.error(request, f"Error creating Sales Order: {e}")

        elif action == 'update_so_status':
            so_id = request.POST.get('so_id')
            new_status = request.POST.get('status')
            so = get_object_or_404(SalesOrder, id=so_id)
            problem = so_manual_status_problem(so, new_status)
            if problem:
                messages.error(request, problem)
                return redirect('so_list')
            old_status = so.status
            so.status = new_status
            if new_status in ['Pending', 'Awaiting Acknowledgement']:
                so.approved_by = request.user
            so.save()
            OrderTimeline.objects.create(sales_order=so, action=f"Status changed to '{new_status}'", user=request.user)
            
            from .utils import deduct_stock_from_allocation
            if old_status not in ['Shipped', 'Delivered'] and new_status in ['Shipped', 'Delivered']:
                deduct_stock_from_allocation('sales_order', so, user=request.user)
                
            messages.success(request, f"Sales Order {so.so_number} updated to {new_status}.")

        return redirect('so_list')

    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    
    sales_orders = SalesOrder.objects.select_related('origin_warehouse', 'created_by', 'approved_by').prefetch_related('items__product')
    
    if search_query:
        # also matches the products on the order, since the list shows them
        sales_orders = sales_orders.filter(
            Q(so_number__icontains=search_query) | Q(client_name__icontains=search_query)
            | Q(pk__in=SalesOrderDetail.objects.filter(
                Q(product__name__icontains=search_query) | Q(product__sku__icontains=search_query)
            ).values('sales_order_id'))
        )
        
    if status_filter:
        sales_orders = sales_orders.filter(status=status_filter)

    # Status sorts in workflow order, not alphabetically
    status_rank = Case(
        *[When(status=code, then=Value(i)) for i, (code, _) in enumerate(SalesOrder.STATUS_CHOICES)],
        default=Value(len(SalesOrder.STATUS_CHOICES)),
    )
    sales_orders, sort_ctx = apply_list_sort(request, sales_orders, {
        'so_number': 'so_number',
        'client_name': 'client_name',
        'origin_warehouse': 'origin_warehouse__name',
        'order_date': 'order_date',
        'status': status_rank,
    }, default='-order_date')
    page_size = get_page_size(request)
    sales_orders = Paginator(sales_orders, page_size).get_page(request.GET.get('page'))

    warehouses = Warehouse.objects.all().order_by('name')
    clients = Client.objects.filter(is_active=True).order_by('name')

    context = {
        'search_query': search_query,
        'status_filter': status_filter,
        'sales_orders': sales_orders,
        'warehouses': warehouses,
        'clients': clients,
        'so_status_choices': SalesOrder.STATUS_CHOICES,
        'next_so_number': generate_next_code(SalesOrder, 'so_number', 'SO', 1001),
        'page_size': page_size,
        **sort_ctx,
    }
    return render(request, 'so_list.html', context)


# Purchase orders
@login_required
def purchase_order_list_view(request):
    denied = action_denied(request, 'po_list')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_po':
            po_number_auto = request.POST.get('po_number_auto') == '1'
            po_number = request.POST.get('po_number')
            if po_number_auto or not po_number:
                po_number = generate_next_code(PurchaseOrder, 'po_number', 'PO', 5001)
            supplier_name = request.POST.get('supplier_name')
            supplier_id = request.POST.get('supplier_id')
            warehouse_id = request.POST.get('target_warehouse_id')
            # Optional first line (the Reorder button on Stockout & Reorder pre-fills the material).
            line_mat_id = request.POST.get('material_id', '').strip()
            line_mat = line_qty = None
            if line_mat_id:
                line_mat = Material.objects.filter(pk=line_mat_id, is_active=True).first() if line_mat_id.isdigit() else None
                line_qty, err = read_decimal(request.POST, 'quantity_ordered', "Quantity", above=0)
                if not line_mat:
                    err = "That material can't be ordered (not found or archived)."
                if err:
                    messages.error(request, err)
                    return redirect(f"{reverse('po_list')}?new=1&material_id={line_mat_id}")
            try:
                wh = get_object_or_404(Warehouse, id=warehouse_id)
                supplier_obj = Supplier.objects.filter(id=supplier_id).first() if supplier_id else None
                with transaction.atomic():
                    po = PurchaseOrder.objects.create(
                        po_number=po_number, supplier_name=supplier_name, supplier=supplier_obj,
                        target_warehouse=wh, status='Draft', created_by=request.user
                    )
                    OrderTimeline.objects.create(purchase_order=po, action="Purchase Order Created (Draft)", user=request.user)
                    if line_mat:
                        PurchaseOrderDetail.objects.create(purchase_order=po, material=line_mat, quantity_ordered=line_qty)
                        OrderTimeline.objects.create(purchase_order=po, action=f"Line item added: {line_mat.name} x{line_qty}", user=request.user)
                messages.success(request, f"Purchase Order {po_number} created.")
                if line_mat:
                    return redirect('po_detail', pk=po.pk)
            except Exception as e:
                messages.error(request, f"Error creating Purchase Order: {e}")

        elif action == 'update_po_status':
            po_id = request.POST.get('po_id')
            new_status = request.POST.get('status')
            po = get_object_or_404(PurchaseOrder, id=po_id)
            if new_status not in po_manual_transitions(po):
                messages.error(request, f"{po.po_number} can't be changed from {po.status} to {new_status} by hand.")
                return redirect('po_list')
            reopening = po.status == 'Completed'
            po.status = new_status
            if new_status in ['Pending', 'Partially Received'] and not reopening:
                po.approved_by = request.user
            if reopening:
                po.completed_date = None
            po.save()
            OrderTimeline.objects.create(purchase_order=po, action="PO reopened to receive more" if reopening else f"Status changed to '{new_status}'", user=request.user)
            if new_status == 'Completed':
                from .utils import release_po_held_batches
                release_po_held_batches(po, request.user)
            messages.success(request, f"Purchase Order {po.po_number} updated to {new_status}.")

        return redirect('po_list')

    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    
    purchase_orders = PurchaseOrder.objects.select_related('target_warehouse', 'created_by', 'approved_by').prefetch_related('items__material').order_by('-order_date')
    
    if search_query:
        purchase_orders = purchase_orders.filter(
            Q(po_number__icontains=search_query) | Q(supplier_name__icontains=search_query)
            # also matches the materials on the order, since the list shows them
            | Q(pk__in=PurchaseOrderDetail.objects.filter(
                Q(material__name__icontains=search_query) | Q(material__sku__icontains=search_query)
            ).values('purchase_order_id'))
        )
        
    if status_filter:
        purchase_orders = purchase_orders.filter(status=status_filter)

    purchase_orders, sort_ctx = apply_list_sort(request, purchase_orders, {
        'po_number': 'po_number',
        'supplier_name': 'supplier_name',
        'target_warehouse': 'target_warehouse__name',
        'order_date': 'order_date',
        'status': 'status',
    }, default='-order_date')
    page_size = get_page_size(request)
    purchase_orders = Paginator(purchase_orders, page_size).get_page(request.GET.get('page'))

    warehouses = Warehouse.objects.all().order_by('name')
    suppliers = Supplier.objects.filter(is_active=True).order_by('name')

    # ?new=1&material_id=N (Reorder button) opens the New PO pop-up with that material
    # and its fastest active supplier picked.
    prefill_material = prefill_supplier = None
    if request.GET.get('new') == '1':
        mat_id = request.GET.get('material_id', '')
        if mat_id.isdigit():
            prefill_material = Material.objects.filter(pk=mat_id, is_active=True).first()
        if prefill_material:
            sm = (SupplierMaterial.objects.filter(material=prefill_material, supplier__is_active=True)
                  .select_related('supplier').order_by(F('lead_time_days').asc(nulls_last=True), 'supplier__name').first())
            prefill_supplier = sm.supplier if sm else None

    context = {
        'search_query': search_query,
        'status_filter': status_filter,
        'purchase_orders': purchase_orders,
        'warehouses': warehouses,
        'suppliers': suppliers,
        'po_materials': Material.objects.filter(is_active=True).order_by('name'),
        'open_po_modal': request.GET.get('new') == '1',
        'prefill_material': prefill_material,
        'prefill_supplier': prefill_supplier,
        'po_status_choices': PurchaseOrder.STATUS_CHOICES,
        'next_po_number': generate_next_code(PurchaseOrder, 'po_number', 'PO', 5001),
        'page_size': page_size,
        **sort_ctx,
    }
    return render(request, 'po_list.html', context)


# Bulk import/export for Sales and Purchase Orders. One CSV row per line item; rows
# sharing an order number make one order. Two kinds of import:
#  - live orders (status blank or Draft) land in Draft, or Pending Approval when an
#    approver is named, and then follow the normal approval flow;
#  - past manual records (status Delivered/Completed/Cancelled) are entered as closed
#    history. They need the approve permission, never touch stock (today's stock comes
#    in through batches) and can't be opened mid-flow.
ORDER_IO = {
    'so': {
        'model': SalesOrder, 'line_model': SalesOrderDetail, 'label': 'Sales Order', 'list_url': 'so_list',
        'perm': 'core.add_salesorder', 'request_perm': 'core.change_salesorder', 'approve_perm': APPROVE_SALES_ORDERS,
        'number': 'so_number', 'prefix': 'SO', 'start': 1001,
        'party': 'client_name', 'party_model': Client, 'party_fk': 'client',
        'warehouse': 'origin_warehouse', 'date': 'fulfillment_deadline', 'date_label': 'Fulfilment deadline',
        'item': 'product', 'item_model': Product, 'order_fk': 'sales_order', 'extra': [],
        'done': 'quantity_shipped', 'closed': ('Delivered', 'Cancelled'), 'done_status': 'Delivered', 'history_dates': [],
        'samples': [('SO-9001', 'Acme Trading', 'PROD1001', '100', '150.00', '2026-12-01', '', '', ''),
                    ('SO-9001', '', 'PROD1002', '20', '85.50', '', '', '', ''),
                    ('', 'Beta Retail (auto number)', 'PROD1001', '5', '', '', '', '', ''),
                    ('SO-0420', 'Acme Trading (past record)', 'PROD1001', '50', '140.00', '2025-03-15', 'Delivered', '2025-03-01', '50')],
    },
    'po': {
        'model': PurchaseOrder, 'line_model': PurchaseOrderDetail, 'label': 'Purchase Order', 'list_url': 'po_list',
        'perm': 'core.add_purchaseorder', 'request_perm': 'core.change_purchaseorder', 'approve_perm': APPROVE_PURCHASE_ORDERS,
        'number': 'po_number', 'prefix': 'PO', 'start': 5001,
        'party': 'supplier_name', 'party_model': Supplier, 'party_fk': 'supplier',
        'warehouse': 'target_warehouse', 'date': 'expected_delivery_date', 'date_label': 'Expected delivery date',
        'item': 'material', 'item_model': Material, 'order_fk': 'purchase_order',
        'extra': [('negotiated_rental_rate_per_mt', 'Rental rate')],
        'done': 'quantity_received', 'closed': ('Completed', 'Cancelled'), 'done_status': 'Completed',
        'history_dates': [('completed_date', 'Completed date')],
        'samples': [('PO-9001', 'Global Chemicals', 'MAT1001', '10', '450.00', '2026-12-01', '', '', ''),
                    ('PO-9001', '', 'MAT1002', '500', '2.50', '', '', '', ''),
                    ('', 'Polymer Supply Co (auto number)', 'MAT1001', '2', '', '', '', '', ''),
                    ('PO-0420', 'Global Chemicals (past record)', 'MAT1001', '8', '430.00', '2025-02-20', 'Completed', '2025-02-01', '8')],
    },
}


def _order_columns(cfg):
    return ([cfg['number'], cfg['party'], cfg['warehouse'], cfg['date'], f"{cfg['item']}_sku", 'quantity_ordered', 'unit_price']
            + [col for col, _ in cfg['extra']] + ['approver', 'approval_remarks', 'status', 'order_date', cfg['done']]
            + [col for col, _ in cfg['history_dates']])


def _csv_date(row, col, label):
    """Read a CSV date as YYYY-MM-DD or day-first DD/MM/YYYY (how Excel re-saves dates here)."""
    from datetime import datetime
    raw = row.get(col, '')
    if not raw:
        return None, None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw, fmt).date(), None
        except ValueError:
            pass
    return None, f"{label} '{raw}' must be a date like 2026-11-05 or 05/11/2026."


def _csv_download(filename):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response, csv.writer(response)


@login_required
def export_order_template(request, kind):
    """Download the reference CSV template for Sales or Purchase Order import."""
    cfg = ORDER_IO[kind]
    response, writer = _csv_download(f"{kind}_import_template.csv")
    writer.writerow(_order_columns(cfg))
    wh = Warehouse.objects.order_by('name').values_list('name', flat=True).first() or 'Main Warehouse'
    for number, party, sku, qty, price, due, status, ordered_on, done in cfg['samples']:
        writer.writerow([number, party, wh if party else '', due, sku, qty, price] + [''] * len(cfg['extra'])
                        + ['', '', status, ordered_on, done] + [due if status else '' for _ in cfg['history_dates']])
    return response


@login_required
def export_orders_csv(request, kind):
    """Export every Sales or Purchase Order as CSV, one row per line item. The import
    columns come first so an export can be re-imported; the rest is for reference."""
    cfg = ORDER_IO[kind]
    if kind == 'so':
        kind_extra = [('manufacturing_plant', 'manufacturing_plant'), ('cancel_reason', 'cancel_reason')]
    else:
        kind_extra = [('linked_production_run', 'linked_production_run.run_number')]
    head_extra = ([('order_total', 'total_value'),
                   ('client_linked' if kind == 'so' else 'supplier_linked', cfg['party_fk'] + '.name'),
                   ('created_by', 'created_by.username'), ('approved_by', 'approved_by.username'),
                   ('rejection_reason', 'rejection_reason'), ('revision_count', 'revision_count')]
                  + kind_extra + [('updated_by', 'updated_by.username'), ('updated_at', 'updated_at')])

    def val(obj, path):
        for attr in path.split('.'):
            obj = getattr(obj, attr, None) if obj is not None else None
        if hasattr(obj, 'isoformat'):
            return obj.isoformat(timespec='minutes') if hasattr(obj, 'hour') else obj.isoformat()
        return '' if obj is None else getattr(obj, 'name', obj)

    response, writer = _csv_download(f"{kind}_export.csv")
    writer.writerow(_order_columns(cfg) + [f"{cfg['item']}_name", 'unit_of_measure', 'quantity_outstanding', 'line_total']
                    + [h for h, _ in head_extra])
    orders = (cfg['model'].objects
              .select_related(cfg['warehouse'], cfg['party_fk'], 'created_by', 'assigned_to', 'approved_by', 'updated_by')
              .prefetch_related(f"items__{cfg['item']}").order_by(cfg['number']))
    for order in orders:
        head = [val(order, f) for f in (cfg['number'], cfg['party'], cfg['warehouse'], cfg['date'])]
        approval = [val(order, 'assigned_to.username'), val(order, 'approval_remarks'), order.status, val(order, 'order_date')]
        dates = [val(order, col) for col, _ in cfg['history_dates']]
        tail = [val(order, path) for _, path in head_extra]
        lines = list(order.items.all())
        if not lines:
            writer.writerow(head + [''] * (3 + len(cfg['extra'])) + approval + [''] + dates + [''] * 4 + tail)
        for line in lines:
            item, done = getattr(line, cfg['item']), getattr(line, cfg['done'])
            writer.writerow(head + [item.sku, line.quantity_ordered, val(line, 'unit_price')]
                            + [val(line, col) for col, _ in cfg['extra']] + approval + [done] + dates
                            + [item.name, item.unit_of_measure, max(line.quantity_ordered - done, 0),
                               line.quantity_ordered * line.unit_price if line.unit_price is not None else '']
                            + tail)
    return response


@login_required
def import_orders(request, kind):
    """Bulk import Sales or Purchase Orders from CSV, with dry-run and per-order error reporting."""
    cfg = ORDER_IO[kind]
    is_dry_run = request.POST.get('dry_run') == '1'

    def fail(msg):
        if is_dry_run:
            return JsonResponse({'success': False, 'error': msg})
        messages.error(request, msg)
        return redirect(cfg['list_url'])

    if request.method != 'POST':
        return redirect(cfg['list_url'])
    if not request.user.has_perm(cfg['perm']):
        return fail(f"You don't have permission to import {cfg['label']}s.")
    csv_file = request.FILES.get('csv_file')
    if not csv_file or not csv_file.name.lower().endswith('.csv'):
        return fail("Please select a .csv file to upload.")
    try:
        reader = csv.DictReader(io.StringIO(csv_file.read().decode('utf-8-sig')))
        headers = [h.strip().lower() for h in (reader.fieldnames or []) if h]
        rows = [{k.strip().lower(): (v or '').strip() for k, v in r.items() if k}
                for r in reader]
    except Exception as e:
        return fail(f"Could not read CSV file: {e}")
    sku_col = f"{cfg['item']}_sku"
    missing = [c for c in (cfg['party'], cfg['warehouse'], sku_col, 'quantity_ordered') if c not in headers]
    if missing:
        return fail(f"CSV header is missing required column(s): {', '.join(missing)}.")

    # Rows with the same order number form one order; a row with no number is its own order.
    groups = {}
    for idx, row in enumerate(rows, start=2):
        if any(row.values()):
            groups.setdefault(row.get(cfg['number'], '').upper() or f'#{idx}', []).append((idx, row))

    items = {i.sku.upper(): i for i in cfg['item_model'].objects.all()}
    warehouses = {w.name.strip().upper(): w for w in Warehouse.objects.all()}
    existing = {n.upper() for n in cfg['model'].objects.values_list(cfg['number'], flat=True)}
    status_names = {code.upper(): code for code in cfg['closed'] + ('Draft',)}
    order_cols = ([cfg['party'], cfg['warehouse'], cfg['date'], 'approver', 'approval_remarks', 'status', 'order_date']
                  + [col for col, _ in cfg['history_dates']])
    today = timezone.localdate()
    created = skipped = errors = 0
    results = []

    for lines in groups.values():
        first_idx, head = lines[0]
        number = head.get(cfg['number'], '').upper()
        party = head.get(cfg['party'], '')
        span = str(first_idx) if len(lines) == 1 else f"{first_idx}-{lines[-1][0]}"

        def result(status, msg):
            results.append({'line': span, 'name': party or '—', 'sku': number or 'Auto', 'status': status, 'msg': msg})

        if number and number in existing:
            skipped += 1
            result('duplicate', f"{number} already exists, so it was skipped.")
            continue

        problems = []
        if not party:
            problems.append(f"{cfg['party']} is required.")
        wh = warehouses.get(head.get(cfg['warehouse'], '').upper())
        if not wh:
            problems.append(f"warehouse '{head.get(cfg['warehouse'], '')}' not found. "
                            f"Use one of: {', '.join(w.name for w in warehouses.values())}.")
        due, err = _csv_date(head, cfg['date'], cfg['date_label'])
        ordered_on, err2 = _csv_date(head, 'order_date', "Order date")
        problems += [e for e in (err, err2) if e]
        if ordered_on and ordered_on > today:
            problems.append("Order date can't be in the future.")

        raw_status = head.get('status', '')
        status = status_names.get(raw_status.upper() or 'DRAFT')
        historical = status not in (None, 'Draft')
        if not status:
            problems.append(f"status '{raw_status}' can't be imported. Use {', '.join(cfg['closed'])} for past records, "
                            "or leave it blank to enter an open order as Draft and run it through the normal flow.")
        elif historical and not request.user.has_perm(cfg['approve_perm']):
            problems.append(f"only users who can approve {cfg['label']}s can import past records.")

        history_dates = {}
        for col, label in cfg['history_dates']:
            history_dates[col], e = _csv_date(head, col, label)
            if e:
                problems.append(e)
            elif history_dates[col] and not historical:
                problems.append(f"{col} is only for past records.")
            elif history_dates[col] and (history_dates[col] > today or (ordered_on and history_dates[col] < ordered_on)):
                problems.append(f"{label} must be between the order date and today.")

        # A live order with an approver goes straight to Request Approval (never past it);
        # on a past record the approver is just who approved it at the time.
        approver_name = head.get('approver', '')
        approver = None
        if approver_name:
            approver = CustomUser.objects.filter(username__iexact=approver_name).first()
            if not historical and not request.user.has_perm(cfg['request_perm']):
                problems.append("you don't have permission to request approval; leave approver blank.")
            elif not historical and not (approver and approver.is_active):
                problems.append(f"approver '{approver_name}' is not an active user.")
            elif approver_problem(approver, cfg['approve_perm']):
                problems.append(f"approver '{approver_name}' can't approve {cfg['label']}s.")
        remarks = head.get('approval_remarks', '')
        problems = [f"Line {first_idx}: {p}" for p in problems]

        parsed, seen = [], set()
        for idx, row in lines:
            errs = [f"{col} differs from line {first_idx} of the same order."
                    for col in order_cols if row.get(col) and row[col].upper() != head.get(col, '').upper()]
            sku = row.get(sku_col, '').upper()
            item = items.get(sku)
            if not item:
                errs.append(f"SKU '{sku}' not found." if sku else "SKU is required.")
            elif not item.is_active and not historical:
                errs.append(f"{sku} is archived, so it can't go on a new order.")
            elif kind == 'so' and item.pk in seen:
                # SOs keep one line per product (same rule as adding a line by hand)
                errs.append(f"{sku} is listed twice; put its total on one row.")
            qty, e = read_decimal(row, 'quantity_ordered', "Quantity", above=0)
            errs.append(e)
            price, e = read_decimal(row, 'unit_price', "Unit price", min_value=0, required=False)
            errs.append(e)
            extra = {}
            for col, label in cfg['extra']:
                extra[col], e = read_decimal(row, col, label, min_value=0, required=False)
                errs.append(e)
            done, e = read_decimal(row, cfg['done'], cfg['done'].replace('_', ' ').capitalize(), min_value=0, required=False)
            errs.append(e)
            if done is None:
                done = qty if status == cfg['done_status'] and qty else Decimal('0')
            elif done and not historical:
                errs.append(f"{cfg['done']} is only for past records; a Draft order hasn't moved any stock.")
            elif qty and done > qty:
                errs.append(f"{cfg['done']} can't be more than quantity_ordered.")
            problems += [f"Line {idx}: {e}" for e in errs if e]
            if item:
                seen.add(item.pk)
            parsed.append((item, qty, price, extra, done))

        if problems:
            errors += 1
            result('error', ' '.join(problems))
            continue

        if not is_dry_run:
            with transaction.atomic():
                number = number or generate_next_code(cfg['model'], cfg['number'], cfg['prefix'], cfg['start'])
                party_obj = cfg['party_model'].objects.filter(name__iexact=party).first()
                order = cfg['model'].objects.create(**{
                    cfg['number']: number, cfg['party']: party_obj.name if party_obj else party,
                    cfg['party_fk']: party_obj, cfg['warehouse']: wh, cfg['date']: due,
                    'status': 'Draft', 'created_by': request.user,
                })
                for item, qty, price, extra, done in parsed:
                    cfg['line_model'].objects.create(**{cfg['order_fk']: order, cfg['item']: item, 'quantity_ordered': qty,
                                                        'unit_price': price, cfg['done']: done, **extra})
                if ordered_on:  # order_date is auto_now_add, so it can only be back-dated after creation
                    order.order_date = ordered_on
                    cfg['model'].objects.filter(pk=order.pk).update(order_date=ordered_on)
                if historical:
                    order.status, order.assigned_to, order.approval_remarks = status, approver, remarks
                    order.approved_by = approver or request.user
                    for col, value in history_dates.items():
                        setattr(order, col, value)
                    order.save()
                    OrderTimeline.objects.create(**{cfg['order_fk']: order, 'user': request.user, 'action':
                        f"Past record imported as {status} with {len(parsed)} line(s). No stock was moved."})
                else:
                    OrderTimeline.objects.create(**{cfg['order_fk']: order, 'user': request.user,
                                                    'action': f"{cfg['label']} Created (Draft) by bulk import with {len(parsed)} line(s)"})
                if approver and not historical:
                    # Same steps as the order page's Request Approval
                    order.status, order.assigned_to, order.approval_remarks = 'Pending Approval', approver, remarks
                    order.save()
                    OrderTimeline.objects.create(**{cfg['order_fk']: order, 'user': request.user, 'action':
                        f"Approval Requested from {approver.get_full_name() or approver.username}{self_approval_note(request.user, approver)}. Remarks: {remarks}"})
                    Notification.objects.create(user=approver, message=f"{cfg['label']} {number} requires your approval.",
                                                link=reverse('approvals_inbox'))
        if number:
            existing.add(number)
        created += 1
        stage = (f"a past {status} record" if historical
                 else f"sent to {approver.username} for approval" if approver else "Draft")
        result('created', f"{'Ready to create' if is_dry_run else 'Created'} as {stage} with {len(parsed)} line(s).")

    if is_dry_run:
        return JsonResponse({'success': True, 'dry_run': True, 'created_count': created, 'updated_count': 0,
                             'skipped_count': skipped, 'error_count': errors, 'results': results})

    summary = f"{cfg['label']} import completed: {created} created, {skipped} skipped as already existing."
    if errors:
        messages.warning(request, f"{summary} {errors} order(s) had errors and were not imported; run the dry-run preview to see why.")
    else:
        messages.success(request, summary)
    return redirect(cfg['list_url'])


# Sales order detail
@login_required
def so_detail_view(request, pk):
    denied = action_denied(request, 'so_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    so = get_object_or_404(SalesOrder.objects.prefetch_related('items__product', 'timeline__user', 'shipments', 'production_runs'), pk=pk)
    products = Product.objects.all().order_by('name')
    warehouses = Warehouse.objects.all().order_by('name')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action in ('add_so_item', 'remove_so_item', 'update_so_header') and so.status in SO_ITEMS_LOCKED:
            messages.error(request, f"{so.so_number} is {so.status}, so its items and details are locked.")
            return redirect('so_detail', pk=so.pk)

        if action == 'add_so_item':
            prod_id = request.POST.get('product_id')
            qty = request.POST.get('quantity_ordered', 0)
            qty, err = read_decimal(request.POST, 'quantity_ordered', "Quantity", above=0)
            unit_price, price_err = read_decimal(request.POST, 'unit_price', "Unit price", min_value=0, required=False)
            err = err or price_err
            if err:
                messages.error(request, err)
                return redirect('so_detail', pk=so.pk)
            try:
                prod = get_object_or_404(Product, id=prod_id)
                if not prod.is_active:
                    messages.error(request, f"{prod.name} is archived, so it can't be added to an order. Restore it first.")
                    return redirect('so_detail', pk=so.pk)
                # One line per product: adding a product already on the order raises that line instead.
                existing = so.items.filter(product=prod).first()
                if existing:
                    old_qty = existing.quantity_ordered
                    existing.quantity_ordered += qty
                    if unit_price is not None:
                        existing.unit_price = unit_price
                    existing.save()
                    OrderTimeline.objects.create(sales_order=so, action=f"Line quantity raised: {prod.name} {old_qty} -> {existing.quantity_ordered}", user=request.user)
                    messages.success(request, f"{prod.name} was already on {so.so_number}, so its quantity was raised to {existing.quantity_ordered}.")
                else:
                    SalesOrderDetail.objects.create(
                        sales_order=so,
                        product=prod,
                        quantity_ordered=qty,
                        unit_price=unit_price
                    )
                    OrderTimeline.objects.create(sales_order=so, action=f"Line item added: {prod.name} x{qty}", user=request.user)
                    messages.success(request, f"Added {prod.name} to {so.so_number}.")
                archived = archived_recipe_materials(prod)
                if archived:
                    messages.warning(request, f"Heads up: {prod.name}'s recipe uses archived material(s): {', '.join(m.name for m in archived)}.")
                if so_return_for_reapproval(so, request.user, "Line item added"):
                    messages.warning(request, f"{so.so_number} was already approved, so it's back in Draft. Request approval again.")
            except Exception as e:
                messages.error(request, f"Error adding item: {e}")

        elif action == 'remove_so_item':
            item_id = request.POST.get('item_id')
            try:
                item = get_object_or_404(SalesOrderDetail, id=item_id, sales_order=so)
                name = item.product.name
                product = item.product
                item.delete()

                from .utils import handle_so_item_removed, release_so_product_allocations
                release_so_product_allocations(so, product, request.user)
                handle_so_item_removed(so, product, request.user)

                OrderTimeline.objects.create(sales_order=so, action=f"Line item removed: {name}", user=request.user)
                messages.success(request, f"Removed {name} from {so.so_number}.")
                if so_return_for_reapproval(so, request.user, "Line item removed"):
                    messages.warning(request, f"{so.so_number} was already approved, so it's back in Draft. Request approval again.")
            except Exception as e:
                messages.error(request, f"Error removing item: {e}")

        elif action == 'update_so_status':
            new_status = request.POST.get('status')
            problem = so_manual_status_problem(so, new_status)
            if problem:
                messages.error(request, problem)
                return redirect('so_detail', pk=so.pk)
            old_status = so.status
            so.status = new_status
            if new_status in ['Pending', 'Awaiting Acknowledgement']:
                so.approved_by = request.user
            so.save()
            OrderTimeline.objects.create(sales_order=so, action=f"Status updated to '{new_status}'", user=request.user)
            
            from .utils import deduct_stock_from_allocation
            if old_status not in ['Shipped', 'Delivered'] and new_status in ['Shipped', 'Delivered']:
                deduct_stock_from_allocation('sales_order', so, user=request.user)

            # Notify followers of status change
            for follower in so.followers.all():
                if follower != request.user:
                    Notification.objects.create(
                        user=follower,
                        message=f"Sales Order {so.so_number} status changed to {new_status}.",
                        link=reverse('so_detail', args=[so.pk])
                    )

            messages.success(request, f"{so.so_number} status updated to {new_status}.")
            
        elif action == 'update_so_header':
            before = (so.client_name, so.origin_warehouse_id, str(so.fulfillment_deadline or ''))
            so.client_name = request.POST.get('client_name', so.client_name)
            wh_id = request.POST.get('origin_warehouse_id')
            if wh_id:
                so.origin_warehouse = get_object_or_404(Warehouse, id=wh_id)
            deadline = request.POST.get('fulfillment_deadline')
            so.fulfillment_deadline = deadline if deadline else so.fulfillment_deadline
            so.updated_by = request.user
            so.save()
            messages.success(request, f"{so.so_number} details updated.")
            if (so.client_name, so.origin_warehouse_id, str(so.fulfillment_deadline or '')) != before:
                if so_return_for_reapproval(so, request.user, "Order details changed"):
                    messages.warning(request, f"{so.so_number} was already approved, so it's back in Draft. Request approval again.")

        elif action == 'request_approval':
            manager_id = request.POST.get('manager_id')
            remarks = request.POST.get('remarks', '').strip()
            
            mgr = CustomUser.objects.filter(id=manager_id).first()
            # Approval is only for unapproved orders (or resending one still pending); an approved or shipped order can't be sent back through it.
            problem = (None if so.status in UNAPPROVED_STATUSES
                       else f"{so.so_number} is {so.status}, so it can't be sent for approval again.")
            problem = problem or approver_problem(mgr, APPROVE_SALES_ORDERS)
            if problem:
                messages.error(request, problem)
            else:
                if so.status == 'Pending Approval':
                    clear_approval_notifications(so.so_number)   # resent: the previous approver's request is void
                so.status = 'Pending Approval'
                so.assigned_to = mgr
                so.approval_remarks = remarks
                so.save()
                
                OrderTimeline.objects.create(sales_order=so, action=f"Approval Requested from {mgr.get_full_name() or mgr.username}{self_approval_note(request.user, mgr)}. Remarks: {remarks}", user=request.user)
                Notification.objects.create(
                    user=mgr,
                    message=f"Sales Order {so.so_number} requires your approval.",
                    link=reverse('approvals_inbox')
                )
                messages.success(request, f"Approval requested from {mgr.get_full_name() or mgr.username}.")
                
        elif action == 'send_to_manufacturing':
            from datetime import timedelta

            # Only approved orders still being fulfilled; Draft/unapproved or already shipping orders can't raise runs.
            if so.status not in SO_MANUFACTURABLE:
                messages.error(request, f"{so.so_number} is {so.status}, so it can't be sent to manufacturing. Only approved orders that aren't ready to ship yet can.")
                return redirect('so_detail', pk=so.pk)

            plant_id = request.POST.get('manufacturing_plant_id')
            plant = None
            if plant_id:
                plant = Warehouse.objects.filter(id=plant_id).first()

            from .utils import create_shortage_production_runs
            created_any = create_shortage_production_runs(so, plant, request.user)

            if so.status == 'Pending':   # an order already In Production keeps that status
                so.status = 'Awaiting Acknowledgement'
                so.save()
            OrderTimeline.objects.create(sales_order=so, action="Shortages sent to manufacturing queue.", user=request.user)
            if created_any:
                messages.success(request, f"Manufacturing orders created for shortages on {so.so_number}.")
            else:
                messages.success(request, f"Status updated. No new manufacturing orders were needed.")
                
        elif action == 'add_follower':
            user_id = request.POST.get('user_id')
            user_to_add = CustomUser.objects.filter(id=user_id).first()
            if user_to_add:
                if not so.followers.filter(pk=user_to_add.pk).exists():
                    so.followers.add(user_to_add)
                    notify_added_follower(request.user, user_to_add, f"Sales Order {so.so_number}", reverse('so_detail', args=[so.pk]))
                messages.success(request, f"Added {user_to_add.username} as a follower.")
            else:
                messages.error(request, "User not found.")

        elif action == 'remove_follower':
            user_id = request.POST.get('user_id')
            user_to_remove = CustomUser.objects.filter(id=user_id).first()
            if user_to_remove:
                so.followers.remove(user_to_remove)
                messages.success(request, f"Removed {user_to_remove.username} from followers.")

        elif action == 'unallocate_so_stock':
            if not request.user.has_perm('core.delete_stockallocation'):
                messages.error(request, "You don't have permission to unallocate stock from an order.")
                return redirect('so_detail', pk=so.pk)

            if so.status in ('Shipped', 'Delivered'):
                messages.error(request, "Stock can't be unallocated once the order has shipped.")
                return redirect('so_detail', pk=so.pk)

            alloc_id = request.POST.get('alloc_id')
            qty_str = request.POST.get('quantity')
            target_so_id = request.POST.get('target_so_id')
            try:
                allocation = get_object_or_404(StockAllocation, id=alloc_id, sales_order=so)
                target_so = SalesOrder.objects.filter(id=target_so_id).first() if target_so_id else None

                from .utils import unallocate_so_batch
                unallocate_so_batch(allocation, qty_str, request.user, target_so=target_so)

                if target_so:
                    messages.success(request, f"Transferred {qty_str} to {target_so.so_number}.")
                else:
                    messages.success(request, f"Released {qty_str} back to available stock.")
            except Exception as e:
                messages.error(request, f"Error unallocating stock: {e}")

        elif action == 'consolidate_stock':
            # "One delivery": move reserved stock outside the origin warehouse there first, one transfer per warehouse.
            # The order keeps the reservation; the shipment tag marks it as on that truck.
            from .utils import so_stock_by_warehouse
            if so.status != 'Ready to Ship':
                messages.error(request, "An order can only be consolidated once it is Ready to Ship.")
            else:
                groups = [g for g in so_stock_by_warehouse(so) if g['warehouse'] and g['warehouse'].id != so.origin_warehouse_id]
                held = sorted({a.batch.batch_number for g in groups for a in g['allocations'] if a.batch.status == 'Quarantined'})
                if held:
                    messages.error(request, f"Batch {', '.join(held)} is in QA quarantine and can't be moved until QA releases it.")
                elif not groups:
                    messages.info(request, f"All of this order's stock is already at {so.origin_warehouse.name}.")
                else:
                    created = []
                    with transaction.atomic():
                        for g in groups:
                            tracking = generate_next_code(Shipment, 'tracking_number', 'SHP', 1001, pad=4)
                            sh = Shipment.objects.create(
                                tracking_number=tracking, direction='Transfer', status='Draft',
                                sales_order=so, origin_warehouse=g['warehouse'],
                                destination_warehouse=so.origin_warehouse, last_edited_by=request.user,
                            )
                            for a in g['allocations']:
                                ShipmentItem.objects.create(shipment=sh, product=a.batch.product, material=a.batch.material,
                                                            batch=a.batch, quantity=a.quantity)
                                a.shipment = sh
                                a.save(update_fields=['shipment'])
                            OrderTimeline.objects.create(shipment=sh, action=f"Drafted to move {so.so_number}'s stock from {g['warehouse'].name} to {so.origin_warehouse.name} for one delivery.", user=request.user)
                            created.append(tracking)
                        OrderTimeline.objects.create(
                            sales_order=so,
                            action=f"Chose one delivery: moving stock to {so.origin_warehouse.name} first via {', '.join(created)}.",
                            user=request.user,
                        )
                    messages.success(request, f"Drafted stock move(s) {', '.join(created)} to {so.origin_warehouse.name}. Take them through Logistics; once they arrive, create the logistics order to ship everything together.")

        elif action == 'cancel_so':
            reason = request.POST.get('reason', '').strip()
            problem = so_cancel_problem(so)
            if not reason:
                problem = "Give a reason for cancelling the order."
            if problem:
                messages.error(request, problem)
                return redirect('so_detail', pk=so.pk)
            cancel_sales_order(so, reason, request.user)
            messages.success(request, f"{so.so_number} cancelled. Its reserved stock is free again and unstarted runs and shipments were cancelled.")

        elif action == 'cancel_delivery_plan':
            # Switch plans: cancel unapproved stock moves/outbound shipments and return their reservations to the order.
            plan = so.shipments.filter(direction__in=['Transfer', 'Outbound']).exclude(status__in=['Completed', 'Cancelled'])
            locked = plan.exclude(status__in=SO_PLAN_SWITCHABLE)
            if not plan.exists():
                messages.info(request, "There is no delivery plan to switch.")
            elif locked.exists():
                messages.error(request, f"{', '.join(locked.values_list('tracking_number', flat=True))} is already approved or on its way, so the plan can't be switched.")
            else:
                with transaction.atomic():
                    for sh in plan:
                        for a in StockAllocation.objects.filter(shipment=sh):
                            a.sales_order = so
                            a.shipment = None
                            a.save(update_fields=['sales_order', 'shipment'])
                        sh.status = 'Cancelled'
                        sh.save(update_fields=['status'])
                        clear_approval_notifications(sh.tracking_number)
                        OrderTimeline.objects.create(shipment=sh, action=f"Cancelled: {so.so_number}'s delivery plan was switched. Reservations returned to the order.", user=request.user)
                    OrderTimeline.objects.create(sales_order=so, action=f"Delivery plan switched. Cancelled {', '.join(sh.tracking_number for sh in plan)}; stock stays reserved for this order.", user=request.user)
                messages.success(request, "Delivery plan cancelled. The stock is still reserved for this order - choose how to deliver it again.")

        if action in ('add_so_item', 'remove_so_item', 'update_so_status', 'request_approval', 'send_to_manufacturing'):
            # "Last edited by" covers line-item and status changes, not just the header form
            from django.utils import timezone as _tz
            SalesOrder.objects.filter(pk=so.pk).update(updated_by=request.user, updated_at=_tz.now())

        return redirect('so_detail', pk=so.pk)

    # BOM readiness and Allocation logic
    line_items_with_bom = []
    from django.db.models import Sum, F
    from .utils import get_batch_produced_for, so_line_commitment

    for item in so.items.select_related('product').all():
        allocations_qs = StockAllocation.objects.filter(sales_order=so, batch__product=item.product).select_related('batch', 'batch__produced_in', 'batch__purchase_order')
        # Reserved, on logistics orders, and shipped stock all count as covered
        commit = so_line_commitment(so, item.product, item.quantity_shipped)
        allocated = float(commit['held'])
        in_logistics = float(commit['in_logistics'])

        allocation_rows = []
        for a in allocations_qs:
            if a.batch.produced_in_id:
                source = f"Newly Manufactured (Run {a.batch.produced_in.run_number})"
            elif a.batch.purchase_order_id:
                source = f"Existing Stock (PO {a.batch.purchase_order.po_number})"
            else:
                source = "Existing Stock"

            produced_for = get_batch_produced_for(a.batch)
            if produced_for and produced_for['so_number'] == so.so_number:
                produced_for = None  # only worth flagging when it differs from the order you're looking at

            allocation_rows.append({
                'alloc_id': a.id, 'batch': a.batch, 'quantity': a.quantity, 'source': source,
                'produced_for': produced_for,
                'can_unallocate': so.status not in ('Shipped', 'Delivered'),
            })

        # Stock moved onto logistics orders is no longer an order reservation but stays listed read-only.
        for si in (ShipmentItem.objects
                   .filter(shipment__sales_order=so, shipment__direction='Outbound', product=item.product, batch__isnull=False)
                   .exclude(shipment__status='Cancelled')
                   .select_related('batch', 'batch__produced_in', 'batch__purchase_order', 'shipment')):
            b = si.batch
            source = (f"Newly Manufactured (Run {b.produced_in.run_number})" if b.produced_in_id
                      else f"Existing Stock (PO {b.purchase_order.po_number})" if b.purchase_order_id
                      else "Existing Stock")
            allocation_rows.append({
                'alloc_id': None, 'batch': b,
                'quantity': si.received_quantity if si.shipment.status == 'Completed' else si.quantity,
                'source': source, 'produced_for': None, 'can_unallocate': False,
                'shipment': si.shipment,
            })

        transfer_targets = SalesOrder.objects.filter(items__product=item.product).exclude(pk=so.pk).exclude(
            status__in=['Draft', 'Cancelled', 'Rejected', 'Shipped', 'Delivered']
        ).distinct().order_by('so_number')

        fulfilled = float(commit['total']) >= float(item.quantity_ordered)

        global_avail = float(Batch.objects.filter(product=item.product, status='Active').annotate(avail=F('quantity')-F('allocated_quantity')).aggregate(s=Sum('avail'))['s'] or 0)
        
        incoming_production = float(ProductionRun.objects.filter(
            sales_order=so,
            target_product=item.product,
            status__in=['Pending Approval', 'Pending Allocation', 'Awaiting Materials', 'Planned', 'InProgress', 'Paused']
        ).aggregate(s=Sum('expected_yield'))['s'] or 0)

        # Deficit is always what this order still lacks; free stock only drives the Draft badge
        if so.status in ['Ready to Ship', 'Shipped', 'Delivered', 'Cancelled']:
            deficit = 0.0
        else:
            deficit = max(0.0, float(item.quantity_ordered) - float(commit['total']) - incoming_production)
        if so.status in ['Draft', 'Pending Approval']:
            stock_short = float(item.quantity_ordered) - global_avail - incoming_production > 0
        else:
            stock_short = deficit > 0

        recipes = item.product.recipe_items.select_related('material').all()
        bom_rows = []
        can_make_units = None
        for r in recipes:
            avail = float(Batch.objects.filter(material=r.material, status='Active').annotate(avail=F('quantity')-F('allocated_quantity')).aggregate(s=Sum('avail'))['s'] or 0)
            needed_per_unit = float(r.quantity_required)
            needed_for_order = needed_per_unit * float(item.quantity_ordered)
            sufficient = avail >= needed_for_order
            if needed_per_unit > 0:
                max_from_this = int(avail / needed_per_unit)
                can_make_units = min(can_make_units, max_from_this) if can_make_units is not None else max_from_this
            bom_rows.append({
                'material': r.material,
                'required_per_unit': needed_per_unit,
                'required_for_order': needed_for_order,
                'available': avail,
                'sufficient': sufficient,
            })
        line_items_with_bom.append({
            'item': item,
            'bom_rows': bom_rows,
            'can_make_units': can_make_units if can_make_units is not None else '∞',
            'bom_ready': all(row['sufficient'] for row in bom_rows),
            'allocated': allocated,
            'in_logistics': in_logistics,
            'deficit': deficit,
            'stock_short': stock_short,
            'free_stock': global_avail if so.status in ['Draft', 'Pending Approval'] else None,
            'fulfilled': fulfilled,
            'incoming_production': incoming_production,
            'subtotal': (item.quantity_ordered * item.unit_price) if item.unit_price is not None else None,
            'allocation_rows': allocation_rows,
            'transfer_targets': transfer_targets,
        })

    has_deficit = any(row['deficit'] > 0 for row in line_items_with_bom)
    manufacturing_plants = Warehouse.objects.filter(location_type__in=Warehouse.MANUFACTURING_TYPES).order_by('name')
    has_unshipped_allocation = so.allocations.filter(shipment__isnull=True).exists()

    from .utils import so_stock_by_warehouse
    stock_split = so_stock_by_warehouse(so)
    open_plan = list(so.shipments.filter(direction__in=['Transfer', 'Outbound'])
                     .exclude(status__in=['Completed', 'Cancelled']).order_by('id'))
    open_stock_moves = [sh for sh in open_plan if sh.direction == 'Transfer']
    open_outbound = [sh for sh in open_plan if sh.direction == 'Outbound']

    # A finished run whose batch is still held for QA release isn't usable yet: say so.
    qa_run_ids = set(Batch.objects.filter(status='Quarantined', produced_in__sales_order=so).values_list('produced_in_id', flat=True))
    so_runs = [r for r in so.production_runs.all() if r.status != 'Cancelled']
    for r in so_runs:
        r.in_qa = r.id in qa_run_ids

    context = {
        'so': so,
        # Scrapped/cancelled runs are history, not part of how this order is being fulfilled
        'so_runs': so_runs,
        'so_in_qa': any(r.in_qa for r in so_runs),
        'stock_split': stock_split,
        'open_stock_moves': open_stock_moves,
        'open_outbound': open_outbound,
        'can_switch_plan': bool(open_plan) and all(sh.status in SO_PLAN_SWITCHABLE for sh in open_plan),
        'line_items': line_items_with_bom,
        'order_total': so.total_value,
        'unpriced_lines': sum(1 for row in line_items_with_bom if row['subtotal'] is None),
        'has_deficit': has_deficit,
        'has_unshipped_allocation': has_unshipped_allocation,
        'items_locked': so.status in SO_ITEMS_LOCKED,
        'products': products,
        'warehouses': warehouses,
        'manufacturing_plants': manufacturing_plants,
        'so_status_choices': [c for c in SalesOrder.STATUS_CHOICES if c[0] == so.status or c[0] in SO_MANUAL_TRANSITIONS.get(so.status, [])],
        'managers': approvers(APPROVE_SALES_ORDERS),
        'can_decide_approval': may_decide_approval(request.user, so),
        'all_users': CustomUser.objects.all().order_by('username'),
    }
    return render(request, 'so_detail.html', context)


# Purchase order detail
@login_required
def po_detail_view(request, pk):
    denied = action_denied(request, 'po_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    po = get_object_or_404(PurchaseOrder.objects.prefetch_related('items__material', 'timeline__user', 'shipments'), pk=pk)
    materials = Material.objects.all().order_by('id')
    warehouses = Warehouse.objects.all().order_by('name')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action in ('add_po_item', 'remove_po_item') and po.status in PO_ITEMS_LOCKED:
            messages.error(request, f"{po.po_number} is {po.status}, so its line items can't be changed.")
            return redirect('po_detail', pk=pk)

        if action == 'add_po_item':
            if po.linked_production_run:
                messages.error(request, "Cannot modify items for a PO automatically generated from a Production Run shortage.")
                return redirect('po_detail', pk=pk)
                
            mat_id = request.POST.get('material_id')
            qty = request.POST.get('quantity_ordered', 0)
            unit_price = request.POST.get('unit_price', None)
            rental_rate = request.POST.get('negotiated_rental_rate_per_mt', None)
            qty, err = read_decimal(request.POST, 'quantity_ordered', "Quantity", above=0)
            unit_price, price_err = read_decimal(request.POST, 'unit_price', "Unit price", min_value=0, required=False)
            rental_rate, rate_err = read_decimal(request.POST, 'negotiated_rental_rate_per_mt', "Rental rate", min_value=0, required=False)
            err = err or price_err or rate_err
            if err:
                messages.error(request, err)
                return redirect('po_detail', pk=pk)
            try:
                mat = get_object_or_404(Material, id=mat_id)
                if not mat.is_active:
                    messages.error(request, f"{mat.name} is archived, so it can't be ordered. Restore it first.")
                    return redirect('po_detail', pk=pk)
                PurchaseOrderDetail.objects.create(
                    purchase_order=po,
                    material=mat,
                    quantity_ordered=qty,
                    unit_price=unit_price,
                    negotiated_rental_rate_per_mt=rental_rate
                )
                OrderTimeline.objects.create(purchase_order=po, action=f"Line item added: {mat.name} x{qty}", user=request.user)
                messages.success(request, f"Added {mat.name} to {po.po_number}.")
                if po_return_for_reapproval(po, request.user, "Line item added"):
                    messages.warning(request, f"{po.po_number} was already approved, so it's back in Draft. Request approval again.")
            except Exception as e:
                messages.error(request, f"Error adding item: {e}")

        elif action == 'remove_po_item':
            if po.linked_production_run:
                messages.error(request, "Cannot modify items for a PO automatically generated from a Production Run shortage.")
                return redirect('po_detail', pk=pk)
                
            item_id = request.POST.get('item_id')
            try:
                item = get_object_or_404(PurchaseOrderDetail, id=item_id, purchase_order=po)
                name = item.material.name
                # Receipts are credited by material, so a line can't go while a delivery still carries it
                carrying = po.shipments.filter(direction='Inbound', items__material=item.material).exclude(status__in=['Completed', 'Cancelled'])
                if carrying.exists():
                    messages.error(request, f"{name} is on delivery {', '.join(carrying.values_list('tracking_number', flat=True).distinct())}. "
                                            "Receive or cancel that delivery before removing the line.")
                    return redirect('po_detail', pk=pk)
                item.delete()
                OrderTimeline.objects.create(purchase_order=po, action=f"Line item removed: {name}", user=request.user)
                messages.success(request, f"Removed {name} from {po.po_number}.")
                if po_return_for_reapproval(po, request.user, "Line item removed"):
                    messages.warning(request, f"{po.po_number} was already approved, so it's back in Draft. Request approval again.")
            except Exception as e:
                messages.error(request, f"Error removing item: {e}")

        elif action == 'mark_received':
            item_id = request.POST.get('item_id')
            qty_received = request.POST.get('qty_received', 0)
            if po.status in UNAPPROVED_STATUSES:
                messages.error(request, f"{po.po_number} hasn't been approved yet, so nothing can be received against it.")
                return redirect('po_detail', pk=po.pk)
            if po.status == 'Cancelled':
                messages.error(request, f"{po.po_number} was cancelled, so nothing can be received against it.")
                return redirect('po_detail', pk=po.pk)
            try:
                item = get_object_or_404(PurchaseOrderDetail, id=item_id, purchase_order=po)
                old_qty = Decimal(str(item.quantity_received))
                try:
                    new_qty = Decimal(str(qty_received).strip())
                except InvalidOperation:
                    messages.error(request, "Enter the received quantity as a number.")
                    return redirect('po_detail', pk=po.pk)
                if po.status == 'Completed' and new_qty > old_qty:
                    messages.error(request, f"{po.po_number} is completed, so a receipt can only be lowered to correct it.")
                    return redirect('po_detail', pk=po.pk)
                if new_qty < 0 or new_qty > item.quantity_ordered:
                    messages.error(request, f"Received quantity must be between 0 and the {item.quantity_ordered} ordered. If the supplier sent more, change the line's ordered quantity first.")
                    return redirect('po_detail', pk=po.pk)
                delta = new_qty - old_qty
                if delta == 0:
                    return redirect('po_detail', pk=po.pk)
                # Optional dates from the supplier's label; blank = today and the material's shelf life
                supplier_expiry, date_err = read_date(request.POST, 'expiry_date', "Expiry date")
                supplier_mfg, mfg_err = read_date(request.POST, 'manufacturing_date', "Manufacture date")
                if date_err or mfg_err:
                    messages.error(request, date_err or mfg_err)
                    return redirect('po_detail', pk=po.pk)
                if supplier_expiry and supplier_mfg and supplier_expiry <= supplier_mfg:
                    messages.error(request, "The expiry date must be after the manufacture date.")
                    return redirect('po_detail', pk=po.pk)

                from .utils import apply_po_material_receipt
                with transaction.atomic():
                    batch = apply_po_material_receipt(item, delta, request.user, expiry_date=supplier_expiry,
                                                      manufacturing_date=supplier_mfg)
                into = f" (+{delta} into Batch {batch.batch_number})" if batch else ""
                OrderTimeline.objects.create(purchase_order=po, action=f"Received {new_qty} of {item.material.name}{into}", user=request.user)
                po.refresh_from_db()
                if delta > 0 and item.material.inspect_on_receipt:
                    messages.success(request, f"Updated received quantity for {item.material.name}. The {delta} received is on QA hold until it's inspected.")
                elif delta > 0 and po.status != 'Completed':
                    messages.success(request, f"Added {delta} of {item.material.name} to Batch {batch.batch_number}. It stays on hold until {po.po_number} is completed.")
                elif delta > 0:
                    messages.success(request, f"Updated received quantity for {item.material.name} and added {delta} to inventory.")
                else:
                    messages.success(request, f"Corrected received quantity for {item.material.name} and took {-delta} back out of inventory.")
            except Exception as e:
                messages.error(request, f"Error updating received qty: {e}")

        elif action == 'cancel_po':
            reason = request.POST.get('reason', '').strip()
            problem = po_cancel_problem(po) or (None if reason else "Give a reason for cancelling the PO.")
            if problem:
                messages.error(request, problem)
                return redirect('po_detail', pk=po.pk)
            cancel_purchase_order(po, reason, request.user)
            messages.success(request, f"{po.po_number} cancelled. Its deliveries that hadn't left were cancelled too.")

        elif action == 'update_po_status':
            new_status = request.POST.get('status')
            if new_status not in po_manual_transitions(po):
                messages.error(request, f"{po.po_number} can't be changed from {po.status} to {new_status} by hand.")
                return redirect('po_detail', pk=po.pk)
            reopening = po.status == 'Completed'
            po.status = new_status
            if new_status in ['Pending', 'Partially Received'] and not reopening:
                po.approved_by = request.user
            if reopening:
                po.completed_date = None
            po.save()
            OrderTimeline.objects.create(purchase_order=po, action="PO reopened to receive more" if reopening else f"Status updated to '{new_status}'", user=request.user)
            if new_status == 'Completed':
                from .utils import release_po_held_batches
                release_po_held_batches(po, request.user)

            # Notify followers of status change
            for follower in po.followers.all():
                if follower != request.user:
                    Notification.objects.create(
                        user=follower,
                        message=f"Purchase Order {po.po_number} status changed to {new_status}.",
                        link=reverse('po_detail', args=[po.pk])
                    )

            messages.success(request, f"{po.po_number} status updated to {new_status}.")

        elif action == 'update_po_header':
            po.supplier_name = request.POST.get('supplier_name', po.supplier_name)
            wh_id = request.POST.get('target_warehouse_id')
            if wh_id:
                po.target_warehouse = get_object_or_404(Warehouse, id=wh_id)
            deadline = request.POST.get('expected_delivery_date')
            po.expected_delivery_date = deadline if deadline else po.expected_delivery_date
            po.updated_by = request.user
            po.save()
            messages.success(request, f"{po.po_number} details updated.")

        elif action == 'request_approval':
            manager_id = request.POST.get('manager_id')
            remarks = request.POST.get('remarks', '').strip()
            
            mgr = CustomUser.objects.filter(id=manager_id).first()
            problem = approver_problem(mgr, APPROVE_PURCHASE_ORDERS)
            if problem:
                messages.error(request, problem)
            else:
                po.status = 'Pending Approval'
                po.assigned_to = mgr
                po.approval_remarks = remarks
                po.save()
                
                OrderTimeline.objects.create(purchase_order=po, action=f"Approval Requested from {mgr.get_full_name() or mgr.username}{self_approval_note(request.user, mgr)}. Remarks: {remarks}", user=request.user)
                Notification.objects.create(
                    user=mgr,
                    message=f"Purchase Order {po.po_number} requires your approval.",
                    link=reverse('approvals_inbox')
                )
                messages.success(request, f"Approval requested from {mgr.get_full_name() or mgr.username}.")
        elif action == 'add_follower':
            user_id = request.POST.get('user_id')
            user_to_add = CustomUser.objects.filter(id=user_id).first()
            if user_to_add:
                if not po.followers.filter(pk=user_to_add.pk).exists():
                    po.followers.add(user_to_add)
                    notify_added_follower(request.user, user_to_add, f"Purchase Order {po.po_number}", reverse('po_detail', args=[po.pk]))
                messages.success(request, f"Added {user_to_add.username} as a follower.")
            else:
                messages.error(request, "User not found.")

        elif action == 'remove_follower':
            user_id = request.POST.get('user_id')
            user_to_remove = CustomUser.objects.filter(id=user_id).first()
            if user_to_remove:
                po.followers.remove(user_to_remove)
                messages.success(request, f"Removed {user_to_remove.username} from followers.")

        return redirect('po_detail', pk=po.pk)

    # Build line item data with totals
    po_items_data = []
    po_total = 0
    # Receipt batches link to the PO and material, not the line, so a line shows its material's batches
    from .utils import po_wait_note
    from .qa import HOLD_ACTIONS
    po_batches = {}
    for b in Batch.objects.filter(purchase_order=po, material__isnull=False).prefetch_related('qa_events', 'qa_requests').order_by('id'):
        if b.status == 'Quarantined':
            holds = [e for e in b.qa_events.all() if e.action in HOLD_ACTIONS]
            last_hold = max(holds, key=lambda e: (e.created_at, e.id)) if holds else None
            if any(r.status in ('Open', 'In Review') for r in b.qa_requests.all()):
                tag = 'QA check'
            elif last_hold and last_hold.note == po_wait_note(po):
                tag = 'On hold (PO)'
            else:
                tag = 'On hold'
        else:
            tag = {'Active': 'Released', 'Depleted': 'Used up', 'Spoiled': 'Written off'}.get(b.status, b.status)
        po_batches.setdefault(b.material_id, []).append({'batch': b, 'tag': tag})
    for item in po.items.select_related('material').all():
        subtotal = float(item.quantity_ordered) * float(item.unit_price or 0)
        po_total += subtotal
        po_items_data.append({
            'item': item,
            'subtotal': subtotal,
            'pct_received': min(100, int((float(item.quantity_received) / float(item.quantity_ordered) * 100))) if float(item.quantity_ordered) > 0 else 0,
            'batches': po_batches.get(item.material_id, []),
        })

    context = {
        'po': po,
        'po_items_data': po_items_data,
        'po_total': po_total,
        'materials': materials,
        'warehouses': warehouses,
        'po_status_choices': [c for c in PurchaseOrder.STATUS_CHOICES if c[0] == po.status or c[0] in po_manual_transitions(po)],
        'unapproved_statuses': UNAPPROVED_STATUSES,
        'items_locked': po.status in PO_ITEMS_LOCKED,
        'can_cancel': po_cancel_problem(po) is None,
        'cancel_note': po.timeline.filter(action__startswith='PO cancelled').last() if po.status == 'Cancelled' else None,
        'managers': approvers(APPROVE_PURCHASE_ORDERS),
        'can_decide_approval': may_decide_approval(request.user, po),
        'all_users': CustomUser.objects.all().order_by('username'),
    }
    return render(request, 'po_detail.html', context)


# Manufacturing and readiness
@login_required
def manufacturing_view(request):
    denied = action_denied(request, 'readiness')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_run':
            run_number_auto = request.POST.get('run_number_auto') == '1'
            run_number = request.POST.get('run_number')
            if run_number_auto or not run_number:
                run_number = generate_next_code(ProductionRun, 'run_number', 'RUN', 801, pad=3)
            prod_id = request.POST.get('target_product_id')
            plant_id = request.POST.get('manufacturing_plant_id')
            so_id = request.POST.get('sales_order_id')
            try:
                yield_qty = float(request.POST.get('expected_yield', 100))
                prod = get_object_or_404(Product, id=prod_id)
                if not prod.is_active:
                    messages.error(request, f"{prod.name} is archived, so no new production run can be scheduled for it. Restore it first.")
                    return redirect('readiness')
                plant = get_object_or_404(Warehouse, id=plant_id)
                so = SalesOrder.objects.filter(id=so_id).first() if so_id else None
                if so and so.status in UNAPPROVED_STATUSES + ('Cancelled',):
                    messages.error(request, f"{so.so_number} hasn't been approved yet, so no production run can be scheduled for it.")
                    return redirect('readiness')

                tomorrow = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
                
                run = ProductionRun.objects.create(
                    run_number=run_number, target_product=prod,
                    expected_yield=yield_qty, manufacturing_plant=plant,
                    status='Pending Approval', supervisor=request.user,
                    created_by=request.user,
                    sales_order=so,
                    start_time=tomorrow,
                    end_time=tomorrow + timedelta(hours=4)
                )
                OrderTimeline.objects.create(production_run=run, action="Manufacturing Order created.", user=request.user)

                # Auto-update SO status to "In Production" if linked
                if so and so.status not in ['In Production', 'Ready to Ship', 'Shipped', 'Delivered']:
                    so.status = 'In Production'
                    so.save()
                    OrderTimeline.objects.create(
                        sales_order=so,
                        action=f"Status auto-updated to 'In Production' (Run {run_number} scheduled)",
                        user=request.user
                    )

                messages.success(request, f"Production Run {run_number} scheduled for {prod.name}.")
                archived = archived_recipe_materials(prod)
                if archived:
                    messages.warning(request, f"Heads up: {prod.name}'s recipe uses archived material(s): {', '.join(m.name for m in archived)}.")
            except Exception as e:
                messages.error(request, f"Error scheduling run: {e}")
        elif action == 'cancel_allocation':
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)

            from .utils import cancel_run_allocation
            problem = cancel_run_allocation(run, request.user)
            if problem:
                messages.error(request, problem)
            else:
                messages.success(request, f"Allocation for Run {run.run_number} has been cancelled.")
            return redirect('readiness')

        elif action == 'draft_transfer_from_shortage':
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)
            
            if run.status != 'Awaiting Materials':
                messages.error(request, "Run is not awaiting materials.")
                return redirect('readiness')
                
            target_warehouse = run.manufacturing_plant
            
            if not target_warehouse:
                messages.error(request, "No manufacturing plant assigned to this run.")
                return redirect('readiness')
                
            # Shortfall per material
            shortages = {}
            for req in run.target_product.recipe_items.select_related('material'):
                needed = Decimal(str(req.quantity_required)) * Decimal(str(run.expected_yield))
                allocated = sum((a.quantity for a in run.allocations.filter(batch__material=req.material)), Decimal('0'))
                if needed - allocated > 0:
                    shortages[req.material] = (needed - allocated).quantize(Decimal('0.01'), rounding=ROUND_CEILING)

            # Free stock elsewhere, earliest expiry first
            from django.db.models import F
            sources = {}
            for mat in shortages:
                for b in unexpired(Batch.objects.filter(material=mat, status='Active', warehouse__isnull=False)
                                   .exclude(warehouse=target_warehouse)).order_by(F('expiry_date').asc(nulls_last=True), 'manufacturing_date'):
                    if b.quantity - b.allocated_quantity > 0:
                        sources.setdefault(b.warehouse, []).append(b)
            if not sources:
                messages.error(request, "No other facility has free stock of the missing materials. Draft a PO instead.")
                return redirect('readiness')

            # Ship from the facility that covers the most
            def coverable(wh):
                return sum(min(shortages[m], sum((b.quantity - b.allocated_quantity for b in sources[wh] if b.material == m), Decimal('0')))
                           for m in shortages)
            origin = max(sources, key=coverable)

            with transaction.atomic():
                shipment = Shipment.objects.create(
                    tracking_number=generate_next_code(Shipment, 'tracking_number', 'SHP', 1001, pad=4),
                    direction='Transfer', status='Draft', linked_production_run=run,
                    origin_warehouse=origin, destination_warehouse=target_warehouse,
                    is_auto_generated=True, last_edited_by=request.user,
                )
                still_short = []
                for mat, short in shortages.items():
                    for b in (b for b in sources[origin] if b.material == mat):
                        if short <= 0:
                            break
                        take = min(b.quantity - b.allocated_quantity, short)
                        ShipmentItem.objects.create(shipment=shipment, batch=b, material=mat, quantity=take)
                        b.allocated_quantity += take
                        b.save(update_fields=['allocated_quantity'])
                        StockAllocation.objects.create(batch=b, production_run=run, shipment=shipment, quantity=take)
                        short -= take
                    if short > 0:
                        still_short.append(f"{short} {mat.unit_of_measure} of {mat.name}")
                OrderTimeline.objects.create(production_run=run, action=f"Draft transfer {shipment.tracking_number} from {origin.name} raised for the shortfall.", user=request.user)

            messages.success(request, f"Draft transfer {shipment.tracking_number} created from {origin.name} to {target_warehouse.name}.")
            if still_short:
                messages.warning(request, f"{origin.name} can't cover everything. Still short: {', '.join(still_short)}.")
            return redirect('readiness')
            
        elif action == 'draft_po_from_shortage':
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)
            
            if run.status != 'Awaiting Materials':
                messages.error(request, "Run is not awaiting materials.")
                return redirect('readiness')
                
            # Find shortages
            shortages, archived_short = [], []
            for req in run.target_product.recipe_items.all():
                needed = float(req.quantity_required) * float(run.expected_yield)
                # Count allocated quantities for THIS run
                allocated = sum(alloc.quantity for alloc in run.allocations.filter(batch__material=req.material))
                short = max(0, needed - float(allocated))
                if short > 0 and not req.material.is_active:
                    archived_short.append(req.material.name)
                elif short > 0:
                    shortages.append({
                        'material': req.material,
                        'qty': short
                    })
            if archived_short:
                messages.warning(request, f"Left off the PO because they're archived: {', '.join(archived_short)}.")
                OrderTimeline.objects.create(production_run=run, user=request.user,
                                             action=f"Archived material(s) left off the shortage PO: {', '.join(archived_short)}")

            if not shortages:
                messages.info(request, "No shortages found." if not archived_short else "Nothing else to order.")
                return redirect('readiness')
                
            # Create Draft PO
            po_number = generate_next_code(PurchaseOrder, 'po_number', 'PO', 601, pad=3)
            
            # Try to auto-assign a purchaser
            purchaser = users_with_perm(HANDLE_PURCHASING, include_superusers=False).order_by('id').first()

            with transaction.atomic():
                po = PurchaseOrder.objects.create(
                    po_number=po_number,
                    supplier_name="To Be Determined",
                    target_warehouse=run.manufacturing_plant,
                    status='Draft',
                    created_by=request.user,
                    assigned_to=purchaser,
                    linked_production_run=run
                )
                
                for short in shortages:
                    PurchaseOrderDetail.objects.create(
                        purchase_order=po,
                        material=short['material'],
                        quantity_ordered=short['qty']
                    )
                    
                # Link PO to run for UI display
                run.linked_pos.add(po)
                
                # Notify purchaser
                if purchaser:
                    Notification.objects.create(
                        user=purchaser,
                        message=f"Draft PO {po.po_number} created for materials short in Production Run {run.run_number}. Please complete and submit for approval.",
                        link=reverse('po_detail', args=[po.pk])
                    )
                    
                messages.success(request, f"Draft PO {po.po_number} created successfully and assigned to Purchasing.")
            return redirect('readiness')

        elif action == 'approve_run':
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)
            if not may_decide_run_approval(request.user, run):
                messages.error(request, "Only someone who may approve production runs (the named approver, if there is one) can approve a run that is pending approval.")
                return redirect('readiness')
            clear_approval_notifications(run.run_number)

            from .utils import approve_production_run
            approve_production_run(run, request.user)
            messages.success(request, f"Production Run {run.run_number} approved. Please allocate materials to begin.")

        elif action == 'update_run_schedule':
            run_id = request.POST.get('run_id')
            start_str = request.POST.get('start_time')
            end_str = request.POST.get('end_time')
            run = get_object_or_404(ProductionRun, id=run_id)
            from django.utils.dateparse import parse_datetime
            if start_str:
                run.start_time = parse_datetime(start_str)
            if end_str:
                run.end_time = parse_datetime(end_str)
            run.save()
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': True})
            return redirect('readiness')

        elif action == 'start_run':
            if not request.user.has_perm('core.change_productionrun'):
                messages.error(request, "Permission Denied: You do not have permission to start Production Runs.")
                return redirect('readiness')
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)
            from .qa import run_hold_problem
            hold_problem = run_hold_problem(run) if run.status == 'Planned' else None
            if hold_problem:
                messages.error(request, hold_problem)
            elif run.status == 'Planned':
                run.status = 'InProgress'
                run.start_time = timezone.now()
                run.save()
                from .utils import mark_order_in_production
                mark_order_in_production(run, request.user)
                messages.success(request, f"Production Run {run.run_number} started.")

        elif action == 'complete_run':
            if not request.user.has_perm('core.change_productionrun'):
                messages.error(request, "Permission Denied: You do not have permission to complete Production Runs.")
                return redirect('readiness')
            # Runs are completed on the run page (needs quantities, extra-material sources and variance checks), never from the list.
            run = get_object_or_404(ProductionRun, id=request.POST.get('run_id'))
            messages.info(request, f"Enter the material usage and yield for {run.run_number} to complete it.")
            return redirect(reverse('production_run_detail', args=[run.pk]) + '#finalize')

        return redirect('readiness')

    loc_filter = request.GET.get('loc_filter', '')
    runs = ProductionRun.objects.select_related('target_product', 'supervisor', 'manufacturing_plant', 'sales_order').order_by('-id')
    if loc_filter:
        runs = runs.filter(manufacturing_plant_id=loc_filter)

    sort_by = request.GET.get('sort', '-id')
    if sort_by in ['run_number', '-run_number', 'target_product__name', '-target_product__name', 'status', '-status', 'start_time', '-start_time']:
        runs = runs.order_by(sort_by)

    from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
    page_size = request.GET.get('page_size', 15)
    try:
        page_size = int(page_size)
    except ValueError:
        page_size = 15

    if page_size > 0:
        paginator = Paginator(runs, page_size)
        page = request.GET.get('page', 1)
        try:
            runs_page = paginator.page(page)
        except PageNotAnInteger:
            runs_page = paginator.page(1)
        except EmptyPage:
            runs_page = paginator.page(paginator.num_pages)
    else:
        runs_page = runs

    products = Product.objects.all()
    plants = Warehouse.objects.filter(location_type__in=Warehouse.MANUFACTURING_TYPES)
    if not plants.exists():
        plants = Warehouse.objects.all()

    # Material availability analysis for recipes — with max_producible
    recipe_readiness = []
    from django.db.models import F
    for p in products:
        recipes = p.recipe_items.select_related('material')
        items = []
        is_ready = True
        max_producible = None
        for r in recipes:
            avail = float(Batch.objects.filter(material=r.material, status='Active').annotate(avail=F('quantity')-F('allocated_quantity')).aggregate(s=Sum('avail'))['s'] or 0)
            needed = float(r.quantity_required)
            sufficient = avail >= needed
            if not sufficient:
                is_ready = False
            if needed > 0:
                can_make = int(avail / needed)
                max_producible = min(max_producible, can_make) if max_producible is not None else can_make
            items.append({
                'material': r.material,
                'required': needed,
                'available': avail,
                'sufficient': sufficient
            })
        recipe_readiness.append({
            'product': p,
            'items': items,
            'is_ready': is_ready,
            'max_producible': max_producible if max_producible is not None else '∞',
        })

    # SO Production Queue — SOs that need/are in production
    so_queue_statuses = ['Pending', 'Awaiting Acknowledgement', 'In Production']
    so_queue_raw = SalesOrder.objects.filter(status__in=so_queue_statuses).prefetch_related(
        'items__product__recipe_items__material', 'production_runs'
    ).order_by('fulfillment_deadline', '-order_date')

    # Build SO queue with per-item BOM readiness
    so_queue = []
    for so in so_queue_raw:
        so_items = []
        for item in so.items.all():
            # A run only handles this item once finished; cancelled runs and runs still awaiting materials or approval do not count.
            existing_run = so.production_runs.filter(target_product=item.product).exclude(status='Cancelled').order_by('-id').first()
            run_resolved = existing_run is not None and existing_run.status == 'Completed'
            run_pending = existing_run is not None and not run_resolved

            # Quantity still uncovered after stock allocated to the SO and yield expected from runs in flight.
            from .utils import so_line_commitment
            allocated = float(so_line_commitment(so, item.product, item.quantity_shipped)['total'])
            incoming_production = float(ProductionRun.objects.filter(
                sales_order=so, target_product=item.product,
                status__in=['Pending Approval', 'Pending Allocation', 'Awaiting Materials', 'Planned', 'InProgress', 'Paused']
            ).aggregate(s=Sum('expected_yield'))['s'] or 0)
            deficit = max(0.0, float(item.quantity_ordered) - allocated - incoming_production)

            # Nothing left to do: covered by allocation and/or an in-flight run.
            if deficit <= 0 and not run_pending:
                continue

            recipes = item.product.recipe_items.select_related('material').all()
            bom_rows = []
            can_make = None
            for r in recipes:
                avail = float(Batch.objects.filter(material=r.material, status='Active').annotate(avail=F('quantity')-F('allocated_quantity')).aggregate(s=Sum('avail'))['s'] or 0)
                needed_per_unit = float(r.quantity_required)
                needed_total = needed_per_unit * deficit
                sufficient = avail >= needed_total
                if needed_per_unit > 0:
                    from_this = int(avail / needed_per_unit)
                    can_make = min(can_make, from_this) if can_make is not None else from_this
                bom_rows.append({
                    'material': r.material,
                    'required_total': needed_total,
                    'available': avail,
                    'sufficient': sufficient,
                })
            so_items.append({
                'item': item,
                'bom_rows': bom_rows,
                'can_make': can_make if can_make is not None else '∞',
                'bom_ready': all(r['sufficient'] for r in bom_rows),
                'existing_run': existing_run,
                'run_resolved': run_resolved,
                'allocated': allocated,
                'deficit': deficit,
            })
        if so_items:
            so_queue.append({
                'so': so,
                'items': so_items,
                'all_ready': all(i['bom_ready'] for i in so_items),
            })

    context = {
        'runs': runs_page,
        'products': products,
        'plants': plants,
        'loc_filter': loc_filter,
        'sort_by': sort_by,
        'page_size': page_size,
        'recipe_readiness': recipe_readiness,
        'so_queue': so_queue,
        'so_status_choices': SalesOrder.STATUS_CHOICES,
        'next_run_number': generate_next_code(ProductionRun, 'run_number', 'RUN', 801, pad=3),
    }
    return render(request, 'manufacturing.html', context)


# Logistics tracker and shipments
@login_required
def shipments_view(request):
    denied = action_denied(request, 'shipments')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_shipment':
            tracking_mode = request.POST.get('tracking_mode', 'auto')
            tracking_number = request.POST.get('tracking_number')
            direction = request.POST.get('direction', 'Inbound')
            status = request.POST.get('status', 'Preparing')
            
            origin_id = request.POST.get('origin_warehouse_id')
            dest_id = request.POST.get('destination_warehouse_id')
            mat_id = request.POST.get('material_id')
            prod_id = request.POST.get('product_id')
            qty = request.POST.get('quantity', 0)
            
            po_id = request.POST.get('purchase_order_id')
            so_id = request.POST.get('sales_order_id')
            batch_id = request.POST.get('batch_id')
            
            dispatch_dt = request.POST.get('dispatch_date')
            eta = request.POST.get('expected_eta_date')
            actual_arrival_dt = request.POST.get('actual_arrival_date')
            external_origin = request.POST.get('external_origin')

            # Facility limit: at least one end of the shipment must be a facility the user may use
            ends = [i for i in (origin_id, dest_id) if i]
            if ends and not any(may_use_warehouse(request.user, i) for i in ends):
                messages.error(request, FACILITY_REFUSED)
                return redirect('shipments')

            try:
                if tracking_mode == 'auto' or not tracking_number:
                    import random, string
                    today_str = date.today().strftime('%Y%m%d')
                    while True:
                        random_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
                        generated = f"SHIP-{today_str}-{random_suffix}"
                        if not Shipment.objects.filter(tracking_number=generated).exists():
                            tracking_number = generated
                            break

                origin_wh = Warehouse.objects.filter(id=origin_id).first() if origin_id else None
                dest_wh = Warehouse.objects.filter(id=dest_id).first() if dest_id else None
                po = PurchaseOrder.objects.filter(id=po_id).first() if po_id else None
                if po and po.status == 'Cancelled':
                    messages.error(request, f"{po.po_number} was cancelled, so no delivery can be raised for it.")
                    return redirect('shipments')
                so = SalesOrder.objects.filter(id=so_id).first() if so_id else None

                # Delivery contact and address come from the linked Client (contact person kept separate from the company name).
                client_contact_name_val = None
                client_contact_phone_val = None
                client_address_val = None
                if so and so.client:
                    client_contact_name_val = so.client.contact_person or None
                    client_contact_phone_val = so.client.phone or so.client.email or None
                    client_address_val = so.client.delivery_address or None

                # Outbound/Transfer shipments start as Draft and go through approval; only Inbound may be registered already underway.
                if direction in ('Outbound', 'Transfer'):
                    status = 'Draft'

                shipment = Shipment.objects.create(
                    tracking_number=tracking_number,
                    direction=direction,
                    status=status,
                    origin_warehouse=origin_wh,
                    destination_warehouse=dest_wh,
                    purchase_order=po,
                    sales_order=so,
                    dispatch_date=dispatch_dt if dispatch_dt else date.today(),
                    expected_eta_date=eta if eta else None,
                    actual_arrival_date=actual_arrival_dt if (status == 'Arrived' and actual_arrival_dt) else None,
                    external_origin=external_origin if external_origin else None,
                    client_contact_name=client_contact_name_val,
                    client_contact_phone=client_contact_phone_val,
                    client_address=client_address_val,
                )
                
                # Pre-fill cargo from the order's outstanding quantities; batches are picked later on the shipment page.
                items_added = 0
                if po:
                    for detail in po.items.all():
                        remaining = float(detail.quantity_ordered) - float(detail.quantity_received)
                        if remaining > 0:
                            ShipmentItem.objects.create(shipment=shipment, material=detail.material, quantity=remaining)
                            items_added += 1
                elif so:
                    for detail in so.items.all():
                        remaining = float(detail.quantity_ordered) - float(detail.quantity_shipped)
                        if remaining > 0:
                            ShipmentItem.objects.create(shipment=shipment, product=detail.product, quantity=remaining)
                            items_added += 1

                if items_added:
                    OrderTimeline.objects.create(shipment=shipment, action=f"Pre-filled {items_added} cargo item(s) from the linked order.", user=request.user)
                    messages.success(request, f"Shipment '{tracking_number}' registered with {items_added} item(s) pulled from the order. Review quantities and batches before dispatch.")
                else:
                    messages.success(request, f"Shipment '{tracking_number}' registered. Now you can add items.")
                return redirect('shipment_detail', pk=shipment.pk)
            except Exception as e:
                messages.error(request, f"Error registering shipment: {e}")

        return redirect('shipments')

    status_filter = request.GET.get('status')
    direction_filter = request.GET.get('direction')
    search_query = request.GET.get('q', '').strip()

    shipments = Shipment.objects.select_related(
        'origin_warehouse', 'destination_warehouse', 'purchase_order', 'sales_order'
    ).prefetch_related('items').order_by('-id')
    # Facility limit: only shipments starting or ending at one of the user's facilities
    limited = limited_warehouse_ids(request.user)
    if limited is not None:
        shipments = shipments.filter(Q(origin_warehouse_id__in=limited) | Q(destination_warehouse_id__in=limited))

    if status_filter:
        shipments = shipments.filter(status=status_filter)
    if direction_filter:
        shipments = shipments.filter(direction=direction_filter)
    if search_query:
        shipments = shipments.filter(
            Q(tracking_number__icontains=search_query)
            | Q(origin_warehouse__name__icontains=search_query)
            | Q(destination_warehouse__name__icontains=search_query)
        )

    shipments, sort_ctx = apply_list_sort(request, shipments, {
        'tracking_number': 'tracking_number',
        'direction': 'direction',
        'status': 'status',
        'expected_eta_date': 'expected_eta_date',
        'id': 'id',
    }, default='-id')
    page_size = get_page_size(request)
    shipments = Paginator(shipments, page_size).get_page(request.GET.get('page'))

    warehouses = Warehouse.objects.all().order_by('name')
    materials = Material.objects.all().order_by('name')
    products = Product.objects.all().order_by('name')
    purchase_orders = PurchaseOrder.objects.exclude(status__in=['Completed', 'Cancelled']).order_by('-po_number')
    sales_orders = SalesOrder.objects.exclude(status='Delivered').order_by('-so_number')
    batches = Batch.objects.filter(status='Active').order_by('batch_number')

    stats = {
        'total': Shipment.objects.count(),
        'dispatched': Shipment.objects.filter(status='Dispatched').count(),
        'preparing': Shipment.objects.filter(status='Preparing').count(),
        'delayed': Shipment.objects.filter(status='Delayed').count(),
        'arrived': Shipment.objects.filter(status='Arrived').count(),
    }

    # Statuses selectable when creating a shipment by hand (Draft and approval stages are set by other workflows).
    initial_status_choices = [
        ('Preparing', 'Approved / Preparing'),
        ('Dispatched', 'Dispatched'),
        ('Arrived', 'Arrived'),
    ]

    context = {
        'shipments': shipments,
        'warehouses': warehouses,
        'materials': materials,
        'products': products,
        'purchase_orders': purchase_orders,
        'sales_orders': sales_orders,
        'batches': batches,
        'stats': stats,
        'status_choices': Shipment.STATUS_CHOICES,
        'initial_status_choices': initial_status_choices,
        'direction_choices': Shipment.DIRECTION_CHOICES,
        'next_tracking_number': generate_next_code(Shipment, 'tracking_number', 'TRK', 101, pad=4),
        'search_query': search_query,
        'page_size': page_size,
        **sort_ctx,
    }
    return render(request, 'shipments.html', context)



# QA actions live in core/qa.py; the QA page is core/views_qa.py.


# Approvals inbox (Action Center)
# Timeline phrases that record an approval decision, for the history list and weekly counts.
APPROVED_ACTION_Q = (Q(action__icontains='Approved by Manager')
                     | Q(action__icontains='approved. Ready for material allocation'))
REJECTED_ACTION_Q = Q(action__icontains='Approval Rejected')
# How many recent decision rows the history list looks through.
HISTORY_SCAN_LIMIT = 500


def _with_comment(text, comment):
    return f"{text} Comment: {comment}" if comment else text


def notify_approval_requester(decider, reference, link, approved, comment='', **timeline_fk):
    """Notifies whoever requested the approval of the outcome (not sent if they decided it themselves)."""
    request_entry = OrderTimeline.objects.filter(**timeline_fk).filter(
        Q(action__startswith='Approval Requested') | Q(action__startswith='Submitted to')
    ).order_by('-timestamp').first()
    requester = request_entry.user if request_entry else None
    if requester is None or requester == decider:
        return
    verdict = 'Approved' if approved else 'Rejected'
    Notification.objects.create(
        user=requester,
        message=_with_comment(f"{verdict}: {reference} was {verdict.lower()} by {decider.get_full_name() or decider.username}.", comment),
        link=link,
    )


@login_required
def approvals_inbox_view(request):
    if not can_approve(request.user):
        messages.error(request, "Permission Denied: You do not have permission to access this area.")
        return redirect('dashboard')
    if request.method == 'POST':
        action = request.POST.get('action')
        item_type = request.POST.get('item_type')
        item_id = request.POST.get('item_id')
        comment = request.POST.get('comment', '').strip()

        # Lets a detail page reuse this handler and return the user to itself.
        back = request.POST.get('next', '')
        if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
            back = reverse('approvals_inbox')
        refused = "Only the assigned approver can decide this, and only while it's pending approval."

        if action not in ('approve', 'reject') or item_type not in ('sales_order', 'production_run', 'purchase_order', 'shipment'):
            messages.error(request, "Unrecognised approval request.")
            return redirect(back)

        if item_type == 'sales_order':
            so = get_object_or_404(SalesOrder, id=item_id)
            if not may_decide_approval(request.user, so):
                messages.error(request, refused)
                return redirect(back)
            clear_approval_notifications(so.so_number)
            if action == 'approve':
                so.status = 'Pending'
                so.approved_by = request.user
                so.updated_by = request.user
                so.save()
                OrderTimeline.objects.create(sales_order=so, action=_with_comment("Approved by Manager.", comment), user=request.user)
                messages.success(request, f"Sales Order {so.so_number} approved.")
            else:
                so.status = 'Draft'
                so.assigned_to = None
                so.approved_by = None
                so.approval_remarks = ''
                so.updated_by = request.user
                so.save()
                OrderTimeline.objects.create(sales_order=so, action=_with_comment("Approval Rejected by Manager.", comment), user=request.user)
                messages.warning(request, f"Sales Order {so.so_number} returned to Draft.")
            notify_approval_requester(request.user, f"Sales Order {so.so_number}", reverse('so_detail', args=[so.pk]),
                                      action == 'approve', comment, sales_order=so)

        elif item_type == 'production_run':
            run = get_object_or_404(ProductionRun, id=item_id)
            if not may_decide_run_approval(request.user, run):
                messages.error(request, "Only someone who may approve production runs (the named approver, if there is one) can decide a run that is pending approval.")
                return redirect(back)
            clear_approval_notifications(run.run_number)
            run_link = reverse('production_run_detail', args=[run.pk])
            if run.actual_yield is not None:
                # Variance sign-off on a finished run: the supervisor and followers hear back.
                to_notify = ([run.supervisor] if run.supervisor else []) + list(run.followers.all())
                if action == 'approve':
                    from .utils import finalize_production_run
                    run.supervisor_signoff = request.user
                    fg_batch = finalize_production_run(run, request.user)
                    OrderTimeline.objects.create(
                        production_run=run,
                        action=_with_comment("Variance Approved by Manager." + (f" FG Batch {fg_batch.batch_number} created." if fg_batch else ""), comment),
                        user=request.user
                    )
                    note = f"Approved: Variance approved for {run.run_number}."
                    messages.success(request, f"Production Run {run.run_number} variance approved and completed.")
                else:
                    run.status = 'InProgress'
                    run.save()
                    OrderTimeline.objects.create(production_run=run, action=_with_comment("Variance Approval Rejected.", comment), user=request.user)
                    note = f"Rejected: Variance rejected for {run.run_number}. Rework required."
                    messages.warning(request, f"Production Run {run.run_number} variance rejected. Returned to In Progress.")
                for user in dict.fromkeys(to_notify):
                    Notification.objects.create(user=user, message=_with_comment(note, comment), link=run_link)
            else:
                # Pre-production approval: clears the run for FEFO allocation, which handles local and cross-warehouse sourcing.
                if action == 'approve':
                    from .utils import approve_production_run
                    approve_production_run(run, request.user)
                    if comment:
                        OrderTimeline.objects.create(production_run=run, action=f"Approval comment: {comment}", user=request.user)
                    note = f"Approved: Production Run {run.run_number} is cleared for material allocation."
                    messages.success(request, f"Production Run {run.run_number} approved. Please allocate materials to begin.")
                else:
                    from .utils import release_production_run_allocations
                    release_production_run_allocations(run, request.user)
                    run.status = 'Cancelled'
                    run.save()
                    OrderTimeline.objects.create(production_run=run, action=_with_comment("Production Approval Rejected - run cancelled.", comment), user=request.user)
                    note = f"Rejected: Production Run {run.run_number} was not approved and has been cancelled."
                    messages.warning(request, f"Production Run {run.run_number} rejected and cancelled.")
                if run.created_by and run.created_by != request.user:
                    Notification.objects.create(user=run.created_by, message=_with_comment(note, comment), link=run_link)

        elif item_type == 'purchase_order':
            po = get_object_or_404(PurchaseOrder, id=item_id)
            if not may_decide_approval(request.user, po):
                messages.error(request, refused)
                return redirect(back)
            clear_approval_notifications(po.po_number)
            if action == 'approve':
                po.status = 'Pending'
                po.approved_by = request.user
                po.save()
                OrderTimeline.objects.create(purchase_order=po, action=_with_comment("Approved by Manager.", comment), user=request.user)
                messages.success(request, f"Purchase Order {po.po_number} approved.")
            else:
                po.status = 'Draft'
                po.assigned_to = None
                po.approval_remarks = ''
                po.save()
                OrderTimeline.objects.create(purchase_order=po, action=_with_comment("Approval Rejected by Manager.", comment), user=request.user)
                messages.warning(request, f"Purchase Order {po.po_number} returned to Draft.")
            notify_approval_requester(request.user, f"Purchase Order {po.po_number}", reverse('po_detail', args=[po.pk]),
                                      action == 'approve', comment, purchase_order=po)

        else:  # shipment
            ship = get_object_or_404(Shipment, id=item_id)
            if not may_decide_approval(request.user, ship):
                messages.error(request, refused)
                return redirect(back)
            clear_approval_notifications(ship.tracking_number)
            if action == 'approve':
                ship.status = 'Preparing'
                ship.save()
                OrderTimeline.objects.create(shipment=ship, action=_with_comment("Approved by Manager.", comment), user=request.user)
                messages.success(request, f"Shipment {ship.tracking_number} approved.")
            else:
                ship.status = 'Logistics Review'
                ship.assigned_to = None
                ship.save()
                OrderTimeline.objects.create(shipment=ship, action=_with_comment("Approval Rejected by Manager.", comment), user=request.user)
                messages.warning(request, f"Shipment {ship.tracking_number} returned to Logistics Review.")
            notify_approval_requester(request.user, f"Shipment {ship.tracking_number}", reverse('shipment_detail', args=[ship.pk]),
                                      action == 'approve', comment, shipment=ship)

        return redirect(back)

    # Same source as the sidebar badge (core/context_processors.py), so they agree.
    pending = {key: list(qs) for key, qs in pending_actions(request.user).items()}
    pending_count = sum(len(items) for items in pending.values())

    notifications = Notification.objects.filter(user=request.user, is_read=False).order_by('-created_at')

    # History: one row per item, showing this user's latest decision on it.
    decisions = (
        OrderTimeline.objects.filter(user=request.user).filter(APPROVED_ACTION_Q | REJECTED_ACTION_Q)
        .select_related('sales_order', 'purchase_order', 'shipment', 'production_run')
        .order_by('-timestamp')[:HISTORY_SCAN_LIMIT]
    )
    seen = set()
    unified_history = []
    for t in decisions:
        for kind, obj in (('so', t.sales_order), ('po', t.purchase_order),
                          ('shipment', t.shipment), ('production_run', t.production_run)):
            if obj is not None:
                break
        else:
            continue
        if (kind, obj.pk) in seen:
            continue
        seen.add((kind, obj.pk))
        unified_history.append({
            'type': kind, 'obj': obj, 'timestamp': t.timestamp,
            'decision': 'Rejected' if 'rejected' in t.action.lower() else 'Approved',
            'status': obj.get_status_display(),
        })

    history_page = Paginator(unified_history, 10).get_page(request.GET.get('page'))

    today = timezone.localdate()
    this_week = OrderTimeline.objects.filter(user=request.user, timestamp__date__gte=today - timedelta(days=today.weekday()))

    active_tab = request.GET.get('tab')
    if active_tab not in ('tasks', 'following'):
        active_tab = 'tasks'

    context = {
        **pending,
        'notifications': notifications,
        'history_page': history_page,
        'followed_sos': request.user.followed_sos.all().order_by('-order_date'),
        'followed_pos': request.user.followed_pos.select_related('supplier', 'assigned_to').order_by('-order_date'),
        'followed_runs': request.user.following_runs.select_related('target_product').order_by('-id'),
        'followed_shipments': request.user.followed_shipments.all().order_by('-id'),
        'pending_count': pending_count,
        'approved_this_week': this_week.filter(APPROVED_ACTION_Q).count(),
        'rejected_this_week': this_week.filter(REJECTED_ACTION_Q).count(),
        'active_tab': active_tab,
    }
    return render(request, 'approvals_inbox.html', context)


RECEIVABLE_STATUSES = ('Arrived', 'Discrepant')


def record_item_receipt(shipment, item, qty, received_date, user, notes=None):
    """One dated receiving round for a shipment line (same for every direction).
    Inbound PO lines also credit the PO and create the stock batch right away."""
    from .utils import apply_po_material_receipt
    qty = Decimal(str(qty))
    ShipmentItemReceipt.objects.create(
        shipment_item=item, quantity=qty, received_date=received_date,
        received_by=user, notes=notes or None
    )
    item.received_quantity = (item.received_quantity or Decimal('0')) + qty
    item.date_confirmed = timezone.now()
    item.save(update_fields=['received_quantity', 'date_confirmed'])

    if shipment.direction == 'Inbound' and shipment.purchase_order and item.material:
        detail = PurchaseOrderDetail.objects.filter(purchase_order=shipment.purchase_order, material=item.material).first()
        if detail:
            batch = apply_po_material_receipt(detail, qty, user, into_batch=item.batch)
            into = f" into Batch {batch.batch_number}" if batch else ""
            if batch and item.batch_id != batch.id:
                item.batch = batch
                item.save(update_fields=['batch'])
            OrderTimeline.objects.create(purchase_order=shipment.purchase_order, user=user,
                                         action=f"Received {qty} of {item.material.name} via {shipment.tracking_number}{into}")

    item_name = item.material.name if item.material else (item.product.name if item.product else 'item')
    batch_note = f" into Batch {item.batch.batch_number}" if shipment.direction == 'Inbound' and item.batch else ""
    OrderTimeline.objects.create(shipment=shipment, action=f"Logged receipt of {qty} for {item_name}{batch_note} on {received_date}.", user=user)
    return item_name


def finish_shipment_receiving(request, shipment):
    """Close out receiving for a shipment: Discrepant if any line mismatches, otherwise Completed with its stock effects. Returns the new status."""
    # Fresh query: the view's prefetched items may predate receipts logged in this request.
    items = list(ShipmentItem.objects.filter(shipment=shipment).select_related('product'))
    if any(item.received_quantity != item.quantity for item in items):
        shipment.status = 'Discrepant'
        shipment.save()
        OrderTimeline.objects.create(shipment=shipment, action="Receiving finalized with a mismatch. Marked Discrepant.", user=request.user)
        messages.warning(request, "Receiving finalized, but quantities don't fully match what was expected. You may log more receipts, or request a Manager Force Close.")
        return shipment.status

    shipment.status = 'Completed'
    shipment.acknowledged_by = request.user
    shipment.last_edited_by = request.user

    if shipment.direction == 'Inbound' and shipment.purchase_order and shipment.purchase_order.linked_production_run:
        run = shipment.purchase_order.linked_production_run
        missing_materials = []
        for req in run.target_product.recipe_items.all():
            needed = float(req.quantity_required) * float(run.expected_yield)
            available = Batch.objects.filter(
                material=req.material,
                warehouse=run.manufacturing_plant,
                status='Active'
            ).aggregate(total=Sum(F('quantity') - F('allocated_quantity')))['total'] or 0
            if float(available) < needed:
                missing_materials.append(req.material.name)

        if not missing_materials:
            msg = f"Materials have arrived for Production Run {run.run_number}. You have sufficient materials to allocate and start the run."
        else:
            msg = f"Partial materials have arrived for Production Run {run.run_number}, but you are still short on: {', '.join(missing_materials)}."
        Notification.objects.create(user=run.supervisor, message=msg, link=f"/operations/manufacture/run/{run.id}/allocate/")

    shipment.save()
    OrderTimeline.objects.create(shipment=shipment, action="Receiving finalized. Shipment completed.", user=request.user)

    if shipment.direction in ['Outbound', 'Transfer']:
        from .utils import deduct_stock_from_allocation, mark_so_delivered_if_fully_shipped, apply_so_product_shipment
        deduct_stock_from_allocation('shipment', shipment, user=request.user)

        if shipment.direction == 'Outbound' and shipment.sales_order:
            if not shipment.credited_to_so:
                for item in items:
                    if item.product:
                        so_detail = SalesOrderDetail.objects.filter(sales_order=shipment.sales_order, product=item.product).first()
                        if so_detail:
                            apply_so_product_shipment(so_detail, item.received_quantity)
                shipment.credited_to_so = True
                shipment.save(update_fields=['credited_to_so'])
            mark_so_delivered_if_fully_shipped(shipment.sales_order, completing_shipment=shipment)

    if shipment.direction == 'Transfer':
        from .utils import receive_transfer_into_destination
        receive_transfer_into_destination(shipment, request.user)

    messages.success(request, "Receiving finalized. Shipment marked Completed.")
    return shipment.status


def shipment_dispatch_problem(shipment):
    """Why an Outbound/Transfer shipment can't be dispatched yet (every item needs a batch at the origin), or None."""
    items = list(shipment.items.select_related('batch', 'batch__warehouse', 'material', 'product'))
    if not items:
        return "Add at least one item before dispatching."
    for item in items:
        name = item.material.name if item.material else (item.product.name if item.product else 'item')
        if not item.batch:
            return f"{name} has no batch. Remove it and add it again with a batch before dispatching."
        if shipment.origin_warehouse_id and item.batch.warehouse_id != shipment.origin_warehouse_id:
            return (f"Batch {item.batch.batch_number} is at "
                    f"{item.batch.warehouse.name if item.batch.warehouse else 'no warehouse'}, "
                    f"not this shipment's origin ({shipment.origin_warehouse.name}).")
        if item.batch.status == 'Quarantined':
            return f"Batch {item.batch.batch_number} is in QA quarantine and can't be shipped until QA releases it."
        if item.batch.expiry_date and item.batch.expiry_date < timezone.localdate():
            return f"Batch {item.batch.batch_number} expired on {item.batch.expiry_date}. Replace it, or have QA extend its expiry first."
    return None


@login_required
def shipment_detail_view(request, pk):
    denied = action_denied(request, 'shipment_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    shipment = get_object_or_404(Shipment.objects.prefetch_related('items__material', 'items__product', 'items__batch'), pk=pk)
    
    route_error = False
    if request.method == 'POST':
        # Facility limit: at least one end of the shipment must be a facility the user may use
        ends = [i for i in (shipment.origin_warehouse_id, shipment.destination_warehouse_id) if i]
        if ends and not any(may_use_warehouse(request.user, i) for i in ends):
            messages.error(request, FACILITY_REFUSED)
            return redirect('shipment_detail', pk=shipment.pk)
        old_status = shipment.status
        action = request.POST.get('action')

        allowed = SHIPMENT_ACTION_STATUSES.get(action)
        if allowed and shipment.direction == 'Inbound' and action == 'scrap_shipment':
            allowed += INBOUND_EXTRA_SCRAPPABLE
        if allowed and shipment.status not in allowed:
            messages.error(request, f"{shipment.tracking_number} is {shipment.status}, so that can't be done now.")
            return redirect('shipment_detail', pk=shipment.pk)

        cargo_before = None
        if action in ('add_item', 'remove_item') and shipment.direction == 'Transfer' and shipment.sales_order:
            messages.error(request, f"This stock move's cargo comes from {shipment.sales_order.so_number}. Change the order, or switch its delivery plan, instead.")
            return redirect('shipment_detail', pk=shipment.pk)
        if action in ('add_item', 'remove_item') and shipment_cargo_locked(shipment):
            messages.error(request, f"Cargo can't be changed once a shipment is {shipment.status}.")
            return redirect('shipment_detail', pk=shipment.pk)
        if action in ('add_item', 'remove_item') and shipment.direction != 'Inbound':
            cargo_before = sorted(shipment.items.values_list('id', 'batch_id', 'quantity'))
        
        if action == 'add_item':
            mat_id = request.POST.get('material_id')
            prod_id = request.POST.get('product_id')
            batch_id = request.POST.get('batch_id')
            qty = request.POST.get('quantity')
            
            try:
                mat = Material.objects.filter(id=mat_id).first() if mat_id else None
                prod = Product.objects.filter(id=prod_id).first() if prod_id else None
                batch = Batch.objects.filter(id=batch_id).first() if batch_id else None
                qty_val = float(qty) if qty else 0.0
                
                # Outbound/Transfer items need a batch at the shipment's origin.
                if shipment.direction in ['Outbound', 'Transfer'] and not batch:
                    messages.error(request, "A Batch MUST be selected for outbound shipments and internal transfers.")
                elif (shipment.direction in ['Outbound', 'Transfer'] and shipment.origin_warehouse_id
                      and batch.warehouse_id != shipment.origin_warehouse_id):
                    messages.error(request, f"Batch {batch.batch_number} is at {batch.warehouse.name if batch.warehouse else 'no warehouse'}, not this shipment's origin ({shipment.origin_warehouse.name}). Move it with an internal transfer first.")
                elif shipment.direction in ['Outbound', 'Transfer'] and batch.status == 'Quarantined':
                    messages.error(request, f"Batch {batch.batch_number} is in QA quarantine and can't be shipped or moved until QA releases it.")
                elif shipment.direction in ['Outbound', 'Transfer'] and batch:
                    qty_val = float(qty) if qty else 0.0
                    if qty_val > float(batch.available_quantity):
                        messages.error(request, f"Cannot add {qty_val}. Only {batch.available_quantity:.2f} available to allocate from batch {batch.batch_number}.")
                    else:
                        ShipmentItem.objects.create(
                            shipment=shipment,
                            material=mat,
                            product=prod,
                            batch=batch,
                            quantity=qty_val
                        )
                        OrderTimeline.objects.create(shipment=shipment, action=f"Added item {mat.sku if mat else prod.sku} (Qty: {qty_val}).", user=request.user)
                        messages.success(request, "Item added to shipment.")
                        # Allocation Logic (Lock Stock)
                        qty_dec = Decimal(str(qty_val))
                        if shipment.sales_order:
                            so_alloc = StockAllocation.objects.filter(sales_order=shipment.sales_order, batch=batch).first()
                            if so_alloc:
                                deduct = min(qty_dec, so_alloc.quantity)
                                so_alloc.quantity -= deduct
                                if so_alloc.quantity <= 0: so_alloc.delete()
                                else: so_alloc.save(update_fields=['quantity'])
                                StockAllocation.objects.create(batch=batch, shipment=shipment, quantity=deduct)
                                qty_dec -= deduct
                        
                        if qty_dec > 0:
                            batch.allocated_quantity += qty_dec
                            batch.save(update_fields=['allocated_quantity'])
                            StockAllocation.objects.create(batch=batch, shipment=shipment, quantity=qty_dec)
                        OrderTimeline.objects.create(shipment=shipment, action=f"Added item {mat.sku if mat else prod.sku} (Qty: {qty_val}) and locked stock.", user=request.user)
                        messages.success(request, "Item added and stock locked.")
                elif shipment.purchase_order and not (mat and shipment.purchase_order.items.filter(material=mat).exists()):
                    # Receipts are credited to the PO line by material; anything else would arrive without becoming stock
                    messages.error(request, f"Only materials on {shipment.purchase_order.po_number} can be added to this delivery. Add the line to the PO first.")
                else:
                    ShipmentItem.objects.create(
                        shipment=shipment,
                        material=mat,
                        product=prod,
                        batch=batch,
                        quantity=qty_val
                    )
                    messages.success(request, "Item added to shipment.")
            except Exception as e:
                messages.error(request, f"Error adding item: {e}")
                
        elif action == 'remove_item':
            item_id = request.POST.get('item_id')
            try:
                item = ShipmentItem.objects.filter(id=item_id, shipment=shipment).first()
                if item:
                    qty = item.quantity
                    batch = item.batch
                    if batch and shipment.direction in ['Outbound', 'Transfer']:
                        alloc = StockAllocation.objects.filter(shipment=shipment, batch=batch).first()
                        if alloc:
                            deduct = min(Decimal(str(qty)), alloc.quantity)
                            alloc.quantity -= deduct
                            if alloc.quantity <= 0: alloc.delete()
                            else: alloc.save(update_fields=['quantity'])
                            batch.allocated_quantity -= deduct
                            batch.save(update_fields=['allocated_quantity'])
                    item.delete()
                    OrderTimeline.objects.create(shipment=shipment, action=f"Removed item from shipment.", user=request.user)
                    messages.success(request, "Item removed and stock lock released.")
            except Exception as e:
                messages.error(request, f"Error removing item: {e}")

        elif action == 'update_route':
            eta = request.POST.get('eta_date')
            arrival = request.POST.get('arrival_date')
            origin_id = request.POST.get('origin_warehouse_id')
            dest_id = request.POST.get('destination_warehouse_id')
            
            # New Logistics Fields
            client_address = request.POST.get('client_address')
            client_contact_name = request.POST.get('client_contact_name')
            client_contact_phone = request.POST.get('client_contact_phone')
            external_tracking_id = request.POST.get('external_tracking_id')
            departure_datetime = request.POST.get('departure_datetime')

            if client_contact_phone is not None:
                try:
                    client_contact_phone = normalise_phone_or_email(client_contact_phone)
                except ValidationError as e:
                    messages.error(request, f"Shipment info not saved. Client contact: {validation_messages(e)}")
                    return redirect('shipment_detail', pk=shipment.pk)

            core_changed = False

            # Check string fields
            if client_address is not None and (shipment.client_address or "") != client_address.strip(): core_changed = True
            if client_contact_name is not None and (shipment.client_contact_name or "") != client_contact_name.strip(): core_changed = True
            if client_contact_phone is not None and (shipment.client_contact_phone or "") != client_contact_phone.strip(): core_changed = True
            if external_tracking_id is not None and (shipment.external_tracking_id or "") != external_tracking_id.strip(): core_changed = True

            # Check FK fields
            if origin_id is not None and str(shipment.origin_warehouse_id or "") != origin_id: core_changed = True
            if dest_id is not None and str(shipment.destination_warehouse_id or "") != dest_id: core_changed = True

            # Check departure_datetime
            if departure_datetime:
                current_dep = shipment.departure_datetime.strftime('%Y-%m-%dT%H:%M') if shipment.departure_datetime else ""
                if current_dep != departure_datetime: core_changed = True
                shipment.departure_datetime = departure_datetime

            if eta: shipment.expected_eta_date = eta
            if arrival: shipment.actual_arrival_date = arrival
            if origin_id: shipment.origin_warehouse_id = origin_id
            if dest_id: shipment.destination_warehouse_id = dest_id
            if client_address is not None: shipment.client_address = client_address
            if client_contact_name is not None: shipment.client_contact_name = client_contact_name
            if client_contact_phone is not None: shipment.client_contact_phone = client_contact_phone
            if external_tracking_id is not None: shipment.external_tracking_id = external_tracking_id
                
            # Inbound shipments are the supplier's record, so edits do not force re-approval.
            if core_changed and shipment.status in ['Preparing', 'Dispatched', 'Delayed'] and shipment.direction != 'Inbound':
                shipment.status = 'Logistics Review'
                messages.warning(request, "Core logistics details were modified. The shipment has been returned to Logistics Review and must be re-approved.")
                OrderTimeline.objects.create(shipment=shipment, action="Core logistics details modified. Status reverted to Logistics Review.", user=request.user)
            else:
                messages.success(request, "Shipment route info saved.")
                OrderTimeline.objects.create(shipment=shipment, action="Updated shipment logistics tracking details.", user=request.user)
                
            shipment.last_edited_by = request.user
            shipment.save()
            
        elif action == 'submit_to_logistics':
            if shipment.status != 'Draft':
                messages.error(request, f"Only a Draft shipment can be submitted to Logistics. This one is {shipment.status}.")
                return redirect('shipment_detail', pk=shipment.pk)
            shipment.status = 'Logistics Review'
            shipment.save()
            messages.success(request, "Shipment submitted to Logistics for review.")
            
        elif action == 'scrap_shipment' and shipment.direction == 'Transfer' and shipment.sales_order:
            # Stock move for an order: hand the reservations back to the order instead of freeing them.
            so = shipment.sales_order
            with transaction.atomic():
                StockAllocation.objects.filter(shipment=shipment).update(sales_order=so, shipment=None)
                shipment.status = 'Cancelled'
                shipment.save(update_fields=['status'])
                OrderTimeline.objects.create(shipment=shipment, action=f"Scrapped. {so.so_number} keeps its stock reservations.", user=request.user)
                OrderTimeline.objects.create(sales_order=so, action=f"Stock move {shipment.tracking_number} scrapped; the stock stays reserved for this order.", user=request.user)
            messages.success(request, f"Stock move scrapped. {so.so_number} keeps its reserved stock.")

        elif (action == 'scrap_shipment' and shipment.direction == 'Outbound' and shipment.sales_order
              and not shipment.linked_production_run and not shipment.credited_to_so):
            # A delivery that never left: the order keeps the stock, ready for another logistics order.
            so = shipment.sales_order
            with transaction.atomic():
                StockAllocation.objects.filter(shipment=shipment).update(sales_order=so, shipment=None)
                shipment.status = 'Cancelled'
                shipment.save(update_fields=['status'])
                clear_approval_notifications(shipment.tracking_number)
                OrderTimeline.objects.create(shipment=shipment, action=f"Scrapped before dispatch. {so.so_number} keeps its stock reservations.", user=request.user)
                OrderTimeline.objects.create(sales_order=so, action=f"Logistics order {shipment.tracking_number} scrapped before dispatch; its stock stays reserved for this order.", user=request.user)
                from .utils import _resync_so_fulfillment_status
                _resync_so_fulfillment_status(so, request.user)
            messages.success(request, f"Shipment scrapped. {so.so_number} keeps its reserved stock; draft a new logistics order when ready.")

        elif action == 'scrap_shipment':
            
            with transaction.atomic():
                for item in shipment.items.all():
                    qty = item.quantity
                    batch = item.batch
                    if batch:
                        qty_dec = Decimal(str(qty))
                        # Release only what the allocation ledger holds and shrink the batch counter by that same amount, not qty_dec.
                        remaining = qty_dec
                        if shipment.linked_production_run:
                            alloc = StockAllocation.objects.filter(production_run=shipment.linked_production_run, shipment=shipment, batch=batch).first()
                            if alloc and remaining > 0:
                                deduct = min(remaining, alloc.quantity)
                                alloc.quantity -= deduct
                                if alloc.quantity <= 0: alloc.delete()
                                else: alloc.save(update_fields=['quantity'])
                                remaining -= deduct

                        elif shipment.sales_order:
                            # If we moved allocations to the shipment
                            alloc = StockAllocation.objects.filter(shipment=shipment, batch=batch).first()
                            if alloc and remaining > 0:
                                deduct = min(remaining, alloc.quantity)
                                alloc.quantity -= deduct
                                if alloc.quantity <= 0: alloc.delete()
                                else: alloc.save(update_fields=['quantity'])
                                remaining -= deduct

                            # Auto-drafted SO shipments: allocations may still sit on the sales order; take only what is still needed.
                            so_alloc = StockAllocation.objects.filter(sales_order=shipment.sales_order, batch=batch).first()
                            if so_alloc and remaining > 0:
                                deduct = min(remaining, so_alloc.quantity)
                                so_alloc.quantity -= deduct
                                if so_alloc.quantity <= 0: so_alloc.delete()
                                else: so_alloc.save(update_fields=['quantity'])
                                remaining -= deduct

                        else:
                            # A plain stock move holds its lock on the shipment itself
                            alloc = StockAllocation.objects.filter(shipment=shipment, batch=batch).first()
                            if alloc and remaining > 0:
                                deduct = min(remaining, alloc.quantity)
                                alloc.quantity -= deduct
                                if alloc.quantity <= 0: alloc.delete()
                                else: alloc.save(update_fields=['quantity'])
                                remaining -= deduct

                        released = qty_dec - remaining
                        if released > 0:
                            batch.allocated_quantity -= released
                            if batch.allocated_quantity < 0: batch.allocated_quantity = 0
                            batch.save(update_fields=['allocated_quantity'])
                
                if shipment.sales_order:
                    so = shipment.sales_order
                    # An already shipped order keeps its status.
                    if so.status not in ('Partially Shipped', 'Shipped', 'Delivered'):
                        so.status = 'Pending'
                        so.save()
                    
                # A run-raised transfer never sent for approval is deleted; the Registry Ledger keeps the trace.
                if shipment.is_auto_generated and shipment.linked_production_run and shipment.status == 'Draft':
                    from .utils import delete_draft_transfer
                    run_pk = shipment.linked_production_run.pk
                    tracking = delete_draft_transfer(shipment, request.user, note="Scrapped before approval.")
                    messages.success(request, f"Draft transfer {tracking} scrapped and deleted. A trace is kept in the Registry Ledger.")
                    return redirect('production_run_detail', pk=run_pk)

                shipment.status = 'Cancelled'
                shipment.linked_production_run = None
                shipment.sales_order = None
                shipment.save()

            messages.success(request, "Shipment scrapped. Cargo locks released and linked orders updated.")

        elif action == 'skip_approval':
            if shipment.direction != 'Inbound':
                messages.error(request, "Only inbound (supplier) shipments can skip manager approval.")
            elif shipment.status not in ('Draft', 'Logistics Review', 'Pending Approval'):
                messages.error(request, f"This shipment is already {shipment.status}; there's no approval left to skip.")
            else:
                shipment.status = 'Preparing'
                shipment.save()
                OrderTimeline.objects.create(shipment=shipment, action="Inbound shipment confirmed without manager approval (outside our control).", user=request.user)
                messages.success(request, "Shipment confirmed and marked as Preparing.")

        elif action == 'submit_for_approval':
            approve_now = request.POST.get('approve_now') == '1' and request.user.has_perm(APPROVE_SHIPMENTS)
            approver_id = str(request.user.id) if approve_now else request.POST.get('approver_id')
            if shipment.direction == 'Transfer' and (not shipment.origin_warehouse or not shipment.destination_warehouse):
                messages.error(request, "Origin and Destination facilities MUST be selected before submitting for approval.")
                route_error = True
            elif shipment.direction == 'Outbound' and (not shipment.client_address or not shipment.client_contact_name or not shipment.client_contact_phone):
                messages.error(request, "Client Address, Contact Name, and Contact Phone MUST be completed before submitting an outbound shipment for approval.")
                route_error = True
            elif not approver_id:
                messages.error(request, "You must select a Manager for approval.")
            else:
                approver = CustomUser.objects.filter(id=approver_id).first()
                problem = approver_problem(approver, APPROVE_SHIPMENTS)
                if problem:
                    messages.error(request, problem)
                elif approver and approve_now:
                    shipment.status = 'Preparing'
                    shipment.assigned_to = approver
                    shipment.save()
                    OrderTimeline.objects.create(shipment=shipment, action=f"Submitted and approved by {approver.get_full_name() or approver.username}{self_approval_note(request.user, approver)}. Now Preparing.", user=request.user)
                    messages.success(request, "Shipment submitted and approved. It is now Preparing.")
                elif approver:
                    shipment.status = 'Pending Approval'
                    shipment.assigned_to = approver
                    shipment.save()
                    OrderTimeline.objects.create(shipment=shipment, action=f"Submitted to {approver.get_full_name() or approver.username} for approval{self_approval_note(request.user, approver)}.", user=request.user)
                    
                    link = reverse('approvals_inbox')
                    Notification.objects.create(
                        user=approver,
                        message=f"Shipment {shipment.tracking_number} requires your approval.",
                        link=link
                    )
                    
                    messages.success(request, f"Shipment submitted to {approver.get_full_name() or approver.username} for Approval.")
                else:
                    messages.error(request, "Invalid Manager selected.")
            
        elif action == 'return_to_manufacturing':
            shipment.status = 'Draft'
            shipment.assigned_to = None   # any approval it was waiting on no longer applies
            shipment.save()
            clear_approval_notifications(shipment.tracking_number)
            OrderTimeline.objects.create(shipment=shipment, action=f"Returned to Draft from {old_status}.", user=request.user)
            messages.warning(request, "Shipment returned to Manufacturing.")
            
        elif action == 'approve':
            if not may_decide_approval(request.user, shipment):
                messages.error(request, "Only the assigned approver can approve a shipment that is pending approval.")
            else:
                shipment.status = 'Preparing'
                shipment.save()
                clear_approval_notifications(shipment.tracking_number)
                OrderTimeline.objects.create(shipment=shipment, action="Approved by Manager. Now Preparing.", user=request.user)
                messages.success(request, "Shipment Approved and is now Preparing.")
                
        elif action == 'reject':
            if not may_decide_approval(request.user, shipment):
                messages.error(request, "Only the assigned approver can reject a shipment that is pending approval.")
            else:
                shipment.status = 'Logistics Review'
                shipment.save()
                clear_approval_notifications(shipment.tracking_number)
                OrderTimeline.objects.create(shipment=shipment, action="Approval rejected by Manager. Returned to Logistics Review.", user=request.user)
                messages.warning(request, "Shipment Rejected and returned to Logistics.")
                
        elif action == 'update_operational_status':
            new_st = request.POST.get('status')
            # Only in-progress shipments and targets; Completed/Discrepant come from receiving, approval stages from approve/reject.
            in_progress = ['Preparing', 'Dispatched', 'Delayed', 'Arrived']
            if shipment.status not in in_progress or new_st not in in_progress:
                messages.error(request, "Status can only be changed between Preparing, Dispatched, Delayed and Arrived. Use the receiving step to complete a shipment.")
                return redirect('shipment_detail', pk=shipment.pk)
            stage = {'Preparing': 0, 'Dispatched': 1, 'Delayed': 1, 'Arrived': 2}
            if stage[new_st] < stage[shipment.status]:
                messages.error(request, f"A shipment can't go back from {shipment.status} to {new_st}.")
                return redirect('shipment_detail', pk=shipment.pk)
            if (shipment.direction in ['Outbound', 'Transfer'] and shipment.status == 'Preparing'
                    and new_st in ['Delayed', 'Arrived']):
                # Dispatch is where cargo is checked and credited, so it can't be skipped
                messages.error(request, f"Mark this shipment as Dispatched before setting it to {new_st}.")
                return redirect('shipment_detail', pk=shipment.pk)
            if new_st == 'Dispatched' and shipment.direction in ['Outbound', 'Transfer']:
                problem = shipment_dispatch_problem(shipment)
                if problem:
                    messages.error(request, problem)
                    return redirect('shipment_detail', pk=shipment.pk)
            if new_st == 'Dispatched' and shipment.direction == 'Outbound':
                if not shipment.external_tracking_id or not shipment.departure_datetime:
                    messages.error(request, "Tracking ID and Departure Date/Time MUST be provided before marking this shipment as Dispatched.")
                    return redirect('shipment_detail', pk=shipment.pk)
            if new_st in in_progress:
                shipment.status = new_st
                shipment.save()
                if new_st != old_status:
                    OrderTimeline.objects.create(shipment=shipment, action=f"Status changed from {old_status} to {new_st}.", user=request.user)

                # First time crossing into Dispatched: credit the cargo to the SO (guarded by credited_to_so to avoid double-counting).
                if (new_st == 'Dispatched' and not shipment.credited_to_so
                        and shipment.direction == 'Outbound' and shipment.sales_order):
                    from .utils import apply_so_product_shipment
                    for si in shipment.items.all():
                        if si.product:
                            so_detail = SalesOrderDetail.objects.filter(sales_order=shipment.sales_order, product=si.product).first()
                            if so_detail:
                                apply_so_product_shipment(so_detail, si.quantity)
                    shipment.credited_to_so = True
                    shipment.save(update_fields=['credited_to_so'])

                messages.success(request, f"Operational status updated to {new_st}.")

        elif action == 'log_item_receipt':
            if shipment.status not in RECEIVABLE_STATUSES:
                messages.error(request, "Receipts can only be logged once the shipment has arrived.")
                return redirect('shipment_detail', pk=shipment.pk)
            try:
                item = get_object_or_404(ShipmentItem, id=request.POST.get('item_id'), shipment=shipment)
                qty = Decimal(request.POST.get('quantity'))
                if qty <= 0:
                    messages.error(request, "Enter a quantity greater than zero.")
                else:
                    received_date = request.POST.get('received_date') or date.today()
                    item_name = record_item_receipt(shipment, item, qty, received_date, request.user, request.POST.get('notes', '').strip())
                    item.refresh_from_db()
                    messages.success(request, f"Logged {qty} received for {item_name}. Running total: {item.received_quantity}.")
            except Exception as e:
                messages.error(request, f"Error logging receipt: {e}")

        elif action == 'finalize_shipment_receiving':
            if shipment.status not in RECEIVABLE_STATUSES:
                messages.error(request, "A shipment can only be finalized once it has arrived.")
            else:
                finish_shipment_receiving(request, shipment)

        elif action == 'receive_all':
            # Log everything not yet received on every line, dated today, then finalize.
            if shipment.status not in RECEIVABLE_STATUSES:
                messages.error(request, "A shipment can only be received once it has arrived.")
            else:
                with transaction.atomic():
                    for item in shipment.items.select_related('material', 'product'):
                        if item.shortage_quantity > 0:
                            record_item_receipt(shipment, item, item.shortage_quantity, date.today(), request.user, "Received all as expected")
                    finish_shipment_receiving(request, shipment)

        elif action == 'request_force_close':
            mgr_id = request.POST.get('assigned_manager_id')
            remarks = request.POST.get('discrepancy_remarks', '').strip()
            if not mgr_id or not remarks:
                messages.error(request, "You must provide remarks and assign a manager to request Force Close.")
                return redirect('shipment_detail', pk=shipment.pk)
                
            mgr = CustomUser.objects.filter(id=mgr_id).first()
            problem = approver_problem(mgr, APPROVE_SHIPMENTS)
            if problem:
                messages.error(request, problem)
                return redirect('shipment_detail', pk=shipment.pk)
            shipment.discrepancy_remarks = remarks
            shipment.assigned_manager = mgr
            shipment.save()
            Notification.objects.create(
                user=mgr,
                message=f"Force Close requested on Discrepant Shipment {shipment.tracking_number}.",
                link=reverse('shipment_detail', args=[shipment.pk])
            )
            messages.success(request, f"Force Close escalation sent to {mgr.get_full_name() or mgr.username}.")
            
        elif action == 'cancel_escalation':
            clear_approval_notifications(shipment.tracking_number)
            shipment.assigned_manager = None
            shipment.discrepancy_remarks = ''
            shipment.save()
            messages.success(request, "Escalation cancelled. You can now edit quantities and retry completion.")
            
        elif action == 'force_close_shipment':
            if shipment.status != 'Discrepant':
                messages.error(request, "Only a Discrepant shipment can be force closed.")
                return redirect('shipment_detail', pk=shipment.pk)
            if request.user != shipment.assigned_manager or not request.user.has_perm(APPROVE_SHIPMENTS):
                messages.error(request, "Only the assigned manager can approve the Force Close. Request it from a manager first.")
                return redirect('shipment_detail', pk=shipment.pk)

            # Transfers and deliveries: the manager says where missing units went: 'origin' (back on the origin batch) or 'lost' (written off).
            shortage_reasons = {}
            if shipment.direction in ('Transfer', 'Outbound'):
                for item in shipment.items.filter(batch__isnull=False):
                    if item.shortage_quantity > 0:
                        reason = request.POST.get(f'shortage_reason_{item.id}')
                        if reason not in ('origin', 'lost'):
                            messages.error(request, "Choose where the missing units went for every short item before force closing.")
                            return redirect('shipment_detail', pk=shipment.pk)
                        shortage_reasons[item.id] = reason

            mgr_comment = request.POST.get('manager_comment', '').strip()
            if mgr_comment:
                if shipment.discrepancy_remarks:
                    shipment.discrepancy_remarks += f"\n\nManager Acknowledgment: {mgr_comment}"
                else:
                    shipment.discrepancy_remarks = f"Manager Acknowledgment: {mgr_comment}"
            
            shipment.status = 'Completed'
            shipment.approved_by = request.user
            shipment.save()
            clear_approval_notifications(shipment.tracking_number)

            # Manually handle discrepancy deduction and lock release
            if shipment.direction in ['Outbound', 'Transfer']:
                allocs = StockAllocation.objects.filter(shipment=shipment)
                for alloc in allocs:
                    batch = alloc.batch
                    item = shipment.items.filter(batch=batch).first()
                    rcv_qty = item.received_quantity if item else 0
                    lost_qty = item.shortage_quantity if item and shortage_reasons.get(item.id) == 'lost' else Decimal('0')

                    batch.quantity -= Decimal(str(rcv_qty)) + lost_qty
                    batch.allocated_quantity -= alloc.quantity
                    from .utils import _close_batch_if_depleted
                    _close_batch_if_depleted(batch)
                    batch.save(update_fields=['quantity', 'allocated_quantity', 'status', 'closed_date'])

                    item_name = batch.material.name if batch.material else (batch.product.name if batch.product else batch.batch_number)
                    RegistryLog.objects.create(
                        action_type='Outbound',
                        item_name=f"{item_name} (Batch {batch.batch_number}) — force closed",
                        material=batch.material,
                        quantity_changed=Decimal(str(rcv_qty)),
                        warehouse=batch.warehouse,
                        user=request.user
                    )
                    if item and item.id in shortage_reasons:
                        short = item.shortage_quantity
                        if shortage_reasons[item.id] == 'lost':
                            RegistryLog.objects.create(
                                action_type='Lost_In_Transit',
                                item_name=f"{item_name} (Batch {batch.batch_number}) — lost in transit ({shipment.tracking_number})",
                                material=batch.material,
                                quantity_changed=short,
                                warehouse=batch.warehouse,
                                user=request.user
                            )
                            note = f"{short} of {item_name} written off as lost in transit."
                        else:
                            note = f"{short} of {item_name} kept at origin (Batch {batch.batch_number})."
                        OrderTimeline.objects.create(shipment=shipment, action=f"Force close shortage: {note}", user=request.user)

                    alloc.delete()

                if shipment.direction == 'Outbound' and shipment.sales_order:
                    from .utils import mark_so_delivered_if_fully_shipped, apply_so_product_shipment
                    for item in shipment.items.all():
                        if item.product:
                            so_detail = SalesOrderDetail.objects.filter(sales_order=shipment.sales_order, product=item.product).first()
                            if so_detail:
                                if not shipment.credited_to_so:
                                    apply_so_product_shipment(so_detail, item.received_quantity)
                                elif item.shortage_quantity > 0:
                                    # Credited in full at dispatch; the client only got what was received
                                    apply_so_product_shipment(so_detail, -item.shortage_quantity)
                    shipment.credited_to_so = True
                    shipment.save(update_fields=['credited_to_so'])
                    mark_so_delivered_if_fully_shipped(shipment.sales_order, completing_shipment=shipment)

            if shipment.direction == 'Transfer':
                from .utils import receive_transfer_into_destination
                receive_transfer_into_destination(shipment, request.user)

            if shipment.purchase_order:
                # Set PO status from its lines
                po = shipment.purchase_order
                lines = list(po.items.all())
                if lines and all(i.quantity_received >= i.quantity_ordered for i in lines):
                    po.status = 'Completed'
                elif any(i.quantity_received > 0 for i in lines) and po.status != 'Completed':
                    po.status = 'Partially Received'
                po.save()
                if po.status == 'Completed':
                    from .utils import release_po_held_batches
                    release_po_held_batches(po, request.user)
            
            messages.success(request, "Shipment Force Closed. Unreceived stock locks released.")
            
        elif action == 'reopen_shipment':
            if shipment.direction == 'Transfer':
                # Stock already moved into the destination batch cannot be undone here; use a stock audit adjustment.
                messages.error(request, "Completed transfers can't be reopened. Correct quantities with a stock audit adjustment instead.")
                return redirect('shipment_detail', pk=shipment.pk)
            shipment.status = 'Arrived'
            shipment.acknowledged_by = None
            shipment.last_edited_by = request.user
            shipment.save()
            RegistryLog.objects.create(
                action_type='Adjusted',
                item_name=f"Shipment {shipment.tracking_number} reopened",
                quantity_changed=0,
                warehouse=shipment.destination_warehouse or shipment.origin_warehouse,
                user=request.user
            )
            messages.success(request, "Shipment reopened for editing.")

        elif action == 'toggle_follow':
            if request.user in shipment.followers.all():
                shipment.followers.remove(request.user)
                messages.success(request, "You are no longer following this shipment.")
            else:
                shipment.followers.add(request.user)
                messages.success(request, "You are now following this shipment.")
                
        elif action == 'add_follower':
            user_id = request.POST.get('user_id')
            if user_id:
                user_obj = CustomUser.objects.filter(id=user_id).first()
                if user_obj and user_obj not in shipment.followers.all():
                    shipment.followers.add(user_obj)
                    notify_added_follower(request.user, user_obj, f"Shipment {shipment.tracking_number}", reverse('shipment_detail', args=[shipment.pk]))
                    messages.success(request, f"Added {user_obj.get_full_name() or user_obj.username} as a follower.")
                    
        elif action == 'remove_follower':
            user_id = request.POST.get('user_id')
            if user_id:
                user_obj = CustomUser.objects.filter(id=user_id).first()
                if user_obj in shipment.followers.all():
                    shipment.followers.remove(user_obj)
                    messages.success(request, f"Removed {user_obj.get_full_name() or user_obj.username} from followers.")

        if (cargo_before is not None and shipment.status in ('Pending Approval', 'Preparing')
                and sorted(shipment.items.values_list('id', 'batch_id', 'quantity')) != cargo_before):
            shipment.status = 'Logistics Review'
            shipment.assigned_to = None
            shipment.save(update_fields=['status', 'assigned_to'])
            clear_approval_notifications(shipment.tracking_number)
            OrderTimeline.objects.create(shipment=shipment, action="Cargo changed after submission - returned to Logistics Review for re-approval.", user=request.user)
            messages.warning(request, "Cargo changed, so the shipment is back in Logistics Review and needs approval again.")

        if old_status not in ['Arrived', 'Completed'] and shipment.status in ['Arrived', 'Completed']:
            run = shipment.linked_production_run
            if run:
                msg = f"Shipment {shipment.tracking_number} linked to Production Run {run.run_number} has arrived."
                OrderTimeline.objects.create(production_run=run, action=f"Linked shipment {shipment.tracking_number} arrived.", user=request.user)
                if run.created_by:
                    Notification.objects.create(user=run.created_by, message=msg, link=f"/operations/manufacture/run/{run.id}/")
                for f in run.followers.all():
                    Notification.objects.create(user=f, message=msg, link=f"/operations/manufacture/run/{run.id}/")

        if not route_error:
            return redirect('shipment_detail', pk=shipment.pk)
        
    materials = Material.objects.all().order_by('name')
    products = Product.objects.all().order_by('name')
    batches = Batch.objects.filter(status='Active').select_related('material', 'product').order_by('batch_number')
    if shipment.direction in ('Outbound', 'Transfer') and shipment.origin_warehouse_id:
        # a shipment leaves from one place - only offer stock that's actually there
        batches = batches.filter(warehouse_id=shipment.origin_warehouse_id)
    managers = approvers(APPROVE_SHIPMENTS)
    all_users = CustomUser.objects.all().order_by('username')
    
    warehouses = Warehouse.objects.all()
    
    # Calculate extra info from timeline
    timeline_events = shipment.timeline.all().order_by('timestamp')
    first_event = timeline_events.first()
    requested_by = first_event.user if first_event else shipment.last_edited_by
    
    submit_event = timeline_events.filter(action__icontains='Submitted to').last()
    coordinator = submit_event.user if submit_event else None

    context = {
        'shipment': shipment,
        'materials': materials,
        'products': products,
        'batches': batches,
        'cargo_locked': shipment_cargo_locked(shipment),
        'managers': managers,
        'all_users': all_users,
        'can_self_approve': request.user.has_perm(APPROVE_SHIPMENTS),
        'status_choices': Shipment.STATUS_CHOICES,
        'warehouses': warehouses,
        'route_error': route_error,
        'requested_by': requested_by,
        'coordinator': coordinator,
    }
    return render(request, 'shipment_detail.html', context)

@login_required
def mark_notifications_read(request):
    if request.method == 'POST':
        request.user.notifications.all().delete()
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok'})
    back = request.META.get('HTTP_REFERER', '')
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect('home')
    return redirect(back)


@login_required
def profile_view(request):
    if request.method == 'POST':
        user = request.user
        user.first_name = request.POST.get('first_name', user.first_name)
        user.last_name = request.POST.get('last_name', user.last_name)
        email = request.POST.get('email', user.email).strip()
        try:
            validate_email_address(email)
        except ValidationError as e:
            messages.error(request, validation_messages(e))
            return redirect('profile')
        if _email_taken(email, exclude=user):
            messages.error(request, f"Another account already uses {email}.")
            return redirect('profile')
        old = CustomUser.objects.get(pk=user.pk)
        user.email = email
        changes = [f"{label}: '{getattr(old, f)}' -> '{getattr(user, f)}'"
                   for f, label in (('first_name', 'First name'), ('last_name', 'Last name'), ('email', 'Email'))
                   if getattr(old, f) != getattr(user, f)]
        user.save()
        if changes:
            log_user_change(request, user, 'profile_changed', '; '.join(changes))
        messages.success(request, "Profile updated successfully.")
        return redirect('profile')
    
    return render(request, 'profile.html', {'user': request.user})


@login_required
def password_change_view(request):
    """Change your own password. After an admin reset the old password isn't asked for: the user just signed in with it."""
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm

    forced = request.user.must_change_password
    form_class = SetPasswordForm if forced else PasswordChangeForm
    form = form_class(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        if forced and request.user.check_password(form.cleaned_data['new_password1']):
            form.add_error('new_password1', "Choose a new password, not the temporary one.")
        else:
            user = form.save(commit=False)
            user.must_change_password = False
            user.temp_password_set_at = None
            with transaction.atomic():
                user.save()
                log_user_change(request, user, 'password_changed', 'New password chosen after an admin reset' if forced else '')
            update_session_auth_hash(request, user)  # keep this session signed in
            messages.success(request, "Your password has been changed.")
            return redirect('dashboard' if forced else 'profile')
    return render(request, 'password_change.html', {'form': form, 'forced': forced})


def _names(objs):
    """Sorted names for an audit entry, e.g. "Logistics, Sales", or "none"."""
    return ', '.join(sorted(o.name for o in objs)) or 'none'


def _email_taken(email, exclude=None):
    """True if another account already uses `email` (ignoring case). Blank emails never clash."""
    return bool(email) and CustomUser.objects.filter(email__iexact=email).exclude(pk=getattr(exclude, 'pk', None)).exists()


def _new_user_problem(editor, form, groups):
    """Why a new account can't be created from `form` (Users page), or None if it can."""
    username_field = CustomUser._meta.get_field('username')
    if not form['username']:
        return "Enter a username."
    try:
        username_field.run_validators(form['username'])
    except ValidationError:
        return f"Username '{form['username']}' isn't allowed. Use up to {username_field.max_length} letters, digits and @ . + - _ only."
    if CustomUser.objects.filter(username__iexact=form['username']).exists():
        return f"The username '{form['username']}' is already taken."
    try:
        validate_email_address(form['email'])
    except ValidationError as e:
        return validation_messages(e)
    if _email_taken(form['email']):
        return f"Another account already uses {form['email']}."
    too_high = sorted(g.name for g in groups if not can_grant_group(editor, g))
    if too_high:
        return f"You can't give the {', '.join(too_high)} role, because it has permissions you don't have."
    return None


def _outranks(editor, target):
    """True if `target` has powers `editor` lacks, so `editor` may not deactivate them or reset their password."""
    return (target.is_superuser and not editor.is_superuser) or not all(
        can_grant_group(editor, g) for g in target.groups.all())


@login_required
def user_management_view(request):
    denied = action_denied(request, 'user_management')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if not request.user.has_perm(MANAGE_USERS):
        messages.error(request, "Permission Denied. You don't have permission to manage users.")
        return redirect('dashboard')

    users = CustomUser.objects.all().prefetch_related('groups', 'allowed_locations')
    
    query = request.GET.get('q', '')
    if query:
        from django.db.models import Q
        users = users.filter(
            Q(username__icontains=query) | 
            Q(first_name__icontains=query) | 
            Q(last_name__icontains=query) |
            Q(email__icontains=query)
        )
    # Deactivated accounts are kept for their history but hidden unless asked for
    show_inactive = request.GET.get('inactive') == '1'
    inactive_count = users.filter(is_active=False).count()
    if not show_inactive:
        users = users.filter(is_active=True)

    users, sort_ctx = apply_list_sort(request, users, {
        'username': 'username',
        'status': 'is_active',
        'last_login': 'last_login',
        'updated_at': 'updated_at',
    }, default='username')
    page_size = get_page_size(request)
    users = Paginator(users, page_size).get_page(request.GET.get('page'))
    from .login_throttle import is_locked_anywhere
    for u in users:
        u.is_locked = is_locked_anywhere(u.username)

    roles = Group.objects.order_by('name')
    warehouses = Warehouse.objects.all()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_user':
            user_id = request.POST.get('user_id')
            user_obj = get_object_or_404(CustomUser, id=user_id)

            # Roles: only groups the editor could hold themselves may be added or removed
            old_groups = set(user_obj.groups.all())
            new_groups = set(Group.objects.filter(id__in=request.POST.getlist('roles')))
            changed = new_groups ^ old_groups
            too_high = sorted(g.name for g in changed if not can_grant_group(request.user, g))
            if too_high:
                messages.error(request, f"You can't add or remove the {', '.join(too_high)} role, because it has permissions you don't have.")
                return redirect('user_management')

            is_active = request.POST.get('is_active') == 'on'
            if not is_active and user_obj.is_active:
                if user_obj == request.user:
                    messages.error(request, "You can't deactivate your own account.")
                    return redirect('user_management')
                if _outranks(request.user, user_obj):
                    messages.error(request, f"You can't deactivate {user_obj.username}, because they have permissions you don't have.")
                    return redirect('user_management')

            old_locations = set(user_obj.allowed_locations.all())
            new_locations = set(Warehouse.objects.filter(id__in=request.POST.getlist('locations')))
            was_active = user_obj.is_active
            with transaction.atomic():
                user_obj.is_active = is_active
                user_obj.groups.set(new_groups)
                user_obj.allowed_locations.set(new_locations)
                user_obj.updated_by = request.user
                user_obj.save()
                if was_active != is_active:
                    log_user_change(request, user_obj, 'activated' if is_active else 'deactivated')
                if changed:
                    log_user_change(request, user_obj, 'roles_changed', f"{_names(old_groups)} -> {_names(new_groups)}")
                if old_locations != new_locations:
                    log_user_change(request, user_obj, 'locations_changed', f"{_names(old_locations)} -> {_names(new_locations)}")
            messages.success(request, f"User {user_obj.username} updated successfully.")
            return redirect('user_management')

        if action == 'reset_password':
            from .account import issue_temp_password
            user_obj = get_object_or_404(CustomUser, id=request.POST.get('user_id'))
            if user_obj == request.user:
                messages.error(request, "To change your own password, use Change password on My Profile.")
            elif not user_obj.is_active:
                messages.error(request, f"{user_obj.username} is inactive. Activate the account before resetting the password.")
            elif _outranks(request.user, user_obj):
                messages.error(request, f"You can't reset {user_obj.username}'s password, because they have permissions you don't have.")
            else:
                with transaction.atomic():
                    password = issue_temp_password(user_obj, request.user)
                    log_user_change(request, user_obj, 'password_reset')
                # Shown once on the next page load, then dropped from the session
                request.session['temp_password_issued'] = {
                    'username': user_obj.username, 'password': password, 'new_account': False}
            return redirect('user_management')

        if action == 'unlock_user':
            from .login_throttle import unlock
            user_obj = get_object_or_404(CustomUser, id=request.POST.get('user_id'))
            unlock(user_obj.username)
            log_user_change(request, user_obj, 'unlocked')
            messages.success(request, f"{user_obj.username} can sign in again.")
            return redirect('user_management')

        if action == 'sign_out_everywhere':
            from .account import sign_out_everywhere
            user_obj = get_object_or_404(CustomUser, id=request.POST.get('user_id'))
            if _outranks(request.user, user_obj):
                messages.error(request, f"You can't sign out {user_obj.username}, because they have permissions you don't have.")
                return redirect('user_management')
            # Signing yourself out everywhere keeps this browser signed in
            keep = request.session.session_key if user_obj == request.user else None
            ended = sign_out_everywhere(user_obj, keep_session_key=keep)
            log_user_change(request, user_obj, 'signed_out_everywhere', f"{ended} session{'s' if ended != 1 else ''} ended")
            where = 'other devices' if keep else 'every device'
            messages.success(request, f"{user_obj.username} was signed out on {where} ({ended} session{'s' if ended != 1 else ''}).")
            return redirect('user_management')

        if action == 'create_user':
            from .account import issue_temp_password
            form = {k: request.POST.get(k, '').strip() for k in ('username', 'first_name', 'last_name', 'email')}
            groups = set(Group.objects.filter(id__in=request.POST.getlist('roles')))
            problem = _new_user_problem(request.user, form, groups)
            if problem:
                messages.error(request, problem)
                return redirect('user_management')
            with transaction.atomic():
                user_obj = CustomUser(username=form['username'], first_name=form['first_name'],
                                      last_name=form['last_name'], email=form['email'], updated_by=request.user)
                user_obj.set_unusable_password()
                user_obj.save()
                user_obj.groups.set(groups)
                locations = Warehouse.objects.filter(id__in=request.POST.getlist('locations'))
                user_obj.allowed_locations.set(locations)
                password = issue_temp_password(user_obj, request.user)
                log_user_change(request, user_obj, 'created',
                                f"Roles: {_names(groups)}. Facilities: {_names(locations)}.")
            request.session['temp_password_issued'] = {
                'username': user_obj.username, 'password': password, 'new_account': True}
            return redirect('user_management')

    context = {
        'temp_password_issued': request.session.pop('temp_password_issued', None),
        'users': users,
        'roles': roles,
        'warehouses': warehouses,
        'query': query,
        'show_inactive': show_inactive,
        'inactive_count': inactive_count,
        'page_size': page_size,
        **sort_ctx,
    }
    return render(request, 'user_management.html', context)



@login_required
def user_audit_log_view(request):
    """History of account changes (created, password resets, roles, facilities, activation), newest first."""
    if not request.user.has_perm(MANAGE_USERS):
        messages.error(request, "Permission Denied. You don't have permission to manage users.")
        return redirect('dashboard')
    from .models import UserAuditLog

    entries = UserAuditLog.objects.select_related('actor', 'target')
    target = None
    user_id = request.GET.get('user', '')
    if user_id.isdigit():
        target = CustomUser.objects.filter(pk=user_id).first()
        entries = entries.filter(target_id=user_id)
    action = request.GET.get('action', '')
    if action in dict(UserAuditLog.ACTION_CHOICES):
        entries = entries.filter(action=action)
    query = request.GET.get('q', '').strip()
    if query:
        entries = entries.filter(Q(target_username__icontains=query) | Q(actor__username__icontains=query)
                                 | Q(action__in=('setting_changed', 'item_archived', 'item_restored'), details__icontains=query))

    page_size = get_page_size(request, default=50)
    keep = request.GET.copy()
    keep.pop('page', None)
    return render(request, 'user_audit_log.html', {
        'entries': Paginator(entries, page_size).get_page(request.GET.get('page')),
        'target': target,
        'action': action,
        'query': query,
        'action_choices': UserAuditLog.ACTION_CHOICES,
        'page_size': page_size,
        'page_qs': keep.urlencode(),
    })


def _fg_fefo_batches(product):
    """Active finished-goods batches with free stock, earliest expiry first (FEFO)."""
    from django.db.models import F
    return [
        b for b in unexpired(Batch.objects.filter(product=product, status='Active'))
        .select_related('warehouse')
        .order_by(F('expiry_date').asc(nulls_last=True), 'manufacturing_date', 'id')
        if b.quantity - b.allocated_quantity > 0
    ]


def _fefo_plan(batches, total):
    """{batch_id: qty} taking `total` from `batches` strictly in FEFO order."""
    plan, remaining = {}, total
    for b in batches:
        if remaining <= 0:
            break
        take = min(b.quantity - b.allocated_quantity, remaining)
        plan[b.id] = take
        remaining -= take
    return plan


@login_required
def so_allocate_view(request, pk):
    denied = action_denied(request, 'so_allocate')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    so = get_object_or_404(SalesOrder, pk=pk)

    def still_needed(item):
        from .utils import so_line_commitment
        covered = so_line_commitment(so, item.product, item.quantity_shipped)['total']
        return max(Decimal(str(item.quantity_ordered)) - covered, Decimal('0'))

    if so.status in UNAPPROVED_STATUSES:
        messages.error(request, f"{so.so_number} hasn't been approved yet, so no stock can be allocated to it.")
        return redirect('so_detail', pk=so.pk)
    # Allocation is open while the order is Pending, or part-shipped with units still owed, as on the order page
    if so.status not in SO_ALLOCATABLE:
        messages.error(request, f"{so.so_number} is {so.status}, so stock can't be allocated to it now.")
        return redirect('so_detail', pk=so.pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'allocate_manual':
            if so.status in ('Shipped', 'Delivered'):
                # Allocation rewrites the order status, so skip finished orders
                messages.error(request, f"{so.so_number} is already {so.status}. Nothing more can be allocated.")
                return redirect('so_detail', pk=so.pk)
            override_reason = request.POST.get('override_reason', '').strip()

            # Validate everything before writing anything
            plans = []  # (item, needed, [(batch, qty), ...])
            off_fefo = []
            for item in so.items.select_related('product'):
                needed = still_needed(item)
                batches = _fg_fefo_batches(item.product)
                chosen = []
                for b in batches:
                    raw = request.POST.get(f'batch_qty_{item.id}_{b.id}', '').strip()
                    try:
                        amt = Decimal(raw) if raw else Decimal('0')
                    except InvalidOperation:
                        messages.error(request, f"Invalid quantity for batch {b.batch_number}.")
                        return redirect('so_allocate', pk=so.pk)
                    if amt < 0:
                        messages.error(request, f"Quantity for batch {b.batch_number} can't be negative.")
                        return redirect('so_allocate', pk=so.pk)
                    if amt > b.quantity - b.allocated_quantity:
                        messages.error(request, f"Cannot allocate {amt} from batch {b.batch_number}. Only {b.quantity - b.allocated_quantity} available.")
                        return redirect('so_allocate', pk=so.pk)
                    # Facility limit deliberately OFF here: orders are filled from whichever facility has stock,
                    # so a site-limited planner must still be able to reserve it. Uncomment to enforce.
                    # if amt > 0 and not may_use_warehouse(request.user, b.warehouse_id):
                    #     messages.error(request, f"Batch {b.batch_number}: {FACILITY_REFUSED}")
                    #     return redirect('so_allocate', pk=so.pk)
                    if amt > 0:
                        chosen.append((b, amt))
                total = sum((amt for _, amt in chosen), Decimal('0'))
                if total > needed:
                    messages.error(request, f"{item.product.name}: allocating {total} is more than the {needed} this order still needs.")
                    return redirect('so_allocate', pk=so.pk)
                # Compare per expiry date: batches expiring the same day are interchangeable
                plan = _fefo_plan(batches, total)
                by_expiry = lambda amounts: {e: sum((amounts.get(b.id, 0) for b in batches if b.expiry_date == e), Decimal('0'))
                                             for e in {b.expiry_date for b in batches}}
                if by_expiry({b.id: amt for b, amt in chosen}) != by_expiry(plan):
                    off_fefo.append(item.product.name)
                plans.append((item, needed, chosen))

            # A quantity posted for a batch the page no longer offers (reserved elsewhere, quarantined, expired) must not vanish silently.
            offered = {f'batch_qty_{item.id}_{b.id}' for item, _, _ in plans for b in _fg_fefo_batches(item.product)}
            stale = [k for k, v in request.POST.items() if k.startswith('batch_qty_') and k not in offered and v.strip() not in ('', '0')]
            if stale:
                ids = [k.rsplit('_', 1)[-1] for k in stale]
                names = ', '.join(Batch.objects.filter(id__in=[i for i in ids if i.isdigit()]).values_list('batch_number', flat=True)) or 'a selected batch'
                messages.error(request, f"Batch {names} is no longer available (reserved elsewhere, quarantined or expired). Refresh the page and pick again.")
                return redirect('so_allocate', pk=so.pk)
            if not any(chosen for _, _, chosen in plans) and request.POST.get('send_to_manufacturing') != 'true':
                messages.error(request, f"Nothing was allocated to {so.so_number}. Enter a quantity for at least one batch.")
                return redirect('so_allocate', pk=so.pk)

            if off_fefo and not override_reason:
                messages.error(request, f"Batches for {', '.join(off_fefo)} don't follow FEFO (earliest expiry first). Give an override reason to continue.")
                return redirect('so_allocate', pk=so.pk)

            from .utils import StockTaken, lock_free_batch
            with transaction.atomic():
                total_unfulfilled_across_so = Decimal('0')
                from .utils import sync_production_run_yield
                for item, needed, chosen in plans:
                    for batch, amt in chosen:
                        try:
                            batch = lock_free_batch(batch.id, amt)
                        except StockTaken as e:
                            transaction.set_rollback(True)   # undo anything already reserved in this submit
                            messages.error(request, str(e))
                            return redirect('so_allocate', pk=so.pk)
                        StockAllocation.objects.create(batch=batch, sales_order=so, quantity=amt)
                        batch.allocated_quantity += amt
                        batch.save(update_fields=['allocated_quantity'])
                    if chosen:
                        OrderTimeline.objects.create(
                            sales_order=so,
                            action=f"Allocated {item.product.sku}: " + ", ".join(f"{amt} from {b.batch_number}" for b, amt in chosen) + ".",
                            user=request.user,
                        )
                    unfulfilled = needed - sum((amt for _, amt in chosen), Decimal('0'))
                    total_unfulfilled_across_so += unfulfilled
                    sync_production_run_yield(so, item.product, unfulfilled, request.user)

                if off_fefo:
                    OrderTimeline.objects.create(
                        sales_order=so,
                        action=f"Non-FEFO allocation for {', '.join(off_fefo)}. Reason: {override_reason}",
                        user=request.user,
                    )

                send_to_mfg = request.POST.get('send_to_manufacturing') == 'true'
                plant_id = request.POST.get('manufacturing_plant_id')

                if send_to_mfg and total_unfulfilled_across_so > 0:
                    plant = None
                    if plant_id:
                        plant = Warehouse.objects.filter(id=plant_id).first()

                    from .utils import create_shortage_production_runs
                    create_shortage_production_runs(so, plant, request.user)

                    if so.status == 'Pending':   # a part-shipped order keeps showing that
                        so.status = 'Awaiting Acknowledgement'
                    OrderTimeline.objects.create(sales_order=so, action="Shortages sent to manufacturing queue.", user=request.user)
                elif total_unfulfilled_across_so <= Decimal('0'):
                    so.status = 'Ready to Ship'
                    OrderTimeline.objects.create(sales_order=so, action="Allocation completed. Status updated to Ready to Ship.", user=request.user)
                else:
                    OrderTimeline.objects.create(sales_order=so, action="Partial allocation completed. Shortages remain.", user=request.user)
                so.save()
                from .utils import prompt_delivery_choice
                prompt_delivery_choice(so, request.user)

                allocated = sum((amt for _, _, chosen in plans for _, amt in chosen), Decimal('0'))
                messages.success(request, f"Allocated {allocated.normalize():f} to {so.so_number}."
                                 + (f" {total_unfulfilled_across_so.normalize():f} still short." if total_unfulfilled_across_so > 0 else " Fully covered."))
                return redirect('so_detail', pk=so.pk)

    # Gather data for UI
    manufacturing_plants = Warehouse.objects.filter(location_type__in=Warehouse.MANUFACTURING_TYPES).order_by('name')

    allocation_data = []
    for item in so.items.select_related('product'):
        needed = still_needed(item)
        batches = _fg_fefo_batches(item.product)
        plan = _fefo_plan(batches, needed)
        allocation_data.append({
            'item_id': item.id,
            'product': item.product,
            'needed': needed,
            'batches': [{
                'id': b.id,
                'number': b.batch_number,
                'warehouse': b.warehouse.name if b.warehouse else 'Unknown',
                'available': b.quantity - b.allocated_quantity,
                'expiry': b.expiry_date,
                'recommended': plan.get(b.id, Decimal('0')),
            } for b in batches],
        })

    return render(request, 'so_allocate.html', {
        'so': so,
        'allocation_data': allocation_data,
        'manufacturing_plants': manufacturing_plants
    })

@login_required
def so_create_shipment_view(request, pk):
    so = get_object_or_404(SalesOrder, pk=pk)
    if request.method == 'POST':
        if not request.user.has_perm('core.add_shipment'):
            messages.error(request, "You don't have permission to create shipments.")
            return redirect('so_detail', pk=so.pk)
        if so.status != 'Ready to Ship':
            messages.error(request, "Order is not ready to ship.")
            return redirect('so_detail', pk=so.pk)

        moving = so.shipments.filter(direction='Transfer').exclude(status__in=['Completed', 'Cancelled'])
        if moving.exists():
            messages.error(request, f"Stock is still being moved to {so.origin_warehouse.name} ({', '.join(moving.values_list('tracking_number', flat=True))}). Wait for it to arrive, or switch to separate deliveries.")
            return redirect('so_detail', pk=so.pk)

        allocations = list(so.allocations.filter(shipment__isnull=True).select_related('batch__product'))
        if not allocations:
            existing = so.shipments.filter(direction='Outbound').exclude(status='Cancelled')
            if existing.exists():
                names = ", ".join(s.tracking_number for s in existing)
                messages.error(request, f"All allocated stock for this order is already held by logistics order(s) {names}.")
            else:
                messages.error(request, "There is no allocated stock to draft a logistics order from.")
            return redirect('so_detail', pk=so.pk)

        held = sorted({a.batch.batch_number for a in allocations if a.batch.status == 'Quarantined'})
        if held:
            messages.error(request, f"Batch {', '.join(held)} is in QA quarantine and can't be shipped until QA releases it.")
            return redirect('so_detail', pk=so.pk)
        expired = sorted({a.batch.batch_number for a in allocations if a.batch.expiry_date and a.batch.expiry_date < timezone.localdate()})
        if expired:
            messages.error(request, f"Batch {', '.join(expired)} has expired. Unallocate it and allocate fresh stock, or have QA extend its expiry first.")
            return redirect('so_detail', pk=so.pk)

        # One outbound shipment per origin warehouse of the allocated stock.
        by_warehouse = {}
        for alloc in allocations:
            by_warehouse.setdefault(alloc.batch.warehouse_id, []).append(alloc)

        with transaction.atomic():
            shipments = []
            for wh_id, wh_allocs in by_warehouse.items():
                tracking_number = generate_next_code(Shipment, 'tracking_number', 'SHP', 1001, pad=4)
                shipment = Shipment.objects.create(
                    tracking_number=tracking_number,
                    sales_order=so,
                    direction='Outbound',
                    status='Draft',
                    origin_warehouse_id=wh_id or (so.origin_warehouse_id if so.origin_warehouse else None),
                    last_edited_by=request.user,
                    client_contact_name=(so.client.contact_person or None) if so.client else None,
                    client_contact_phone=(so.client.phone or so.client.email or None) if so.client else None,
                    client_address=(so.client.delivery_address or None) if so.client else None,
                )
                OrderTimeline.objects.create(shipment=shipment, action=f"Drafted from {so.so_number}.", user=request.user)

                # Create shipment items from allocated stock and move the stock lock onto this shipment so a sibling scrap cannot release it.
                for alloc in wh_allocs:
                    ShipmentItem.objects.create(
                        shipment=shipment,
                        product=alloc.batch.product,
                        batch=alloc.batch,
                        quantity=alloc.quantity
                    )
                    StockAllocation.objects.create(batch=alloc.batch, shipment=shipment, quantity=alloc.quantity)
                    alloc.delete()

                # so.status stays 'Ready to Ship' until logistics dispatches it.

                OrderTimeline.objects.create(
                    sales_order=so,
                    action=f"Auto-drafted logistics shipment {tracking_number}"
                           + (f" from {shipment.origin_warehouse.name}." if shipment.origin_warehouse else "."),
                    user=request.user
                )
                shipments.append(shipment)

            if len(shipments) == 1:
                messages.success(request, f"Logistics Order {shipments[0].tracking_number} drafted successfully.")
                return redirect('shipment_detail', pk=shipments[0].pk)
            messages.success(
                request,
                f"Stock is in {len(shipments)} warehouses, so {len(shipments)} logistics orders were drafted: "
                + ", ".join(sh.tracking_number for sh in shipments) + "."
            )
            return redirect('so_detail', pk=so.pk)

    return redirect('so_detail', pk=so.pk)

@login_required
def production_run_allocate_view(request, pk):
    denied = action_denied(request, 'production_run_allocate')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    from django.db.models import F
    from decimal import Decimal
    
    run = get_object_or_404(ProductionRun, pk=pk)
    if run.status not in ('Pending Allocation', 'Planned', 'Awaiting Materials', 'InProgress', 'Paused'):
        messages.error(request, f"Materials can't be allocated to a run that is {run.get_status_display()}.")
        return redirect('production_run_detail', pk=pk)
    # More than the recipe needs can be reserved at any stage, but only with a reason.
    extra_allowed = True
    under_way = run.status in ('InProgress', 'Paused')

    # Existing allocations stay; suggestions only cover what is still short.
    existing_allocations = list(run.allocations.select_related('batch__material', 'batch__warehouse', 'shipment'))
    existing_by_material = {}
    for a in existing_allocations:
        existing_by_material[a.batch.material_id] = existing_by_material.get(a.batch.material_id, Decimal('0')) + a.quantity
    existing_shipments = run.linked_shipments.all()

    # Calculate FEFO recommendations
    recipe_reqs = []
    fefo_recommended_ids = []

    for req in run.target_product.recipe_items.all():
        # Round the requirement up to 2dp stock precision (recipes are 4dp) to avoid under-allocating.
        required_total = (Decimal(str(req.quantity_required)) * Decimal(str(run.expected_yield))).quantize(
            Decimal('0.01'), rounding=ROUND_CEILING
        )
        already = existing_by_material.get(req.material_id, Decimal('0'))
        needed = max(required_total - already, Decimal('0'))

        # Get all active batches globally, ordered by expiry date (FEFO)
        batches = unexpired(Batch.objects.filter(material=req.material, status='Active')).annotate(
            avail=F('quantity') - F('allocated_quantity')
        ).filter(avail__gt=0).order_by(F('expiry_date').asc(nulls_last=True), 'manufacturing_date', 'id')
        
        remaining = needed
        batch_list = []
        for b in batches:
            avail_dec = Decimal(str(b.avail))
            take = Decimal('0')
            is_fefo = False
            
            if remaining > 0:
                take = min(avail_dec, remaining)
                remaining -= take
                is_fefo = True
                fefo_recommended_ids.append(b.id)
                
            batch_list.append({
                'obj': b, 
                'suggested_qty': float(take), 
                'is_fefo': is_fefo,
                'avail': float(avail_dec)
            })
            
        recipe_reqs.append({
            'material': req.material,
            'needed': float(needed),
            'required_total': float(required_total),
            'already': float(already),
            'batch_list': batch_list,
        })

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'allocate_run':
            # No acknowledgement needed for top-ups: existing allocation is netted off and over-allocating is refused.
            # The run's plant is set at creation; only ask if it has none.
            destination_warehouse = run.manufacturing_plant
            if not destination_warehouse:
                wh_id = request.POST.get('warehouse_id')
                if not wh_id:
                    messages.error(request, "Please select the manufacturing facility for this run.")
                    return redirect('production_run_allocate', pk=pk)
                destination_warehouse = get_object_or_404(Warehouse, id=wh_id)
                run.manufacturing_plant = destination_warehouse
            
            override_reason = request.POST.get('override_reason', '').strip()
            
            # Check for non-FEFO overrides
            selected_allocations = {} # batch_id -> qty
            is_overridden = False
            
            for key, value in request.POST.items():
                if key.startswith('batch_qty_') and value:
                    try:
                        batch_id = int(key.replace('batch_qty_', ''))
                        qty = Decimal(value)
                        if qty > 0:
                            selected_allocations[batch_id] = qty
                            if batch_id not in fefo_recommended_ids:
                                is_overridden = True
                    except (ValueError, TypeError):
                        pass

            # Facility limit deliberately OFF here: runs draw materials from whichever facility has them,
            # so a site-limited planner must still be able to reserve them. Uncomment to enforce.
            # limited = limited_warehouse_ids(request.user)
            # if limited is not None and Batch.objects.filter(id__in=selected_allocations).exclude(warehouse_id__in=limited).exists():
            #     messages.error(request, FACILITY_REFUSED)
            #     return redirect('production_run_allocate', pk=pk)

            # Re-check the page's numbers: only offered batches, within free stock, no over-allocation, shortfalls acknowledged.
            batch_info = {}
            for rr in recipe_reqs:
                for b in rr['batch_list']:
                    batch_info[b['obj'].id] = (rr['material'], Decimal(str(b['avail'])))
            # A material needing nothing more has no FEFO plan to deviate from; the extra reason covers that case.
            # Overridden if the picked amounts differ from the FEFO plan for the same total
            is_overridden = False
            for rr in recipe_reqs:
                if Decimal(str(rr['needed'])) <= Decimal('0.005'):
                    continue
                picked = {b['obj'].id: selected_allocations[b['obj'].id] for b in rr['batch_list']
                          if b['obj'].id in selected_allocations}
                left = sum(picked.values(), Decimal('0'))
                for b in rr['batch_list']:
                    fefo_take = min(Decimal(str(b['avail'])), left)
                    left -= fefo_take
                    if abs(picked.get(b['obj'].id, Decimal('0')) - fefo_take) > Decimal('0.005'):
                        is_overridden = True
            selected_by_material = {}
            for batch_id, qty in selected_allocations.items():
                if batch_id not in batch_info:
                    messages.error(request, "One of the selected batches isn't available for this run any more. Please review and try again.")
                    return redirect('production_run_allocate', pk=pk)
                material, avail = batch_info[batch_id]
                if qty > avail:
                    messages.error(request, f"Cannot allocate {qty} {material.unit_of_measure} of {material.name} from one batch; only {avail.quantize(Decimal('0.01'))} is free.")
                    return redirect('production_run_allocate', pk=pk)
                selected_by_material[material.id] = selected_by_material.get(material.id, Decimal('0')) + qty
            shortages = []
            extras = []   # (material, quantity beyond the recipe requirement)
            for rr in recipe_reqs:
                still_needed = Decimal(str(rr['needed']))
                picked = selected_by_material.get(rr['material'].id, Decimal('0'))
                if picked > still_needed + Decimal('0.005'):
                    if not extra_allowed:
                        messages.error(request, f"{rr['material'].name}: allocating {picked} is more than the {still_needed.quantize(Decimal('0.01'))} still needed.")
                        return redirect('production_run_allocate', pk=pk)
                    extras.append((rr['material'], (picked - still_needed).quantize(Decimal('0.01'))))
                if still_needed - picked > Decimal('0.005'):
                    shortages.append(f"{rr['material'].name} short by {(still_needed - picked).quantize(Decimal('0.01'))} {rr['material'].unit_of_measure}")
            extra_reason = request.POST.get('extra_reason', '').strip()
            if extras and not extra_reason:
                messages.error(request, "Give a reason for allocating more than the recipe needs: " + ", ".join(f"{m.name} +{q}" for m, q in extras) + ".")
                return redirect('production_run_allocate', pk=pk)
            if shortages and not request.POST.get('allocate_short'):
                messages.error(request, "Not enough is allocated: " + "; ".join(shortages) + ". Tick 'allocate anyway' to continue with a shortfall.")
                return redirect('production_run_allocate', pk=pk)

            if is_overridden and not override_reason:
                messages.error(request, "You selected non-FEFO recommended batches. Please provide an override reason.")
                return redirect('production_run_allocate', pk=pk)
                
            if is_overridden:
                run.fefo_override_reason = override_reason
                
            # Perform Allocations & Auto-Logistics
            with transaction.atomic():
                warehouse_groups = {} # warehouse_id -> [ {batch, qty} ]

                from .utils import StockTaken, lock_free_batch
                for batch_id, qty in selected_allocations.items():
                    try:
                        batch = lock_free_batch(batch_id, qty)
                    except StockTaken as e:
                        transaction.set_rollback(True)
                        messages.error(request, str(e))
                        return redirect('production_run_allocate', pk=pk)
                    wh = batch.warehouse if batch.warehouse else None
                    wh_key = wh.id if wh else None
                    if wh_key not in warehouse_groups:
                        warehouse_groups[wh_key] = []
                    warehouse_groups[wh_key].append({'batch': batch, 'qty': qty})

                # Auto-generate Shipments split by origin, then lock stock against each shipment
                transfer_count = 0
                for origin_wh_id, items in warehouse_groups.items():
                    origin_warehouse = Warehouse.objects.get(id=origin_wh_id) if origin_wh_id else None

                    if origin_wh_id == destination_warehouse.id:
                        # Already at the plant: reserve it, no transfer needed
                        for item in items:
                            item['batch'].allocated_quantity += item['qty']
                            item['batch'].save(update_fields=['allocated_quantity'])
                            StockAllocation.objects.create(batch=item['batch'], production_run=run, quantity=item['qty'])
                        continue
                    transfer_count += 1

                    # Generate unique tracking number
                    tracking = generate_next_code(Shipment, 'tracking_number', 'SHP', 1001, pad=4)

                    shipment = Shipment.objects.create(
                        tracking_number=tracking,
                        direction='Transfer',
                        status='Draft',
                        linked_production_run=run,
                        origin_warehouse=origin_warehouse,
                        destination_warehouse=destination_warehouse,
                        is_auto_generated=True,
                        last_edited_by=request.user
                    )

                    for item in items:
                        batch = item['batch']
                        qty = item['qty']

                        ShipmentItem.objects.create(
                            shipment=shipment,
                            batch=batch,
                            material=batch.material,
                            quantity=qty
                        )

                        batch.allocated_quantity += qty
                        batch.save(update_fields=['allocated_quantity'])

                        StockAllocation.objects.create(batch=batch, production_run=run, shipment=shipment, quantity=qty)

                if shortages:
                    OrderTimeline.objects.create(
                        production_run=run,
                        action="Allocated with a shortfall: " + "; ".join(shortages) + ".",
                        user=request.user)
                for material, qty in extras:
                    OrderTimeline.objects.create(
                        production_run=run,
                        action=f"Extra material allocated during production: {qty} {material.unit_of_measure} of {material.name} beyond the recipe requirement. Reason: {extra_reason}",
                        user=request.user)

                # A run already under way keeps its status; otherwise wait for shipments only if transfers were needed.
                if run.status not in ('InProgress', 'Paused'):
                    run.status = 'Awaiting Materials' if transfer_count else 'Planned'
                run.save()

            under_way = run.status in ('InProgress', 'Paused')
            if transfer_count:
                messages.success(request, f"Materials allocated. Auto-generated {transfer_count} draft logistics shipment(s).")
            elif under_way:
                messages.success(request, "Materials allocated from stock already at the plant.")
            else:
                messages.success(request, "Materials allocated from stock already at the plant. Ready to start.")
            return redirect('production_run_detail', pk=run.pk) # Redirect to the new dedicated detail page
            
    warehouses = Warehouse.objects.all()
    
    return render(request, 'production_allocate.html', {
        'run': run,
        'recipe_reqs': recipe_reqs,
        'warehouses': warehouses,
        'existing_allocations': existing_allocations,
        'existing_shipments': existing_shipments,
        'extra_allowed': extra_allowed,
        'under_way': under_way,
    })

def parse_extra_material_sources(request, bom_materials, allocated_by_material):
    """Validates the extra-material section of the run-completion form: each excess must be confirmed and fully sourced from plant batches or "not in records" rows (with a reason). Returns (sources, error)."""
    from decimal import Decimal, InvalidOperation
    cent = Decimal('0.01')
    sources = []
    for bom in bom_materials:
        mat = bom['material']
        raw = request.POST.get(f'actual_qty_{mat.id}')
        if not raw:
            continue
        try:
            actual = Decimal(raw)
        except InvalidOperation:
            return [], f"Enter a valid quantity for {mat.name}."
        allocated = Decimal(str(allocated_by_material.get(mat.id, 0)))
        excess = (actual - allocated).quantize(cent)
        if excess <= 0:
            continue

        unit = mat.unit_of_measure
        if not request.POST.get(f'extra_confirm_{mat.id}'):
            return [], f"{mat.name}: {excess} {unit} more than allocated was entered. Please confirm that figure is correct."

        batch_ids = request.POST.getlist(f'extra_batch_{mat.id}')
        qtys = request.POST.getlist(f'extra_qty_{mat.id}')
        reasons = request.POST.getlist(f'extra_reason_{mat.id}')
        by_id = {b.id: b for b in bom['plant_batches']}
        used_per_batch, total = {}, Decimal('0')
        for i, choice in enumerate(batch_ids):
            qty_raw = (qtys[i] if i < len(qtys) else '').strip()
            if not choice and not qty_raw:
                continue
            try:
                qty = Decimal(qty_raw)
            except InvalidOperation:
                return [], f"{mat.name}: enter a quantity for each source row."
            if qty <= 0:
                return [], f"{mat.name}: source quantities must be greater than zero."
            if choice == 'unrecorded':
                reason = (reasons[i] if i < len(reasons) else '').strip()
                if not reason:
                    return [], f"{mat.name}: a reason is required for material from a batch not in records."
                sources.append({'material': mat, 'batch': None, 'qty': qty, 'reason': reason})
            else:
                try:
                    batch = by_id[int(choice)]
                except (ValueError, KeyError):
                    return [], f"{mat.name}: choose a batch (or 'Batch not in records') for each source row."
                used_per_batch[batch.id] = used_per_batch.get(batch.id, Decimal('0')) + qty
                if used_per_batch[batch.id] > Decimal(str(batch.avail)):
                    return [], f"{mat.name}: batch {batch.batch_number} only has {Decimal(str(batch.avail)).quantize(cent)} {unit} free."
                sources.append({'material': mat, 'batch': batch, 'qty': qty, 'reason': ''})
            total += qty
        if abs(total - excess) > Decimal('0.005'):
            return [], f"{mat.name}: the sources add up to {total} {unit} but {excess} {unit} more than allocated was entered."
    return sources, None


@login_required
def production_run_detail_view(request, pk):
    denied = action_denied(request, 'production_run_detail')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    from .models import RunMaterialUsage
    from decimal import Decimal
    run = get_object_or_404(ProductionRun, pk=pk)
    linked_shipments = run.linked_shipments.all()
    
    # Calculate if we have any pending shipments (Soft Lock check)
    all_shipments_arrived = True
    pending_shipments_count = 0
    for shp in linked_shipments:
        if shp.status not in ['Arrived', 'Completed']:
            all_shipments_arrived = False
            pending_shipments_count += 1
            
    # BOM for the usage form and pre-start preview. "Ready" = allocated stock physically at the plant (no transfer, or transfer arrived).
    allocated_by_material, ready_by_material = {}, {}
    for a in run.allocations.select_related('batch', 'shipment'):
        mid = a.batch.material_id
        qty = float(a.quantity)
        allocated_by_material[mid] = allocated_by_material.get(mid, 0) + qty
        if a.shipment is None or a.shipment.status in ('Arrived', 'Completed'):
            ready_by_material[mid] = ready_by_material.get(mid, 0) + qty

    bom_materials = []
    for req in run.target_product.recipe_items.all():
        needed = float(req.quantity_required) * float(run.expected_yield)
        avail = float(unexpired(Batch.objects.filter(material=req.material, status='Active')).annotate(
            avail=F('quantity') - F('allocated_quantity')
        ).aggregate(s=Sum('avail'))['s'] or 0)
        allocated = allocated_by_material.get(req.material_id, 0)
        ready = ready_by_material.get(req.material_id, 0)
        if ready >= needed - 0.005:
            state = 'ready'
        elif allocated >= needed - 0.005:
            state = 'in_transit'
        elif allocated > 0:
            state = 'partial'
        else:
            state = 'none'
        # Unreserved stock at the run's plant: what extra material can be traced to
        plant_batches = []
        if run.manufacturing_plant_id:
            plant_batches = list(unexpired(Batch.objects.filter(
                material=req.material, status='Active', warehouse_id=run.manufacturing_plant_id,
            )).annotate(avail=F('quantity') - F('allocated_quantity')).filter(avail__gt=0).order_by(
                F('expiry_date').asc(nulls_last=True), 'manufacturing_date'))
        bom_materials.append({
            'material': req.material,
            'needed': needed,
            'available': avail,
            'sufficient': avail >= needed,
            'allocated': allocated,
            'ready': ready,
            'state': state,
            'plant_batches': plant_batches,
        })
            
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'schedule_run':
            start_val = request.POST.get('start_time')
            end_val = request.POST.get('end_time')
            if start_val: run.start_time = start_val
            if end_val: run.end_time = end_val
            run.save()
            messages.success(request, "Production schedule updated successfully. Calendar is updated.")
            return redirect('production_run_detail', pk=pk)
            
        elif action == 'start_production':
            if run.status == 'Pending Allocation':
                messages.error(request, "Allocate materials before starting production.")
                return redirect('production_run_detail', pk=pk)
            if run.status not in ('Planned', 'Awaiting Materials'):
                # A stale page or replayed form must not restart a paused, running or finished run
                messages.error(request, f"This run is {run.get_status_display()} and can't be started.")
                return redirect('production_run_detail', pk=pk)
            from .qa import run_hold_problem
            hold_problem = run_hold_problem(run)
            if hold_problem:
                messages.error(request, hold_problem)
                return redirect('production_run_detail', pk=pk)
            override_remark = request.POST.get('override_remark', '').strip()
            if not all_shipments_arrived and not override_remark:
                messages.error(request, "Cannot start production: some shipments have not arrived. An override remark is required.")
                return redirect('production_run_detail', pk=pk)
                
            run.status = 'InProgress'
            run.exact_start_time = timezone.now()
            if override_remark:
                run.fefo_override_reason = (run.fefo_override_reason or '') + f"\nStarted with partial materials. Remark: {override_remark}"
            run.save()
            from .utils import mark_order_in_production
            mark_order_in_production(run, request.user)
            messages.success(request, f"Production Run {run.run_number} started.")
            return redirect('production_run_detail', pk=pk)

        elif action == 'scrap_run':
            if run.status in ['InProgress', 'Paused', 'Completed', 'Cancelled']:
                messages.error(request, "This run has already started (or finished) and can't be scrapped — use Pause or the normal completion flow instead.")
                return redirect('production_run_detail', pk=pk)

            reason = request.POST.get('reason', '').strip()
            if not reason:
                messages.error(request, "A reason is required to scrap a production run.")
                return redirect('production_run_detail', pk=pk)

            from .utils import release_production_run_allocations, release_order_after_run_stopped
            release_production_run_allocations(run, request.user)
            run.status = 'Cancelled'
            run.save()
            release_order_after_run_stopped(run, request.user)
            OrderTimeline.objects.create(production_run=run, action=f"Run scrapped before starting. Reason: {reason}", user=request.user)
            messages.success(request, f"Production Run {run.run_number} scrapped.")
            return redirect('readiness')

        elif action == 'abort_run':
            # Stop a run part-way: record what was really used and made, free the rest, and close it as Cancelled.
            if run.status not in ('InProgress', 'Paused'):
                messages.error(request, "Only a run that's In Progress or Paused can be aborted. Scrap it instead if it hasn't started.")
                return redirect('production_run_detail', pk=pk)
            reason = request.POST.get('reason', '').strip()
            if not reason:
                messages.error(request, "Give a reason for aborting the run.")
                return redirect('production_run_detail', pk=pk)
            used = {}
            for bom in bom_materials:
                qty, err = read_decimal(request.POST, f"used_qty_{bom['material'].id}", f"{bom['material'].name} used", min_value=0, required=False)
                allocated = Decimal(str(bom['allocated'])).quantize(Decimal('0.01'))
                if not err and qty and qty > allocated + Decimal('0.005'):
                    err = f"{bom['material'].name}: {qty} used is more than the {allocated} allocated. An aborted run can only use what was allocated to it."
                if err:
                    messages.error(request, err)
                    return redirect('production_run_detail', pk=pk)
                used[bom['material']] = (qty or Decimal('0'), Decimal(str(bom['needed'])))
            from .utils import finalize_production_run, release_order_after_run_stopped
            with transaction.atomic():
                # Unlike completion, nothing counts as used unless entered: an aborted run may have poured nothing.
                for material, (qty, expected) in used.items():
                    RunMaterialUsage.objects.update_or_create(production_run=run, material=material,
                                                              defaults={'expected_qty': expected, 'actual_qty': qty})
                made = run.yield_logs.aggregate(s=Sum('quantity'))['s'] or Decimal('0')
                run.actual_yield = made if made > 0 else None
                fg_batch = finalize_production_run(run, request.user, verb='aborted')
                run.status = 'Cancelled'
                run.save(update_fields=['status'])
                if run.sales_order:
                    run.sales_order.refresh_from_db()
                release_order_after_run_stopped(run, request.user)
                OrderTimeline.objects.create(
                    production_run=run, user=request.user,
                    action=f"Run aborted. Reason: {reason}. Unused materials released"
                           + (f"; {made} already made went to batch {fg_batch.batch_number}." if fg_batch else "; nothing had been made."))
                if run.sales_order:
                    OrderTimeline.objects.create(sales_order=run.sales_order, user=request.user,
                                                 action=f"Production Run {run.run_number} aborted. Reason: {reason}")
            messages.success(request, f"Run {run.run_number} aborted. Unused materials are free again.")
            return redirect('production_run_detail', pk=pk)

        elif action == 'pause_production':
            if run.status != 'InProgress':
                messages.error(request, "Only a run that's In Progress can be paused.")
                return redirect('production_run_detail', pk=pk)
            run.status = 'Paused'
            run.save()
            OrderTimeline.objects.create(production_run=run, action="Production paused.", user=request.user)
            messages.success(request, f"Production Run {run.run_number} paused.")
            return redirect('production_run_detail', pk=pk)

        elif action == 'resume_production':
            if run.status != 'Paused':
                messages.error(request, "This run isn't paused.")
                return redirect('production_run_detail', pk=pk)
            run.status = 'InProgress'
            run.save()
            OrderTimeline.objects.create(production_run=run, action="Production resumed.", user=request.user)
            messages.success(request, f"Production Run {run.run_number} resumed.")
            return redirect('production_run_detail', pk=pk)

        elif action == 'log_yield':
            from .models import ProductionRunYieldLog
            if run.status not in ['InProgress', 'Paused']:
                messages.error(request, "Yield can only be logged while a run is In Progress or Paused.")
                return redirect('production_run_detail', pk=pk)

            notes = request.POST.get('notes', '').strip()
            qty, err = read_decimal(request.POST, 'quantity', "Quantity", above=0)
            log_date, date_err = read_date(request.POST, 'log_date', "Log date")
            err = err or date_err
            if not err and log_date and log_date > timezone.localdate():
                err = "The log date can't be in the future."
            if err:
                messages.error(request, err)
                return redirect('production_run_detail', pk=pk)
            try:
                logged = run.yield_logs.aggregate(s=Sum('quantity'))['s'] or Decimal('0')
                over = logged + qty - run.expected_yield
                if over > 0 and not notes:
                    # Overrun is real sometimes, but it needs a reason on record
                    messages.error(request, f"That would log {logged + qty} of the {run.expected_yield} expected, {over} over. "
                                            "Give a reason in the notes to log more than expected.")
                else:
                    if over > 0:
                        OrderTimeline.objects.create(production_run=run, action=f"Yield logged over the expected {run.expected_yield} by {over}. Reason: {notes}", user=request.user)
                    log_date = log_date or timezone.localdate()
                    ProductionRunYieldLog.objects.create(
                        production_run=run, quantity=qty, log_date=log_date,
                        logged_by=request.user, notes=notes or None
                    )
                    OrderTimeline.objects.create(production_run=run, action=f"Logged {qty} units of yield on {log_date}.", user=request.user)
                    messages.success(request, f"Logged {qty} units.")
            except Exception as e:
                messages.error(request, f"Error logging yield: {e}")
            return redirect('production_run_detail', pk=pk)

        elif action == 'complete_production':
            if run.status != 'InProgress':
                messages.error(request, "Only a run that's In Progress can be completed.")
                return redirect('production_run_detail', pk=pk)

            # Anything poured beyond the allocation must be confirmed and traced to a batch or an unrecorded source with a reason.
            extra_sources, extra_error = parse_extra_material_sources(request, bom_materials, allocated_by_material)
            if extra_error:
                messages.error(request, extra_error)
                return redirect('production_run_detail', pk=pk)
            approver_id = request.POST.get('approver_id')
            if approver_id:
                problem = approver_problem(CustomUser.objects.filter(id=approver_id).first(), APPROVE_PRODUCTION_RUNS)
                if problem:
                    messages.error(request, problem)
                    return redirect('production_run_detail', pk=pk)
            _, yield_error = read_decimal(request.POST, 'actual_yield', "Actual yield", min_value=0, required=False)
            if yield_error:
                messages.error(request, yield_error)
                return redirect('production_run_detail', pk=pk)

            # 1. Process Material Usage and Variances
            has_high_variance = False
            with transaction.atomic():
                # Fill any final yield beyond what is logged as a yield-log entry so the log and progress bar reconcile.
                fg_raw = request.POST.get('actual_yield')
                if fg_raw:
                    from .models import ProductionRunYieldLog
                    logged = run.yield_logs.aggregate(s=Sum('quantity'))['s'] or Decimal('0')
                    shortfall = Decimal(fg_raw) - logged
                    if shortfall > 0:
                        ProductionRunYieldLog.objects.create(
                            production_run=run, quantity=shortfall, log_date=timezone.now().date(),
                            logged_by=request.user, notes="Auto-filled on completion",
                        )
                        OrderTimeline.objects.create(production_run=run, action=f"Auto-filled {shortfall} units of yield on completion.", user=request.user)
                for bom in bom_materials:
                    mat_id = bom['material'].id
                    actual = request.POST.get(f'actual_qty_{mat_id}')
                    if actual:
                        actual_dec = Decimal(actual)
                        expected_dec = Decimal(str(bom['needed']))
                        
                        usage, _ = RunMaterialUsage.objects.update_or_create(
                            production_run=run,
                            material=bom['material'],
                            defaults={'expected_qty': expected_dec, 'actual_qty': actual_dec}
                        )
                        
                        # Note: variance_pct is calculated in save()
                        if usage.variance_pct > Decimal('3.0') or usage.variance_pct < Decimal('-3.0'):
                            has_high_variance = True
                            
                # Record where the extra material came from (replaces any earlier submission).
                from .models import RunExtraMaterial
                run.extra_sources.all().delete()
                for src in extra_sources:
                    RunExtraMaterial.objects.create(
                        production_run=run, material=src['material'], batch=src['batch'],
                        quantity=src['qty'], reason=src['reason'], recorded_by=request.user,
                    )
                    unit = src['material'].unit_of_measure
                    if src['batch']:
                        note = f"Extra {src['qty']} {unit} of {src['material'].name} taken from batch {src['batch'].batch_number}."
                    else:
                        note = f"Extra {src['qty']} {unit} of {src['material'].name} used from a batch not in records. Reason: {src['reason']}"
                    OrderTimeline.objects.create(production_run=run, action=note, user=request.user)

                # 2. Check for supervisor sign-off if variance > 3%
                if has_high_variance:
                    approver_id = request.POST.get('approver_id')
                    if approver_id:
                        run.assigned_to_id = approver_id
                    follower_ids = request.POST.getlist('follower_ids')
                    run.signoff_reason = request.POST.get('variance_remark', 'High variance recorded.')
                    run.status = 'Pending Approval'
                    fg_qty = request.POST.get('actual_yield')
                    if fg_qty:
                        run.actual_yield = Decimal(fg_qty)
                    run.save()
                    
                    if follower_ids:
                        run.followers.set(follower_ids)
                        
                    if run.assigned_to:
                        Notification.objects.create(
                            user=run.assigned_to,
                            message=f"Variance Approval Required: Production Run {run.run_number} exceeded 3% material variance. Review required.",
                            link=reverse('approvals_inbox'),
                        )
                    for follower_id in follower_ids:
                        Notification.objects.create(
                            user_id=follower_id,
                            message=f"Following: Production Run {run.run_number} is pending variance approval.",
                            link=f"/operations/manufacture/run/{run.id}/",
                        )
                    
                    OrderTimeline.objects.create(
                        production_run=run,
                        action=f"Production Run {run.run_number} submitted for Variance Approval.",
                        user=request.user
                    )
                        
                    messages.info(request, f"Production Run {run.run_number} submitted for Variance Approval.")
                    return redirect('production_run_detail', pk=pk)
            
                # 3. Finalize Run: create FG batch, allocate it to the linked SO, advance SO status
                fg_qty = request.POST.get('actual_yield')
                if fg_qty:
                    run.actual_yield = Decimal(fg_qty)

                from .utils import finalize_production_run
                fg_batch = finalize_production_run(run, request.user)

                OrderTimeline.objects.create(
                    production_run=run,
                    action=f"Production Run {run.run_number} completed."
                           + (f" FG batch {fg_batch.batch_number} created." if fg_batch else ""),
                    user=request.user
                )

            messages.success(request, f"Production Run {run.run_number} completed successfully!")
            return redirect('production_run_detail', pk=pk)
            
        elif action == 'add_follower':
            user_id = request.POST.get('user_id')
            if user_id:
                user_obj = get_object_or_404(CustomUser, id=user_id)
                if user_obj not in run.followers.all():
                    run.followers.add(user_obj)
                    OrderTimeline.objects.create(production_run=run, action=f"Added {user_obj.get_full_name() or user_obj.username} as a follower.", user=request.user)
                    notify_added_follower(request.user, user_obj, f"Production Run {run.run_number}", reverse('production_run_detail', args=[run.pk]))
                    messages.success(request, f"Added {user_obj.get_full_name() or user_obj.username} as a follower.")
            return redirect('production_run_detail', pk=pk)

        elif action == 'remove_follower':
            user_id = request.POST.get('user_id')
            if user_id:
                user_obj = get_object_or_404(CustomUser, id=user_id)
                if user_obj in run.followers.all():
                    run.followers.remove(user_obj)
                    OrderTimeline.objects.create(production_run=run, action=f"Removed {user_obj.get_full_name() or user_obj.username} from followers.", user=request.user)
                    messages.success(request, f"Removed {user_obj.get_full_name() or user_obj.username} from followers.")
            return redirect('production_run_detail', pk=pk)
            
        elif action == 'remove_allocation':
            # Allocation is add-only except that one line can come off before the run starts, if its transfer is still a Draft.
            from .utils import remove_run_allocation
            alloc = StockAllocation.objects.filter(id=request.POST.get('alloc_id'), production_run=run).select_related('batch__material', 'shipment').first()
            if not alloc:
                messages.error(request, "That allocation line no longer exists.")
            else:
                problem = remove_run_allocation(run, alloc, request.user)
                if problem:
                    messages.error(request, problem)
                else:
                    messages.success(request, "Allocation line removed.")
            return redirect('production_run_detail', pk=pk)

        elif action == 'cancel_allocation':
            # To redo an allocation before the run starts. Only additions are allowed otherwise.
            from .utils import cancel_run_allocation
            problem = cancel_run_allocation(run, request.user)
            if problem:
                messages.error(request, problem)
            else:
                messages.success(request, f"Allocation for {run.run_number} cancelled. Allocate again to continue.")
            return redirect('production_run_detail', pk=pk)
            
    material_usage_rows = []
    total_material_cost = Decimal('0')
    for usage in run.material_usages.select_related('material').all():
        consumptions = run.consumptions.filter(
            consumed_batch__material=usage.material
        ).select_related('consumed_batch')
        row_cost = sum((c.quantity_used * c.consumed_batch.material.cost_per_unit for c in consumptions), Decimal('0'))
        # Extra material from a batch that isn't in the records is costed at the standard rate
        unrecorded = list(run.extra_sources.filter(material=usage.material, batch__isnull=True))
        row_cost += sum((u.quantity * usage.material.cost_per_unit for u in unrecorded), Decimal('0'))
        total_material_cost += row_cost
        material_usage_rows.append({'usage': usage, 'consumptions': consumptions, 'cost': row_cost, 'unrecorded': unrecorded})

    yield_efficiency = None
    if run.actual_yield is not None and run.expected_yield:
        yield_efficiency = (Decimal(str(run.actual_yield)) / Decimal(str(run.expected_yield))) * 100

    yield_logs = run.yield_logs.all()
    yield_logs_sum = sum((y.quantity for y in yield_logs), Decimal('0'))
    if run.status == 'Completed' and run.actual_yield and yield_logs_sum < run.actual_yield:
        # completed in one go (no interim yield logs) - the recorded actual yield is the output
        yield_logs_sum = run.actual_yield
    yield_progress_pct = None
    if run.expected_yield:
        yield_progress_pct = min(100, float((yield_logs_sum / Decimal(str(run.expected_yield))) * 100))
    yield_remaining = max(Decimal(str(run.expected_yield or 0)) - yield_logs_sum, Decimal('0'))

    priority = None
    if run.sales_order and run.sales_order.fulfillment_deadline and run.status not in ['Completed', 'Cancelled']:
        days_left = (run.sales_order.fulfillment_deadline - timezone.now().date()).days
        if days_left < 0:
            priority = 'Overdue'
        elif days_left <= 3:
            priority = 'Due Soon'
        else:
            priority = 'On Track'

    materials_short = [b for b in bom_materials if b['allocated'] < b['needed'] - 0.005]

    return render(request, 'production_run_detail.html', {
        'run': run,
        'materials_short': materials_short,
        'archived_materials': archived_recipe_materials(run.target_product),
        'can_decide_approval': may_decide_run_approval(request.user, run),
        'linked_shipments': linked_shipments,
        'all_shipments_arrived': all_shipments_arrived,
        'pending_shipments_count': pending_shipments_count,
        'bom_materials': bom_materials,
        'managers': approvers(APPROVE_PRODUCTION_RUNS),
        'all_users': CustomUser.objects.prefetch_related('groups'),
        'timeline_events': run.timeline.all().order_by('-timestamp'),
        'allocations': run.allocations.select_related('batch__material', 'batch__warehouse', 'shipment'),
        'consumptions': run.consumptions.select_related('consumed_batch__material', 'consumed_batch__warehouse'),
        'extra_sources': run.extra_sources.select_related('material', 'batch', 'recorded_by'),
        'material_usage_rows': material_usage_rows,
        'total_material_cost': total_material_cost,
        'produced_batches': run.produced_batches.all(),
        'yield_efficiency': yield_efficiency,
        'yield_logs': yield_logs,
        'yield_logs_sum': yield_logs_sum,
        'yield_progress_pct': yield_progress_pct,
        'yield_remaining': yield_remaining,
        'priority': priority,
    })

@login_required
def shipment_pick_list_view(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    
    return render(request, 'shipment_pick_list.html', {
        'shipment': shipment,
        'items': shipment.items.all().order_by('batch__location')
    })

def batch_public_info_view(request, batch_number):
    """ Public tracking page for a batch. No login required. """
    batch = get_object_or_404(Batch.objects.select_related(
        'material', 'product', 'purchase_order', 'produced_in', 'produced_in__manufacturing_plant'
    ), batch_number=batch_number)
    return render(request, 'batch_public_info.html', {'batch': batch})

@login_required
def batch_print_label_view(request, batch_number):
    """ View dedicated for printing the QR label """
    batch = get_object_or_404(Batch.objects.select_related(
        'material', 'product'
    ), batch_number=batch_number)
    return render(request, 'batch_print_label.html', {'batch': batch})
