# TG-Desk CRM — Backend

FastAPI service that powers the Telegram support desk: live group chats, an SLA
engine for unanswered merchant questions, a dual-admin-panel transaction lookup
and operator shift management.

The **frozen interface contract** (endpoints, WebSocket events, DB schema, seed
requirements) lives in [`../CONTRACTS.md`](../CONTRACTS.md). This document covers
how the code behind that contract is organised and how to run it.

---

## 1. Architecture

```
backend/
├── seed.py                 # idempotent demo-data seeder (CLI)
├── requirements.txt
└── app/
    ├── main.py             # FastAPI app, lifespan, /health, /ws, router mounting
    ├── config.py           # pydantic-settings → `settings` (env vars, §5)
    ├── database.py         # async engine, SessionLocal, get_db, init_models()
    ├── models.py           # SQLAlchemy 2.0 declarative models (§6)
    ├── enums.py            # shift/SLA/message/tx enums shared by models + schemas
    ├── schemas.py          # pydantic request/response models (§3)
    ├── security.py         # bcrypt hashing, JWT issue/verify, auth dependencies
    ├── utils.py            # UTC helpers, phone/TX extraction, mock RNG
    ├── ws_manager.py       # WebSocket connection registry + event envelope
    ├── redis_bus.py        # fan-out of WS events across processes
    ├── background.py       # SLA deadline sweeper + heartbeat tick
    ├── routers/
    │   ├── auth.py         # /auth/login|me|logout  (+ in-memory login limiter)
    │   ├── chats.py        # /chats list/detail, read, assign, typing
    │   ├── messages.py     # POST /chats/{id}/messages (collision lock)
    │   ├── sla.py          # /sla/stats/me, /sla/incidents
    │   ├── tx.py           # /tx/lookup, /tx/lookup/{id}, /tx/audit, /tx/suggest
    │   ├── shift.py        # /shift/status, /shift/operators, /shift/heartbeat
    │   ├── internal.py     # /internal/broadcast, /system/info
    │   ├── _common.py      # ORM → schema serialisers, safe_broadcast
    │   └── _provisioning.py# supervisor bootstrap / password sync
    └── services/
        └── admin_adapters.py  # ADMIN_ONE / ADMIN_TWO adapters (§3.5)
```

**Layering.** Routers hold *only* HTTP concerns: authentication, request
validation, status codes. They read/write through SQLAlchemy sessions obtained
from `get_db` and convert rows with helpers from `routers/_common.py`. Anything
that talks to the outside world (the two admin panels, the currency/status
vocabulary of transactions) lives in `app/services/`. Cross-cutting concerns
(WS fan-out, SLA state transitions) are single-purpose modules so no router has
to duplicate them.

**Everything is async.** The engine is `postgresql+asyncpg`, sessions are
`AsyncSession`, and every route handler that touches the DB is `async def`.

---

## 2. SLA engine

An **SLA incident** is one unanswered merchant message that must receive an
operator reply within the configured target (`BACKEND_SLA_SECONDS`, default
`300`).

### States

| Status | Meaning |
| --- | --- |
| `PENDING` | Message arrived, no operator reply yet. The countdown is running. |
| `RESOLVED` | An operator replied inside (or after) the target. `resolved_at`, `resolved_by_message_id` and `response_time_seconds` are set. |

### Deadlines

`deadline_at = created_at + sla_target_seconds`, both taken from the *trigger
message* time and the operator's `sla_target_seconds` (per-operator override,
seeded at `300`). `response_time_seconds` is frozen at resolution so historical
statistics never drift.

### What opens an incident

Only inbound merchant traffic — `CLIENT` and `BOT` messages — can open one.
Operator messages never do. A message that opens an incident cannot quote an
earlier message: the clock must start on a clean turn.

### Round-robin assignment

Unassigned chats are handed to the next eligible operator in rotation. Only
operators whose `shift_status` is `ONLINE` are candidates; `PAUSE` and
`OFFLINE` operators are skipped. If no operator is online the incident stays
`UNASSIGNED` and keeps counting down (this is the deliberately alarming
`live-gamma` seed case with a negative remaining time).

### Resolution

