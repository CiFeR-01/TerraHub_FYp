from django.core.exceptions import PermissionDenied
from functools import wraps
from django.contrib import messages
from django.shortcuts import redirect

def role_required(allowed_roles):
    """
    Decorator for views that checks that the user has one of the allowed roles,
    raising a PermissionDenied (403) exception if they do not.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login
                return redirect_to_login(request.get_full_path())
            
            # Allow superusers unconditionally
            if request.user.is_superuser:
                return view_func(request, *args, **kwargs)
                
            if hasattr(request.user, 'role') and request.user.role in allowed_roles:
                return view_func(request, *args, **kwargs)
                
            messages.error(request, "Permission Denied: You do not have the required role to access this area.")
            return redirect('dashboard')
            
        return _wrapped_view
    return decorator
