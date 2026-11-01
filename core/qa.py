"""QA and spoilage control: holds, inspections, releases, write-offs, returns, shelf-life extensions, QA requests,
approvals, recall tracing and the daily expiry sweep. Views call these; every action records a QAEvent with a reason."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Max, Q
from django.urls import reverse
from django.utils import timezone

from .models import (Batch, Notification, OrderTimeline, QAApproval, QAEvent, QAInspection, QARequest,
                     QA_REASON_CHOICES, RegistryLog, StockAllocation)
from .permissions import (APPROVE_QA_WRITEOFFS, PERFORM_QA, may_use_warehouse, users_with_perm)
from .settings_store import get_setting

OPEN_STATUSES = ('Active', 'Quarantined')
REASONS = dict(QA_REASON_CHOICES)
HOLD_ACTIONS = ('quarantined', 'auto_hold')
# Checklist shown on the inspection form; the inspector ticks each one that is fine.
INSPECTION_CHECKS = (
    'Visual condition normal',
    'Packaging intact and labelled',
    'No pests or foreign matter',
    'Odour and colour normal',
    'Stored at the right conditions',
)


class QAError(Exception):
    """A QA action that can't go ahead; the message is shown to the user as is."""


# Small helpers

def qty_with_unit(batch):
    """The batch quantity with its unit, e.g. "10 kg" or "1,250 pcs"."""
    from .templatetags.quantity_extras import qty_display
    item = batch.material or batch.product
    return f"{qty_display(batch.quantity)} {item.unit_of_measure}" if item else qty_display(batch.quantity)


def item_name(batch):
    if batch.material_id:
        return batch.material.name
    if batch.product_id:
        return batch.product.name
    return batch.batch_number


def unit_value(batch):
    """RM per unit: material cost, or product selling price."""
    if batch.material_id:
        return Decimal(str(batch.material.cost_per_unit or 0))
    if batch.product_id:
        return Decimal(str(batch.product.price_per_unit or 0))
    return Decimal('0')


def batch_value(batch, quantity=None):
    qty = batch.quantity if quantity is None else Decimal(str(quantity))
    return (unit_value(batch) * qty).quantize(Decimal('0.01'))


def is_expired(batch, today=None):
    today = today or timezone.localdate()
    return bool(batch.expiry_date and batch.expiry_date < today)


def reason_label(code):
    return REASONS.get(code, code or '')


def check_reason(reason):
    if reason not in REASONS:
        raise QAError("Pick a reason.")
    return reason


def read_quantity(raw, batch, label="Quantity", required=False):
    """None (whole batch) for a blank value, else a Decimal between 0 and the batch's quantity.
    With `required`, a blank value is refused instead of meaning the whole batch."""
    raw = (raw or '').strip() if isinstance(raw, str) else raw
    if raw in (None, ''):
        if required:
            raise QAError(f"Enter the {label.lower()} to write off (the batch has {batch.quantity}).")
        return None
    try:
        qty = Decimal(str(raw))
    except InvalidOperation:
        raise QAError(f"{label} must be a number.")
    if not qty.is_finite() or qty <= 0:
        raise QAError(f"{label} must be more than 0.")
    if qty > batch.quantity:
        raise QAError(f"{label} can't be more than the {batch.quantity} in batch {batch.batch_number}.")
    return None if qty == batch.quantity else qty


def log_event(batch, action, user, reason='', note='', quantity=None, value=None, request=None):
    return QAEvent.objects.create(batch=batch, action=action, user=user, reason=reason or '', note=note or '',
                                  quantity=quantity, value_rm=value, request=request)


def qa_staff(warehouse_id=None):
    """Users who can act on QA work for this facility."""
    return [u for u in users_with_perm(PERFORM_QA) if warehouse_id is None or may_use_warehouse(u, warehouse_id)]


