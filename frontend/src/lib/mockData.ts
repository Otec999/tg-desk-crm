/**
 * Offline mock dataset (CONTRACTS.md §7 mirror).
 *
 * Used when `VITE_MOCK_MODE=true` or when the backend is unreachable, so the
 * whole UI (3 columns, SLA countdowns, replies, deletes, tx lookups, locks) is
 * demonstrable with `npm run dev` alone. Shapes are byte-identical to the API.
 */

import { extractTxIds, uuid } from "./format";
import type {
  ChatListItem,
  Message,
  Operator,
  PersonalSlaStats,
  TxAdminPanel,
  TxLookupResponse,
  TxPanelResult,
} from "./types";

const NOW = Date.now();

function iso(offsetSeconds: number): string {
  return new Date(NOW + offsetSeconds * 1000).toISOString();
}

/* ------------------------------------------------------------- operators */

export const MOCK_OPERATORS: Operator[] = [
  {
    id: 1,
    username: "op1",
    display_name: "Operator One",
    role: "OPERATOR",
    shift_status: "ONLINE",
    is_online_ws: true,
    last_seen_at: iso(-5),
    sla_target_seconds: 300,
    created_at: iso(-86400 * 30),
  },
  {
    id: 2,
    username: "op2",
    display_name: "Operator Two",
    role: "OPERATOR",
    shift_status: "ONLINE",
    is_online_ws: true,
    last_seen_at: iso(-12),
    sla_target_seconds: 300,
    created_at: iso(-86400 * 30),
  },
  {
    id: 11,
    username: "admin",
    display_name: "Shift Supervisor",
    role: "SUPERVISOR",
    shift_status: "ONLINE",
    is_online_ws: true,
    last_seen_at: iso(-3),
    sla_target_seconds: 300,
    created_at: iso(-86400 * 60),
  },
];

export const MOCK_CURRENT_OPERATOR: Operator = MOCK_OPERATORS[0];

export const MOCK_SLA_STATS: PersonalSlaStats = {
  operator_id: 1,
  shift_status: "ONLINE",
  avg_response_seconds: 75,
  resolved_count: 48,
  open_count: 2,
  overdue_count: 0,
  sla_percent: 98.0,
  sla_target_seconds: 300,
};

/* ----------------------------------------------------------------- chats */

interface MockChatSpec {
  id: number;
  title: string;
  username: string;
  merchant_code: string;
  assigned: number | null;
  unread: number;
  slaRemaining: number | null;
  overdue?: boolean;
}

const CHAT_SPECS: MockChatSpec[] = [
  { id: 1, title: "Merchant Alpha — Payouts", username: "merchant_alpha_support", merchant_code: "MRC-ALPHA", assigned: 1, unread: 2, slaRemaining: 238 },
  { id: 2, title: "Merchant Beta — Settlements", username: "merchant_beta_ops", merchant_code: "MRC-BETA", assigned: 1, unread: 4, slaRemaining: 41 },
  { id: 3, title: "Merchant Gamma — Chargebacks", username: "merchant_gamma_risk", merchant_code: "MRC-GAMMA", assigned: null, unread: 1, slaRemaining: -120, overdue: true },
  { id: 4, title: "Merchant Delta — KYC", username: "merchant_delta_kyc", merchant_code: "MRC-DELTA", assigned: 2, unread: 0, slaRemaining: null },
  { id: 5, title: "Merchant Epsilon — Refunds", username: "merchant_epsilon_fin", merchant_code: "MRC-EPSILON", assigned: 1, unread: 0, slaRemaining: null },
  { id: 6, title: "Merchant Zeta — Integrations / API", username: "merchant_zeta_dev", merchant_code: "MRC-ZETA", assigned: null, unread: 0, slaRemaining: null },
];

/* -------------------------------------------------------------- messages */

