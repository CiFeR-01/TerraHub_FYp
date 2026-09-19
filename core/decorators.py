from functools import wraps
from django.contrib import messages
from django.shortcuts import redirect

def permission_or_redirect(perm):
    """
    Decorator for views that need `perm` (e.g. 'core.approve_requests').
    Users without it are sent back to the dashboard with a message instead of
    a bare 403 page.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login
                return redirect_to_login(request.get_full_path())

            if request.user.has_perm(perm):
                return view_func(request, *args, **kwargs)

            messages.error(request, "Permission Denied: You do not have permission to access this area.")
            return redirect('dashboard')

        return _wrapped_view
    return decorator
