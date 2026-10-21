# TG-Desk CRM — Карта кода: Удаление сообщений (A) и SLA (B)

> Разведка выполнена только чтением. **Ни один файл проекта не изменён.**
> Все ссылки вида `path:LINE` — проверены прямым чтением файла.

---

# ТЕМА A — Удаление сообщений

## A.1. Реализация удаления из Telegram (`канал tg_deleted`)

**Файл:** `backend/app/services/tg_ingest.py`
**Функция:** `handle_deleted(session_factory, broadcast, payload)` — **строка 487**

Сигнатура (`tg_ingest.py:487-491`):
```python
async def handle_deleted(
    session_factory: SessionFactory,
    broadcast: BroadcastFn | None,
    payload: dict[str, Any],
) -> None:
```

Докстринг прямо фиксирует политику (`tg_ingest.py:492`):
> `"""Record Telegram-side deletions as a soft flag — the rows are **never** deleted.`

Входной payload (`tg_ingest.py:494-496`):
```python
{"kind":"MESSAGE_DELETED","tg_chat_id":-1001234567890,"tg_msg_ids":[90210,90211]}
```

**Алгоритм по шагам:**
| Строки | Действие |
|---|---|
| `504-506` | отбраковка не-dict payload |
| `509-511` | нормализация `tg_msg_ids` через `_coerce_int`, список опциональных int |
| `512-517` | пустой `tg_msg_ids` → no-op (Telegram так присылает при полном удалении диалога) |
| `519-522` | отсутствие `tg_chat_id` → warning + return |
| `525-530` | поиск `GroupChat` по `tg_chat_id`; неизвестный чат → no-op |
| `532-545` | цикл: `_find_message_by_tg_id` → soft-flag |
| `541-542` | идемпотентность: уже удалённое (`is_deleted_in_tg and deleted_at is not None`) пропускается |
| `543-544` | `message.is_deleted_in_tg = True`; `message.deleted_at = now` |
| `549` | **один** `db.commit()` на пачку |
| `551-556` | сборка `WsMessageDeletedData.from_orm_message` → `_safe_broadcast(EVT_MESSAGE_DELETED, ...)` |

Ключевая цитата (`tg_ingest.py:543-545`):
```python
message.is_deleted_in_tg = True
message.deleted_at = now
changed.append(message)
```

**Маршрутизация канала** — `backend/app/background.py:64-70`:
```python
CHANNEL_HANDLERS: dict[str, str] = {
    CH_TG_INCOMING: "handle_incoming",
    CH_TG_DELETED: "handle_deleted",
    ...
}
```
Константа канала: `backend/app/redis_bus.py:27` → `CH_TG_DELETED: Final[str] = "tg_deleted"`.

**Вывод A.1:** реализовано как **мягкое удаление (soft-delete)**. Физического `DELETE` строк нет нигде.

---

## A.2. Модель `GroupMessage` — поля удаления

**Файл:** `backend/app/models.py`, класс `GroupMessage` (`models.py:517`), таблица `group_messages` (`models.py:520`).

| Поле | Строка | Точное определение |
|---|---|---|
| `is_deleted_in_tg` | **548-550** | `Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")` |
| `deleted_at` | **551** | `Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)` |
| `is_deleted` (property) | **684-686** | `return bool(self.is_deleted_in_tg or self.deleted_at is not None)` |

Цитата (`models.py:548-551`):
```python
is_deleted_in_tg: Mapped[bool] = mapped_column(
    Boolean, nullable=False, default=False, server_default="false"
)
deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Свойство-хелпер (`models.py:683-686`):
```python
@property
def is_deleted(self) -> bool:
    """``True`` when the message was deleted on the Telegram side."""
    return bool(self.is_deleted_in_tg or self.deleted_at is not None)
```

> **Замечание:** у пары полей **нет inline-комментария `#:`** (в отличие от соседних полей). Докстринга/комментария про «anti-delete» в модели нет — семантика описана только в `tg_ingest.py`.

**Pydantic-зеркало** — `backend/app/schemas.py:556-557`:
```python
is_deleted_in_tg: bool = False
deleted_at: UtcDatetime | None = None
```
Заполнение в `from_orm_message`: `schemas.py:641-642`.

---

## A.3. REST-эндпоинт удаления — **ОТСУТСТВУЕТ (подтверждено)**

**Файл:** `backend/app/routers/messages.py`. Роутер: `message_router = APIRouter(prefix="/messages", ...)` (`messages.py:81`), `chats_router` (`messages.py:78`).

Полный список маршрутов (получен точным поиском `@xxx_router.<method>`):

| Строка | Метод | Путь | Назначение |
|---|---|---|---|
| `296` | POST | `/chats/{chat_id}/messages` | отправка текста (`send_message`) |
| `427` | POST | `/chats/{chat_id}/attachments` | отправка вложений (`send_attachments`) |
| `747` | PATCH | `/messages/{message_id}` | **редактирование** (`edit_message`) |
| `869` | GET | `/messages/{message_id}/edits` | история правок (`get_message_edits`) |
| `911` | POST | `/messages/{message_id}/forward` | пересылка (`forward_single_message`) |

