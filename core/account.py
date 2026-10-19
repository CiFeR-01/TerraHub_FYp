"""Admin-issued temporary passwords and the forced password change that follows.

An admin resets a user's password from the Users page: the app makes a short random password, shows it to the
admin once, and the user must choose their own password the first time they sign in with it. A temporary
password nobody has used within TEMP_PASSWORD_HOURS is refused at sign-in.
"""
import secrets
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

# Lowercase letters and digits without look-alikes (0/o, 1/l/i) so it is easy to read out and type.
TEMP_PASSWORD_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'
TEMP_PASSWORD_LENGTH = 9
TEMP_PASSWORD_HOURS = 24
TEMP_PASSWORD_EXPIRED_MESSAGE = ('This temporary password has expired. '
                                 'Ask an admin to reset your password again.')


def make_temp_password():
    return ''.join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(TEMP_PASSWORD_LENGTH))


def issue_temp_password(user, by):
    """Give `user` a new temporary password and return it. Changing the password also signs them out everywhere."""
    password = make_temp_password()
    user.set_password(password)
    user.must_change_password = True
    user.temp_password_set_at = timezone.now()
    user.updated_by = by
    user.save()
    return password


def log_user_change(request, target, action, details=''):
    """Record an account change in UserAuditLog, with the signed-in user as the actor."""
    from .models import UserAuditLog

    actor = request.user if request.user.is_authenticated else None
    UserAuditLog.objects.create(target=target, target_username=target.username, actor=actor,
                                action=action, details=details, ip_address=_valid_ip(request))


def _valid_ip(request):
    import ipaddress
    from .login_throttle import _client_ip

    ip = _client_ip(request)
    try:
        ipaddress.ip_address(ip)
    except ValueError:  # 'unknown' or a garbled forwarded header
        return None
    return ip


def log_setting_change(request, key, details):
    """Record a System Settings change in the same audit log (no target user)."""
    from .models import UserAuditLog
    UserAuditLog.objects.create(target=None, target_username='', actor=request.user, action='setting_changed',
                                details=f"{key}: {details}", ip_address=_valid_ip(request))


def sign_out_everywhere(user, keep_session_key=None):
    """End every signed-in session of `user` except `keep_session_key`. Returns how many were ended."""
    from django.contrib.sessions.models import Session
    ended = 0
    for session in Session.objects.filter(expire_date__gt=timezone.now()).exclude(session_key=keep_session_key):
        if session.get_decoded().get('_auth_user_id') == str(user.pk):
            session.delete()
            ended += 1
    return ended


def idle_timeout_seconds(request=None):
    """Idle sign-out time: the session_idle_timeout_minutes setting, never above the SESSION_IDLE_TIMEOUT cap.
    Read once per request (IdleTimeoutMiddleware keeps it on the request)."""
    cached = getattr(request, '_idle_timeout_seconds', None)
    if cached is not None:
        return cached
    from .settings_store import get_setting
    seconds = max(60, min(get_setting('session_idle_timeout_minutes') * 60, settings.SESSION_IDLE_TIMEOUT))
    if request is not None:
        request._idle_timeout_seconds = seconds
    return seconds


def temp_password_expired(user):
    return (user.must_change_password and user.temp_password_set_at is not None
            and timezone.now() - user.temp_password_set_at > timedelta(hours=TEMP_PASSWORD_HOURS))


class ForcePasswordChangeMiddleware:
    """Sends a user who signed in with a temporary password to the change-password page until they set their own."""
    ALLOWED_URL_NAMES = ('password_change', 'logout', 'session_keepalive')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and getattr(user, 'must_change_password', False):
            allowed = {reverse(name) for name in self.ALLOWED_URL_NAMES}
            if request.path not in allowed and not request.path.startswith('/' + settings.STATIC_URL.lstrip('/')):
                return redirect('password_change')
        return self.get_response(request)


class IdleTimeoutMiddleware:
    """Signs a user out after idle_timeout_seconds() without a request. The session cookie's own age
    (SESSION_COOKIE_AGE) is the upper bound; this lets the System Settings page shorten it without a deploy."""
    SESSION_KEY = '_last_seen'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            now = int(time.time())
            last_seen = request.session.get(self.SESSION_KEY)
            if last_seen is not None and now - last_seen > idle_timeout_seconds(request):
                logout(request)
                if request.path != reverse('session_keepalive'):
                    return redirect(f"{reverse('login')}?reason=idle")
            else:
                request.session[self.SESSION_KEY] = now
        return self.get_response(request)