interface MsgExtra {
  phone?: string;
  /** 1-based index of another spec in the same chat that this message quotes. */
  replyTo?: number;
  /** Marks the message as soft-deleted in Telegram (anti-delete demo). */
  deleted?: boolean;
}

type MsgSpec = [
  chatId: number,
  senderType: Message["sender_type"],
  senderName: string,
  text: string,
  minutesAgo: number,
  extra?: MsgExtra,
];

const MESSAGE_SPECS: MsgSpec[] = [
  [1, "CLIENT", "Jane Doe", "Hi team, our payout TX-4412-DD is stuck in FAILED since this morning. Can you check what happened?", 42, { phone: "+1 555 0100" }],
  [1, "BOT", "AcmePayout Bot", "Transaction: TX-4412-DD | Provider: AcmePayout | Amount: 1500.00 USDT | Status: FAILED | Reason: Insufficient provider balance", 41],
  [1, "OPERATOR", "Operator Two", "Hey Jane! Pulling the gateway log for TX-4412-DD right now, one moment please.", 38, { replyTo: 1 }],
  [1, "CLIENT", "Jane Doe", "Thanks. Also can you confirm the settlement for TX-7781-AC went through? Merchant finance is asking.", 26],
  [1, "BOT", "GatewayMonitor", "Alert: TX-7781-AC COMPLETED after 2 retries. Provider: PayGate. Amount: 240.50 EUR.", 24],
  [1, "OPERATOR", "Operator Two", "TX-7781-AC is fully settled — funds credited to your settlement wallet at 10:42 UTC. For TX-4412-DD we are re-routing via CryptoBridge.", 21],
  [1, "CLIENT", "Jane Doe", "Perfect, I will forward it to finance.", 18],
  [1, "CLIENT", "Jane Doe", "one more thing — our callback URL keeps rejecting refund notifications for TX-0000-XX", 12],
  [1, "CLIENT", "Jane Doe", "please confirm the refund id you have on your side, we need it urgently for the chargeback dispute filing today", 5, { deleted: true }],
  [1, "BOT", "AcmePayout Bot", "Retry scheduled: TX-4412-DD queued for provider failover (attempt 2/3) at 12:30 UTC.", 3],
  [1, "CLIENT", "Jane Doe", "Any update on the retry?", 1],

  [2, "CLIENT", "Ahmed Karim", "Settlement for yesterday is short by 12.4%. TX-1102-BE still shows PROCESSING.", 35],
  [2, "BOT", "GatewayMonitor", "Transaction: TX-1102-BE | Status: PROCESSING | Amount: 8400.00 USDT | Provider: SwiftBank | Started 06:15 UTC", 34],
  [2, "OPERATOR", "Operator Two", "Ahmed, checking TX-1102-BE with backoffice — SwiftBank cut-off is 18:00 UTC, I will confirm before that.", 30, { replyTo: 12 }],
  [2, "CLIENT", "Ahmed Karim", "We need it today. Also TX-9931-ZQ refund should NOT hit our main balance.", 22],
  [2, "BOT", "AcmePayout Bot", "Transaction: TX-9931-ZQ | Status: REFUNDED | Amount: 310.00 USD | Provider: CryptoBridge", 20],
  [2, "CLIENT", "Ahmed Karim", "escalating this to your account manager, this is the third delayed settlement this week and we are losing merchants", 8, { deleted: true }],
  [2, "CLIENT", "Ahmed Karim", "Sending the settlement report to you in a minute.", 2],

  [3, "BOT", "GatewayMonitor", "Chargeback opened: TX-7781-AC | Amount: 240.50 EUR | Reason: Fraudulent — cardholder denies transaction", 60],
  [3, "CLIENT", "Maria Santos", "We got the chargeback notice for TX-7781-AC. Do we have proof of delivery?", 55],
  [3, "OPERATOR", "Operator Two", "Maria, I will attach the 3DS log and delivery receipt for TX-7781-AC to the dispute packet.", 50, { replyTo: 19 }],
  [3, "CLIENT", "Maria Santos", "Deadline for the dispute is tomorrow 09:00 UTC.", 47],

  [4, "CLIENT", "Liu Wei", "KYC documents for the new UBO have been uploaded. Please review.", 140],
  [4, "OPERATOR", "Operator Two", "Received, Liu. Compliance will review within one business day.", 135, { replyTo: 22 }],
  [4, "BOT", "AcmePayout Bot", "KYC status changed: MERCHANT MRC-DELTA -> UNDER_REVIEW", 130],

  [5, "CLIENT", "Jane Doe", "Refund TX-9931-ZQ processed but the customer says they did not receive it.", 300],
  [5, "BOT", "AcmePayout Bot", "Transaction: TX-9931-ZQ | Status: REFUNDED | Amount: 310.00 USD | Provider: CryptoBridge | Ref: RFD-88213", 298],
  [5, "OPERATOR", "Operator Two", "Customer refund settled on-chain, hash 0x91ac… proved. Sharing the receipt now.", 290, { replyTo: 25 }],

  [6, "CLIENT", "Ahmed Karim", "Our webhook endpoint returns 500 on payout callbacks. Signature verification fails.", 420],
  [6, "OPERATOR", "Operator Two", "Please check your clock skew (recommend NTP) — signatures expire after 5 minutes.", 410, { replyTo: 28 }],
  [6, "CLIENT", "Ahmed Karim", "We pinned NTP. Retry for TX-1102-BE failed again.", 400],
];

