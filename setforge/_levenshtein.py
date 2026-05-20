"""Wagner-Fischer Levenshtein distance for validate close-match suggestions.

Vendored as a tiny utility to avoid a heavyweight fuzzy-matching dep for
a single use case. See :func:`setforge.cli._validate_errors.suggest_close_match`.
"""


def levenshtein(a: str, b: str) -> int:
    """Return the Levenshtein edit distance between ``a`` and ``b``.

    Iterative Wagner-Fischer DP with ``O(min(|a|, |b|))`` space — the
    two-row rolling-buffer optimization. Operates on Unicode code
    points (Python ``str`` iteration).
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Ensure ``a`` is the shorter side so the rolling buffer is the
    # shorter dimension.
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j, cb in enumerate(b, 1):
        curr = [j] + [0] * len(a)
        for i, ca in enumerate(a, 1):
            cost = 0 if ca == cb else 1
            curr[i] = min(
                curr[i - 1] + 1,  # insertion
                prev[i] + 1,  # deletion
                prev[i - 1] + cost,  # substitution
            )
        prev = curr
    return prev[-1]
