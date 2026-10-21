"use client";

import { useEffect, useState } from "react";

import { ConnectionIndicator, useRealtime, useWatchSymbols } from "@/components/realtime-provider";
import { cn } from "@/lib/utils";

const WATCHLIST_KEY = "btp.watchlist";
const DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"];

export function formatPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const digits = v >= 1000 ? 2 : v >= 1 ? 4 : 6;
  return v.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: digits });
}

function loadWatchlist(): string[] {
  if (typeof window === "undefined") return DEFAULT_WATCHLIST;
  try {
    const raw = JSON.parse(window.localStorage.getItem(WATCHLIST_KEY) ?? "null");
    if (Array.isArray(raw) && raw.every((s) => typeof s === "string")) return raw.slice(0, 20);
  } catch {
    /* ignore */
  }
  return DEFAULT_WATCHLIST;
}

export function TickerBar() {
  const [symbols, setSymbols] = useState<string[]>(DEFAULT_WATCHLIST);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const { tickers, net } = useRealtime();
  useWatchSymbols(symbols);

  useEffect(() => setSymbols(loadWatchlist()), []);

  const save = () => {
    const list = draft
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter((s) => /^[A-Z0-9]{2,20}$/.test(s))
      .map((s) => (s.endsWith("USDT") ? s : `${s}USDT`))
      .slice(0, 20);
    const next = list.length ? Array.from(new Set(list)) : DEFAULT_WATCHLIST;
    window.localStorage.setItem(WATCHLIST_KEY, JSON.stringify(next));
    setSymbols(next);
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
            {symbols.map((s) => {
              const t = tickers[s];
              const change = t?.P ?? null;
              return (
                <div key={s} className="flex shrink-0 items-baseline gap-1.5 tabular-nums">
                  <span className="font-medium">{s.replace(/USDT$/, "")}</span>
                  <span>{formatPrice(t?.c)}</span>
                  <span className={cn(change === null ? "" : change >= 0 ? "text-green-500" : "text-red-500")}>
                    {change === null ? "" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}
                  </span>
                </div>
              );
            })}
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
