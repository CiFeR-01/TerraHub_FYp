"""Short-lived cache for heavy analytics. `@cached_analytics` caches for TTL seconds until `invalidate()`; adds `fn.uncached(...)` and `fn.with_meta(...)` -> (value, computed_at)."""
import functools
import hashlib

from django.core.cache import cache
from django.utils import timezone

TTL = 60
_VERSION_KEY = "analytics:version"


def _version():
    v = cache.get(_VERSION_KEY)
    if v is None:
        v = 1
        cache.add(_VERSION_KEY, v, None)
    return v


def invalidate():
    """Drop every cached analytics result (version bump; stale keys just expire)."""
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        cache.set(_VERSION_KEY, 2, None)


def cached_analytics(fn):
    name = fn.__name__

    def _key(args, kwargs):
        digest = hashlib.md5(f"{args!r}{sorted(kwargs.items())!r}".encode()).hexdigest()
        return f"analytics:{_version()}:{name}:{digest}"

    @functools.wraps(fn)
    def with_meta(*args, **kwargs):
        key = _key(args, kwargs)
        hit = cache.get(key)
        if hit is not None:
            return hit
        result = (fn(*args, **kwargs), timezone.now())
        cache.set(key, result, TTL)
        return result

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return with_meta(*args, **kwargs)[0]

    wrapper.uncached = fn
    wrapper.with_meta = with_meta
    return wrapper
