"use client";

import { useState, useSyncExternalStore } from "react";

import { ConnectionIndicator, useRealtimeNet, useTicker, useWatchSymbols } from "@/components/realtime-provider";
import { cn } from "@/lib/utils";

const WATCHLIST_KEY = "btp.watchlist";
const DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"];

export function formatPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const digits = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
  return v.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: digits });
}

function parseWatchlist(value: string | null): string[] {
  try {
    const raw = JSON.parse(value ?? "null");
    if (Array.isArray(raw) && raw.every((s) => typeof s === "string")) return raw.slice(0, 20);
  } catch {
    /* ignore */
  }
  return DEFAULT_WATCHLIST;
}

// Watchlist persisted in localStorage, read through useSyncExternalStore (no setState-in-effect, SSR-safe).
const watchlistListeners = new Set<() => void>();
let watchlistCache: { raw: string | null; list: string[] } = { raw: null, list: DEFAULT_WATCHLIST };

function subscribeWatchlist(cb: () => void) {
  watchlistListeners.add(cb);
  window.addEventListener("storage", cb);
  return () => {
    watchlistListeners.delete(cb);
    window.removeEventListener("storage", cb);
  };
}

function getWatchlist(): string[] {
  const raw = window.localStorage.getItem(WATCHLIST_KEY);
  if (raw !== watchlistCache.raw) watchlistCache = { raw, list: parseWatchlist(raw) };
  return watchlistCache.list;
}

function saveWatchlist(list: string[]) {
  window.localStorage.setItem(WATCHLIST_KEY, JSON.stringify(list));
  watchlistListeners.forEach((cb) => cb());
}

/** One quote; subscribes to its own symbol so a message re-renders only the symbols it changed. */
function TickerItem({ symbol }: { symbol: string }) {
  const t = useTicker(symbol);
  const change = t?.P ?? null;
  return (
    <div className="flex shrink-0 items-baseline gap-1.5 tabular-nums">
      <span className="font-medium">{symbol.replace(/USDT$/, "")}</span>
      <span>{formatPrice(t?.c)}</span>
      <span className={cn(change === null ? "" : change >= 0 ? "text-green-500" : "text-red-500")}>
        {change === null ? "" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}
      </span>
    </div>
  );
}

export function TickerBar() {
  const symbols = useSyncExternalStore(subscribeWatchlist, getWatchlist, () => DEFAULT_WATCHLIST);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const net = useRealtimeNet();
  useWatchSymbols(symbols);

  const save = () => {
    const list = draft
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter((s) => /^[A-Z0-9]{2,20}$/.test(s))
      .map((s) => (s.endsWith("USDT") ? s : `${s}USDT`))
      .slice(0, 20);
    const next = list.length ? Array.from(new Set(list)) : DEFAULT_WATCHLIST;
    saveWatchlist(next);
    setEditing(false);
  };

  return (
    <div className="mb-4 flex items-center gap-3 overflow-x-auto rounded-md border bg-card px-3 py-2 text-xs">
      <ConnectionIndicator className="shrink-0" />
      {net === "test" && (
        <span className="shrink-0 rounded bg-yellow-500/20 px-1.5 py-0.5 text-[10px] uppercase">testnet</span>
      )}
      {editing ? (
        <form
          className="flex flex-1 items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <input
            autoFocus
            className="h-7 flex-1 rounded border bg-background px-2"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Símbolos da barra de cotações"
            placeholder="BTCUSDT, ETHUSDT, …"
          />
          <button type="submit" className="text-primary">
            salvar
          </button>
        </form>
      ) : (
        <>
          <div className="flex flex-1 gap-4">
            {symbols.map((s) => (
              <TickerItem key={s} symbol={s} />
            ))}
          </div>
          <button
            type="button"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={() => {
              setDraft(symbols.join(", "));
              setEditing(true);
            }}
          >
            editar
          </button>
        </>
      )}
    </div>
  );
}
