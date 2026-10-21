import { Menu, X } from "lucide-react";
import { useEffect } from "react";

import { useCRMStore } from "../store/useCRMStore";
import { ChatList } from "./ChatList";
import { TxSidebar } from "./TxSidebar";

export type DrawerKind = "chats" | "lookup" | null;

/**
 * Mobile drawer: below `md` the three columns do not fit, so the chat list and
 * the transaction sidebar slide in as an overlay. Desktop layout is untouched.
 */
export function MobileDrawer({ kind, onClose }: { kind: DrawerKind; onClose: () => void }): JSX.Element | null {
  const selectedChatId = useCRMStore((state) => state.selectedChatId);

  useEffect(() => {
    if (kind === null) return;
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [kind, onClose]);

  /* auto-close the chat drawer once a group is picked */
  useEffect(() => {
    if (kind === "chats" && selectedChatId !== null) onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedChatId]);

  if (kind === null) return null;

  return (
    <div className="fixed inset-0 z-40 flex md:hidden">
      <button
        type="button"
        aria-label="Close panel"
        onClick={onClose}
        className="absolute inset-0 animate-fade-in bg-black/60 backdrop-blur-sm"
      />
      <div
        className={`relative flex h-full w-[86vw] max-w-[360px] animate-slide-up flex-col border-r border-tg-border bg-tg-panel shadow-pop ${
          kind === "lookup" ? "ml-auto animate-fade-in border-l border-r-0" : ""
        }`}
      >
        <button
          type="button"
          onClick={onClose}
          className="absolute right-2 top-2 z-10 grid h-7 w-7 place-items-center rounded-full bg-tg-elevated text-tg-textMuted"
          aria-label="Close"
        >
          <X size={14} />
        </button>
        {kind === "chats" ? <ChatList /> : <TxSidebar />}
      </div>
    </div>
  );
}

/** Toolbar used inside the chat header on small screens. */
export function MobileToolbar({ onOpen }: { onOpen: (kind: Exclude<DrawerKind, null>) => void }): JSX.Element {
  return (
    <div className="flex items-center gap-1 md:hidden">
      <button
        type="button"
        onClick={() => onOpen("chats")}
        className="grid h-8 w-8 place-items-center rounded-full text-tg-textMuted hover:bg-tg-hover hover:text-tg-text"
        aria-label="Open chat list"
      >
        <Menu size={16} />
      </button>
    </div>
  );
}
