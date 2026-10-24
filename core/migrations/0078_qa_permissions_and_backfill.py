from decimal import Decimal

from django.contrib.auth.management import create_permissions
from django.contrib.contenttypes.management import create_contenttypes
from django.db import migrations
from django.db.models import F

# Each new QA permission goes to the groups that could already do that job, judged by what they hold (never by group name):
# send to QA -> any group that edits something in core; QA work and write-offs -> groups that edit batches;
# approving big write-offs -> groups that approve anything.
def _wants(codename, held):
    if codename == 'request_qa':
        return any(c.startswith('change_') for c in held)
    if codename in ('perform_qa', 'dispose_stock'):
        return 'change_batch' in held
    if codename == 'approve_qa_writeoffs':
        return any(c.startswith('approve_') for c in held)
    return False


def grant_new_permissions(apps, schema_editor):
    core_config = apps.get_app_config('core')
    core_config.models_module = True
    create_contenttypes(core_config, verbosity=0, apps=apps)
    create_permissions(core_config, verbosity=0, apps=apps)
    core_config.models_module = None

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    new = {p.codename: p for p in Permission.objects.filter(
        content_type__app_label='core',
        codename__in=('request_qa', 'perform_qa', 'dispose_stock', 'approve_qa_writeoffs'))}
    for group in Group.objects.all():
        held = set(group.permissions.filter(content_type__app_label='core').values_list('codename', flat=True))
        for codename, perm in new.items():
            if _wants(codename, held):
                group.permissions.add(perm)


def backfill(apps, schema_editor):
    Batch = apps.get_model('core', 'Batch')
    QAEvent = apps.get_model('core', 'QAEvent')
    RegistryLog = apps.get_model('core', 'RegistryLog')

    # Every batch remembers the expiry it arrived with, so shelf-life extensions can be capped.
    Batch.objects.filter(original_expiry_date__isnull=True, expiry_date__isnull=False).update(original_expiry_date=F('expiry_date'))

    # Lost-in-transit stock was logged as a disposal before it had its own action.
    RegistryLog.objects.filter(action_type='Spoiled_Disposal', item_name__icontains='lost in transit').update(action_type='Lost_In_Transit')

    # Older write-offs get a QA history entry so the spoilage report and batch pages include them.
    done = set(QAEvent.objects.filter(action='disposed').values_list('batch_id', flat=True))
    for b in Batch.objects.filter(status='Spoiled').exclude(id__in=done).select_related('material', 'product'):
        log = (RegistryLog.objects.filter(action_type='Spoiled_Disposal', item_name__contains=b.batch_number)
               .order_by('-timestamp').first())
        qty = log.quantity_changed if log else b.quantity
        if b.material_id:
            unit = Decimal(str(b.material.cost_per_unit or 0))
        elif b.product_id:
            unit = Decimal(str(b.product.price_per_unit or 0))
        else:
            unit = Decimal('0')
        extra = {'created_at': log.timestamp} if log else {}
        QAEvent.objects.create(
            batch=b, action='disposed', reason='other', quantity=qty,
            value_rm=(Decimal(str(qty or 0)) * unit).quantize(Decimal('0.01')),
            note='Recorded before the QA upgrade.', user=log.user if log else None, **extra,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0077_qa_spoilage'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(grant_new_permissions, migrations.RunPython.noop),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
