from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, F, Case, When, Value, DecimalField, Count
from django.db.models.functions import Coalesce
from datetime import date
from .models import Warehouse, Batch, SalesOrder, Shipment, RegistryLog

@login_required
def dashboard_view(request):
    # 1. Warehouse Space & Cost Optimization Metrics (used_mt sum of total_weight_mt for active batches)
    used_mt_annotation = Coalesce(
        Sum(
            Case(
                When(
                    locations__batch__status='Active',
                    locations__batch__material__isnull=False,
                    then=F('locations__batch__quantity') * F('locations__batch__material__weight_mt_per_unit')
                ),
                When(
                    locations__batch__status='Active',
                    locations__batch__product__isnull=False,
                    then=F('locations__batch__quantity') * F('locations__batch__product__weight_mt_per_unit')
                ),
                default=Value(0),
                output_field=DecimalField()
            )
        ),
        Value(0, output_field=DecimalField())
    )


    warehouses = Warehouse.objects.annotate(
        used_mt=used_mt_annotation
    ).order_by('name')

    warehouse_stats = []
    total_capacity = 0.0
    total_used = 0.0
    total_daily_cost = 0.0

    for w in warehouses:
        used_mt = float(w.used_mt)
        capacity_mt = float(w.total_capacity_mt)
        total_capacity += capacity_mt
        total_used += used_mt
        
        # Scale rental cost: 0 if Internal; capacity * cost if Overall; used * cost if Usage
        if w.ownership_type == 'Internal':
            daily_cost = 0.0
            billing_mode = "Internal"
        elif w.rental_billing_method == 'Overall':
            daily_cost = float(w.total_capacity_mt * w.rental_cost_per_mt)
            billing_mode = "Overall Capacity"
        else: # Usage
            daily_cost = float(w.used_mt * w.rental_cost_per_mt)
            billing_mode = "Usage"
            
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
            'daily_cost': daily_cost,
            'billing_mode': billing_mode,
            'utilization_percent': utilization_percent,
        })

    # chart_data dict formatted for safe JSON injection
    chart_data = {
        'labels': [item['name'] for item in warehouse_stats],
        'capacities': [item['capacity_mt'] for item in warehouse_stats],
        'used': [item['used_mt'] for item in warehouse_stats],
    }

    # 2. Inventory Metrics
    raw_materials_sum = Batch.objects.filter(status='Active', material__isnull=False).aggregate(total=Sum('quantity'))['total'] or 0
    finished_goods_sum = Batch.objects.filter(status='Active', product__isnull=False).aggregate(total=Sum('quantity'))['total'] or 0

    inventory_metrics = {
        'raw_materials': float(raw_materials_sum),
        'finished_goods': float(finished_goods_sum),
    }

    # Global utilization percent
    if total_capacity > 0:
        global_utilization = (total_used / total_capacity) * 100
    else:
        global_utilization = 0.0

    # 3. Recent logs (pre-fetching related warehouse models, ordered descending by timestamp)
    recent_logs = RegistryLog.objects.select_related('warehouse').order_by('-timestamp')[:5]

    # 4. Active shipments (status is not 'Arrived', ordered by expected_eta_date)
    active_shipments = Shipment.objects.exclude(status='Arrived').select_related(
        'material', 'product', 'origin_warehouse', 'destination_warehouse'
    ).order_by('expected_eta_date')

    # 5. Degrading batches (active batches, <= 30 days remaining shelf life OR less than material.safe_storage_days)
    active_batches = Batch.objects.filter(status='Active').select_related('material', 'product', 'location__warehouse')
    today = date.today()
    degrading_batches = []
    for b in active_batches:
        if b.expiry_date:
            days_remaining = (b.expiry_date - today).days
            # Filter condition
            if days_remaining <= 30 or (b.material and days_remaining < b.material.safe_storage_days):
                b.days_remaining = days_remaining
                b.threshold_days = b.material.safe_storage_days if b.material else 30
                degrading_batches.append(b)

    # 6. Sales order stats
    so_counts = SalesOrder.objects.values('status').annotate(count=Count('id'))
    counts_dict = {item['status']: item['count'] for item in so_counts}
    
    sales_order_stats = {
        'Draft': counts_dict.get('Draft', 0),
        'Pending_Approval': counts_dict.get('Pending Approval', 0),
        'Pending_Approved': counts_dict.get('Pending', 0),
        'In_Production': counts_dict.get('Awaiting Acknowledgement', 0) + counts_dict.get('In Production', 0),
        'Ready_to_Ship': counts_dict.get('Ready to Ship', 0),
        'Shipped_Delivered': counts_dict.get('Shipped', 0) + counts_dict.get('Delivered', 0),
    }

    context = {
        'warehouse_stats': warehouse_stats,
        'chart_data': chart_data,
        'inventory_metrics': inventory_metrics,
        'global_utilization': global_utilization,
        'total_daily_cost': total_daily_cost,
        'recent_logs': recent_logs,
        'active_shipments': active_shipments,
        'degrading_batches': degrading_batches,
        'sales_order_stats': sales_order_stats,
        'current_timestamp': date.today().strftime('%Y-%m-%d'),
    }

    return render(request, 'dashboard.html', context)

@login_required
def system_view(request):
    return render(request, 'system.html')

def home_view(request):
    return render(request, 'home.html')
