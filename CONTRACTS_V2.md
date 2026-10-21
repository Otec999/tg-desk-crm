# CONTRACTS v2 — расширение TG-Desk CRM

> **Замораживается до начала параллельной разработки.**
> Ни один агент не меняет сигнатуры из этого документа без правки самого документа.

## 0. Инварианты (не нарушать)

- Все тексты интерфейса — **на русском**.
- Никаких `TODO`, `...`, `pass` в бизнес-логике. Всё пишется целиком.
- Каждый новый модуль покрывается тестами в `backend/tests/`.
- Тесты **идемпотентны**: повторный запуск не падает.
- Существующие 86 тестов, `verify_e2e.py` (28), `verify_worker_bridge.py` (27) обязаны остаться зелёными.
- Порт API 8000, фронт 5173, Postgres 5433, Redis 6380.
- Стиль: Tailwind, палитра `tg-bg #0f0f0f`, `tg-panel #181818`, `tg-elevated #212121`, акцент `#8774e1`.

## 1. Новые enum-значения (`app/enums.py`)

```python
class OperatorRole(_StrEnum):
    OPERATOR = "OPERATOR"
    SUPERVISOR = "SUPERVISOR"
    ADMIN = "ADMIN"          # NEW — полный доступ, отдельная форма входа

class MessageKind(_StrEnum):  # NEW
    TEXT = "TEXT"
    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"
    VOICE = "VOICE"
    VIDEO = "VIDEO"
    STICKER = "STICKER"
    AUDIO = "AUDIO"
    OTHER = "OTHER"

class ReactionEmoji(_StrEnum):  # NEW — ровно 6
    THUMBS_UP = "👍"
    HEART = "❤️"
    FIRE = "🔥"
    EYES = "👀"
    CLAP = "👏"
    PRAY = "🙏"
```

## 2. Схема БД — новые таблицы

### 2.1 `media_attachments` (NEW)
| колонка | тип | примечания |
|---|---|---|
| id | Integer PK | |
| message_id | FK group_messages.id, CASCADE | |
| kind | String(16) | `MessageKind`, CHECK |
| file_name | String(255) NULL | |
| mime_type | String(128) NULL | |
| size_bytes | BigInteger NULL | |
| width / height | Integer NULL | только изображения |
| duration_seconds | Integer NULL | голос/видео |
| storage_path | String(512) | путь ОТНОСИТЕЛЬНО `settings.media_root` |
| tg_file_id | String(255) NULL | для повторной загрузки |
| thumbnail_path | String(512) NULL | превью |
| created_at | TIMESTAMPTZ | `server_default=now()` |

Уникальность: `UNIQUE(message_id, storage_path)`.

### 2.2 `message_reactions` (NEW)
| колонка | тип | примечания |
|---|---|---|
| id | Integer PK | |
| message_id | FK group_messages.id, CASCADE | |
| operator_id | FK operators.id NULL | NULL если реакция пришла из Telegram |
| emoji | String(16) | CHECK по 6 значениям |
| tg_user_id | BigInteger NULL | кто поставил в TG |
| source | String(16) | `CRM` \| `TELEGRAM`, CHECK |
| created_at | TIMESTAMPTZ | |

Уникальность: `UNIQUE(message_id, operator_id, emoji)` где `operator_id NOT NULL`;
`UNIQUE(message_id, tg_user_id, emoji)` где `tg_user_id NOT NULL`.

### 2.3 Расширение `operators`
Добавить:
- `display_name` — уже есть, используется как «имя оператора» в админке.
- `is_active` Boolean NOT NULL DEFAULT true — блокировка учётки без удаления.
- `max_chats` Integer NULL — лимит одновременных чатов (ограничение).
- `can_delete_messages` Boolean NOT NULL DEFAULT false.
- `can_export` Boolean NOT NULL DEFAULT false.
- `avatar_color` String(7) NULL — HEX для аватара.

