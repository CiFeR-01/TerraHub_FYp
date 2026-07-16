from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('', views.home_view, name='home'),
    path('login/', auth_views.LoginView.as_view(template_name='login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='home'), name='logout'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('system/', views.system_view, name='system'),
    path('warehouse/inventory/', views.warehouse_inventory_view, name='warehouse_inventory'),
    path('warehouse/facilities/', views.facility_management_view, name='warehouse_list'),
    path('warehouse/create/', views.warehouse_create_view, name='warehouse_create'),
]