def notify(users, message, link, exclude=None):
    for u in dict.fromkeys(users):
        if exclude is not None and u.pk == exclude.pk:
            continue
        Notification.objects.create(user=u, message=message, link=link)


def _dashboard_link(tab=None):
    url = reverse('qa_dashboard')
    return f"{url}?tab={tab}" if tab else url


def _tell_reservations(batch, user, text):
    """Adds a timeline note to every order/run holding stock in `batch`."""
    for a in batch.allocations.select_related('sales_order', 'production_run'):
        if a.sales_order_id:
            OrderTimeline.objects.create(sales_order=a.sales_order, action=text, user=user)
        elif a.production_run_id:
            OrderTimeline.objects.create(production_run=a.production_run, action=text, user=user)


# Splitting a lot

def split_batch(batch, quantity, user, why):
    """Moves `quantity` of `batch` into a new child batch (same item, dates, place and rent rate) and returns it.
    Reservations on the parent are cut back first so they still fit what it keeps."""
    from .utils import trim_batch_reservations
    quantity = Decimal(str(quantity))
    keep = batch.quantity - quantity
    problem = trim_batch_reservations(batch, keep, user, f"part of the batch split off ({why})")
    if problem:
        raise QAError(problem)
    n = batch.child_batches.count() + 1
    number = f"{batch.batch_number}-S{n}"
    while Batch.objects.filter(batch_number=number).exists():
        n += 1
        number = f"{batch.batch_number}-S{n}"
    child = Batch.objects.create(
        batch_number=number, status=batch.status, material=batch.material, product=batch.product,
        quantity=quantity, purchase_order=batch.purchase_order, produced_in=batch.produced_in,
        manufacturing_date=batch.manufacturing_date, expiry_date=batch.expiry_date,
        original_expiry_date=batch.original_expiry_date, extension_days_total=batch.extension_days_total,
        warehouse=batch.warehouse, location=batch.location, rental_rate_per_mt=batch.rental_rate_per_mt,
        term_start_date=batch.term_start_date,
        parent_batch=batch,
    )
    batch.refresh_from_db(fields=['allocated_quantity'])
    batch.quantity = keep
    batch.save(update_fields=['quantity'])
    log_event(batch, 'split', user, note=f"{quantity} moved to new batch {child.batch_number}: {why}", quantity=quantity)
    log_event(child, 'split', user, note=f"Split off batch {batch.batch_number}: {why}", quantity=quantity)
    return child


def _portion(batch, quantity, user, why):
    """The batch itself for the whole lot (quantity None), else a child batch holding `quantity`."""
    if quantity is None or quantity >= batch.quantity:
        return batch
    return split_batch(batch, quantity, user, why)


# Holds, inspections and releases

def hold_batch(batch, user, reason, note='', quantity=None, request=None, auto=False):
    """Puts a batch (or `quantity` of it) on QA hold. Reservations stay, but the stock can't ship or go into production.
    An automatic hold for expiry also releases the reservations, since the stock won't come back by itself."""
    from .utils import trim_batch_reservations
    check_reason(reason)
    if batch.status != 'Active':
        raise QAError(f"Batch {batch.batch_number} is {batch.get_status_display()}, so it can't be put on hold.")
    with transaction.atomic():
        target = _portion(batch, quantity, user, f"held for QA: {reason_label(reason)}")
        target.status = 'Quarantined'
        target.save(update_fields=['status'])
        log_event(target, 'auto_hold' if auto else 'quarantined', user, reason, note, target.quantity,
                  batch_value(target), request)
        if auto:
            problem = trim_batch_reservations(target, 0, user, "batch expired and was put on QA hold")
            if problem:  # stock already on a truck keeps its reservation; dispatch refuses expired stock anyway
                log_event(target, 'auto_hold', user, reason, f"Reservations kept: {problem}")
        else:
            _tell_reservations(target, user, f"Batch {target.batch_number} was put on QA hold ({reason_label(reason)}). "
                                             f"It can't ship or go into production until QA releases it.")
    return target


