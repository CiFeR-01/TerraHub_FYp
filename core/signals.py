"""
Keeps today's WarehouseUtilizationSnapshot rows current without a cron job.

Whenever something that changes a warehouse's occupancy or rent is saved - a
batch (arriving, leaving, used up, quarantined, moved), a warehouse (rate,
capacity, billing method, type) or a material/product unit weight - the affected
warehouses' rows for today are rewritten once the transaction commits. The last
write of the day is that day's figure; a day with no write means nothing changed.

Bulk QuerySet.update()/bulk_create() skip these signals (the seed and simulation
scripts use them); analytics.ensure_today_snapshots() on the Rent Opportunities
and Capacity pages covers that gap for today.
"""
from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete, post_init, post_save
from django.dispatch import receiver

from .models import Batch, Material, Product, Warehouse

OPEN_BATCH_STATUSES = ('Active', 'Quarantined')


def _refresh(warehouse_ids):
    ids = {i for i in warehouse_ids if i}
    if ids:
        from .analytics import snapshot_warehouse_utilization
        transaction.on_commit(partial(snapshot_warehouse_utilization, warehouse_ids=ids))


@receiver(post_init, sender=Batch)
def _remember_batch_warehouse(sender, instance, **kwargs):
    instance._snapshot_orig_warehouse_id = instance.warehouse_id


@receiver(post_save, sender=Batch)
def _batch_saved(sender, instance, raw=False, **kwargs):
    if raw:
        return
    # a batch moved between warehouses changes both
    _refresh({instance.warehouse_id, getattr(instance, '_snapshot_orig_warehouse_id', None)})
    instance._snapshot_orig_warehouse_id = instance.warehouse_id


@receiver(post_delete, sender=Batch)
def _batch_deleted(sender, instance, **kwargs):
    _refresh({instance.warehouse_id})


@receiver(post_save, sender=Warehouse)
def _warehouse_saved(sender, instance, raw=False, **kwargs):
    if not raw:
        _refresh({instance.id})


def _remember_weight(sender, instance, **kwargs):
    instance._snapshot_orig_weight = instance.weight_mt_per_unit


def _weight_saved(sender, instance, raw=False, **kwargs):
    if raw or getattr(instance, '_snapshot_orig_weight', None) == instance.weight_mt_per_unit:
        return
    instance._snapshot_orig_weight = instance.weight_mt_per_unit
    field = 'material' if sender is Material else 'product'
    _refresh(set(
        Batch.objects.filter(**{field: instance}, status__in=OPEN_BATCH_STATUSES)
        .values_list('warehouse_id', flat=True).distinct()
    ))


for _model in (Material, Product):
    post_init.connect(_remember_weight, sender=_model, dispatch_uid=f'snapshot_weight_init_{_model.__name__}')
    post_save.connect(_weight_saved, sender=_model, dispatch_uid=f'snapshot_weight_save_{_model.__name__}')
