/**
 * Reconnecting WebSocket client for `/ws` (CONTRACTS.md §4).
 *
 * Features
 * - Exponential backoff with jitter (1s -> 30s), infinite retries.
 * - Client heartbeat every 20s (`{"type":"HEARTBEAT"}`) so the server can
 *   apply the offline fallback; `PONG` latency is tracked for the top bar.
 * - Typed publish helpers for `TYPING_LOCK` / `TYPING_UNLOCK` / `PING`.
 * - Outbox buffer for events emitted while the socket is down.
 * - No-op safe: every method tolerates a closed socket.
 */

import type { ChatFilter, WsEnvelope } from "./types";

export type WsStatus = "idle" | "connecting" | "open" | "closed";

export interface WsClientOptions {
  url: string;
  token: string;
  onMessage: (envelope: WsEnvelope<unknown>) => void;
  onStatus: (status: WsStatus, meta: { attempt: number; latencyMs: number | null }) => void;
  heartbeatMs?: number;
  maxBackoffMs?: number;
}

type MessageHandler = (envelope: WsEnvelope<unknown>) => void;

export class WsClient {
  private socket: WebSocket | null = null;
  private readonly options: WsClientOptions;
  private handler: MessageHandler;
  private heartbeatTimer: number | null = null;
  private reconnectTimer: number | null = null;
  private attempt = 0;
  private closedByUser = false;
  private latencyMs: number | null = null;
  private pingSentAt: number | null = null;
  private readonly outbox: string[] = [];
  private status: WsStatus = "idle";
  private lastPongAt: number | null = null;

  constructor(options: WsClientOptions) {
    this.options = options;
    this.handler = options.onMessage;
  }

  get currentStatus(): WsStatus {
    return this.status;
  }

  /** Replace the message handler (used when the store re-registers). */
  setHandler(handler: MessageHandler): void {
    this.handler = handler;
  }

  /** Rotate the JWT (e.g. after a re-login) and force a reconnect. */
  setToken(token: string): void {
    this.options.token = token;
    this.reconnectNow();
  }

  connect(): void {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this.closedByUser = false;
    this.setStatus("connecting");

    const separator = this.options.url.includes("?") ? "&" : "?";
    const url = `${this.options.url}${separator}token=${encodeURIComponent(this.options.token)}`;

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.setStatus("open");
      this.startHeartbeat();
      this.flushOutbox();
    };

    socket.onmessage = (event: MessageEvent<string>) => {
      let envelope: WsEnvelope<unknown>;
      try {
        envelope = JSON.parse(event.data) as WsEnvelope<unknown>;
      } catch {
        return;
      }
      if (envelope?.type === "PONG") {
        this.lastPongAt = Date.now();
        if (this.pingSentAt !== null) {
          this.latencyMs = Date.now() - this.pingSentAt;
          this.pingSentAt = null;
          this.setStatus("open");
        }
        return;
      }
      if (envelope?.type === "CONNECTED") {
        this.setStatus("open");
      }
      try {
        this.handler(envelope);
      } catch (error) {
        console.error("[ws] handler failed", error);
      }
    };

    socket.onerror = () => {
      /* onclose always follows; nothing to do here. */
    };

    socket.onclose = (event: CloseEvent) => {
      this.stopHeartbeat();
      this.socket = null;
      this.setStatus("closed");
      if (this.closedByUser || event.code === 1008) {
        // 1008 = policy violation (bad token) — do not hammer the server.
        return;
      }
      this.scheduleReconnect();
    };
  }

  disconnect(): void {
    this.closedByUser = true;
    this.stopHeartbeat();
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      try {
        this.socket.close(1000, "client shutdown");
      } catch {
        /* ignore */
      }
      this.socket = null;
    }
    this.setStatus("closed");
  }

  /* ------------------------------------------------------------ publish */

  /** Raw send; buffers while the socket is down. Returns false when buffered. */
  private sendRaw(payload: Record<string, unknown>): boolean {
    const serialized = JSON.stringify(payload);
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(serialized);
      return true;
    }
    if (this.outbox.length > 50) this.outbox.shift();
    this.outbox.push(serialized);
    return false;
  }

  ping(): void {
    this.pingSentAt = Date.now();
    this.sendRaw({ type: "PING" });
  }

  heartbeat(): void {
    this.sendRaw({ type: "HEARTBEAT" });
  }

  /** Announce the collision lock (anti-double reply). */
  lockChat(chatId: number): void {
    this.sendRaw({ type: "TYPING_LOCK", chat_id: chatId });
  }

  unlockChat(chatId: number): void {
    this.sendRaw({ type: "TYPING_UNLOCK", chat_id: chatId });
  }

  subscribe(chatId: number): void {
    this.sendRaw({ type: "SUBSCRIBE", chat_id: chatId });
  }

  setFilter(filter: ChatFilter): void {
    this.sendRaw({ type: "SET_FILTER", filter });
  }

  /* ----------------------------------------------------------- internal */

  private flushOutbox(): void {
    while (this.outbox.length > 0 && this.socket && this.socket.readyState === WebSocket.OPEN) {
      const next = this.outbox.shift();
      if (next) this.socket.send(next);
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    const interval = this.options.heartbeatMs ?? 20_000;
    this.heartbeatTimer = window.setInterval(() => {
      this.heartbeat();
      this.ping();
    }, interval);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== null) {
      window.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private reconnectNow(): void {
    if (this.socket) {
      try {
        this.socket.close(4000, "token rotated");
      } catch {
        /* ignore */
      }
      this.socket = null;
    }
    this.connect();
  }

  private scheduleReconnect(): void {
    if (this.closedByUser || this.reconnectTimer !== null) return;
    const maxBackoff = this.options.maxBackoffMs ?? 30_000;
    const base = Math.min(maxBackoff, 1000 * 2 ** Math.min(this.attempt, 5));
    const jitter = Math.random() * 400;
    const delay = Math.round(base + jitter);
    this.attempt += 1;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private setStatus(status: WsStatus): void {
    this.status = status;
    try {
      this.options.onStatus(status, { attempt: this.attempt, latencyMs: this.latencyMs });
    } catch (error) {
      console.error("[ws] status handler failed", error);
    }
  }

  /** Age of the last successful pong, used by the connection indicator. */
  get lastPongAgeMs(): number | null {
    return this.lastPongAt === null ? null : Date.now() - this.lastPongAt;
  }
}
