"""Login throttling for /login/ and /admin/: refuses sign-in after repeated failures.

Two counters, both kept in the default cache for the lockout window from the first failure:
- username + IP: locks after the fail limit (the normal case, one person guessing).
- username alone: locks after 4x that, so guessing spread over many IPs is still capped,
  while one stranger can't lock a real user out with just a few bad attempts.
There is no IP-only counter because behind Railway's proxy many users can share one address.
The limit and window are the login_fail_limit / login_lockout_minutes settings (System Settings page),
defaulting to LOGIN_FAIL_LIMIT / LOGIN_LOCKOUT_SECONDS. Admins can clear a lockout with unlock().
"""
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


def fail_limit():
    from .settings_store import get_setting
    return get_setting('login_fail_limit')


def lockout_seconds():
    from .settings_store import get_setting
    return get_setting('login_lockout_minutes') * 60


def _ips_key(username):
    return f'login-fail:ips:{(username or "").strip().lower()}'


def is_locked(username, request):
    user_key, pair_key = _keys(username, request)
    limit = fail_limit()
    return cache.get(pair_key, 0) >= limit or cache.get(user_key, 0) >= limit * 4


def is_locked_anywhere(username):
    """True if `username` is locked out from any address (for the Users page)."""
    name = (username or '').strip().lower()
    limit = fail_limit()
    if cache.get(f'login-fail:user:{name}', 0) >= limit * 4:
        return True
    return any(cache.get(f'login-fail:pair:{name}:{ip}', 0) >= limit for ip in cache.get(_ips_key(name), ()))


def unlock(username):
    """Clear every failed-sign-in counter for `username`."""
    name = (username or '').strip().lower()
    ips = cache.get(_ips_key(name), ())
    cache.delete_many([f'login-fail:user:{name}', _ips_key(name)] + [f'login-fail:pair:{name}:{ip}' for ip in ips])


def _bump(key):
    cache.add(key, 0, lockout_seconds())
    try:
        cache.incr(key)
    except ValueError:  # key expired between add and incr, or a dummy cache
        pass


@receiver(user_login_failed)
def record_failure(sender, credentials, request=None, **kwargs):
    from .account import log_sign_in
    username = credentials.get('username') or credentials.get(get_user_model().USERNAME_FIELD)
    for key in _keys(username, request):
        _bump(key)
    # Remember which addresses have counters, so unlock() can find them
    ips = set(cache.get(_ips_key(username), ())) | {_client_ip(request)}
    cache.set(_ips_key(username), ips, lockout_seconds())
    # Login history in the Audit Log. Only real sign-in attempts (they carry a request), not code calling authenticate().
    if request is None:
        return
    log_sign_in(request, username, 'sign_in_failed')
    user_key, pair_key = _keys(username, request)
    limit = fail_limit()
    # The attempt that reaches a limit is the one that locks; later refused attempts don't log it again.
    if cache.get(pair_key, 0) == limit:
        reason = f"{limit} failed attempts from this address"
    elif cache.get(user_key, 0) == limit * 4:
        reason = f"{limit * 4} failed attempts from several addresses"
    else:
        return
    log_sign_in(request, username, 'locked_out', f"{reason}; locked for {lockout_seconds() // 60} minutes")


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
            raise ValidationError(LOCKED_MESSAGE.format(minutes=lockout_seconds() // 60),
                                  code='locked')
        return super().clean()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        from .account import TEMP_PASSWORD_EXPIRED_MESSAGE, log_sign_in, temp_password_expired
        if temp_password_expired(user):
            raise ValidationError(TEMP_PASSWORD_EXPIRED_MESSAGE, code='temp_password_expired')
        # Logged here rather than on user_logged_in so tests' force_login and other code logins stay out of the history
        log_sign_in(self.request, user.get_username(), 'signed_in')
