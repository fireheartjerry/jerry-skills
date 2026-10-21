"""Core logic."""


def frobnicate(values):
    """Sorted unique values, doubled."""
    return [2 * v for v in sorted(set(values))]
