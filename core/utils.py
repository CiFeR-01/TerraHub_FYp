import re
from datetime import date
from decimal import Decimal
from django.db import transaction


def _close_batch_if_depleted(batch):
    """If a batch's quantity has been decremented to zero (or below, from a
    rounding edge), marks it Depleted and stamps closed_date - the moment it
    stops accruing rent. No-op if already closed (Depleted/Spoiled) so a
    second decrement pass can't stomp an existing closed_date."""
    if batch.quantity <= 0 and batch.status not in ('Depleted', 'Spoiled'):
        batch.status = 'Depleted'
        batch.closed_date = date.today()
        return True
    return False


def format_mt(total_mt):
    """Human-friendly display for an already-computed MT amount: auto-switches
    kg -> MT so a small figure (e.g. one batch's weight) doesn't read as a
    fiddly '0.03 MT' when '30 kg' is what a person actually means."""
    total_mt = float(total_mt)
    if total_mt >= 1:
        return f"{total_mt:.2f} MT"
    return f"{total_mt * 1000:.0f} kg"


def format_stock_display(quantity, product):
    """Human-friendly stock figure for a Product: a 'pcs' product stays a
    plain count (weight_mt_per_unit is only there for capacity/logistics
    math, not what a person means by "how many"); everything else converts
    to real weight, auto-switching kg -> MT via format_mt() so it never
    reads as a stagnant, always-the-same per-unit config value."""
    if product.unit_of_measure == 'pcs':
        return f"{float(quantity):,.0f} pcs"
    return format_mt(float(quantity) * float(product.weight_mt_per_unit))


def generate_next_code(model_class, field_name, prefix, default_num=1001, pad=4):
    """
    Generates an automated unique ID like SO-1004, PO-5003, RUN-809, PRD-1001, MAT-1001.
    Searches existing database records for codes, parses the highest trailing integer matching prefix, and increments by 1.
    """
    existing_codes = model_class.objects.values_list(field_name, flat=True)
    max_num = 0
    for code in existing_codes:
        if not code:
            continue
        nums = re.findall(r'\d+', str(code))
        if nums:
            num = int(nums[-1])
            if num > max_num:
                max_num = num
    
    if max_num < default_num - 1:
        next_num = default_num
    else:
        next_num = max_num + 1
        
    candidate = f"{prefix}-{next_num:0{pad}d}" if pad else f"{prefix}-{next_num}"
    while model_class.objects.filter(**{field_name: candidate}).exists():
        next_num += 1
        candidate = f"{prefix}-{next_num:0{pad}d}" if pad else f"{prefix}-{next_num}"
    return candidate

def unexpired(batches):
    """Narrows a Batch queryset to stock that can still be used: no expiry date,
    or one that hasn't passed yet. FEFO picking must never suggest expired stock."""
    from django.db.models import Q
    from django.utils import timezone
    return batches.filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=timezone.localdate()))


def trim_batch_reservations(batch, keep, user, why):
    """Cuts the reservations on `batch` down to `keep` units, for when stock is
    spoiled or a count finds less than was reserved. Order and run reservations
    are cut newest first; an order that loses stock drops back from Ready to Ship
    to Pending, and a run from Planned to Pending Allocation, so they get
    allocated again. Stock already loaded on an open shipment is never cut:
    returns an error message (and changes nothing) if that would be needed,
    else None. Saves the batch's allocated_quantity."""
    from .models import OrderTimeline
    keep = max(Decimal(str(keep)), Decimal('0'))
    if batch.allocated_quantity <= keep:
        return None

    allocs = list(batch.allocations.select_related('sales_order', 'production_run', 'shipment').order_by('-id'))
    on_trucks = [a for a in allocs if a.shipment and a.shipment.status not in ('Completed', 'Cancelled')]
    if sum((a.quantity for a in on_trucks), Decimal('0')) > keep:
        trucks = ', '.join(sorted({a.shipment.tracking_number for a in on_trucks}))
        return f"Stock from batch {batch.batch_number} is loaded on {trucks}. Scrap or finish that shipment first."

    excess = batch.allocated_quantity - keep
    for a in allocs:
        if excess <= 0:
            break
        if a in on_trucks:
            continue
        cut = min(a.quantity, excess)
        excess -= cut
        a.quantity -= cut
        if a.quantity <= 0:
            a.delete()
        else:
            a.save(update_fields=['quantity'])
        note = f"{cut} reserved from batch {batch.batch_number} released: {why}."
        if a.sales_order:
            so = a.sales_order
            if so.status == 'Ready to Ship':
                so.status = 'Pending'
                so.save(update_fields=['status'])
                note += " Allocate stock again before shipping."
            OrderTimeline.objects.create(sales_order=so, action=note, user=user)
        elif a.production_run:
            run = a.production_run
            if run.status == 'Planned':
                run.status = 'Pending Allocation'
                run.save(update_fields=['status'])
                note += " Allocate materials again before starting."
            OrderTimeline.objects.create(production_run=run, action=note, user=user)

    # Any excess left over was a counter with no reservation row behind it
    batch.allocated_quantity = keep
    batch.save(update_fields=['allocated_quantity'])
    return None


def allocate_stock(order_type, order, material_or_product, required_qty, warehouse=None):
    """
    Allocates `required_qty` of a Material or Product to a SalesOrder, ProductionRun, or Shipment.
    Returns the total quantity successfully allocated (which may be less than required_qty).
    """
    from .models import Batch, StockAllocation
    
    if required_qty <= 0:
        return 0

    if type(material_or_product).__name__ == 'Product':
        batches = Batch.objects.filter(product=material_or_product, status='Active')
    else:
        batches = Batch.objects.filter(material=material_or_product, status='Active')
        
    if warehouse:
        batches = batches.filter(warehouse=warehouse)
        
    from django.db.models import F
    batches = unexpired(batches).order_by(F('expiry_date').asc(nulls_last=True), 'manufacturing_date')

    remaining_to_allocate = Decimal(str(required_qty))
    total_allocated = Decimal('0')

    with transaction.atomic():
        for batch in batches:
            if remaining_to_allocate <= 0:
                break
                
            available = batch.quantity - batch.allocated_quantity
            if available <= 0:
                continue

            qty_to_take = min(available, remaining_to_allocate)
            batch.allocated_quantity += qty_to_take
            batch.save(update_fields=['allocated_quantity'])
            
            kwargs = {
                'batch': batch,
                'quantity': qty_to_take
            }
            if order_type == 'sales_order':
                kwargs['sales_order'] = order
            elif order_type == 'production_run':
                kwargs['production_run'] = order
            elif order_type == 'shipment':
                kwargs['shipment'] = order
                
            StockAllocation.objects.create(**kwargs)
            
            remaining_to_allocate -= qty_to_take
            total_allocated += qty_to_take
            
    return total_allocated

