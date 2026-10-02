"""Every "who may do X" rule lives here. Roles are Django Groups; code asks about permissions, never role names.

App-wide permissions are declared on core.models.Capability; the rest are Django's per-model ones (core.change_product etc)."""
from django.db.models import Q

APPROVE_REQUESTS = 'core.approve_requests'
OVERRIDE_APPROVALS = 'core.override_approvals'
ADJUST_PHYSICAL_STOCK = 'core.adjust_physical_stock'
MANAGE_USERS = 'core.manage_users'
SET_ORDER_STATUS = 'core.set_order_status'
HANDLE_PURCHASING = 'core.handle_purchasing'


def is_admin_user(user):
    """Admin-level override: may self-approve and decide any pending approval."""
    return user.has_perm(OVERRIDE_APPROVALS)


def can_approve(user):
    """Who may approve anything at all (and be picked as an approver)."""
    return user.has_perm(APPROVE_REQUESTS)


def can_grant_group(user, group):
    """A user may only hand out (or take away) a group whose permissions they
    hold themselves, so nobody can promote themselves or anyone else above
    their own level."""
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


def approvers():
    """Users who can be sent an approval request, for the approver dropdowns."""
    return users_with_perm(APPROVE_REQUESTS).prefetch_related('groups').order_by('username')


def pending_actions(user):
    """The work items waiting on `user` in the Action Center, keyed by section.
    Both the page and the sidebar badge count from here so they always agree."""
    from .models import SalesOrder, PurchaseOrder, ProductionRun, Shipment

    runs = ProductionRun.objects.filter(status='Pending Approval')
    if not is_admin_user(user):
        # Like may_decide_run_approval: unassigned runs are open to any approver, a named approver's runs are theirs alone.
        runs = runs.filter(Q(assigned_to__isnull=True) | Q(assigned_to=user))
    return {
        'pending_sos': SalesOrder.objects.filter(status='Pending Approval', assigned_to=user)
            .prefetch_related('items').order_by('order_date'),
        'pending_runs': runs.select_related('target_product').order_by('start_time'),
        'pending_pos': PurchaseOrder.objects.filter(status='Pending Approval', assigned_to=user)
            .select_related('supplier', 'linked_production_run').prefetch_related('items').order_by('order_date'),
        'pending_shipment_approvals': Shipment.objects.filter(status='Pending Approval', assigned_to=user).order_by('-id'),
        'pending_shipments': Shipment.objects.filter(status='Discrepant', assigned_manager=user).order_by('-id'),
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
                                     'update_so_header')} | {'update_so_status': SET_ORDER_STATUS},
    'po_detail': {a: PURCHASE for a in ('add_po_item', 'remove_po_item', 'request_approval', 'update_po_header',
                                        'update_po_status')} | {'mark_received': 'core.change_purchaseorderdetail'},
    'readiness': {'create_run': 'core.add_productionrun'} | {
        a: RUN for a in ('cancel_allocation', 'update_run_schedule', 'draft_transfer_from_shortage',
                         'draft_po_from_shortage', 'start_run', 'complete_run')},
    'shipments': {'create_shipment': 'core.add_shipment'},
    'qa_dashboard': {a: 'core.change_batch' for a in ('extend_expiry', 'quarantine', 'release_quarantine', 'spoil_dispose')},
    'shipment_detail': {a: SHIP for a in ('update_route', 'submit_to_logistics', 'submit_for_approval', 'skip_approval',
                                          'scrap_shipment', 'update_operational_status', 'reopen_shipment',
                                          'return_to_manufacturing', 'request_force_close', 'cancel_escalation')}
    | {'add_item': 'core.change_shipmentitem', 'remove_item': 'core.change_shipmentitem'}
    | {a: RECEIVE for a in ('log_item_receipt', 'receive_all', 'complete_shipment', 'finalize_shipment_receiving')},
    'user_management': {'add_location': 'core.add_warehouse'},
    'so_allocate': {'allocate_manual': 'core.add_stockallocation'},
    'production_run_allocate': {'allocate_run': RUN},
    'production_run_detail': {a: RUN for a in ('cancel_allocation', 'complete_production', 'log_yield', 'pause_production',
                                               'remove_allocation', 'resume_production', 'schedule_run', 'scrap_run',
                                               'start_production')},
}


def action_denied(request, page):
    """Return an error message if the user can't run the posted action, else None."""
    if request.method != 'POST':
        return None
    perm = ACTION_PERMS.get(page, {}).get(request.POST.get('action'))
    if perm and not request.user.has_perm(perm):
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