def _last_hold_user(batch):
    ev = batch.qa_events.filter(action__in=HOLD_ACTIONS).order_by('-created_at', '-id').first()
    return ev.user if ev else None


def release_batch(batch, user, note='', request=None):
    """QA release: the batch becomes Active again (and a finished batch held for its order is reserved now)."""
    from .utils import allocate_finished_batch_to_order
    from .views import clear_approval_notifications
    if batch.status != 'Quarantined':
        raise QAError(f"Batch {batch.batch_number} is {batch.get_status_display()}, not on hold, so it can't be released.")
    if is_expired(batch):
        raise QAError(f"Batch {batch.batch_number} expired on {batch.expiry_date}. Extend its shelf life or write it off instead.")
    if get_setting('qa_release_needs_second_person'):
        held_by = _last_hold_user(batch)
        if held_by is not None and held_by.pk == user.pk:
            raise QAError("You put this batch on hold, so someone else has to release it.")
    with transaction.atomic():
        batch.status = 'Active'
        batch.save(update_fields=['status'])
        clear_approval_notifications(batch.batch_number)
        log_event(batch, 'released', user, note=note, quantity=batch.quantity, request=request)
        # Released from On hold or the batch page: close its QA Check request too
        for other in batch.qa_requests.filter(status__in=('Open', 'In Review')).exclude(pk=getattr(request, 'pk', None)):
            resolve_request(other, user, 'released', note)
        run = batch.produced_in
        if run:
            OrderTimeline.objects.create(production_run=run, action=f"FG batch {batch.batch_number} released by QA.", user=user)
            if run.sales_order and not batch.allocations.exists():
                allocate_finished_batch_to_order(run, batch, user, event=f"FG batch {batch.batch_number} (Run {run.run_number}) released by QA.")
        _tell_reservations(batch, user, f"Batch {batch.batch_number} was released by QA and can be used again.")
    return batch


def inspect_batch(batch, user, result, checks=(), moisture=None, notes='', request=None):
    """Records an inspection. Pass releases a held batch; fail keeps it on hold (or puts an Active batch on hold)."""
    if result not in ('pass', 'fail'):
        raise QAError("Choose Pass or Fail for the inspection.")
    if batch.status not in OPEN_STATUSES:
        raise QAError(f"Batch {batch.batch_number} is {batch.get_status_display()}, so it can't be inspected.")
    if result == 'pass' and batch.status == 'Quarantined':
        # Check the release rules before recording anything
        if is_expired(batch):
            raise QAError(f"Batch {batch.batch_number} expired on {batch.expiry_date}. Extend its shelf life or write it off instead.")
        if get_setting('qa_release_needs_second_person'):
            held_by = _last_hold_user(batch)
            if held_by is not None and held_by.pk == user.pk:
                raise QAError("You put this batch on hold, so someone else has to release it.")
    ticked = set(checks)
    # No ticks at all = a release without a checklist (older forms); record it without check rows.
    check_rows = [{'check': c, 'ok': c in ticked} for c in INSPECTION_CHECKS] if ticked else []
    with transaction.atomic():
        insp = QAInspection.objects.create(batch=batch, request=request, result=result, checks=check_rows,
                                           moisture_pct=moisture, notes=notes or '', inspector=user)
        failed = [c['check'] for c in check_rows if not c['ok']]
        summary = "Passed" if result == 'pass' else "Failed"
        if failed:
            summary += f" (not ok: {', '.join(failed)})"
        if moisture is not None:
            summary += f"; moisture {moisture}%"
        if notes:
            summary += f". {notes}"
        log_event(batch, 'inspected', user, 'failed_inspection' if result == 'fail' else 'routine_check', summary,
                  batch.quantity, request=request)
        if result == 'pass' and batch.status == 'Quarantined':
            release_batch(batch, user, note="Released after a passed inspection.", request=request)
        elif result == 'fail' and batch.status == 'Active':
            hold_batch(batch, user, 'failed_inspection', notes, request=request)
    return insp


