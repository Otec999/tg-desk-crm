# TG-Desk CRM — SHARED CONTRACTS (single source of truth)

This file is the frozen cross-service contract. Backend, Telethon worker and Frontend MUST implement it exactly.
Do not rename anything here. If something is genuinely missing, add it in an additive (backwards compatible) way.

---

## 0. Ports / origins

| Service | URL |
|---|---|
| Frontend (Vite dev) | `http://localhost:5173` |
| Backend REST | `http://localhost:8000/api` |
| Backend WebSocket | `ws://localhost:8000/ws` (query param `?token=<jwt>`) |
| Backend health | `http://localhost:8000/health` |
| PostgreSQL 16 | `localhost:5433` (host) -> `5432` (container) |
| Redis 7 | `localhost:6380` (host) -> `6379` (container) |

CORS allowed origins for dev: `http://localhost:5173`, `http://127.0.0.1:5173`.

---

## 1. Environment variables (`.env` at repo root, loaded by backend via python-dotenv)

```
# ---- PostgreSQL ----
POSTGRES_USER=tgcrm
POSTGRES_PASSWORD=tgcrm_pass
POSTGRES_DB=tgcrm
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
# ---- Redis ----
REDIS_URL=redis://localhost:6380/0
# ---- Backend ----
BACKEND_SECRET_KEY=change-me-in-production-please-use-a-long-random-string
BACKEND_ACCESS_TOKEN_EXPIRE_MINUTES=720
BACKEND_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
BACKEND_SLA_SECONDS=300
BACKEND_WS_HEARTBEAT_TIMEOUT_SECONDS=45
BACKEND_WS_TICK_SECONDS=2
BACKEND_SUPERVISOR_USERNAME=admin
BACKEND_SUPERVISOR_PASSWORD=admin123
BACKEND_SEED_OPERATOR_PASSWORD=operator123
# ---- Admin adapters (mock now, real later) ----
ADMIN_ONE_BASE_URL=https://admin-one.internal.example/api
ADMIN_ONE_API_KEY=mock-admin-one-key
ADMIN_TWO_BASE_URL=https://admin-two.internal.example/api
ADMIN_TWO_API_KEY=mock-admin-two-key
# ---- Telethon worker ----
TG_API_ID=123456
TG_API_HASH=0123456789abcdef0123456789abcdef
TG_SESSION_STRING=
TG_STRING_SESSION=
TG_SESSION_FILE=./telethon_worker/tg_desk.session
TG_MONITORED_CHAT_IDS=
TELEGRAM_CHAT_ID=
TELEGRAM_ADMIN_CHAT_ID=
TELEGRAM_ADMIN_CHAT_IDS=
```

The `DATABASE_URL` is derived as
`postgresql+asyncpg://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}`.
Default values must match `.env.example` so that the stack boots with zero editing.

---

## 2. Canonical enum values (string constants, stored as VARCHAR + CHECK)

```
ShiftStatus        : "ONLINE" | "PAUSE" | "OFFLINE"
DeliveryStatus     : "pending" | "sent" | "failed"
SLAIncidentStatus  : "PENDING" | "RESOLVED"
TxAdminPanel       : "ADMIN_ONE" | "ADMIN_TWO"
TxStatus (mock)    : "COMPLETED" | "PROCESSING" | "PENDING" | "FAILED" | "DECLINED" | "REFUNDED" | "NOT_FOUND"
MessageSenderType  : "OPERATOR" | "CLIENT" | "BOT" | "SYSTEM"
```

Every entity has `id` (int PK), and JSON responses use snake_case exactly as in the DB.
Datetimes are **timezone-aware UTC ISO 8601 with `Z` suffix** in JSON (e.g. `2024-06-01T12:00:00Z`).

---

## 3. REST API (prefix `/api`)

All routes except `/api/auth/login` and `/health` require `Authorization: Bearer <jwt>`.
Errors: `{"detail": "<message>"}` with proper status code (401/403/404/409/422/503).

### 3.1 Auth

`POST /api/auth/login`
```json
// request
{"username": "op1", "password": "operator123"}
// 200 response
{"access_token": "<jwt>", "token_type": "bearer", "expires_in": 43200, "operator": <OperatorOut>}
```
`GET /api/auth/me` -> `<OperatorOut>`

### 3.2 OperatorOut / Shift

```json
// OperatorOut
{
  "id": 1,
  "username": "op1",
  "display_name": "Operator One",
  "role": "OPERATOR",            // "OPERATOR" | "SUPERVISOR"
  "shift_status": "ONLINE",
  "is_online_ws": true,
  "last_seen_at": "2024-06-01T12:00:00Z",
  "sla_target_seconds": 300,
  "created_at": "2024-06-01T10:00:00Z"
}
```

