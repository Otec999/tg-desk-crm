"""Unit checks for the search service primitives (escaping, snippets, spans).

Run::

    python check_search_service.py

Exits non-zero when any check fails.
"""

from __future__ import annotations

import sys

from app.services.search import (
    escape_like,
    find_spans,
    like_contains_pattern,
    make_snippet,
    normalize_query,
)

failures: list[str] = []


def check(name: str, actual: object, expected: object) -> None:
    """Compare ``actual`` with ``expected`` and record the outcome."""
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        expected: {expected!r}")
        print(f"        actual:   {actual!r}")
        failures.append(name)


def check_raises(name: str, fn, arg: str) -> None:
    """Assert that ``fn(arg)`` raises ``ValueError``."""
    try:
        fn(arg)
    except ValueError:
        print(f"[PASS] {name}")
        return
    print(f"[FAIL] {name} (no ValueError)")
    failures.append(name)


# --------------------------------------------------------------- escaping
check("escape_like('100%')", escape_like("100%"), "100\\%")
check("escape_like('a_b')", escape_like("a_b"), "a\\_b")
check("escape_like single backslash", escape_like("a\\b"), "a\\\\b")
check("escape_like backslash+percent", escape_like("\\%"), "\\\\\\%")
check("escape_like plain", escape_like("payout"), "payout")
check("escape_like empty", escape_like(""), "")
check("like_contains_pattern", like_contains_pattern("a%b"), "%a\\%b%")

# --------------------------------------------------------------- validation
check_raises("normalize_query whitespace raises", normalize_query, "   ")
check_raises("normalize_query too long raises", normalize_query, "x" * 201)
check("normalize_query trims", normalize_query("  ab  "), "ab")

# --------------------------------------------------------------- spans
check("find_spans simple", find_spans("abc abc", "abc"), [[0, 3], [4, 7]])
check("find_spans case-insensitive", find_spans("Payout", "pAY"), [[0, 3]])
check("find_spans no overlap", find_spans("aaaa", "aa"), [[0, 2], [2, 4]])
check("find_spans none", find_spans("abc", "z"), [])
check("find_spans empty needle", find_spans("abc", ""), [])

# --------------------------------------------------------------- snippet
snippet, spans = make_snippet("Payout TX-1 failed", "payout")
check("short text kept whole", snippet, "Payout TX-1 failed")
check("short text span", spans, [[0, 6]])

long_text = "x" * 200 + " NEEDLE " + "y" * 200
snippet, spans = make_snippet(long_text, "needle", width=120)
check("long text leading ellipsis", snippet.startswith("\u2026"), True)
check("long text trailing ellipsis", snippet.endswith("\u2026"), True)
check("long text shows needle", "NEEDLE" in snippet, True)
check("long text width respected", len(snippet) <= 122, True)
if spans:
    start, end = spans[0]
    check("long text span hits needle", snippet[start:end].upper(), "NEEDLE")
else:
    check("long text span hits needle", "no span", "NEEDLE")

snippet, spans = make_snippet("Hello there", "zzz")
check("no-match snippet is head", snippet, "Hello there")
check("no-match spans empty", spans, [])

snippet, spans = make_snippet("AB" + "z" * 300, "ab", width=120)
check("near-start no leading ellipsis", snippet.startswith("\u2026"), False)
if spans:
    start, end = spans[0]
    check("near-start span valid", snippet[start:end].upper(), "AB")
else:
    check("near-start span valid", "no span", "AB")

snippet, spans = make_snippet("q" * 100 + "target" + "q" * 100, "target", width=100)
if spans:
    start, end = spans[0]
    check("centered span exact", snippet[start:end], "target")
else:
    check("centered span exact", "no span", "target")

snippet, spans = make_snippet("no percent here", "%")
check("literal percent finds nothing", spans, [])

# merchant_code is part of the corpus (§6.2): it must not disturb snippet offsets.
snippet, spans = make_snippet("Merchant Alpha - Payouts", "MRC-ALPHA")
check("code miss on title yields no spans", spans, [])

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("ALL SERVICE CHECKS PASSED")