Exactly one reply resolves an incident: the operator message posted with
`reply_to_id` / `reply_to_tg_msg_id` pointing at the trigger, or the reply that
is explicitly marked as the resolver. Resolution is idempotent — posting a
second reply does not create a second incident and does not overwrite the first
measurement.

### Deadline sweeper

`app/background.py` runs a light tick (`BACKEND_WS_TICK_SECONDS`, default `2s`)
that broadcasts `SLA_UPDATE` for incidents approaching or past their deadline,
so dashboards count down live without polling.

---

## 3. Collision lock (typing)

Two operators must not answer the same merchant message. `POST
/chats/{chat_id}/typing` claims a short-lived, chat-scoped lock tied to the
operator and the message being answered. While the lock is held, other
operators see `TYPING_LOCK` and the send endpoint rejects their draft with a
conflict instead of delivering a duplicate answer. The lock is released by an
explicit unlock, by sending the message, or by expiry, and is always announced
over WebSocket (`TYPING_LOCK` / `TYPING_UNLOCK`).

---

## 4. Transaction lookup — the dual admin adapter

`ADMIN_ONE` and `ADMIN_TWO` are two *different* external admin panels that may
disagree about the same transaction. The lookup endpoint asks both and returns
the union in a single payload, so the frontend can show a side-by-side
comparison (`ADMIN_ONE` vs `ADMIN_TWO`) rather than a single "truth".

Both panels are served by **one adapter class each** (`AdminOneAdapter`,
`AdminTwoAdapter`) built on a shared `BaseAdminAdapter` ABC. Each instance
decides at construction time whether it is mocking or live:

```python
self.use_mock = not self.base_url.startswith(("http://", "https://")) or (
    "example" in self.base_url.lower()
)
```

So the mock is used while `ADMIN_ONE_BASE_URL` is unset or still points at an
`*.example` placeholder; the moment a real `http(s)` URL is configured, the same
adapter performs the upstream call instead. `get_adapter(panel)` returns the
singleton per panel, and `lookup_both(tx_id)` fans out to both.

The mock path serves a curated dataset (`MockAdminMixin.CURATED_DATASET`) that is
the single source of truth for the seeded demo and deliberately encodes
real-world disagreement:

| TX id | ADMIN_ONE | ADMIN_TWO |
| --- | --- | --- |
| `TX-4412-DD` | `FAILED`, 1500.00 USDT | `DECLINED` |
| `TX-7781-AC` | `COMPLETED`, 240.50 EUR | `PROCESSING` |
| `TX-1102-BE` | `PROCESSING`, 89.99 USD | `PENDING` |
| `TX-9931-ZQ` | `REFUNDED`, 3120.00 USDT | `REFUNDED` |
| `TX-5520-KM` | `DECLINED`, 640.00 GBP | `FAILED` |
| `TX-0000-XX` | not found | not found |

Ids outside the curated set still answer deterministically (seeded by
`mock_rng(tx_id, salt=...)`), and anything starting with `TX-0000` resolves to a
`NOT_FOUND` shape rather than an error. A panel that fails degrades to a
`status="ERROR"` result so one dead upstream never takes the lookup down.

### Swapping a mock for the real panel

1. Point `ADMIN_ONE_BASE_URL` (and `ADMIN_TWO_BASE_URL`) at the real endpoint and
   set the matching `*_API_KEY`. `use_mock` flips to `False` automatically — no
   code change and no redeploy of routing logic.
2. Fill in the `PRODUCTION SWAP` block in `AdminOneAdapter.fetch` /
   `AdminTwoAdapter.fetch` with the upstream call. The intended shape is
   `GET {base_url}/transactions/{tx_id}` with a bearer key, mapped onto the same
   `TxResult` fields (`status`, `amount`, `currency`, `provider`,
   `error_reason`, `external_ref`, `raw`). `httpx` is the expected client and
   `timeout_s` is already defined per adapter.
3. Normalise the provider's status vocabulary onto `TxStatus`; unknown values
   should map to `NOT_FOUND`/`UNKNOWN` rather than raising, so one odd payload
   never 500s the endpoint.

Because the response shape is defined by `TxResult` and not by the transport,
`/tx/lookup` keeps its contract and the frontend is untouched.

