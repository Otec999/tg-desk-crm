import {
  Activity,
  ChevronDown,
  CircleDollarSign,
  LogOut,
  Radio,
  ShieldCheck,
  Timer,
  Users,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { formatHumanDuration } from "../lib/format";
import type { ShiftStatus } from "../lib/types";
import { useCRMStore } from "../store/useCRMStore";

const SHIFT_META: Record<ShiftStatus, { label: string; dot: string; text: string; hint: string }> = {
  ONLINE: {
    label: "ONLINE",
    dot: "bg-tg-online",
    text: "text-tg-online",
    hint: "Receiving round-robin SLA assignments",
  },
  PAUSE: {
    label: "PAUSE",
    dot: "bg-tg-pause",
    text: "text-tg-pause",
    hint: "Temporarily excluded from new assignments",
  },
  OFFLINE: {
    label: "OFFLINE",
    dot: "bg-tg-offline",
    text: "text-tg-offline",
    hint: "Shift closed — no new incidents assigned",
  },
};

export function TopBar(): JSX.Element {
  const operator = useCRMStore((state) => state.operator);
  const operators = useCRMStore((state) => state.operators);
  const slaStats = useCRMStore((state) => state.slaStats);
  const connection = useCRMStore((state) => state.connection);
  const wsLatencyMs = useCRMStore((state) => state.wsLatencyMs);
  const mode = useCRMStore((state) => state.mode);
  const setShift = useCRMStore((state) => state.setShift);
  const logout = useCRMStore((state) => state.logout);

  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent): void => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const onlineCount = useMemo(
    () => operators.filter((item) => item.shift_status === "ONLINE" && item.is_online_ws).length,
    [operators],
  );

  if (!operator) return <></>;

  const shift = SHIFT_META[operator.shift_status];
  const slaPercent = slaStats?.sla_percent ?? 100;
  const slaTone = slaPercent >= 95 ? "text-tg-success" : slaPercent >= 85 ? "text-tg-warning" : "text-tg-danger";

  const connectionBadge =
    mode === "mock"
      ? { icon: CircleDollarSign, label: "MOCK DATA", tone: "text-tg-warning border-tg-warning/40 bg-tg-warning/10" }
      : connection === "open"
        ? {
            icon: Wifi,
            label: wsLatencyMs !== null ? `LIVE · ${wsLatencyMs}ms` : "LIVE",
            tone: "text-tg-success border-tg-success/40 bg-tg-success/10",
          }
        : connection === "connecting"
          ? { icon: Radio, label: "CONNECTING", tone: "text-tg-info border-tg-info/40 bg-tg-info/10" }
          : { icon: WifiOff, label: "RECONNECTING", tone: "text-tg-danger border-tg-danger/40 bg-tg-danger/10" };

  const ConnectionIcon = connectionBadge.icon;

  return (
    <header className="z-20 flex h-16 shrink-0 items-center justify-between gap-4 border-b border-tg-border bg-tg-panel px-4">
      {/* identity */}
      <div className="flex min-w-0 items-center gap-3">
        <div className="relative">
          <div className="grid h-10 w-10 place-items-center rounded-full bg-tg-accent/20 text-sm font-semibold text-tg-accentHover ring-1 ring-tg-accent/30">
            {operator.display_name
              .split(" ")
              .map((part) => part[0])
              .slice(0, 2)
              .join("")
              .toUpperCase()}
          </div>
          <span
            className={`absolute -bottom-0.5 -right-0.5 h-3 w-3 rounded-full border-2 border-tg-panel ${shift.dot}`}
            aria-hidden
          />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-tg-text">{operator.display_name}</span>
            <span className="rounded border border-tg-border bg-tg-elevated px-1.5 py-0.5 text-2xs font-medium uppercase tracking-wide text-tg-textMuted">
              {operator.role === "SUPERVISOR" ? (
                <span className="inline-flex items-center gap-1">
                  <ShieldCheck size={10} /> Supervisor
                </span>
              ) : (
                "Operator"
              )}
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-2xs text-tg-textMuted">
            <span className="font-mono">@{operator.username}</span>
            <span className="text-tg-textFaint">·</span>
            <span className="inline-flex items-center gap-1">
              <Users size={10} />
              {onlineCount} online
            </span>
          </div>
        </div>
      </div>

      {/* personal SLA */}
      <div className="hidden items-center gap-4 rounded-lg border border-tg-border bg-tg-panelAlt px-4 py-1.5 lg:flex">
        <Metric
          icon={<Timer size={13} className="text-tg-accentHover" />}
          label="Avg Reply"
          value={formatHumanDuration(slaStats?.avg_response_seconds ?? 0)}
          title="Average first-response time over the last 30 days"
        />
        <span className="divider-v" />
        <Metric
          icon={<Activity size={13} className="text-tg-accentHover" />}
          label="SLA"
          value={`${slaPercent.toFixed(0)}%`}
          valueClass={slaTone}
          title={`${slaStats?.resolved_count ?? 0} resolved · ${slaStats?.open_count ?? 0} open · ${slaStats?.overdue_count ?? 0} overdue`}
        />
        <span className="divider-v" />
        <Metric
          icon={<Radio size={13} className="text-tg-accentHover" />}
          label="Open"
          value={String(slaStats?.open_count ?? 0)}
          valueClass={(slaStats?.overdue_count ?? 0) > 0 ? "text-tg-danger" : undefined}
          title="Incidents currently in PENDING state"
        />
      </div>

      {/* actions */}
      <div className="flex items-center gap-2">
        <span
          className={`hidden items-center gap-1.5 rounded-md border px-2 py-1 text-2xs font-semibold uppercase tracking-wider sm:inline-flex ${connectionBadge.tone}`}
          title={mode === "mock" ? "Backend unreachable — demo dataset" : "WebSocket connection to the API"}
        >
          <ConnectionIcon size={11} />
          {connectionBadge.label}
        </span>

        <div className="relative" ref={menuRef}>
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            className="flex items-center gap-2 rounded-lg border border-tg-border bg-tg-panelAlt px-3 py-1.5 text-xs font-semibold transition hover:bg-tg-hover"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            title={shift.hint}
          >
            <span className={`shift-dot ${shift.dot}`} />
            <span className={shift.text}>{shift.label}</span>
            <ChevronDown size={13} className={`text-tg-textMuted transition-transform ${menuOpen ? "rotate-180" : ""}`} />
          </button>

          {menuOpen && (
            <div
              role="menu"
              className="absolute right-0 top-11 z-30 w-60 animate-slide-up overflow-hidden rounded-xl border border-tg-border bg-tg-elevated shadow-pop"
            >
              <p className="border-b border-tg-border px-3 py-2 text-2xs uppercase tracking-wider text-tg-textFaint">
                Shift status
              </p>
              {(Object.keys(SHIFT_META) as ShiftStatus[]).map((status) => {
                const meta = SHIFT_META[status];
                const active = operator.shift_status === status;
                return (
                  <button
                    key={status}
                    type="button"
                    role="menuitemradio"
                    aria-checked={active}
                    onClick={() => {
                      setMenuOpen(false);
                      if (!active) void setShift(status);
                    }}
                    className={`flex w-full items-center gap-2.5 px-3 py-2 text-left text-xs transition hover:bg-tg-hover ${
                      active ? "bg-tg-hover/60" : ""
                    }`}
                  >
                    <span className={`shift-dot ${meta.dot}`} />
                    <span className={`font-semibold ${meta.text}`}>{meta.label}</span>
                    {active && <span className="ml-auto text-2xs text-tg-textFaint">current</span>}
                  </button>
                );
              })}
              <div className="border-t border-tg-border p-1">
                <button
                  type="button"
                  onClick={() => {
                    setMenuOpen(false);
                    logout();
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-xs text-tg-textMuted transition hover:bg-tg-hover hover:text-tg-danger"
                >
                  <LogOut size={13} />
                  End session
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}

function Metric({
  icon,
  label,
  value,
  valueClass = "text-tg-text",
  title,
}: {
  icon: JSX.Element;
  label: string;
  value: string;
  valueClass?: string;
  title?: string;
}): JSX.Element {
  return (
    <div className="flex items-center gap-2" title={title}>
      {icon}
      <div className="leading-tight">
        <div className="text-2xs uppercase tracking-wider text-tg-textFaint">{label}</div>
        <div className={`tabular text-xs font-semibold ${valueClass}`}>{value}</div>
      </div>
    </div>
  );
}