# Write-offs and returns

def _needs_approval(user, value):
    if user.has_perm(APPROVE_QA_WRITEOFFS):
        return False
    return value >= Decimal(str(get_setting('qa_writeoff_approval_rm')))


def _ask_approval(batch, user, kind, reason, note, request, quantity=None, days=None, value=None):
    existing = batch.qa_approvals.filter(status='Pending').first()
    if existing:
        raise QAError(f"Batch {batch.batch_number} already has a {existing.get_kind_display().lower()} waiting for approval.")
    appr = QAApproval.objects.create(batch=batch, kind=kind, quantity=quantity, days=days, reason=reason, note=note or '',
                                     value_rm=value, request=request, requested_by=user)
    what = (f"write off {quantity or batch.quantity} of batch {batch.batch_number} (RM {value:,.2f})" if kind == 'dispose'
            else f"extend batch {batch.batch_number} by {days} days")
    log_event(batch, 'approval_requested', user, reason, f"Asked for approval to {what}. {note}".strip(), quantity, value, request)
    notify(users_with_perm(APPROVE_QA_WRITEOFFS), f"QA approval needed: {what} ({reason_label(reason)}).",
           reverse('approvals_inbox'), exclude=user)
    return appr


def dispose_batch(batch, user, reason, note='', quantity=None, request=None, approved=False):
    """Writes off a batch (or `quantity` of it). Returns ('done', batch) or ('pending', QAApproval) when the value
    needs approval first; while it waits the stock is held."""
    from .utils import trim_batch_reservations
    check_reason(reason)
    if batch.status not in OPEN_STATUSES:
        raise QAError(f"Batch {batch.batch_number} is {batch.get_status_display()}, so it can't be written off.")
    qty = batch.quantity if quantity is None else quantity
    free = batch.quantity - batch.allocated_quantity
    if qty > free:
        # Reserved stock is never written off: whoever holds the reservation releases it first
        holders = sorted({a.sales_order.so_number if a.sales_order_id else a.production_run.run_number if a.production_run_id
                          else a.shipment.tracking_number if a.shipment_id else 'an order'
                          for a in batch.allocations.select_related('sales_order', 'production_run', 'shipment')})
        raise QAError(f"{batch.allocated_quantity} of batch {batch.batch_number} is reserved for {', '.join(holders)}. "
                      f"Release that reservation first" + (f", or write off no more than {free}." if free > 0 else "."))
    value = batch_value(batch, qty)
    with transaction.atomic():
        if not approved and _needs_approval(user, value):
            target = batch
            if batch.status == 'Active':
                target = hold_batch(batch, user, reason, f"Held while a write-off waits for approval. {note}".strip(),
                                    quantity=quantity, request=request)
            elif quantity is not None:
                target = _portion(batch, quantity, user, f"write-off waiting for approval: {reason_label(reason)}")
            return 'pending', _ask_approval(target, user, 'dispose', reason, note, request,
                                            quantity=target.quantity, value=value)
        target = _portion(batch, quantity, user, f"written off: {reason_label(reason)}")
        problem = trim_batch_reservations(target, 0, user, f"batch written off ({reason_label(reason)})")
        if problem:
            raise QAError(problem)
        target.status = 'Spoiled'
        target.closed_date = timezone.localdate()
        target.save(update_fields=['status', 'closed_date'])
        RegistryLog.objects.create(
            action_type='Spoiled_Disposal',
            item_name=f"Batch {target.batch_number} Disposed ({reason_label(reason)})",
            material=target.material, quantity_changed=target.quantity, warehouse=target.warehouse, user=user,
        )
        if reason == 'returned_to_supplier' and target.purchase_order_id:
            OrderTimeline.objects.create(
                purchase_order=target.purchase_order, user=user, notes=note or None,
                action=f"Wrote off {qty_with_unit(target)} of {item_name(target)} (batch {target.batch_number}): returned to supplier.",
            )
        log_event(target, 'disposed', user, reason, note, target.quantity, batch_value(target), request)
    return 'done', target


