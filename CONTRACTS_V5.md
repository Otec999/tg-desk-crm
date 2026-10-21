# CONTRACTS V5 — удаление наружу, настройки SLA, быстрые ответы, глобальный поиск, мгновенные реакции

Замороженный контракт для всех параллельных исполнителей. **Имена, сигнатуры и схемы
менять нельзя** — на них одновременно работают backend, worker и frontend.

Версия наследует V1–V4. Всё, что здесь не переопределено, действует по-прежнему.

---

## §1. Удаление сообщения: только CRM → Telegram, серый след сохраняется

### 1.1. Направление

Удаление **инициируется исключительно из CRM**. Обратное направление (Telegram → CRM)
уже работает и **не меняется**: `tg_ingest.handle_deleted` помечает `is_deleted_in_tg`,
строка остаётся.

### 1.2. Инвариант «ничего не пропадает»

Строка `GroupMessage` **никогда не удаляется физически** — ни при удалении из CRM,
ни при удалении из Telegram. Удаление из CRM ставит те же два поля, что и удаление
из Telegram:

```
is_deleted_in_tg = True
deleted_at      = <момент удаления>
```

плюс новое поле-признак источника:

```
deleted_source  = 'CRM' | 'TELEGRAM' | NULL   (NULL = не удалено)
```

Миграция добавляет колонку `deleted_source VARCHAR(16) NULL` и расширяет CHECK:

```
CHECK (deleted_source IS NULL OR deleted_source IN ('CRM','TELEGRAM'))
CHECK ((is_deleted_in_tg = false AND deleted_source IS NULL)
    OR (is_deleted_in_tg = true  AND deleted_source IS NOT NULL))
```

Второй CHECK гарантирует, что флаг и источник не разъедутся.

### 1.3. REST

```
DELETE /api/messages/{message_id}
  → 200 DeletedMessageOut
  → 404 если сообщение не найдено
  → 409 если уже удалено (is_deleted_in_tg = true)
  → 403 если у оператора can_delete_messages = False
```

`DeletedMessageOut`:

```json
{
  "message_id": 245,
  "chat_id": 7,
  "deleted_at": "2026-10-21T10:00:00Z",
  "deleted_source": "CRM",
  "message": { ...MessageOut... },
  "telegram": { "requested": true, "delivered": true, "error": null }
}
```

`message` — полный `MessageOut` уже помеченного сообщения, чтобы фронт обновил пузырь
без перезагрузки. `telegram.delivered=false` при `error` не делает запрос ошибочным:
CRM-состояние меняется **всегда**, недоставка в Telegram отражается только в ответе.

### 1.4. Redis-канал `tg_delete_out` (backend → worker)

```json
{ "cmd_id": "<uuid4>", "message_id": 245, "chat_id": 7,
  "tg_chat_id": -1004338873193, "tg_msg_id": 57, "operator_username": "op1" }
```

Публикуется, только если `tg_msg_id` и `tg_chat_id` не NULL. Ошибка публикации не
ломает ответ (best-effort, как в V4 §2).

### 1.5. ACK

Воркер отвечает в существующий `tg_outgoing_ack`, расширенный полем `cmd_kind`:

```json
{ "cmd_id": "...", "cmd_kind": "DELETE", "message_id": 245, "tg_msg_id": 57,
  "ok": true, "error": null }
```

Для V4-реакций `cmd_kind = "REACTION"`. Старые потребители ACK игнорируют незнакомое поле.

### 1.6. Worker

`DeleteMessagesRequest(channel, id=[tg_msg_id])`. Обрабатываются `TimeoutError`,
`FloodWaitError`, `RPCError`. Сообщение, уже удалённое в Telegram
(`MessageDeleteForbiddenError`), считается успехом (`ok=true`): цель достигнута.

Канал **не** входит в `WORKER_CHANNELS` (это канал, который воркер *потребляет*).

---

## §2. Настройки SLA в админке

### 2.1. Хранение

Новая таблица `app_settings` — одна строка, `id = 1`:

