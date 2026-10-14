"""Every "who may do X" rule. Code checks permissions (core.models.Capability or per-model), never role names."""
from django.db.models import Q

# Approving is per module: holding a module's permission lets you approve its requests, including your own.
APPROVE_SALES_ORDERS = 'core.approve_sales_orders'
APPROVE_PURCHASE_ORDERS = 'core.approve_purchase_orders'
APPROVE_PRODUCTION_RUNS = 'core.approve_production_runs'
APPROVE_SHIPMENTS = 'core.approve_shipments'
APPROVE_PERMS = (APPROVE_SALES_ORDERS, APPROVE_PURCHASE_ORDERS, APPROVE_PRODUCTION_RUNS, APPROVE_SHIPMENTS)
OVERRIDE_APPROVALS = 'core.override_approvals'
ADJUST_PHYSICAL_STOCK = 'core.adjust_physical_stock'
MANAGE_USERS = 'core.manage_users'
SET_ORDER_STATUS = 'core.set_order_status'
HANDLE_PURCHASING = 'core.handle_purchasing'
# Finished goods for orders and materials for runs are separate jobs, so separate permissions.
ALLOCATE_ORDER_STOCK = 'core.allocate_order_stock'
ALLOCATE_RUN_MATERIALS = 'core.allocate_run_materials'
CANCEL_ORDERS = 'core.cancel_orders'


def is_admin_user(user):
    """Admin-level access: every facility and the system tools. Grants no approval powers (those are per module)."""
    return user.has_perm(OVERRIDE_APPROVALS)


def approve_perm(obj):
    """The approve permission for `obj`'s module (a SalesOrder, PurchaseOrder, ProductionRun or Shipment)."""
    return {'SalesOrder': APPROVE_SALES_ORDERS, 'PurchaseOrder': APPROVE_PURCHASE_ORDERS,
            'ProductionRun': APPROVE_PRODUCTION_RUNS, 'Shipment': APPROVE_SHIPMENTS}[type(obj).__name__]


def can_approve(user):
    """Who may approve in at least one module (and so sees the Action Center)."""
    return has_any_perm(user, APPROVE_PERMS)


def can_grant_group(user, group):
    """A user may only grant or revoke a group whose permissions they hold themselves."""
    if user.is_superuser:
        return True
    return all(
        user.has_perm(f"{app_label}.{codename}")
        for app_label, codename in group.permissions.values_list('content_type__app_label', 'codename')
        # HANDLE_PURCHASING only marks who purchasing work is routed to; it isn't a power
        if f"{app_label}.{codename}" != HANDLE_PURCHASING
    )


def users_with_perm(perm, include_superusers=True):
    """Active users holding `perm` ('app_label.codename') directly or through
    a group. Superusers implicitly hold every permission."""
    from .models import CustomUser

    app_label, codename = perm.split('.', 1)
    grants = Q(groups__permissions__content_type__app_label=app_label,
               groups__permissions__codename=codename) | \
        Q(user_permissions__content_type__app_label=app_label,
          user_permissions__codename=codename)
    if include_superusers:
        grants |= Q(is_superuser=True)
    return CustomUser.objects.filter(grants, is_active=True).distinct()


def approvers(perm):
    """Users who can be sent an approval request needing `perm`, for the approver dropdowns."""
    return users_with_perm(perm).prefetch_related('groups').order_by('username')


def pending_actions(user):
    """The work items waiting on `user` in the Action Center, keyed by section.
    Both the page and the sidebar badge count from here so they always agree."""
    from .models import SalesOrder, PurchaseOrder, ProductionRun, Shipment

    def mine(model, perm, **filters):
        # Sections for a module the user can't approve in stay empty.
        qs = model.objects.filter(**filters)
        return qs if user.has_perm(perm) else qs.none()

    # Like may_decide_run_approval: unassigned runs are open to any run approver, a named approver's runs are theirs alone.
    runs = mine(ProductionRun, APPROVE_PRODUCTION_RUNS, status='Pending Approval').filter(
        Q(assigned_to__isnull=True) | Q(assigned_to=user))
    return {
        'pending_sos': mine(SalesOrder, APPROVE_SALES_ORDERS, status='Pending Approval', assigned_to=user)
            .prefetch_related('items').order_by('order_date'),
        'pending_runs': runs.select_related('target_product').order_by('start_time'),
        'pending_pos': mine(PurchaseOrder, APPROVE_PURCHASE_ORDERS, status='Pending Approval', assigned_to=user)
            .select_related('supplier', 'linked_production_run').prefetch_related('items').order_by('order_date'),
        'pending_shipment_approvals': mine(Shipment, APPROVE_SHIPMENTS, status='Pending Approval', assigned_to=user).order_by('-id'),
        'pending_shipments': mine(Shipment, APPROVE_SHIPMENTS, status='Discrepant', assigned_manager=user).order_by('-id'),
    }