**Маршрута `DELETE /api/messages/{id}` НЕТ.** Ни одного `@message_router.delete` / `@chats_router.delete` в файле не существует.

Права на удаление тем не менее **уже заложены** — `messages.py:613-617`:
```python
§2.3: the author may always edit; anybody else needs ``can_delete_messages``
...
return bool(getattr(operator, "can_delete_messages", False)) or bool(...)
```
и `messages.py:790` — при редактировании удалённого сообщения возвращается `409`:
```python
if bool(getattr(message, "is_deleted_in_tg", False)):
```

**Вывод A.3:** эндпоинт нужно создавать с нуля. При этом `Operator.can_delete_messages` (`models.py:318-320`) — уже готовый permission-флаг, а `_operator_outranks` (`messages.py:610`) — готовый хелпер проверки прав.

---

## A.4. Обработка в воркере — **ТОЛЬКО входящая (Telegram → CRM)**

**Файл:** `telethon_worker/worker.py`

**Подписка на событие** (`worker.py:939`):
```python
client.add_event_handler(self._on_message_deleted, events.MessageDeleted())
```

**Обработчик** `_on_message_deleted` (`worker.py:1254`), публикует в Redis (`worker.py:1280-1292`):
```python
payload: dict[str, Any] = {
    "kind": KIND_MESSAGE_DELETED,
    "tg_chat_id": tg_chat_id,          # may be None — see docstring
    "tg_msg_ids": deleted_ids,
}
...
if await bus.publish(CHANNEL_DELETED, payload):
```
Важная деталь (`worker.py:1261-1268`): `tg_chat_id` намеренно nullable, бэкенд должен матчить id глобально.

**Карта каналов** (`worker.py:11-16`):
```
tg_incoming       worker -> backend   NEW_MESSAGE
tg_outgoing       backend -> worker   send request
tg_outgoing_ack   worker -> backend   delivery result
tg_deleted        worker -> backend   MESSAGE_DELETED
tg_edits          worker -> backend   MESSAGE_EDITED
tg_reactions      worker -> backend   REACTION_UPDATE
```

**Подписка воркера на команды CRM** (`worker.py:1516-1517`):
```python
await pubsub.subscribe(
    CHANNEL_OUTGOING, CHANNEL_FORWARD, CHANNEL_REACTION_OUT
```
То есть backend→worker существуют ровно **3** канала: `tg_outgoing`, `tg_forward`, `tg_reaction_out`. **Канала удаления нет.**

Диспетчер (`worker.py:1576-1582`):
```python
if channel == CHANNEL_FORWARD:
    await self._handle_forward_command(message)
elif channel == CHANNEL_REACTION_OUT:
    await self._handle_reaction_command(message)
else:
    await self._handle_outgoing_message(message)
```

**Поиск `DeleteMessages` / `delete_messages` / `client.delete_*` по всему воркеру — 0 совпадений.**
Единственный `.delete(` в файле — `worker.py:2391: await redis_client.delete(REDIS_HEARTBEAT_KEY)` (служебная очистка Redis-ключа, к Telegram отношения не имеет).

**Вывод A.4:** удаление **строго одностороннее**: Telegram → CRM. Исходящей команды «удалить в Telegram» (CRM → Telegram) **не существует ни на одном слое** (нет ни Redis-канала, ни обработчика, ни вызова Telethon API).

---

## A.5. Frontend — отображение удалённого сообщения

**Файл:** `frontend/src/components/MessageBubble.tsx`

| Строка | Код | Назначение |
|---|---|---|
| `137` | `const deleted = message.is_deleted_in_tg;` | флаг |
| `161` | `const deletedClass = deleted ? "bubble-deleted" : "";` | CSS-класс |
| `162` | `const quotedDeleted = quotedMessage?.is_deleted_in_tg ?? false;` | для цитаты |
| `207` | применяет `deletedClass` к контейнеру пузыря | |
| `215-233` | цитата-«удалено»: `border-tg-danger/70`, `text-tg-danger`, `line-through decoration-tg-danger/50` | |
| `224-226` | бейдж `<Trash2 size={8} /> deleted` | |
| `253-262` | **баннер удаления** | |
| `266` | `<div className={deleted ? "opacity-60 grayscale" : ""}>` | приглушение медиа |
| `281` | текст: `text-tg-textMuted line-through decoration-tg-danger/40` | зачёркивание |
| `315`, `346`, `351` | `{!deleted && (...)}` — скрытие реакций / forward / edit | |

Баннер (`MessageBubble.tsx:254-261`):
```tsx
{deleted && (
  <div className="mb-1 flex items-start gap-1.5 rounded-md border border-tg-danger/40 bg-tg-danger/15 px-1.5 py-1 text-[10px] font-semibold text-tg-danger">
    <Trash2 size={11} className="mt-[1px] shrink-0" />
    <span>
      Deleted in Telegram by sender at{" "}
      <span className="tabular">{formatClockSeconds(message.deleted_at)}</span>
    </span>
  </div>
)}
```

**Стиль — НЕ серый, а красно-«тревожный».** `frontend/src/index.css:130-146`:
```css
.bubble-deleted {
  background-color: rgba(229, 72, 77, 0.1);
  border: 1px solid rgba(229, 72, 77, 0.45);
  box-shadow: inset 0 0 0 1px rgba(245, 165, 36, 0.08), 0 0 18px rgba(229, 72, 77, 0.08);
}
.bubble-deleted::before {  /* диагональная штриховка 135deg */ }
```

