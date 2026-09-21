from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    CustomUser, Warehouse, WarehouseLocation, Material, Product,
    ProductRecipe, ProductionRun, ProductionConsumption, Batch,
    PurchaseOrder, PurchaseOrderDetail, SalesOrder, SalesOrderDetail,
    Shipment, StockAudit, RegistryLog, OrderTimeline, Notification,
    Supplier, SupplierMaterial, Client, SystemSetting, WarehouseUtilizationSnapshot,
    OpsBriefing, RentSuggestion,
)

class CustomUserAdmin(UserAdmin):
    model = CustomUser
    # Roles are the "Groups" field under Permissions; what each role may do is
    # set on its Group (Authentication and Authorization > Groups).
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
admin.site.register(Warehouse)
admin.site.register(WarehouseLocation)
admin.site.register(Material)
admin.site.register(Product)
admin.site.register(ProductRecipe)
admin.site.register(ProductionRun)
admin.site.register(ProductionConsumption)
admin.site.register(PurchaseOrder)
admin.site.register(SalesOrder)
admin.site.register(SalesOrderDetail)
admin.site.register(Shipment)
admin.site.register(StockAudit)
admin.site.register(RegistryLog)
admin.site.register(OrderTimeline)
admin.site.register(Notification)
admin.site.register(Supplier)
admin.site.register(SupplierMaterial)
admin.site.register(Client)


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ('batch_number', 'status', 'material', 'product', 'quantity',
                     'warehouse', 'rental_rate_per_mt', 'closed_date', 'expiry_date')
    list_filter = ('status', 'warehouse')
    search_fields = ('batch_number',)


@admin.register(PurchaseOrderDetail)
class PurchaseOrderDetailAdmin(admin.ModelAdmin):
    list_display = ('purchase_order', 'material', 'quantity_ordered', 'quantity_received',
                     'unit_price', 'negotiated_rental_rate_per_mt')
    list_filter = ('purchase_order__status',)
    search_fields = ('purchase_order__po_number', 'material__sku')


@admin.register(WarehouseUtilizationSnapshot)
class WarehouseUtilizationSnapshotAdmin(admin.ModelAdmin):
    list_display = ('warehouse', 'snapshot_date', 'utilization_percent', 'used_mt', 'capacity_mt')
    list_filter = ('warehouse', 'snapshot_date')
    date_hierarchy = 'snapshot_date'


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


@admin.register(OpsBriefing)
class OpsBriefingAdmin(admin.ModelAdmin):
    list_display = ('generated_at', 'category', 'period', 'status', 'model_id',
                    'signal_count', 'input_tokens', 'output_tokens', 'generated_by', 'headline')
    list_filter = ('category', 'status', 'period', 'model_id')
    date_hierarchy = 'generated_at'
    readonly_fields = ('generated_at', 'category', 'period', 'status', 'headline', 'body_text',
                       'signals_json', 'signal_count', 'model_id', 'input_tokens',
                       'output_tokens', 'error_detail', 'generated_by')

    def has_add_permission(self, request):
        # Created by generate_ops_briefing / the briefing page, never hand-typed.
        return False


@admin.register(RentSuggestion)
class RentSuggestionAdmin(admin.ModelAdmin):
    list_display = ('decided_at', 'decision', 'batch_number', 'origin_warehouse', 'destination_warehouse',
                    'move_mt', 'est_daily_saving', 'dismiss_reason', 'decided_by', 'shipment')
    list_filter = ('decision', 'dismiss_reason', 'origin_warehouse')
    search_fields = ('batch_number', 'item_name')
    date_hierarchy = 'decided_at'
