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
    batches = batches.order_by(F('expiry_date').asc(nulls_last=True), 'manufacturing_date')

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
        prev_allocated = StockAllocation.objects.filter(
            sales_order=so, batch__product=item.product
        ).aggregate(s=Sum('quantity'))['s'] or Decimal('0')
        unfulfilled = qty_needed - Decimal(str(prev_allocated))

        if unfulfilled > 0:
            pr = ProductionRun.objects.filter(sales_order=so, target_product=item.product).exclude(status='Cancelled').first()
            if not pr:
                run_number = f"PR-{so.so_number}-{item.product.sku}"
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


def release_production_run_allocations(run):
    """Releases every StockAllocation held by a run back to stock, and cancels
    any auto-generated Draft transfer shipments left over from allocating it."""
    from .models import StockAllocation, Shipment

    allocs = StockAllocation.objects.filter(production_run=run)
    for alloc in allocs:
        batch = alloc.batch
        batch.allocated_quantity -= alloc.quantity
        if batch.allocated_quantity < 0:
            batch.allocated_quantity = 0
        batch.save(update_fields=['allocated_quantity'])
        alloc.delete()

    Shipment.objects.filter(
        linked_production_run=run, is_auto_generated=True, status='Draft'
    ).update(status='Cancelled')


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
        release_production_run_allocations(run)
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
        release_production_run_allocations(run)
        run.status = 'Cancelled'
        run.save(update_fields=['status'])
        OrderTimeline.objects.create(
            production_run=run,
            action=f"Auto-cancelled: {product.sku} was removed from {so.so_number}, this run is no longer needed.",
            user=user
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
            batch.allocated_quantity -= alloc.quantity
            if batch.allocated_quantity < 0:
                batch.allocated_quantity = 0
            _close_batch_if_depleted(batch)
            batch.save(update_fields=['quantity', 'allocated_quantity', 'status', 'closed_date'])
            alloc.delete()

        if remaining > 0 and run.manufacturing_plant:
            extra_batches = Batch.objects.filter(
                material=material, status='Active', warehouse=run.manufacturing_plant
            ).annotate(avail=F('quantity') - F('allocated_quantity')).filter(avail__gt=0).order_by(
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

        consumed = actual_used - remaining
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


def approve_production_run(run, user):
    """Approves a 'Pending Approval' run, clearing it for material allocation
    via the FEFO allocation screen."""
    from .models import OrderTimeline
    run.status = 'Pending Allocation'
    run.save()
    OrderTimeline.objects.create(
        production_run=run,
        action=f"Production Run {run.run_number} approved. Ready for material allocation.",
        user=user
    )


def finalize_production_run(run, user):
    """Completes a run: creates the finished-goods batch, marks it Completed,
    and (if linked to a SalesOrder) allocates the batch to that order's line
    item and advances its status. Returns the batch, or None if no yield."""
    import uuid
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import Sum
    from .models import Batch, StockAllocation, OrderTimeline, RegistryLog

    consume_materials_for_run(run, user)

    fg_batch = None
    if run.actual_yield:
        fg_batch = Batch.objects.create(
            batch_number=f"FG-{run.run_number}-{str(uuid.uuid4())[:4]}",
            status='Active',
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

    if run.sales_order and fg_batch:
        so = run.sales_order
        item = so.items.filter(product=run.target_product).first()

        if item:
            already_allocated = StockAllocation.objects.filter(
                sales_order=so, batch__product=item.product
            ).aggregate(s=Sum('quantity'))['s'] or Decimal('0')
            outstanding = Decimal(str(item.quantity_ordered)) - Decimal(str(already_allocated)) - Decimal(str(item.quantity_shipped))

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
            alloc_sum = StockAllocation.objects.filter(
                sales_order=so, batch__product=it.product
            ).aggregate(s=Sum('quantity'))['s'] or Decimal('0')
            if Decimal(str(alloc_sum)) + Decimal(str(it.quantity_shipped)) < Decimal(str(it.quantity_ordered)):
                fully_covered = False
                break

        if so.status not in ['Shipped', 'Delivered', 'Cancelled', 'Rejected', 'Draft']:
            so.status = 'Ready to Ship' if fully_covered else 'Pending'
            so.save()

        OrderTimeline.objects.create(
            sales_order=so,
            action=f"Production Run {run.run_number} completed. FG batch {fg_batch.batch_number} created."
                   + (" Order fully covered — moved to Ready to Ship." if fully_covered else " Order still has outstanding items."),
            user=user
        )

    return fg_batch


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
        alloc_sum = StockAllocation.objects.filter(sales_order=so, batch__product=it.product).aggregate(s=Sum('quantity'))['s'] or Decimal('0')
        if Decimal(str(alloc_sum)) + Decimal(str(it.quantity_shipped)) < Decimal(str(it.quantity_ordered)):
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
            batch_number=f"B-{po.po_number}-{po_detail.material.sku}-{uuid.uuid4().hex[:6].upper()}",
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

        new_batch, created = Batch.objects.get_or_create(
            batch_number=f"{b.batch_number}-TRF-{shipment.id}",
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
        if run and run.status not in ('Completed', 'Cancelled'):
            new_batch.allocated_quantity = rcv_qty
            new_batch.save(update_fields=['allocated_quantity'])
            StockAllocation.objects.create(batch=new_batch, production_run=run, quantity=rcv_qty)


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
    not early, while another shipment against it is still in transit."""
    outstanding = so.shipments.filter(direction='Outbound').exclude(status__in=['Completed', 'Cancelled'])
    if completing_shipment is not None:
        outstanding = outstanding.exclude(pk=completing_shipment.pk)

    if not outstanding.exists():
        so.status = 'Delivered'
        so.save(update_fields=['status'])
        return True
    return False


