# Telethon ⇄ Redis bridge worker (`telethon_worker/`)

A long-lived Python process that logs in to the **corporate Telegram account** as a
normal user (MTProto via Telethon), watches the merchant groups, and bridges every
message / edit / deletion into the FastAPI backend over **Redis pub/sub**. Operator
replies queued by the backend are pushed back into Telegram and acknowledged.

```
Telegram (MTProto)  <──Telethon──>  worker.py  <──Redis pub/sub──>  FastAPI backend
```

The channel names and JSON field names are **frozen** by [`CONTRACTS.md` §5](../CONTRACTS.md).
Renaming anything here breaks the backend.

---

## 1. Get `api_id` / `api_hash`

1. Open <https://my.telegram.org> and log in with the **corporate account's** phone number.
2. **API development tools** → fill in any app title (e.g. `TG-Desk CRM`) and platform.
3. Copy the generated **App api_id** (a number) and **App api_hash** (32 hex chars).
4. Put them in the repository-root `.env` (copy `.env.example` first):

```dotenv
TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef
```

> The values shipped in `.env.example` are placeholders. The worker **refuses to start**
> while they are unchanged and tells you which variable to fix.

---

## 2. Create the session (log in once)

A session is the "authorised device" token. The worker never logs in interactively
when a session already exists — pick **one** of these:

### Option A — interactive worker path (recommended)

```powershell
cd telethon_worker
python worker.py --login
```

You will be asked for:

- `Phone (+...)` — e.g. `+79991234567`
- `Login code (from Telegram)` — the code Telegram sends you
- `2FA password` — only if two-step verification is enabled

The worker prints a single line:

```
TG_SESSION_STRING=1BVtsOK4...
```

Paste it into `.env` (`TG_SESSION_STRING=...`). Keep it secret — it grants full account access.

> Without `--login`, the same prompts appear automatically **only on a machine with no
> session at all** (empty `TG_SESSION_STRING` *and* no `TG_SESSION_FILE`). That path
> saves the session to the `.session` file and continues running.

### Option B — classic snippet

```powershell
python -c "from telethon.sync import TelegramClient; from telethon.sessions import StringSession; c = TelegramClient(StringSession(), API_ID, API_HASH); c.start(); print(c.session.save())"
```

Replace `API_ID` / `API_HASH` with your numbers, then store the printed value in
`TG_SESSION_STRING`.

### Option C — session file only

Leave `TG_SESSION_STRING` empty and keep `TG_SESSION_FILE=./telethon_worker/tg_desk.session`
(relative paths resolve against the **repo root**). The first run logs in and Telethon
writes that file; later runs reuse it silently. `*.session` is git-ignored.

**Never share** the session string or the `.session` file, and never commit them.

---

## 3. Install & run

```powershell
cd telethon_worker
python -m pip install -r requirements.txt
python worker.py
```

`cryptg` is listed in `requirements.txt` but **optional at runtime**: if the wheel
cannot be built, the worker logs `cryptg acceleration: NOT installed` and still works
(just slower on large media).

### CLI flags

