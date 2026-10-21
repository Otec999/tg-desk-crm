# CONTRACTS_V3 — пересылка и редактирование (ЗАМОРОЖЕНО)

Дополнение к `CONTRACTS.md` (v1) и `CONTRACTS_V2.md` (v2). Ничего из v1/v2 не отменяется.

---

## 1. Пересылка сообщений между чатами

### 1.1 Требование
Оператор берёт сообщение в чате А (фото + подпись, или только текст) и отправляет его в чат Б.

**Критично:** в чате Б **не должно быть видно**, что сообщение пришло из другого чата и отправлено другим человеком. Всегда отправляем **от лица нашего Telegram-аккаунта** (того, под которым работает воркер), как обычное сообщение с нашим текстом.

Это значит: **никакого Telegram-`forward`** (`forward_messages`), потому что он показывает «Переслано от …». Вместо этого:
- текст/подпись копируются как **собственный текст**;
- медиа скачивается из чата А и загружается в чат Б как новое вложение.

### 1.2 REST
```
POST /api/messages/{message_id}/forward
Body: {"target_chat_id": 12, "comment": "необязательный текст сверху"}
→ 201 MessageOut   (сообщение, созданное в целевом чате)
```
Ошибки: `404` сообщение/чат не найдены, `409` целевой чат занят блокировкой ввода,
`422` целевой чат совпадает с исходным.

### 1.3 Redis (backend → worker)
Новый канал **`tg_forward`**, payload:
```json
{
  "cmd_id": "uuid",
  "source_chat_id": 7,
  "source_message_id": 155,
  "target_chat_id": 12,
  "target_tg_chat_id": -1004338873193,
  "message_id": 170,
  "text": "подпись или текст",
  "attachment_path": "C:\\...\\storage\\media\\chats\\7\\155\\photo.jpg",
  "attachment_kind": "PHOTO",
  "reply_to_tg_msg_id": null
}
```
Воркер:
- `attachment_path` есть → `client.send_file(target, path, caption=text)`
- иначе → `client.send_message(target, text)`
- **никогда** не использует `forward_messages`
- публикует ACK в `tg_outgoing_ack` как обычно (`cmd_id`, `message_id`, `tg_msg_id`, `ok`)

### 1.4 Сохранение в БД
В целевом чате создаётся **новое** `GroupMessage`:
- `sender_type = 'OPERATOR'`, `sender_operator_id` = кто переслал
- `text` = `comment` + `\n` + исходная подпись (или просто исходный текст)
- `forwarded_from` = **`NULL`** — имя исходного отправителя не должно утекать
- вложения копируются как новые `MediaAttachment` со своими файлами (копия на диске, чтобы удаление исходного сообщения не сломало пересланное)

---

## 2. Редактирование сообщений

### 2.1 Асимметрия — главное требование
| Источник правки | Что происходит |
|---|---|
| **Из Telegram** (`tg_edits`) | Текст в CRM обновляется, но **старая версия сохраняется** в истории правок. Причина: клиент может злоупотребить, подменив текст. Мы должны видеть, что было написано изначально. |
| **Из CRM** (оператор) | Текст обновляется в CRM **и** в Telegram. История правок **не ведётся** — это наша собственная правка. |

### 2.2 Таблица `message_edits`
```sql
CREATE TABLE message_edits (
  id            INTEGER PRIMARY KEY,
  message_id    BIGINT NOT NULL REFERENCES group_messages(id) ON DELETE CASCADE,
  source        VARCHAR(16) NOT NULL,   -- 'TELEGRAM' | 'CRM'
  old_text      TEXT NOT NULL,
  new_text      TEXT NOT NULL,
  operator_id   INTEGER NULL REFERENCES operators(id) ON DELETE SET NULL,
  actor_name    VARCHAR(255) NULL,      -- имя из TG или оператора
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_message_edits_message ON message_edits(message_id, created_at);
```
Заполняется **только** для `source = 'TELEGRAM'`. Для `source='CRM'` запись не создаётся.

### 2.3 REST
```
PATCH /api/messages/{message_id}
Body: {"text": "новый текст"}
→ 200 {message: MessageOut, edited_in_telegram: true}
```
- Правит только свои сообщения? **Нет** — любой оператор может править, если у него есть `can_delete_messages` или он автор. Иначе `403`.
- Если сообщение пришло из TG (`sender_type IN ('CLIENT','BOT')`) — правка из CRM **всё равно разрешена** (это наш ответ клиенту).
- После правки в Telegram уходит команда в `tg_outgoing` с `edit_tg_msg_id`, воркер вызывает `client.edit_message(...)`.