def last_write_off(batch):
    """The write-off event that closed `batch`, or None."""
    if batch.status != 'Spoiled':
        return None
    return batch.qa_events.filter(action='disposed').order_by('-created_at', '-id').first()


def undo_deadline(batch):
    """Last day the batch's write-off can still be undone, or None if it can't be."""
    ev = last_write_off(batch)
    days = int(get_setting('qa_writeoff_undo_days') or 0)
    if ev is None or days <= 0:
        return None
    return timezone.localtime(ev.created_at).date() + timedelta(days=days)


def undo_write_off(batch, user, note=''):
    """Reverses a write-off made by mistake: the batch comes back (on hold if it has expired), the ledger gets a
    matching + row and the write-off drops out of the spoilage figures. Reservations released at write-off stay released."""
    ev = last_write_off(batch)
    if ev is None:
        raise QAError(f"Batch {batch.batch_number} isn't written off, so there's nothing to undo.")
    deadline = undo_deadline(batch)
    if deadline is None or timezone.localdate() > deadline:
        raise QAError(f"The write-off of batch {batch.batch_number} is too old to undo.")
    with transaction.atomic():
        batch.status = 'Quarantined' if is_expired(batch) else 'Active'
        batch.closed_date = None
        batch.save(update_fields=['status', 'closed_date'])
        RegistryLog.objects.create(
            action_type='Adjusted',
            item_name=f"Batch {batch.batch_number} write-off undone",
            material=batch.material, quantity_changed=batch.quantity, warehouse=batch.warehouse, user=user,
        )
        ev.action = 'disposed_undone'
        ev.save(update_fields=['action'])
        log_event(batch, 'writeoff_undone', user, ev.reason,
                  (f"Back {'on hold (expired)' if batch.status == 'Quarantined' else 'in Active stock'}. {note}").strip(),
                  batch.quantity, ev.value_rm)
        if ev.reason == 'returned_to_supplier' and batch.purchase_order_id:
            OrderTimeline.objects.create(purchase_order=batch.purchase_order, user=user,
                                         action=f"Write-off of batch {batch.batch_number} undone.")
    return batch


# Shelf-life extensions

def extension_cap_days(batch):
    """Most days of extension the batch may get in total without approval."""
    original = batch.original_expiry_date or batch.expiry_date
    shelf = (original - batch.manufacturing_date).days if (original and batch.manufacturing_date) else 365
    return int(max(shelf, 0) * float(get_setting('qa_extension_cap_pct')) / 100)


def extend_expiry(batch, user, days, reason, note='', request=None, approved=False):
    """Pushes the expiry out by `days`. Beyond the cap (qa_extension_cap_pct of the original shelf life) it needs
    approval. Returns ('done', batch) or ('pending', QAApproval)."""
    check_reason(reason)
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise QAError("Enter the number of days to extend by.")
    if days <= 0:
        raise QAError("Extend the expiry by at least 1 day.")
    max_days = get_setting('qa_extension_max_days')
    if days > max_days:
        raise QAError(f"Extend by at most {max_days} days at a time.")
    if batch.status not in OPEN_STATUSES:
        raise QAError(f"Batch {batch.batch_number} is {batch.get_status_display()}, so its shelf life can't be extended.")
    with transaction.atomic():
        if (not approved and not user.has_perm(APPROVE_QA_WRITEOFFS)
                and batch.extension_days_total + days > extension_cap_days(batch)):
            return 'pending', _ask_approval(batch, user, 'extend', reason, note, request, days=days)
        batch.expiry_date = batch.expiry_date + timedelta(days=days)
        batch.extension_days_total += days
        batch.save(update_fields=['expiry_date', 'extension_days_total'])
        RegistryLog.objects.create(
            action_type='QA_Extension', item_name=f"Batch {batch.batch_number} (+{days} days, now {batch.expiry_date})",
            material=batch.material, quantity_changed=Decimal('0'), warehouse=batch.warehouse, user=user,
        )
        log_event(batch, 'extended', user, reason, f"+{days} days, new expiry {batch.expiry_date}. {note}".strip(),
                  request=request)
    return 'done', batch


