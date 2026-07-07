from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CustomUser

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
