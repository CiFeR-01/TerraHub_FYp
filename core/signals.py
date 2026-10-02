"""Keeps today's WarehouseUtilizationSnapshot rows current without a cron job.

When a batch, warehouse or unit weight changes, the affected warehouses' rows for today are rewritten after the transaction commits.
Bulk update()/bulk_create() skip these signals; the Capacity page warns on drift and "Run snapshot now" (or snapshot_utilization) repairs it."""
from functools import partial

from django.db import transaction
from django.db.models.signals import post_delete, post_init, post_save
from django.dispatch import receiver

from .analytics_cache import invalidate as _invalidate_analytics
from .models import (
    Batch, Material, Product, ProductionRun, PurchaseOrder, PurchaseOrderDetail, RegistryLog,
    RentSuggestion, SalesOrder, SalesOrderDetail, Shipment, ShipmentItem, SupplierMaterial,
    SystemSetting, Warehouse, WarehouseUtilizationSnapshot,
)

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


# Changes to data the cached analytics read drop the cache after commit (bulk update()/bulk_create() skip signals; the short TTL covers those).
def _analytics_changed(sender, raw=False, **kwargs):
    if not raw:
        transaction.on_commit(_invalidate_analytics)


for _model in (Batch, Warehouse, Material, Product, SupplierMaterial, ProductionRun, PurchaseOrder,
               PurchaseOrderDetail, SalesOrder, SalesOrderDetail, Shipment, ShipmentItem, RegistryLog,
               RentSuggestion, SystemSetting, WarehouseUtilizationSnapshot):
    for _signal in (post_save, post_delete):
        _signal.connect(_analytics_changed, sender=_model,
                        dispatch_uid=f'analytics_cache_{_signal.__class__.__name__}_{_model.__name__}')
