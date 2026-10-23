"""
Fold the duplicate 'Purchaser' role into 'Purchasing' (migration 0060 gave both the same
permissions). Members move across, any extra permission Purchaser had is kept on Purchasing,
then Purchaser is deleted. Reverse is a no-op: there is nothing to split back.
"""
from django.db import migrations


def merge_purchaser(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    old = Group.objects.filter(name='Purchaser').first()
    if old is None:
        return
    new, _ = Group.objects.get_or_create(name='Purchasing')
    new.permissions.add(*old.permissions.all())
    for user in old.user_set.all():
        user.groups.add(new)
    old.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0075_material_reorder_point'),
    ]

    operations = [
        migrations.RunPython(merge_purchaser, migrations.RunPython.noop, elidable=False),
    ]
