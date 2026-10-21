// Real-time client for /api/v1/ws.
// - Auth: the httpOnly access-token cookie is sent by the browser on the upgrade request.
// - Reconnects with exponential backoff + jitter; on 4401 (expired token) it refreshes the session first.
// - Detects stale connections (no message for 3 heartbeats) and reconnects.
// - Subscriptions (symbols/klines) are reference-counted across components and re-sent after reconnecting.
import { API_URL, tryRefresh } from "./api";

export type ConnectionStatus = "idle" | "connecting" | "open" | "reconnecting" | "unauthorized" | "disabled" | "error";
export type Net = "main" | "test";

export interface Ticker {
  s: string;
  c: number | null; // last price
  o: number | null;
  h: number | null;
  l: number | null;
  q: number | null; // 24h quote volume
  P: number | null; // 24h change %
}

export interface Mark {
  s: string;
  p: number | null; // mark price
  r: number | null; // funding rate
  T: number | null; // next funding time (ms)
}

export interface KlineMsg {
  s: string;
  i: string;
  t: number;
  T: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
  x: boolean;
}

export interface RealtimeMessage<T = unknown> {
  type: string;
  ts: number;
  data: T;
  net?: Net;
}

export interface RealtimeSnapshot {
  status: ConnectionStatus;
  broker: string | null;
  net: Net;
  tickers: Record<string, Ticker>;
  marks: Record<string, Mark>;
  lastMessageAt: number | null;
  attempts: number;
}

type Listener = (msg: RealtimeMessage) => void;

const MAX_BACKOFF_MS = 30_000;
const CLOSE_UNAUTHORIZED = 4401;
const CLOSE_FORBIDDEN = 4403;
const CLOSE_NOT_FOUND = 4404;
const CLOSE_TOO_MANY = 4429;
const CLOSE_DISABLED = 1013;

export function wsUrl(workspaceId: string): string {
  const explicit = process.env.NEXT_PUBLIC_WS_URL;
  let base: string;
  if (explicit) {
    base = explicit;
  } else if (/^https?:\/\//.test(API_URL)) {
    base = `${API_URL.replace(/^http/, "ws").replace(/\/$/, "")}/ws`;
  } else {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    base = `${proto}//${window.location.host}${API_URL.replace(/\/$/, "")}/ws`;
  }
  return `${base}${base.includes("?") ? "&" : "?"}workspace_id=${encodeURIComponent(workspaceId)}`;
}

export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(attempt, 5));
  return Math.round(base / 2 + random() * (base / 2)); // "equal jitter"
}

export class RealtimeClient {
  private ws: WebSocket | null = null;
  private snapshot: RealtimeSnapshot;
  private subscribers = new Set<() => void>();
  private listeners = new Map<string, Set<Listener>>();
  private symbolRefs = new Map<string, number>();
  private klineRefs = new Map<string, number>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private staleTimer: ReturnType<typeof setInterval> | null = null;
  private subscribeTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatMs = 15_000;
  private stopped = false;
  private lastSeen = 0;

  constructor(private readonly workspaceId: string) {
    this.snapshot = {
      status: "idle",
      broker: null,
      net: "main",
      tickers: {},
      marks: {},
      lastMessageAt: null,
      attempts: 0,
    };
  }

  // ---------------------------------------------------------------- store (useSyncExternalStore)
  getSnapshot = (): RealtimeSnapshot => this.snapshot;

  subscribe = (cb: () => void): (() => void) => {
    this.subscribers.add(cb);
    return () => this.subscribers.delete(cb);
  };

  private set(patch: Partial<RealtimeSnapshot>) {
    this.snapshot = { ...this.snapshot, ...patch };
    this.subscribers.forEach((cb) => cb());
  }

  on(type: string, listener: Listener): () => void {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
    return () => this.listeners.get(type)?.delete(listener);
  }

  // ---------------------------------------------------------------- subscriptions
  setNet(net: Net) {
    if (net === this.snapshot.net) return;
    this.set({ net, tickers: {}, marks: {} });
    this.scheduleSubscribe();
  }

  watchSymbols(symbols: string[]): () => void {
    const list = symbols.map((s) => s.toUpperCase()).filter(Boolean);
    list.forEach((s) => this.symbolRefs.set(s, (this.symbolRefs.get(s) ?? 0) + 1));
    this.scheduleSubscribe();
    return () => {
      list.forEach((s) => {
        const n = (this.symbolRefs.get(s) ?? 1) - 1;
        if (n <= 0) this.symbolRefs.delete(s);
        else this.symbolRefs.set(s, n);
      });
      this.scheduleSubscribe();
    };
  }

