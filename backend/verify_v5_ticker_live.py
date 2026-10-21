"""Verify that the live backend keeps its SLA ticker and heartbeat sweeper alive.

The supervisor logs "Task <name> returned unexpectedly" when a body returns instead of
looping. This watches ``/health`` plus the SLA broadcast stream for long enough to catch
a restart loop: a healthy ticker emits an ``SLA_UPDATE`` frame every
``backend_ws_tick_seconds`` (2 s by default).
"""

from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, r"C:\Users\Работа\Desktop\Custom CRM\backend")
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
import websockets  # noqa: E402

API = "http://127.0.0.1:8000"
WATCH_SECONDS = 14.0


async def main() -> int:
    async with httpx.AsyncClient(base_url=API, timeout=30.0) as client:
        r = await client.post("/api/auth/login", json={"username": "op1", "password": "operator123"})
        token = r.json()["access_token"]

        ws_url = f"ws://127.0.0.1:8000/ws?token={token}"
        frames: list[dict] = []
        try:
            async with websockets.connect(ws_url, open_timeout=15) as ws:
                deadline = asyncio.get_running_loop().time() + WATCH_SECONDS
                while asyncio.get_running_loop().time() < deadline:
                    remaining = deadline - asyncio.get_running_loop().time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                    except asyncio.TimeoutError:
                        break
                    try:
                        frames.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] websocket error: {type(exc).__name__}: {exc}")
            return 1

    sla_frames = [f for f in frames if f.get("type") == "SLA_UPDATE"]
    print(f"watched {WATCH_SECONDS:.0f}s: {len(frames)} frame(s), {len(sla_frames)} SLA_UPDATE")

    ok = True
    if len(sla_frames) >= 4:
        print(f"[PASS] SLA ticker is live ({len(sla_frames)} ticks in {WATCH_SECONDS:.0f}s, ~2s cadence)")
    else:
        print(f"[FAIL] SLA ticker looks stalled only {len(sla_frames)} tick(s)")
        ok = False

    # The health endpoint must stay responsive: a 2-5s restart loop starves the loop.
    async with httpx.AsyncClient(base_url=API, timeout=15.0) as client:
        for attempt in range(3):
            r = await client.get("/health")
            healthy = r.status_code == 200 and r.json().get("db") is True
            print(f"[{'PASS' if healthy else 'FAIL'}] /health attempt {attempt + 1}")
            ok = ok and healthy
            await asyncio.sleep(1.5)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
