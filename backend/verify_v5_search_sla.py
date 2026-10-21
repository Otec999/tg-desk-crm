"""End-to-end check of CONTRACTS_V5 В§2 (admin SLA settings) and В§6 (global search)."""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, r"C:\Users\Р Р°Р±РѕС‚Р°\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402

API = "http://127.0.0.1:8000"
results: list[tuple[bool, str, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


async def op_client() -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=API, timeout=60.0)
    r = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
    client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return client


async def admin_client() -> httpx.AsyncClient | None:
    client = httpx.AsyncClient(base_url=API, timeout=60.0)
    for path in ("/api/admin/login", "/api/auth/admin-login"):
        try:
            r = await client.post(path, json={"username": "admin", "password": "admin123"})
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            return client
    await client.aclose()
    return None


async def main() -> int:
    op = await op_client()

    print("=== search (В§6) ===")
    r = await op.get("/api/search", params={"q": "payout"})
    check(r.status_code == 200, "q=payout -> 200", r.text[:160])
    body = r.json()
    check(body.get("total", 0) > 0, "substring finds messages", str(body.get("total")))
    items = body.get("items", [])
    check(bool(items), "items returned")
    if items:
        hit = items[0]
        spans = hit.get("match_spans") or []
        snippet = hit.get("snippet") or ""
        if spans:
            start, end = spans[0]
            check(
                snippet[start:end].lower() == "payout",
                "match_spans point at the needle inside the snippet",
                f"{snippet[start:end]!r}",
            )
        else:
            check(False, "match_spans present")

    r = await op.get("/api/search", params={"q": "zzzznothing"})
    check(r.status_code == 200 and r.json()["total"] == 0, "no-match query -> 0 total")

    r = await op.get("/api/search", params={"q": "payout%"})
    check(r.status_code == 200 and r.json()["total"] == 0, "LIKE wildcard is escaped (payout% finds nothing)")

    r = await op.get("/api/search", params={"q": "MRC-ALPHA"})
    found_code = r.status_code == 200 and r.json()["total"] > 0
    check(found_code, "search by merchant_code (MRC-ALPHA)", f"total={r.json().get('total') if r.status_code==200 else r.status_code}")

    r = await op.get("/api/search", params={"q": ""})
    check(r.status_code == 422, "empty q -> 422", str(r.status_code))

    anon = httpx.AsyncClient(base_url=API, timeout=30.0)
    r = await anon.get("/api/search", params={"q": "payout"})
    check(r.status_code == 401, "search without token -> 401", str(r.status_code))
    await anon.aclose()

    print("=== admin SLA settings (В§2) ===")
    r = await op.get("/api/admin/settings")
    check(r.status_code in (401, 403), "operator blocked from settings", str(r.status_code))

    admin = await admin_client()
    check(admin is not None, "admin login")
    if admin is not None:
        r = await admin.get("/api/admin/settings")
        check(r.status_code == 200, "GET /api/admin/settings -> 200", r.text[:200])
        original = r.json() if r.status_code == 200 else {}
        print(f"    current: {original}")

        r = await admin.patch("/api/admin/settings", json={"sla_target_seconds": 1234})
        check(r.status_code == 200, "PATCH sla_target_seconds -> 200", r.text[:200])
        check(r.json().get("sla_target_seconds") == 1234, "value persisted in the response")

        r = await admin.get("/api/admin/settings")
        check(r.json().get("sla_target_seconds") == 1234, "value re-read from the DB")

        r = await admin.patch("/api/admin/settings", json={"sla_target_seconds": 5})
        check(r.status_code == 422, "out-of-range target -> 422", str(r.status_code))

        r = await admin.patch("/api/admin/settings", json={"sla_enabled": False})
        check(r.json().get("sla_enabled") is False, "sla can be disabled")
        r = await admin.patch("/api/admin/settings", json={"sla_enabled": True})
        check(r.json().get("sla_enabled") is True, "sla can be re-enabled")

        # restore
        restore = {k: original[k] for k in ("sla_enabled", "sla_target_seconds", "sla_reset_on_operator_reply") if k in original}
        if restore:
            r = await admin.patch("/api/admin/settings", json=restore)
            check(r.status_code == 200, "original settings restored")
        await admin.aclose()

    await op.aclose()
    print(f"\n=== {sum(1 for ok, _, _ in results if ok)}/{len(results)} checks passed ===")
    failed = [label for ok, label, _ in results if not ok]
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