  watchKlines(symbol: string, interval: string): () => void {
    const key = `${symbol.toUpperCase()}:${interval}`;
    this.klineRefs.set(key, (this.klineRefs.get(key) ?? 0) + 1);
    this.scheduleSubscribe();
    return () => {
      const n = (this.klineRefs.get(key) ?? 1) - 1;
      if (n <= 0) this.klineRefs.delete(key);
      else this.klineRefs.set(key, n);
      this.scheduleSubscribe();
    };
  }

  private scheduleSubscribe() {
    if (this.subscribeTimer) clearTimeout(this.subscribeTimer);
    this.subscribeTimer = setTimeout(() => this.sendSubscribe(), 50);
  }

  private sendSubscribe() {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const klines = Array.from(this.klineRefs.keys()).map((k) => {
      const [symbol, interval] = k.split(":");
      return { symbol, interval };
    });
    this.ws.send(
      JSON.stringify({ op: "subscribe", net: this.snapshot.net, symbols: Array.from(this.symbolRefs.keys()), klines })
    );
  }

  // ---------------------------------------------------------------- connection
  start() {
    this.stopped = false;
    if (!this.ws) this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.staleTimer) clearInterval(this.staleTimer);
    if (this.subscribeTimer) clearTimeout(this.subscribeTimer);
    const ws = this.ws;
    this.ws = null;
    ws?.close(1000);
    this.set({ status: "idle" });
  }

  private connect() {
    if (this.stopped) return;
    this.set({ status: this.snapshot.attempts > 0 ? "reconnecting" : "connecting" });
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl(this.workspaceId));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onmessage = (ev) => this.handle(ev.data);
    ws.onclose = (ev) => {
      if (this.ws !== ws) return; // superseded
      this.ws = null;
      if (this.staleTimer) clearInterval(this.staleTimer);
      void this.handleClose(ev.code);
    };
    ws.onerror = () => {
      /* onclose follows */
    };
  }

  private async handleClose(code: number) {
    if (this.stopped) return;
    if (code === CLOSE_UNAUTHORIZED) {
      if (await tryRefresh()) {
        this.set({ attempts: 0 });
        this.connect();
      } else {
        this.set({ status: "unauthorized" });
      }
      return;
    }
    if (code === CLOSE_FORBIDDEN || code === CLOSE_NOT_FOUND) {
      this.set({ status: "error" });
      return;
    }
    if (code === CLOSE_DISABLED) {
      this.set({ status: "disabled" });
      return;
    }
    this.scheduleReconnect(code === CLOSE_TOO_MANY ? MAX_BACKOFF_MS : undefined);
  }

  private scheduleReconnect(delay?: number) {
    if (this.stopped) return;
    const attempts = this.snapshot.attempts + 1;
    this.set({ status: "reconnecting", attempts });
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), delay ?? backoffDelay(attempts - 1));
  }

  /** Reconnect now (e.g. "reconectar" button or the tab becoming visible again). */
  reconnectNow() {
    if (this.stopped) return;
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.set({ attempts: 0 });
    this.connect();
  }

  private startStaleWatch() {
    if (this.staleTimer) clearInterval(this.staleTimer);
    this.staleTimer = setInterval(() => {
      const last = this.lastSeen;
      if (Date.now() - last > this.heartbeatMs * 3 && this.ws) {
        const ws = this.ws;
        this.ws = null;
        ws.close(4000);
        this.scheduleReconnect();
      }
    }, 5_000);
  }

  private handle(raw: unknown) {
    if (typeof raw !== "string") return;
    let msg: RealtimeMessage;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    const now = Date.now();
    this.lastSeen = now;
    switch (msg.type) {
      case "hello": {
        const d = msg.data as { broker?: string; heartbeat_seconds?: number };
        this.heartbeatMs = (d.heartbeat_seconds ?? 15) * 1000;
        this.set({ status: "open", attempts: 0, broker: d.broker ?? null, lastMessageAt: now });
        this.startStaleWatch();
        this.sendSubscribe();
        break;
      }
      case "tickers": {
        const tickers = { ...this.snapshot.tickers };
        for (const t of msg.data as Ticker[]) tickers[t.s] = t;
        this.set({ tickers, lastMessageAt: now });
        break;
      }
      case "mark": {
        const marks = { ...this.snapshot.marks };
        for (const m of msg.data as Mark[]) marks[m.s] = m;
        this.set({ marks, lastMessageAt: now });
        break;
      }
      default:
        break; // heartbeats/events: no store update (no re-render); listeners below
    }
    this.listeners.get(msg.type)?.forEach((l) => l(msg));
    this.listeners.get("*")?.forEach((l) => l(msg));
  }
}
