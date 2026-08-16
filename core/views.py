from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django import forms
from django.db import transaction
from django.db.models import Sum, F, Case, When, Value, DecimalField, Count, Q
from django.db.models.functions import Coalesce
from datetime import date, timedelta
import csv
import io
from .models import (
    CustomUser, Warehouse, WarehouseLocation, Material, Product,
    ProductRecipe, ProductionRun, ProductionConsumption, Batch,
    PurchaseOrder, PurchaseOrderDetail, SalesOrder, SalesOrderDetail,
    Shipment, ShipmentItem, StockAudit, RegistryLog, OrderTimeline, Notification, Role
)
from .utils import generate_next_code


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
        'origin_warehouse', 'destination_warehouse'
    ).prefetch_related('items').order_by('expected_eta_date')

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
    system_metrics = {
        'total_users': CustomUser.objects.count(),
        'total_warehouses': Warehouse.objects.count(),
        'total_batches': Batch.objects.count(),
        'total_logs': RegistryLog.objects.count(),
        'total_sales_orders': SalesOrder.objects.count(),
        'total_purchase_orders': PurchaseOrder.objects.count(),
        'total_shipments': Shipment.objects.count(),
        'total_materials': Material.objects.count(),
        'total_products': Product.objects.count(),
    }
    recent_registry = RegistryLog.objects.select_related('user', 'warehouse').order_by('-timestamp')[:8]
    
    from .db_tracker import get_db_status, DB_QUERY_LOGS
    db_status = get_db_status()
    initial_logs = list(DB_QUERY_LOGS)
    
    return render(request, 'system.html', {
        'metrics': system_metrics,
        'recent_logs': recent_registry,
        'db_status': db_status,
        'db_logs': initial_logs,
    })


from django.http import JsonResponse
import datetime

@login_required
def db_logs_api_view(request):
    from .db_tracker import DB_QUERY_LOGS, get_db_status
    db_status = get_db_status()
    return JsonResponse({
        'logs': list(DB_QUERY_LOGS),
        'db_status': db_status
    })


@login_required
def db_clear_logs_view(request):
    from .db_tracker import DB_QUERY_LOGS
    DB_QUERY_LOGS.clear()
    return JsonResponse({'status': 'success'})


@login_required
def db_test_op_view(request):
    op_type = request.GET.get('type', 'read')
    if op_type == 'write':
        from .models import Notification
        Notification.objects.create(
            user=request.user,
            message=f"DB Write Telemetry Test at {datetime.datetime.now().strftime('%H:%M:%S')}",
            link="",
            is_read=True
        )
        msg = "Write query (INSERT Notification) executed successfully."
    else:
        from .models import CustomUser
        _ = list(CustomUser.objects.filter(id=request.user.id))
        msg = "Read query (SELECT CustomUser) executed successfully."
        
    return JsonResponse({'status': 'success', 'message': msg})



def home_view(request):
    return render(request, 'home.html')


@login_required
def warehouse_inventory_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'manual_receive':
            if not request.user.can_adjust_physical_stock:
                messages.error(request, "Permission Denied: You cannot manually adjust stock.")
                return redirect('warehouse_inventory')
            
            material_id = request.POST.get('material_id')
            product_id = request.POST.get('product_id')
            qty = request.POST.get('quantity')
            wh_id = request.POST.get('warehouse_id')
            expiry = request.POST.get('expiry_date')
            
            if not qty or float(qty) <= 0:
                messages.error(request, "Invalid quantity.")
                return redirect('warehouse_inventory')
                
            try:
                loc = WarehouseLocation.objects.filter(warehouse_id=wh_id).first()
                if not loc:
                    messages.error(request, "Target warehouse has no defined locations.")
                    return redirect('warehouse_inventory')
                
                if material_id:
                    mat = get_object_or_404(Material, id=material_id)
                    b = Batch.objects.create(
                        batch_number=f"M-ADJ-{mat.sku}-{date.today().strftime('%Y%m%d')}",
                        status='Active',
                        material=mat,
                        quantity=float(qty),
                        manufacturing_date=date.today(),
                        expiry_date=expiry if expiry else date.today() + timedelta(days=365),
                        location=loc
                    )
                    log_item = mat.name
                elif product_id:
                    prod = get_object_or_404(Product, id=product_id)
                    b = Batch.objects.create(
                        batch_number=f"P-ADJ-{prod.sku}-{date.today().strftime('%Y%m%d')}",
                        status='Active',
                        product=prod,
                        quantity=float(qty),
                        manufacturing_date=date.today(),
                        expiry_date=expiry if expiry else date.today() + timedelta(days=365),
                        location=loc
                    )
                    log_item = prod.name
                else:
                    messages.error(request, "Must select either Material or Product.")
                    return redirect('warehouse_inventory')
                    
                RegistryLog.objects.create(
                    action_type='Adjusted',
                    item_name=f"Manual Receipt of {log_item}",
                    quantity_changed=float(qty),
                    warehouse=loc.warehouse,
                    user=request.user
                )
                messages.success(request, f"Successfully received {qty} of {log_item}.")
            except Exception as e:
                messages.error(request, f"Error receiving stock: {e}")
            return redirect('warehouse_inventory')

    warehouses = Warehouse.objects.all().order_by('name')
    warehouse_id = request.GET.get('warehouse_id')
    selected_warehouse = None
    batches = []
    global_kpis = None

    if warehouse_id:
        try:
            selected_warehouse = Warehouse.objects.get(id=warehouse_id)
            batches = Batch.objects.filter(location__warehouse=selected_warehouse, status='Active').select_related(
                'material', 'product', 'location', 'location__warehouse'
            ).order_by('location__zone_name', 'location__aisle')
        except Warehouse.DoesNotExist:
            pass

    if not selected_warehouse:
        batches = Batch.objects.filter(status='Active').select_related(
            'material', 'product', 'location', 'location__warehouse'
        ).order_by('-manufacturing_date')
        
        from django.db.models import Sum
        total_cap = Warehouse.objects.aggregate(t=Sum('total_capacity_mt'))['t'] or 0
        global_kpis = {
            'total_warehouses': warehouses.count(),
            'total_capacity': total_cap,
            'total_batches': batches.count(),
        }

    can_adjust = request.user.can_adjust_physical_stock

    context = {
        'warehouses': warehouses,
        'materials': Material.objects.all().order_by('name'),
        'products': Product.objects.all().order_by('name'),
        'selected_warehouse': selected_warehouse,
        'batches': batches,
        'global_kpis': global_kpis,
        'can_adjust': can_adjust,
    }
    return render(request, 'warehouse_inventory.html', context)