```
GET /api/messages/{message_id}/edits
→ {"items": [MessageEditOut]}
```
`MessageEditOut`: `{id, message_id, source, old_text, new_text, operator_id, actor_name, created_at}`
Доступно всем операторам (прозрачность — цель функции).

### 2.4 Redis (backend → worker)
Расширение `tg_outgoing` — новое **необязательное** поле:
```json
{"edit_tg_msg_id": 90210, "text": "новый текст", ...}
```
Воркер: если `edit_tg_msg_id` задан → `client.edit_message(entity, tg_msg_id, text)`, вместо `send_message`.
ACK тот же, `tg_msg_id` = отредактированный id.

### 2.5 Redis (worker → backend) — расширение `tg_edits`
Помимо `text` теперь может присылать:
```json
{"kind":"MESSAGE_EDITED","tg_chat_id":-100…,"tg_msg_id":90210,"text":"…",
 "edited_at":"…","actor_name":"Jane Doe","actor_username":"jane"}
```
`actor_name` — запасной вариант, если объект отправителя недоступен.

### 2.6 WS-события
| Тип | Данные |
|---|---|
| `MESSAGE_EDITED` | `{message_id, chat_id, text, edited_at, source, edit}` где `edit` — `MessageEditOut` для TG-правок, иначе `null` |
| `MESSAGE_FORWARDED` | `{source_message_id, source_chat_id, message, chat}` — чтобы UI обновил оба чата |

---

## 3. Фронтенд

### 3.1 Компоненты
| Файл | Назначение |
|---|---|
| `components/ForwardDialog.tsx` | Модалка выбора целевого чата: поиск, список чатов, опциональный комментарий, кнопка «Переслать». Предупреждение: «Будет отправлено от имени нашего аккаунта — имя исходного отправителя скрыто». |
| `components/EditHistory.tsx` | Раскрывающийся блок под сообщением: «Изменено из Telegram» + список версий (было → стало, кто, когда). Только для `source='TELEGRAM'`. |
| `components/MessageBubble.tsx` | Кнопки в меню сообщения: «Переслать» (`Forward`), «Изменить» (`Pencil`). Бейдж «изменено» + `EditHistory`. |
| `components/MessageInput.tsx` | Режим редактирования: баннер «Редактирование сообщения», Esc = отмена, Enter = сохранить. |

### 3.2 Store
```ts
startEditing(message: Message): void
cancelEditing(): void
saveEdit(text: string): Promise<void>     // PATCH + локальное обновление
openForwardDialog(message: Message): void
forwardMessage(targetChatId: number, comment?: string): Promise<void>
loadEditHistory(messageId: number): Promise<void>
```
Состояние: `editingMessageId`, `forwardSource: Message | null`, `editHistory: Record<number, MessageEdit[]>`.

---

## 4. Владение файлами (агенты)

| Агент | Файлы |
|---|---|
| B1 (модель+миграция) | `backend/app/models.py` (MessageEdit), `backend/app/enums.py` (EditSource), `backend/migrate_v2.py` |
| B2 (сервис правок) | `backend/app/services/edits.py` (НОВЫЙ), правка `backend/app/services/tg_ingest.py::handle_edit` |
| B3 (сервис пересылки) | `backend/app/services/forwarding.py` (НОВЫЙ) |
| B4 (роутеры) | `backend/app/routers/messages.py` (PATCH + forward + edits), `backend/app/schemas.py` |
| B5 (воркер) | `telethon_worker/worker.py` (`tg_forward`, `edit_message`) |
| B6 (UI пересылки) | `frontend/src/components/ForwardDialog.tsx` |
| B7 (UI истории) | `frontend/src/components/EditHistory.tsx` |
| B8 (UI интеграция) | `frontend/src/components/MessageBubble.tsx`, `MessageInput.tsx`, `ChatWindow.tsx`, `store/useCRMStore.ts`, `lib/api.ts`, `lib/types.ts` |

---

## 5. Инварианты (не нарушать)
1. `forwarded_from` в пересланном сообщении — всегда `NULL`.
2. Воркер **никогда** не вызывает `forward_messages` для этой функции.
3. `message_edits` пишется **только** для `source='TELEGRAM'`.
4. Правка из CRM снимает `delivery_status` → снова `pending`, пока не придёт ACK.
5. Удалённое в Telegram сообщение (`is_deleted_in_tg=true`) не редактируется — `409`.
6. Пересылка копирует файлы физически; общий файл между двумя сообщениями запрещён.
