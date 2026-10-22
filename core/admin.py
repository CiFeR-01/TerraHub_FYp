from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    CustomUser, Warehouse, WarehouseLocation, Material, Product,
    ProductRecipe, ProductionRun, Batch,
    PurchaseOrder, PurchaseOrderDetail, SalesOrder, SalesOrderDetail,
    Shipment, ShipmentItem, StockAllocation,
    Supplier, SupplierMaterial, Client, SystemSetting,
    OpsBriefing, RentSuggestion,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """View-only: these rows are written by the app's own flows (stock moves, status rules, timeline),
    so editing them here would bypass that logic and leave stock out of step."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
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

# Transactions: view-only, for checking data while debugging.
for _model in (ProductionRun, PurchaseOrder, SalesOrder, SalesOrderDetail,
               Shipment, ShipmentItem, StockAllocation):
    admin.site.register(_model, ReadOnlyAdmin)

# Left out on purpose: logs and ledgers (ProductionConsumption, ShipmentItemReceipt, StockAudit,
# RegistryLog, OrderTimeline, Notification, UserAuditLog,
# WarehouseUtilizationSnapshot, BriefingTick) and run detail rows. Editing them desyncs stock or history.


@admin.register(Batch)
class BatchAdmin(ReadOnlyAdmin):
    list_display = ('batch_number', 'status', 'material', 'product', 'quantity',
                     'warehouse', 'rental_rate_per_mt', 'closed_date', 'expiry_date')
    list_filter = ('status', 'warehouse')
    search_fields = ('batch_number',)


@admin.register(PurchaseOrderDetail)
class PurchaseOrderDetailAdmin(ReadOnlyAdmin):
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