def so_line_commitment(so, product, quantity_shipped=None):
    """How much of `product` a sales order has already covered, split by where the stock is:

      held          reserved directly on the order (still to be put on a logistics order)
      in_logistics  on the order's outbound logistics orders that haven't been credited as shipped yet.
                    Creating a logistics order moves the reservation onto the shipment, so this stock
                    no longer belongs to the order itself - but it is still the order's.
      shipped       already credited to the order (SalesOrderDetail.quantity_shipped)
      total         all three: what the order no longer needs found or made.

    Shipments already credited to the order are left out of in_logistics because their cargo is
    in `shipped` - counting both would count it twice. Cancelled shipments hold nothing, and a
    stock move (consolidation) keeps the order's own reservation, so it is not counted here."""
    from django.db.models import Sum
    from .models import StockAllocation

    held = StockAllocation.objects.filter(
        sales_order=so, batch__product=product,
    ).aggregate(s=Sum('quantity'))['s'] or Decimal('0')
    in_logistics = StockAllocation.objects.filter(
        sales_order__isnull=True, shipment__sales_order=so, shipment__direction='Outbound',
        shipment__credited_to_so=False, batch__product=product,
    ).exclude(shipment__status='Cancelled').aggregate(s=Sum('quantity'))['s'] or Decimal('0')
    if quantity_shipped is None:
        quantity_shipped = sum(
            (i.quantity_shipped or Decimal('0') for i in so.items.all() if i.product_id == product.id),
            Decimal('0'),
        )
    held, in_logistics, shipped = Decimal(str(held)), Decimal(str(in_logistics)), Decimal(str(quantity_shipped or 0))
    return {'held': held, 'in_logistics': in_logistics, 'shipped': shipped,
            'total': held + in_logistics + shipped}


def create_shortage_production_runs(so, plant, user):
    """Creates a 'Pending Approval' Production Run for each SO line item not
    yet covered by StockAllocations. Returns True if any run was created."""
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import Sum
    from .models import ProductionRun, StockAllocation, OrderTimeline

    created_any = False
    for item in so.items.all():
        qty_needed = Decimal(str(item.quantity_ordered))
        unfulfilled = qty_needed - so_line_commitment(so, item.product, item.quantity_shipped)['total']

        if unfulfilled > 0:
            pr = ProductionRun.objects.filter(sales_order=so, target_product=item.product).exclude(status='Cancelled').first()
            if not pr:
                # A cancelled run from an earlier attempt still holds the plain name, so take the next free suffix
                base_number = f"PR-{so.so_number}-{item.product.sku}"
                run_number, attempt = base_number, 1
                while ProductionRun.objects.filter(run_number=run_number).exists():
                    attempt += 1
                    run_number = f"{base_number}-{attempt}"
                pr_new = ProductionRun.objects.create(
                    run_number=run_number,
                    target_product=item.product,
                    expected_yield=unfulfilled,
                    status='Pending Approval',
                    sales_order=so,
                    manufacturing_plant=plant,
                    start_time=timezone.now() + timedelta(days=1),
                    end_time=timezone.now() + timedelta(days=1, hours=4),
                    created_by=user
                )
                OrderTimeline.objects.create(production_run=pr_new, action="Manufacturing Order created. Pending Approval.", user=user)
                created_any = True

    return created_any


def delete_draft_transfer(shipment, user=None, note=""):
    """Deletes a transfer that a production run raised and that never went for approval,
    instead of leaving a Cancelled shell behind. The trace is the Registry Ledger: one
    'Draft Transfer Deleted' row per line (material, quantity, where it was to come from)
    plus a note on the run's timeline. No stock moves here - callers release the
    allocations. Returns the tracking number."""
    from .models import RegistryLog, OrderTimeline

    tracking = shipment.tracking_number
    run = shipment.linked_production_run
    route = f"{shipment.origin_warehouse.name if shipment.origin_warehouse else '?'} -> {shipment.destination_warehouse.name if shipment.destination_warehouse else '?'}"
    suffix = f" ({run.run_number})" if run else ""
    tail = f" {note}" if note else ""
    lines = list(shipment.items.select_related('material', 'batch', 'batch__material'))

    with transaction.atomic():
        for it in lines:
            material = it.material or (it.batch.material if it.batch else None)
            name = material.name if material else (it.product.name if it.product else 'Item')
            batch_part = f" (Batch {it.batch.batch_number})" if it.batch else ""
            RegistryLog.objects.create(
                action_type='Draft_Transfer_Deleted',
                item_name=f"{name}{batch_part} - draft transfer {tracking} deleted{suffix}, {route}.{tail}",
                material=material,
                quantity_changed=it.quantity,
                warehouse=shipment.origin_warehouse,
                user=user,
            )
        if not lines:
            RegistryLog.objects.create(
                action_type='Draft_Transfer_Deleted',
                item_name=f"Draft transfer {tracking} deleted{suffix}, {route}, no lines.{tail}",
                quantity_changed=Decimal('0'),
                warehouse=shipment.origin_warehouse,
                user=user,
            )
        if run:
            OrderTimeline.objects.create(
                production_run=run,
                action=f"Draft transfer {tracking} deleted before approval (see the Registry Ledger).",
                user=user,
            )
        shipment.delete()
    return tracking