```
id                     INTEGER PRIMARY KEY
sla_enabled            BOOLEAN NOT NULL DEFAULT true
sla_target_seconds     INTEGER NOT NULL DEFAULT 1800
sla_reset_on_operator_reply BOOLEAN NOT NULL DEFAULT true
updated_at             TIMESTAMPTZ NOT NULL
updated_by             VARCHAR(64) NULL
```

`CHECK (sla_target_seconds BETWEEN 60 AND 86400)`

Читается через `services/settings_service.get_settings(db)`, кэшируется в процессе
на 5 секунд (ключ `_cache`), инвалидируется при записи.

### 2.2. REST (только администратор)

```
GET   /api/admin/settings        → AdminSettingsOut
PATCH /api/admin/settings        → AdminSettingsOut
```

`AdminSettingsOut`:

```json
{ "sla_enabled": true, "sla_target_seconds": 1800,
  "sla_reset_on_operator_reply": true, "updated_at": "...", "updated_by": "admin" }
```

### 2.3. Семантика

* `sla_enabled = false` → `open_incident` возвращает `None` **всегда**; новые инциденты
  не создаются. Уже открытые **не** закрываются автоматически — они живут своей жизнью
  и могут быть закрыты ответом. Тикер перестаёт поднимать эскалацию по новым.
* `sla_enabled = true` → прежнее поведение.
* `sla_target_seconds` — глобальный дефолт. Приоритет: `Operator.sla_target_seconds`
  (если задан) → `app_settings.sla_target_seconds`.
* Переключение `sla_enabled` пишется в `admin_audit_log`.

---

## §3. Новая формула SLA: отсчёт от последнего ответа CRM

### 3.1. Правило

Клок SLA для чата **взводится заново на каждый ответ оператора** и тикает от этого
момента. Точная семантика:

| Событие | Действие |
|---|---|
| Входящее сообщение клиента, PENDING-инцидента нет | открыть инцидент, `deadline_at = сейчас + target` |
| Входящее сообщение клиента, PENDING уже есть | **переиспользовать** инцидент, дедлайн **не** продлевать (клиент не должен получать отсрочку) |
| Ответ оператора (любой, включая не-цитатный) | закрыть PENDING-инцидент **и** ничего не открывать |
| Следующее входящее после ответа оператора | открыть **новый** инцидент с новым дедлайном |
| В чате вообще не было ответа CRM | клок тикает от первого входящего без ответа |

Ключевой смысл фразы «если последний ответ с нашей стороны был — обнуляется и
начинается»: ответ оператора **сбрасывает** клок в ноль, а новый клок стартует со
следующего входящего сообщения.

### 3.2. Где реализуется

* `sla_engine.resolve_for_reply` — при `reply_to_msg_id is None` закрывает самый
  срочный PENDING-инцидент чата (уже реализовано в V4-итерации, сохраняется).
* `get_settings(db).sla_reset_on_operator_reply = true` (дефолт) включает это правило.
* При `false` ответ оператора закрывает инцидент, только если он цитирует сообщение
  клиента (легаси-поведение).

---

## §4. Мгновенные реакции

### 4.1. Требование

Клик по эмодзи и его смена должны быть видны **сразу**, без задержки.

### 4.2. Причины текущей задержки (замерено)

| Слой | Замер | Вывод |
|---|---|---|
| `PUT /reaction` HTTP | ~70 мс | не узкое место |
| WS-кадр после ответа | +1–2 мс | не узкое место |
| Отрисовка в браузере | **172 мс** | ← узкое место |

### 4.3. Что исправляется

1. `ChatWindow.tsx` — `handleReact` / `handleUnreact` оборачиваются в `useCallback`,
   чтобы `memo(MessageBubble)` заработал и не перерисовывался весь таймлайн.
2. `MessageBubble.tsx` — `onAdd`/`onRemove` стабилизируются через `useCallback`,
   чтобы заработал `memo(ReactionBar)`.
3. `services/reactions.py` — `_broadcast` и `publish_reaction_to_telegram` уходят из
   критического пути ответа (`asyncio.create_task` либо после `return`).

