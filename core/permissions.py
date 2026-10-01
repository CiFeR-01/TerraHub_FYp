"""
Every "who may do X" rule lives here. Roles are Django Groups managed in the
Django admin; code only ever asks about permissions, never about a role or
group name, so adding or reshaping a role needs no code change.

The app-wide permissions are declared on core.models.Capability; the rest are
Django's normal per-model ones (core.change_product etc).
"""
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
        # Mirrors may_decide_run_approval: unassigned runs are open to any
        # approver, a named approver's runs are theirs alone.
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
