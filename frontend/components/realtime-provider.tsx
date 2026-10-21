"use client";

import { createContext, useContext, useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import { useAuth } from "@/components/auth-provider";
import { cn } from "@/lib/utils";
import {
  RealtimeClient,
  type ConnectionStatus,
  type KlineMsg,
  type Net,
  type RealtimeMessage,
  type RealtimeSnapshot,
} from "@/lib/realtime";

const RealtimeContext = createContext<RealtimeClient | null>(null);

const EMPTY: RealtimeSnapshot = {
  status: "idle",
  broker: null,
  net: "main",
  tickers: {},
  marks: {},
  lastMessageAt: null,
  attempts: 0,
};

export function RealtimeProvider({ children }: { children: React.ReactNode }) {
  const { workspace } = useAuth();
  const workspaceId = workspace?.id ?? null;
  const client = useMemo(() => (workspaceId ? new RealtimeClient(workspaceId) : null), [workspaceId]);

  useEffect(() => {
    if (!client) return;
    client.start();
    const onVisible = () => document.visibilityState === "visible" && client.reconnectNow();
    const onOnline = () => client.reconnectNow();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      client.stop();
    };
  }, [client]);

  return <RealtimeContext.Provider value={client}>{children}</RealtimeContext.Provider>;
}

export function useRealtimeClient(): RealtimeClient | null {
  return useContext(RealtimeContext);
}

const noopSubscribe = () => () => undefined;

export function useRealtime(): RealtimeSnapshot {
  const client = useRealtimeClient();
  return useSyncExternalStore(
    client?.subscribe ?? noopSubscribe,
    client?.getSnapshot ?? (() => EMPTY),
    () => EMPTY
  );
}

/** Subscribe to tickers/mark prices of `symbols` while the component is mounted. */
export function useWatchSymbols(symbols: string[]) {
  const client = useRealtimeClient();
  const key = symbols.map((s) => s.toUpperCase()).sort().join(",");
  useEffect(() => {
    if (!client || !key) return;
    return client.watchSymbols(key.split(","));
  }, [client, key]);
}

export function useNet(net: Net | null) {
  const client = useRealtimeClient();
  useEffect(() => {
    if (client && net) client.setNet(net);
  }, [client, net]);
}

/** Call `handler` for every real-time message of the given types (stable across re-renders). */
export function useRealtimeEvent(types: string[], handler: (msg: RealtimeMessage) => void) {
  const client = useRealtimeClient();
  const ref = useRef(handler);
  ref.current = handler;
  const key = types.join(",");
  useEffect(() => {
    if (!client) return;
    const offs = key.split(",").map((t) => client.on(t, (m) => ref.current(m)));
    return () => offs.forEach((off) => off());
  }, [client, key]);
}

export function useKlineStream(symbol: string, interval: string, onKline: (k: KlineMsg) => void) {
  const client = useRealtimeClient();
  const ref = useRef(onKline);
  ref.current = onKline;
  useEffect(() => {
    if (!client || !symbol) return;
    const unwatch = client.watchKlines(symbol, interval);
    const off = client.on("kline", (m) => {
      const k = m.data as KlineMsg;
      if (k.s === symbol.toUpperCase() && k.i === interval) ref.current(k);
    });
    return () => {
      off();
      unwatch();
    };
  }, [client, symbol, interval]);
}

const STATUS_LABEL: Record<ConnectionStatus, string> = {
  idle: "Desconectado",
  connecting: "Conectando…",
  open: "Tempo real",
  reconnecting: "Reconectando…",
  too_many: "Muitas abas abertas",
  unauthorized: "Sessão expirada",
  disabled: "Tempo real desativado",
  error: "Sem acesso",
};

export function ConnectionIndicator({ className }: { className?: string }) {
  const { status, attempts, broker } = useRealtime();
  const client = useRealtimeClient();
  const color =
    status === "open"
      ? "bg-green-500"
      : status === "connecting" || status === "reconnecting" || status === "too_many"
        ? "bg-yellow-500 animate-pulse"
        : "bg-red-500";
  const title =
    status === "open"
      ? `Conectado (${broker === "redis" ? "Redis" : "processo local"})`
      : status === "reconnecting"
        ? `Tentativa ${attempts}; clique para reconectar agora`
        : status === "too_many"
          ? "Limite de conexões em tempo real por usuário atingido (cada aba aberta usa uma). Feche outras abas; nova tentativa em 30 s ou clique para tentar agora."
        : STATUS_LABEL[status];
  return (
    <button
      type="button"
      onClick={() => client?.reconnectNow()}
      title={title}
      className={cn("inline-flex items-center gap-1.5 text-xs text-muted-foreground", className)}
      aria-live="polite"
    >
      <span className={cn("h-2 w-2 rounded-full", color)} aria-hidden />
      {STATUS_LABEL[status]}
    </button>
  );
}
