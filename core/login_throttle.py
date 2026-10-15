"""Login throttling for /login/ and /admin/: refuses sign-in after repeated failures.

Two counters, both kept in the default cache for LOGIN_LOCKOUT_SECONDS from the first failure:
- username + IP: locks after LOGIN_FAIL_LIMIT failures (the normal case, one person guessing).
- username alone: locks after 4x that, so guessing spread over many IPs is still capped,
  while one stranger can't lock a real user out with just a few bad attempts.
There is no IP-only counter because behind Railway's proxy many users can share one address.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.dispatch import receiver

LOCKED_MESSAGE = 'Too many failed sign-in attempts. Please wait {minutes} minutes and try again.'


def _client_ip(request):
    if request is None:
        return 'unknown'
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return forwarded.split(',')[0].strip() or request.META.get('REMOTE_ADDR', 'unknown')


def _keys(username, request):
    name = (username or '').strip().lower()
    return f'login-fail:user:{name}', f'login-fail:pair:{name}:{_client_ip(request)}'


def is_locked(username, request):
    user_key, pair_key = _keys(username, request)
    limit = settings.LOGIN_FAIL_LIMIT
    return cache.get(pair_key, 0) >= limit or cache.get(user_key, 0) >= limit * 4


def _bump(key):
    cache.add(key, 0, settings.LOGIN_LOCKOUT_SECONDS)
    try:
        cache.incr(key)
    except ValueError:  # key expired between add and incr, or a dummy cache
        pass


@receiver(user_login_failed)
def record_failure(sender, credentials, request=None, **kwargs):
    username = credentials.get('username') or credentials.get(get_user_model().USERNAME_FIELD)
    for key in _keys(username, request):
        _bump(key)


@receiver(user_logged_in)
def clear_failures(sender, request, user, **kwargs):
    cache.delete_many(_keys(user.get_username(), request))


class ThrottledModelBackend(ModelBackend):
    """ModelBackend that refuses to check passwords while the username is locked out."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(get_user_model().USERNAME_FIELD)
        if is_locked(username, request):
            raise PermissionDenied  # stops authenticate() and counts as a failed login
        return super().authenticate(request, username=username, password=password, **kwargs)


class ThrottledAuthenticationForm(AuthenticationForm):
    """Login form that says why sign-in is refused while locked out, instead of 'wrong password'."""

    def clean(self):
        if is_locked(self.cleaned_data.get('username'), self.request):
            raise ValidationError(LOCKED_MESSAGE.format(minutes=settings.LOGIN_LOCKOUT_SECONDS // 60),
                                  code='locked')
        return super().clean()