# Permission needed for each form action, per page. Unlisted actions are open to any signed-in user.
ORDER = 'core.change_salesorder'
PURCHASE = 'core.change_purchaseorder'
RUN = 'core.change_productionrun'
SHIP = 'core.change_shipment'
RECEIVE = 'core.add_shipmentitemreceipt'
ACTION_PERMS = {
    'batch_detail': {'update_batch': 'core.change_batch'},
    'stock_audit': {'create': 'core.add_stockaudit', 'resolve': 'core.change_stockaudit'},
    'product_recipe_api': {a: 'core.change_productrecipe' for a in ('clone_recipe', 'delete_item', 'save_batch')},
    'product_list': {'create_product': 'core.add_product', 'add_recipe': 'core.add_productrecipe',
                     'toggle_active': 'core.change_product'},
    'product_detail': {'update_product': 'core.change_product'},
    'material_list': {'toggle_active': 'core.change_material'},
    'material_detail': {'toggle_active': 'core.change_material', 'update_material': 'core.change_material'},
    'supplier_list': {'toggle_active': 'core.change_supplier'},
    'client_list': {'toggle_active': 'core.change_client'},
    'so_list': {'create_so': 'core.add_salesorder', 'update_so_status': SET_ORDER_STATUS},
    'po_list': {'create_po': 'core.add_purchaseorder', 'update_po_status': PURCHASE},
    'so_detail': {a: ORDER for a in ('add_so_item', 'remove_so_item', 'request_approval', 'cancel_delivery_plan',
                                     'consolidate_stock', 'send_to_manufacturing', 'unallocate_so_stock',
                                     'update_so_header')} | {'update_so_status': SET_ORDER_STATUS, 'cancel_so': CANCEL_ORDERS},
    'po_detail': {a: PURCHASE for a in ('add_po_item', 'remove_po_item', 'request_approval', 'update_po_header',
                                        'update_po_status')} | {'mark_received': ('core.change_purchaseorderdetail', 'core.add_batch', RECEIVE)},
    'readiness': {'create_run': 'core.add_productionrun'} | {
        a: RUN for a in ('cancel_allocation', 'update_run_schedule', 'draft_po_from_shortage', 'start_run', 'complete_run')}
    | {'draft_transfer_from_shortage': ALLOCATE_RUN_MATERIALS},
    'shipments': {'create_shipment': 'core.add_shipment'},
    'qa_dashboard': {a: 'core.change_batch' for a in ('extend_expiry', 'quarantine', 'release_quarantine', 'spoil_dispose')},
    'shipment_detail': {a: SHIP for a in ('update_route', 'submit_to_logistics', 'submit_for_approval', 'skip_approval',
                                          'scrap_shipment', 'update_operational_status', 'reopen_shipment',
                                          'return_to_manufacturing', 'cancel_escalation')}
    | {'request_force_close': (SHIP, RECEIVE)}
    | {'add_item': 'core.change_shipmentitem', 'remove_item': 'core.change_shipmentitem'}
    | {a: RECEIVE for a in ('log_item_receipt', 'receive_all', 'complete_shipment', 'finalize_shipment_receiving')},
    'user_management': {'add_location': 'core.add_warehouse'},
    'so_allocate': {'allocate_manual': ALLOCATE_ORDER_STOCK},
    'production_run_allocate': {'allocate_run': ALLOCATE_RUN_MATERIALS},
    'production_run_detail': {a: RUN for a in ('cancel_allocation', 'complete_production', 'log_yield', 'pause_production',
                                               'remove_allocation', 'resume_production', 'schedule_run', 'scrap_run',
                                               'start_production')} | {'abort_run': CANCEL_ORDERS},
}


def has_any_perm(user, perm):
    """True if the user holds `perm`, or any of them when it's a tuple."""
    perms = perm if isinstance(perm, tuple) else (perm,)
    return any(user.has_perm(p) for p in perms)


def action_denied(request, page):
    """Return an error message if the user can't run the posted action, else None."""
    if request.method != 'POST':
        return None
    perm = ACTION_PERMS.get(page, {}).get(request.POST.get('action'))
    if perm and not has_any_perm(request.user, perm):
        return "You don't have permission to do that. Ask a Manager or Admin if you need it."
    return None


def limited_warehouse_ids(user):
    """IDs of the facilities the user is limited to, or None for all facilities."""
    if user.is_superuser or is_admin_user(user):
        return None
    ids = set(user.allowed_locations.values_list('id', flat=True))
    return ids or None


def may_use_warehouse(user, warehouse_id):
    ids = limited_warehouse_ids(user)
    if ids is None:
        return True
    try:
        return int(warehouse_id) in ids
    except (TypeError, ValueError):
        return False


FACILITY_REFUSED = "You're limited to other facilities, so you can't change stock here."