# Approvals (Action Center)

def decide_approval(approval, user, approve, comment=''):
    if not user.has_perm(APPROVE_QA_WRITEOFFS):
        raise QAError("You can't approve QA write-offs or extensions.")
    if approval.status != 'Pending':
        raise QAError("This request has already been decided.")
    batch = approval.batch
    with transaction.atomic():
        if approve:
            if approval.kind == 'dispose':
                dispose_batch(batch, approval.requested_by or user, approval.reason, approval.note,
                              request=approval.request, approved=True)
                outcome = 'disposed'
            else:
                extend_expiry(batch, approval.requested_by or user, approval.days, approval.reason, approval.note,
                              request=approval.request, approved=True)
                outcome = 'extended'
            if approval.request and approval.request.is_open:
                resolve_request(approval.request, user, outcome, comment)
        else:
            log_event(batch, 'approval_rejected', user, approval.reason, comment or '', approval.quantity, approval.value_rm,
                      approval.request)
        approval.status = 'Approved' if approve else 'Rejected'
        approval.decided_by = user
        approval.decided_at = timezone.now()
        approval.comment = comment or ''
        approval.save()
    verdict = 'approved' if approve else 'rejected'
    if approval.requested_by:
        notify([approval.requested_by],
               f"QA {approval.get_kind_display().lower()} for batch {batch.batch_number} was {verdict} by "
               f"{user.get_full_name() or user.username}." + (f" Comment: {comment}" if comment else ''),
               reverse('batch_detail', args=[batch.batch_number]), exclude=user)
    return approval


# QA requests ("Send to QA")

def create_request(batch, user, reason, note='', quantity=None, priority='Normal', source='', hold=False, auto=False):
    """auto=True: raised by the system (new finished goods, checked-on-arrival receipts), so the user is told too."""
    check_reason(reason)
    if batch.status not in OPEN_STATUSES:
        raise QAError(f"Batch {batch.batch_number} is {batch.get_status_display()}, so it can't be sent to QA.")
    if priority not in dict(QARequest.PRIORITY_CHOICES):
        priority = 'Normal'
    existing = batch.qa_requests.filter(status__in=('Open', 'In Review')).first()
    if existing:
        raise QAError(f"Batch {batch.batch_number} is already waiting for QA (request #{existing.pk}).")
    with transaction.atomic():
        target = batch
        if hold and batch.status == 'Active':
            target = hold_batch(batch, user, reason, f"Held when sent to QA. {note}".strip(), quantity=quantity)
            quantity = None
        req = QARequest.objects.create(batch=target, quantity=quantity, reason=reason, note=note or '', priority=priority,
                                       source=(source or '')[:100], hold_requested=bool(hold), requested_by=user)
        log_event(target, 'requested', user, reason,
                  f"{priority} priority{' from ' + source if source else ''}. {note}".strip(), quantity, request=req)
        QAEvent.objects.filter(batch=target, action__in=HOLD_ACTIONS, request__isnull=True,
                               created_at__gte=timezone.now() - timedelta(minutes=1)).update(request=req)
    notify(qa_staff(target.warehouse_id),
           f"Sent to QA ({priority}): batch {target.batch_number}, {item_name(target)} - {reason_label(reason)}"
           f"{' (on hold)' if target.status == 'Quarantined' else ''}.",
           _dashboard_link('requests'), exclude=None if auto else user)
    return req