Типы: `frontend/src/lib/types.ts:118-119` (`is_deleted_in_tg: boolean; deleted_at: string | null`).
WS-тип: `types.ts:237-240` (`WsMessageDeletedData`).

**Вывод A.5:** anti-delete визуально реализован полноценно (баннер + штриховка + зачёркивание + grayscale медиа). Если по дизайну нужен именно **серый** вариант — придётся править `.bubble-deleted` в `index.css:130`.

---

## A.6. Контракты — раздел про удаление

Отдельного раздела «Удаление сообщений» **нет ни в одном** контракте. Упоминания разрознены:

| Файл:строка | Содержание |
|---|---|
| `CONTRACTS.md:181-182` | поля в `MessageOut`: `"is_deleted_in_tg": false, "deleted_at": null` |
| `CONTRACTS.md:258` | WS: `MESSAGE_DELETED` → `{"message_id":55,"chat_id":1,"deleted_at":"...","message":<MessageOut>}` |
| `CONTRACTS.md:292` | Redis: `tg_deleted` `worker -> backend` `{"kind":"MESSAGE_DELETED","tg_chat_id":...,"tg_msg_ids":[90210,90211]}` |
| `CONTRACTS.md:304` | DDL `group_messages`: `is_deleted_in_tg bool, deleted_at` |
| `CONTRACTS.md:316` | сид: одна пара сообщений с `is_deleted_in_tg=True` |
| `CONTRACTS_V2.md:82` | `can_delete_messages Boolean NOT NULL DEFAULT false` |
| `CONTRACTS_V2.md:125` | `DELETE /api/messages/{id}/reaction` (только реакция, не сообщение) |
| `CONTRACTS_V3.md:88` | права: автор или `can_delete_messages` — про **правку** |
| `CONTRACTS_V3.md:166` | `is_deleted_in_tg=true` → редактирование запрещено, `409` |
| `CONTRACTS_V4.md:26` | `DELETE /api/messages/{id}/reaction` |

**Вывод A.6:** направление `worker -> backend` зафиксировано как «frozen». Канал/эндпоинт для обратного удаления **не описан в контрактах вообще** — при реализации контракт придётся расширять (по аналогии с `tg_reaction_out`/`tg_forward`).

---

# ТЕМА B — SLA

## B.1. `sla_engine.py` — открытие и закрытие инцидента

**Файл:** `backend/app/services/sla_engine.py` (1272 строки). `__all__` — строки `56-80`.

### Функции жизненного цикла

| Функция | Строка | Роль |
|---|---|---|
| `open_incident` | **295** | открыть/переиспользовать инцидент |
| `has_pending_incident` | **398** | `EXISTS`-проверка |
| `_pending_incident` | **377** | самый срочный PENDING чата |
| `resolve_for_reply` | **411** | закрыть по ответу оператора |
| `close_incident` | **563** | админское закрытие **без ответа** |
| `tick_incidents` | **844** | обновление всех PENDING |
| `tick` | **912** | один heartbeat SLA |
| `sla_update_payload` | **758** | payload `SLA_UPDATE` |
| `broadcast_incident_resolved` | **810** | `SLA_RESOLVED` |
| `personal_stats` | **1078** | статистика оператора |
| `supervisor_dashboard` | **1146** | дашборд SUPERVISOR |
| `assign_round_robin` | **681** | назначение |
| `sla_summary` | **1030** | объект `sla` в `ChatListItem` |
| `burning_chat_ids` | **1054** | чаты, которые «горят» |

### `open_incident` (`sla_engine.py:295-349`)

Правила (docstring `302-313`):
- сообщения оператора SLA не открывают (`315-320`);
- если в чате уже есть PENDING — он **переиспользуется**, дедлайн НЕ продлевается (`324-334`);
- иначе создаётся новый и назначается round-robin.

Расчёт дедлайна (`sla_engine.py:344-345`):
```python
now = utcnow()
deadline = now + _next_multiple_of_tick(timedelta(seconds=_sla_target_for(owner)))
```

**Разрешение целевого SLA** — `_sla_target_for` (`sla_engine.py:184-200`):
```python
"""``operators.sla_target_seconds`` wins when the incident already has an owner (a documented
per-operator override); otherwise the global ``settings.backend_sla_seconds`` applies."""
for candidate in (
    getattr(operator, "sla_target_seconds", None),
    getattr(settings, "backend_sla_seconds", None),
):
    ...
return 300
```
> ⚠️ Приоритет: **per-operator** → **глобальный env** → хардкод `300`.

### `resolve_for_reply` (`sla_engine.py:411-513`)

Приоритет матчинга (`423-430`): сначала `GroupMessage.id` (CRM), затем `tg_msg_id`, только в пределах чата. Без цитаты — закрывается **самый срочный** открытый инцидент.

