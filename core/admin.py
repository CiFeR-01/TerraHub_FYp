from django import forms
from django.contrib import admin
from django.db import transaction
from django.contrib.auth.admin import UserAdmin
from .models import (
    CustomUser, Warehouse, WarehouseLocation, Material, Product,
    ProductRecipe, ProductionRun, Batch,
    PurchaseOrder, PurchaseOrderDetail, SalesOrder, SalesOrderDetail,
    Shipment, ShipmentItem, StockAllocation,
    Supplier, SupplierMaterial, Client, SystemSetting, RegistryLog,
    OpsBriefing, RentSuggestion,
)


# Only IT (superusers) can open the Django admin; staff flags and model permissions don't grant it.
admin.site.has_permission = lambda request: request.user.is_active and request.user.is_superuser


class EditableAdmin(admin.ModelAdmin):
    """Transactions: editable, but rows are only added or deleted through the app's flows."""

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyAdmin(EditableAdmin):
    """View-only history (briefings, rent decisions)."""

    def has_change_permission(self, request, obj=None):
        return False


class CustomUserAdmin(UserAdmin):
    model = CustomUser
    # Roles are Groups (Authentication and Authorization > Groups).
    fieldsets = UserAdmin.fieldsets + (
        ('Custom Attributes', {'fields': ('branch',)}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Role & Branch', {'fields': ('groups', 'branch')}),
    )
    list_display = UserAdmin.list_display + ('role_label', 'branch')

    @admin.display(description='Role')
    def role_label(self, obj):
        return obj.role_label

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('groups')

admin.site.register(CustomUser, CustomUserAdmin)

# Master data: safe to fix by hand.
admin.site.register(Warehouse)
admin.site.register(WarehouseLocation)
admin.site.register(Material)
admin.site.register(Product)
admin.site.register(ProductRecipe)
admin.site.register(Supplier)
admin.site.register(SupplierMaterial)
admin.site.register(Client)

admin.site.register(ProductionRun, EditableAdmin)
admin.site.register(PurchaseOrder, EditableAdmin)
admin.site.register(SalesOrder, EditableAdmin)
admin.site.register(Shipment, EditableAdmin)


@admin.register(SalesOrderDetail)
class SalesOrderDetailAdmin(EditableAdmin):
    readonly_fields = ('quantity_shipped',)


@admin.register(ShipmentItem)
class ShipmentItemAdmin(EditableAdmin):
    readonly_fields = ('quantity', 'received_quantity')


@admin.register(StockAllocation)
class StockAllocationAdmin(EditableAdmin):
    # moving an allocation to another batch would leave both batches' allocated_quantity wrong
    readonly_fields = ('batch', 'quantity')

# Left out on purpose: logs and ledgers (ProductionConsumption, ShipmentItemReceipt, StockAudit,
# RegistryLog, OrderTimeline, Notification, UserAuditLog,
# WarehouseUtilizationSnapshot, BriefingTick) and run detail rows. Editing them desyncs stock or history.


class BatchAdminForm(forms.ModelForm):
    class Meta:
        model = Batch
        fields = '__all__'

    def clean_warehouse(self):
        new = self.cleaned_data.get('warehouse')
        b = self.instance
        if not b.pk or new == b.warehouse:
            return new
        if b.allocated_quantity > 0 or b.allocations.exists():
            raise forms.ValidationError("This batch is reserved. Release its reservations before moving it.")
        on_shipment = (ShipmentItem.objects.filter(batch=b)
                       .exclude(shipment__status__in=('Completed', 'Cancelled'))
                       .values_list('shipment__tracking_number', flat=True).first())
        if on_shipment:
            raise forms.ValidationError(f"This batch is on open shipment {on_shipment}. Finish or cancel it first.")
        if b.qa_requests.filter(status__in=('Open', 'In Review')).exists():
            raise forms.ValidationError("This batch has an open QA request. Close it before moving the batch.")
        return new


@admin.register(Batch)
class BatchAdmin(EditableAdmin):
    form = BatchAdminForm
    readonly_fields = ('quantity', 'allocated_quantity')
    list_display = ('batch_number', 'status', 'material', 'product', 'quantity',
                     'warehouse', 'rental_rate_per_mt', 'closed_date', 'expiry_date')
    list_filter = ('status', 'warehouse')
    search_fields = ('batch_number',)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not (change and 'warehouse' in form.changed_data):
            return
        # Facility corrected by hand: log it out of the old facility and into the new one.
        # Capacity snapshots refresh from the Batch post_save signal.
        old = Warehouse.objects.filter(pk=form.initial.get('warehouse')).first()
        for action, wh in (('Outbound', old), ('Inbound', obj.warehouse)):
            RegistryLog.objects.create(
                action_type=action,
                item_name=f"Facility corrected in admin: Batch {obj.batch_number}",
                material=obj.material, quantity_changed=obj.quantity, warehouse=wh, user=request.user,
            )


@admin.register(PurchaseOrderDetail)
class PurchaseOrderDetailAdmin(EditableAdmin):
    readonly_fields = ('quantity_received',)
    list_display = ('purchase_order', 'material', 'quantity_ordered', 'quantity_received',
                     'unit_price', 'negotiated_rental_rate_per_mt')
    list_filter = ('purchase_order__status',)
    search_fields = ('purchase_order__po_number', 'material__sku')


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'value_type', 'description', 'updated_at', 'updated_by')
    list_editable = ('value',)
    readonly_fields = ('key', 'value_type', 'description', 'updated_at', 'updated_by')
    search_fields = ('key', 'description')

    def has_add_permission(self, request):
        # Rows are seeded by migration from core.settings_store.REGISTRY.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# History the app only partly shows (last 8 briefings per page; dismissed rent moves only as a count).
@admin.register(OpsBriefing)
class OpsBriefingAdmin(ReadOnlyAdmin):
    list_display = ('generated_at', 'category', 'period', 'status', 'model_id',
                    'signal_count', 'input_tokens', 'output_tokens', 'generated_by', 'headline')
    list_filter = ('category', 'status', 'period', 'model_id')
    date_hierarchy = 'generated_at'


@admin.register(RentSuggestion)
class RentSuggestionAdmin(ReadOnlyAdmin):
    list_display = ('decided_at', 'decision', 'batch_number', 'origin_warehouse', 'destination_warehouse',
                    'move_mt', 'est_daily_saving', 'dismiss_reason', 'decided_by', 'shipment')
    list_filter = ('decision', 'dismiss_reason', 'origin_warehouse')
    search_fields = ('batch_number', 'item_name')
    date_hierarchy = 'decided_at'