def assign_request(req, user):
    if not req.is_open:
        raise QAError("This request is already closed.")
    req.status = 'In Review'
    req.assigned_to = user
    req.save(update_fields=['status', 'assigned_to'])
    return req


def resolve_request(req, user, outcome, note=''):
    if outcome not in dict(QARequest.OUTCOME_CHOICES):
        raise QAError("Pick an outcome for the request.")
    if not req.is_open:
        return req
    req.status = 'Cancelled' if outcome == 'cancelled' else 'Resolved'
    req.outcome = outcome
    req.outcome_note = note or ''
    req.resolved_by = user
    req.resolved_at = timezone.now()
    req.save(update_fields=['status', 'outcome', 'outcome_note', 'resolved_by', 'resolved_at'])
    Notification.objects.filter(is_read=False, message__startswith='Sent to QA',
                                message__contains=f"batch {req.batch.batch_number},").update(is_read=True)
    if req.requested_by:
        notify([req.requested_by],
               f"QA closed your request for batch {req.batch.batch_number}: {req.get_outcome_display()}."
               + (f" {note}" if note else ''),
               reverse('batch_detail', args=[req.batch.batch_number]), exclude=user)
    return req


# Production guard

def run_hold_problem(run):
    """Why `run` can't start: a batch it holds is on QA hold or has expired. None if it's fine."""
    today = timezone.localdate()
    bad = []
    for a in run.allocations.select_related('batch'):
        b = a.batch
        if b.status == 'Quarantined':
            bad.append(f"{b.batch_number} is on QA hold")
        elif b.expiry_date and b.expiry_date < today:
            bad.append(f"{b.batch_number} expired on {b.expiry_date}")
    if not bad:
        return None
    return (f"Can't start {run.run_number}: batch {'; '.join(bad)}. Remove it from the run and allocate fresh stock, "
            "or ask QA to release it.")


# Recall and traceability

def trace(batch):
    """Where a batch came from and where it went: upstream raw batches (for a finished batch), downstream runs,
    finished batches, orders and shipments (for a raw batch)."""
    from .models import ProductionConsumption, RunExtraMaterial, ShipmentItem
    up, runs, fg, orders, shipments = [], [], [], {}, {}
    if batch.produced_in_id:
        run = batch.produced_in
        up = [c.consumed_batch for c in ProductionConsumption.objects.filter(production_run=run).select_related('consumed_batch')]
        up += [x.batch for x in RunExtraMaterial.objects.filter(production_run=run, batch__isnull=False).select_related('batch')]
        up = list({b.pk: b for b in up}.values())
    if batch.material_id:
        run_ids = set(ProductionConsumption.objects.filter(consumed_batch=batch).values_list('production_run_id', flat=True))
        run_ids |= set(RunExtraMaterial.objects.filter(batch=batch).values_list('production_run_id', flat=True))
        run_ids |= set(StockAllocation.objects.filter(batch=batch, production_run__isnull=False).values_list('production_run_id', flat=True))
        from .models import ProductionRun
        runs = list(ProductionRun.objects.filter(id__in=run_ids).select_related('target_product').order_by('-id'))
        fg = list(Batch.objects.filter(produced_in_id__in=run_ids).select_related('product', 'warehouse').order_by('-id'))
    finished = fg if batch.material_id else [batch]
    for a in StockAllocation.objects.filter(batch__in=finished, sales_order__isnull=False).select_related('sales_order'):
        orders[a.sales_order_id] = a.sales_order
    for si in ShipmentItem.objects.filter(batch__in=finished).select_related('shipment', 'shipment__sales_order'):
        shipments[si.shipment_id] = si.shipment
        if si.shipment.sales_order_id:
            orders[si.shipment.sales_order_id] = si.shipment.sales_order
    return {
        'upstream': up, 'runs': runs, 'finished_batches': fg,
        'orders': sorted(orders.values(), key=lambda o: o.so_number),
        'shipments': sorted(shipments.values(), key=lambda s: s.tracking_number),
        'holdable': [b for b in fg if b.status == 'Active'],
    }