@login_required
def batch_detail_view(request, batch_number):
    from django.shortcuts import get_object_or_404
    batch = get_object_or_404(Batch.objects.select_related(
        'material', 'product', 'location', 'location__warehouse', 'purchase_order', 'produced_in', 'produced_in__manufacturing_plant'
    ), batch_number=batch_number)
    
    logs = RegistryLog.objects.filter(item_name__icontains=batch.batch_number).select_related('user', 'warehouse').order_by('-timestamp')
    
    context = {
        'batch': batch,
        'logs': logs,
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
        used_mt=used_mt_annotation,
        zone_count=Count('locations', distinct=True),
        batch_count=Count('locations__batch', distinct=True, filter=Q(locations__batch__status='Active')),
    ).order_by('name')

    facility_list = []
    total_capacity = 0.0
    total_used = 0.0
    total_daily_cost = 0.0

    for w in warehouses:
        used = float(w.used_mt)
        cap = float(w.total_capacity_mt)
        total_capacity += cap
        total_used += used
        util = (used / cap * 100) if cap > 0 else 0.0

        if w.ownership_type == 'Internal':
            daily_cost = 0.0
        elif w.rental_billing_method == 'Overall':
            daily_cost = float(w.total_capacity_mt * w.rental_cost_per_mt)
        else:
            daily_cost = float(w.used_mt * w.rental_cost_per_mt)
        total_daily_cost += daily_cost

        facility_list.append({
            'id': w.id,
            'name': w.name,
            'location_type': w.get_location_type_display(),
            'ownership_type': w.get_ownership_type_display(),
            'raw_ownership': w.ownership_type,
            'capacity_mt': cap,
            'used_mt': used,
            'utilization': util,
            'daily_cost': daily_cost,
            'billing_method': w.get_rental_billing_method_display(),
            'cost_per_mt': float(w.rental_cost_per_mt),
            'zone_count': w.zone_count,
            'batch_count': w.batch_count,
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


# --------------------------------------------------------------------------
# STOCK AUDIT (TALLY)
# --------------------------------------------------------------------------
@login_required
def stock_audit_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create':
            batch_id = request.POST.get('batch_id')
            try:
                actual_qty = float(request.POST.get('actual_quantity', 0))
                batch = get_object_or_404(Batch, id=batch_id)
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
            if audit.status == 'Pending':
                with transaction.atomic():
                    variance = audit.variance
                    audit.status = 'Resolved'
                    audit.save()
                    
                    b = audit.batch
                    b.quantity = audit.actual_quantity
                    b.save()

                    wh = b.location.warehouse if b.location else None
                    RegistryLog.objects.create(
                        action_type='Adjusted',
                        item_name=f"Batch {b.batch_number} ({b.material or b.product})",
                        quantity_changed=variance,
                        warehouse=wh,
                        user=request.user
                    )
                messages.success(request, f"Audit #{audit.id} resolved. Physical stock updated.")

        return redirect('stock_audit')

    audits = StockAudit.objects.select_related('batch', 'auditor', 'batch__location__warehouse').order_by('-audit_date')
    active_batches = Batch.objects.filter(status='Active').select_related('material', 'product', 'location')
    
    pending_count = audits.filter(status='Pending').count()
    resolved_count = audits.filter(status='Resolved').count()

    context = {
        'audits': audits,
        'active_batches': active_batches,
        'pending_count': pending_count,
        'resolved_count': resolved_count,
    }
    return render(request, 'stock_audit.html', context)


# --------------------------------------------------------------------------
# REGISTRY LEDGER
# --------------------------------------------------------------------------
@login_required
def registry_ledger_view(request):
    action_filter = request.GET.get('action')
    search_query = request.GET.get('q', '').strip()
    
    logs = RegistryLog.objects.select_related('warehouse', 'user').order_by('-timestamp')
    if action_filter:
        logs = logs.filter(action_type=action_filter)
    if search_query:
        logs = logs.filter(Q(item_name__icontains=search_query) | Q(warehouse__name__icontains=search_query))

    stats = {
        'inbound': RegistryLog.objects.filter(action_type='Inbound').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'outbound': RegistryLog.objects.filter(action_type='Outbound').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'consumed': RegistryLog.objects.filter(action_type='Consumed_For_Manufacturing').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'produced': RegistryLog.objects.filter(action_type='Produced').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
        'disposed': RegistryLog.objects.filter(action_type='Spoiled_Disposal').aggregate(s=Coalesce(Sum('quantity_changed'), Value(0, output_field=DecimalField())))['s'],
    }

    context = {
        'logs': logs,
        'action_filter': action_filter,
        'search_query': search_query,
        'stats': stats,
        'action_choices': RegistryLog.ACTION_CHOICES,
    }
    return render(request, 'registry_ledger.html', context)


# --------------------------------------------------------------------------
# BULK IMPORT / EXPORT & REFERENCE TEMPLATE HANDLERS
# --------------------------------------------------------------------------

@login_required
def export_product_template(request):
    """Download reference CSV template for Products."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="product_import_template.csv"'
    writer = csv.writer(response)
    writer.writerow(['name', 'sku', 'description', 'unit_of_measure', 'weight_mt_per_unit', 'price_per_unit'])
    writer.writerow(['Polymer Compound Alpha', 'PROD1001', 'High density industrial resin', 'pcs', '0.5000', '150.00'])
    writer.writerow(['Bio-Solvent Solution', 'PROD1002', 'Organic chemical solvent', 'L', '1.0000', '85.50'])
    writer.writerow(['Composite Sheet Grade B', '', 'Standard structural panel (Auto SKU)', 'pcs', '0.2500', '45.00'])
    return response


@login_required
def export_products_csv(request):
    """Export all registered Products as CSV."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="products_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['name', 'sku', 'description', 'unit_of_measure', 'weight_mt_per_unit', 'price_per_unit'])
    for prod in Product.objects.all().order_by('sku'):
        writer.writerow([
            prod.name,
            prod.sku,
            prod.description or '',
            prod.unit_of_measure,
            f"{prod.weight_mt_per_unit:.4f}",
            f"{prod.price_per_unit:.2f}"
        ])
    return response


@login_required
def import_products(request):
    """Bulk import Products from uploaded CSV with dry-run, anti-duplication, and error reporting."""
    if request.method != 'POST':
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

            try:
                weight = float(weight_str) if weight_str else 1.0
                if weight < 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: Weight MT/Unit must be a non-negative number."
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
                    unit_of_measure=uom, weight_mt_per_unit=weight, price_per_unit=price
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

            try:
                weight = float(weight_str) if weight_str else 1.0
                if weight < 0: raise ValueError()
            except ValueError:
                err = f"Line {idx}: Weight MT/Unit must be a non-negative number."
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
            f"{r.quantity_required:.2f}",
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


@login_required
def save_product_recipe_api(request):
    """API endpoint for live inline recipe modifications (add, update, delete, clone)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST method required.'}, status=400)

    action = request.POST.get('action')
    product_id = request.POST.get('product_id')

    if action == 'delete_item':
        recipe_id = request.POST.get('recipe_id')
        try:
            item = get_object_or_404(ProductRecipe, id=recipe_id)
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
            
            source_items = ProductRecipe.objects.filter(product=source_prod)
            cloned_count = 0
            for item in source_items:
                ProductRecipe.objects.update_or_create(
                    product=target_prod,
                    material=item.material,
                    defaults={'quantity_required': item.quantity_required}
                )
                cloned_count += 1

            return JsonResponse({
                'success': True,
                'message': f"Successfully cloned {cloned_count} requirement(s) from {source_prod.sku} to {target_prod.sku}.",
                'product_id': target_prod.id
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)}, status=400)

    elif action == 'save_batch':
        try:
            product = get_object_or_404(Product, id=product_id)
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


# --------------------------------------------------------------------------
# PRODUCTS CATALOG
# --------------------------------------------------------------------------
@login_required
def product_list_view(request):
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
            try:
                weight = float(request.POST.get('weight_mt_per_unit', 1.0))
                price = float(request.POST.get('price_per_unit', 0.0))
                Product.objects.create(
                    name=name, sku=sku, description=description,
                    unit_of_measure=uom, weight_mt_per_unit=weight, price_per_unit=price
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
                ProductRecipe.objects.create(product=prod, material=mat, quantity_required=float(qty))
                messages.success(request, f"Recipe requirement of {qty} {mat.sku} added for {prod.sku}.")
            except Exception as e:
                messages.error(request, f"Error adding recipe item: {e}")

        return redirect('product_list')

    products = Product.objects.prefetch_related('recipe_items__material').order_by('name')
    materials = Material.objects.all().order_by('name')

    context = {
        'products': products,
        'materials': materials,
        'next_product_sku': generate_next_code(Product, 'sku', 'PROD', 1001, pad=4),
    }
    return render(request, 'product_list.html', context)


@login_required
def product_detail_view(request, pk):
    product = get_object_or_404(Product.objects.prefetch_related('recipe_items__material'), pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'update_product':
            try:
                product.name = request.POST.get('name', product.name)
                product.sku = request.POST.get('sku', product.sku)
                product.description = request.POST.get('description', product.description)
                product.unit_of_measure = request.POST.get('unit_of_measure', product.unit_of_measure)
                product.weight_mt_per_unit = float(request.POST.get('weight_mt_per_unit', product.weight_mt_per_unit))
                product.price_per_unit = float(request.POST.get('price_per_unit', product.price_per_unit))
                product.save()
                messages.success(request, f"Product '{product.sku}' updated successfully.")
            except Exception as e:
                messages.error(request, f"Error updating product: {e}")
        return redirect('product_detail', pk=pk)

    # Inventory Overview
    active_batches = Batch.objects.filter(product=product, status='Active').select_related('location__warehouse').order_by('expiry_date')
    total_stock = sum(b.quantity for b in active_batches)

    # Manufacturing History
    production_runs = ProductionRun.objects.filter(target_product=product).select_related('supervisor', 'sales_order').order_by('-id')[:10]
    
    # Sales Orders containing this product for the Chart
    sales_details = SalesOrderDetail.objects.filter(product=product).select_related('sales_order')
    
    # Aggregate sales by month for the chart
    sales_data = []
    for sd in sales_details:
        if sd.sales_order.order_date:
            sales_data.append({
                'date': sd.sales_order.order_date.strftime('%Y-%m'),
                'quantity': float(sd.quantity_ordered)
            })
    sales_by_month = {}
    for item in sales_data:
        m = item['date']
        sales_by_month[m] = sales_by_month.get(m, 0) + item['quantity']
        
    chart_labels = sorted(sales_by_month.keys())
    chart_sales_data = [sales_by_month[lbl] for lbl in chart_labels]

    context = {
        'product': product,
        'products': Product.objects.all().order_by('name'),
        'materials': Material.objects.all().order_by('name'),
        'active_batches': active_batches,
        'total_stock': total_stock,
        'production_runs': production_runs,
        'chart_labels': chart_labels,
        'chart_sales_data': chart_sales_data,
        'uom_choices': Product.UNIT_CHOICES,
    }
    return render(request, 'product_detail.html', context)


# --------------------------------------------------------------------------
# MATERIALS HUB
# --------------------------------------------------------------------------
@login_required
def material_list_view(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        sku_auto = request.POST.get('sku_auto') == '1'
        sku = request.POST.get('sku')
        if sku_auto or not sku:
            sku = generate_next_code(Material, 'sku', 'MAT', 1001, pad=4)
        category = request.POST.get('category')
        uom = request.POST.get('unit_of_measure', 'MT')
        try:
            safe_days = int(request.POST.get('safe_storage_days', 90))
            weight = float(request.POST.get('weight_mt_per_unit', 1.0))
            cost = float(request.POST.get('cost_per_unit', 0.0))
            Material.objects.create(
                name=name, sku=sku, category=category, unit_of_measure=uom,
                safe_storage_days=safe_days, weight_mt_per_unit=weight, cost_per_unit=cost
            )
            messages.success(request, f"Material '{name}' (SKU: {sku}) registered.")
        except Exception as e:
            messages.error(request, f"Error registering material: {e}")
        return redirect('material_list')

    materials = Material.objects.all().order_by('name')
    
    # Calculate stock totals per material
    material_data = []
    for m in materials:
        total_qty = Batch.objects.filter(material=m, status='Active').aggregate(s=Sum('quantity'))['s'] or 0
        material_data.append({
            'material': m,
            'current_stock': float(total_qty),
            'total_mt': float(total_qty * m.weight_mt_per_unit),
            'total_value': float(total_qty * m.cost_per_unit),
        })

    context = {
        'material_data': material_data,
        'next_material_sku': generate_next_code(Material, 'sku', 'MAT', 1001, pad=4),
    }
    return render(request, 'material_list.html', context)


@login_required
def material_edit_view(request, pk):
    """View to edit an existing raw material record."""
    material = get_object_or_404(Material, pk=pk)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        sku = request.POST.get('sku', '').strip().upper()
        category = request.POST.get('category', '').strip()
        uom = request.POST.get('unit_of_measure', 'MT')
        
        try:
            safe_days = int(request.POST.get('safe_storage_days', 90))
            weight = float(request.POST.get('weight_mt_per_unit', 1.0))
            cost = float(request.POST.get('cost_per_unit', 0.0))

            if not name:
                messages.error(request, "Material name is required.")
                return redirect('material_list')

            if not sku:
                sku = material.sku

            # Check SKU uniqueness against other materials
            if Material.objects.filter(sku=sku).exclude(pk=material.pk).exists():
                messages.error(request, f"SKU '{sku}' is already assigned to another material.")
                return redirect('material_list')

            material.name = name
            material.sku = sku
            material.category = category or material.category
            material.unit_of_measure = uom
            material.safe_storage_days = safe_days
            material.weight_mt_per_unit = weight
            material.cost_per_unit = cost
            material.save()

            RegistryLog.objects.create(
                action_type='Adjusted',
                item_name=f"Updated Material '{material.name}' (SKU: {material.sku})",
                quantity_changed=0,
                warehouse=None,
                user=request.user if request.user.is_authenticated else None
            )

            messages.success(request, f"Material '{material.name}' (SKU: {material.sku}) updated successfully.")
        except Exception as e:
            messages.error(request, f"Error updating material: {e}")

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
        })

    return render(request, 'material_form.html', {'material': material, 'edit_mode': True})


# --------------------------------------------------------------------------
# SALES ORDERS
# --------------------------------------------------------------------------
@login_required
def sales_order_list_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_so':
            so_number_auto = request.POST.get('so_number_auto') == '1'
            so_number = request.POST.get('so_number')
            if so_number_auto or not so_number:
                so_number = generate_next_code(SalesOrder, 'so_number', 'SO', 1001)
            client_name = request.POST.get('client_name')
            warehouse_id = request.POST.get('origin_warehouse_id')
            try:
                wh = get_object_or_404(Warehouse, id=warehouse_id)
                so = SalesOrder.objects.create(
                    so_number=so_number, client_name=client_name,
                    origin_warehouse=wh, status='Draft', created_by=request.user
                )
                OrderTimeline.objects.create(sales_order=so, action="Sales Order Created (Draft)", user=request.user)
                messages.success(request, f"Sales Order {so_number} created.")
            except Exception as e:
                messages.error(request, f"Error creating Sales Order: {e}")

        elif action == 'update_so_status':
            new_status = request.POST.get('status')
            if new_status in ['Pending', 'Awaiting Acknowledgement']:
                if not request.user.is_superuser and getattr(request.user, 'role', '') not in ['Admin', 'Manager']:
                    messages.error(request, "Permission Denied: Only Managers can approve Sales Orders.")
                    return redirect('so_list')
            so_id = request.POST.get('so_id')
            new_status = request.POST.get('status')
            so = get_object_or_404(SalesOrder, id=so_id)
            old_status = so.status
            so.status = new_status
            if new_status in ['Pending', 'Awaiting Acknowledgement']:
                so.approved_by = request.user
            so.save()
            OrderTimeline.objects.create(sales_order=so, action=f"Status changed to '{new_status}'", user=request.user)
            
            from .utils import deduct_stock_from_allocation
            if old_status not in ['Shipped', 'Delivered'] and new_status in ['Shipped', 'Delivered']:
                deduct_stock_from_allocation('sales_order', so)
                
            messages.success(request, f"Sales Order {so.so_number} updated to {new_status}.")

        return redirect('so_list')

    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    
    sales_orders = SalesOrder.objects.select_related('origin_warehouse', 'created_by', 'approved_by').prefetch_related('items__product', 'timeline').order_by('-order_date')
    
    if search_query:
        sales_orders = sales_orders.filter(
            Q(so_number__icontains=search_query) | Q(client_name__icontains=search_query)
        )
        
    if status_filter:
        sales_orders = sales_orders.filter(status=status_filter)

    warehouses = Warehouse.objects.all().order_by('name')

    context = {
        'search_query': search_query,
        'status_filter': status_filter,
        'sales_orders': sales_orders,
        'warehouses': warehouses,
        'so_status_choices': SalesOrder.STATUS_CHOICES,
        'next_so_number': generate_next_code(SalesOrder, 'so_number', 'SO', 1001),
    }
    return render(request, 'so_list.html', context)


# --------------------------------------------------------------------------
# PURCHASE ORDERS
# --------------------------------------------------------------------------
@login_required
def purchase_order_list_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'create_po':
            po_number_auto = request.POST.get('po_number_auto') == '1'
            po_number = request.POST.get('po_number')
            if po_number_auto or not po_number:
                po_number = generate_next_code(PurchaseOrder, 'po_number', 'PO', 5001)
            supplier_name = request.POST.get('supplier_name')
            warehouse_id = request.POST.get('target_warehouse_id')
            try:
                wh = get_object_or_404(Warehouse, id=warehouse_id)
                po = PurchaseOrder.objects.create(
                    po_number=po_number, supplier_name=supplier_name,
                    target_warehouse=wh, status='Draft', created_by=request.user
                )
                OrderTimeline.objects.create(purchase_order=po, action="Purchase Order Created (Draft)", user=request.user)
                messages.success(request, f"Purchase Order {po_number} created.")
            except Exception as e:
                messages.error(request, f"Error creating Purchase Order: {e}")

        elif action == 'update_po_status':
            po_id = request.POST.get('po_id')
            new_status = request.POST.get('status')
            po = get_object_or_404(PurchaseOrder, id=po_id)
            po.status = new_status
            if new_status in ['Pending', 'Partially Received']:
                po.approved_by = request.user
            po.save()
            OrderTimeline.objects.create(purchase_order=po, action=f"Status changed to '{new_status}'", user=request.user)
            messages.success(request, f"Purchase Order {po.po_number} updated to {new_status}.")

        return redirect('po_list')

    search_query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()
    
    purchase_orders = PurchaseOrder.objects.select_related('target_warehouse', 'created_by', 'approved_by').prefetch_related('items__material', 'timeline').order_by('-order_date')
    
    if search_query:
        purchase_orders = purchase_orders.filter(
            Q(po_number__icontains=search_query) | Q(supplier_name__icontains=search_query)
        )
        
    if status_filter:
        purchase_orders = purchase_orders.filter(status=status_filter)

    warehouses = Warehouse.objects.all().order_by('name')

    context = {
        'search_query': search_query,
        'status_filter': status_filter,
        'purchase_orders': purchase_orders,
        'warehouses': warehouses,
        'po_status_choices': PurchaseOrder.STATUS_CHOICES,
        'next_po_number': generate_next_code(PurchaseOrder, 'po_number', 'PO', 5001),
    }
    return render(request, 'po_list.html', context)


# --------------------------------------------------------------------------
# SALES ORDER DETAIL
# --------------------------------------------------------------------------
@login_required
def so_detail_view(request, pk):
    so = get_object_or_404(SalesOrder.objects.prefetch_related('items__product', 'timeline__user'), pk=pk)
    products = Product.objects.all().order_by('name')
    warehouses = Warehouse.objects.all().order_by('name')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_so_item':
            prod_id = request.POST.get('product_id')
            qty = request.POST.get('quantity_ordered', 0)
            unit_price = request.POST.get('unit_price', None)
            try:
                prod = get_object_or_404(Product, id=prod_id)
                SalesOrderDetail.objects.create(
                    sales_order=so,
                    product=prod,
                    quantity_ordered=float(qty),
                    unit_price=float(unit_price) if unit_price else None
                )
                OrderTimeline.objects.create(sales_order=so, action=f"Line item added: {prod.name} x{qty}", user=request.user)
                messages.success(request, f"Added {prod.name} to {so.so_number}.")
            except Exception as e:
                messages.error(request, f"Error adding item: {e}")

        elif action == 'remove_so_item':
            item_id = request.POST.get('item_id')
            try:
                item = get_object_or_404(SalesOrderDetail, id=item_id, sales_order=so)
                name = item.product.name
                item.delete()
                OrderTimeline.objects.create(sales_order=so, action=f"Line item removed: {name}", user=request.user)
                messages.success(request, f"Removed {name} from {so.so_number}.")
            except Exception as e:
                messages.error(request, f"Error removing item: {e}")

        elif action == 'update_so_status':
            new_status = request.POST.get('status')
            if new_status in ['Pending', 'Awaiting Acknowledgement']:
                if not request.user.is_superuser and getattr(request.user, 'role', '') not in ['Admin', 'Manager']:
                    messages.error(request, "Permission Denied: Only Managers can approve Sales Orders.")
                    return redirect('so_detail', pk=so.pk)
            old_status = so.status
            so.status = new_status
            if new_status in ['Pending', 'Awaiting Acknowledgement']:
                so.approved_by = request.user
            so.save()
            OrderTimeline.objects.create(sales_order=so, action=f"Status updated to '{new_status}'", user=request.user)
            
            from .utils import deduct_stock_from_allocation
            if old_status not in ['Shipped', 'Delivered'] and new_status in ['Shipped', 'Delivered']:
                deduct_stock_from_allocation('sales_order', so)

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
            so.client_name = request.POST.get('client_name', so.client_name)
            wh_id = request.POST.get('origin_warehouse_id')
            if wh_id:
                so.origin_warehouse = get_object_or_404(Warehouse, id=wh_id)
            deadline = request.POST.get('fulfillment_deadline')
            so.fulfillment_deadline = deadline if deadline else so.fulfillment_deadline
            so.updated_by = request.user
            so.save()
            messages.success(request, f"{so.so_number} details updated.")

        elif action == 'request_approval':
            manager_id = request.POST.get('manager_id')
            remarks = request.POST.get('remarks', '').strip()
            
            mgr = CustomUser.objects.filter(id=manager_id).first()
            if not mgr:
                messages.error(request, "Please select a valid manager for approval.")
            else:
                so.status = 'Pending Approval'
                so.assigned_to = mgr
                so.approval_remarks = remarks
                so.save()
                
                OrderTimeline.objects.create(sales_order=so, action=f"Approval Requested from {mgr.username}. Remarks: {remarks}", user=request.user)
                Notification.objects.create(
                    user=mgr,
                    message=f"Sales Order {so.so_number} requires your approval.",
                    link=reverse('approvals_inbox')
                )
                messages.success(request, f"Approval requested from {mgr.get_full_name() or mgr.username}.")
        elif action == 'add_follower':
            user_id = request.POST.get('user_id')
            user_to_add = CustomUser.objects.filter(id=user_id).first()
            if user_to_add:
                so.followers.add(user_to_add)
                messages.success(request, f"Added {user_to_add.username} as a follower.")
            else:
                messages.error(request, "User not found.")

        elif action == 'remove_follower':
            user_id = request.POST.get('user_id')
            user_to_remove = CustomUser.objects.filter(id=user_id).first()
            if user_to_remove:
                so.followers.remove(user_to_remove)
                messages.success(request, f"Removed {user_to_remove.username} from followers.")

        return redirect('so_detail', pk=so.pk)

    # BOM readiness and Allocation logic
    line_items_with_bom = []
    from django.db.models import Sum, F
    from .models import StockAllocation
    
    for item in so.items.select_related('product').all():
        allocated = float(StockAllocation.objects.filter(sales_order=so, batch__product=item.product).aggregate(s=Sum('quantity'))['s'] or 0)
        global_avail = float(Batch.objects.filter(product=item.product, status='Active').annotate(avail=F('quantity')-F('allocated_quantity')).aggregate(s=Sum('avail'))['s'] or 0)
        
        incoming_production = float(ProductionRun.objects.filter(
            sales_order=so,
            target_product=item.product,
            status__in=['Pending Approval', 'Planned', 'InProgress']
        ).aggregate(s=Sum('expected_yield'))['s'] or 0)

        if so.status in ['Ready to Ship', 'Shipped', 'Delivered', 'Cancelled']:
            deficit = 0.0
        else:
            if so.status in ['Draft', 'Pending Approval']:
                deficit = float(item.quantity_ordered) - global_avail - incoming_production
            else:
                deficit = float(item.quantity_ordered) - allocated - incoming_production
                
            deficit = max(0.0, deficit)

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
            'deficit': deficit,
        })

    context = {
        'so': so,
        'line_items': line_items_with_bom,
        'products': products,
        'warehouses': warehouses,
        'so_status_choices': SalesOrder.STATUS_CHOICES,
        'managers': CustomUser.objects.filter(role__in=['Admin', 'Manager']).order_by('username'),
        'all_users': CustomUser.objects.all().order_by('username'),
    }
    return render(request, 'so_detail.html', context)


# --------------------------------------------------------------------------
# PURCHASE ORDER DETAIL
# --------------------------------------------------------------------------
@login_required
def po_detail_view(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.prefetch_related('items__material', 'timeline__user'), pk=pk)
    materials = Material.objects.all().order_by('name')
    warehouses = Warehouse.objects.all().order_by('name')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_po_item':
            if po.linked_production_run:
                messages.error(request, "Cannot modify items for a PO automatically generated from a Production Run shortage.")
                return redirect('po_detail', pk=pk)
                
            mat_id = request.POST.get('material_id')
            qty = request.POST.get('quantity_ordered', 0)
            unit_price = request.POST.get('unit_price', None)
            try:
                mat = get_object_or_404(Material, id=mat_id)
                PurchaseOrderDetail.objects.create(
                    purchase_order=po,
                    material=mat,
                    quantity_ordered=float(qty),
                    unit_price=float(unit_price) if unit_price else None
                )
                OrderTimeline.objects.create(purchase_order=po, action=f"Line item added: {mat.name} x{qty}", user=request.user)
                messages.success(request, f"Added {mat.name} to {po.po_number}.")
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
                item.delete()
                OrderTimeline.objects.create(purchase_order=po, action=f"Line item removed: {name}", user=request.user)
                messages.success(request, f"Removed {name} from {po.po_number}.")
            except Exception as e:
                messages.error(request, f"Error removing item: {e}")

        elif action == 'mark_received':
            item_id = request.POST.get('item_id')
            qty_received = request.POST.get('qty_received', 0)
            try:
                item = get_object_or_404(PurchaseOrderDetail, id=item_id, purchase_order=po)
                old_qty = float(item.quantity_received)
                new_qty = float(qty_received)
                delta = new_qty - old_qty
                
                item.quantity_received = new_qty
                item.save()
                OrderTimeline.objects.create(purchase_order=po, action=f"Received {new_qty} of {item.material.name}", user=request.user)
                
                if delta > 0:
                    from datetime import date, timedelta
                    from .models import Batch, WarehouseLocation
                    loc = WarehouseLocation.objects.filter(warehouse=po.target_warehouse).first()
                    Batch.objects.create(
                        batch_number=f"B-{po.po_number}-{item.material.sku}-{int(new_qty)}",
                        status='Active',
                        material=item.material,
                        quantity=delta,
                        manufacturing_date=date.today(),
                        expiry_date=date.today() + timedelta(days=365),
                        location=loc
                    )
                
                # Auto update PO status
                all_items = po.items.all()
                total_ordered = sum(float(i.quantity_ordered) for i in all_items)
                total_received = sum(float(i.quantity_received) for i in all_items)
                if total_received >= total_ordered:
                    po.status = 'Completed'
                elif total_received > 0:
                    po.status = 'Partially Received'
                po.save()
                messages.success(request, f"Updated received quantity for {item.material.name} and added {delta} to inventory.")
            except Exception as e:
                messages.error(request, f"Error updating received qty: {e}")

        elif action == 'update_po_status':
            new_status = request.POST.get('status')
            po.status = new_status
            if new_status in ['Pending', 'Partially Received']:
                po.approved_by = request.user
            po.save()
            OrderTimeline.objects.create(purchase_order=po, action=f"Status updated to '{new_status}'", user=request.user)

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
            if not mgr:
                messages.error(request, "Please select a valid manager for approval.")
            else:
                po.status = 'Pending Approval'
                po.assigned_to = mgr
                po.approval_remarks = remarks
                po.save()
                
                OrderTimeline.objects.create(purchase_order=po, action=f"Approval Requested from {mgr.username}. Remarks: {remarks}", user=request.user)
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
                po.followers.add(user_to_add)
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
    for item in po.items.select_related('material').all():
        subtotal = float(item.quantity_ordered) * float(item.unit_price or 0)
        po_total += subtotal
        po_items_data.append({
            'item': item,
            'subtotal': subtotal,
            'pct_received': min(100, int((float(item.quantity_received) / float(item.quantity_ordered) * 100))) if float(item.quantity_ordered) > 0 else 0,
        })

    context = {
        'po': po,
        'po_items_data': po_items_data,
        'po_total': po_total,
        'materials': materials,
        'warehouses': warehouses,
        'po_status_choices': PurchaseOrder.STATUS_CHOICES,
        'managers': CustomUser.objects.filter(role__in=['Admin', 'Manager']).order_by('username'),
        'all_users': CustomUser.objects.all().order_by('username'),
    }
    return render(request, 'po_detail.html', context)


# --------------------------------------------------------------------------
# MANUFACTURING & READINESS
# --------------------------------------------------------------------------
@login_required
def manufacturing_view(request):
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
                plant = get_object_or_404(Warehouse, id=plant_id)
                so = SalesOrder.objects.filter(id=so_id).first() if so_id else None

                from django.utils import timezone
                tomorrow = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
                
                run = ProductionRun.objects.create(
                    run_number=run_number, target_product=prod,
                    expected_yield=yield_qty, manufacturing_plant=plant,
                    status='Pending Approval', supervisor=request.user,
                    sales_order=so,
                    start_time=tomorrow,
                    end_time=tomorrow + timedelta(hours=4)
                )

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
            except Exception as e:
                messages.error(request, f"Error scheduling run: {e}")
        elif action == 'draft_po_from_shortage':
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)
            
            if run.status != 'Awaiting Materials':
                messages.error(request, "Run is not awaiting materials.")
                return redirect('readiness')
                
            from .models import PurchaseOrder, PurchaseOrderDetail
            from django.utils import timezone
            from datetime import timedelta
            
            # Find shortages
            shortages = []
            for req in run.target_product.recipe_items.all():
                needed = float(req.quantity_required) * float(run.expected_yield)
                # Count allocated quantities for THIS run
                allocated = sum(alloc.quantity for alloc in run.allocations.filter(batch__material=req.material))
                short = max(0, needed - float(allocated))
                if short > 0:
                    shortages.append({
                        'material': req.material,
                        'qty': short
                    })
                    
            if not shortages:
                messages.info(request, "No shortages found.")
                return redirect('readiness')
                
            # Create Draft PO
            po_number = generate_next_code(PurchaseOrder, 'po_number', 'PO', 601, pad=3)
            
            # Try to auto-assign a purchaser
            purchaser = CustomUser.objects.filter(roles__name__icontains='Purchasing').first()
            if not purchaser:
                 purchaser = CustomUser.objects.filter(role__icontains='purchas').first()
                 
            with transaction.atomic():
                po = PurchaseOrder.objects.create(
                    po_number=po_number,
                    supplier="To Be Determined",
                    destination_warehouse=run.manufacturing_plant,
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
                        link=f"/operations/orders/po/{po.pk}/"
                    )
                    
                messages.success(request, f"Draft PO {po.po_number} created successfully and assigned to Purchasing.")
            return redirect('readiness')

        elif action == 'approve_run':
            if not request.user.is_superuser and getattr(request.user, 'role', '') not in ['Admin', 'Manager']:
                messages.error(request, "Permission Denied: Only Managers can approve Production Runs.")
                return redirect('readiness')
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)

            missing_materials = []
            for req in run.target_product.recipe_items.all():
                needed = float(req.quantity_required) * float(run.expected_yield)
                available = Batch.objects.filter(
                    material=req.material, 
                    location__warehouse=run.manufacturing_plant,
                    status='Active'
                ).aggregate(total=Sum(F('quantity') - F('allocated_quantity')))['total'] or 0
                if float(available) < needed:
                    missing_materials.append(f"{req.material.name} (Need {needed}, Have {available})")
                    
            if missing_materials:
                messages.error(request, f"Cannot approve run. Insufficient materials at {run.manufacturing_plant.name}: {', '.join(missing_materials)}")
                return redirect('readiness')

            run.status = 'Planned'
            run.save()
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
            if not request.user.is_superuser and getattr(request.user, 'role', '') not in ['Admin', 'Manager', 'Staff_Edit']:
                messages.error(request, "Permission Denied: You do not have permission to start Production Runs.")
                return redirect('readiness')
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)
            if run.status == 'Planned':
                run.status = 'InProgress'
                from django.utils import timezone
                run.start_time = timezone.now()
                run.save()
                messages.success(request, f"Production Run {run.run_number} started.")

        elif action == 'complete_run':
            if not request.user.is_superuser and getattr(request.user, 'role', '') not in ['Admin', 'Manager', 'Staff_Edit']:
                messages.error(request, "Permission Denied: You do not have permission to complete Production Runs.")
                return redirect('readiness')
            run_id = request.POST.get('run_id')
            run = get_object_or_404(ProductionRun, id=run_id)
            with transaction.atomic():
                from django.utils import timezone
                from .utils import deduct_stock_from_allocation
                
                if run.status == 'InProgress':
                    actual_yield = request.POST.get('actual_yield')
                    run.actual_yield = float(actual_yield) if actual_yield else run.expected_yield
                    run.status = 'Completed'
                    run.end_time = timezone.now()
                    run.save()
                    
                    # Permanently deduct the allocated raw materials
                    deduct_stock_from_allocation('production_run', run)

                    # Find warehouse location for storing finished product
                    loc = WarehouseLocation.objects.filter(warehouse=run.manufacturing_plant).first()
                    batch = Batch.objects.create(
                        batch_number=f"B-PROD-{run.run_number}",
                        status='Active',
                        product=run.target_product,
                        quantity=run.actual_yield,
                        produced_in=run,
                        manufacturing_date=date.today(),
                        expiry_date=date.today() + timedelta(days=365),
                        location=loc
                    )
                    RegistryLog.objects.create(
                        action_type='Produced',
                        item_name=f"{run.target_product.name} (Batch {batch.batch_number})",
                        quantity_changed=run.actual_yield,
                        warehouse=run.manufacturing_plant,
                        user=request.user
                    )

                    # Auto-check if all runs for a linked SO are complete
                    if run.sales_order:
                        so = run.sales_order
                        so_runs = ProductionRun.objects.filter(sales_order=so)
                        all_complete = all(r.status == 'Completed' for r in so_runs)
                        if all_complete:
                            so.status = 'Ready to Ship'
                            so.save()
                            OrderTimeline.objects.create(
                                sales_order=so,
                                action="All production runs completed ➔ status auto-updated to 'Ready to Ship'",
                                user=request.user
                            )
                    messages.success(request, f"Production Run {run.run_number} completed. Produced {run.actual_yield} units of {run.target_product.sku}.")

        return redirect('readiness')

    loc_filter = request.GET.get('loc_filter', '')
    runs = ProductionRun.objects.select_related('target_product', 'supervisor', 'manufacturing_plant', 'sales_order').order_by('-id')
    if loc_filter:
        runs = runs.filter(manufacturing_plant_id=loc_filter)
    products = Product.objects.all()
    plants = Warehouse.objects.filter(location_type='Manufacturing')
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
            recipes = item.product.recipe_items.select_related('material').all()
            bom_rows = []
            can_make = None
            for r in recipes:
                avail = float(Batch.objects.filter(material=r.material, status='Active').annotate(avail=F('quantity')-F('allocated_quantity')).aggregate(s=Sum('avail'))['s'] or 0)
                needed_per_unit = float(r.quantity_required)
                needed_total = needed_per_unit * float(item.quantity_ordered)
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
            # Check if a run already exists for this product+SO
            existing_run = so.production_runs.filter(target_product=item.product).first()
            so_items.append({
                'item': item,
                'bom_rows': bom_rows,
                'can_make': can_make if can_make is not None else '∞',
                'bom_ready': all(r['sufficient'] for r in bom_rows),
                'existing_run': existing_run,
            })
        so_queue.append({
            'so': so,
            'items': so_items,
            'all_ready': all(i['bom_ready'] for i in so_items),
        })

    context = {
        'runs': runs,
        'products': products,
        'plants': plants,
        'loc_filter': loc_filter,
        'recipe_readiness': recipe_readiness,
        'so_queue': so_queue,
        'so_status_choices': SalesOrder.STATUS_CHOICES,
        'next_run_number': generate_next_code(ProductionRun, 'run_number', 'RUN', 801, pad=3),
    }
    return render(request, 'manufacturing.html', context)


# --------------------------------------------------------------------------
# LOGISTICS TRACKER & SHIPMENTS
# --------------------------------------------------------------------------
@login_required
def shipments_view(request):
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
                so = SalesOrder.objects.filter(id=so_id).first() if so_id else None

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
                    external_origin=external_origin if external_origin else None
                )
                
                # Auto update Sales Order status based on Shipment
                if so:
                    if status == 'Preparing':
                        so.status = 'Ready to Ship'
                    elif status == 'Dispatched':
                        so.status = 'Shipped'
                    elif status == 'Arrived':
                        so.status = 'Delivered'
                    so.save()
                    
                messages.success(request, f"Shipment '{tracking_number}' registered. Now you can add items.")
                return redirect('shipment_detail', pk=shipment.pk)
            except Exception as e:
                messages.error(request, f"Error registering shipment: {e}")

        elif action == 'update_status':
            shipment_id = request.POST.get('shipment_id')
            new_status = request.POST.get('status')
            shipment = get_object_or_404(Shipment, id=shipment_id)
            shipment.status = new_status
            if new_status == 'Arrived':
                shipment.actual_arrival_date = date.today()
            shipment.save()
            
            # Auto update Sales Order status based on Shipment
            if shipment.sales_order:
                so = shipment.sales_order
                if new_status == 'Preparing':
                    so.status = 'Ready to Ship'
                elif new_status == 'Dispatched':
                    so.status = 'Shipped'
                elif new_status == 'Arrived':
                    so.status = 'Delivered'
                so.save()
            
            messages.success(request, f"Shipment {shipment.tracking_number} updated to {new_status}.")

        return redirect('shipments')

    status_filter = request.GET.get('status')
    direction_filter = request.GET.get('direction')

    shipments = Shipment.objects.select_related(
        'origin_warehouse', 'destination_warehouse', 'purchase_order', 'sales_order'
    ).prefetch_related('items').order_by('-id')

    if status_filter:
        shipments = shipments.filter(status=status_filter)
    if direction_filter:
        shipments = shipments.filter(direction=direction_filter)

    warehouses = Warehouse.objects.all().order_by('name')
    materials = Material.objects.all().order_by('name')
    products = Product.objects.all().order_by('name')
    purchase_orders = PurchaseOrder.objects.exclude(status='Completed').order_by('-po_number')
    sales_orders = SalesOrder.objects.exclude(status='Delivered').order_by('-so_number')
    batches = Batch.objects.filter(status='Active').order_by('batch_number')

    stats = {
        'total': Shipment.objects.count(),
        'dispatched': Shipment.objects.filter(status='Dispatched').count(),
        'preparing': Shipment.objects.filter(status='Preparing').count(),
        'delayed': Shipment.objects.filter(status='Delayed').count(),
        'arrived': Shipment.objects.filter(status='Arrived').count(),
    }

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
        'direction_choices': Shipment.DIRECTION_CHOICES,
        'next_tracking_number': generate_next_code(Shipment, 'tracking_number', 'TRK', 101, pad=4),
    }
    return render(request, 'shipments.html', context)



# --------------------------------------------------------------------------
# QA & SPOILAGE CONTROL
# --------------------------------------------------------------------------
@login_required
def qa_dashboard_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        batch_id = request.POST.get('batch_id')
        batch = get_object_or_404(Batch, id=batch_id)

        if action == 'extend_expiry':
            try:
                days = int(request.POST.get('extra_days', 30))
                batch.expiry_date = batch.expiry_date + timedelta(days=days)
                batch.save()
                RegistryLog.objects.create(
                    action_type='QA_Extension',
                    item_name=f"Batch {batch.batch_number} (+{days} days)",
                    quantity_changed=batch.quantity,
                    warehouse=batch.location.warehouse if batch.location else None,
                    user=request.user
                )
                messages.success(request, f"Expiry date for batch {batch.batch_number} extended by {days} days.")
            except Exception as e:
                messages.error(request, f"Error extending expiry: {e}")

        elif action == 'quarantine':
            batch.status = 'Quarantined'
            batch.save()
            messages.warning(request, f"Batch {batch.batch_number} placed in Quarantine.")

        elif action == 'release_quarantine':
            batch.status = 'Active'
            batch.save()
            messages.success(request, f"Batch {batch.batch_number} released to Active inventory.")

        elif action == 'spoil_dispose':
            batch.status = 'Spoiled'
            batch.save()
            RegistryLog.objects.create(
                action_type='Spoiled_Disposal',
                item_name=f"Batch {batch.batch_number} Disposed",
                quantity_changed=batch.quantity,
                warehouse=batch.location.warehouse if batch.location else None,
                user=request.user
            )
            messages.error(request, f"Batch {batch.batch_number} marked as Spoiled / Disposed.")

        return redirect('qa_dashboard')

    today = date.today()
    batches = Batch.objects.select_related('material', 'product', 'location__warehouse').order_by('expiry_date')
    
    near_expiry = []
    quarantined = []
    spoiled = []
    healthy = []

    for b in batches:
        days_left = (b.expiry_date - today).days if b.expiry_date else 999
        b.days_remaining = days_left
        
        if b.status == 'Quarantined':
            quarantined.append(b)
        elif b.status == 'Spoiled':
            spoiled.append(b)
        elif days_left <= 30:
            near_expiry.append(b)
        else:
            healthy.append(b)

    context = {
        'near_expiry': near_expiry,
        'quarantined': quarantined,
        'spoiled': spoiled,
        'healthy': healthy,
    }
    return render(request, 'qa_dashboard.html', context)


# --------------------------------------------------------------------------
# APPROVALS INBOX (Action Center)
# --------------------------------------------------------------------------
from .decorators import role_required

@login_required
@role_required(['Admin', 'Manager'])

def approvals_inbox_view(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        item_type = request.POST.get('item_type')
        item_id = request.POST.get('item_id')
        comment = request.POST.get('comment', '').strip()

        if item_type == 'sales_order':
            so = get_object_or_404(SalesOrder, id=item_id)
            if action == 'approve':
                old_status = so.status
                so.status = 'Pending'
                so.approved_by = request.user
                so.save()
                OrderTimeline.objects.create(sales_order=so, action=f"Approved by Manager. Comment: {comment}" if comment else "Approved by Manager.", user=request.user)
                
                messages.success(request, f"Sales Order {so.so_number} approved.")
            elif action == 'reject':
                so.status = 'Draft'
                so.assigned_to = None
                so.approval_remarks = ''
                so.save()
                OrderTimeline.objects.create(sales_order=so, action=f"Approval Rejected by Manager. Comment: {comment}" if comment else "Approval Rejected by Manager.", user=request.user)
                messages.warning(request, f"Sales Order {so.so_number} returned to Draft.")

        elif item_type == 'production_run':
            run = get_object_or_404(ProductionRun, id=item_id)
            if action == 'approve':
                missing_materials = []
                for req in run.target_product.recipe_items.all():
                    needed = float(req.quantity_required) * float(run.expected_yield)
                    available = Batch.objects.filter(
                        material=req.material, 
                        location__warehouse=run.manufacturing_plant,
                        status='Active'
                    ).aggregate(total=Sum(F('quantity') - F('allocated_quantity')))['total'] or 0
                    if float(available) < needed:
                        missing_materials.append(f"{req.material.name} (Need {needed}, Have {available})")
                        
                if missing_materials:
                    messages.error(request, f"Cannot approve run {run.run_number}. Insufficient materials at {run.manufacturing_plant.name}: {', '.join(missing_materials)}")
                    return redirect('approvals_inbox')
                
                run.status = 'Planned'
                run.save()
                from .utils import allocate_stock
                for req in run.target_product.recipe_items.all():
                    allocate_stock('production_run', run, req.material, float(req.quantity_required) * float(run.expected_yield), warehouse=run.manufacturing_plant)
                messages.success(request, f"Production Run {run.run_number} approved and allocated.")
            elif action == 'reject':
                run.status = 'Cancelled'
                run.save()
                messages.warning(request, f"Production Run {run.run_number} rejected.")
                
        elif item_type == 'purchase_order':
            po = get_object_or_404(PurchaseOrder, id=item_id)
            if action == 'approve':
                po.status = 'Sent'
                po.save()
                OrderTimeline.objects.create(purchase_order=po, action=f"Approved by Manager. Comment: {comment}" if comment else "Approved by Manager.", user=request.user)
                messages.success(request, f"Purchase Order {po.po_number} approved.")
            elif action == 'reject':
                po.status = 'Draft'
                po.assigned_to = None
                po.approval_remarks = ''
                po.save()
                OrderTimeline.objects.create(purchase_order=po, action=f"Approval Rejected by Manager. Comment: {comment}" if comment else "Approval Rejected by Manager.", user=request.user)
                messages.warning(request, f"Purchase Order {po.po_number} returned to Draft.")
                
        return redirect('approvals_inbox')

    pending_sos = SalesOrder.objects.filter(status='Pending Approval', assigned_to=request.user).order_by('order_date').prefetch_related('items__product')
    pending_runs = ProductionRun.objects.filter(status='Pending Approval').order_by('start_time')
    pending_pos = PurchaseOrder.objects.filter(status='Pending Approval', assigned_to=request.user).order_by('order_date').prefetch_related('items__material')
    pending_shipments = Shipment.objects.filter(status='Discrepant', assigned_manager=request.user).order_by('-id')
    
    # Notifications
    notifications = Notification.objects.filter(user=request.user, is_read=False).order_by('-created_at')
    
    # History
    history_sos = SalesOrder.objects.filter(timeline__user=request.user, timeline__action__icontains='Approv').distinct().order_by('-order_date')
    history_pos = PurchaseOrder.objects.filter(timeline__user=request.user, timeline__action__icontains='Approv').distinct().order_by('-order_date')
    followed_pos = request.user.followed_pos.all().order_by('-order_date')

    # Analytics
    pending_count = pending_sos.count() + pending_pos.count() + pending_runs.count() + pending_shipments.count()
    start_of_week = date.today() - timedelta(days=date.today().weekday())
    approved_this_week = OrderTimeline.objects.filter(user=request.user, action__icontains='Approved by Manager', timestamp__gte=start_of_week).count()
    rejected_this_week = OrderTimeline.objects.filter(user=request.user, action__icontains='Approval Rejected', timestamp__gte=start_of_week).count()

    context = {
        'pending_sos': pending_sos,
        'pending_runs': pending_runs,
        'pending_pos': pending_pos,
        'pending_shipments': pending_shipments,
        'notifications': notifications,
        'history_sos': history_sos,
        'history_pos': history_pos,
        'followed_pos': followed_pos,
        'pending_count': pending_count,
        'approved_this_week': approved_this_week,
        'rejected_this_week': rejected_this_week,
    }
    return render(request, 'approvals_inbox.html', context)


@login_required
def shipment_detail_view(request, pk):
    shipment = get_object_or_404(Shipment.objects.prefetch_related('items__material', 'items__product', 'items__batch'), pk=pk)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        
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
                
                # Validation for Internal Transfers
                if shipment.direction == 'Transfer' and not batch:
                    messages.error(request, "A Batch MUST be selected for Internal Transfers.")
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
                        # Allocation Logic (Lock Stock)
                        from .models import StockAllocation
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
                        messages.success(request, "Item added and stock locked.")
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
                        from .models import StockAllocation
                        alloc = StockAllocation.objects.filter(shipment=shipment, batch=batch).first()
                        if alloc:
                            deduct = min(Decimal(str(qty)), alloc.quantity)
                            alloc.quantity -= deduct
                            if alloc.quantity <= 0: alloc.delete()
                            else: alloc.save(update_fields=['quantity'])
                            batch.allocated_quantity -= deduct
                            batch.save(update_fields=['allocated_quantity'])
                    item.delete()
                    messages.success(request, "Item removed and stock lock released.")
            except Exception as e:
                messages.error(request, f"Error removing item: {e}")

        elif action == 'update_dates':
            eta = request.POST.get('eta_date')
            arrival = request.POST.get('arrival_date')
            if eta:
                shipment.expected_eta_date = eta
            if arrival:
                shipment.actual_arrival_date = arrival
            shipment.last_edited_by = request.user
            shipment.save()
            messages.success(request, "Shipment dates updated.")
            
        elif action == 'complete_shipment':
            has_discrepancy = False
            for item in shipment.items.all():
                req_qty_str = request.POST.get(f'received_qty_{item.id}')
                if req_qty_str:
                    rcv = float(req_qty_str)
                    item.received_quantity = rcv
                    from django.utils import timezone
                    item.date_confirmed = timezone.now()
                    item.save()
                    if rcv != float(item.quantity):
                        has_discrepancy = True
            
            if has_discrepancy:
                shipment.status = 'Discrepant'
                shipment.save()
                messages.warning(request, "Shipment receipt saved. Shortage detected. You may correct it later or request a Manager Force Close.")
            else:
                shipment.status = 'Completed'
                shipment.acknowledged_by = request.user
                shipment.last_edited_by = request.user
                                # Check if it was an inbound shipment for a PO linked to a Production Run
                if shipment.direction == 'Inbound' and shipment.purchase_order and shipment.purchase_order.linked_production_run:
                    run = shipment.purchase_order.linked_production_run
                    # Check if all materials are sufficient now
                    missing_materials = []
                    for req in run.target_product.recipe_items.all():
                        needed = float(req.quantity_required) * float(run.expected_yield)
                        available = Batch.objects.filter(
                            material=req.material, 
                            location__warehouse=run.manufacturing_plant,
                            status='Active'
                        ).aggregate(total=Sum(F('quantity') - F('allocated_quantity')))['total'] or 0
                        if float(available) < needed:
                            missing_materials.append(req.material.name)
                            
                    if not missing_materials:
                        msg = f"Materials have arrived for Production Run {run.run_number}. You have sufficient materials to allocate and start the run."
                    else:
                        msg = f"Partial materials have arrived for Production Run {run.run_number}, but you are still short on: {', '.join(missing_materials)}."
                        
                    Notification.objects.create(
                        user=run.supervisor,
                        message=msg,
                        link=f"/operations/manufacture/run/{run.id}/allocate/"
                    )

                shipment.save()
                
                # Release lock and deduct stock
                if shipment.direction in ['Outbound', 'Transfer']:
                    from .utils import deduct_stock_from_allocation
                    deduct_stock_from_allocation('shipment', shipment)
                
                if shipment.direction == 'Transfer' and shipment.destination_warehouse:
                    loc = WarehouseLocation.objects.filter(warehouse=shipment.destination_warehouse).first()
                    if loc:
                        for item in shipment.items.all():
                            if not item.batch or float(item.received_quantity) <= 0: continue
                            b = item.batch
                            rcv_qty = float(item.received_quantity)
                            
                            new_batch, created = Batch.objects.get_or_create(
                                batch_number=f"{b.batch_number}-TRF-{shipment.id}",
                                defaults={
                                    'status': 'Active',
                                    'material': b.material,
                                    'product': b.product,
                                    'quantity': rcv_qty,
                                    'manufacturing_date': b.manufacturing_date,
                                    'expiry_date': b.expiry_date,
                                    'location': loc,
                                    'produced_in': b.produced_in
                                }
                            )
                            if not created:
                                # Update quantity in case it was reopened and changed
                                new_batch.quantity = rcv_qty
                                new_batch.save(update_fields=['quantity'])
                            RegistryLog.objects.create(
                                action_type='Inbound',
                                item_name=f"Internal Transfer Received: {new_batch.batch_number}",
                                quantity_changed=rcv_qty,
                                warehouse=shipment.destination_warehouse,
                                user=request.user
                            )
                messages.success(request, "Shipment receipt confirmed and marked as Completed.")
        
        elif action == 'request_force_close':
            mgr_id = request.POST.get('assigned_manager_id')
            remarks = request.POST.get('discrepancy_remarks', '').strip()
            if not mgr_id or not remarks:
                messages.error(request, "You must provide remarks and assign a manager to request Force Close.")
                return redirect('shipment_detail', pk=shipment.pk)
                
            mgr = CustomUser.objects.get(id=mgr_id)
            shipment.discrepancy_remarks = remarks
            shipment.assigned_manager = mgr
            shipment.save()
            Notification.objects.create(
                user=mgr,
                message=f"Force Close requested on Discrepant Shipment {shipment.tracking_number}.",
                link=f"/operations/shipments/{shipment.id}/"
            )
            messages.success(request, f"Force Close escalation sent to {mgr.get_full_name() or mgr.username}.")
            
        elif action == 'cancel_escalation':
            shipment.assigned_manager = None
            shipment.discrepancy_remarks = ''
            shipment.save()
            messages.success(request, "Escalation cancelled. You can now edit quantities and retry completion.")
            
        elif action == 'force_close_shipment':
            if shipment.assigned_manager and request.user != shipment.assigned_manager:
                messages.error(request, "Only the assigned manager can approve the Force Close.")
                return redirect('shipment_detail', pk=shipment.pk)
                
            mgr_comment = request.POST.get('manager_comment', '').strip()
            if mgr_comment:
                if shipment.discrepancy_remarks:
                    shipment.discrepancy_remarks += f"\n\nManager Acknowledgment: {mgr_comment}"
                else:
                    shipment.discrepancy_remarks = f"Manager Acknowledgment: {mgr_comment}"
            
            shipment.status = 'Completed'
            shipment.approved_by = request.user
            shipment.save()
            
            # Manually handle discrepancy deduction and lock release
            if shipment.direction in ['Outbound', 'Transfer']:
                from .models import StockAllocation
                allocs = StockAllocation.objects.filter(shipment=shipment)
                for alloc in allocs:
                    batch = alloc.batch
                    item = shipment.items.filter(batch=batch).first()
                    rcv_qty = item.received_quantity if item else 0
                    
                    batch.quantity -= Decimal(str(rcv_qty))
                    batch.allocated_quantity -= alloc.quantity
                    batch.save(update_fields=['quantity', 'allocated_quantity'])
                    alloc.delete()
                    
            if shipment.direction == 'Transfer' and shipment.destination_warehouse:
                loc = WarehouseLocation.objects.filter(warehouse=shipment.destination_warehouse).first()
                if loc:
                    for item in shipment.items.all():
                        if not item.batch or float(item.received_quantity) <= 0: continue
                        b = item.batch
                        rcv_qty = float(item.received_quantity)
                        
                        new_batch, created = Batch.objects.get_or_create(
                            batch_number=f"{b.batch_number}-TRF-{shipment.id}",
                            defaults={
                                'status': 'Active',
                                'material': b.material,
                                'product': b.product,
                                'quantity': rcv_qty,
                                'manufacturing_date': b.manufacturing_date,
                                'expiry_date': b.expiry_date,
                                'location': loc,
                                'produced_in': b.produced_in
                            }
                        )
                        if not created:
                            new_batch.quantity = rcv_qty
                            new_batch.save(update_fields=['quantity'])
            
            if shipment.purchase_order:
                po = shipment.purchase_order
                po.status = 'Partially Received'
                po.save()
            
            messages.success(request, "Shipment Force Closed. Unreceived stock locks released.")
            
        elif action == 'reopen_shipment':
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

        elif action == 'update_status':
            new_status = request.POST.get('status')
            shipment.status = new_status
            if new_status == 'Arrived':
                shipment.actual_arrival_date = date.today()
            shipment.last_edited_by = request.user
            shipment.save()
            
            # Auto update Sales Order status based on Shipment
            if shipment.sales_order:
                so = shipment.sales_order
                if new_status == 'Preparing':
                    so.status = 'Ready to Ship'
                elif new_status == 'Dispatched':
                    so.status = 'Shipped'
                elif new_status == 'Arrived':
                    so.status = 'Delivered'
                so.save()
            
            messages.success(request, f"Shipment {shipment.tracking_number} updated to {new_status}.")
                
        return redirect('shipment_detail', pk=shipment.pk)
        
    materials = Material.objects.all().order_by('name')
    products = Product.objects.all().order_by('name')
    batches = Batch.objects.filter(status='Active').order_by('batch_number')
    managers = CustomUser.objects.filter(role__in=['Admin', 'Manager'])
    
    context = {
        'shipment': shipment,
        'materials': materials,
        'products': products,
        'batches': batches,
        'managers': managers,
        'status_choices': Shipment.STATUS_CHOICES,
    }
    return render(request, 'shipment_detail.html', context)

def production_run_allocate_view(request, pk):
    run = get_object_or_404(ProductionRun, pk=pk)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'allocate_custom':
            from .models import StockAllocation
            from decimal import Decimal
            with transaction.atomic():
                for key, value in request.POST.items():
                    if key.startswith('allocate_') and value:
                        try:
                            parts = key.split('_')
                            batch_id = parts[1]
                            mat_id = parts[2]
                            qty = float(value)
                            
                            if qty > 0:
                                batch = Batch.objects.get(id=batch_id)
                                batch.allocated_quantity += Decimal(str(qty))
                                batch.save(update_fields=['allocated_quantity'])
                                StockAllocation.objects.create(
                                    batch=batch,
                                    production_run=run,
                                    quantity=qty
                                )
                        except Exception as e:
                            pass
                
                run.status = 'InProgress'
                from django.utils import timezone
                run.start_time = timezone.now()
                run.save()
                messages.success(request, f"Materials allocated successfully. Production Run {run.run_number} started.")
                return redirect('readiness')
                
        elif action == 'allocate_auto':
            from .utils import allocate_stock
            for req in run.target_product.recipe_items.all():
                allocate_stock('production_run', run, req.material, float(req.quantity_required) * float(run.expected_yield), warehouse=run.manufacturing_plant)
            run.status = 'InProgress'
            from django.utils import timezone
            run.start_time = timezone.now()
            run.save()
            messages.success(request, f"Materials automatically allocated using FEFO/FIFO. Production Run {run.run_number} started.")
            return redirect('readiness')

    # Get materials and recommended batches
    recipe_reqs = []
    from django.db.models import F
    for req in run.target_product.recipe_items.all():
        needed = float(req.quantity_required) * float(run.expected_yield)
        batches = Batch.objects.filter(
            material=req.material,
            location__warehouse=run.manufacturing_plant,
            status='Active'
        ).annotate(
            avail=F('quantity') - F('allocated_quantity')
        ).filter(avail__gt=0).order_by(F('expiry_date').asc(nulls_last=True), 'manufacturing_date')
        
        batch_list = []
        for b in batches:
            if remaining > 0:
                take = min(float(b.avail), remaining)
                remaining -= take
            else:
                take = 0
            batch_list.append({'obj': b, 'suggested_qty': take})
            
        recipe_reqs.append({
            'material': req.material,
            'needed': needed,
            'batch_list': batch_list,
        })

    return render(request, 'production_allocate.html', {
        'run': run,
        'recipe_reqs': recipe_reqs
    })

@login_required
def mark_notifications_read(request):
    if request.method == 'POST':
        request.user.notifications.all().delete()
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok'})
    return redirect(request.META.get('HTTP_REFERER', 'home'))


@login_required
def profile_view(request):
    if request.method == 'POST':
        user = request.user
        user.first_name = request.POST.get('first_name', user.first_name)
        user.last_name = request.POST.get('last_name', user.last_name)
        user.email = request.POST.get('email', user.email)
        user.save()
        messages.success(request, "Profile updated successfully.")
        return redirect('profile')
    
    return render(request, 'profile.html', {'user': request.user})

@login_required
def user_management_view(request):
    is_admin = request.user.is_superuser or request.user.has_role('Admin') or request.user.role == 'Admin'
    is_manager = request.user.has_role('Manager') or request.user.role == 'Manager'
    
    if not (is_admin or is_manager):
        messages.error(request, "Permission Denied. Only Admins and Managers can manage users.")
        return redirect('dashboard')

    
    users = CustomUser.objects.all().prefetch_related('roles', 'allowed_locations')
    
    query = request.GET.get('q', '')
    if query:
        from django.db.models import Q
        users = users.filter(
            Q(username__icontains=query) | 
            Q(first_name__icontains=query) | 
            Q(last_name__icontains=query) |
            Q(email__icontains=query)
        )
        
    roles = Role.objects.all()
    warehouses = Warehouse.objects.all()
    
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add_location':
            name = request.POST.get('name')
            loc_type = request.POST.get('location_type')
            if name and loc_type:
                Warehouse.objects.create(
                    name=name,
                    location_type=loc_type,
                    ownership_type='Internal'
                )
                messages.success(request, f"Location '{name}' added successfully.")
            return redirect('user_management')

        if action == 'update_user':
            user_id = request.POST.get('user_id')
            user_obj = get_object_or_404(CustomUser, id=user_id)
            
            user_obj.is_active = request.POST.get('is_active') == 'on'
            
            # Roles
            role_ids = request.POST.getlist('roles')
            user_obj.roles.set(Role.objects.filter(id__in=role_ids))
            
            # Locations
            location_ids = request.POST.getlist('locations')
            user_obj.allowed_locations.set(Warehouse.objects.filter(id__in=location_ids))
            
            user_obj.updated_by = request.user
            user_obj.save()
            messages.success(request, f"User {user_obj.username} updated successfully.")
            return redirect('user_management')
            
    context = {
        'users': users,
        'roles': roles,
        'warehouses': warehouses,
        'query': query
    }
    return render(request, 'user_management.html', context)



@login_required
def so_allocate_view(request, pk):
    from .models import StockAllocation, Batch
    so = get_object_or_404(SalesOrder, pk=pk)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'allocate_manual':
            from .models import Batch, StockAllocation, ProductionRun
            from decimal import Decimal
            
            with transaction.atomic():
                for item in so.items.all():
                    qty_needed = Decimal(str(item.quantity_ordered))
                    allocated = Decimal('0')
                    
                    # Read inputs like batch_qty_123 where 123 is batch ID
                    for key, val in request.POST.items():
                        if key.startswith(f'batch_qty_{item.id}_'):
                            batch_id = key.split('_')[-1]
                            allocate_amt = Decimal(val) if val else Decimal('0')
                            
                            if allocate_amt > 0:
                                batch = Batch.objects.get(id=batch_id)
                                available = batch.quantity - batch.allocated_quantity
                                if allocate_amt > available:
                                    messages.error(request, f"Cannot allocate {allocate_amt} from batch {batch.batch_number}. Only {available} available.")
                                    return redirect('so_allocate', pk=so.pk)
                                    
                                StockAllocation.objects.create(
                                    batch=batch,
                                    sales_order=so,
                                    quantity=allocate_amt
                                )
                                batch.allocated_quantity += allocate_amt
                                batch.save()
                                allocated += allocate_amt
                                
                    unfulfilled = qty_needed - allocated
                    if unfulfilled > 0:
                        # Push remaining to manufacturing queue
                        # Check if a production run already exists for this SO and product
                        pr = ProductionRun.objects.filter(sales_order=so, target_product=item.product).first()
                        if not pr:
                            run_number = f"PR-{so.so_number}-{item.product.sku}"
                            from django.utils import timezone
                            from datetime import timedelta
                            ProductionRun.objects.create(
                                run_number=run_number,
                                target_product=item.product,
                                expected_yield=unfulfilled,
                                status='Pending Allocation',
                                sales_order=so,
                                start_time=timezone.now() + timedelta(days=1),
                                end_time=timezone.now() + timedelta(days=1, hours=4)
                            )
                
                so.status = 'Awaiting Acknowledgement'
                so.save()
                OrderTimeline.objects.create(sales_order=so, action=f"Manual allocation completed. Sent to manufacturing.", user=request.user)
                messages.success(request, f"Allocation complete for {so.so_number}.")
                return redirect('so_detail', pk=so.pk)

    # Gather data for UI
    from django.db.models import F
    allocation_data = []
    
    # Existing allocations
    existing = StockAllocation.objects.filter(sales_order=so)
    allocated_by_item = {}
    for alloc in existing:
        prod_id = alloc.batch.product.id
        allocated_by_item[prod_id] = allocated_by_item.get(prod_id, 0) + float(alloc.quantity)

    for item in so.items.all():
        needed = float(item.quantity_ordered) - allocated_by_item.get(item.product.id, 0)
        
        batches = Batch.objects.filter(product=item.product, status='Active')\
                               .order_by(F('expiry_date').asc(nulls_last=True), 'manufacturing_date')
        
        batch_list = []
        for b in batches:
            avail = float(b.quantity - b.allocated_quantity)
            if avail > 0:
                batch_list.append({
                    'id': b.id,
                    'number': b.batch_number,
                    'warehouse': b.location.warehouse.name if b.location and b.location.warehouse else 'Unknown',
                    'available': avail,
                    'expiry': b.expiry_date
                })
                
        allocation_data.append({
            'item_id': item.id,
            'product': item.product,
            'needed': needed,
            'batches': batch_list
        })
        
    return render(request, 'so_allocate.html', {'so': so, 'allocation_data': allocation_data})



@login_required
def production_run_allocate_view(request, pk):
    from .models import ProductionRun, Batch, StockAllocation
    run = get_object_or_404(ProductionRun, pk=pk)
    
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'allocate_run':
            wh_id = request.POST.get('warehouse_id')
            if not wh_id:
                messages.error(request, "Please select a facility.")
                return redirect('production_run_allocate', pk=pk)
                
            warehouse = get_object_or_404(Warehouse, id=wh_id)
            
            # Check permission
            if not request.user.is_superuser and warehouse not in request.user.allowed_locations.all():
                messages.error(request, "You do not have access to acknowledge orders for this facility.")
                return redirect('production_run_allocate', pk=pk)
                
            run.manufacturing_plant = warehouse
            
            from .models import Batch, StockAllocation
            from decimal import Decimal
            
            has_shortage = False
            with transaction.atomic():
                for req in run.target_product.recipe_items.all():
                    needed = Decimal(str(req.quantity_required)) * Decimal(str(run.expected_yield))
                    allocated = Decimal('0')
                    
                    # FIFO Auto-Allocate from the selected warehouse
                    batches = Batch.objects.filter(material=req.material, status='Active', location__warehouse=warehouse).order_by('expiry_date')
                    for b in batches:
                        if allocated >= needed:
                            break
                        avail = b.quantity - b.allocated_quantity
                        if avail > 0:
                            take = min(avail, needed - allocated)
                            StockAllocation.objects.create(batch=b, production_run=run, quantity=take)
                            b.allocated_quantity += take
                            b.save()
                            allocated += take
                            
                    if allocated < needed:
                        has_shortage = True
                        
                if has_shortage:
                    run.status = 'Awaiting Materials'
                    messages.warning(request, f"Order acknowledged, but materials are short. Please Draft a PO for the shortages.")
                else:
                    run.status = 'Planned'
                    messages.success(request, f"Materials allocated successfully. Run {run.run_number} is Planned.")
                    
                run.save()
            return redirect('readiness')

    warehouses = request.user.allowed_locations.all() if not request.user.is_superuser else Warehouse.objects.all()
    
    # Calculate shortages for preview
    preview = []
    for req in run.target_product.recipe_items.all():
        needed = float(req.quantity_required) * float(run.expected_yield)
        avail = sum(b.quantity - b.allocated_quantity for b in Batch.objects.filter(material=req.material, status='Active'))
        preview.append({
            'material': req.material,
            'needed': needed,
            'avail': avail,
            'shortage': max(0, needed - float(avail))
        })
        
    return render(request, 'production_allocate.html', {'run': run, 'warehouses': warehouses, 'preview': preview})

