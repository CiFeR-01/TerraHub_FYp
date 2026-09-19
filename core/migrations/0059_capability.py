from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0058_backfill_snapshot_rent'),
    ]

    operations = [
        migrations.CreateModel(
            name='Capability',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ],
            options={
                'managed': False,
                'default_permissions': (),
                'permissions': [
                    ('approve_requests', 'Can approve requests (orders, shipments, production runs)'),
                    ('override_approvals', 'Can self-approve and decide any pending approval'),
                    ('adjust_physical_stock', 'Can manually adjust physical stock'),
                    ('manage_users', 'Can manage users and their facility access'),
                    ('set_order_status', 'Can set a sales order status by hand'),
                    ('handle_purchasing', 'Receives purchase orders drafted for material shortages'),
                ],
            },
        ),
    ]