def release_production_run_allocations(run, user=None):
    """Releases every StockAllocation held by a run back to stock, and deletes
    any auto-generated Draft transfer shipments left over from allocating it
    (a trace of each stays in the Registry Ledger)."""
    from .models import StockAllocation, Shipment

    allocs = StockAllocation.objects.filter(production_run=run)
    for alloc in allocs:
        batch = alloc.batch
        batch.allocated_quantity -= alloc.quantity
        if batch.allocated_quantity < 0:
            batch.allocated_quantity = 0
        batch.save(update_fields=['allocated_quantity'])
        alloc.delete()

    for draft in Shipment.objects.filter(
        linked_production_run=run, is_auto_generated=True, status='Draft'
    ).select_related('origin_warehouse', 'destination_warehouse', 'linked_production_run'):
        delete_draft_transfer(draft, user, note="Allocation released.")


def cancel_run_allocation(run, user=None):
    """Undoes a run's allocation so it can be redone from scratch: releases the stock and
    cancels its Draft auto-transfers, and puts the run back to Pending Allocation.
    Refused (returns a message) once production has started, or once a transfer has left
    Draft - those are Logistics' to cancel or receive, and changing their lines from here
    would leave the shipment out of step. Returns None on success."""
    from .models import OrderTimeline

    if run.status not in ('Pending Allocation', 'Planned', 'Awaiting Materials'):
        return f"Allocation can only be cancelled before production starts (this run is {run.get_status_display()})."
    under_way = list(run.linked_shipments.exclude(status__in=['Draft', 'Cancelled']))
    if under_way:
        numbers = ", ".join(s.tracking_number for s in under_way)
        return f"Transfers for this run are already under way ({numbers}). Cancel or receive them in Logistics first."

    release_production_run_allocations(run, user)
    run.status = 'Pending Allocation'
    run.save()
    OrderTimeline.objects.create(production_run=run, action="Allocation cancelled. Materials released; allocate again to continue.", user=user)
    return None


def remove_run_allocation(run, alloc, user=None):
    """Takes one allocation line off a run that hasn't started, leaving its others alone.
    The stock goes back to the batch and the line comes off its Draft transfer (the transfer
    is cancelled if that was its last line). Refused once production has started or when the
    line's transfer has left Draft - those belong to Logistics. Returns None on success,
    otherwise a message."""
    from .models import OrderTimeline, ShipmentItem

    if run.status not in ('Planned', 'Awaiting Materials'):
        return f"An allocation can only be removed before production starts (this run is {run.get_status_display()})."
    shipment = alloc.shipment
    if shipment and shipment.status != 'Draft':
        return f"This line is on transfer {shipment.tracking_number} ({shipment.status}). Cancel or receive that transfer in Logistics instead."

    batch, qty = alloc.batch, alloc.quantity
    with transaction.atomic():
        batch.allocated_quantity = max(batch.allocated_quantity - qty, Decimal('0'))
        batch.save(update_fields=['allocated_quantity'])
        alloc.delete()
        if shipment:
            if shipment.items.exclude(batch=batch).exists():
                ShipmentItem.objects.filter(shipment=shipment, batch=batch).delete()
            else:
                # That was the transfer's last line: delete the transfer, leaving its ledger trace
                delete_draft_transfer(shipment, user, note="Its last line was removed.")

        # Waiting on transfers only while some allocated stock is still on its way
        remaining = run.allocations.all()
        if not remaining.exists():
            run.status = 'Pending Allocation'
        elif remaining.filter(shipment__isnull=False).exclude(shipment__status__in=['Arrived', 'Completed']).exists():
            run.status = 'Awaiting Materials'
        else:
            run.status = 'Planned'
        run.save()
        OrderTimeline.objects.create(
            production_run=run,
            action=f"Removed {qty} from batch {batch.batch_number} from the allocation.",
            user=user)
    return None


def sync_production_run_yield(so, product, unfulfilled, user):
    """Keeps a not-yet-started run's expected_yield in sync with the SO's
    remaining shortfall (cancels it if the shortfall reaches zero)."""
    from .models import ProductionRun, OrderTimeline

    run = ProductionRun.objects.filter(
        sales_order=so, target_product=product,
        status__in=['Pending Approval', 'Pending Allocation', 'Awaiting Materials', 'Planned']
    ).first()
    if not run:
        return

    unfulfilled = Decimal(str(unfulfilled))
    if unfulfilled <= 0:
        old_yield = run.expected_yield
        release_production_run_allocations(run, user)
        run.status = 'Cancelled'
        run.save(update_fields=['status'])
        OrderTimeline.objects.create(
            production_run=run,
            action=f"Auto-cancelled: SO shortage fully covered by direct stock allocation ({old_yield} MT no longer needed).",
            user=user
        )
    elif unfulfilled != run.expected_yield:
        old_yield = run.expected_yield
        run.expected_yield = unfulfilled
        run.save(update_fields=['expected_yield'])
        OrderTimeline.objects.create(
            production_run=run,
            action=f"Expected yield adjusted from {old_yield} to {unfulfilled} MT after additional stock was directly allocated to the SO.",
            user=user
        )


def handle_so_item_removed(so, product, user):
    """Called when a Sales Order line item is deleted. Cancels its linked
    Production Run if not yet started; otherwise just notes it on the timeline."""
    from .models import ProductionRun, OrderTimeline

    run = ProductionRun.objects.filter(sales_order=so, target_product=product).exclude(status='Cancelled').first()
    if not run:
        return

    if run.status in ['InProgress', 'Completed']:
        OrderTimeline.objects.create(
            production_run=run,
            action=f"Note: {product.sku} was removed from {so.so_number} — this run's SO no longer requires it.",
            user=user
        )
    else:
        release_production_run_allocations(run, user)
        run.status = 'Cancelled'
        run.save(update_fields=['status'])
        OrderTimeline.objects.create(
            production_run=run,
            action=f"Auto-cancelled: {product.sku} was removed from {so.so_number}, this run is no longer needed.",
            user=user
        )