Гонки защищены guarded UPDATE (`471-490`):
```python
result = await session.execute(
    update(SLAIncident)
    .where(
        SLAIncident.id == int(incident.id),
        SLAIncident.status == SlaIncidentStatus.PENDING.value,
    )
    .values(status=SlaIncidentStatus.RESOLVED.value, resolved_at=now,
            resolved_by_message_id=int(getattr(outbound_message, "id")),
            response_time_seconds=elapsed)
    .execution_options(synchronize_session=False)
)
if int(getattr(result, "rowcount", 0) or 0) == 0:
    ...return None
```

---

## B.2. Где вызывается `sla_engine.open_incident` — **КРИТИЧНО**

### ✅ Точка открытия: `backend/app/services/tg_ingest.py:368-370`
```python
had_pending = await sla_engine.has_pending_incident(db, chat.id)
incident = await sla_engine.open_incident(db, message)
opened_clock = incident is not None and not had_pending
```
Импорт: `tg_ingest.py:58` → `from app.services import sla_engine`.

Контекст: это внутри `handle_incoming`, в блоке обработки входящего сообщения — после инкремента `chat.unread_count` (`365`) и `chat.last_message_at` (`366`), до `db.commit()` (`372`).

Дальше (`tg_ingest.py:387-407`) при `opened_clock` идёт веер WS-броадкаста: `EVT_SLA_UPDATE` и `EVT_CHAT_ASSIGNED`.

### **ЭТО ЕДИНСТВЕННАЯ точка открытия инцидента в рабочем коде.**

Полный список вызовов `open_incident` по проекту:
| Файл:строка | Тип |
|---|---|
| `backend/app/services/tg_ingest.py:369` | **боевой вызов** |
| `backend/app/services/sla_engine.py:295` | определение |
| `backend/app/services/sla_engine.py:317/327/339` | строки логов внутри функции |

> ⚠️ **Важно:** `sla_engine.open_incident` **НЕ вызывается** из роутеров. В `backend/app/routers/chats.py:81` есть **другая** функция `open_incidents_for(db, chat_ids)` — это только **чтение** существующих инцидентов для сборки `ChatListItem`, не создание.

### Где вызывается `resolve_for_reply` (для полноты картины)
| Файл:строка | Контекст |
|---|---|
| `backend/app/routers/messages.py:386` | `send_message` |
| `backend/app/routers/messages.py:542` | `send_attachments` |
| `backend/app/services/forwarding.py:492` | пересылка |

---

## B.3. Админка: устройство, настройки, хранение

**Роутер:** `backend/app/routers/admin.py` — `router = APIRouter(prefix="/admin", tags=["admin"])` (`admin.py:92`).
**Префикс API:** монтируется через `main.py:216` под `/api` → итого `/api/admin/...`.

### Все маршруты `backend/app/routers/admin.py`

| Строка | Метод | Путь | Handler |
|---|---|---|---|
| `211` | POST | `/login` | `admin_login` (`222`) — отдельный admin-token, scope=admin |
| `323` | GET | `/operators` | `list_operators` (`329`) |
| `338` | POST | `/operators` | `create_operator` (`345`) |
| `356` | PATCH | `/operators/{operator_id}` | `update_operator` (`366`) |
| `378` | DELETE | `/operators/{operator_id}` | `deactivate_operator` (`388`) — мягкое `is_active=false` |
| `399` | POST | `/operators/{operator_id}/reset-password` | `reset_password` (`406`) |
| `422` | GET | `/accounts` | `list_accounts` (`428`) |
| `437` | POST | `/accounts` | `create_account` (`444`) |
| `454` | PATCH | `/accounts/{account_id}` | `update_account` (`461`) |
| `472` | DELETE | `/accounts/{account_id}` | `delete_account` (`479`) |
| `494` | GET | `/audit` | `list_audit` (`500`) |

### 🔴 Где хранятся настройки — **ответ: ТОЛЬКО в env-конфиге, через API НЕ меняются**

- Настроек SLA/рабочих часов/фича-флагов в админке **нет**.
- Нет ни одного `GET/PUT /api/admin/settings` — среди 11 маршрутов такого не существует.
- Сервис `backend/app/services/admin.py` — функции управления операторами/аккаунтами/аудитом; глобальных настроек там нет.
- Аудит пишется в таблицу `admin_audit_log` (`models.py:1233`).

### 🔴 Глобальные настройки не читаются из роутеров вообще

`get_settings()` (`config.py:226-229`) — `@functools.lru_cache(maxsize=1)` над pydantic `Settings`, читает `.env` (`config.py:45-52`). В `routers/admin.py` **не импортируется**. Значение закэшировано → изменение `.env` требует рестарта процесса.

### 🔴 `sla_target_seconds` — «тройной разрыв»: поле выпало из трёх слоёв

**Слой 1 — не отдаётся в ответе.** `backend/app/schemas.py:1229-1243` (`OperatorAdminOut`):
```python
class OperatorAdminOut(_ORMModel):
    """Admin view of an operator record (CONTRACTS_V2 §3.3)."""
    id: int
    username: str
    display_name: str = ""
    role: str = OperatorRole.OPERATOR.value
    shift_status: str = ShiftStatus.OFFLINE.value
    is_active: bool = True
    max_chats: int | None = None
    can_delete_messages: bool = False
    can_export: bool = False
    avatar_color: str | None = None
```
`sla_target_seconds` отсутствует.

