from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    CustomUser, Warehouse, WarehouseLocation, Material, Product,
    ProductRecipe, ProductionRun, ProductionConsumption, Batch,
    PurchaseOrder, PurchaseOrderDetail, SalesOrder, SalesOrderDetail,
    Shipment, StockAudit, RegistryLog, OrderTimeline, Notification,
    Supplier, SupplierMaterial, Client, SystemSetting, WarehouseUtilizationSnapshot,
    OpsBriefing,
)

class CustomUserAdmin(UserAdmin):
    model = CustomUser
    fieldsets = UserAdmin.fieldsets + (
        ('Custom Attributes', {'fields': ('role', 'branch', 'can_adjust_physical_stock')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Custom Attributes', {'fields': ('role', 'branch', 'can_adjust_physical_stock')}),
    )
    list_display = UserAdmin.list_display + ('role', 'branch', 'can_adjust_physical_stock')

admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(Warehouse)
admin.site.register(WarehouseLocation)
admin.site.register(Material)
admin.site.register(Product)
admin.site.register(ProductRecipe)
admin.site.register(ProductionRun)
admin.site.register(ProductionConsumption)
admin.site.register(Batch)
admin.site.register(PurchaseOrder)
admin.site.register(PurchaseOrderDetail)
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