def release_so_product_allocations(so, product, user):
    """A line item was removed from a Sales Order: free every finished-goods
    reservation this order holds for that product, and take those batches off any
    stock-move transfer still in progress for the order (cancelling a transfer
    left with no cargo)."""
    from .models import StockAllocation, ShipmentItem, OrderTimeline

    allocs = list(
        StockAllocation.objects.filter(sales_order=so, batch__product=product)
        .select_related('batch', 'shipment')
    )
    if not allocs:
        return

    touched = {}
    for a in allocs:
        b = a.batch
        sh = a.shipment
        if sh and sh.direction == 'Transfer' and sh.status not in ('Completed', 'Cancelled'):
            ShipmentItem.objects.filter(shipment=sh, batch=b).delete()
            touched[sh.id] = sh
        b.allocated_quantity = max((b.allocated_quantity or Decimal('0')) - a.quantity, Decimal('0'))
        b.save(update_fields=['allocated_quantity'])
        a.delete()

    for sh in touched.values():
        if not sh.items.exists():
            sh.status = 'Cancelled'
            sh.save(update_fields=['status'])
            OrderTimeline.objects.create(shipment=sh, action=f"Auto-cancelled: nothing left to move after {product.sku} was removed from {so.so_number}.", user=user)
        else:
            OrderTimeline.objects.create(shipment=sh, action=f"{product.sku} batches removed after the line was dropped from {so.so_number}.", user=user)
    OrderTimeline.objects.create(sales_order=so, action=f"Released the stock reserved for {product.sku} after its line was removed.", user=user)


# ---------------------------------------------------------------------------
# Delivering an order whose stock is in several warehouses
# ---------------------------------------------------------------------------
# Either ship separately (one outbound shipment per warehouse - so_create_shipment)
# or first move everything to the order's origin with internal transfers linked to
# the order, then ship once. While a stock-move transfer is in progress its
# reservation rows carry BOTH sales_order (the order owns the stock) and shipment
# (the truck carrying it); on arrival the reservation moves to the arrived batch.

def so_stock_by_warehouse(so):
    """The order's reserved stock not yet on any shipment, per warehouse:
    [{'warehouse': Warehouse, 'qty': Decimal, 'allocations': [...]}, ...]."""
    from .models import StockAllocation

    groups = {}
    for a in (StockAllocation.objects.filter(sales_order=so, shipment__isnull=True)
              .select_related('batch__warehouse', 'batch__product')):
        g = groups.setdefault(a.batch.warehouse_id, {'warehouse': a.batch.warehouse, 'qty': Decimal('0'), 'allocations': []})
        g['qty'] += a.quantity
        g['allocations'].append(a)
    return sorted(groups.values(), key=lambda g: (g['warehouse'].name if g['warehouse'] else ''))


def prompt_delivery_choice(so, user):
    """When an order is Ready to Ship with its stock in more than one warehouse, tell
    whoever created it to choose how to deliver (once - not on every recheck)."""
    from django.urls import reverse
    from .models import Notification, OrderTimeline

    if so.status != 'Ready to Ship':
        return
    split = so_stock_by_warehouse(so)
    if len(split) < 2:
        return
    message = f"{so.so_number} is ready to ship, but its stock is in {len(split)} warehouses. Choose how to deliver it."
    if so.created_by and not Notification.objects.filter(user=so.created_by, message=message).exists():
        Notification.objects.create(user=so.created_by, message=message, link=reverse('so_detail', args=[so.pk]))
        OrderTimeline.objects.create(
            sales_order=so,
            action=f"Stock is in {len(split)} warehouses ({', '.join(g['warehouse'].name for g in split if g['warehouse'])}). Choose to ship separately or move it all to {so.origin_warehouse.name} first.",
            user=user,
        )


def consume_materials_for_run(run, user):
    """Deducts the raw materials a completed run used: consumes from its
    allocations first, then tops up from plant stock via FEFO if actual usage
    exceeded what was allocated. Logs a ProductionConsumption + RegistryLog entry."""
    from django.db.models import F
    from .models import Batch, StockAllocation, ProductionConsumption, RunMaterialUsage, Material, RegistryLog

    usage_by_material = {
        u.material_id: Decimal(str(u.actual_qty))
        for u in RunMaterialUsage.objects.filter(production_run=run)
    }

    released = []   # unused allocated stock handed back, one entry per batch
    material_ids = set(usage_by_material.keys())
    material_ids.update(
        StockAllocation.objects.filter(production_run=run, batch__material__isnull=False)
        .values_list('batch__material_id', flat=True)
    )

    for material_id in material_ids:
        material = Material.objects.get(id=material_id)
        allocations = list(
            StockAllocation.objects.filter(production_run=run, batch__material=material).select_related('batch')
        )
        allocated_total = sum((a.quantity for a in allocations), Decimal('0'))
        actual_used = usage_by_material.get(material_id, allocated_total)

        remaining = actual_used
        allocations.sort(key=lambda a: (a.batch.expiry_date is None, a.batch.expiry_date, a.batch.manufacturing_date))

        for alloc in allocations:
            batch = alloc.batch
            take = min(alloc.quantity, remaining) if remaining > 0 else Decimal('0')

            if take > 0:
                batch.quantity -= take
                ProductionConsumption.objects.create(production_run=run, consumed_batch=batch, quantity_used=take)
                remaining -= take

            # Release the full hold this allocation had — whatever wasn't consumed becomes available again
            unused = alloc.quantity - take
            if unused > 0:
                released.append(f"{unused.normalize():f} {material.unit_of_measure} of {material.name} back to batch {batch.batch_number}")
            batch.allocated_quantity -= alloc.quantity
            if batch.allocated_quantity < 0:
                batch.allocated_quantity = 0
            _close_batch_if_depleted(batch)
            batch.save(update_fields=['quantity', 'allocated_quantity', 'status', 'closed_date'])
            alloc.delete()

        # Material poured beyond the allocation: the user said where it came from.
        extras = list(run.extra_sources.filter(material=material))
        unrecorded_total = Decimal('0')
        for ex in extras:
            if ex.batch_id is None:
                # Stock the system never held: note the usage, deduct nothing
                unrecorded_total += ex.quantity
                remaining -= ex.quantity
                continue
            batch = Batch.objects.get(id=ex.batch_id)
            free = max(batch.quantity - batch.allocated_quantity, Decimal('0'))
            take = min(ex.quantity, free)
            if take > 0:
                batch.quantity -= take
                _close_batch_if_depleted(batch)
                batch.save(update_fields=['quantity', 'status', 'closed_date'])
                ProductionConsumption.objects.create(production_run=run, consumed_batch=batch, quantity_used=take)
                remaining -= take

        # No sources recorded (e.g. submitted before extra-material tracing existed):
        # fall back to the plant's earliest-expiring stock.
        if remaining > 0 and not extras and run.manufacturing_plant:
            extra_batches = unexpired(Batch.objects.filter(
                material=material, status='Active', warehouse=run.manufacturing_plant
            )).annotate(avail=F('quantity') - F('allocated_quantity')).filter(avail__gt=0).order_by(
                F('expiry_date').asc(nulls_last=True), 'manufacturing_date'
            )
            for batch in extra_batches:
                if remaining <= 0:
                    break
                take = min(Decimal(str(batch.avail)), remaining)
                if take <= 0:
                    continue
                batch.quantity -= take
                _close_batch_if_depleted(batch)
                batch.save(update_fields=['quantity', 'status', 'closed_date'])
                ProductionConsumption.objects.create(production_run=run, consumed_batch=batch, quantity_used=take)
                remaining -= take

        consumed = actual_used - remaining - unrecorded_total   # stock actually deducted
        if consumed > 0:
            RegistryLog.objects.create(
                action_type='Consumed_For_Manufacturing',
                item_name=f"{material.name} (Run {run.run_number})",
                material=material,
                quantity_changed=consumed,
                warehouse=run.manufacturing_plant,
                user=user
            )

        if remaining > 0:
            from .models import OrderTimeline
            OrderTimeline.objects.create(
                production_run=run,
                action=f"Warning: {material.sku} usage exceeded available stock by {remaining}.",
                user=user
            )

    if released:
        from .models import OrderTimeline
        OrderTimeline.objects.create(
            production_run=run,
            action="Unused allocated stock released: " + "; ".join(released) + ".",
            user=user
        )


