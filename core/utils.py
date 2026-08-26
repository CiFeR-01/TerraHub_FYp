import re
from decimal import Decimal
from django.db import transaction
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
    """
    For each line item on `so` not yet fully covered by StockAllocations, create a
    Production Run to manufacture the shortfall (unless one already exists for that
    SO+product). Runs are created as 'Pending Approval' — same as every other manual
    production run — so a manager reviews them in the Approvals Inbox before anyone
    can start allocating materials against them. Returns True if any run was created.
    """
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


def consume_materials_for_run(run, user):
    """
    Physically deducts the raw materials a completed run used. For each material
    in the run's recipe: consumes from whatever's still allocated to this run
    (releasing any unused portion back to availability), then — for materials
    whose allocation was already resolved by an arrived transfer shipment, or
    where actual usage exceeded what was allocated — draws the remainder from
    the plant's current stock via FEFO. Uses RunMaterialUsage.actual_qty when
    available (the detailed completion form), otherwise assumes the allocated
    amount was used as planned (the quick-complete path). Records a
    ProductionConsumption row per batch drawn from and a RegistryLog entry per
    material.
    """
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
            batch.save(update_fields=['quantity', 'allocated_quantity'])
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
                batch.save(update_fields=['quantity'])
                ProductionConsumption.objects.create(production_run=run, consumed_batch=batch, quantity_used=take)
                remaining -= take

        consumed = actual_used - remaining
        if consumed > 0:
            RegistryLog.objects.create(
                action_type='Consumed_For_Manufacturing',
                item_name=f"{material.name} (Run {run.run_number})",
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
    """
    Approves a 'Pending Approval' production run, clearing it for material
    allocation via the FEFO allocation screen (production_run_allocate_view),
    which already handles both local and cross-warehouse sourcing correctly —
    so approval itself doesn't need its own separate material check.
    """
    from .models import OrderTimeline
    run.status = 'Pending Allocation'
    run.save()
    OrderTimeline.objects.create(
        production_run=run,
        action=f"Production Run {run.run_number} approved. Ready for material allocation.",
        user=user
    )


def finalize_production_run(run, user):
    """
    Completes a production run: creates the finished-goods batch, marks the run
    Completed, and — if the run is linked to a SalesOrder — allocates the new
    batch directly against that order's matching line item (not a generic FEFO
    sweep, since this stock was produced specifically for it) and advances the
    order's status. Returns the created batch (or None if there was no yield).
    """
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
                        action=f"Auto-allocated {take} {item.product.unit_of_measure} of {item.product.sku} from newly produced batch {fg_batch.batch_number} (Run {run.run_number}).",
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
    """
    Permanently deducts the allocated stock from the physical batch quantities,
    typically when an order is shipped or a production run is completed. Logs one
    RegistryLog entry per batch so the Outbound movement actually shows up in the
    Registry Ledger — this previously deducted stock silently.
    """
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
            batch.save(update_fields=['quantity', 'allocated_quantity'])

            item_name = batch.material.name if batch.material else (batch.product.name if batch.product else batch.batch_number)
            RegistryLog.objects.create(
                action_type='Outbound',
                item_name=f"{item_name} (Batch {batch.batch_number})",
                quantity_changed=-alloc.quantity,
                warehouse=batch.warehouse,
                user=user
            )

            alloc.delete()


def apply_po_material_receipt(po_detail, delta_qty, user):
    """
    Adds delta_qty to a PurchaseOrderDetail's quantity_received, creates a Batch for
    the delta at the PO's target warehouse, and recomputes the parent PO's status
    (Partially Received / Completed). This is the single source of truth for "goods
    received against a PO" — used both by the PO page's own "mark received" action
    and by Shipment-side receipt logging, so quantities/batches/status stay
    consistent no matter which page was used to record it.
    """
    import uuid
    from datetime import date, timedelta
    from .models import Batch, WarehouseLocation, RegistryLog

    po = po_detail.purchase_order
    delta_qty = Decimal(str(delta_qty))

    po_detail.quantity_received = (po_detail.quantity_received or Decimal('0')) + delta_qty
    po_detail.save(update_fields=['quantity_received'])

    if delta_qty > 0:
        loc = WarehouseLocation.objects.filter(warehouse=po.target_warehouse).first()
        batch = Batch.objects.create(
            batch_number=f"B-{po.po_number}-{po_detail.material.sku}-{uuid.uuid4().hex[:6].upper()}",
            status='Active',
            material=po_detail.material,
            quantity=delta_qty,
            manufacturing_date=date.today(),
            expiry_date=date.today() + timedelta(days=365),
            warehouse=po.target_warehouse,
            purchase_order=po,
            location=f"Zone {loc.zone_name} Aisle {loc.aisle}" if loc else None
        )
        RegistryLog.objects.create(
            action_type='Inbound',
            item_name=f"{po_detail.material.name} (Batch {batch.batch_number})",
            quantity_changed=delta_qty,
            warehouse=po.target_warehouse,
            user=user
        )

    all_items = po.items.all()
    total_ordered = sum(Decimal(str(i.quantity_ordered)) for i in all_items)
    total_received = sum(Decimal(str(i.quantity_received)) for i in all_items)
    if total_received >= total_ordered:
        po.status = 'Completed'
    elif total_received > 0:
        po.status = 'Partially Received'
    po.save(update_fields=['status'])


def apply_so_product_shipment(so_detail, delta_qty):
    """
    Adds delta_qty to a SalesOrderDetail's quantity_shipped and recomputes the parent
    SO's status (Partially Shipped / Shipped). Called when a linked Outbound shipment
    actually dispatches, so an SO fulfilled across several shipments over time (partial
    deliveries) accumulates correctly instead of the status flipping to "Shipped" on
    the first truck regardless of how much was actually sent.
    """
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
    """
    Marks a SalesOrder Delivered once a linked Outbound shipment actually completes
    (delivery confirmed) — but only if no other shipment against the same SO is still
    outstanding, so a partially-fulfilled SO with a second truck still in transit
    correctly stays at Shipped/Partially Shipped instead of jumping to Delivered early.
    """
    outstanding = so.shipments.filter(direction='Outbound').exclude(status__in=['Completed', 'Cancelled'])
    if completing_shipment is not None:
        outstanding = outstanding.exclude(pk=completing_shipment.pk)

    if not outstanding.exists():
        so.status = 'Delivered'
        so.save(update_fields=['status'])
        return True
    return False