`GET  /api/shift/status` -> `<PersonalSlaStats>` (see 3.6) for the caller
`GET  /api/shift/operators` -> `{"items": [<OperatorOut>, ...]}` (everyone can read; used by the roster)
`POST /api/shift/status` body `{"status": "ONLINE"}` -> `<OperatorOut>`
`POST /api/shift/heartbeat` -> `{"ok": true, "server_time": "..."}` (called every 20 s by the frontend)

`GET /api/sla/stats/me` -> `<PersonalSlaStats>`

```json
// PersonalSlaStats
{
  "operator_id": 1,
  "shift_status": "ONLINE",
  "avg_response_seconds": 75,
  "resolved_count": 12,
  "open_count": 2,
  "overdue_count": 0,
  "sla_percent": 98.0,
  "sla_target_seconds": 300
}
```

### 3.3 Chats

`GET /api/chats?filter=all|mine|burning` -> `{"items": [<ChatListItem>, ...]}`

```json
// ChatListItem
{
  "id": 1,
  "tg_chat_id": -1001234567890,
  "title": "Merchant Alpha — Payouts",
  "username": "merchant_alpha_support",
  "merchant_code": "MRC-ALPHA",
  "unread_count": 3,
  "assigned_operator_id": 2,
  "assigned_operator_name": "Operator Two",
  "last_message": {
    "id": 55,
    "text": "TX-4412-DD declined",
    "sender_name": "AcmePayout Bot",
    "is_outgoing": false,
    "created_at": "2024-06-01T12:00:00Z"
  },
  "sla": {"status": "PENDING", "deadline_at": "2024-06-01T12:05:00Z", "remaining_seconds": 120, "is_overdue": false} // or null
}
```

`GET /api/chats/{chat_id}/messages?limit=100&before_id=` -> `{"items": [<MessageOut>, ...], "has_more": false}` (ascending by `created_at`)
`POST /api/chats/{chat_id}/read` -> `{"ok": true, "unread_count": 0}`
`POST /api/chats/{chat_id}/assign` body `{"operator_id": 2 | null}` (SUPERVISOR or self) -> `<ChatListItem>`

```json
// MessageOut
{
  "id": 55,                                  // CRM message id (bigint)
  "chat_id": 1,
  "tg_msg_id": 90210,                        // nullable
  "sender_type": "BOT",                       // OPERATOR | CLIENT | BOT | SYSTEM
  "sender_operator_id": 2,                    // nullable, set when sender_type == OPERATOR
  "sender_name": "Operator Two",
  "sender_tg_id": 771234,                     // nullable (raw TG user id)
  "text": "TX-4412-DD declined",
  "reply_to_id": 54,                          // nullable, CRM id of quoted message
  "reply_to_tg_msg_id": 90204,                // nullable
  "phone": "+1 555 0100",                     // nullable — used for tx lookup fallback
  "is_deleted_in_tg": false,
  "deleted_at": null,                         // ISO or null
  "sla_incident_id": 9,                       // nullable — incident this message RESOLVED
  "delivery_status": "sent",                  // pending | sent | failed
  "is_outgoing": false,
  "created_at": "2024-06-01T12:00:00Z"
}
```

### 3.4 Messages

`POST /api/chats/{chat_id}/messages`
```json
{"text": "Hello!\nTX-...: COMPLETED", "reply_to_msg_id": 55, "client_nonce": "uuid-from-frontend"}
```
`reply_to_msg_id` is **either** a CRM message id **or** a TG msg id (backend resolves both).
-> 201 `<MessageOut>` (delivery_status = "pending", then updated to "sent"/"failed" by worker ack).
409 when the chat is locked by another operator (`TYPING_LOCK`), body includes `{"detail": "...", "locked_by": "<display_name>"}`.
Idempotency: same `client_nonce` returns the previously created message.

### 3.5 Typing / collision lock

`POST /api/chats/{chat_id}/typing` body `{"action": "start" | "stop"}` -> `{"ok": true, "locked_by": "<display_name or null>"}`
Lock TTL = 15 s, refreshed by repeated starts, released on stop / send / disconnect.

### 3.6 Transaction lookup (Dual admin adapters)

`POST /api/tx/lookup`
```json
{"tx_id": "TX-4412-DD", "panel": "ADMIN_ONE"}   // panel optional -> both panels
```
-> 200 `<TxLookupResponse>`

