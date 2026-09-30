"""
core/analytics_cache.py - short-lived cache for the heavy analytics computations.

`@cached_analytics` wraps a pure function in core/analytics.py. Results live
for TTL seconds and are dropped early when `invalidate()` bumps the version
(core/signals.py calls it whenever data the analytics read is saved or deleted,
and the pages' Refresh button calls it too). The wrapped function keeps two extras:

    fn.uncached(*a, **kw)   always recompute (used where a decision must act on
                            fresh data, e.g. accepting a rent suggestion)
    fn.with_meta(*a, **kw)  -> (value, computed_at), for the "as of" stamp

The cache stores a pickled copy, so callers can freely mutate the rows they get.
"""
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