Инвариант: оптимистичное обновление в store (`putReaction`/`removeReaction`)
**не трогается** — оно уже корректно.

---

## §5. Редактируемые быстрые ответы

### 5.1. Таблица `quick_replies`

```
id            SERIAL PRIMARY KEY
shortcut      VARCHAR(32) NOT NULL          -- короткий код, уникальный
title         VARCHAR(120) NOT NULL         -- подпись в списке
body          TEXT NOT NULL                 -- текст, вставляемый в поле ввода
category      VARCHAR(40) NOT NULL DEFAULT 'general'
sort_order    INTEGER NOT NULL DEFAULT 0
is_active     BOOLEAN NOT NULL DEFAULT true
created_by_id INTEGER NULL REFERENCES operators(id) ON DELETE SET NULL
created_at    TIMESTAMPTZ NOT NULL
updated_at    TIMESTAMPTZ NOT NULL
```

Индексы: `UNIQUE (shortcut)`, `INDEX (is_active, sort_order)`.
`CHECK (length(btrim(body)) > 0)`.

### 5.2. REST

```
GET    /api/quick-replies              → QuickReplyListOut  (все активные, отсортированы)
GET    /api/quick-replies?all=true     → включая неактивные (только администратор)
POST   /api/quick-replies              → 201 QuickReplyOut
PATCH  /api/quick-replies/{id}         → 200 QuickReplyOut
DELETE /api/quick-replies/{id}         → 204
```

Ошибки: `404` нет такого; `409` дубликат `shortcut`; `422` пустой `body`.

`QuickReplyOut`:

```json
{ "id": 1, "shortcut": "tx", "title": "Запросить TX ID",
  "body": "Could you please share the transaction ID…",
  "category": "general", "sort_order": 10, "is_active": true,
  "created_by": "admin", "created_at": "...", "updated_at": "..." }
```

### 5.3. WS-событие `QUICK_REPLIES_UPDATED`

```json
{ "type": "QUICK_REPLIES_UPDATED", "data": { "items": [ ...QuickReplyOut... ] } }
```

Расслается всем подключённым операторам после любого изменения, чтобы список
обновился у всех без перезагрузки.

### 5.4. Сид

Четыре текущих захардкоженных ответа переносятся в таблицу при первом старте
(идемпотентно, по `shortcut`). Хардкод `QUICK_REPLIES` из `MessageInput.tsx`
**удаляется**.

---

## §6. Глобальный поиск

### 6.1. Индекс