function buildMessages(chatId: number): Message[] {
  const specs = MESSAGE_SPECS.filter((spec) => spec[0] === chatId);
  const localToGlobal = new Map<number, number>();
  let tgCounter = chatId * 1000 + 90000;

  return specs.map((spec, index) => {
    const [, senderType, senderName, text, minutesAgo, extra] = spec;
    const id = chatId * 100 + index + 1;
    if (extra?.replyTo !== undefined) localToGlobal.set(index + 1, extra.replyTo);
    tgCounter += 1;
    const isOutgoing = senderType === "OPERATOR";
    return {
      id,
      chat_id: chatId,
      tg_msg_id: tgCounter,
      sender_type: senderType,
      sender_operator_id: isOutgoing ? 2 : null,
      sender_name: senderName,
      sender_tg_id: senderType === "BOT" ? 500000001 : senderType === "OPERATOR" ? 900001 : 700000000 + index,
      text,
      reply_to_id: null,
      reply_to_tg_msg_id: null,
      phone: extra?.phone ?? null,
      is_deleted_in_tg: extra?.deleted ?? false,
      deleted_at: extra?.deleted ? iso(-minutesAgo * 60 + 60) : null,
      sla_incident_id: null,
      delivery_status: isOutgoing ? "sent" : "sent",
      is_outgoing: isOutgoing,
      created_at: iso(-minutesAgo * 60),
    } satisfies Message;
  }).map((message, index, all) => {
    const spec = specs[index];
    const replyLocal = spec?.[5]?.replyTo;
    if (replyLocal === undefined) return message;
    const target = all[replyLocal - 1];
    return {
      ...message,
      reply_to_id: target?.id ?? null,
      reply_to_tg_msg_id: target?.tg_msg_id ?? null,
    } satisfies Message;
  });
}

export interface MockDataset {
  chats: ChatListItem[];
  messagesByChat: Record<number, Message[]>;
}

