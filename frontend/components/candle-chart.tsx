"use client";

// Candlestick chart (TradingView lightweight-charts, Apache-2.0) with EMA overlays, trade markers and
// price lines for entry / stop loss / take profit. History from GET /market/klines, live candles via WebSocket.
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useCallback, useEffect, useRef, useState } from "react";

import { useKlineStream } from "@/components/realtime-provider";
import { api, errorMessage } from "@/lib/api";
import type { KlineMsg } from "@/lib/realtime";

export interface ChartTrade {
  time: string; // ISO
  side: "BUY" | "SELL";
  price: number;
  quantity: number;
  realized_pnl?: number;
}

export interface ChartLine {
  price: number;
  title: string;
  color: string;
  dashed?: boolean;
}

const EMA_COLORS: Record<number, string> = { 20: "#f59e0b", 50: "#3b82f6", 200: "#a855f7" };

export function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = [];
  const k = 2 / (period + 1);
  let prev: number | null = null;
  let sum = 0;
  values.forEach((v, i) => {
    if (i < period - 1) {
      sum += v;
      out.push(null);
    } else if (i === period - 1) {
      sum += v;
      prev = sum / period;
      out.push(prev);
    } else {
      prev = v * k + (prev as number) * (1 - k);
      out.push(prev);
    }
  });
  return out;
}

/**
 * EMA value at index `i` given the EMA at `i - 1` (O(1); O(period) only for the seed value).
 * Used for live candles so a kline update does not recompute/re-send the whole series.
 */
export function emaAt(closes: number[], i: number, period: number, prev: number | null): number | null {
  if (i < period - 1) return null;
  if (i === period - 1) {
    let sum = 0;
    for (let j = 0; j < period; j++) sum += closes[j];
    return sum / period;
  }
  if (prev === null) return null;
  const k = 2 / (period + 1);
  return closes[i] * k + prev * (1 - k);
}

const toTime = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v ? `hsl(${v})` : fallback;
}

