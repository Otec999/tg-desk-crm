import { Flame, Inbox, MessageSquareDashed, Search, Trash2, UserCheck } from "lucide-react";
import { useMemo, useState } from "react";

import { avatarColor, formatChatListTime, initials, previewText } from "../lib/format";
import type { ChatFilter, UiChatItem } from "../lib/types";
import { selectVisibleChats, useCRMStore } from "../store/useCRMStore";
import { SlaBadge } from "./SlaBadge";

const FILTERS: { key: ChatFilter; label: string; icon: JSX.Element }[] = [
  { key: "all", label: "All", icon: <Inbox size={12} /> },
  { key: "mine", label: "Assigned to Me", icon: <UserCheck size={12} /> },
  { key: "burning", label: "SLA Burning", icon: <Flame size={12} /> },
];

export function ChatList(): JSX.Element {
  const chats = useCRMStore(selectVisibleChats);
  const allChats = useCRMStore((state) => state.chats);
  const filter = useCRMStore((state) => state.filter);
  const setFilter = useCRMStore((state) => state.setFilter);
  const selectedChatId = useCRMStore((state) => state.selectedChatId);
  const selectChat = useCRMStore((state) => state.selectChat);
  const loadingChats = useCRMStore((state) => state.loadingChats);
  const openSearch = useCRMStore((state) => state.openSearch);
  const [query, setQuery] = useState("");

  const burningCount = useMemo(
    () =>
      allChats.filter(
        (chat) => chat.sla !== null && chat.sla.status === "PENDING" && (chat.sla_remaining_seconds ?? 0) <= 60,
      ).length,
    [allChats],
  );

  const totalUnread = useMemo(() => allChats.reduce((sum, chat) => sum + chat.unread_count, 0), [allChats]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return chats;
    return chats.filter(
      (chat) =>
        chat.title.toLowerCase().includes(needle) ||
        (chat.merchant_code ?? "").toLowerCase().includes(needle) ||
        (chat.username ?? "").toLowerCase().includes(needle),
    );
  }, [chats, query]);

  return (
    <aside className="flex h-full min-h-0 flex-col bg-tg-panel">
      {/* header + search */}
      <div className="shrink-0 border-b border-tg-border px-3 pb-2 pt-3">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-tg-text">
            Chats
            {totalUnread > 0 && (
              <span className="ml-2 rounded-full bg-tg-accent px-1.5 py-0.5 text-2xs font-bold text-white">
                {totalUnread}
              </span>
            )}
          </h2>
          <div className="flex items-center gap-1.5">
            <span className="text-2xs uppercase tracking-wider text-tg-textFaint">{allChats.length} groups</span>
            {/* CONTRACTS_V5 §6.3 — entry point to the global overlay (Ctrl/⌘+K). */}
            <button
              type="button"
              onClick={openSearch}
              aria-label="Глобальный поиск"
              title="Глобальный поиск (Ctrl+K)"
              data-testid="open-global-search"
              className="grid h-6 w-6 shrink-0 place-items-center rounded-md text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-text focus:outline-none focus-visible:ring-2 focus-visible:ring-tg-accent"
            >
              <Search size={13} />
            </button>
          </div>
        </div>
        <div className="relative">
          <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-tg-textFaint" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search merchants / MRC codes"
            className="w-full rounded-lg border border-transparent bg-tg-elevated py-1.5 pl-8 pr-2 text-xs text-tg-text placeholder:text-tg-textFaint focus:border-tg-accent/50 focus:bg-tg-hover focus:outline-none"
            aria-label="Search chats"
          />
        </div>
      </div>

      {/* filter tabs */}
      <div role="tablist" aria-label="Chat filters" className="flex shrink-0 gap-1 border-b border-tg-border px-2 py-1.5">
        {FILTERS.map((item) => {
          const active = filter === item.key;
          return (
            <button
              key={item.key}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setFilter(item.key)}
              className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-2xs font-semibold transition ${
                active
                  ? "bg-tg-accent/15 text-tg-accentHover shadow-[inset_0_0_0_1px_rgba(135,116,225,0.35)]"
                  : "text-tg-textMuted hover:bg-tg-hover hover:text-tg-text"
              }`}
            >
              {item.icon}
              <span className="truncate">{item.label}</span>
              {item.key === "burning" && burningCount > 0 && (
                <span
                  className={`rounded-full px-1 text-[10px] font-bold leading-4 ${
                    active ? "bg-tg-danger text-white" : "bg-tg-danger/80 text-white"
                  }`}
                >
                  {burningCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* list */}
      <div className="scroll-area min-h-0 flex-1" role="listbox" aria-label="Chat list">
        {loadingChats && visible.length === 0 && <SkeletonList />}
        {!loadingChats && visible.length === 0 && (
          <div className="flex flex-col items-center gap-2 px-4 py-16 text-center">
            <MessageSquareDashed size={28} className="text-tg-textFaint" />
            <p className="text-xs text-tg-textMuted">
              {filter === "burning"
                ? "No incidents burning right now — well handled."
                : filter === "mine"
                  ? "No groups assigned to you yet."
                  : "No chats match your search."}
            </p>
          </div>
        )}
        {visible.map((chat) => (
          <ChatRow
            key={chat.id}
            chat={chat}
            active={chat.id === selectedChatId}
            onSelect={() => void selectChat(chat.id)}
          />
        ))}
      </div>
    </aside>
  );
}

function ChatRow({
  chat,
  active,
  onSelect,
}: {
  chat: UiChatItem;
  active: boolean;
  onSelect: () => void;
}): JSX.Element {
  const last = chat.last_message;
  const overdue = (chat.sla_remaining_seconds ?? 1) < 0 && chat.sla !== null;
  const deletedLast = last?.text?.startsWith("\uD83D\uDDD1") ?? false;

  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      onClick={onSelect}
      className={`relative flex w-full gap-2.5 border-b border-tg-borderSoft px-3 py-2.5 text-left transition-colors ${
        active ? "bg-tg-accent/12" : "hover:bg-tg-hover/70"
      }`}
    >
      {active && <span className="absolute inset-y-0 left-0 w-0.5 bg-tg-accent" aria-hidden />}

      <div
        className="grid h-10 w-10 shrink-0 place-items-center rounded-full text-xs font-bold text-white"
        style={{ backgroundColor: avatarColor(chat.title) }}
        aria-hidden
      >
        {initials(chat.title)}
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-xs font-semibold text-tg-text">{chat.title}</span>
          {chat.unread_count > 0 && (
            <span className="ml-auto shrink-0 rounded-full bg-tg-accent px-1.5 py-[1px] text-[10px] font-bold leading-4 text-white">
              {chat.unread_count > 99 ? "99+" : chat.unread_count}
            </span>
          )}
          {chat.unread_count === 0 && last && (
            <span className="ml-auto shrink-0 text-2xs text-tg-textFaint">{formatChatListTime(last.created_at)}</span>
          )}
        </div>

        <div className="mt-0.5 flex items-center gap-1.5">
          <p
            className={`min-w-0 flex-1 truncate text-2xs ${
              deletedLast ? "text-tg-danger/80 line-through decoration-tg-danger/40" : "text-tg-textMuted"
            }`}
          >
            {last ? (
              <>
                {last.is_outgoing && <span className="text-tg-accentHover">You: </span>}
                {deletedLast && <Trash2 size={10} className="mr-0.5 inline text-tg-danger" />}
                {previewText(last.text, 46)}
              </>
            ) : (
              <span className="italic text-tg-textFaint">No messages yet</span>
            )}
          </p>
          <SlaBadge chat={chat} size="sm" showIcon={false} />
        </div>

        <div className="mt-1 flex items-center gap-1.5">
          {chat.merchant_code && (
            <span className="rounded border border-tg-border bg-tg-elevated px-1 font-mono text-[9px] uppercase tracking-wide text-tg-textFaint">
              {chat.merchant_code}
            </span>
          )}
          {chat.assigned_operator_name ? (
            <span className="inline-flex items-center gap-0.5 text-[10px] text-tg-textFaint">
              <UserCheck size={9} />
              {chat.assigned_operator_name}
            </span>
          ) : (
            chat.sla && <span className="text-[10px] font-medium text-tg-warning">Unassigned</span>
          )}
          {overdue && (
            <span className="ml-auto rounded bg-tg-danger/15 px-1 text-[9px] font-bold uppercase text-tg-danger">
              overdue
            </span>
          )}
        </div>
      </div>
    </button>
  );
}

function SkeletonList(): JSX.Element {
  return (
    <div className="space-y-1 p-2" aria-hidden>
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="flex gap-2.5 rounded-lg p-2">
          <div className="h-10 w-10 shrink-0 animate-pulse rounded-full bg-tg-elevated" />
          <div className="flex-1 space-y-1.5 py-1">
            <div className="h-2.5 w-2/3 animate-pulse rounded bg-tg-elevated" />
            <div className="h-2 w-full animate-pulse rounded bg-tg-elevated/70" />
          </div>
        </div>
      ))}
    </div>
  );
}
