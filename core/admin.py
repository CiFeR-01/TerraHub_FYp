from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    CustomUser, Warehouse, WarehouseLocation, Material, Product,
    ProductRecipe, ProductionRun, ProductionConsumption, Batch,
    PurchaseOrder, PurchaseOrderDetail, SalesOrder, SalesOrderDetail,
    Shipment, StockAudit, RegistryLog, OrderTimeline, Notification
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