```json
// TxLookupResponse
{
  "tx_id": "TX-4412-DD",
  "looked_up_at": "2024-06-01T12:00:00Z",
  "admin_one": {
    "panel": "ADMIN_ONE",
    "found": true,
    "tx_id": "TX-4412-DD",
    "status": "FAILED",
    "amount": "1500.00",          // STRING decimal, 2 dp
    "currency": "USDT",
    "provider": "AcmePayout",
    "error_reason": "Insufficient provider balance",
    "external_ref": "gw_7f3a91",
    "raw": {"...": "..."}         // provider payload (mock)
  },
  "admin_two": { ...same shape with "panel": "ADMIN_TWO"... }
}
```
`GET /api/tx/lookup/{tx_id}?panel=` -> same response.
`GET /api/tx/audit?limit=100` -> `{"items":[{"id":1,"operator_id":2,"operator_username":"op2","tx_id":"TX-4412-DD","admin_panel":"ADMIN_ONE","found":true,"created_at":"..."}]}`
(SUPERVISOR sees all, operator sees own.)

Mock determinism: derived from a stable hash of the uppercase `tx_id` string, so the same TX ID always returns the same card in both panels, including `NOT_FOUND` for ids starting with `TX-0000`.

### 3.7 Internal broadcast / ops

`POST /api/internal/broadcast` (SUPERVISOR only) body `{"event": "<WS envelope>", "target": "ALL"}` -> `{"ok": true}`
`GET  /api/system/info` -> `{"telegram_connected": true, "ws_clients": 3, "workers": {...}, "sla_ticking": true}`

---

## 4. WebSocket protocol (`/ws?token=<jwt>`)

Server -> client envelope, always:
```json
{"type": "<EVENT_TYPE>", "ts": "2024-06-01T12:00:00Z", "data": { ... }}
```

| type | data |
|---|---|
| `CONNECTED` | `{"operator": <OperatorOut>}` |
| `NEW_MESSAGE` | `<MessageOut>` + `{"chat": <ChatListItem>}` under `data.chat` |
| `MESSAGE_DELETED` | `{"message_id": 55, "chat_id": 1, "deleted_at": "...", "message": <MessageOut>}` |
| `SLA_UPDATE` | `{"chat_id": 1, "incident_id": 9, "status": "PENDING", "deadline_at": "...", "remaining_seconds": 120, "assigned_operator_id": 2, "assigned_operator_name": "Operator Two", "is_overdue": false}` |
| `SLA_RESOLVED` | `{"chat_id": 1, "incident_id": 9, "operator_id": 2, "response_time_seconds": 75, "stats": <PersonalSlaStats>}` |
| `TYPING_LOCK` | `{"chat_id": 1, "operator_id": 2, "operator_name": "Operator Two", "until": "..."}` |
| `TYPING_UNLOCK` | `{"chat_id": 1, "operator_id": 2}` |
| `SHIFT_CHANGED` | `<OperatorOut>` |
| `OPERATOR_PRESENCE` | `{"operator_id": 2, "is_online_ws": true, "shift_status": "ONLINE"}` |
| `CHAT_ASSIGNED` | `{"chat_id": 1, "assigned_operator_id": 2, "assigned_operator_name": "Operator Two"}` |
| `MESSAGE_STATUS` | `{"message_id": 55, "chat_id": 1, "delivery_status": "sent", "tg_msg_id": 90210}` |
| `ERROR` | `{"detail": "..."}` |
| `PONG` | `{"server_time": "..."}` |

Client -> server (plain JSON objects, no envelope):
```json
{"type": "PING"}
{"type": "TYPING_LOCK", "chat_id": 1}
{"type": "TYPING_UNLOCK", "chat_id": 1}
{"type": "HEARTBEAT"}
{"type": "SUBSCRIBE", "chat_id": 1}
```

Every server broadcast goes to **all** connected operators.
Heartbeat: frontend sends `HEARTBEAT` every 20 s; backend marks `is_online_ws=false` and falls back
`shift_status` ONLINE -> PAUSE (not OFFLINE) after `BACKEND_WS_HEARTBEAT_TIMEOUT_SECONDS` of silence.

---

## 5. Redis channels & payloads (backend <-> telethon worker)

| channel | direction | payload |
|---|---|---|
| `tg_incoming` | worker -> backend | `{"kind":"NEW_MESSAGE","tg_chat_id":-1001234567890,"tg_msg_id":90210,"chat_title":"...","chat_username":"...","sender_tg_id":771234,"sender_name":"Jane Doe","sender_username":"jane","is_bot":false,"text":"...","reply_to_tg_msg_id":90204,"tg_date":"2024-06-01T12:00:00+00:00","merchant_code":null}` |
| `tg_outgoing` | backend -> worker | `{"cmd_id":"uuid","chat_id":1,"tg_chat_id":-1001234567890,"message_id":55,"text":"...","reply_to_tg_msg_id":90204,"operator_username":"op1","via_crm":true}` |
| `tg_outgoing_ack` | worker -> backend | `{"cmd_id":"uuid","chat_id":1,"message_id":55,"tg_msg_id":90210,"ok":true,"error":null}` |
| `tg_deleted` | worker -> backend | `{"kind":"MESSAGE_DELETED","tg_chat_id":-1001234567890,"tg_msg_ids":[90210,90211]}` |
| `tg_edits` | worker -> backend | `{"kind":"MESSAGE_EDITED","tg_chat_id":-100...,"tg_msg_id":90210,"text":"..."}` |

