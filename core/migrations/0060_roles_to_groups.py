"""
Move roles onto Django Groups. Each role a user had - the old `role` field or
the `roles` link to the Role table - becomes a Group of the same name (the two
Staff roles get readable names), the user joins it, and the group is given the
permissions that reproduce what the role could do when checks were hard-coded
by role name. Users with the old `can_adjust_physical_stock` flag get the
core.adjust_physical_stock permission directly.

The standard groups below are created even when no user holds them yet (a
fresh database). Groups are only added to, never trimmed, so re-running against a database
where an Admin has already tuned a group leaves their extra grants alone.
After this, what a role may do is edited in Django admin > Groups.
"""
from django.contrib.auth.management import create_permissions
from django.contrib.contenttypes.management import create_contenttypes
from django.db import migrations

RENAMED = {'Staff_Edit': 'Staff (Editor)', 'Staff_View': 'Staff (Viewer)'}

# Business data every role can at least look at (drives the sidebar).
BUSINESS_MODELS = [
    'warehouse', 'warehouselocation', 'material', 'product', 'productrecipe',
    'supplier', 'suppliermaterial', 'client', 'productionrun', 'productionrunyieldlog',
    'runmaterialusage', 'productionconsumption', 'batch', 'purchaseorder',
    'purchaseorderdetail', 'salesorder', 'salesorderdetail', 'shipment', 'shipmentitem',
    'shipmentitemreceipt', 'stockaudit', 'registrylog', 'ordertimeline', 'notification',
    'stockallocation', 'warehouseutilizationsnapshot', 'opsbriefing',
]
# Snapshots and AI briefings (a paid model call) stay Manager-and-up.
MANAGER_ONLY_MODELS = ['warehouseutilizationsnapshot', 'opsbriefing']


def _add_change(models):
    return [f'{action}_{m}' for m in models for action in ('add', 'change')]


VIEW_ALL = [f'view_{m}' for m in BUSINESS_MODELS]
EDIT_ALL = _add_change(BUSINESS_MODELS)
EDIT_STAFF = _add_change([m for m in BUSINESS_MODELS if m not in MANAGER_ONLY_MODELS])

# Marks who work gets routed to rather than a power, so Admins don't get it
# (they'd otherwise be picked to receive shortage POs).
ASSIGNMENT_ONLY = {'handle_purchasing'}

# Codenames (app core) per group, on top of VIEW_ALL. '*' = every core permission
# except the ones in ASSIGNMENT_ONLY.
GROUP_PERMS = {
    'Admin': '*',
    'Manager': EDIT_ALL + ['delete_stockallocation', 'approve_requests', 'manage_users', 'set_order_status'],
    'Staff (Editor)': EDIT_STAFF,
    'Staff (Viewer)': [],
    'Sales': _add_change(['salesorder', 'salesorderdetail', 'client', 'stockallocation', 'ordertimeline']),
    'Purchasing': _add_change(['purchaseorder', 'purchaseorderdetail', 'supplier', 'suppliermaterial', 'ordertimeline'])
    + ['handle_purchasing'],
    'Purchaser': _add_change(['purchaseorder', 'purchaseorderdetail', 'supplier', 'suppliermaterial', 'ordertimeline'])
    + ['handle_purchasing'],
    'Logistics': _add_change(['shipment', 'shipmentitem', 'shipmentitemreceipt', 'ordertimeline']),
    'Manufacturing': _add_change(['productionrun', 'productionrunyieldlog', 'runmaterialusage', 'productionconsumption',
                                  'productrecipe', 'batch', 'stockallocation', 'ordertimeline']),
    'Warehouse': _add_change(['batch', 'stockaudit', 'warehouselocation', 'registrylog', 'shipmentitemreceipt']),
}


def roles_to_groups(apps, schema_editor):
    # Permissions for new models (Capability) are normally created after
    # migrate finishes; make them now so they can be granted.
    core_config = apps.get_app_config('core')
    core_config.models_module = True
    create_contenttypes(core_config, verbosity=0, apps=apps)
    create_permissions(core_config, verbosity=0, apps=apps)
    core_config.models_module = None

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    CustomUser = apps.get_model('core', 'CustomUser')
    Role = apps.get_model('core', 'Role')

    core_perms = {p.codename: p for p in Permission.objects.filter(content_type__app_label='core')}

    role_names = set(Role.objects.values_list('name', flat=True))
    role_names |= set(CustomUser.objects.exclude(role='').values_list('role', flat=True))
    # Always seed the standard set so a fresh database starts with usable roles.
    role_names |= {name for name in GROUP_PERMS if name != 'Purchaser'}

    groups = {}
    for name in sorted(role_names):
        group_name = RENAMED.get(name, name)
        group, _ = Group.objects.get_or_create(name=group_name)
        spec = GROUP_PERMS.get(group_name, [])
        if spec == '*':
            perms = [p for c, p in core_perms.items() if c not in ASSIGNMENT_ONLY]
        else:
            perms = [core_perms[c] for c in VIEW_ALL + spec if c in core_perms]
        group.permissions.add(*perms)
        groups[name] = group

    for user in CustomUser.objects.all():
        names = set(user.roles.values_list('name', flat=True))
        if user.role:
            names.add(user.role)
        user.groups.add(*[groups[n] for n in names])
        if user.can_adjust_physical_stock:
            user.user_permissions.add(core_perms['adjust_physical_stock'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0059_capability'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(roles_to_groups, migrations.RunPython.noop),
    ]
