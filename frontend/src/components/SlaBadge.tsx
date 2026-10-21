import { AlarmClock, Clock3, Flame } from "lucide-react";

import { formatCountdown } from "../lib/format";
import type { UiChatItem } from "../lib/types";

export type SlaBadgeSize = "sm" | "md";

/**
 * Live SLA countdown badge.
 * - green (> 120 s), amber (<= 120 s), red (< 60 s, pulsing), dark red (overdue)
 */
export function SlaBadge({
  chat,
  size = "md",
  showIcon = true,
}: {
  chat: Pick<UiChatItem, "sla" | "sla_remaining_seconds">;
  size?: SlaBadgeSize;
  showIcon?: boolean;
}): JSX.Element | null {
  if (!chat.sla) return null;
  const remaining = chat.sla_remaining_seconds ?? chat.sla.remaining_seconds;
  const overdue = remaining !== null && remaining < 0;
  const critical = remaining !== null && remaining < 60 && !overdue;
  const warning = remaining !== null && remaining >= 60 && remaining <= 120;

  const tone = overdue
    ? "bg-tg-danger/20 text-tg-danger border-tg-danger/50"
    : critical
      ? "bg-tg-danger/15 text-tg-danger border-tg-danger/45 animate-pulse-danger"
      : warning
        ? "bg-tg-warning/15 text-tg-warning border-tg-warning/45"
        : "bg-tg-success/12 text-tg-success border-tg-success/35";

  const Icon = overdue ? AlarmClock : critical ? Flame : Clock3;
  const padding = size === "sm" ? "px-1.5 py-0.5 text-2xs" : "px-1.5 py-0.5 text-[11px]";

  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-md border tabular font-semibold leading-none ${tone} ${padding}`}
      title={
        overdue
          ? `SLA overdue by ${formatCountdown(remaining)}`
          : `SLA deadline ${new Date(chat.sla.deadline_at).toLocaleTimeString()}`
      }
      aria-label={`SLA remaining ${formatCountdown(remaining)}`}
    >
      {showIcon && <Icon size={size === "sm" ? 9 : 10} strokeWidth={2.5} />}
      {formatCountdown(remaining)}
    </span>
  );
}

/** Hatched flame pill shown in the "SLA Burning" tab. */
export function BurningPill({ count }: { count: number }): JSX.Element {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-2xs font-semibold ${
        count > 0
          ? "border-tg-danger/50 bg-tg-danger/15 text-tg-danger"
          : "border-tg-border bg-tg-elevated text-tg-textFaint"
      }`}
    >
      <Flame size={10} />
      {count}
    </span>
  );
}