function buildChats(messagesByChat: Record<number, Message[]>): ChatListItem[] {
  return CHAT_SPECS.map((spec) => {
    const messages = messagesByChat[spec.id] ?? [];
    const last = messages[messages.length - 1] ?? null;
    return {
      id: spec.id,
      tg_chat_id: -1001234567880 - spec.id,
      title: spec.title,
      username: spec.username,
      merchant_code: spec.merchant_code,
      unread_count: spec.unread,
      assigned_operator_id: spec.assigned,
      assigned_operator_name:
        spec.assigned === null ? null : (MOCK_OPERATORS.find((op) => op.id === spec.assigned)?.display_name ?? null),
      last_message: last
        ? {
            id: last.id,
            text: last.text,
            sender_name: last.sender_name,
            is_outgoing: last.is_outgoing,
            created_at: last.created_at,
          }
        : null,
      sla:
        spec.slaRemaining === null
          ? null
          : {
              status: "PENDING",
              deadline_at: iso(spec.slaRemaining),
              remaining_seconds: spec.slaRemaining,
              is_overdue: Boolean(spec.overdue),
            },
    } satisfies ChatListItem;
  });
}

export function createMockDataset(): MockDataset {
  const messagesByChat: Record<number, Message[]> = {};
  for (const spec of CHAT_SPECS) {
    messagesByChat[spec.id] = buildMessages(spec.id);
  }
  return { chats: buildChats(messagesByChat), messagesByChat };
}

/* ---------------------------------------------------------- tx lookups */

interface TxFixture {
  admin_one: Omit<TxPanelResult, "panel">;
  admin_two: Omit<TxPanelResult, "panel">;
}

const TX_FIXTURES: Record<string, TxFixture> = {
  "TX-4412-DD": {
    admin_one: {
      found: true,
      tx_id: "TX-4412-DD",
      status: "FAILED",
      amount: "1500.00",
      currency: "USDT",
      provider: "AcmePayout",
      error_reason: "Insufficient provider balance",
      external_ref: "gw_7f3a91",
      raw: { trace_id: "gw_7f3a91", state: "FAILED", retries: 2 },
    },
    admin_two: {
      found: true,
      tx_id: "TX-4412-DD",
      status: "DECLINED",
      amount: "1500.00",
      currency: "USDT",
      provider: "AcmePayout",
      error_reason: "Bank rejected: insufficient funds on merchant settlement account",
      external_ref: "bk_2c41aa",
      raw: { trace_id: "bk_2c41aa", state: "DECLINED" },
    },
  },
  "TX-7781-AC": {
    admin_one: {
      found: true,
      tx_id: "TX-7781-AC",
      status: "COMPLETED",
      amount: "240.50",
      currency: "EUR",
      provider: "PayGate",
      error_reason: null,
      external_ref: "gw_b10e77",
      raw: { trace_id: "gw_b10e77", state: "COMPLETED" },
    },
    admin_two: {
      found: true,
      tx_id: "TX-7781-AC",
      status: "PROCESSING",
      amount: "240.50",
      currency: "EUR",
      provider: "PayGate",
      error_reason: null,
      external_ref: "bk_55d10c",
      raw: { trace_id: "bk_55d10c", state: "PROCESSING" },
    },
  },
  "TX-1102-BE": {
    admin_one: {
      found: true,
      tx_id: "TX-1102-BE",
      status: "PROCESSING",
      amount: "8400.00",
      currency: "USDT",
      provider: "SwiftBank",
      error_reason: null,
      external_ref: "gw_aa0192",
      raw: { trace_id: "gw_aa0192", state: "PROCESSING" },
    },
    admin_two: {
      found: true,
      tx_id: "TX-1102-BE",
      status: "PENDING",
      amount: "8400.00",
      currency: "USDT",
      provider: "SwiftBank",
      error_reason: null,
      external_ref: "bk_77c3e1",
      raw: { trace_id: "bk_77c3e1", state: "PENDING" },
    },
  },
  "TX-9931-ZQ": {
    admin_one: {
      found: true,
      tx_id: "TX-9931-ZQ",
      status: "REFUNDED",
      amount: "310.00",
      currency: "USD",
      provider: "CryptoBridge",
      error_reason: null,
      external_ref: "gw_44fb09",
      raw: { trace_id: "gw_44fb09", state: "REFUNDED", refund_ref: "RFD-88213" },
    },
    admin_two: {
      found: true,
      tx_id: "TX-9931-ZQ",
      status: "REFUNDED",
      amount: "310.00",
      currency: "USD",
      provider: "CryptoBridge",
      error_reason: null,
      external_ref: "bk_91ac34",
      raw: { trace_id: "bk_91ac34", state: "REFUNDED" },
    },
  },
};