**Слой 2 — не принимается в запросе.** `schemas.py:1281-1291` (`OperatorUpdateRequest`) — поля нет, а `_StrictModel` отвергает неизвестные поля.

**Слой 3 — не применяется в сервисе.** `backend/app/services/admin.py:121-130`:
```python
#: ``Operator`` attributes an administrator may change through ``PATCH``.
_OPERATOR_EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "display_name",
    "role",
    "is_active",
    "max_chats",
    "can_delete_messages",
    "can_export",
    "avatar_color",
)
```
`sla_target_seconds` в списке **отсутствует**.

Схема тоже не принимает это поле — `backend/app/schemas.py:1281-1291` (`OperatorUpdateRequest`): `display_name, password, role, is_active, max_chats, can_delete_messages, can_export, avatar_color`. Плюс `_StrictModel` (лишние поля → ошибка валидации).

**Слой 4 — нет и в TS-типе.** `frontend/src/store/useAdminStore.ts:30-41` (`AdminOperator`) — 9 полей, SLA нет.

**Вывод B.3:** SLA-настройки сейчас **полностью статичны**: только `.env` в момент старта + seed. Никакого runtime-управления нет ни в API, ни в UI. Per-operator override **недостижим через `/api/admin/*` ни при каких условиях** — читается только через desk-эндпоинты (`shift.py:62`, `sla_engine.py:1191,1220,1246`).

### Авторизация админки (для реализации новых роутов)

- В сервисном слое `services/admin.py` авторизация **не проверяется** — только в HTTP через `Depends`. Трёхступенчатая модель `security.py:528-589`:
  - `require_admin` (`security.py:560`) — 401 без admin-scope токена, 403 для не-ADMIN;
  - `require_privileged` (`security.py:578`) — ADMIN или SUPERVISOR (только чтение);
  - `security.py:534` — перечитывание строки из БД → «revocation instant».
- Аудит: `record_audit` (`services/admin.py:369`) — **единственная** точка записи `admin_audit_log` (`models.py:1219`, `AuditEntry`). Транзакционно (в той же транзакции, что и мутация). Вызовы: `admin.py:280` (login) + `services/admin.py:557, 627, 669, 706, 827, 912, 950`.
- Гвард «последний админ»: `_lock_active_admins` (`admin.py:449-473`, `FOR UPDATE`, `ORDER BY id + 0` против дедлока) + `_guard_last_active_admin` (`admin.py:476-505`, 409 `"Нельзя изменить последнего администратора"`).

---

## B.4. `Operator.sla_target_seconds` и глобальный SLA

| Сущность | Файл:строка | Определение |
|---|---|---|
| `Operator.sla_target_seconds` | **`models.py:307-309`** | `Mapped[int] = mapped_column(Integer, nullable=False, default=300, server_default="300")` |
| CHECK-констрейнт | `models.py:364` | `CheckConstraint("sla_target_seconds > 0", name="sla_target_positive")` |
| Фолбэк-свойство | `models.py:430` | `return self.sla_target_seconds or 300` |
| Глобальный `backend_sla_seconds` | **`config.py:68`** | `backend_sla_seconds: int = 300` (секция `# ---- Backend`, pydantic-settings, `env_prefix=""`) |
| Геттер настроек | `config.py:227` | `def get_settings() -> Settings:` |
| Разрешение приоритета | `sla_engine.py:184-200` | `_sla_target_for()` |

Использование глобального значения:
- `backend/app/main.py:107` — в инициализации
- `backend/app/main.py:341` — фолбэк при выдаче оператора
- `backend/app/background.py:206` — фолбэк в снапшоте
- `backend/app/routers/_provisioning.py:88` — `sla_target_seconds=int(getattr(settings, "backend_sla_seconds", DEFAULT_SLA_TARGET_SECONDS))`
- `backend/app/routers/shift.py:62` — фолбэк в статистике
- `backend/app/routers/_common.py:187` — `int(obj.sla_target_seconds or 0)`
- `schemas.py:175, 215` — дефолты `300`

**Вывод B.4:** оба уровня есть, но глобальный — **только read-only из env**.

---

## B.5. Таблица настроек в БД — **ОТСУТСТВУЕТ**

Все `__tablename__` в `backend/app/models.py` (полный список, 10 таблиц):

| Строка | Таблица |
|---|---|
| `285` | `operators` |
| `447` | `group_chats` |
| `520` | `group_messages` |
| `729` | `sla_incidents` |
| `845` | `tx_lookup_audit` |
| `916` | `media_attachments` |
| `1039` | `message_reactions` |
| `1155` | `telegram_accounts` |
| `1233` | `admin_audit_log` |
| `1316` | `message_edits` |

**Таблиц `settings`, `app_settings`, `config`, `system_settings`, `global_settings` — НЕТ.**
Поиск по `app_settings|SettingsOut|SettingsUpdate|def .*_settings` дал единственное совпадение — `config.py:227 def get_settings()` (env, не БД).

**Вывод B.5:** хранилище глобальных настроек нужно создавать с нуля (модель + миграция + сервис + API).

---

## B.6. Фоновый `tick` — **ЕСТЬ, работает**

**Файл:** `backend/app/background.py`

