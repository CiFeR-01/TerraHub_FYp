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
        batches = batches.filter(location__warehouse=warehouse)
        
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

def deduct_stock_from_allocation(order_type, order):
    """
    Permanently deducts the allocated stock from the physical batch quantities,
    typically when an order is shipped or a production run is completed.
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
            batch.quantity -= alloc.quantity
            batch.allocated_quantity -= alloc.quantity
            batch.save(update_fields=['quantity', 'allocated_quantity'])
            alloc.delete()