Backend also uses keys `typing_lock:{chat_id}` (short TTL string = operator id) and
`xadd`-free simple pub/sub for WS fan-out with channel `ws_fanout` (optional, JSON envelope as in §4).

---

## 6. Database tables (SQLAlchemy 2.0 async models, `app/models.py`)

- `operators`: id, username(unique), password_hash, display_name, role, shift_status, is_online_ws, last_seen_at, sla_target_seconds, created_at, updated_at
- `group_chats`: id, tg_chat_id(unique bigint), title, username, merchant_code, unread_count, assigned_operator_id FK, last_message_at, created_at, updated_at
- `group_messages`: id, chat_id FK, tg_msg_id bigint nullable, sender_type, sender_operator_id FK nullable, sender_name, sender_tg_id bigint nullable, text, reply_to_id FK nullable (self), reply_to_tg_msg_id bigint nullable, phone, is_deleted_in_tg bool, deleted_at, sla_incident_id FK nullable, delivery_status, client_nonce unique nullable, created_at
- `sla_incidents`: id, chat_id FK, message_id FK (triggering client message), assigned_operator_id FK nullable, status, deadline_at, resolved_at nullable, resolved_by_message_id FK nullable, response_time_seconds int nullable, escalated bool, created_at
- `tx_lookup_audit`: id, operator_id FK, tx_id, admin_panel, found bool, response_status, lookup_ms int, created_at

Indexes: `group_messages(chat_id, created_at)`, `group_messages(chat_id, tg_msg_id)`, `sla_incidents(status, chat_id)`, `tx_lookup_audit(tx_id)`, `tx_lookup_audit(operator_id, created_at)`.

---

## 7. Seed data (`backend/seed.py`)

- 10 operators `op1`..`op10` (password `operator123`, display names "Operator One".."Operator Ten"), roles OPERATOR; plus supervisor `admin`/`admin123`.
- 6 merchant group chats, e.g. "Merchant Alpha — Payouts" (`MRC-ALPHA`), "Merchant Beta — Settlements" (`MRC-BETA`), "Merchant Gamma — Chargebacks", "Merchant Delta — KYC", "Merchant Epsilon — Refunds", "Merchant Zeta — Integrations / API".
- 8–14 messages per chat mixing CLIENTS, BOT alerts (AcmePayout Bot / GatewayMonitor) with real-looking TX IDs (`TX-4412-DD`, `TX-7781-AC`, `TX-1102-BE`, `TX-9931-ZQ`, `TX-0000-XX` for NOT_FOUND), one operator reply with attribution, one pair of messages flagged `is_deleted_in_tg=True` with `deleted_at`, one message containing a `<code>`-style transaction id.
- 3 live `SLAIncident` rows: one PENDING with ~4 min left, one PENDING burning (< 60 s), one overdue; 20 RESOLVED historical incidents so SLA% is realistic.
- Seed is idempotent (`python -m seed --reset` drops+recreates; default upserts).

---

## 8. Frontend structure (frozen paths)

```
frontend/
  package.json  vite.config.ts  tsconfig.json  tsconfig.node.json
  tailwind.config.js  postcss.config.js  index.html
  .env.example
  src/main.tsx  src/App.tsx  src/index.css
  src/lib/api.ts        # typed REST client (fetch, bearer token)
  src/lib/ws.ts         # WsClient with reconnect + heartbeat + handler registry
  src/lib/types.ts      # mirrors §3/§4 exactly
  src/lib/mockData.ts   # offline fallback dataset (MOCK mode)
  src/lib/format.ts     # time, duration, SLA countdown helpers
  src/store/useCRMStore.ts      # Zustand store (auth, chats, messages, locks, sla, tx)
  src/components/TopBar.tsx
  src/components/ChatList.tsx
  src/components/SlaBadge.tsx
  src/components/ChatWindow.tsx
  src/components/MessageBubble.tsx
  src/components/MessageInput.tsx
  src/components/TxSidebar.tsx
  src/components/LoginScreen.tsx
```
Env: `VITE_API_URL=http://localhost:8000` (REST base + ws base derived). `VITE_MOCK_MODE=false` forces mock state.

Colours: bg `#0f0f0f` / panels `#181818` / elevated `#212121` / accent `#8774e1` / secondary text `#a0a0a0`,
bubble-in `#212121`, bubble-out `#8774e1` (or `#2b5278`-style alt), danger `#e5484d`, warning `#f5a524`.