Every lookup — mock or real, successful or not — is written to
`tx_lookup_audit` (who asked, which tx, from which panel, what was returned), so
the audit trail is identical in both modes.

---

## 5. Database

**PostgreSQL only.** The models use `GENERATED BY DEFAULT AS IDENTITY` primary
keys, `UNIQUE` constraints and `CHECK` constraints that are meaningful on
PostgreSQL (text length `<= 4096`, enum-value whitelists). Production and
development both target PostgreSQL via `asyncpg` (`POSTGRES_*` → `DATABASE_URL`).

SQLite (`aiosqlite`) is supported **for tests only**: `seed.py` applies a small
compatibility shim (`_apply_sqlite_compat`) that drops `char_length`-based
`CHECK`s and widens/coerces primary keys, because SQLite has no `char_length()`
and would otherwise lose the implicit rowid alias. Do not ship SQLite as the
application database.

Tables: `operators`, `group_chats`, `group_messages`, `sla_incidents`,
`tx_lookup_audit` — see `CONTRACTS.md` §6 for the full column list.

Verified on PostgreSQL 16.15: all timestamps are `timestamp with time zone`,
enum-ish columns are `varchar(24)` guarded by `CHECK … = ANY(ARRAY[...])`, every
`CHECK`/`UNIQUE`/FK (10 FKs, including the `group_messages ↔ sla_incidents`
cycle) is present, and the seeded data round-trips through the API without a
single invariant violation.

---

## 6. Configuration

