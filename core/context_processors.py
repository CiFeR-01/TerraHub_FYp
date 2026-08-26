from django.utils.safestring import mark_safe

from .models import SalesOrder, PurchaseOrder, ProductionRun

def approvals_count(request):
    """
    Context processor to make the total number of pending approvals
    available in all templates for the Action Center badge.
    """
    # Only calculate for authenticated users with Admin or Manager roles
    if request.user.is_authenticated:
        if request.user.is_superuser or (hasattr(request.user, 'role') and request.user.role in ['Admin', 'Manager']):
            # For Sales Orders, we assume 'Draft' needs approval to move to 'Pending' (Approved)
            so_count = SalesOrder.objects.filter(status='Draft').count()

            # For Production Runs, 'Pending Approval' needs to move to 'Planned'
            run_count = ProductionRun.objects.filter(status='Pending Approval').count()

            # For Purchase Orders, 'Pending' needs to be approved to move to 'Sent' or similar
            # Wait, let's just use 'Pending' for POs
            po_count = PurchaseOrder.objects.filter(status='Pending').count()

            total_approvals = so_count + run_count + po_count
            return {'pending_approvals_total': total_approvals}

    return {'pending_approvals_total': 0}


NAV_ICONS = {
    'overview': mark_safe(
        '<rect x="3" y="3" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
    ),
    'inventory': mark_safe(
        '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<polyline points="3.27 6.96 12 12.01 20.73 6.96" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<line x1="12" y1="22.08" x2="12" y2="12" fill="none" stroke="currentColor" stroke-width="2"/>'
    ),
    'catalog': mark_safe(
        '<ellipse cx="12" cy="5" rx="9" ry="3" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<path d="M3 5v6c0 1.66 4 3 9 3s9-1.34 9-3V5" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<path d="M3 11v6c0 1.66 4 3 9 3s9-1.34 9-3v-6" fill="none" stroke="currentColor" stroke-width="2"/>'
    ),
    'operations': mark_safe(
        '<rect x="1" y="3" width="15" height="13" rx="2" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<polygon points="16 8 20 8 23 11 23 16 16 16 16 8" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<circle cx="5.5" cy="18.5" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<circle cx="18.5" cy="18.5" r="2.5" fill="none" stroke="currentColor" stroke-width="2"/>'
    ),
    'admin': mark_safe(
        '<rect x="3" y="11" width="18" height="11" rx="2" ry="2" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<path d="M7 11V7a5 5 0 0 1 10 0v4" fill="none" stroke="currentColor" stroke-width="2"/>'
    ),
}


def _has_any_role(user, *role_names):
    if user.is_superuser:
        return True
    if getattr(user, 'role', None) in role_names:
        return True
    return any(user.has_role(name) for name in role_names)


def sidebar_nav(request):
    """
    Builds the sidebar navigation as a single data structure so group/link
    visibility and the "active" state live in one place instead of being
    re-checked per link in the template.
    """
    user = request.user
    if not user.is_authenticated:
        return {}

    url_name = request.resolver_match.url_name if request.resolver_match else None
    groups = []

    # Overview
    overview_items = [{'label': 'Dashboard & Analytics', 'url_name': 'dashboard'}]
    if user.is_superuser:
        overview_items.append({'label': 'System Console', 'url_name': 'system'})
    overview_items.append({'label': 'Digital Assistant (AI)', 'url_name': None, 'css_class': 'ai-link'})
    if _has_any_role(user, 'Admin', 'Manager'):
        overview_items.append({
            'label': 'Action Center',
            'url_name': 'approvals_inbox',
            'show_badge': True,
        })
    groups.append({'id': 'overview', 'label': 'Overview', 'icon': NAV_ICONS['overview'], 'items': overview_items})

    # Inventory & Facilities
    inventory_items = []
    if user.has_perm('core.view_warehouse'):
        inventory_items.append({'label': 'Warehouse Inventory', 'url_name': 'warehouse_inventory', 'css_class': 'emerald-link'})
        inventory_items.append({'label': 'Facility Management', 'url_name': 'warehouse_list'})
    if user.has_perm('core.view_stockaudit') or user.has_perm('core.add_stockaudit'):
        inventory_items.append({'label': 'Stock Tally (Audit)', 'url_name': 'stock_audit'})
    if user.has_perm('core.view_registrylog'):
        inventory_items.append({'label': 'Registry Ledger', 'url_name': 'registry'})
    if inventory_items:
        groups.append({'id': 'inventory', 'label': 'Inventory & Facilities', 'icon': NAV_ICONS['inventory'], 'items': inventory_items})

    # Catalog & Directory
    catalog_items = []
    if user.has_perm('core.view_product'):
        catalog_items.append({'label': 'Products Catalog', 'url_name': 'product_list'})
    if user.has_perm('core.view_material'):
        catalog_items.append({'label': 'Materials Hub', 'url_name': 'material_list'})
    if user.has_perm('core.view_supplier'):
        catalog_items.append({'label': 'Suppliers', 'url_name': 'supplier_list'})
    if user.has_perm('core.view_client'):
        catalog_items.append({'label': 'Clients', 'url_name': 'client_list'})
    if catalog_items:
        groups.append({'id': 'catalog', 'label': 'Catalog & Directory', 'icon': NAV_ICONS['catalog'], 'items': catalog_items})

    # Operations & Logistics
    ops_items = []
    if user.has_perm('core.view_shipment'):
        ops_items.append({'label': 'Sales Orders', 'url_name': 'so_list'})
        ops_items.append({'label': 'Purchase Orders', 'url_name': 'po_list'})
        ops_items.append({'label': 'Logistics Tracker', 'url_name': 'shipments'})
    if user.has_perm('core.view_productrecipe') or user.has_perm('core.add_batch'):
        ops_items.append({'label': 'Manufacture', 'url_name': 'readiness'})
    if user.has_perm('core.change_batch'):
        ops_items.append({'label': 'QA & Spoilage Control', 'url_name': 'qa_dashboard', 'css_class': 'warning-link'})
    if ops_items:
        groups.append({'id': 'operations', 'label': 'Operations & Logistics', 'icon': NAV_ICONS['operations'], 'items': ops_items})

    # Admin
    if _has_any_role(user, 'Admin', 'Manager'):
        groups.append({
            'id': 'admin',
            'label': 'Admin',
            'icon': NAV_ICONS['admin'],
            'items': [{'label': 'Users Management', 'url_name': 'user_management'}],
        })

    for group in groups:
        group_active = False
        for item in group['items']:
            item['active'] = bool(item.get('url_name')) and item['url_name'] == url_name
            if item['active']:
                group_active = True
        group['active'] = group_active

    return {'nav_groups': groups}
