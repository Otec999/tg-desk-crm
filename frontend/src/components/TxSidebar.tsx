import {
  AlertCircle,
  ArrowRightLeft,
  CheckCircle2,
  ClipboardCopy,
  Clock3,
  Database,
  FileSearch,
  Loader2,
  RefreshCw,
  Search,
  Server,
  ShieldAlert,
  Timer,
  Trash2,
  XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";

import { formatHumanDuration, normalizeTxId } from "../lib/format";
import type { TxLookupResponse, TxPanelResult } from "../lib/types";
import { selectSelectedChat, useCRMStore, type TxLookupState, type TxPanelKey } from "../store/useCRMStore";
import { SlaBadge } from "./SlaBadge";

const PANEL_META: Record<TxPanelKey, { title: string; subtitle: string; icon: JSX.Element; accent: string }> = {
  one: { title: "Admin Panel 1", subtitle: "Gateway", icon: <Server size={13} />, accent: "text-tg-info" },
  two: { title: "Admin Panel 2", subtitle: "Backoffice", icon: <Database size={13} />, accent: "text-tg-accentHover" },
};

export function TxSidebar(): JSX.Element {
  const chat = useCRMStore(selectSelectedChat);
  const txOne = useCRMStore((s) => s.txOne);
  const txTwo = useCRMStore((s) => s.txTwo);
  const lookupTx = useCRMStore((s) => s.lookupTx);
  const clearTx = useCRMStore((s) => s.clearTx);
  const insertTxIntoReply = useCRMStore((s) => s.insertTxIntoReply);
  const slaStats = useCRMStore((s) => s.slaStats);

  const [inputOne, setInputOne] = useState("");
  const [inputTwo, setInputTwo] = useState("");

  useEffect(() => {
    if (txOne.tx_id) setInputOne(txOne.tx_id);
  }, [txOne.tx_id]);
  useEffect(() => {
    if (txTwo.tx_id) setInputTwo(txTwo.tx_id);
  }, [txTwo.tx_id]);

  const submit = (panel: TxPanelKey): void => {
    const txId = normalizeTxId(panel === "one" ? inputOne : inputTwo);
    if (txId) void lookupTx(txId, { panel, source: "manual" });
  };

  const sharedResult: TxLookupResponse | null = txOne.result ?? txTwo.result;

  return (
    <aside className="flex h-full min-h-0 flex-col bg-tg-panel">
      <div className="shrink-0 border-b border-tg-border px-3 py-2.5">
        <div className="flex items-center justify-between">
          <h2 className="flex items-center gap-1.5 text-xs font-semibold text-tg-text">
            <FileSearch size={13} className="text-tg-accentHover" />
            Transaction Lookup
          </h2>
          <button
            type="button"
            onClick={() => {
              clearTx();
              setInputOne("");
              setInputTwo("");
            }}
            className="rounded p-1 text-tg-textFaint transition hover:bg-tg-hover hover:text-tg-text"
            title="Clear both lookups"
            aria-label="Clear both lookups"
          >
            <Trash2 size={12} />
          </button>
        </div>
        <p className="mt-0.5 text-[10px] text-tg-textFaint">
          Reads the gateway and the backoffice in one shot. Every query is audit-logged.
        </p>
      </div>

      <div className="scroll-area min-h-0 flex-1 space-y-3 p-3">
        <LookupCard
          panel="one"
          value={inputOne}
          onChange={setInputOne}
          onSubmit={() => submit("one")}
          state={txOne}
          onInsert={() => insertTxIntoReply("one")}
        />

        <div className="flex items-center gap-2 text-[9px] uppercase tracking-widest text-tg-textFaint">
          <span className="h-px flex-1 bg-tg-border" />
          <ArrowRightLeft size={11} />
          dual-source
          <span className="h-px flex-1 bg-tg-border" />
        </div>

        <LookupCard
          panel="two"
          value={inputTwo}
          onChange={setInputTwo}
          onSubmit={() => submit("two")}
          state={txTwo}
          onInsert={() => insertTxIntoReply("two")}
        />

        {sharedResult && <ComparisonRow result={sharedResult} />}

        <section className="rounded-lg border border-tg-border bg-tg-panelAlt p-2.5">
          <h3 className="mb-2 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-tg-textFaint">
            <Timer size={11} /> SLA — current group
          </h3>
          {chat ? (
            <div className="space-y-1.5 text-2xs">
              <Row label="Group" value={chat.title} />
              <Row label="Merchant" value={chat.merchant_code ?? "—"} mono />
              <Row label="Assignee" value={chat.assigned_operator_name ?? "unassigned"} />
              <div className="flex items-center justify-between">
                <span className="text-tg-textFaint">Countdown</span>
                {chat.sla ? <SlaBadge chat={chat} /> : <span className="text-tg-textMuted">no open incident</span>}
              </div>
              {chat.sla && (
                <Row
                  label="Deadline"
                  value={new Date(chat.sla.deadline_at).toLocaleTimeString(undefined, {
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                  })}
                  mono
                />
              )}
              <div className="my-1.5 h-px bg-tg-border" />
              <Row label="Avg reply (me)" value={formatHumanDuration(slaStats?.avg_response_seconds ?? 0)} />
              <Row
                label="Personal SLA"
                value={`${(slaStats?.sla_percent ?? 100).toFixed(0)}%`}
                tone={(slaStats?.sla_percent ?? 100) >= 95 ? "text-tg-success" : "text-tg-warning"}
              />
              <Row label="Open incidents" value={String(slaStats?.open_count ?? 0)} />
            </div>
          ) : (
            <p className="text-2xs text-tg-textFaint">Select a group to see its SLA state.</p>
          )}
        </section>
      </div>
    </aside>
  );
}

function LookupCard({
  panel,
  value,
  onChange,
  onSubmit,
  state,
  onInsert,
}: {
  panel: TxPanelKey;
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  state: TxLookupState;
  onInsert: () => void;
}): JSX.Element {
  const meta = PANEL_META[panel];
  const result = state.result ? (panel === "one" ? state.result.admin_one : state.result.admin_two) : null;

  return (
    <section className="rounded-lg border border-tg-border bg-tg-panelAlt p-2.5">
      <header className="mb-2 flex items-center justify-between">
        <h3 className={`flex items-center gap-1.5 text-[11px] font-semibold ${meta.accent}`}>
          {meta.icon}
          {meta.title}
        </h3>
        <span className="text-[9px] uppercase tracking-wider text-tg-textFaint">{meta.subtitle}</span>
      </header>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit();
        }}
        className="flex gap-1.5"
      >
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="TX ID — e.g. TX-4412-DD"
          spellCheck={false}
          autoComplete="off"
          className="min-w-0 flex-1 rounded-md border border-tg-border bg-tg-bg px-2 py-1.5 font-mono text-[11px] uppercase tracking-wide text-tg-text placeholder:font-sans placeholder:normal-case placeholder:tracking-normal placeholder:text-tg-textFaint focus:border-tg-accent/60 focus:outline-none"
          aria-label={`${meta.title} transaction ID`}
        />
        <button
          type="submit"
          disabled={state.loading || value.trim().length === 0}
          className="grid h-[30px] w-8 shrink-0 place-items-center rounded-md bg-tg-accent/20 text-tg-accentHover transition hover:bg-tg-accent/35 disabled:cursor-not-allowed disabled:opacity-40"
          title="Search"
          aria-label={`Search ${meta.title}`}
        >
          {state.loading ? <Loader2 size={13} className="animate-spin" /> : <Search size={13} />}
        </button>
      </form>

      {state.error && (
        <p className="mt-2 flex items-center gap-1 rounded bg-tg-danger/10 px-1.5 py-1 text-[10px] text-tg-danger">
          <AlertCircle size={10} /> {state.error}
        </p>
      )}

      {result && (
        <div className="mt-2 animate-fade-in space-y-1 rounded-md border border-tg-border bg-tg-bg/60 p-2 text-2xs">
          <div className="mb-1 flex items-center justify-between">
            <span className="font-mono text-[11px] font-semibold text-tg-text">{result.tx_id}</span>
            <StatusChip status={result.status} found={result.found} />
          </div>
          <Row label="Amount" value={formatAmount(result)} mono />
          <Row label="Currency" value={result.currency ?? "—"} mono />
          <Row label="Provider" value={result.provider ?? "—"} />
          {result.external_ref && <Row label="Ref" value={result.external_ref} mono />}
          {result.error_reason && (
            <div className="mt-1 flex items-start gap-1 rounded bg-tg-danger/10 px-1.5 py-1 text-[10px] text-tg-danger">
              <ShieldAlert size={10} className="mt-[1px] shrink-0" />
              <span>{result.error_reason}</span>
            </div>
          )}
          <div className="flex items-center gap-1 pt-1">
            <button
              type="button"
              onClick={onInsert}
              className="flex flex-1 items-center justify-center gap-1 rounded-md border border-tg-accent/40 bg-tg-accent/15 px-1.5 py-1 text-[10px] font-semibold text-tg-accentHover transition hover:bg-tg-accent/30 hover:text-white"
              title="Insert the formatted status into the chat input"
            >
              <ArrowRightLeft size={10} /> Insert into reply
            </button>
            <button
              type="button"
              onClick={() =>
                void navigator.clipboard?.writeText(`${result.tx_id} → ${result.status}`).catch(() => undefined)
              }
              className="grid h-[22px] w-6 place-items-center rounded-md border border-tg-border text-tg-textMuted transition hover:text-tg-text"
              title="Copy status"
              aria-label="Copy status"
            >
              <ClipboardCopy size={10} />
            </button>
          </div>
          {!result.found && (
            <p className="flex items-center gap-1 text-[10px] text-tg-textFaint">
              <RefreshCw size={9} /> Not present in this system — check the other panel.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function ComparisonRow({ result }: { result: TxLookupResponse }): JSX.Element {
  const same = result.admin_one.status === result.admin_two.status;
  const mismatched = result.admin_one.found && result.admin_two.found && !same;
  return (
    <div
      className={`rounded-lg border px-2.5 py-1.5 text-[10px] ${
        mismatched
          ? "border-tg-warning/40 bg-tg-warning/10 text-tg-warning"
          : "border-tg-success/30 bg-tg-success/10 text-tg-success"
      }`}
    >
      <div className="flex items-center gap-1 font-semibold">
        {mismatched ? <AlertCircle size={11} /> : <CheckCircle2 size={11} />}
        {mismatched ? "Panels disagree" : "Panels agree"}
      </div>
      <div className="mt-0.5 font-mono text-tg-textMuted">
        {result.admin_one.status} / {result.admin_two.status}
      </div>
    </div>
  );
}

function StatusChip({ status, found }: { status: string; found: boolean }): JSX.Element {
  const failed = status === "FAILED" || status === "DECLINED" || status === "ERROR";
  const tone = !found
    ? "border-tg-textFaint/40 bg-tg-textFaint/10 text-tg-textMuted"
    : status === "COMPLETED"
      ? "border-tg-success/45 bg-tg-success/15 text-tg-success"
      : failed
        ? "border-tg-danger/45 bg-tg-danger/15 text-tg-danger"
        : status === "REFUNDED"
          ? "border-tg-accent/45 bg-tg-accent/15 text-tg-accentHover"
          : "border-tg-warning/45 bg-tg-warning/15 text-tg-warning";
  const Icon = status === "COMPLETED" ? CheckCircle2 : failed ? XCircle : Clock3;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide ${tone}`}
    >
      <Icon size={9} />
      {status}
    </span>
  );
}

function Row({
  label,
  value,
  mono = false,
  tone = "text-tg-text",
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: string;
}): JSX.Element {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="shrink-0 text-tg-textFaint">{label}</span>
      <span className={`truncate text-right ${mono ? "font-mono" : ""} ${tone}`} title={value}>
        {value}
      </span>
    </div>
  );
}

function formatAmount(result: TxPanelResult): string {
  if (!result.amount) return "—";
  const numeric = Number(result.amount);
  if (Number.isNaN(numeric)) return result.amount;
  return numeric.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