Settings are read from the environment (or `backend/.env`) by
`app/config.py`; names are case-insensitive.

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `dev` | `dev` / `staging` / `production`. `production` enables real admin adapters and blocks `seed.py --reset` without `--force`. |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host. |
| `POSTGRES_PORT` | `5433` | PostgreSQL port. |
| `POSTGRES_USER` | `tgcrm` | Database user. |
| `POSTGRES_PASSWORD` | `tgcrm_pass` | Database password. |
| `POSTGRES_DB` | `tgcrm` | Database name. |
| `DATABASE_URL` | *(derived)* | Full SQLAlchemy URL; overrides the `POSTGRES_*` parts. |
| `REDIS_URL` | `redis://localhost:6380/0` | Redis for cross-process WS fan-out. |
| `BACKEND_SECRET_KEY` | *(dev placeholder)* | JWT signing key — **must** be changed in production. |
| `BACKEND_ACCESS_TOKEN_EXPIRE_MINUTES` | `720` | JWT lifetime. |
| `BACKEND_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated allowed origins. |
| `BACKEND_SLA_SECONDS` | `300` | Default SLA target per operator. |
| `BACKEND_WS_HEARTBEAT_TIMEOUT_SECONDS` | `45` | Drop a socket after this much silence. |
| `BACKEND_WS_TICK_SECONDS` | `2` | SLA sweeper tick. |
| `BACKEND_SUPERVISOR_USERNAME` | `admin` | Bootstrap supervisor login. |
| `BACKEND_SUPERVISOR_PASSWORD` | `admin123` | Bootstrap supervisor password. |
| `BACKEND_SEED_OPERATOR_PASSWORD` | `operator123` | Password for seeded operators. |
| `ADMIN_ONE_BASE_URL` / `ADMIN_ONE_API_KEY` | mock values | Admin panel #1 endpoint + key. |
| `ADMIN_TWO_BASE_URL` / `ADMIN_TWO_API_KEY` | mock values | Admin panel #2 endpoint + key. |
| `TG_API_ID` / `TG_API_HASH` | placeholder | Telegram MTProto app credentials. |
| `TG_SESSION_STRING` / `TG_STRING_SESSION` | *(empty)* | Telethon session (worker only). |
| `TG_SESSION_FILE` | `./telethon_worker/tg_desk.session` | Session file fallback. |
| `TG_MONITORED_CHAT_IDS` | *(empty)* | Comma-separated `tg_chat_id`s to ingest. |
| `TELEGRAM_CHAT_ID` / `TELEGRAM_ADMIN_CHAT_ID(S)` | *(empty)* | Alert destinations. |

---

## 7. Seeding

`seed.py` fills the database with a coherent demo workspace. It is **idempotent**:
operators are upserted by `username`, chats by `tg_chat_id`, incidents by
`(chat_id, message_id)` and audits by their natural key, so running it twice
creates nothing new and produces identical data.

```bash
cd backend
python seed.py                 # upsert demo data (history on)
python -m seed                 # same thing
```

| Flag | Effect |
| --- | --- |
| `--reset` | Drop and recreate every table before seeding. Refused when `APP_ENV` is `production`. |
| `--force` | Allow `--reset` in production. |
| `--operators N` | Number of `OPERATOR` rows to create (default `10`, range `1..999`). The supervisor is created in addition, so `--operators 25` yields 26 accounts. |
| `--with-history` / `--no-history` | Generate chat transcripts + incidents (default: **on**). `--no-history` seeds only operators, chats and the audit log. |

What you get with defaults:

* **11 operators** — `op1` … `op10` (`Operator One` … `Operator Ten`, role
  `OPERATOR`) plus supervisor `admin` (role `SUPERVISOR`). Shift mix: `op1`–`op4`
  `ONLINE`, `op5`–`op6` `PAUSE`, `op7`–`op10` `OFFLINE`; `is_online_ws` is `True`
  exactly for the `ONLINE` ones. Everyone has `sla_target_seconds = 300`.
* **6 chats** — `MRC-ALPHA` … `MRC-ZETA`, `tg_chat_id`s `-1001234567890` …
  `-1001234567895`, four of them pre-assigned (`ALPHA`→`op1`, `BETA`→`op2`,
  `GAMMA`→`op3`, `DELTA`→`op5`), realistic `unread_count`s.
* **91 messages** — 14–16 per chat, spread over the last 48 hours and 3–25
  minutes apart, with client turns, automated bot alerts quoting a bare
  `TX-XXXX-XX`, operator replies (some threading via `reply_to_id`) and exactly
  one soft-deleted message per chat. The canonical transactions
  `TX-4412-DD` (FAILED), `TX-7781-AC` (COMPLETED), `TX-1102-BE` (PROCESSING),
  `TX-9931-ZQ` (REFUNDED) and `TX-0000-XX` (NOT FOUND) all appear.
* **23 SLA incidents** — 20 resolved historical ones (18 within target → a
  90.0% hit rate) plus 3 live ones: `op1` with ~240 s left, `op2` with ~40 s
  left, and an **unassigned, already-overdue** incident that demonstrates the
  alarm state.
* **6 audit rows** in `tx_lookup_audit`, including a `NOT_FOUND` lookup.

Credentials after seeding:

| Login | Password | Role |
| --- | --- | --- |
| `admin` | `admin123` | `SUPERVISOR` |
| `op1` … `op10` | `operator123` | `OPERATOR` |

### A note on bcrypt

`requirements.txt` pins `bcrypt==4.0.1`; `passlib` 1.7.4 reads
`bcrypt.__about__.__version__`, which newer bcrypt releases removed, so a
mismatched environment logs `(trapped) error reading bcrypt version` on first
hash. It is a noisy version probe, not a failure — hashing and verification
still work, and installing the pinned version silences it.

---

## 8. Running

```bash
cd backend
pip install -r requirements.txt
python seed.py --reset          # optional: demo data
uvicorn app.main:app --reload --port 8000
```

* API root: `http://127.0.0.1:8000` (business routes under `/api`)
* Interactive docs: `http://127.0.0.1:8000/docs`
* Health probe: `http://127.0.0.1:8000/health` → `{"status":"ok","db":true,"redis":true,...}`

PostgreSQL must be reachable (default `localhost:5433`); Redis is optional for a
single-process run and required only when fanning WS events across workers.

### Docker infrastructure

The dev infrastructure runs in Docker (PostgreSQL 16 + Redis 7). The bundled
containers map `5433 → 5432` and `6380 → 6379`, matching `POSTGRES_PORT` and
`REDIS_URL` in `config.py`:

```bash
docker ps
# tgdesk-postgres  postgres:16-alpine  0.0.0.0:5433->5432/tcp  (healthy)
# tgdesk-redis     redis:7-alpine      0.0.0.0:6380->6379/tcp  (healthy)
```