def approve_production_run(run, user):
    """Approves a 'Pending Approval' run, clearing it for material allocation
    via the FEFO allocation screen."""
    from .models import OrderTimeline
    from django.utils import timezone
    run.status = 'Pending Allocation'
    run.approved_by = user
    run.approved_at = timezone.now()
    run.save()
    OrderTimeline.objects.create(
        production_run=run,
        action=f"Production Run {run.run_number} approved. Ready for material allocation.",
        user=user
    )


def finalize_production_run(run, user):
    """Completes a run: creates the finished-goods batch, marks it Completed,
    and (if linked to a SalesOrder) allocates the batch to that order's line
    item and advances its status. With the qa_hold_new_finished_goods setting on,
    the batch starts Quarantined and the order allocation waits for QA release
    (see allocate_finished_batch_to_order). Returns the batch, or None if no yield."""
    import uuid
    from django.utils import timezone
    from datetime import timedelta
    from django.urls import reverse
    from .models import Batch, OrderTimeline, RegistryLog, Notification
    from .settings_store import get_setting

    consume_materials_for_run(run, user)
    qa_hold = get_setting("qa_hold_new_finished_goods")

    fg_batch = None
    if run.actual_yield:
        fg_batch = Batch.objects.create(
            batch_number=f"FG-{run.run_number}-{str(uuid.uuid4())[:4]}",
            status='Quarantined' if qa_hold else 'Active',
            product=run.target_product,
            quantity=run.actual_yield,
            produced_in=run,
            warehouse=run.manufacturing_plant,
            manufacturing_date=timezone.now().date(),
            expiry_date=timezone.now().date() + timedelta(days=365)
        )
        RegistryLog.objects.create(
            action_type='Produced',
            item_name=f"{run.target_product.name} (Batch {fg_batch.batch_number})",
            quantity_changed=run.actual_yield,
            warehouse=run.manufacturing_plant,
            user=user
        )

    run.status = 'Completed'
    run.exact_end_time = timezone.now()
    run.save()

    if fg_batch and qa_hold:
        OrderTimeline.objects.create(
            production_run=run,
            action=f"FG batch {fg_batch.batch_number} created and held for QA release.",
            user=user
        )
        if run.sales_order:
            OrderTimeline.objects.create(
                sales_order=run.sales_order,
                action=f"Production Run {run.run_number} completed. FG batch {fg_batch.batch_number} is awaiting QA release before it can be allocated.",
                user=user
            )
        from .permissions import approvers
        for reviewer in approvers():
            Notification.objects.create(
                user=reviewer,
                message=f"QA release needed: batch {fg_batch.batch_number} ({run.target_product.name}) from Run {run.run_number}.",
                link=reverse("qa_dashboard")
            )
    elif fg_batch and run.sales_order:
        allocate_finished_batch_to_order(run, fg_batch, user, event=f"Production Run {run.run_number} completed. FG batch {fg_batch.batch_number} created.")

    return fg_batch


def allocate_finished_batch_to_order(run, fg_batch, user, event):
    """Reserve a run's finished batch for the sales order it was made for (up to what
    the order still needs) and move the order to Ready to Ship / Pending. Called at
    run completion, or on QA release when the batch was held."""
    from django.db.models import Sum
    from .models import StockAllocation, OrderTimeline

    so = run.sales_order
    if not so or so.status in ['Shipped', 'Delivered', 'Cancelled', 'Rejected', 'Draft']:
        return
    item = so.items.filter(product=run.target_product).first()

    if item:
        outstanding = Decimal(str(item.quantity_ordered)) - so_line_commitment(so, item.product, item.quantity_shipped)['total']

        if outstanding > 0:
            take = min(fg_batch.quantity - fg_batch.allocated_quantity, outstanding)
            if take > 0:
                fg_batch.allocated_quantity += take
                fg_batch.save(update_fields=['allocated_quantity'])
                StockAllocation.objects.create(batch=fg_batch, sales_order=so, quantity=take)
                OrderTimeline.objects.create(
                    sales_order=so,
                    action=f"Auto-allocated {take} units of {item.product.sku} from newly produced batch {fg_batch.batch_number} (Run {run.run_number}).",
                    user=user
                )

    fully_covered = True
    for it in so.items.all():
        if so_line_commitment(so, it.product, it.quantity_shipped)['total'] < Decimal(str(it.quantity_ordered)):
            fully_covered = False
            break

    so.status = 'Ready to Ship' if fully_covered else 'Pending'
    so.save()

    OrderTimeline.objects.create(
        sales_order=so,
        action=event + (" Order fully covered — moved to Ready to Ship." if fully_covered else " Order still has outstanding items."),
        user=user
    )
    prompt_delivery_choice(so, user)


