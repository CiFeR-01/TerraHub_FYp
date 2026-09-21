from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0060_roles_to_groups'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='customuser',
            name='roles',
        ),
        migrations.RemoveField(
            model_name='customuser',
            name='can_adjust_physical_stock',
        ),
        migrations.RemoveField(
            model_name='customuser',
            name='role',
        ),
        migrations.DeleteModel(
            name='Role',
        ),
    ]
