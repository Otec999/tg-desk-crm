"""Check that a SUPERVISOR can read the SLA policy but not change it (read-only)."""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402

API = "http://127.0.0.1:8000"
results: list[tuple[bool, str, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  -> {detail}"))


async def login(client: httpx.AsyncClient, user: str, password: str) -> bool:
    for path in ("/api/admin/login", "/api/auth/admin-login", "/api/auth/login"):
        try:
            r = await client.post(path, json={"username": user, "password": password})
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            return True
    return False


async def main() -> int:
    async with httpx.AsyncClient(base_url=API, timeout=45.0) as sup:
        ok = await login(sup, "supervisor", "admin123")
        check(ok, "supervisor login")
        if not ok:
            return 1

        print("\n=== supervisor: read the SLA policy ===")
        r = await sup.get("/api/admin/settings")
        check(r.status_code == 200, "GET /api/admin/settings -> 200 (read allowed)", str(r.status_code))
        if r.status_code == 200:
            body = r.json()
            print(f"    {body}")
            check("sla_enabled" in body, "policy is visible to the supervisor")
            original = body.get("sla_target_seconds")

            print("\n=== supervisor: cannot mutate ===")
            r2 = await sup.patch("/api/admin/settings", json={"sla_target_seconds": 4321})
            check(r2.status_code == 403, "PATCH -> 403 (write denied)", str(r2.status_code))

            r3 = await sup.get("/api/admin/settings")
            check(
                r3.json().get("sla_target_seconds") == original,
                "value unchanged after the rejected PATCH",
                f"{original} -> {r3.json().get('sla_target_seconds')}",
            )

        print("\n=== admin: still fully in control ===")
        async with httpx.AsyncClient(base_url=API, timeout=45.0) as adm:
            if await login(adm, "admin", "admin123"):
                r = await adm.patch("/api/admin/settings", json={"sla_target_seconds": 1800})
                check(r.status_code == 200, "admin PATCH -> 200", str(r.status_code))
                r = await adm.patch("/api/admin/settings", json={"sla_target_seconds": 300})
                check(r.status_code == 200, "restored to 300")

    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} checks passed ===")
    failed = [label for ok, label, _ in results if not ok]
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