| Элемент | Строка | Код |
|---|---|---|
| Имя задачи | **`59`** | `TASK_SLA: str = "sla_ticker"` |
| Реестр задач | `61` | `TASK_NAMES = (TASK_HEARTBEAT, TASK_SLA, TASK_REDIS)` |
| Определение | **`405`** | `async def sla_ticker(interval: float | None = None)` |
| Вызов движка | **`420`** | `await _call_sla_tick(sla_engine.tick, ctx)` |
| Броадкаст | `422-423` | `for frame in await sla_snapshot(ctx): await ctx.broadcast(envelope(EVT_SLA_UPDATE, frame))` |
| Супервизор-цикл | `427` | `await _supervised_loop(TASK_SLA, _body, ...)` |
| **Регистрация в старте** | **`531`** | `_register_task(app, TASK_SLA, sla_ticker(settings.backend_ws_tick_seconds))` |

`start_all` (`background.py:517`), список задач (`529-533`):
```python
return [
    _register_task(app, TASK_HEARTBEAT, heartbeat_sweeper(HEARTBEAT_INTERVAL_SECONDS)),
    _register_task(app, TASK_SLA, sla_ticker(settings.backend_ws_tick_seconds)),
    _register_task(app, TASK_REDIS, redis_consumer(factory, manager.broadcast)),
]
```
Остановка — `stop_all` (`background.py:536`). Период тика = `backend_ws_tick_seconds = 2` (`config.py:70`), минимум `0.25` (`background.py:408`).

Диагностика живости задачи: `backend/app/routers/internal.py:33` (`SLA_TICKER_TASK`) и `internal.py:82` (проверка «тикер жив»).

**Вывод B.6:** тикер существует, зарегистрирован и работает. Логика `sla_engine.tick` (`sla_engine.py:912`) — escalate + (re)assign + broadcast.

---

## B.7. Frontend: админ-панель и роут `/admin/panel`

**Роутинг — `frontend/src/App.tsx`** (ручной, без react-router):
- `35` → `const ADMIN_LOGIN_PATH = "/admin";`
- `36` → `const ADMIN_PANEL_PATH = "/admin/panel";`
- `69` → `export default function App()`; `71` → `const route = resolveRoute(pathname);`
- `75-82` → `route === "admin-login"` → `<AdminLoginPage />`
- `84-91` → `route === "admin-panel"` → `<AdminPage />`
- `93` → иначе `<CrmApp />`

Комментарий (`App.tsx:73-74`): админка — **отдельная сессия**, не поднимает операторский store, WS и SLA-тикер.

**Страницы:**
| Файл | Строка | Элемент |
|---|---|---|
| `frontend/src/pages/AdminLoginPage.tsx` | `15` | `const PANEL_PATH = "/admin/panel";` |
| `frontend/src/pages/AdminPage.tsx` | `75` | `const LOGIN_PATH = "/admin";` |
| `frontend/src/pages/AdminPage.tsx` | `1480` | `export default function AdminPage(): JSX.Element` |

**Вкладки админки — `AdminPage.tsx:78-82`:**
```tsx
const TABS: ReadonlyArray<{ key: TabKey; label: string; icon: LucideIcon }> = [
  { key: "operators", label: "Операторы", icon: Users },
  { key: "accounts", label: "Telegram-аккаунты", icon: Database },
  { key: "audit", label: "Журнал действий", icon: History },
];
```
Состояние: `1510` → `useState<TabKey>("operators")`; рендер вкладок `1704`/`1719`/`1733`.
Тип: `AdminPage.tsx:73` → `type TabKey = "operators" | "accounts" | "audit";`

**🔴 Вкладки «Настройки» / «SLA» НЕТ.** Grep `sla_target|slaTarget|settings|config|настройк` по `AdminPage.tsx` (1762 строки) и по `useAdminStore.ts` (1067 строк) → **0 совпадений**.

**Per-operator SLA в UI тоже не редактируется.** `OperatorFormState` (`AdminPage.tsx:440-449`) — 9 полей, SLA нет:
```tsx
interface OperatorFormState {
  username: string;
  display_name: string;
  password: string;
  role: AdminRole;
  max_chats: string;
  can_delete_messages: boolean;
  can_export: boolean;
  is_active: boolean;
}
```
Единственное числовое поле — «Лимит чатов» (`AdminPage.tsx:690-698`) с `inputMode="numeric"` и `parseOptionalInt` (`:158-163`). **Это готовый образец для будущего SLA-инпута**, как и `validateOperatorCreate`/`validateOperatorUpdate` (`:546`, `:568`).

### Ключевые компоненты панели (для добавления вкладки)

| Строки | Компонент | Роль |
|---|---|---|
| `172-194` | `Field` | label + input + hint/error |
| `199-204` | `TextInput` | `forwardRef` |
| `235-264` | `Toggle` | switch |
| `266-277` | `RoleBadge` | бейдж роли |
| `336-391` | `Modal` | модалка (Esc, клик по фону) |
| `393-415` / `417-436` | `PrimaryButton` / `GhostButton` | кнопки |
| `483-784` | `OperatorModal` | форма оператора |
| `786-961` | `OperatorsTab` | вкладка «Операторы» (таблица) |
| `999-1228` | `AccountModal` | форма аккаунта |
| `1318-1390` | `AccountsTab` | вкладка «Аккаунты» (карточки) |
| `1422-1473` | `AuditTab` | вкладка «Журнал» |
| `1480-1762` | `AdminPage` | корневой компонент |

