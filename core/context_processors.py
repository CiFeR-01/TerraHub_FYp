from django.conf import settings
from django.utils.safestring import mark_safe

from .permissions import can_approve, pending_actions, MANAGE_USERS, ACTION_PERMS, has_any_perm, is_admin_user

def qa_requests_count(request):
    """Open QA requests (sidebar badge on QA & Spoilage Control) for users who work QA, and the reasons the
    "Send to QA" pop-up (base.html) offers."""
    if not request.user.is_authenticated:
        return {'open_qa_requests_total': 0}
    from .models import QA_REASON_CHOICES
    out = {'qa_reason_choices': QA_REASON_CHOICES}
    if request.user.has_perm('core.perform_qa'):
        from .models import QARequest
        from .permissions import limited_warehouse_ids
        qs = QARequest.objects.filter(status__in=('Open', 'In Review'))
        ids = limited_warehouse_ids(request.user)
        if ids is not None:
            qs = qs.filter(batch__warehouse_id__in=ids)
        out['open_qa_requests_total'] = qs.count()
    else:
        out['open_qa_requests_total'] = 0
    return out


def approvals_count(request):
    """Number of items waiting on the user in the Action Center (sidebar badge), counted from the same querysets the page lists."""
    if request.user.is_authenticated and can_approve(request.user):
        total = sum(qs.count() for qs in pending_actions(request.user).values())
        return {'pending_approvals_total': total}
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
    'dss': mark_safe(
        '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<polygon points="15.5 8.5 13.5 13.5 8.5 15.5 10.5 10.5 15.5 8.5" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linejoin="round"/>'
    ),
    'analytics': mark_safe(
        '<path d="M3 3v18h18" fill="none" stroke="currentColor" stroke-width="2"/>'
        '<path d="M7 14l3-4 3 3 5-7" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    ),
}


def _perm_ok(user, perm):
    """perm may be None (always visible), a permission string, a tuple/list
    of permission strings (OR semantics), or a callable(user) -> bool."""
    if perm is None:
        return True
    if callable(perm):
        return perm(user)
    if isinstance(perm, (list, tuple)):
        return any(user.has_perm(p) for p in perm)
    return user.has_perm(perm)


# Decision Support: pages that recommend what to do next (Rent Reduction also acts on it and measures the result).
# Each gets its own sidebar link; they share one tab strip via `_analytics_tabs`.
DECISION_SUPPORT_PAGES = [
    ('rent_opportunities', 'Rent Reduction', 'core.view_warehouse'),
    ('forecast', 'Stockout & Reorder', 'core.view_material'),
    ('capacity_forecast', 'Inventory Capacity', 'core.view_warehouse'),
    ('so_delivery_risk', 'Delivery Performance', 'core.view_shipment'),
]

# Analytics taxonomy (scorecards and trends): (category label, [(url_name, page label, permission), ...]).
# Source for the sidebar category links and the tab strip in core/views_analytics.py's `_analytics_tabs`.
ANALYTICS_CATEGORIES = [
    ('Supply Chain', [
        ('supplier_scorecard', 'Supplier Performance', 'core.view_supplier'),
        ('shipment_logistics', 'Logistics Performance', 'core.view_shipment'),
    ]),
    ('Demand', [
        ('product_sales_trend', 'Demand & Sales Trends', 'core.view_product'),
    ]),
    ('Operations', [
        ('production_yield', 'Yield Performance', ('core.view_productrecipe', 'core.add_batch')),
        ('audit_accuracy', 'Inventory Audit Accuracy', ('core.view_stockaudit', 'core.add_stockaudit')),
        ('spoilage_report', 'Spoilage & Write-offs', ('core.perform_qa', 'core.dispose_stock', 'core.approve_qa_writeoffs')),
    ]),
]


