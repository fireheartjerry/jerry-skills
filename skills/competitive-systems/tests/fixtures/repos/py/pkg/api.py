"""Public API: pages over frobnicate's output."""
from .core import frobnicate


def paginate(values, page, size=2):
    """One page (0-based) of frobnicate(values)."""
    out = frobnicate(values)
    return out[page * size:(page + 1) * size]
