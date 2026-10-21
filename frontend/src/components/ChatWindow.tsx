import {
  ArrowDown,
  ArrowUp,
  Flame,
  Hash,
  MessageSquare,
  RefreshCw,
  Search,
  Trash2,
  UserCheck,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { avatarColor, formatClock, initials, parseDate, secondsUntil } from "../lib/format";
import type { Attachment, Message } from "../lib/types";
import {
  selectSelectedChat,
  selectSelectedMessages,
  useCRMStore,
} from "../store/useCRMStore";
import { ImageLightbox } from "./ImageLightbox";
import { MessageBubble, TypingLockBanner } from "./MessageBubble";
import { MessageInput } from "./MessageInput";
import { SlaBadge } from "./SlaBadge";
import { ForwardDialog } from "./ForwardDialog";

/** CONTRACTS_V2 §3.1 — full-screen viewer target (one message's attachments + cursor). */
interface LightboxState {
  attachments: Attachment[];
  index: number;
}

export function ChatWindow(): JSX.Element {
  const chat = useCRMStore(selectSelectedChat);
  const messages = useCRMStore(selectSelectedMessages);
  const operator = useCRMStore((state) => state.operator);
  const loadingMessages = useCRMStore((state) => state.loadingMessages);
  const replyTargetId = useCRMStore((state) => state.replyTargetId);
  const setReplyTarget = useCRMStore((state) => state.setReplyTarget);
  const jumpToMessage = useCRMStore((state) => state.jumpToMessage);
  const clearHighlight = useCRMStore((state) => state.clearHighlight);
  const highlightMessageId = useCRMStore((state) => state.highlightMessageId);
  const typingLocks = useCRMStore((state) => state.typingLocks);
  const lookupTx = useCRMStore((state) => state.lookupTx);
  const lookupSource = useCRMStore((state) => state.txOne.source);
  const loadMessages = useCRMStore((state) => state.loadMessages);
  const putReaction = useCRMStore((state) => state.putReaction);
  const removeReaction = useCRMStore((state) => state.removeReaction);
  /* CONTRACTS_V3 §3.1 — edit / forward intents and the forward dialog state. */
  const startEditing = useCRMStore((state) => state.startEditing);
  const openForwardDialog = useCRMStore((state) => state.openForwardDialog);
  const forwardSource = useCRMStore((state) => state.forwardSource);
  const forwardSubmitting = useCRMStore((state) => state.forwardSubmitting);
  const forwardError = useCRMStore((state) => state.forwardError);
  const closeForwardDialog = useCRMStore((state) => state.closeForwardDialog);
  const forwardMessage = useCRMStore((state) => state.forwardMessage);
  /* The dialog offers every chat, not just the filtered visible ones. */
  const chats = useCRMStore((state) => state.chats);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const [showJumpButton, setShowJumpButton] = useState(false);
  const [showTopButton, setShowTopButton] = useState(false);
  const [atBottom, setAtBottom] = useState(true);
  /* CONTRACTS_V2 §3.1 — lightbox lives here so it floats above the timeline. */
  const [lightbox, setLightbox] = useState<LightboxState | null>(null);

  /* The lightbox is scoped to the chat it was opened from: switching chats must
     not leave a viewer open over a different conversation's media. */
  useEffect(() => {
    setLightbox(null);
  }, [chat?.id]);

  const messagesById = useMemo(() => {
    const map = new Map<number, Message>();
    for (const message of messages) map.set(message.id, message);
    return map;
  }, [messages]);

  const messagesByTgId = useMemo(() => {
    const map = new Map<number, Message>();
    for (const message of messages) {
      if (message.tg_msg_id !== null) map.set(message.tg_msg_id, message);
    }
    return map;
  }, [messages]);

  const resolveQuoted = useCallback(
    (message: Message): Message | null => {
      if (message.reply_to_id !== null) {
        const byId = messagesById.get(message.reply_to_id);
        if (byId) return byId;
      }
      if (message.reply_to_tg_msg_id !== null) {
        return messagesByTgId.get(message.reply_to_tg_msg_id) ?? null;
      }
      return null;
    },
    [messagesById, messagesByTgId],
  );

  /* ------------------------------------------------------------ scrolling */

  /**
   * Scroll the timeline to its newest message.
   *
   * `bottomRef` is a zero-height sentinel rendered after the last bubble, so
   * `scrollIntoView` is exact regardless of message heights. Falls back to a direct
   * `scrollTop` assignment when the sentinel is not mounted yet (first paint), which is
   * what makes "open a chat and land at the bottom" reliable.
   */
  const scrollToBottom = useCallback((smooth = false) => {
    const sentinel = bottomRef.current;
    if (sentinel) {
      sentinel.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "end" });
      return;
    }
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, []);

  /** Scroll back to the oldest message currently loaded. */
  const scrollToTop = useCallback((smooth = false) => {
    const element = scrollRef.current;
    if (!element) return;
    if (smooth && typeof element.scrollTo === "function") {
      element.scrollTo({ top: 0, behavior: "smooth" });
    } else {
      element.scrollTop = 0;
    }
  }, []);

  const handleScroll = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    const isBottom = distance < 80;
    setAtBottom(isBottom);
    setShowJumpButton(distance > 400);
    /* Hide "up" once the timeline is already at (or near) its first message: the
       threshold tracks one viewport so a tall window still hides it at the bottom. */
    const topThreshold = Math.max(400, Math.round(element.clientHeight * 0.5));
    setShowTopButton(!isBottom && element.scrollTop > topThreshold);
    if (element.scrollTop < 40 && chat) {
      const first = messages[0];
      if (first && !loadingMessages) void loadMessages(chat.id, first.id);
    }
  }, [chat, loadMessages, loadingMessages, messages]);

  /* Re-measure whenever the timeline content changes: new messages, a media attachment
     finishing its load, or a bubble collapsing. Without this the buttons would stay in
     their previous visibility state after the content resized. */
  useEffect(() => {
    handleScroll();
  }, [handleScroll, messages.length]);

  /* Pin to the bottom when a chat opens. */
  useEffect(() => {
    if (!chat) return;
    scrollToBottom(false);
  }, [chat?.id, scrollToBottom]);

  /* Keep the view pinned to the bottom while the timeline is still laying out.
     Images and media attachments finish loading after the first paint and grow the
     content, which would otherwise leave the view a few hundred pixels short of the
     last message. A ResizeObserver on the content container re-pins on every growth
     until the operator scrolls away. */
  useEffect(() => {
    if (!chat) return;
    if (highlightMessageId !== null) return;

    let pinned = true;
    let frame = 0;

    const pin = () => {
      if (!pinned) return;
      if (frame) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        if (pinned) scrollToBottom(false);
      });
    };

    /* Re-pin immediately, then once more after the layout settles. */
    pin();
    const settle = window.setTimeout(pin, 60);

    const sentinel = bottomRef.current;
    const content = sentinel?.parentElement ?? null;
    const observer =
      typeof ResizeObserver !== "undefined" && content ? new ResizeObserver(pin) : null;
    if (observer && content) observer.observe(content);

    /* Attachment thumbnails and avatars decode *after* the DOM node for them exists,
       and a decoded image can change the layout of bubbles without resizing the outer
       container synchronously. Re-pin on every load/capture event that bubbles up from
       the media inside the timeline. */
    const onMediaSettled = () => pin();
    content?.addEventListener("load", onMediaSettled, true);
    content?.addEventListener("loadedmetadata", onMediaSettled, true);
    content?.addEventListener("error", onMediaSettled, true);

    /* The operator scrolling away lifts the pin: explicit intent wins over layout. */
    const element = scrollRef.current;
    const release = () => {
      if (!element) return;
      const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
      if (distance > 200) pinned = false;
    };
    element?.addEventListener("wheel", release, { passive: true });
    element?.addEventListener("touchmove", release, { passive: true });

    return () => {
      pinned = false;
      window.clearTimeout(settle);
      if (frame) window.cancelAnimationFrame(frame);
      observer?.disconnect();
      content?.removeEventListener("load", onMediaSettled, true);
      content?.removeEventListener("loadedmetadata", onMediaSettled, true);
      content?.removeEventListener("error", onMediaSettled, true);
      element?.removeEventListener("wheel", release);
      element?.removeEventListener("touchmove", release);
    };
  }, [chat?.id, scrollToBottom, highlightMessageId]);

  /* The timeline fills in pages: `loadMessages` appends older/newer rows and each page
     changes the scroll height. While the operator has not scrolled away, keep the view
     on the newest message so opening a chat always ends at the bottom. */
  useEffect(() => {
    if (!chat) return;
    if (highlightMessageId !== null) return;
    const element = scrollRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    /* Only correct a *small* shortfall: a large one means the operator deliberately
       scrolled up, and yanking the view would be hostile. */
    if (distance > 0 && distance <= 800) scrollToBottom(false);
  }, [chat, messages.length, loadingMessages, scrollToBottom, highlightMessageId]);

  /* follow new messages only when the operator is already at the bottom */
  const lastMessageId = messages[messages.length - 1]?.id ?? null;
  useEffect(() => {
    if (highlightMessageId !== null) return;
    if (atBottom) scrollToBottom(true);
  }, [lastMessageId, atBottom, scrollToBottom, highlightMessageId]);

  /* scroll to a highlighted (quoted / searched) message */
  useEffect(() => {
    if (highlightMessageId === null) return;
    const element = document.getElementById(`msg-${highlightMessageId}`);
    if (element) {
      /* Instant, not smooth: a `smooth` scroll is animated and the follow-the-bottom
         effect below can interrupt it mid-flight, leaving the hit off-screen. */
      element.scrollIntoView({ behavior: "auto", block: "center" });
      window.setTimeout(clearHighlight, 1900);
    }
  }, [highlightMessageId, clearHighlight]);

  /* CONTRACTS_V5 §4.3 п.1 — every callback handed to `memo(MessageBubble)` must keep
     its identity across renders, otherwise a single reaction click re-renders the
     whole timeline (a 54-message chat paid ~170 ms for one emoji). Zustand action
     references are stable for the lifetime of the store, so the dependency lists
     below are constant and these callbacks are created exactly once.
     They must stay *above* the `if (!chat)` early return: a hook after it would
     change the hook count between "no chat selected" and "chat selected". */
  const openMedia = useCallback((attachments: Attachment[], index: number): void => {
    if (attachments.length === 0) return;
    setLightbox({ attachments, index: Math.min(Math.max(index, 0), attachments.length - 1) });
  }, []);

  const handleReact = useCallback(
    (messageId: number, emoji: string): void => void putReaction(messageId, emoji),
    [putReaction],
  );
  const handleUnreact = useCallback(
    (messageId: number): void => void removeReaction(messageId),
    [removeReaction],
  );
  const handleForward = useCallback(
    (message: Message): void => openForwardDialog(message),
    [openForwardDialog],
  );
  const handleEdit = useCallback((message: Message): void => startEditing(message), [startEditing]);
  /* These two used to be inline arrows inside the `renderTimeline` call and
     therefore defeated `memo` on every render, not just on reaction clicks. */
  const handleReply = useCallback((message: Message): void => setReplyTarget(message.id), [setReplyTarget]);
  const handleSearchTx = useCallback(
    (txId: string): void => void lookupTx(txId, { source: "click" }),
    [lookupTx],
  );

  if (!chat) {
    return (
      <div className="chat-wallpaper flex h-full flex-col items-center justify-center gap-3 text-tg-textMuted">
        <div className="grid h-16 w-16 place-items-center rounded-full bg-tg-elevated">
          <MessageSquare size={26} className="text-tg-accentHover" />
        </div>
        <p className="text-sm font-medium">Select a merchant group to start</p>
        <p className="max-w-xs text-center text-2xs">
          All incoming Telegram messages, deletions and SLA incidents from the unified corporate account stream here in
          real time.
        </p>
      </div>
    );
  }

  const replyTo = replyTargetId !== null ? (messagesById.get(replyTargetId) ?? null) : null;
  const lock = typingLocks[chat.id] ?? null;
  const lockedBy = lock && lock.operator_id !== operator?.id ? lock : null;
  const slaDeadline = chat.sla ? parseDate(chat.sla.deadline_at) : null;

  return (
    <div className="flex h-full min-h-0 flex-col bg-tg-bg">
      {/* header */}
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-tg-border bg-tg-panel px-4">
        <div
          className="grid h-9 w-9 shrink-0 place-items-center rounded-full text-xs font-bold text-white"
          style={{ backgroundColor: avatarColor(chat.title) }}
          aria-hidden
        >
          {initials(chat.title)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-[13px] font-semibold text-tg-text">{chat.title}</h1>
            {chat.merchant_code && (
              <span className="shrink-0 rounded border border-tg-border bg-tg-elevated px-1 font-mono text-[9px] uppercase tracking-wide text-tg-textMuted">
                {chat.merchant_code}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 text-2xs text-tg-textMuted">
            {chat.username && (
              <span className="inline-flex items-center gap-0.5">
                <Hash size={9} />@{chat.username}
              </span>
            )}
            <span className="text-tg-textFaint">·</span>
            <span className="inline-flex items-center gap-1">
              <Users size={9} /> group
            </span>
            {chat.assigned_operator_name && (
              <>
                <span className="text-tg-textFaint">·</span>
                <span className="inline-flex items-center gap-1">
                  <UserCheck size={9} className="text-tg-accentHover" />
                  {chat.assigned_operator_name}
                </span>
              </>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {slaDeadline && (
            <div className="hidden flex-col items-end sm:flex">
              <span className="text-[9px] uppercase tracking-wider text-tg-textFaint">SLA deadline</span>
              <span className="tabular text-2xs text-tg-textMuted">
                {slaDeadline.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} ·{" "}
                {Math.max(0, Math.round((chat.sla_remaining_seconds ?? 0) / 60))}m left
              </span>
            </div>
          )}
          <SlaBadge chat={chat} />
          <button
            type="button"
            onClick={() => void loadMessages(chat.id)}
            className="grid h-8 w-8 place-items-center rounded-full text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text"
            title="Reload messages"
            aria-label="Reload messages"
          >
            <RefreshCw size={14} className={loadingMessages ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* SLA strip */}
      {chat.sla && (
        <div
          className={`flex shrink-0 items-center gap-2 border-b px-4 py-1 text-2xs ${
            (chat.sla_remaining_seconds ?? 1) < 0
              ? "border-tg-danger/30 bg-tg-danger/10 text-tg-danger"
              : (chat.sla_remaining_seconds ?? 1) < 60
                ? "border-tg-warning/30 bg-tg-warning/10 text-tg-warning"
                : "border-tg-border bg-tg-panelAlt text-tg-textMuted"
          }`}
        >
          <Flame size={11} />
          <span className="font-semibold uppercase tracking-wider">SLA incident #{chat.sla.status}</span>
          <span className="text-tg-textFaint">·</span>
          <span>
            Respond within{" "}
            <span className="font-semibold tabular">
              {Math.max(0, secondsUntil(chat.sla.deadline_at) ?? 0)}s
            </span>
          </span>
          <span className="ml-auto hidden sm:inline">
            Assignee: {chat.assigned_operator_name ?? "unassigned"}
          </span>
        </div>
      )}

      {/* messages */}
      <div className="relative min-h-0 flex-1">
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="chat-wallpaper scroll-area absolute inset-0 py-3"
          role="log"
          aria-live="polite"
        >
          {messages.length === 0 && (
            <div className="flex h-full items-center justify-center">
              <p className="rounded-lg bg-tg-elevated/80 px-3 py-2 text-2xs text-tg-textMuted">
                {loadingMessages ? "Loading history…" : "No messages in this group yet."}
              </p>
            </div>
          )}
          {renderTimeline(messages, {
            operatorId: operator?.id ?? null,
            resolveQuoted,
            highlight: highlightMessageId,
            onReply: handleReply,
            onJump: jumpToMessage,
            onSearchTx: handleSearchTx,
            onReact: handleReact,
            onUnreact: handleUnreact,
            onOpenMedia: openMedia,
            onForward: handleForward,
            onEdit: handleEdit,
          })}
          <div ref={bottomRef} className="h-1" />
        </div>

        {(showTopButton || showJumpButton) && (
          <div className="absolute bottom-4 right-4 flex flex-col gap-2">
            {showTopButton && (
              <button
                type="button"
                onClick={() => scrollToTop(true)}
                className="grid h-9 w-9 animate-fade-in place-items-center rounded-full border border-tg-border bg-tg-elevated text-tg-textMuted shadow-pop transition hover:text-tg-text"
                aria-label="Прокрутить вверх"
                title="В начало чата"
                data-testid="scroll-to-top"
              >
                <ArrowUp size={16} />
              </button>
            )}
            {showJumpButton && (
              <button
                type="button"
                onClick={() => scrollToBottom(true)}
                className="grid h-9 w-9 animate-fade-in place-items-center rounded-full border border-tg-border bg-tg-elevated text-tg-textMuted shadow-pop transition hover:text-tg-text"
                aria-label="Прокрутить вниз"
                title="К последним сообщениям"
                data-testid="scroll-to-bottom"
              >
                <ArrowDown size={16} />
              </button>
            )}
          </div>
        )}
      </div>

      {/* lock banner + composer */}
      {lockedBy && (
        <TypingLockBanner operatorName={lockedBy.operator_name} remainingMs={lockedBy.remaining_ms} />
      )}

      <MessageInput chatId={chat.id} replyTo={replyTo} messages={messages} lockedBy={lockedBy} />

      {lock === null && lookupSource === "click" && (
        <span className="sr-only" aria-live="polite">
          Transaction lookup triggered from a message
        </span>
      )}
      <span className="sr-only" aria-live="polite">
        {chat.unread_count > 0 ? `${chat.unread_count} unread messages` : ""}
      </span>
      <span className="hidden">
        <Search size={1} />
        <Trash2 size={1} />
        {formatClock(chat.last_message?.created_at ?? null)}
      </span>

      {/* CONTRACTS_V2 §3.1 — full-screen media viewer */}
      {lightbox && (
        <ImageLightbox
          attachments={lightbox.attachments}
          index={lightbox.index}
          onClose={() => setLightbox(null)}
          onNavigate={(index) =>
            setLightbox((current) => {
              if (!current) return current;
              const total = current.attachments.length;
              if (total === 0) return current;
              const safe = Math.min(Math.max(index, 0), total - 1);
              return safe === current.index ? current : { ...current, index: safe };
            })
          }
        />
      )}

      {/* CONTRACTS_V3 §1 — forward dialog (rendered for whichever chat it was opened from) */}
      {forwardSource !== null && (
        <ForwardDialog
          sourceMessage={forwardSource}
          chats={chats}
          currentChatId={forwardSource.chat_id}
          onClose={closeForwardDialog}
          onConfirm={forwardMessage}
          submitting={forwardSubmitting}
          error={forwardError}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------- timeline */

interface TimelineOptions {
  operatorId: number | null;
  resolveQuoted: (message: Message) => Message | null;
  highlight: number | null;
  onReply: (message: Message) => void;
  onJump: (messageId: number) => void;
  onSearchTx: (txId: string) => void;
  /** CONTRACTS_V2 §3.2 — reaction intents forwarded to the store. */
  onReact: (messageId: number, emoji: string) => void;
  onUnreact: (messageId: number) => void;
  /** CONTRACTS_V2 §3.1 — open the lightbox at `index` inside `attachments`. */
  onOpenMedia: (attachments: Attachment[], index: number) => void;
  /** CONTRACTS_V3 §3.1 — «Переслать» on the bubble action row. */
  onForward: (message: Message) => void;
  /** CONTRACTS_V3 §3.1 — «Изменить» on the bubble action row. */
  onEdit: (message: Message) => void;
}

function renderTimeline(messages: Message[], options: TimelineOptions): JSX.Element[] {
  const nodes: JSX.Element[] = [];
  let lastDateLabel = "";

  messages.forEach((message, index) => {
    const date = parseDate(message.created_at);
    const label = date
      ? date.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short" })
      : "";
    if (label !== lastDateLabel) {
      lastDateLabel = label;
      nodes.push(
        <div key={`date-${message.id}`} className="my-2 flex justify-center">
          <span className="rounded-full bg-tg-elevated/90 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-tg-textMuted shadow-bubble backdrop-blur">
            {label}
          </span>
        </div>,
      );
    }

    const quoted = options.resolveQuoted(message);
    nodes.push(
      <MessageBubble
        key={message.id}
        message={message}
        quotedMessage={quoted}
        isCurrentUserAuthor={message.sender_operator_id !== null && message.sender_operator_id === options.operatorId}
        highlighted={options.highlight === message.id}
        onReply={options.onReply}
        onJumpTo={options.onJump}
        onSearchTx={options.onSearchTx}
        onReact={options.onReact}
        onUnreact={options.onUnreact}
        onOpenMedia={options.onOpenMedia}
        onForward={options.onForward}
        onEdit={options.onEdit}
      />,
    );

    /* day separator for long gaps */
    const next = messages[index + 1];
    if (next && date && parseDate(next.created_at)) {
      const gapMinutes =
        ((parseDate(next.created_at) as Date).getTime() - date.getTime()) / 60_000;
      if (gapMinutes > 90) {
        nodes.push(
          <div key={`gap-${message.id}`} className="py-1 text-center text-[9px] uppercase tracking-widest text-tg-textFaint">
            later
          </div>,
        );
      }
    }
  });

  return nodes;
}
