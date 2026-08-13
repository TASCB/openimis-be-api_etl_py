"""Per-run cache for location code -> name lookups. Misses are cached too."""

import logging

LOG = logging.getLogger(__name__)

_LOCATION_NAME_CACHE = {}


def clear_location_cache():
    _LOCATION_NAME_CACHE.clear()


def location_name_for_code(code):
    """Return the location name for `code`, or None. Caches hits and misses."""
    if not code:
        return None

    code = str(code)
    if code in _LOCATION_NAME_CACHE:
        return _LOCATION_NAME_CACHE[code]

    name = None
    try:
        from location.models import Location

        loc = Location.objects.filter(code=code).only("id", "name").first()
        if loc:
            name = loc.name
    except Exception:
        LOG.debug("Location lookup failed for code %s", code, exc_info=True)
        return None

    _LOCATION_NAME_CACHE[code] = name
    return name