export function CandleChart({
  symbol,
  interval,
  testnet = false,
  trades = [],
  lines = [],
  emas = [20, 50],
  height = 420,
}: {
  symbol: string;
  interval: string;
  testnet?: boolean;
  trades?: ChartTrade[];
  lines?: ChartLine[];
  emas?: number[];
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const emaRefs = useRef<Map<number, ISeriesApi<"Line">>>(new Map());
  const emaValuesRef = useRef<Map<number, (number | null)[]>>(new Map());
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const barsRef = useRef<CandlestickData<UTCTimestamp>[]>([]);
  // Which request finished last (and how): loading/error are derived from it, so the history effect only sets
  // state from its async callbacks.
  const historyKey = `${symbol}|${interval}|${testnet ? 1 : 0}`;
  const [loaded, setLoaded] = useState<{ key: string; error: string | null } | null>(null);
  const loading = loaded?.key !== historyKey;
  const error = loaded?.key === historyKey ? loaded.error : null;
  const emaKey = emas.join(",");

  // create / destroy the chart
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = createChart(el, {
      height,
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: cssVar("--muted-foreground", "#888"),
      },
      grid: {
        vertLines: { color: "rgba(128,128,128,0.12)" },
        horzLines: { color: "rgba(128,128,128,0.12)" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    candleRef.current = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderVisible: false,
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });
    markersRef.current = createSeriesMarkers(candleRef.current, []);
    const emaSeries = new Map<number, ISeriesApi<"Line">>();
    emaKey
      .split(",")
      .filter(Boolean)
      .map(Number)
      .forEach((p) =>
        emaSeries.set(
          p,
          chart.addSeries(LineSeries, {
            color: EMA_COLORS[p] ?? "#94a3b8",
            lineWidth: 1,
            priceLineVisible: false,
            lastValueVisible: false,
            crosshairMarkerVisible: false,
            title: `EMA ${p}`,
          })
        )
      );
    emaRefs.current = emaSeries;
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      markersRef.current = null;
      priceLinesRef.current = [];
      emaRefs.current = new Map();
      emaValuesRef.current = new Map();
    };
  }, [height, emaKey]);

  const refreshEmas = useCallback(() => {
    const bars = barsRef.current;
    const closes = bars.map((b) => b.close);
    emaRefs.current.forEach((series, period) => {
      const values = ema(closes, period);
      emaValuesRef.current.set(period, values);
      const data: LineData<UTCTimestamp>[] = [];
      values.forEach((v, i) => v !== null && data.push({ time: bars[i].time, value: v }));
      series.setData(data);
    });
  }, []);

  // incremental EMA for the last bar: O(1) per series instead of recomputing 500 points and
  // re-sending them with setData on every kline message
  const updateLastEma = useCallback(() => {
    const bars = barsRef.current;
    const i = bars.length - 1;
    if (i < 0) return;
    const closes = bars.map((b) => b.close);
    emaRefs.current.forEach((series, period) => {
      const values = emaValuesRef.current.get(period);
      if (!values || values.length < i) {
        refreshEmas();
        return;
      }
      values.length = i; // drop the in-progress value (same bar) before recomputing it
      const v = emaAt(closes, i, period, i > 0 ? values[i - 1] : null);
      values.push(v);
      if (v !== null) series.update({ time: bars[i].time, value: v });
    });
  }, [refreshEmas]);

  // history
  useEffect(() => {
    let cancelled = false;
    api
      .klines({ symbol, interval, limit: 500, testnet })
      .then((rows) => {
        if (cancelled || !candleRef.current) return;
        barsRef.current = rows.map((r) => ({ time: toTime(r.t), open: r.o, high: r.h, low: r.l, close: r.c }));
        candleRef.current.setData(barsRef.current);
        refreshEmas();
        chartRef.current?.timeScale().fitContent();
      })
      .then(() => !cancelled && setLoaded({ key: historyKey, error: null }))
      .catch((e) => !cancelled && setLoaded({ key: historyKey, error: errorMessage(e) }));
    return () => {
      cancelled = true;
    };
  }, [symbol, interval, testnet, emaKey, refreshEmas, historyKey]);

  // live candles
  useKlineStream(symbol, interval, (k: KlineMsg) => {
    const series = candleRef.current;
    if (!series || !barsRef.current.length) return;
    const bar = { time: toTime(k.t), open: k.o, high: k.h, low: k.l, close: k.c };
    const bars = barsRef.current;
    const last = bars[bars.length - 1];
    if (bar.time < last.time) return; // stale update
    if (bar.time === last.time) bars[bars.length - 1] = bar;
    else bars.push(bar);
    series.update(bar);
    updateLastEma();
  });

  // trade markers (entries/exits)
  useEffect(() => {
    const markers: SeriesMarker<Time>[] = trades
      .map((t) => ({ ...t, ts: Date.parse(t.time) }))
      .filter((t) => !Number.isNaN(t.ts))
      .sort((a, b) => a.ts - b.ts)
      .map((t) => {
        const exit = (t.realized_pnl ?? 0) !== 0;
        return {
          time: toTime(t.ts),
          position: t.side === "BUY" ? "belowBar" : "aboveBar",
          color: t.side === "BUY" ? "#22c55e" : "#ef4444",
          shape: t.side === "BUY" ? "arrowUp" : "arrowDown",
          text: `${exit ? "Saída" : "Entrada"} ${t.side === "BUY" ? "C" : "V"} ${t.quantity} @ ${t.price}`,
        } as SeriesMarker<Time>;
      });
    markersRef.current?.setMarkers(markers);
  }, [trades, loading]);

  // entry / SL / TP price lines
  const linesKey = JSON.stringify(lines);
  useEffect(() => {
    const series = candleRef.current;
    if (!series) return;
    priceLinesRef.current.forEach((pl) => series.removePriceLine(pl));
    priceLinesRef.current = (JSON.parse(linesKey) as ChartLine[])
      .filter((l) => Number.isFinite(l.price) && l.price > 0)
      .map((l) =>
        series.createPriceLine({
          price: l.price,
          color: l.color,
          lineWidth: 1,
          lineStyle: l.dashed ? LineStyle.Dashed : LineStyle.Solid,
          axisLabelVisible: true,
          title: l.title,
        })
      );
  }, [linesKey, loading]);

  return (
    <div className="relative w-full" style={{ height }}>
      <div ref={containerRef} className="absolute inset-0" />
      {loading && <div className="absolute left-2 top-2 text-xs text-muted-foreground">Carregando candles…</div>}
      {error && <div className="absolute left-2 top-2 text-xs text-red-500">Falha ao carregar candles: {error}</div>}
    </div>
  );
}