def get_batch_reservations(batch):
    """Every current reservation against a batch, resolved to a human label +
    URL (Sales Order, Production Run, or Shipment)."""
    from django.urls import reverse
    from .models import StockAllocation

    reservations = []
    for alloc in StockAllocation.objects.filter(batch=batch).select_related('sales_order', 'production_run', 'shipment'):
        if alloc.sales_order:
            reservations.append({
                'quantity': alloc.quantity,
                'label': f"Sales Order {alloc.sales_order.so_number}",
                'url': reverse('so_detail', args=[alloc.sales_order.pk]),
            })
        elif alloc.production_run:
            reservations.append({
                'quantity': alloc.quantity,
                'label': f"Production Run {alloc.production_run.run_number}",
                'url': reverse('production_run_detail', args=[alloc.production_run.pk]),
            })
        elif alloc.shipment:
            reservations.append({
                'quantity': alloc.quantity,
                'label': f"Shipment {alloc.shipment.tracking_number}",
                'url': reverse('shipment_detail', args=[alloc.shipment.pk]),
            })
    return reservations


def get_batch_produced_for(batch):
    """If manufactured (not received via PO), the SO its Production Run was
    originally linked to - "intended for", distinct from get_batch_reservations."""
    if batch.produced_in and batch.produced_in.sales_order:
        so = batch.produced_in.sales_order
        from django.urls import reverse
        return {'so_number': so.so_number, 'url': reverse('so_detail', args=[so.pk]), 'run_number': batch.produced_in.run_number}
    return None


def unallocate_so_batch(allocation, quantity, user, target_so=None):
    """Releases a quantity of an SO-level StockAllocation, or transfers it
    directly to target_so if given. Returns (source_so, target_so_or_None)."""
    from .models import StockAllocation, OrderTimeline, SalesOrder

    if allocation.sales_order is None:
        raise ValueError("This allocation isn't held by a Sales Order — nothing to unallocate here.")

    quantity = Decimal(str(quantity))
    if quantity <= 0 or quantity > allocation.quantity:
        raise ValueError(f"Quantity must be between 0 and {allocation.quantity}.")

    source_so = allocation.sales_order
    batch = allocation.batch
    product = batch.product

    with transaction.atomic():
        if quantity == allocation.quantity:
            allocation.delete()
        else:
            allocation.quantity -= quantity
            allocation.save(update_fields=['quantity'])

        batch.allocated_quantity -= quantity
        if batch.allocated_quantity < 0:
            batch.allocated_quantity = 0
        batch.save(update_fields=['allocated_quantity'])

        if target_so:
            batch.allocated_quantity += quantity
            batch.save(update_fields=['allocated_quantity'])
            StockAllocation.objects.create(batch=batch, sales_order=target_so, quantity=quantity)
            OrderTimeline.objects.create(
                sales_order=source_so,
                action=f"Unallocated {quantity} units of {product.sku} (Batch {batch.batch_number}) — transferred to {target_so.so_number}.",
                user=user
            )
            OrderTimeline.objects.create(
                sales_order=target_so,
                action=f"Received {quantity} units of {product.sku} (Batch {batch.batch_number}) transferred from {source_so.so_number}.",
                user=user
            )
        else:
            OrderTimeline.objects.create(
                sales_order=source_so,
                action=f"Unallocated {quantity} units of {product.sku} (Batch {batch.batch_number}) — released back to available stock.",
                user=user
            )

    _resync_so_fulfillment_status(source_so, user)
    if target_so:
        _resync_so_fulfillment_status(target_so, user)

    return source_so, target_so


def _resync_so_fulfillment_status(so, user):
    """After allocations change, nudges an SO's status to reflect whether it's
    now fully covered (same check finalize_production_run does at completion)."""
    from django.db.models import Sum
    from .models import StockAllocation, OrderTimeline

    if so.status in ['Shipped', 'Delivered', 'Cancelled', 'Rejected', 'Draft', 'Pending Approval']:
        return

    fully_covered = True
    for it in so.items.all():
        if so_line_commitment(so, it.product, it.quantity_shipped)['total'] < Decimal(str(it.quantity_ordered)):
            fully_covered = False
            break

    new_status = 'Ready to Ship' if fully_covered else 'Pending'
    if so.status != new_status:
        old_status = so.status
        so.status = new_status
        so.save(update_fields=['status'])
        OrderTimeline.objects.create(sales_order=so, action=f"Status auto-updated from {old_status} to {new_status} after allocation change.", user=user)


def deallocate_stock(order_type, order):
    """
    Reverses all allocations for a specific SalesOrder, ProductionRun, or Shipment.
    """
    from .models import StockAllocation
    with transaction.atomic():
        if order_type == 'sales_order':
            allocs = StockAllocation.objects.filter(sales_order=order)
        elif order_type == 'shipment':
            allocs = StockAllocation.objects.filter(shipment=order)
        else:
            allocs = StockAllocation.objects.filter(production_run=order)
            
        for alloc in allocs:
            batch = alloc.batch
            batch.allocated_quantity -= alloc.quantity
            batch.save(update_fields=['allocated_quantity'])
            alloc.delete()

def deduct_stock_from_allocation(order_type, order, user=None):
    """Permanently deducts allocated stock from physical batch quantities
    (order shipped / run completed), logging an Outbound RegistryLog entry per batch."""
    from .models import StockAllocation, RegistryLog
    with transaction.atomic():
        if order_type == 'sales_order':
            allocs = StockAllocation.objects.filter(sales_order=order)
        elif order_type == 'shipment':
            allocs = StockAllocation.objects.filter(shipment=order)
        else:
            allocs = StockAllocation.objects.filter(production_run=order)

        for alloc in allocs:
            batch = alloc.batch
            batch.quantity -= alloc.quantity
            batch.allocated_quantity -= alloc.quantity
            _close_batch_if_depleted(batch)
            batch.save(update_fields=['quantity', 'allocated_quantity', 'status', 'closed_date'])

            item_name = batch.material.name if batch.material else (batch.product.name if batch.product else batch.batch_number)
            RegistryLog.objects.create(
                action_type='Outbound',
                item_name=f"{item_name} (Batch {batch.batch_number})",
                material=batch.material,
                quantity_changed=alloc.quantity,
                warehouse=batch.warehouse,
                user=user
            )

            alloc.delete()