def sidebar_nav(request):
    """Builds the sidebar navigation as one data structure so link visibility and the active state live in one place."""
    user = request.user
    if not user.is_authenticated:
        return {}

    url_name = request.resolver_match.url_name if request.resolver_match else None
    groups = []

    # Overview
    overview_items = [{'label': 'Dashboard & Analytics', 'url_name': 'dashboard'}]
    overview_items.append({
        'label': 'AI Copilot', 'url_name': 'briefing_hub',
        'alias_url_names': ['ops_briefing', 'category_briefing'], 'css_class': 'ai-link',
    })
    if can_approve(user):
        overview_items.append({
            'label': 'Action Center',
            'url_name': 'approvals_inbox',
            'show_badge': True,
        })
    groups.append({'id': 'overview', 'short': 'Home', 'label': 'Overview', 'icon': NAV_ICONS['overview'], 'items': overview_items})

    # Decision Support: one link per page
    dss_items = [{'label': label, 'url_name': name} for name, label, perm in DECISION_SUPPORT_PAGES if _perm_ok(user, perm)]
    if dss_items:
        groups.append({'id': 'dss', 'short': 'DSS', 'label': 'Decision Support', 'icon': NAV_ICONS['dss'], 'items': dss_items})

    # Analytics: one collapsed link per category, landing on the first report the user can see; siblings appear as tabs on the page.
    # The AI Copilot link lives in Overview.
    analytics_items = []
    for category_label, item_specs in ANALYTICS_CATEGORIES:
        visible = [url_name for url_name, label, perm in item_specs if _perm_ok(user, perm)]
        if visible:
            analytics_items.append({
                'label': category_label,
                'url_name': visible[0],
                'alias_url_names': visible[1:],
            })
    if analytics_items:
        groups.append({'id': 'analytics', 'short': 'Analytics', 'label': 'Analytics', 'icon': NAV_ICONS['analytics'], 'items': analytics_items})

    # Inventory & Facilities
    inventory_items = []
    if user.has_perm('core.view_warehouse'):
        inventory_items.append({'label': 'Warehouse Inventory', 'url_name': 'warehouse_inventory', 'css_class': 'emerald-link', 'alias_url_names': ['batch_detail']})
        inventory_items.append({'label': 'Facility Management', 'url_name': 'warehouse_list', 'alias_url_names': ['warehouse_create', 'warehouse_edit']})
    if user.has_perm('core.view_stockaudit') or user.has_perm('core.add_stockaudit'):
        inventory_items.append({'label': 'Stock Tally (Audit)', 'url_name': 'stock_audit'})
    if user.has_perm('core.view_registrylog'):
        inventory_items.append({'label': 'Registry Ledger', 'url_name': 'registry'})
    if inventory_items:
        groups.append({'id': 'inventory', 'short': 'Stock', 'label': 'Inventory & Facilities', 'icon': NAV_ICONS['inventory'], 'items': inventory_items})

    # Catalog & Directory
    catalog_items = []
    if user.has_perm('core.view_product'):
        catalog_items.append({'label': 'Products Catalog', 'url_name': 'product_list', 'alias_url_names': ['product_detail']})
    if user.has_perm('core.view_material'):
        catalog_items.append({'label': 'Materials Hub', 'url_name': 'material_list', 'alias_url_names': ['material_detail', 'material_edit']})
    if user.has_perm('core.view_supplier'):
        catalog_items.append({'label': 'Suppliers', 'url_name': 'supplier_list', 'alias_url_names': ['supplier_detail']})
    if user.has_perm('core.view_client'):
        catalog_items.append({'label': 'Clients', 'url_name': 'client_list', 'alias_url_names': ['client_detail']})
    if catalog_items:
        groups.append({'id': 'catalog', 'short': 'Catalog', 'label': 'Catalog & Directory', 'icon': NAV_ICONS['catalog'], 'items': catalog_items})

    # Operations & Logistics
    ops_items = []
    if user.has_perm('core.view_shipment'):
        ops_items.append({'label': 'Sales Orders', 'url_name': 'so_list', 'alias_url_names': ['so_detail', 'so_allocate']})
        ops_items.append({'label': 'Purchase Orders', 'url_name': 'po_list', 'alias_url_names': ['po_detail']})
        ops_items.append({'label': 'Logistics Tracker', 'url_name': 'shipments', 'alias_url_names': ['shipment_detail']})
    if user.has_perm('core.view_productrecipe') or user.has_perm('core.add_batch'):
        ops_items.append({'label': 'Manufacture', 'url_name': 'readiness', 'alias_url_names': ['production_run_detail', 'production_run_allocate']})
    if has_any_perm(user, ('core.perform_qa', 'core.dispose_stock', 'core.approve_qa_writeoffs')):
        ops_items.append({'label': 'QA & Spoilage Control', 'url_name': 'qa_dashboard', 'css_class': 'warning-link',
                          'show_qa_badge': True})
    if ops_items:
        groups.append({'id': 'operations', 'short': 'Orders', 'label': 'Operations & Logistics', 'icon': NAV_ICONS['operations'], 'items': ops_items})

    # Admin
    admin_items = []
    if user.has_perm(MANAGE_USERS):
        admin_items.append({'label': 'Users Management', 'url_name': 'user_management'})
        admin_items.append({'label': 'Roles & Permissions', 'url_name': 'roles'})
        admin_items.append({'label': 'Audit Log', 'url_name': 'user_audit_log'})
    if user.is_superuser or is_admin_user(user):
        admin_items.append({'label': 'System Settings', 'url_name': 'system_settings'})
    if admin_items:
        groups.append({
            'id': 'admin',
            'short': 'Admin',
            'label': 'Admin',
            'icon': NAV_ICONS['admin'],
            'items': admin_items,
        })

    for group in groups:
        group_active = False
        for item in group['items']:
            names = [item['url_name']] + item.get('alias_url_names', []) if item.get('url_name') else []
            item['active'] = url_name in names
            if item['active']:
                group_active = True
        group['active'] = group_active

    return {'nav_groups': groups}


def session_timeout(request):
    """Idle-timeout figures for the sign-out warning in base.html."""
    from .account import idle_timeout_seconds
    timeout = idle_timeout_seconds(request)
    return {
        'session_idle_timeout': timeout,
        'session_idle_warning': settings.SESSION_IDLE_WARNING,
        'session_idle_timeout_minutes': timeout // 60,
    }


def denied_actions(request):
    """Form actions on this page the user isn't allowed to run, for base.html to hide."""
    match = getattr(request, 'resolver_match', None)
    if not request.user.is_authenticated or not match:
        return {'denied_actions': []}
    perms = ACTION_PERMS.get(match.url_name, {})
    return {'denied_actions': sorted(a for a, perm in perms.items() if perm and not has_any_perm(request.user, perm))}