### 2.4 Расширение `group_messages`
- `kind` String(16) NOT NULL DEFAULT `TEXT` (CHECK).
- `caption` Text NULL — подпись к медиа.
- `forwarded_from` String(255) NULL.

### 2.5 `telegram_accounts` (NEW) — мультиаккаунт
| колонка | тип |
|---|---|
| id | Integer PK |
| label | String(64) — «Основной аккаунт» |
| tg_user_id | BigInteger NULL |
| tg_username | String(64) NULL |
| phone | String(32) NULL |
| session_string | Text NULL — **шифруется** |
| api_id | Integer NULL |
| api_hash | String(64) NULL — **шифруется** |
| is_active | Boolean NOT NULL DEFAULT true |
| created_at / updated_at | TIMESTAMPTZ |

## 3. REST API — новые эндпоинты

### 3.1 Медиа
| метод | путь | тело / ответ |
|---|---|---|
| GET | `/api/messages/{id}/attachments` | `{items: AttachmentOut[]}` |
| POST | `/api/chats/{chat_id}/attachments` | multipart `file` → `201 MessageOut` (оператор отправляет файл) |
| GET | `/api/media/{attachment_id}` | бинарный поток, `Content-Type` из записи, поддержка `Range` |

`AttachmentOut`:
```json
{"id":1,"message_id":93,"kind":"PHOTO","file_name":"receipt.jpg","mime_type":"image/jpeg",
 "size_bytes":48211,"width":800,"height":600,"duration_seconds":null,
 "url":"/api/media/1","thumbnail_url":"/api/media/1?variant=thumb"}
```

### 3.2 Реакции
| метод | путь | тело |
|---|---|---|
| PUT | `/api/messages/{id}/reaction` | `{"emoji":"👍"}` → поставить/сменить |
| DELETE | `/api/messages/{id}/reaction` | снять свою |
| GET | `/api/messages/{id}/reactions` | `{items:[{emoji,count,me:bool,operators:[]}]}` |

Правило: один оператор — **одна** реакция на сообщение. Повторный `PUT` с другим эмодзи заменяет.

### 3.3 Админка (требует роль `ADMIN` или `SUPERVISOR` где указано)
| метод | путь | роль | тело |
|---|---|---|---|
| POST | `/api/admin/login` | — | `{username,password}` → отдельный токен с `scope:"admin"` |
| GET | `/api/admin/operators` | ADMIN/SUPERVISOR | список |
| POST | `/api/admin/operators` | ADMIN | `{username,display_name,password,role,...}` |
| PATCH | `/api/admin/operators/{id}` | ADMIN | частичное обновление |
| DELETE | `/api/admin/operators/{id}` | ADMIN | мягкое (`is_active=false`) |
| POST | `/api/admin/operators/{id}/reset-password` | ADMIN | `{new_password}` |
| GET | `/api/admin/accounts` | ADMIN | список Telegram-аккаунтов (session замаскирован) |
| POST | `/api/admin/accounts` | ADMIN | создать |
| PATCH | `/api/admin/accounts/{id}` | ADMIN | изменить |
| DELETE | `/api/admin/accounts/{id}` | ADMIN | удалить |
| GET | `/api/admin/audit` | ADMIN | журнал действий |

**Ограничения:** `SUPERVISOR` видит список операторов и статистику, но **не может** менять роли, удалять учётки и трогать аккаунты.

### 3.4 Боты
Бот — обычный участник с `sender_type = "BOT"`.
- Дополнительное поле в `GroupMessage`: ничего нового, `sender_username` уже есть.
- Ответ боту: обычный `POST /api/chats/{id}/messages`, если текст содержит `@username`, воркер отправит как есть.
- В UI бот помечается бейджем 🤖 и своим цветом.
- **Не** ингестить `sender_type=BOT` в SLA (боты не считаются клиентами, SLA не открывают).

## 4. WebSocket — новые события

Формат конверта прежний: `{"type","ts","data"}`.