def apply_po_material_receipt(po_detail, delta_qty, user):
    """Single source of truth for "goods received against a PO": adds
    delta_qty to quantity_received, creates a Batch for it, and recomputes
    the PO's status (Partially Received / Completed)."""
    import uuid
    from datetime import date, timedelta
    from .models import Batch, WarehouseLocation, RegistryLog

    po = po_detail.purchase_order
    delta_qty = Decimal(str(delta_qty))

    po_detail.quantity_received = (po_detail.quantity_received or Decimal('0')) + delta_qty
    po_detail.save(update_fields=['quantity_received'])

    if delta_qty > 0:
        loc = WarehouseLocation.objects.filter(warehouse=po.target_warehouse).first()
        # None means "track the warehouse's current rate live" (open_batch_rent_expr()
        # falls back to Warehouse.rental_cost_per_mt for null-rate batches). Only a
        # genuine per-PO negotiated rate gets locked in permanently - the warehouse's
        # own standing rate can still be corrected/edited later without stranding
        # existing batches at a stale snapshot.
        rental_rate = po_detail.negotiated_rental_rate_per_mt
        batch = Batch.objects.create(
            batch_number=po_batch_number(po, po_detail.material),
            status='Active',
            material=po_detail.material,
            quantity=delta_qty,
            manufacturing_date=date.today(),
            expiry_date=date.today() + timedelta(days=365),
            warehouse=po.target_warehouse,
            purchase_order=po,
            location=f"Zone {loc.zone_name} Aisle {loc.aisle}" if loc else None,
            rental_rate_per_mt=rental_rate
        )
        RegistryLog.objects.create(
            action_type='Inbound',
            item_name=f"{po_detail.material.name} (Batch {batch.batch_number})",
            material=po_detail.material,
            quantity_changed=delta_qty,
            warehouse=po.target_warehouse,
            user=user
        )

    all_items = po.items.all()
    total_ordered = sum(Decimal(str(i.quantity_ordered)) for i in all_items)
    total_received = sum(Decimal(str(i.quantity_received)) for i in all_items)
    if total_received >= total_ordered:
        po.status = 'Completed'
        if po.completed_date is None:
            po.completed_date = date.today()
        po.save(update_fields=['status', 'completed_date'])
    else:
        if total_received > 0:
            po.status = 'Partially Received'
        po.save(update_fields=['status'])


def po_batch_number(po, material):
    """Short batch id for stock received on a PO: B-<PO ref>-<material ref>-<6 hex>, e.g.
    PO-TS003 + MAT-1005 -> B-TS003-1005-A1B2C3. The redundant PO-/MAT- prefixes are
    dropped; the random tail keeps it unique across receipts of the same PO and material."""
    import uuid
    po_ref = re.sub(r'^PO-', '', po.po_number)
    mat_ref = re.sub(r'^MAT-', '', material.sku)
    return f"B-{po_ref}-{mat_ref}-{uuid.uuid4().hex[:6].upper()}"


def receive_transfer_into_destination(shipment, user):
    """Creates the destination-side batch for each received item of an internal
    Transfer. Shared by complete_shipment and force_close_shipment. Completed
    transfers can't be reopened, so an existing batch means it was already
    received and is left untouched.

    The new batch is stamped with warehouse=destination so the stock counts
    toward that warehouse's utilization, rent and allocation - previously only
    `location` was set, leaving warehouse NULL. It is created whether or not
    the destination has any WarehouseLocation rows (zone left blank), since the
    origin side has already been deducted by this point. rental_rate_per_mt is
    left NULL so it tracks the destination's rate, not the origin's."""
    from .models import Batch, RegistryLog, StockAllocation

    dest = shipment.destination_warehouse
    if shipment.direction != 'Transfer' or not dest:
        return

    for item in shipment.items.select_related('batch', 'batch__material', 'batch__product', 'batch__produced_in'):
        if not item.batch or (item.received_quantity or 0) <= 0:
            continue
        b = item.batch
        rcv_qty = Decimal(str(item.received_quantity))

        # Keep the original batch number and add only this transfer's id: a batch moved
        # again must not stack suffixes (X-TRF-1-TRF-9-...), which grows without bound and
        # would eventually overflow the 100-character batch_number column.
        root_number = re.sub(r'(-TRF-\d+)+$', '', b.batch_number)
        new_batch, created = Batch.objects.get_or_create(
            batch_number=f"{root_number}-TRF-{shipment.id}",
            defaults={
                'status': 'Active',
                'material': b.material,
                'product': b.product,
                'quantity': rcv_qty,
                'manufacturing_date': b.manufacturing_date,
                'expiry_date': b.expiry_date,
                'warehouse': dest,
                'purchase_order': b.purchase_order,
                'produced_in': b.produced_in,
            }
        )
        if not created:
            continue
        RegistryLog.objects.create(
            action_type='Inbound',
            item_name=f"Internal Transfer Received: {new_batch.batch_number}",
            material=new_batch.material,
            quantity_changed=rcv_qty,
            warehouse=dest,
            user=user
        )

        # Receiving deletes the origin allocation, which was also the production run's
        # reservation - carry it onto the arrived batch so the stock stays held for
        # the run instead of sitting unreserved at the plant.
        run = shipment.linked_production_run
        so = shipment.sales_order
        if run and run.status not in ('Completed', 'Cancelled'):
            new_batch.allocated_quantity = rcv_qty
            new_batch.save(update_fields=['allocated_quantity'])
            StockAllocation.objects.create(batch=new_batch, production_run=run, quantity=rcv_qty)
        elif so and so.status not in ('Shipped', 'Delivered', 'Cancelled', 'Rejected'):
            # stock moved to the order's warehouse for one delivery - keep it reserved
            new_batch.allocated_quantity = rcv_qty
            new_batch.save(update_fields=['allocated_quantity'])
            StockAllocation.objects.create(batch=new_batch, sales_order=so, quantity=rcv_qty)

    so = shipment.sales_order
    if so:
        from .models import OrderTimeline
        _resync_so_fulfillment_status(so, user)   # a short delivery can leave it under-covered
        still_moving = so.shipments.filter(direction='Transfer').exclude(status__in=['Completed', 'Cancelled'])
        OrderTimeline.objects.create(
            sales_order=so,
            action=(f"Stock move {shipment.tracking_number} arrived at {dest.name}."
                    + ("" if still_moving.exists() else " All stock is now together - create the logistics order to ship it in one delivery.")),
            user=user,
        )