def hold_downstream(batch, user, note=''):
    """Puts every Active finished batch made from `batch` on hold (a supplier recall). Returns the batches held."""
    held = []
    for fg in trace(batch)['holdable']:
        held.append(hold_batch(fg, user, 'supplier_defect',
                               f"Recall: made from batch {batch.batch_number}. {note}".strip()))
    log_event(batch, 'quarantined' if batch.status == 'Quarantined' else 'requested', user, 'supplier_defect',
              f"Recall: {len(held)} finished batch(es) made from this batch put on hold. {note}".strip())
    return held


# Daily sweep: expiry holds and alerts

def daily_sweep(today=None):
    """Idempotent: holds newly expired stock (setting qa_auto_hold_expired), warns QA once per batch nearing expiry,
    and reminds QA about long holds. Returns counts. Run by `manage.py qa_sweep` and lazily from the dashboards."""
    from .models import CustomUser
    today = today or timezone.localdate()
    system = None
    counts = {'auto_held': 0, 'warned': 0, 'hold_alerts': 0}

    if get_setting('qa_auto_hold_expired'):
        for b in (Batch.objects.filter(status='Active', expiry_date__lt=today)
                  .select_related('material', 'product', 'warehouse')):
            try:
                hold_batch(b, system, 'expired', f"Expired on {b.expiry_date}.", auto=True)
            except QAError:
                continue
            counts['auto_held'] += 1
            notify(qa_staff(b.warehouse_id), f"Batch {b.batch_number} ({item_name(b)}) expired on {b.expiry_date} "
                                            f"and was put on QA hold.", _dashboard_link('quarantined'))

    warn_by = today + timedelta(days=get_setting('qa_expiry_warning_days'))
    warned = set(QAEvent.objects.filter(action='expiry_warning').values_list('batch_id', flat=True))
    for b in (Batch.objects.filter(status='Active', expiry_date__gte=today, expiry_date__lte=warn_by)
              .exclude(id__in=warned).select_related('material', 'product', 'warehouse')):
        days = (b.expiry_date - today).days
        log_event(b, 'expiry_warning', system, 'near_expiry', f"Expires in {days} day(s).")
        notify(qa_staff(b.warehouse_id), f"Batch {b.batch_number} ({item_name(b)}) expires in {days} day(s), on "
                                        f"{b.expiry_date}.", _dashboard_link('expiring'))
        counts['warned'] += 1

    alert_days = get_setting('qa_quarantine_alert_days')
    held = (Batch.objects.filter(status='Quarantined')
            .annotate(held_at=Max('qa_events__created_at', filter=Q(qa_events__action__in=HOLD_ACTIONS)),
                      alerted_at=Max('qa_events__created_at', filter=Q(qa_events__action='hold_alert')))
            .select_related('material', 'product', 'warehouse'))
    cutoff = timezone.now() - timedelta(days=alert_days)
    for b in held:
        if b.held_at is None or b.held_at > cutoff:
            continue
        if b.alerted_at is not None and b.alerted_at >= b.held_at:
            continue
        days = (timezone.now() - b.held_at).days
        log_event(b, 'hold_alert', system, '', f"On hold for {days} days.")
        notify(qa_staff(b.warehouse_id), f"Batch {b.batch_number} ({item_name(b)}) has been on QA hold for {days} days.",
               _dashboard_link('quarantined'))
        counts['hold_alerts'] += 1
    return counts


_LAST_SWEEP = {}


def sweep_if_due():
    """daily_sweep at most once per hour per process, for pages that want fresh holds without a scheduler."""
    import time
    now = time.monotonic()
    if now - _LAST_SWEEP.get('t', -1e9) < 3600:
        return None
    _LAST_SWEEP['t'] = now
    return daily_sweep()
