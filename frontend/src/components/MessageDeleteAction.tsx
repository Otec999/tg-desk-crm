/**
 * CONTRACTS_V5 §1.3 — the delete action for one message bubble.
 *
 * Rendered by `ChatWindow` as the bubble's hover action. Two things force this to
 * be a component instead of a prop threaded from the top:
 *
 * 1. **`ChatWindow` is shared** (another agent owns it for the reaction-perf work),
 *    so this file exists to keep my footprint to two lines of JSX there — the
 *    handlers below are module-level constants, i.e. referentially stable, so
 *    `memo(MessageBubble)` keeps working (§4.3 п.1).
 * 2. **Rights** are read from the store here (`selectCanDeleteMessages`) rather
 *    than passed down: no prop has to be plumbed through a file I do not own.
 *
 * The confirmation is inline in the bubble. `window.confirm` was rejected on
 * purpose: it blocks the event loop (visible frame drop on a busy timeline, §4.1)
 * and cannot be styled. Clicking again within `CONFIRM_TIMEOUT_MS` confirms;
 * moving the mouse away, pressing Escape or the timeout all cancel.
 */
import { Check, Loader2, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import type { Message } from "../lib/types";
import { selectCanDeleteMessages, useCRMStore } from "../store/useCRMStore";

/** How long the armed state survives without a second click. */
const CONFIRM_TIMEOUT_MS = 5000;

export interface MessageDeleteActionProps {
  message: Message;
}

/**
 * `true` when this message can be deleted right now: it exists on the server
 * (`id > 0`; optimistic sends carry a negative id), it is not already deleted
 * (the server answers `409`) and the operator holds the right (§1.3 `403`).
 */
function useCanDelete(message: Message): boolean {
  const canDelete = useCRMStore(selectCanDeleteMessages);
  return canDelete && !message.is_deleted_in_tg && message.id > 0;
}

export function MessageDeleteAction({ message }: MessageDeleteActionProps): JSX.Element | null {
  const allowed = useCanDelete(message);
  /* Every hook must run before any early return: bailing out above them would
     change the hook order between renders and throw "Rendered fewer hooks than
     expected" as soon as a message became undeletable. */
  const [armed, setArmed] = useState(false);
  const [pending, setPending] = useState(false);
  const timerRef = useRef<number | null>(null);

  /* The delete request itself. Reached only through the component, never stored in
     the prop chain, so a bubble re-render cannot restart it. */
  const run = (): void => {
    if (pending) return;
    setPending(true);
    setArmed(false);
    void useCRMStore
      .getState()
      .deleteMessage(message.id)
      .finally(() => setPending(false));
  };

  const clearTimer = useCallback(() => {
    if (timerRef.current === null) return;
    window.clearTimeout(timerRef.current);
    timerRef.current = null;
  }, []);

  useEffect(() => clearTimer, [clearTimer]);

  /* A message that became undeletable (WS `MESSAGE_DELETED`, rights revoked) must
     not keep a primed confirm button around. */
  useEffect(() => {
    if (allowed) return;
    setArmed(false);
    clearTimer();
  }, [allowed, clearTimer]);

  const handleClick = (): void => {
    if (!allowed || pending) return;
    if (!armed) {
      setArmed(true);
      clearTimer();
      timerRef.current = window.setTimeout(() => {
        setArmed(false);
        timerRef.current = null;
      }, CONFIRM_TIMEOUT_MS);
      return;
    }
    clearTimer();
    run();
  };

  const handleCancel = (): void => {
    setArmed(false);
    clearTimer();
  };

  useEffect(() => {
    if (!armed) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key !== "Escape") return;
      /* Consume the key so the composer's own Escape handler does not fire too. */
      event.preventDefault();
      event.stopPropagation();
      handleCancel();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [armed]);

  if (!allowed) return null;

  if (armed) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md border border-tg-danger/50 bg-tg-danger/15 px-1 text-[10px]">
        <button
          type="button"
          onClick={handleClick}
          disabled={pending}
          className="inline-flex items-center gap-0.5 font-semibold text-tg-danger transition hover:text-white disabled:opacity-50"
          aria-label="Подтвердить удаление сообщения"
          title="Удалить сообщение из CRM и Telegram"
        >
          {pending ? <Loader2 size={10} className="animate-spin" /> : <Check size={10} />}
          Удалить
        </button>
        <button
          type="button"
          onClick={handleCancel}
          className="text-tg-textMuted transition hover:text-tg-text"
          aria-label="Отменить удаление сообщения"
          title="Отмена (Esc)"
        >
          ✕
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={pending}
      className="grid h-5 w-5 place-items-center rounded text-tg-textMuted transition hover:bg-tg-elevated hover:text-tg-danger disabled:cursor-not-allowed disabled:opacity-50"
      aria-label="Удалить сообщение"
      title="Удалить"
      aria-busy={pending}
    >
      {pending ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} />}
    </button>
  );
}

export default MessageDeleteAction;