Стор `useAdminStore.ts`: единый `http<T>` хелпер (`:233-262`, AbortController 20s, `Bearer`, `credentials:"omit"`), дедупликатор `once()` (`:598`), ключи `tgdesk.admin_token`/`tgdesk.admin_operator` (`:124-125`).

### ⚠️ Дефекты, релевантные доработке
| Файл:строка | Проблема |
|---|---|
| `AdminPage.tsx:1514` | `role.toUpperCase() !== "ADMIN"` → любая будущая роль считается супервизором |
| `AdminPage.tsx:1696-1701` + `1705-1716` | баннер «Режим супервизора» только информирует; `OperatorsTab` не получает `disabled` → SUPERVISOR видит кликабельные кнопки мутаций, отказ лишь 403 с сервера |
| `AdminPage.tsx:111-112` | `AUDIT_ACTION_LABEL` ждёт `admin_login_failed`/`admin_logout`, но бэкенд их **не пишет** |

---

# ИТОГОВЫЙ СВОД: что есть / чего нет

## ✅ УЖЕ ЕСТЬ

**Удаление (A):**
- `handle_deleted` soft-delete с идемпотентностью (`tg_ingest.py:487`)
- Поля `is_deleted_in_tg` / `deleted_at` + property `is_deleted` (`models.py:548-551`, `684-686`)
- Схема `MessageOut` (`schemas.py:556-557`)
- Входящий обработчик воркера (`worker.py:1254`) + подписка на `events.MessageDeleted` (`worker.py:939`)
- Redis-канал `tg_deleted` (`redis_bus.py:27`, `background.py:66`)
- WS-событие `MESSAGE_DELETED` (`CONTRACTS.md:258`)
- Полный anti-delete UI: баннер, штриховка, зачёркивание, grayscale, скрытие действий (`MessageBubble.tsx:137-351`, `index.css:130-146`)
- Permission-флаг `Operator.can_delete_messages` (`models.py:318-320`)
- Запрет правки удалённого → `409` (`messages.py:790`, `edits.py:395`)

**SLA (B):**
- Полноценный `sla_engine.py`: `open_incident`, `resolve_for_reply`, `close_incident`, `tick`, `assign_round_robin`, `personal_stats`, `supervisor_dashboard`
- `Operator.sla_target_seconds` + CHECK > 0 (`models.py:307-309`, `364`)
- Глобальный `backend_sla_seconds` (`config.py:68`)
- Приоритетное разрешение `_sla_target_for` (`sla_engine.py:184-200`)
- Работающий `sla_ticker` (`background.py:405`, зарегистрирован на `531`)
- Админ-API из 11 маршрутов + `admin_audit_log`
- Админ-UI с 3 вкладками (`AdminPage.tsx:78-82`)

## ❌ ЧЕГО НЕТ (создавать с нуля)

### Тема A — удаление «из CRM в Telegram»
1. **Redis-канал `tg_delete_out`** (backend → worker) — по аналогии с `tg_reaction_out`/`tg_forward`. В `worker.py:1516-1517` нужно добавить в `subscribe`.
2. **Обработчик в воркере** `_handle_delete_command` + вызов Telethon `client.delete_messages(entity, ids, revoke=...)`. Сейчас в воркере **0** вызовов удаления.
3. **REST-эндпоинт** `DELETE /api/messages/{message_id}` в `backend/app/routers/messages.py` (5 маршрутов, ни одного DELETE). Нужна проверка прав: автор / `can_delete_messages` (`_operator_outranks`, `messages.py:610`).
4. **Pydantic-схемы** запроса/ответа удаления; **сервис** (предпочтительно `backend/app/services/deletes.py` по образцу `edits.py`).
5. **ACK-канал** `tg_delete_ack` + обработчик в `CHANNEL_HANDLERS` (`background.py:64-70`).
6. **Поле аудита удаления** — например `deleted_in_tg_at` / `deleted_by_operator_id` / `is_deleted_by_crm`; сейчас в `models.py` такого разделения нет (только «удалено в Telegram»).
7. **Миграция** БД для новых полей (образец — `backend/migrate_v2.py`).
8. **Расширение контрактов** — раздел «Удаление сообщений» отсутствует во всех 4 файлах CONTRACTS.
9. **Frontend:** кнопка «Удалить» в `MessageBubble.tsx` (сейчас только `{!deleted && ...}` для forward/edit), метод в `frontend/src/lib/api.ts`, тип в `types.ts`.