If Docker Desktop lives outside `PATH` (e.g.
`%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin\docker.exe`), call it by
full path. Outside Docker, point `POSTGRES_HOST` / `POSTGRES_PORT` /
`DATABASE_URL` at any reachable PostgreSQL 14+ instance.

---

## 9. Authentication

1. `POST /api/auth/login` with `{"username": "...", "password": "..."}` returns
   a JWT access token plus the operator profile.
2. Send it on every request as `Authorization: Bearer <token>`.
3. `GET /api/auth/me` returns the current operator; `POST /api/auth/logout`
   clears the server-side presence state.

Tokens carry the operator id and role; `require_supervisor` guards
supervisor-only routes. Passwords are stored as bcrypt hashes and verified with
`passlib`. Ten failed logins for one username inside five minutes return `429`
until the window passes. WebSocket clients authenticate with the same token
(query parameter or first frame) and are closed with code `1008` when it is
invalid.

---

## 10. Endpoints

Every business route is mounted under the **`/api`** prefix; only `/health` and
`/ws` sit at the root. Paths below are absolute.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness + DB/Redis probe → `{"status":"ok","db":true,"redis":true}`. |
| `POST` | `/api/auth/login` | Obtain a JWT. |
| `GET` | `/api/auth/me` | Current operator profile. |
| `POST` | `/api/auth/logout` | End the session. |
| `GET` | `/api/chats` | Chat list with unread counts, assignment and latest message. |
| `GET` | `/api/chats/{chat_id}/messages` | Paginated transcript, oldest first. |
| `POST` | `/api/chats/{chat_id}/messages` | Send an operator reply (honours the typing lock) → `201`. |
| `POST` | `/api/chats/{chat_id}/read` | Reset the unread counter. |
| `POST` | `/api/chats/{chat_id}/assign` | Assign / reassign the chat (**supervisor only**, else `403`). |
| `POST` | `/api/chats/{chat_id}/typing` | Claim (`action:"start"`) or release (`"stop"`) the collision lock. |
| `GET` | `/api/sla/stats/me` | Personal SLA scoreboard (hit rate, average response, open/overdue). |
| `GET` | `/api/sla/incidents` | Incident list, filterable by status/operator. |
| `POST` | `/api/tx/lookup` | Look a transaction up in **both** admin panels. |
| `GET` | `/api/tx/lookup/{tx_id}` | Same lookup by path parameter. |
| `GET` | `/api/tx/audit` | Audit trail of previous lookups. |
| `GET` | `/api/tx/suggest` | Id suggestions for the lookup box. |
| `GET` | `/api/shift/status` | Current shift / presence state. |
| `GET` | `/api/shift/operators` | Operator roster with shift states. |
| `POST` | `/api/shift/status` | Change own shift status. |
| `POST` | `/api/shift/heartbeat` | Keep the presence alive. |
| `POST` | `/api/internal/broadcast` | Internal WS fan-out hook. |
| `GET` | `/api/system/info` | Build/runtime info. |
| `WS` | `/ws` | Realtime event stream (`?token=<jwt>`). |

---

## 11. WebSocket events

All frames share one envelope:

```json
{ "type": "NEW_MESSAGE", "data": { } }
```

| Event | When |
| --- | --- |
| `CONNECTED` | Handshake accepted, token verified. |
| `SUBSCRIBED` | Client subscribed to a chat / filter set. |
| `NEW_MESSAGE` | A message arrived or was sent. |
| `MESSAGE_DELETED` | A message was deleted in Telegram. |
| `MESSAGE_STATUS` | Delivery status of an outgoing message changed. |
| `SLA_UPDATE` | An incident's remaining time changed (ticker). |
| `SLA_RESOLVED` | An incident was closed. |
| `TYPING_LOCK` | Another operator claimed the reply lock. |
| `TYPING_UNLOCK` | The lock was released or expired. |
| `CHAT_ASSIGNED` | Chat ownership changed. |
| `SHIFT_CHANGED` | An operator changed shift status. |
| `OPERATOR_PRESENCE` | Online/offline presence changed. |
| `PONG` | Heartbeat reply. |
| `ERROR` | Recoverable client error (bad payload, lock conflict). |
| `INTERNAL_ERROR` | Unexpected server error. |

Close codes: `1008` invalid token, `1012` server restart.
