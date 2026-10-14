from django.contrib.auth.management import create_permissions
from django.contrib.contenttypes.management import create_contenttypes
from django.db import migrations

MODULE_PERMS = ('approve_sales_orders', 'approve_purchase_orders', 'approve_production_runs', 'approve_shipments')


def split_approve_requests(apps, schema_editor):
    """Whoever could approve everything (a group or a user holding approve_requests) gets all four module permissions."""
    core_config = apps.get_app_config('core')
    core_config.models_module = True
    create_contenttypes(core_config, verbosity=0, apps=apps)
    create_permissions(core_config, verbosity=0, apps=apps)
    core_config.models_module = None

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    CustomUser = apps.get_model('core', 'CustomUser')
    # create_permissions doesn't rename existing permissions; override_approvals no longer grants approval powers.
    Permission.objects.filter(content_type__app_label='core', codename='override_approvals').update(
        name='Can work in every facility and use the system tools')
    new = list(Permission.objects.filter(content_type__app_label='core', codename__in=MODULE_PERMS))
    old = Permission.objects.filter(content_type__app_label='core', codename='approve_requests').first()
    if old is None:
        return
    for group in Group.objects.filter(permissions=old):
        group.permissions.add(*new)
    for user in CustomUser.objects.filter(user_permissions=old):
        user.user_permissions.add(*new)
    old.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0069_sales_order_cancel_and_permissions'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='capability',
            options={'default_permissions': (), 'managed': False, 'permissions': [('approve_sales_orders', 'Can approve sales orders'), ('approve_purchase_orders', 'Can approve purchase orders'), ('approve_production_runs', 'Can approve production runs'), ('approve_shipments', 'Can approve shipments and force-close discrepant ones'), ('override_approvals', 'Can work in every facility and use the system tools'), ('adjust_physical_stock', 'Can manually adjust physical stock'), ('manage_users', 'Can manage users and their facility access'), ('set_order_status', 'Can set a sales order status by hand'), ('handle_purchasing', 'Receives purchase orders drafted for material shortages'), ('allocate_order_stock', 'Can reserve finished goods for sales orders'), ('allocate_run_materials', 'Can reserve materials for production runs'), ('cancel_orders', 'Can cancel sales orders and abort production runs')]},
        ),
        migrations.RunPython(split_approve_requests, migrations.RunPython.noop),
    ]