| Flag | Effect |
|---|---|
| *(none)* | Run the bridge (foreground, `Ctrl+C` stops it cleanly). |
| `--dry-run` | Connect with the current credentials, log the dialogs it would monitor, exit. **Does not touch Redis.** Use it to smoke-test `api_id` / `api_hash` / session. |
| `--list-chats` | List every group/supergroup the account can see, marking which ones match `TG_MONITORED_CHAT_IDS`. Implies a no-Redis run. |
| `--login` | Force the interactive login, print a fresh `TG_SESSION_STRING`, exit. |
| `--log-level LEVEL` | Override the `LOG_LEVEL` env var (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`). |

Typical smoke test before trusting the bridge:

```powershell
python worker.py --dry-run --log-level DEBUG
```

### Unit tests

No network, no Redis, no Telegram account is needed — every dependency is an
in-memory double:

```powershell
cd telethon_worker
..\.venv\Scripts\python.exe -m pytest -q
```

`test_media.py` sits next to the modules; command-handler tests live in `tests/`
(e.g. `tests/test_worker_delete_out.py` for `tg_delete_out`).

---

## 4. Configuration (repository-root `.env`)

| Variable | Meaning |
|---|---|
| `TG_API_ID` / `TG_API_HASH` | From my.telegram.org. Required. |
| `TG_SESSION_STRING` | `StringSession` value. Preferred over the file. |
| `TG_STRING_SESSION` | Legacy alias, read when `TG_SESSION_STRING` is empty. |
| `TG_SESSION_FILE` | `*.session` path, relative to the repo root. |
| `TG_MONITORED_CHAT_IDS` | CSV of numeric `tg_chat_id`s and/or titles/usernames. **Empty = every group.** |
| `TELEGRAM_CHAT_ID`, `TELEGRAM_ADMIN_CHAT_ID`, `TELEGRAM_ADMIN_CHAT_IDS` | Merged into the list above (legacy keys). |
| `REDIS_URL` | Default `redis://localhost:6380/0` (host port of the Compose Redis). |
| `LOG_LEVEL` | `INFO` by default. |
| `TG_WORKER_NAME` | Name reported in `worker:status`; defaults to the hostname. |
| `TG_HEARTBEAT_SECONDS` | Heartbeat period, default `20`. |
| `TG_RECONNECT_MAX_DELAY` | Exponential-backoff cap in seconds, default `30`. |
| `MEDIA_ROOT` | Where attachments are downloaded. Default `storage/media` (repo root). `media_attachments.storage_path` is stored **relative** to it. |
| `MAX_DOWNLOAD_BYTES` | Ceiling for one attachment, default `52428800` (50 MiB). Accepts `50MB` / `1.5GiB`. Larger files are reported to the CRM but not saved. Aliases: `MEDIA_MAX_DOWNLOAD_BYTES`, `TG_MAX_DOWNLOAD_BYTES`. |

Numeric ids match exactly (`-1001234567890`). Text entries are matched
case-insensitively against the chat username and as a substring of the title, so
`TG_MONITORED_CHAT_IDS=merchant alpha,-1001234567890` works.

**The worker never logs** `TG_API_ID`, `TG_API_HASH` or the session string; the startup
banner shows them as `<set>` and `1BVtsO… (len=…)` only.

---

## 5. How the Redis bridge works

| Channel | Direction | Published by | Consumed by |
|---|---|---|---|
| `tg_incoming` | worker → backend | `NewMessage` handler | backend ingester |
| `tg_outgoing` | backend → worker | backend (`POST /api/chats/{id}/messages`) | `outgoing_loop` |
| `tg_outgoing_ack` | worker → backend | `outgoing_loop` | backend (`MESSAGE_STATUS`) |
| `tg_deleted` | worker → backend | `MessageDeleted` handler | backend |
| `tg_edits` | worker → backend | `MessageEdited` handler | backend |
| `tg_reactions` | worker → backend | `MessageReactionUpdated` / raw `UpdateMessageReactions` | backend (CONTRACTS_V2 §4) |
| `tg_reaction_out` | backend → worker | backend (§4 реакции) | `_handle_reaction_command` |
| `tg_delete_out` | backend → worker | backend (`DELETE /api/messages/{id}`) | `_handle_delete_command` |

### Exact payloads

`tg_incoming` — one per group message (`media`, `caption`, `message_kind` and
`sender_type` are the CONTRACTS_V2 additions; `kind` stays the envelope
discriminator):

```json
{"kind":"NEW_MESSAGE","tg_chat_id":-1001234567890,"tg_msg_id":90210,"chat_title":"Merchant Alpha — Payouts","chat_username":"merchant_alpha","sender_tg_id":771234,"sender_name":"Jane Doe","sender_username":"jane","is_bot":false,"sender_type":"USER","text":"TX-4412-DD declined","message_kind":"TEXT","caption":null,"attachment_count":0,"media":[],"reply_to_tg_msg_id":90204,"tg_date":"2024-06-01T12:00:00+00:00","merchant_code":"MRC-ALPHA"}
```

Each `media[]` element mirrors `media_attachments` (`storage_path` is
POSIX-style and relative to `MEDIA_ROOT`; `storage_path: null` means "too large"):

```json
{"kind":"PHOTO","file_name":"photo_123.jpg","mime_type":"image/jpeg","size_bytes":48211,"width":800,"height":600,"duration_seconds":null,"storage_path":"chats/-1001234567890/90210/photo_123.jpg","tg_file_id":"photo/123/456…","thumbnail_path":"chats/-1001234567890/90210/photo_123.jpg.thumb.jpg"}
```

`tg_deleted` — batched, `tg_chat_id` may be `null` (see §6):

```json
{"kind":"MESSAGE_DELETED","tg_chat_id":-1001234567890,"tg_msg_ids":[90210,90211]}
```

`tg_edits` — one per edited message (`edited_at` is additive to the contract):

```json
{"kind":"MESSAGE_EDITED","tg_chat_id":-1001234567890,"tg_msg_id":90210,"text":"…","edited_at":"2024-06-01T12:05:00+00:00"}
```

`tg_reactions` — one per reaction change; `reactions: []` means "all removed".
Only the six contract emoji (`👍 ❤️ 🔥 👀 👏 🙏`) are forwarded; custom emoji and
anything else are dropped:

```json
{"kind":"REACTION_UPDATE","tg_chat_id":-1001234567890,"tg_msg_id":90210,"reactions":[{"emoji":"👍","count":2,"user_ids":[123]}]}
```

`tg_outgoing_ack` — exactly one per command, success **and** failure
(`attachment_sent: true` is added only for a successful `send_file`;
`cmd_kind` is added only for V5 command channels — `"REACTION"` / `"DELETE"` —
so the plain-send ack keeps the legacy shape):

```json
{"cmd_id":"uuid","chat_id":1,"message_id":55,"tg_msg_id":90210,"ok":true,"error":null}
{"cmd_id":"uuid","chat_id":1,"message_id":55,"tg_msg_id":90210,"ok":true,"error":null,"attachment_sent":true}
{"cmd_id":"uuid","chat_id":1,"message_id":55,"tg_msg_id":null,"ok":false,"error":"FloodWaitError: A wait of 42 seconds is required"}
{"cmd_id":"uuid","cmd_kind":"REACTION","chat_id":1,"message_id":55,"tg_msg_id":90210,"ok":true,"error":null}
{"cmd_id":"uuid","cmd_kind":"DELETE","chat_id":7,"message_id":245,"tg_msg_id":57,"ok":true,"error":null}
```

`tg_delete_out` (backend → worker, CONTRACTS_V5 §1.4) — delete one of our messages
in Telegram. The worker calls `channels.DeleteMessagesRequest(channel=<peer>,
id=[tg_msg_id])`; a message Telegram already considers gone
(`MessageDeleteForbiddenError`: already deleted / service message / not ours) is
answered with `ok: true` — the goal is reached, so the CRM must not show a
"not delivered" hint. `TimeoutError`, `FloodWaitError` and any other `RPCError`
produce `ok: false` with a readable `error`:

```json
{"cmd_id":"uuid","message_id":245,"chat_id":7,"tg_chat_id":-1004338873193,"tg_msg_id":57,"operator_username":"op1"}
```

Deletion is one-way (CRM → Telegram); `tg_deleted` keeps working in the opposite
direction and neither side erases a `group_messages` row (§1.1, §1.2).

`tg_outgoing` (backend → worker) carries `tg_chat_id`; `chat_id` alone is the CRM's own
integer id and cannot be resolved by Telegram, so the worker answers with
`ok: false` instead of guessing.

`attachment_path` (absolute path on the worker host) switches the send to
`client.send_file(entity, path, caption=text or None, reply_to=…)`; the 1024-char
caption limit applies instead of the 4096-char body limit. Without the field the
command is sent with `send_message` exactly as before.

### Liveness keys (for `GET /api/system/info`)

| Key | Type | Content |
|---|---|---|
| `worker:status` | hash | `connected` (`1`/`0`), `updated_at`, `worker`, `monitored_chats`, `media_root`, `max_download_bytes`, plus counters `incoming`/`deleted`/`edited`/`acks`/`failures`/`reactions`/`media_downloaded`/`media_failed`/`attachments_sent`/`forwarded`/`delete_out` |
| `worker:heartbeat` | string | ISO timestamp, TTL = `heartbeat_seconds × 3` |

On a clean shutdown the worker sets `connected=0` and deletes `worker:heartbeat`.

---

## 6. Deletes, edits and echo suppression

**Edits** are forwarded verbatim; the backend matches on `(tg_chat_id, tg_msg_id)`.

**Deletes** arrive in batches. When Telegram reports a deletion **without** the chat
(e.g. after a reconnect, or a moderator action in a supergroup), `tg_chat_id` is `null`.
This is intentional: the backend then matches the ids **globally** across
`group_messages`, so the CRM still flags them `is_deleted_in_tg = true`. Silently
dropping unattributable deletions would leave deleted messages visible forever.

**Deletion out** (CRM → Telegram, CONTRACTS_V5 §1) is a separate, one-way command on
`tg_delete_out`: `channels.DeleteMessagesRequest(channel=<peer>, id=[tg_msg_id])`. The
peer is the entity cached by `_resolve_entity` (falls back to the raw `tg_chat_id` if
`get_entity` fails), so supergroups and forums both work. `MessageDeleteForbiddenError`
— already deleted, a service message, or a message our account may not touch — is
reported as `ok: true`. The `group_messages` row is never erased by either direction.

**Echo suppression** — messages the CRM sends come back through
`events.NewMessage` with `out=True`. They are ignored outright, because the backend
already stored them as outgoing, and re-ingesting them would double-post. As a second
line of defence the worker keeps a module-level
`pending_crm_sends: dict[(chat, text_hash)] -> deadline` populated right after a
successful `send_message`, and drops any matching message for **30 seconds**.

**Group only** — private chats, bots and broadcast channels are never bridged; only
groups and supergroups (including forum topics, where `reply_to_msg_id` is read one
level deeper).

**Media-only messages** are forwarded with a placeholder (`[photo]`, `[document]`,
`[voice message]`, `[video]`, `[sticker]`, `[location]`, …) so the backend always has
a non-empty `text`.

**Merchant code** — taken from an explicit `#MRC-XXX` hashtag in the group title, or
derived from the first business word when the title looks like a merchant group
(`"Merchant Alpha — Payouts"` → `MRC-ALPHA`). Unrelated groups get `null`.

---

## 7. Adding the corporate account to groups

The account must **already be a member** of every group it watches — Telethon cannot
join channels silently or read what it cannot see.

1. Log in to Telegram as the corporate account.
2. Join each merchant group (ask the merchant for an invite link; join, don't lurk).
3. If the group is a **channel** (`t.me/...`, not a discussion group), it is ignored by
   design — ask the merchant to use a group/supergroup, or add the account with read
   rights and change the contract first.
4. Confirm with `python worker.py --dry-run` — the groups appear in the listing.
5. Optionally restrict the worker to those groups with `TG_MONITORED_CHAT_IDS`.

> Telethon uses a **user** session, so it sees exactly what the account sees. Never put
> API keys in a bot account for this — bots cannot read group history and cannot see
> edits/deletions from other members.

---

## 8. Flood-wait and rate limiting

- `FloodWaitError` from `send_message` → the worker sleeps **`min(e.seconds, 300)`**,
  then publishes `ok: false` with `FloodWaitError: ...`. The backend marks the row
  `failed`; the operator retries later.
- Sends are wrapped in a **90 s** timeout so one stuck request cannot freeze the loop.
- Read-only event handling is throttled by Telegram automatically; `NewMessage` bursts
  in busy groups are the normal load profile and are handled sequentially per account.
- Backend-side retry loops should back off — the worker acks every attempt, it does not
  queue them internally.

---

## 9. Running it as a long-lived process

The worker is **not** a cron job: it must stay running 24/7. Message history is
**only captured after the worker connects** — nothing earlier is backfilled, so start
it once and keep it alive.

### Windows — NSSM (recommended)

```powershell
nssm install TgDeskWorker "C:\...\Python312\python.exe" "worker.py"
nssm set TgDeskWorker AppDirectory "C:\Users\Работа\Desktop\Custom CRM\telethon_worker"
nssm set TgDeskWorker AppStdout  "C:\Users\Работа\Desktop\Custom CRM\telethon_worker\worker.log"
nssm set TgDeskWorker AppStderr  "C:\Users\Работа\Desktop\Custom CRM\telethon_worker\worker.err.log"
nssm set TgDeskWorker AppRotateFiles 1
nssm set TgDeskWorker Start SERVICE_AUTO_START
nssm start TgDeskWorker
```

### Windows — Task Scheduler

Task `TgDeskWorker`, trigger **At startup** (and *Restart on failure*), action
`python.exe` with argument `worker.py` and *Start in* = `...\telethon_worker`.
Tick **Run whether user is logged on or not**.

### pm2

```bash
pm2 start worker.py --name tg-desk-worker --interpreter python --cwd ./telethon_worker
pm2 save && pm2 startup
```

### systemd

```ini
[Unit]
Description=TG-Desk Telethon worker
After=network-online.target

[Service]
WorkingDirectory=/opt/custom-crm/telethon_worker
ExecStart=/usr/bin/python3 worker.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Operational notes

- The supervisor auto-reconnects forever with `1, 2, 4, …` seconds backoff capped at
  `TG_RECONNECT_MAX_DELAY` (default 30 s) and **recreates the client** each time.
- `Ctrl+C` / `SIGTERM` closes the Telethon client and the Redis connection cleanly;
  on Windows `Ctrl+C` is the supported signal (`SIGBREAK` is also handled).
- Redis unreachable at startup → the worker retries for ~30 s and then exits with a
  clear error instead of hanging silently.
- Two workers on the same session will fight over updates — run exactly **one** per
  account, or give each worker its own account.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `credentials are not configured` | `TG_API_ID` / `TG_API_HASH` still placeholders in `.env`. |
| `TG_SESSION_STRING was rejected` | Session revoked/expired → `python worker.py --login` and update `.env`. |
| `Redis is unreachable … after 30s` | Start Redis: `docker compose -f docker-compose.dev.yml up -d redis`; check `REDIS_URL` (host port **6380**). |
| `--dry-run` shows `monitored=0` | Account is not in the groups, or `TG_MONITORED_CHAT_IDS` does not match (check spacing/case). |
| Messages arrive in CRM but replies don't send | Backend must include `tg_chat_id`; an ack with `ok:false` explains why. |
| `FloodWaitError` in logs | Telegram throttled the account; the worker already waited and failed the message. |
| `cryptg acceleration: NOT installed` | Optional speedup missing — harmless. |