def create_rent_transfer(origin, destination, candidates, user):
    """Turn Rent Opportunities suggestions into ONE Draft internal transfer from
    `origin` to `destination` carrying the suggested batches, and record each as an
    Accepted RentSuggestion (with the estimate, for comparing with the real
    saving later). `candidates` are rows from analytics.rent_reduction_opportunities().

    Each batch's share is converted from MT to units and capped at what's still
    unreserved, then reserved for the transfer exactly as adding it by hand would
    (batch.allocated_quantity + a shipment-scoped StockAllocation). Returns the
    Shipment, or None if nothing could be moved (stock changed in the meantime)."""
    from decimal import ROUND_DOWN
    from .models import Batch, Shipment, ShipmentItem, StockAllocation, OrderTimeline, RentSuggestion

    with transaction.atomic():
        planned = []
        for c in candidates:
            batch = Batch.objects.select_for_update(of=('self',)).select_related('material', 'product').get(pk=c['batch_id'])
            unit_mt = batch.material.weight_mt_per_unit if batch.material_id else batch.product.weight_mt_per_unit
            if not unit_mt:
                continue
            qty = min(batch.available_quantity,
                      (Decimal(str(c['move_mt'])) / unit_mt).quantize(Decimal('0.01'), rounding=ROUND_DOWN))
            if qty > 0:
                planned.append((batch, c, qty, qty * unit_mt))
        if not planned:
            return None

        shipment = Shipment.objects.create(
            tracking_number=generate_next_code(Shipment, 'tracking_number', 'SHP', 1001, pad=4),
            direction='Transfer', status='Draft',
            origin_warehouse=origin, destination_warehouse=destination, last_edited_by=user,
        )
        total_daily = Decimal('0')
        for batch, c, qty, moved_mt in planned:
            ShipmentItem.objects.create(shipment=shipment, material=batch.material, product=batch.product,
                                        batch=batch, quantity=qty)
            batch.allocated_quantity += qty
            batch.save(update_fields=['allocated_quantity'])
            StockAllocation.objects.create(batch=batch, shipment=shipment, quantity=qty)

            rate = Decimal(str(c['rate_per_mt']))
            daily = (moved_mt * rate).quantize(Decimal('0.01'))
            total_daily += daily
            RentSuggestion.objects.create(
                decision='Accepted', decided_by=user, batch=batch, batch_number=batch.batch_number,
                item_name=(batch.material.name if batch.material_id else batch.product.name),
                origin_warehouse=origin, destination_warehouse=destination, shipment=shipment,
                move_mt=moved_mt.quantize(Decimal('0.001')), rate_per_mt=rate,
                est_daily_saving=daily, est_total_saving=(daily * int(c['stay_days'])),
                est_stay_days=int(c['stay_days']),
            )
        OrderTimeline.objects.create(
            shipment=shipment,
            action=(f"Drafted from a Rent Opportunities suggestion: {len(planned)} batch(es) from {origin.name} "
                    f"to {destination.name}, estimated to save RM {total_daily}/day in rent."),
            user=user,
        )
    return shipment


def dismiss_rent_suggestions(candidates_with_origin, reason, note, user):
    """Record each (origin_warehouse, candidate) as Dismissed with `reason`, and
    leave those batches out of the suggestions for dss_dismiss_snooze_days."""
    from datetime import timedelta
    from .models import Batch, RentSuggestion
    from .settings_store import get_setting

    until = date.today() + timedelta(days=int(get_setting('dss_dismiss_snooze_days')))
    with transaction.atomic():
        for origin, c in candidates_with_origin:
            RentSuggestion.objects.create(
                decision='Dismissed', decided_by=user, batch=Batch.objects.get(pk=c['batch_id']),
                batch_number=c['batch_number'], item_name=c['item'],
                origin_warehouse=origin, destination_warehouse_id=c['destination_id'],
                move_mt=Decimal(str(c['move_mt'])), rate_per_mt=Decimal(str(c['rate_per_mt'])),
                est_daily_saving=Decimal(str(c['daily_saving'])), est_total_saving=Decimal(str(c['total_saving'])),
                est_stay_days=int(c['stay_days']), dismiss_reason=reason, dismiss_note=note[:255],
                snoozed_until=until,
            )
    return until


def apply_so_product_shipment(so_detail, delta_qty):
    """Adds delta_qty to quantity_shipped and recomputes the SO's status
    (Partially Shipped / Shipped), accumulating correctly across several shipments."""
    so = so_detail.sales_order
    delta_qty = Decimal(str(delta_qty))

    so_detail.quantity_shipped = (so_detail.quantity_shipped or Decimal('0')) + delta_qty
    so_detail.save(update_fields=['quantity_shipped'])

    all_items = so.items.all()
    total_ordered = sum(Decimal(str(i.quantity_ordered)) for i in all_items)
    total_shipped = sum(Decimal(str(i.quantity_shipped)) for i in all_items)
    if so.status not in ['Delivered']:
        if total_shipped >= total_ordered:
            so.status = 'Shipped'
        elif total_shipped > 0:
            so.status = 'Partially Shipped'
        so.save(update_fields=['status'])


def mark_so_delivered_if_fully_shipped(so, completing_shipment=None):
    """Marks a SalesOrder Delivered once its Outbound shipment(s) all complete -
    not early, while another shipment against it is still in transit, and not
    while any line still has units owed to the client."""
    outstanding = so.shipments.filter(direction='Outbound').exclude(status__in=['Completed', 'Cancelled'])
    if completing_shipment is not None:
        outstanding = outstanding.exclude(pk=completing_shipment.pk)
    owed = any(
        Decimal(str(i.quantity_shipped or 0)) < Decimal(str(i.quantity_ordered))
        for i in so.items.all()
    )

    if not outstanding.exists() and not owed:
        so.status = 'Delivered'
        so.save(update_fields=['status'])
        return True
    return False


