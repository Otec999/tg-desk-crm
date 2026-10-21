"""Live API verification for the editable quick replies (CONTRACTS_V5 §5.2).

Run against a booted backend::

    python verify_quick_replies_live.py [base_url]

The script exercises the whole contract against a real HTTP server and a real
database — no mocks — and prints one ``PASS``/``FAIL`` line per assertion:

1. ``POST /api/auth/login`` as ``op1`` and as ``admin``;
2. ``GET  /api/quick-replies`` — the seeded catalogue is returned, ordered by
   ``sort_order``, and every item matches the ``QuickReplyOut`` shape;
3. ``GET  /api/quick-replies?all=true`` — refused (``403``) for a plain operator;
4. ``POST /api/quick-replies`` — ``201`` with the stored row;
5. ``PATCH /api/quick-replies/{id}`` — ``200`` and the body really changed;
6. ``POST`` with the same shortcut — ``409``;
7. ``PATCH`` with a duplicate shortcut — ``409``;
8. ``POST`` with a blank body — ``422``; with an unsafe shortcut — ``422``;
9. plain operator may not mutate — ``403``;
10. ``DELETE`` — ``204``; a repeated ``DELETE`` — ``404``; ``PATCH`` a deleted
    reply — ``404``.

Everything created here is removed again, so the script is safe to re-run and
leaves the seeded catalogue untouched. Exit code 0 means every assertion passed.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any, Final

DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8000"
ADMIN_USERNAME: Final[str] = "admin"
ADMIN_PASSWORD: Final[str] = "admin123"
OPERATOR_USERNAME: Final[str] = "op1"
OPERATOR_PASSWORD: Final[str] = "operator123"
TIMEOUT_SECONDS: Final[int] = 20

results: list[tuple[bool, str]] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    """Record one assertion and print its outcome."""
    results.append((bool(condition), label))
    mark = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"[{mark}] {label}{suffix}")
    return bool(condition)


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    """Perform one HTTP call and return ``(status_code, decoded_body)``.

    Never raises for a 4xx/5xx: the status is part of what the contract checks.
    """
    url = f"{base_url.rstrip('/')}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_obj = urllib.request.Request(url=url, data=data, method=method)
    request_obj.add_header("Accept", "application/json")
    if data is not None:
        request_obj.add_header("Content-Type", "application/json")
    if token:
        request_obj.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request_obj, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
            status_code = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status_code = int(exc.code)
    except urllib.error.URLError as exc:
        print(f"FATAL: cannot reach {url}: {exc.reason}")
        raise SystemExit(2) from exc
    if not raw:
        return status_code, None
    try:
        return status_code, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status_code, raw.decode("utf-8", "replace")


def login(base_url: str, username: str, password: str) -> str | None:
    """Authenticate through the desk form and return the bearer token."""
    status_code, body = request(
        base_url,
        "POST",
        "/api/auth/login",
        payload={"username": username, "password": password},
    )
    if status_code != 200 or not isinstance(body, dict):
        print(f"[FAIL] login {username}: HTTP {status_code} {body}")
        results.append((False, f"login {username}"))
        return None
    token = body.get("access_token") or body.get("token")
    if not token:
        print(f"[FAIL] login {username}: no access_token in {body}")
        results.append((False, f"login {username}"))
        return None
    print(f"[PASS] login {username}")
    results.append((True, f"login {username}"))
    return str(token)


REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "shortcut",
    "title",
    "body",
    "category",
    "sort_order",
    "is_active",
    "created_by",
    "created_at",
    "updated_at",
)


def main(argv: list[str]) -> int:
    """Run every assertion and return the process exit code."""
    base_url = argv[1] if len(argv) > 1 else DEFAULT_BASE_URL
    print(f"=== verifying {base_url} ===\n")

    admin_token = login(base_url, ADMIN_USERNAME, ADMIN_PASSWORD)
    operator_token = login(base_url, OPERATOR_USERNAME, OPERATOR_PASSWORD)
    if not admin_token or not operator_token:
        print("\nFATAL: could not authenticate")
        return 2

    # --- 1. the seeded catalogue -------------------------------------------
    status_code, body = request(base_url, "GET", "/api/quick-replies", token=operator_token)
    check(status_code == 200, "GET /api/quick-replies -> 200", f"got {status_code}")
    items = (body or {}).get("items") if isinstance(body, dict) else None
    check(isinstance(items, list), "response carries an items list")
    items = items or []
    check(len(items) >= 4, "seeded catalogue has at least 4 replies", f"got {len(items)}")

    if items:
        missing = [key for key in REQUIRED_FIELDS if key not in items[0]]
        check(not missing, "items match the QuickReplyOut shape", f"missing {missing}")
        order = [(int(item["sort_order"]), int(item["id"])) for item in items]
        check(order == sorted(order), "items sorted by (sort_order, id)")
        shortcuts = {str(item["shortcut"]) for item in items}
        expected = {"thanks", "txid", "escalated", "resolved"}
        check(
            expected <= shortcuts,
            "seed produced thanks/txid/escalated/resolved",
            f"got {sorted(shortcuts)}",
        )
        check(
            all(isinstance(item["created_by"], str) or item["created_by"] is None for item in items),
            "created_by is a name string (or null), never a bare id",
        )

    # --- 2. ?all=true is administrator-only --------------------------------
    status_code, _ = request(
        base_url, "GET", "/api/quick-replies?all=true", token=operator_token
    )
    check(status_code == 403, "GET ?all=true as operator -> 403", f"got {status_code}")
    status_code, body = request(base_url, "GET", "/api/quick-replies?all=true", token=admin_token)
    check(status_code == 200, "GET ?all=true as admin -> 200", f"got {status_code}")

    # --- 3. a plain operator cannot mutate ---------------------------------
    status_code, _ = request(
        base_url,
        "POST",
        "/api/quick-replies",
        token=operator_token,
        payload={"shortcut": "nope", "title": "Nope", "body": "nope"},
    )
    check(status_code == 403, "POST as plain operator -> 403", f"got {status_code}")

    # --- 4. create --------------------------------------------------------
    marker = uuid.uuid4().hex[:8]
    shortcut = f"verify-{marker}"
    original_body = f"Original body created by verify_quick_replies_live ({marker})."
    status_code, created = request(
        base_url,
        "POST",
        "/api/quick-replies",
        token=admin_token,
        payload={
            "shortcut": shortcut,
            "title": "Проверка API",
            "body": original_body,
            "category": "qa",
            "sort_order": 999,
        },
    )
    ok = check(status_code == 201, "POST new reply -> 201", f"got {status_code} {created}")
    created = created if isinstance(created, dict) else {}
    reply_id = created.get("id")
    check(isinstance(reply_id, int) and reply_id > 0, "created reply has an id", f"got {reply_id}")
    check(created.get("shortcut") == shortcut, "shortcut stored verbatim", f"got {created.get('shortcut')}")
    check(created.get("body") == original_body, "body stored verbatim")
    check(isinstance(created.get("created_by"), str), "created_by resolves to a name", f"got {created.get('created_by')}")
    check(created.get("is_active") is True, "is_active defaults to true")
    check(created.get("category") == "qa", "category stored", f"got {created.get('category')}")
    if not (ok and isinstance(reply_id, int)):
        print("\nFATAL: cannot continue without a created reply")
        return _summary()

    # --- 5. patch changes the body ----------------------------------------
    updated_body = f"UPDATED body — rewritten by PATCH ({marker})."
    status_code, patched = request(
        base_url,
        "PATCH",
        f"/api/quick-replies/{reply_id}",
        token=admin_token,
        payload={"body": updated_body},
    )
    check(status_code == 200, "PATCH reply -> 200", f"got {status_code} {patched}")
    patched = patched if isinstance(patched, dict) else {}
    check(patched.get("body") == updated_body, "PATCH changed the body", f"got {patched.get('body')!r}")
    check(patched.get("title") == "Проверка API", "PATCH left untouched fields alone")
    check(patched.get("updated_at") is not None, "PATCH refreshed updated_at")

    # re-read through the list endpoint
    status_code, body = request(base_url, "GET", "/api/quick-replies", token=operator_token)
    listed = next(
        (item for item in ((body or {}).get("items") or []) if int(item.get("id", -1)) == reply_id),
        None,
    )
    check(listed is not None and listed.get("body") == updated_body, "GET reflects the new body")

    # --- 6. duplicate shortcut on create ----------------------------------
    status_code, body = request(
        base_url,
        "POST",
        "/api/quick-replies",
        token=admin_token,
        payload={"shortcut": shortcut, "title": "Duplicate", "body": "duplicate shortcut"},
    )
    check(status_code == 409, "POST duplicate shortcut -> 409", f"got {status_code} {body}")

    # --- 7. duplicate shortcut on patch -----------------------------------
    status_code, body = request(
        base_url,
        "POST",
        "/api/quick-replies",
        token=admin_token,
        payload={"shortcut": f"{shortcut}-b", "title": "Второй", "body": "second reply"},
    )
    second_id = (body or {}).get("id") if isinstance(body, dict) else None
    check(status_code == 201, "POST a second reply -> 201", f"got {status_code}")
    if isinstance(second_id, int):
        status_code, body = request(
            base_url,
            "PATCH",
            f"/api/quick-replies/{second_id}",
            token=admin_token,
            payload={"shortcut": shortcut},
        )
        check(status_code == 409, "PATCH onto a taken shortcut -> 409", f"got {status_code} {body}")
        status_code, _ = request(
            base_url, "DELETE", f"/api/quick-replies/{second_id}", token=admin_token
        )
        check(status_code == 204, "cleanup: DELETE second reply -> 204", f"got {status_code}")

    # --- 8. validation ----------------------------------------------------
    status_code, body = request(
        base_url,
        "POST",
        "/api/quick-replies",
        token=admin_token,
        payload={"shortcut": "blank-body", "title": "Blank", "body": "   "},
    )
    check(status_code == 422, "POST blank body -> 422", f"got {status_code} {body}")
    status_code, body = request(
        base_url,
        "POST",
        "/api/quick-replies",
        token=admin_token,
        payload={"shortcut": "bad shortcut!", "title": "Bad", "body": "text"},
    )
    check(status_code == 422, "POST unsafe shortcut -> 422", f"got {status_code} {body}")
    status_code, body = request(
        base_url,
        "POST",
        "/api/quick-replies",
        token=admin_token,
        payload={"shortcut": "x" * 33, "title": "Long", "body": "text"},
    )
    check(status_code == 422, "POST 33-char shortcut -> 422", f"got {status_code} {body}")

    # --- 9. delete, then a repeated delete --------------------------------
    status_code, body = request(
        base_url, "DELETE", f"/api/quick-replies/{reply_id}", token=admin_token
    )
    check(status_code == 204, "DELETE reply -> 204", f"got {status_code} {body}")
    status_code, body = request(
        base_url, "DELETE", f"/api/quick-replies/{reply_id}", token=admin_token
    )
    check(status_code == 404, "repeated DELETE -> 404", f"got {status_code} {body}")
    status_code, body = request(
        base_url,
        "PATCH",
        f"/api/quick-replies/{reply_id}",
        token=admin_token,
        payload={"title": "Ghost"},
    )
    check(status_code == 404, "PATCH a deleted reply -> 404", f"got {status_code} {body}")
    status_code, _ = request(base_url, "GET", "/api/quick-replies", token=None)
    check(status_code == 401, "GET without a token -> 401", f"got {status_code}")

    return _summary()


def _summary() -> int:
    """Print the tally and return the exit code."""
    passed = sum(1 for ok, _ in results if ok)
    failed = [(ok, label) for ok, label in results if not ok]
    print(f"\n=== {passed}/{len(results)} checks passed ===")
    for _, label in failed:
        print(f"  FAILED: {label}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
