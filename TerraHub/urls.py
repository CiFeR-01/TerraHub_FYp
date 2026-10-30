"""URL configuration for TerraHub."""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('core.urls')),
]

handler400 = 'core.errors.bad_request'
handler403 = 'core.errors.permission_denied'
handler404 = 'core.errors.page_not_found'
handler500 = 'core.errors.server_error'