const STATUS_POOL = ["COMPLETED", "PROCESSING", "PENDING", "FAILED", "DECLINED", "REFUNDED"] as const;
const PROVIDERS = ["AcmePayout", "PayGate", "SwiftBank", "CryptoBridge", "NovaPay"];
const CURRENCIES = ["USDT", "EUR", "USD", "GBP"];

function hashOf(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) % 1_000_003;
  }
  return hash;
}

function buildPanelResult(txId: string, panel: TxAdminPanel): TxPanelResult {
  const fixture = TX_FIXTURES[txId];
  if (fixture) {
    return { ...(panel === "ADMIN_ONE" ? fixture.admin_one : fixture.admin_two), panel };
  }
  if (txId.startsWith("TX-0000")) {
    return {
      panel,
      found: false,
      tx_id: txId,
      status: "NOT_FOUND",
      amount: null,
      currency: null,
      provider: null,
      error_reason: "No transaction with this id in this system",
      external_ref: null,
      raw: {},
    };
  }
  const seed = hashOf(`${txId}:${panel}`);
  const status = STATUS_POOL[seed % STATUS_POOL.length];
  const provider = PROVIDERS[seed % PROVIDERS.length];
  const currency = CURRENCIES[(seed >> 3) % CURRENCIES.length];
  const amount = (((seed % 250000) / 100) + 5).toFixed(2);
  const failed = status === "FAILED" || status === "DECLINED";
  return {
    panel,
    found: true,
    tx_id: txId,
    status,
    amount,
    currency,
    provider,
    error_reason: failed ? "Provider declined: risk limit exceeded" : null,
    external_ref: `${panel === "ADMIN_ONE" ? "gw" : "bk"}_${seed.toString(16).padStart(6, "0").slice(0, 6)}`,
    raw: { trace_id: `${panel === "ADMIN_ONE" ? "gw" : "bk"}_${seed}`, state: status, provider },
  };
}

/** Deterministic mock of `POST /api/tx/lookup` — mirrors the backend adapter. */
export function mockLookupTx(rawTxId: string): TxLookupResponse {
  const txId = rawTxId.trim().toUpperCase();
  return {
    tx_id: txId,
    looked_up_at: new Date().toISOString(),
    admin_one: buildPanelResult(txId, "ADMIN_ONE"),
    admin_two: buildPanelResult(txId, "ADMIN_TWO"),
  };
}

/** Build a Telegram-style quoted block for "Insert into reply". */
export function formatTxForReply(result: TxLookupResponse): string {
  const lines: string[] = [`📄 Transaction ${result.tx_id}`];
  const one = result.admin_one;
  if (one.found) {
    lines.push(
      `• Gateway: ${one.status}${one.error_reason ? ` (${one.error_reason})` : ""} — ${one.amount ?? "—"} ${one.currency ?? ""}`,
    );
  } else {
    lines.push("• Gateway: NOT FOUND");
  }
  const two = result.admin_two;
  if (two.found) {
    lines.push(
      `• Backoffice: ${two.status}${two.error_reason ? ` (${two.error_reason})` : ""} — ${two.amount ?? "—"} ${two.currency ?? ""}`,
    );
  } else {
    lines.push("• Backoffice: NOT FOUND");
  }
  const provider = one.provider ?? two.provider;
  if (provider) lines.push(`• Provider: ${provider}`);
  return lines.join("\n");
}

/** Swap tx ids in a text for the click-to-search token list (UI sugar). */
export function txIdsIn(text: string): string[] {
  return extractTxIds(text);
}

export const MOCK_NONCE = (): string => uuid();