PostgreSQL, `pg_trgm` + GIN (подстрочный поиск как в Telegram; `tsvector` не подходит —
он не находит подстроки в телефонах и номерах):

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS ix_gm_text_trgm    ON group_messages USING gin ((text) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_gm_caption_trgm ON group_messages USING gin ((caption) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_gm_sender_trgm  ON group_messages USING gin ((sender_name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_gc_title_trgm   ON group_chats    USING gin ((title) gin_trgm_ops);
```

Миграция — `backend/migrate_v3_search.py`, идемпотентная, с `--dry-run`, по образцу
`migrate_v2.py`. На не-PostgreSQL пропускается.

### 6.2. REST

```
GET /api/search?q=<строка>&chat_id=&sender=all|client|operator
                &date_from=&date_to=&limit=20&offset=0&include_deleted=false
  → 200 SearchResponseOut
```

`q`: 1..200 символов, обязателен; регистронезависимый подстрочный поиск по
`text`, `caption`, `sender_name`, `phone`, `GroupChat.title` и `GroupChat.merchant_code`.

> Уточнение (реализовано в `services/search.py`): `merchant_code` участвует и в
> поиске, и в выдаче (`chat_merchant_code`), чтобы оператор мог найти чат по
> `MRC-ALPHA`. Подстрочный поиск обязателен: `q=5607` должен находить `+1792560788`.

`SearchResponseOut`:

```json
{
  "items": [{
    "chat_id": 7, "chat_title": "Merchant Alpha - Payouts",
    "chat_merchant_code": "MRC-001",
    "message": { ...MessageOut... },
    "snippet": "…найдено слово…",
    "match_spans": [[12, 18]],
    "score": 0.87
  }],
  "total": 42, "has_more": true, "took_ms": 11
}
```

Ранжирование: `similarity()` по `text`/`caption` DESC, затем `created_at` DESC.

### 6.3. UI

Оверлей в стиле Telegram (`SearchOverlay.tsx`), горячая клавиша **Ctrl/⌘+K**.
Клик по результату → переход в чат и подсветка сообщения через существующие
`jumpToMessage` + `flash-target`.

`selectChat` не должен перезатирать контекст: добавляется опция
`selectChat(chatId, { preserveContext: true })`.

---

## §7. Владение файлами

| Файл | Владелец |
|---|---|
| `backend/app/models.py` (§1.2, §2.1, §5.1) | Модели |
| `backend/migrate_v4.py` (`deleted_source`, `app_settings`, `quick_replies`) | Модели |
| `backend/migrate_v3_search.py` (§6.1) | Поиск-БД |
| `backend/app/services/settings_service.py` (§2.1) | SLA-бэкенд |
| `backend/app/services/sla_engine.py` (§2.3, §3) | SLA-бэкенд |
| `backend/app/routers/admin.py` (§2.2) | SLA-бэкенд |
| `backend/app/services/message_delete.py` (§1.3, §1.4) | Удаление-бэкенд |
| `backend/app/routers/messages.py` (§1.3) | Удаление-бэкенд |
| `backend/app/redis_bus.py` (§1.4) | Удаление-бэкенд |
| `telethon_worker/worker.py` (§1.5, §1.6) | Удаление-воркер |
| `backend/app/models.py` — `QuickReply` (§5.1) | Быстрые ответы-бэкенд |
| `backend/app/schemas.py` — `QuickReply*` (§5.2) | Быстрые ответы-бэкенд |
| `backend/app/services/quick_replies.py` (§5.2, §5.3) | Быстрые ответы-бэкенд |
| `backend/app/routers/quick_replies.py` (§5.2) | Быстрые ответы-бэкенд |
| `backend/app/services/reactions.py` (§4.3 п.3) | Реакции-бэкенд |
| `backend/app/services/search.py`, `backend/app/routers/search.py` (§6.2) | Поиск-бэкенд |
| `frontend/src/components/ChatWindow.tsx` (§4.3 п.1) | Фронт-перф |
| `frontend/src/components/MessageBubble.tsx` (§4.3 п.2, §1 кнопка удаления) | Фронт-перф |
| `frontend/src/components/QuickReplyEditor.tsx` (§5) | Фронт-бэкенд быстрых |
| `frontend/src/components/MessageInput.tsx` (§5.4) | Фронт-бэкенд быстрых |
| `frontend/src/components/SearchOverlay.tsx` (§6.3) | Фронт-поиск |
| `frontend/src/lib/api.ts`, `types.ts` | каждый — только свои методы/типы |
| `frontend/src/store/useCRMStore.ts` | согласовано по секциям |
| `frontend/src/pages/AdminPage.tsx` (§2 UI, §5 UI) | Админ-UI |

---

## §8. Инварианты

1. Строки `group_messages` не удаляются физически никогда.
2. `is_deleted_in_tg` и `deleted_source` согласованы (CHECK гарантирует).
3. Удаление из CRM всегда коммитится, даже если Telegram недоступен.
4. `sla_enabled=false` не создаёт новых инцидентов, но не закрывает старые.
5. Ответ оператора сбрасывает SLA-клок; новый клок стартует со следующего входящего.
6. Оптимистичное обновление реакций не удаляется и не обходится.
7. Скорость реакции не должна зависеть от числа сообщений в чате.
8. Поиск ищет по подстроке, а не по словам (телефоны, коды, опечатки).
9. Быстрые ответы живут в БД; в коде фронтенда их списка нет.
10. `tg_delete_out` не входит в `WORKER_CHANNELS`.
