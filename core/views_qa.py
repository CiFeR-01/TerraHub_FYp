"""QA & Spoilage Control pages: the QA dashboard (requests, expiry, holds, approvals, history), "Send to QA",
and the Spoilage & Write-offs report. The rules live in core/qa.py."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Prefetch, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from . import analytics, qa
from .models import Batch, QAApproval, QAEvent, QARequest, QA_REASON_CHOICES, StockAllocation, Warehouse
from .permissions import (APPROVE_QA_WRITEOFFS, DISPOSE_STOCK, FACILITY_REFUSED, PERFORM_QA, REQUEST_QA,
                          action_denied, limited_warehouse_ids, may_use_warehouse)
from .settings_store import get_setting

TABS = ('requests', 'expiring', 'at_risk', 'quarantined', 'approvals', 'history')
# Reasons offered per action (all reasons are valid everywhere; these just order the dropdowns sensibly).
QA_REASONS = QA_REASON_CHOICES


def _back(request, default):
    nxt = request.POST.get('next') or ''
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return default


def _open_request(request, batch):
    """The QA request this action answers, if one was posted and is still open for this batch."""
    rid = request.POST.get('request_id')
    if not rid:
        return None
    return QARequest.objects.filter(pk=rid, batch=batch, status__in=('Open', 'In Review')).first()


def _resolve(req, user, outcome, note=''):
    if req is not None:
        qa.resolve_request(req, user, outcome, note)


def _handle_post(request):
    """Runs one QA action. Returns the URL to go back to."""
    action = request.POST.get('action')
    default = reverse('qa_dashboard') + (f"?tab={request.POST.get('tab')}" if request.POST.get('tab') in TABS else '')
    back = _back(request, default)
    user = request.user
    reason = request.POST.get('reason', '')
    note = (request.POST.get('note') or '').strip()

    if action in ('approve_qa', 'reject_qa'):
        approval = get_object_or_404(QAApproval, pk=request.POST.get('approval_id'))
        if not may_use_warehouse(user, approval.batch.warehouse_id):
            messages.error(request, FACILITY_REFUSED)
            return back
        try:
            qa.decide_approval(approval, user, action == 'approve_qa', (request.POST.get('comment') or '').strip())
        except qa.QAError as e:
            messages.error(request, str(e))
            return back
        verb = 'approved' if action == 'approve_qa' else 'rejected'
        messages.success(request, f"{approval.get_kind_display()} for batch {approval.batch.batch_number} {verb}.")
        return back

    if action in ('assign_request', 'resolve_request'):
        req = get_object_or_404(QARequest, pk=request.POST.get('request_id'))
        if not may_use_warehouse(user, req.batch.warehouse_id):
            messages.error(request, FACILITY_REFUSED)
            return back
        try:
            if action == 'assign_request':
                qa.assign_request(req, user)
                messages.success(request, f"You're reviewing the QA request for batch {req.batch.batch_number}.")
            else:
                outcome = request.POST.get('outcome', '')
                if outcome not in ('no_issue', 'held', 'cancelled'):
                    raise qa.QAError("Close the request with No issue found, Kept on hold or Cancelled, "
                                     "or use the batch's QA actions to release, write off or extend it.")
                qa.resolve_request(req, user, outcome, note)
                messages.success(request, f"QA request for batch {req.batch.batch_number} closed: {req.get_outcome_display()}.")
        except qa.QAError as e:
            messages.error(request, str(e))
        return back

    batch = get_object_or_404(Batch.objects.select_related('material', 'product', 'warehouse', 'purchase_order'),
                              id=request.POST.get('batch_id'))
    if not may_use_warehouse(user, batch.warehouse_id):
        messages.error(request, FACILITY_REFUSED)
        return back
    req = _open_request(request, batch)
    try:
        if action == 'quarantine':
            qty = qa.read_quantity(request.POST.get('quantity'), batch)
            held = qa.hold_batch(batch, user, reason, note, quantity=qty, request=req)
            _resolve(req, user, 'held', note)
            messages.warning(request, f"Batch {held.batch_number} put on QA hold.")
        elif action in ('release_quarantine', 'inspect'):
            result = request.POST.get('result', 'pass' if action == 'release_quarantine' else '')
            moisture = None
            if (request.POST.get('moisture_pct') or '').strip():
                try:
                    moisture = Decimal(request.POST['moisture_pct'].strip())
                except InvalidOperation:
                    raise qa.QAError("Moisture must be a number.")
            qa.inspect_batch(batch, user, result, request.POST.getlist('checks'), moisture, note, request=req)
            batch.refresh_from_db()
            if result == 'pass':
                _resolve(req, user, 'released' if batch.status == 'Active' else 'no_issue', note)
                messages.success(request, f"Batch {batch.batch_number} passed inspection"
                                          f"{' and is back in Active stock' if batch.status == 'Active' else ''}.")
            else:
                messages.warning(request, f"Batch {batch.batch_number} failed inspection and is on hold. "
                                          "Write it off if it can't be used.")
        elif action == 'spoil_dispose':
            qty = qa.read_quantity(request.POST.get('quantity'), batch)
            state, obj = qa.dispose_batch(batch, user, reason, note, quantity=qty, request=req)
            if state == 'pending':
                messages.warning(request, f"Writing off batch {obj.batch.batch_number} (RM {obj.value_rm:,.2f}) needs approval. "
                                          "It's on hold and waiting in the Action Center.")
            else:
                _resolve(req, user, 'disposed', note)
                messages.error(request, f"Batch {obj.batch_number} written off ({obj.quantity}).")
        elif action == 'undo_write_off':
            qa.undo_write_off(batch, user, note)
            messages.success(request, f"Write-off of batch {batch.batch_number} undone. "
                                      f"It's {'on hold because it has expired' if batch.status == 'Quarantined' else 'back in Active stock'}.")
        elif action == 'extend_expiry':
            state, obj = qa.extend_expiry(batch, user, request.POST.get('extra_days', ''), reason, note, request=req)
            if state == 'pending':
                messages.warning(request, f"Extending batch {batch.batch_number} by {obj.days} days goes past the "
                                          f"{qa.extension_cap_days(batch)}-day limit, so it's waiting for approval in the Action Center.")
            else:
                _resolve(req, user, 'extended', note)
                messages.success(request, f"Expiry for batch {obj.batch_number} extended to {obj.expiry_date}.")
        elif action == 'hold_downstream':
            held = qa.hold_downstream(batch, user, note)
            messages.warning(request, f"{len(held)} finished batch(es) made from {batch.batch_number} put on hold."
                             if held else f"No Active finished batches were made from {batch.batch_number}.")
        else:
            messages.error(request, "That action isn't available on this page.")
    except qa.QAError as e:
        messages.error(request, str(e))
    return back


@login_required
def qa_dashboard_view(request):
    denied = action_denied(request, 'qa_dashboard')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        return redirect(_handle_post(request))

    qa.daily_sweep()

    today = timezone.localdate()
    window = get_setting('qa_near_expiry_days')
    limited = limited_warehouse_ids(request.user)
    warehouses = Warehouse.objects.order_by('name')
    if limited is not None:
        warehouses = warehouses.filter(id__in=limited)
    wh = request.GET.get('wh', '')
    scope = Q()
    if wh.isdigit():
        scope = Q(warehouse_id=int(wh))
    elif limited is not None:
        scope = Q(warehouse_id__in=limited)
    wh_ids = [int(wh)] if wh.isdigit() else (sorted(limited) if limited is not None else None)

    base = Batch.objects.filter(scope).select_related('material', 'product', 'warehouse', 'purchase_order')
    reserved = Prefetch('allocations', queryset=StockAllocation.objects.select_related('sales_order', 'production_run', 'shipment'))

    near_expiry = list(base.filter(status='Active', expiry_date__isnull=False, expiry_date__lte=today + timedelta(days=window))
                       .prefetch_related(reserved).order_by('expiry_date'))
    for b in near_expiry:
        b.days_remaining = (b.expiry_date - today).days
        b.value = qa.batch_value(b)

    quarantined = list(
        # Stock waiting on a QA request (new finished goods, checked-on-arrival receipts) is under QA Check instead
        base.filter(status='Quarantined').exclude(qa_requests__status__in=('Open', 'In Review'))
        .prefetch_related(reserved).annotate(
            held_at=Max('qa_events__created_at', filter=Q(qa_events__action__in=qa.HOLD_ACTIONS)),
        ).order_by('expiry_date'))
    last_hold = {}
    for ev in (QAEvent.objects.filter(batch__in=[b.id for b in quarantined], action__in=qa.HOLD_ACTIONS)
               .select_related('user').order_by('created_at', 'id')):
        last_hold[ev.batch_id] = ev
    for b in quarantined:
        b.value = qa.batch_value(b)
        b.days_held = (timezone.now() - b.held_at).days if b.held_at else None
        b.hold_event = last_hold.get(b.id)
        b.expired = qa.is_expired(b, today)

    spoiled = Batch.objects.filter(scope, status='Spoiled')
    requests_open = list(QARequest.objects.filter(status__in=('Open', 'In Review'))
                         .filter(Q(batch__warehouse_id__in=wh_ids) if wh_ids is not None else Q())
                         .select_related('batch', 'batch__material', 'batch__product', 'batch__warehouse',
                                         'requested_by', 'assigned_to')
                         .order_by('created_at'))
    priority_rank = {'Urgent': 0, 'High': 1, 'Normal': 2, 'Low': 3}
    requests_open.sort(key=lambda r: (priority_rank.get(r.priority, 9), r.created_at))
    approvals = list(QAApproval.objects.filter(status='Pending')
                     .filter(Q(batch__warehouse_id__in=wh_ids) if wh_ids is not None else Q())
                     .select_related('batch', 'batch__material', 'batch__product', 'requested_by'))
    at_risk = analytics.expiry_risk(horizon_days=max(window, 90), warehouse_ids=wh_ids, today=today)
    history = Paginator(QAEvent.objects.filter(Q(batch__warehouse_id__in=wh_ids) if wh_ids is not None else Q())
                        .exclude(action__in=('expiry_warning', 'hold_alert'))
                        .select_related('batch', 'user', 'request'), 25).get_page(request.GET.get('page'))

    month_ago = timezone.now() - timedelta(days=30)
    written_off = (QAEvent.objects.filter(action='disposed', created_at__gte=month_ago)
                   .filter(Q(batch__warehouse_id__in=wh_ids) if wh_ids is not None else Q())
                   .aggregate(v=Sum('value_rm'))['v'] or Decimal('0'))
    expired_count = sum(1 for b in near_expiry if b.days_remaining < 0) + sum(1 for b in quarantined if b.expired)
    kpis = {
        'open_requests': len(requests_open),
        'urgent_requests': sum(1 for r in requests_open if r.priority in ('Urgent', 'High')),
        'near_expiry': len(near_expiry),
        'expired': expired_count,
        'on_hold': len(quarantined),
        'on_hold_value': sum((b.value for b in quarantined), Decimal('0')),
        'at_risk_value': sum((r['value_at_risk'] for r in at_risk), Decimal('0')),
        'written_off_30d': written_off,
        'approvals': len(approvals),
    }

    tab = request.GET.get('tab')
    if tab not in TABS:
        tab = 'requests' if requests_open else 'expiring'

    tab_list = [
        ('requests', 'QA Check', len(requests_open)),
        ('expiring', 'Expiring', len(near_expiry)),
        ('at_risk', 'At risk', len(at_risk)),
        ('quarantined', 'On hold', len(quarantined)),
        ('approvals', 'Approvals', len(approvals)),
        ('history', 'History', 0),
    ]

    return render(request, 'qa_dashboard.html', {
        'tab': tab,
        'tab_list': tab_list,
        'near_expiry': near_expiry,
        'quarantined': quarantined,
        'spoiled': spoiled,
        'requests_open': requests_open,
        'approvals': approvals,
        'at_risk': at_risk,
        'history': history,
        'kpis': kpis,
        'window': window,
        'warning_days': get_setting('qa_expiry_warning_days'),
        'warehouses': warehouses,
        'wh': wh,
        'qa_reasons': QA_REASONS,
        'qa_checks': qa.INSPECTION_CHECKS,
        'can_qa': request.user.has_perm(PERFORM_QA),
        'can_dispose': request.user.has_perm(DISPOSE_STOCK),
        'can_approve_qa': request.user.has_perm(APPROVE_QA_WRITEOFFS),
        'today': today,
    })


@login_required
def qa_request_create(request):
    """"Send to QA" from any page: raises a QA request (optionally holding the stock right away)."""
    if request.method != 'POST':
        return redirect('qa_dashboard')
    back = _back(request, reverse('qa_dashboard'))
    if not request.user.has_perm(REQUEST_QA):
        messages.error(request, "You don't have permission to send batches to QA. Ask a Manager or Admin if you need it.")
        return redirect(back)
    batch = get_object_or_404(Batch.objects.select_related('material', 'product', 'warehouse'), id=request.POST.get('batch_id'))
    if not may_use_warehouse(request.user, batch.warehouse_id):
        messages.error(request, FACILITY_REFUSED)
        return redirect(back)
    try:
        qty = qa.read_quantity(request.POST.get('quantity'), batch)
        req = qa.create_request(batch, request.user, request.POST.get('reason', ''),
                                (request.POST.get('note') or '').strip(), quantity=qty,
                                priority=request.POST.get('priority', 'Normal'),
                                source=(request.POST.get('source') or '').strip(),
                                hold=request.POST.get('hold') == '1')
    except qa.QAError as e:
        messages.error(request, str(e))
        return redirect(back)
    held = " It's on hold until QA looks at it." if req.batch.status == 'Quarantined' and req.hold_requested else ''
    messages.success(request, f"Batch {req.batch.batch_number} sent to QA.{held}")
    return redirect(back)
