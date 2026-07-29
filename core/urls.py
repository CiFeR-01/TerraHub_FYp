from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('', views.home_view, name='home'),
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='home'), name='logout'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('system/', views.system_view, name='system'),
    path('system/db-logs/', views.db_logs_api_view, name='db_logs_api'),
    path('system/db-logs/clear/', views.db_clear_logs_view, name='db_clear_logs'),
    path('system/db-logs/test/', views.db_test_op_view, name='db_test_op'),
    path('warehouse/inventory/', views.warehouse_inventory_view, name='warehouse_inventory'),
    path('warehouse/facilities/', views.facility_management_view, name='warehouse_list'),
    path('warehouse/create/', views.warehouse_create_view, name='warehouse_create'),
    path('warehouse/<int:pk>/edit/', views.warehouse_edit_view, name='warehouse_edit'),
    path('warehouse/stock-audit/', views.stock_audit_view, name='stock_audit'),
    path('warehouse/registry/', views.registry_ledger_view, name='registry'),
    path('catalog/products/', views.product_list_view, name='product_list'),
    path('catalog/materials/', views.material_list_view, name='material_list'),
    path('operations/orders/', views.order_list_view, name='order_list'),
    path('operations/orders/so/<int:pk>/', views.so_detail_view, name='so_detail'),
    path('operations/orders/po/<int:pk>/', views.po_detail_view, name='po_detail'),
    path('operations/shipments/', views.shipments_view, name='shipments'),
    path('operations/manufacture/', views.manufacturing_view, name='readiness'),
    path('operations/qa/', views.qa_dashboard_view, name='qa_dashboard'),
]