### Тема B — настраиваемый SLA из админки
1. **Таблица настроек в БД** — `app_settings` (key/value или типизированные колонки). В `models.py` **нет ни одной** таблицы настроек.
2. **Модель + миграция** для неё.
3. **Сервис настроек** (например `backend/app/services/settings.py`) с кэшем и приоритетом БД → env → дефолт.
4. **Переработка `_sla_target_for`** (`sla_engine.py:184-200`) — сейчас читает только ORM-объект + env; нужен источник из БД. Учесть, что функция **синхронная** — потребуется либо предзагрузка значения в `preload`, либо перевод на async.
5. **Эндпоинты** `GET /api/admin/settings` + `PATCH /api/admin/settings` (в `admin.py` — 11 маршрутов, ни одного).
6. **`sla_target_seconds` в `_OPERATOR_EDITABLE_FIELDS`** (`services/admin.py:122-130`) + в `OperatorUpdateRequest` (`schemas.py:1281-1291`) — иначе per-operator override недостижим через API.
7. **Вкладка «Настройки» в админ-UI** — `TABS` (`AdminPage.tsx:78-82`) содержит только 3 вкладки; нужен новый `TabKey` + компонент формы + методы в `useAdminStore.ts` (там **0** упоминаний settings).
8. **Применение на лету** — сейчас `settings` читается из env при старте; изменение дедлайна требует, чтобы `open_incident`/`tick` брали актуальное значение (инвалидация кэша).
9. **Аудит изменений настроек** в `admin_audit_log` (`models.py:1233`).
10. **Синхронизация `schemas.py:175,215`** — там жёстко зашит дефолт `300`.

---

## Предупреждения для разработки

- ⚠️ **`open_incident` вызывается ТОЛЬКО из `tg_ingest.py:369`.** Если требование «открывать SLA при входящем» уже покрыто — трогать роутеры не нужно. Но любое новое место открытия SLA потребует повторного вызова `broadcast` для `EVT_SLA_UPDATE`/`EVT_CHAT_ASSIGNED` (`tg_ingest.py:387-407`).
- ⚠️ **Идемпотентность `handle_deleted`**: условие `tg_ingest.py:541` требует `deleted_at is not None`. При добавлении CRM-удаления нельзя ставить `deleted_at` без `is_deleted_in_tg` — иначе повторная обработка `tg_deleted` перезапишет метку.
- ⚠️ **Стиль удалённого — красный, не серый** (`index.css:130-134`). Если ТЗ требует серый — это правка CSS.
- ⚠️ `_sla_target_for` синхронная; при переходе на БД-настройку нельзя просто `await` внутри неё.
- ⚠️ `_StrictModel` (`schemas.py`) отвергает неизвестные поля — добавление `sla_target_seconds` в API требует правки схемы, иначе `422`.
- ⚠️ **`admin_adapters.py` — полностью мок.** `use_mock` включается автоматически, если URL не http(s) или содержит `"example"` (`admin_adapters.py:258-260`), а дефолты содержат `example` (`config.py:77,79`). При `use_mock=False` бросается `NotImplementedError` (`:582-584`, `:638-640`) — реальный HTTP-код есть только в комментарии. К удалению/SLA прямого отношения не имеет, но влияет на любые «настройки интеграций» в админке.
- ⚠️ **`adapter_registry()`** (`admin_adapters.py:659-675`) — процесс-глобальный синглтон под `threading.Lock`, строится один раз → смена `ADMIN_*_BASE_URL` требует рестарта.
- ⚠️ **Тесты фиксируют env-only поведение**: `backend/tests/test_contract_smoke.py:31` → `assert settings.backend_sla_seconds == 300`. При переносе SLA в БД этот тест и `backend/tests/test_sla_logic.py:180-189` потребуют обновления.

---

# Приложение: проверочная сводка «запрос → факт»

| Пункт ТЗ | Ответ | Доказательство |
|---|---|---|
| A.1 удаление в `tg_ingest.py` | ✅ Есть, soft-delete | `tg_ingest.py:487`, флаг `543-545` |
| A.2 `is_deleted_in_tg` / `deleted_at` | ✅ Есть | `models.py:548-551` |
| A.3 REST-удаление в `messages.py` | ❌ **НЕТ** | 5 маршрутов: `296,427,747,869,911` |
| A.4 команда удаления CRM→TG в воркере | ❌ **НЕТ**, только TG→CRM | `worker.py:1254`; subscribe `1516-1517` (3 канала); `DeleteMessages` — 0 |
| A.5 отображение в `MessageBubble` | ✅ Есть; **стиль красный, не серый** | `MessageBubble.tsx:137,161,254-262`; `index.css:130-134` |
| A.6 раздел про удаление в контрактах | ❌ Отдельного раздела нет | `CONTRACTS.md:258,292`; `V3:166` |
| B.1 `open_incident` / `resolve_for_reply` | ✅ Есть | `sla_engine.py:295`, `411` |
| B.2 где вызывается `open_incident` | ✅ **ЕДИНСТВЕННО** `tg_ingest.py:369` | `368-370` |
| B.3 админка | ✅ 11 роутов; настройки **только env** | `admin.py:92,211-500` |
| B.4 `Operator.sla_target_seconds` + `backend_sla_seconds` | ✅ Есть оба | `models.py:307-309`; `config.py:68` |
| B.5 таблица настроек в БД | ❌ **НЕТ** | 10 таблиц, `models.py` |
| B.6 фоновая задача `sla_ticker` | ✅ Есть и работает | `background.py:59,405,531` |
| B.7 роут `/admin/panel` | ✅ Есть, react-router нет | `App.tsx:36,47-52,84-91`; `AdminPage.tsx:1480` |
