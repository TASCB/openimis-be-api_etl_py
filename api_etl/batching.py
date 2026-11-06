"""Household-aware import batches for both Survey Solutions sources."""
import json
from collections import OrderedDict


def household_batches(rows, size=2000):
    households = OrderedDict()
    for index, row in enumerate(rows):
        extra = row.get('json_ext') or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (ValueError, TypeError):
                extra = {}
        code = row.get('group_code') or (extra.get('group_code') if isinstance(extra, dict) else None)
        key = ('household', str(code)) if code is not None and code != '' else ('row', index)
        households.setdefault(key, []).append(row)
    batch = []
    for members in households.values():
        if batch and size > 1 and len(batch) + len(members) > size:
            yield batch
            batch = []
        batch.extend(members)
    if batch:
        yield batch
