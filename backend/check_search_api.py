"""End-to-end verification of ``GET /api/search`` against a running backend.

Run::

    python check_search_api.py

Logs in as ``op1`` / ``operator123`` on http://127.0.0.1:8000 and exercises the
contract from CONTRACTS_V5 §6.2: hit, miss, special characters, empty query,
phone substring, partial-word substring and merchant_code search.

Exits non-zero when any check fails.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx

BASE_URL = "http://127.0.0.1:8000"
USERNAME = "op1"
PASSWORD = "operator123"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record one assertion."""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        failures.append(name)


def brief(payload: Any) -> str:
    """One-line summary of a search response."""
    if not isinstance(payload, dict):
        return repr(payload)[:300]
    items = payload.get("items") or []
    first = items[0] if items else None
    head = (
        f"total={payload.get('total')} items={len(items)} "
        f"has_more={payload.get('has_more')} took_ms={payload.get('took_ms')}"
    )
    if first:
        head += (
            f" | chat={first.get('chat_id')} code={first.get('chat_merchant_code')!r}"
            f" snippet={first.get('snippet')[:55]!r} spans={first.get('match_spans')}"
        )
    return head


def main() -> int:
    """Log in, then run every §6.2 scenario and report the outcome."""
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        # ---------------------------------------------------------- login
        login = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        check("POST /api/auth/login -> 200", login.status_code == 200, f"status={login.status_code}")
        if login.status_code != 200:
            print("\nCannot continue without a token.")
            return 1
        payload = login.json()
        token = payload.get("access_token") or payload.get("token")
        check("login returned an access token", bool(token))
        client.headers["Authorization"] = f"Bearer {token}"

        # ------------------------------------------------- 1. real substring
        r = client.get("/api/search", params={"q": "payout"})
        body = r.json()
        check("GET /api/search?q=payout -> 200", r.status_code == 200, f"status={r.status_code}")
        check("q=payout finds items", len(body.get("items") or []) > 0, brief(body))
        check("q=payout total > 0", int(body.get("total") or 0) > 0)
        item = (body.get("items") or [None])[0]
        if item:
            for field in ("chat_id", "chat_title", "chat_merchant_code",
                          "message", "snippet", "match_spans", "score"):
                check(f"hit has '{field}'", field in item)
            msg = item.get("message") or {}
            for field in ("id", "chat_id", "text", "sender_type", "created_at"):
                check(f"message has '{field}'", field in msg)
            spans = item.get("match_spans") or []
            snippet = item.get("snippet") or ""
            if spans:
                s, e = spans[0]
                check("match_spans point at the needle",
                      snippet[s:e].lower() == "payout", f"snippet[{s}:{e}]={snippet[s:e]!r}")
            else:
                print("[INFO] first hit matched outside the body (phone/title/code), spans empty")

        # -------------------------------------------------------- 2. no hits
        r = client.get("/api/search", params={"q": "zzzzznotfound"})
        body = r.json()
        check("q=zzzzznotfound -> 200", r.status_code == 200, f"status={r.status_code}")
        check("q=zzzzznotfound items empty", body.get("items") == [], brief(body))
        check("q=zzzzznotfound total=0", int(body.get("total") or 0) == 0)
        check("q=zzzzznotfound has_more=false", body.get("has_more") is False)

        # ------------------------------------- 3. '%' must NOT be a wildcard
        all_live = client.get("/api/search", params={"q": "a", "limit": 1}).json().get("total") or 0
        r = client.get("/api/search", params={"q": "%"})
        pct = r.json()
        check("q=% -> not 500", r.status_code != 500, f"status={r.status_code}")
        check("q=% handled (200 or 422)", r.status_code in (200, 422), f"status={r.status_code}")
        if r.status_code == 200:
            check("q=% does not match everything",
                  int(pct.get("total") or 0) < max(all_live, 1), brief(pct))
        r_u = client.get("/api/search", params={"q": "_"})
        check("q=_ -> not 500", r_u.status_code != 500, f"status={r_u.status_code}")
        if r_u.status_code == 200:
            check("q=_ does not match everything",
                  int(r_u.json().get("total") or 0) < max(all_live, 1), brief(r_u.json()))

        # ------------------------------------------------------- 4. empty q
        for label, params in (
            ("q=''", {"q": ""}),
            ("q='   '", {"q": "   "}),
            ("q=201 chars", {"q": "x" * 201}),
            ("missing q", {}),
        ):
            r = client.get("/api/search", params=params)
            check(f"{label} -> 422", r.status_code == 422, f"status={r.status_code}")

        # -------------------------------------------- 5. phone + substrings
        r = client.get("/api/search", params={"q": "+1792560788"})
        check("q=+1792560788 -> 200", r.status_code == 200, f"status={r.status_code}")
        check("full phone finds a message", int(r.json().get("total") or 0) > 0, brief(r.json()))

        r = client.get("/api/search", params={"q": "5607"})
        check("q=5607 (phone substring) -> 200", r.status_code == 200)
        check("phone substring finds a message", int(r.json().get("total") or 0) > 0, brief(r.json()))

        r = client.get("/api/search", params={"q": "pay"})
        check("q=pay (3-char partial word) -> 200", r.status_code == 200)
        check("partial word finds messages", int(r.json().get("total") or 0) > 0, brief(r.json()))

        r = client.get("/api/search", params={"q": "AYOU"})
        check("case-insensitive substring finds messages",
              int(r.json().get("total") or 0) > 0, brief(r.json()))

        # ------------------------------------------- 6. pagination / filters
        r = client.get("/api/search", params={"q": "payout", "limit": 2})
        page = r.json()
        check("limit=2 respected", len(page.get("items") or []) <= 2, brief(page))
        check("has_more true when total > 2",
              page.get("has_more") is True or int(page.get("total") or 0) <= 2,
              f"total={page.get('total')} has_more={page.get('has_more')}")

        r = client.get("/api/search", params={"q": "payout", "sender": "operator"})
        op_body = r.json()
        check("sender=operator -> 200", r.status_code == 200)
        if op_body.get("items"):
            check("sender=operator returns only CRM replies",
                  all(i["message"]["sender_type"] == "OPERATOR" for i in op_body["items"]))

        r = client.get("/api/search", params={"q": "payout", "sender": "client"})
        cl_body = r.json()
        check("sender=client -> 200", r.status_code == 200)
        if cl_body.get("items"):
            check("sender=client excludes operator replies",
                  all(i["message"]["sender_type"] != "OPERATOR" for i in cl_body["items"]))

        r = client.get("/api/search", params={"q": "Merchant Omega"})
        check("q='Merchant Omega' (chat title) -> 200", r.status_code == 200)
        title_body = r.json()
        check("chat title is searchable", int(title_body.get("total") or 0) > 0, brief(title_body))
        if title_body.get("items"):
            check("title hit carries the merchant code",
                  title_body["items"][0].get("chat_merchant_code") == "MRC-OMEGA",
                  f"code={title_body['items'][0].get('chat_merchant_code')!r}")

        r = client.get("/api/search", params={"q": "payout", "chat_id": 7})
        chat_body = r.json()
        check("chat_id filter -> 200", r.status_code == 200, brief(chat_body))
        if chat_body.get("items"):
            check("chat_id filter stays in chat", all(i["chat_id"] == 7 for i in chat_body["items"]))

        with_del = client.get("/api/search", params={"q": "payout", "include_deleted": "true"})
        live = client.get("/api/search", params={"q": "payout", "include_deleted": "false"})
        check("include_deleted=true -> 200", with_del.status_code == 200)
        check("include_deleted widens the result set",
              int(with_del.json().get("total") or 0) >= int(live.json().get("total") or 0),
              f"with_deleted={with_del.json().get('total')} live={live.json().get('total')}")

        # ------------------------------- 9. merchant_code is searchable (§6.2)
        # Several chats may legitimately share one merchant code, so the assertion is
        # "every hit's chat really owns this code", not "all hits are chat N".
        for code in ("MRC-ALPHA", "MRC-OMEGA"):
            r = client.get("/api/search", params={"q": code, "limit": 200})
            mb = r.json()
            check(f"q={code} (merchant_code) -> 200", r.status_code == 200, f"status={r.status_code}")
            check(f"q={code} finds messages", int(mb.get("total") or 0) > 0, brief(mb))
            items = mb.get("items") or []
            if items:
                codes = sorted({i.get("chat_merchant_code") for i in items})
                check(f"q={code} returns only that merchant", codes == [code], f"codes={codes}")
                check(f"q={code} has_more consistent",
                      mb.get("has_more") == (len(items) < int(mb.get("total") or 0)),
                      f"items={len(items)} total={mb.get('total')}")

        r = client.get("/api/search", params={"q": "MRC-ALP"})
        check("q=MRC-ALP (partial merchant_code) finds messages",
              int(r.json().get("total") or 0) > 0, brief(r.json()))
        r = client.get("/api/search", params={"q": "mrc-omega"})
        check("q=mrc-omega (case-insensitive code) finds messages",
              int(r.json().get("total") or 0) > 0, brief(r.json()))

        # ------------------------------- 10. no duplicates (title + code both match)
        r = client.get("/api/search", params={"q": "MRC-OMEGA", "limit": 200})
        dup = r.json()
        ids = [i["message"]["id"] for i in dup.get("items") or []]
        check("no duplicate messages in a page", len(ids) == len(set(ids)),
              f"items={len(ids)} unique={len(set(ids))}")
        check("total equals distinct rows", int(dup.get("total") or 0) == len(set(ids)),
              f"total={dup.get('total')} distinct={len(set(ids))}")

        r = client.get("/api/search", params={"q": "omega", "limit": 200})
        both = r.json()
        both_ids = [i["message"]["id"] for i in both.get("items") or []]
        check("no duplicates when title+code both match", len(both_ids) == len(set(both_ids)),
              f"items={len(both_ids)} unique={len(set(both_ids))}")
        check("title+code overlap counted once", int(both.get("total") or 0) == len(set(both_ids)),
              f"total={both.get('total')} distinct={len(set(both_ids))}")

        r = client.get("/api/search", params={"q": "alpha", "limit": 200})
        al = r.json()
        al_ids = [i["message"]["id"] for i in al.get("items") or []]
        check("alpha total equals distinct rows", int(al.get("total") or 0) == len(set(al_ids)),
              f"total={al.get('total')} distinct={len(set(al_ids))}")

        # --------------------------------------------- 7. auth is required
        anon = httpx.get(f"{BASE_URL}/api/search", params={"q": "payout"}, timeout=15.0)
        check("unauthenticated -> 401", anon.status_code == 401, f"status={anon.status_code}")

        # ---------------------------------------- 8. date filter sanity
        r = client.get("/api/search", params={"q": "payout", "date_from": "2000-01-01T00:00:00Z"})
        check("date_from -> 200", r.status_code == 200)
        r = client.get("/api/search", params={"q": "payout", "date_from": "2099-01-01T00:00:00Z"})
        check("future date_from yields 0", int(r.json().get("total") or 0) == 0, brief(r.json()))

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("ALL API CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