| тип | data |
|---|---|
| `MESSAGE_REACTION` | `{message_id, chat_id, emoji, count, operator_id?, tg_user_id?}` |
| `MESSAGE_REACTION_REMOVED` | `{message_id, chat_id, emoji, operator_id?, tg_user_id?}` |
| `MESSAGE_MEDIA` | `{message_id, chat_id, attachments:[AttachmentOut]}` |
| `ADMIN_OPERATOR_CHANGED` | `{operator_id, action:"created"\|"updated"\|"deleted"}` |

Redis-каналы: добавить `tg_reactions` (воркер → backend).

## 5. Воркер (Telethon)

- При `NewMessage` с медиа: скачать в `MEDIA_ROOT/chats/{chat_id}/{message_id}/`, создать запись `media_attachments`, опубликовать `tg_incoming` с полем `media` (список).
- При `MessageReactionUpdated`/`Raw` с реакциями: публиковать в `tg_reactions`.
- Отправка исходящего файла: команда `tg_outgoing` получает `attachment_path`, воркер делает `send_file`.
- Ответ боту: ничего особенного, обычная отправка.
- **Мониторить только** чаты из `TG_MONITORED_CHAT_IDS` (если пусто — все).

## 6. Фронтенд — новые компоненты

| файл | назначение |
|---|---|
| `components/MediaBubble.tsx` | рендер фото/файла/голоса в пузыре |
| `components/ImageLightbox.tsx` | полноэкранный просмотр |
| `components/ReactionBar.tsx` | 6 эмодзи + счётчики |
| `components/FilePicker.tsx` | прикрепление файла (drag&drop + кнопка) |
| `pages/AdminPage.tsx` | админка: вкладки Операторы / Аккаунты / Аудит |
| `pages/AdminLoginPage.tsx` | отдельная форма входа для админа |
| `store/useAdminStore.ts` | состояние админки |

Роутинг: без библиотеки — простой switch по `window.location.pathname`.
`/admin` → вход админа, `/admin/panel` → панель.

## 7. Настройки (`app/config.py`)

Добавить:
- `media_root: Path = Path("storage/media")`
- `max_upload_bytes: int = 25 * 1024 * 1024`
- `admin_session_ttl_minutes: int = 480`
- `secret_box_key`: ключ для шифрования session_string (Fernet). Если не задан — выводится из `BACKEND_SECRET_KEY` через SHA-256.

## 8. Порядок работы агентов (без конфликтов)

Каждый агент трогает **только свои файлы**. Общие файлы (`models.py`, `enums.py`, `schemas.py`, `main.py`, `config.py`) правит **только** агент, указанный как владелец, остальные присылают патчи через отчёт.

| # | Агент | Владеет |
|---|---|---|
| A1 | Модели и enum | `enums.py`, `models.py`, миграция, `seed.py` |
| A2 | Медиа-бэкенд | `services/media.py`, `routers/media.py`, `config.py` (media_root) |
| A3 | Реакции-бэкенд | `services/reactions.py`, `routers/reactions.py` |
| A4 | Админка-бэкенд | `services/admin.py`, `routers/admin.py`, `security.py` (scope) |
| A5 | Воркер | `telethon_worker/worker.py`, `telethon_worker/media.py` |
| A6 | Схемы и WS | `schemas.py`, `ws_manager.py`, `main.py` (только регистрация роутеров) |
| A7 | UI медиа | `MediaBubble.tsx`, `ImageLightbox.tsx`, `FilePicker.tsx` |
| A8 | UI реакции | `ReactionBar.tsx` |
| A9 | UI админка | `AdminPage.tsx`, `AdminLoginPage.tsx`, `useAdminStore.ts` |
| A10 | Интеграция UI | `ChatWindow.tsx`, `App.tsx`, `api.ts`, `types.ts`, `useCRMStore.ts` |
| A11 | Тесты | `backend/tests/test_media.py`, `test_reactions.py`, `test_admin.py` |
| A12 | Ревью | вычитка всего, отчёт без правок |
