"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export const SERIES_COLORS = ["#22c55e", "#3b82f6", "#f59e0b", "#a855f7", "#ec4899", "#14b8a6", "#f97316", "#94a3b8"];

const LOSS_COLOR = "#ef4444";
const money = (v: number) => v.toLocaleString("pt-BR", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const tooltipStyle = { background: "#0f172a", border: "1px solid #334155" };

export type PnlBarRow = Record<string, number | string> & { period: string };

export function PnlBarChart({ data, series, height = 300 }: { data: PnlBarRow[]; series: string[]; height?: number }) {
  return (
    <div className="w-full" style={{ height }} role="img" aria-label="Gráfico de barras do PnL líquido por período">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="period" fontSize={12} minTickGap={16} />
          <YAxis fontSize={12} width={80} tickFormatter={(v: number) => v.toLocaleString("pt-BR")} />
          <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => money(v)} />
          {series.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
          <ReferenceLine y={0} stroke="#64748b" />
          {series.length === 1 ? (
            // Single series: green for gains, red for losses.
            <Bar dataKey={series[0]} isAnimationActive={false}>
              {data.map((row) => (
                <Cell key={row.period} fill={Number(row[series[0]]) < 0 ? LOSS_COLOR : SERIES_COLORS[0]} />
              ))}
            </Bar>
          ) : (
            series.map((s, i) => (
              <Bar key={s} dataKey={s} stackId="pnl" fill={SERIES_COLORS[i % SERIES_COLORS.length]} isAnimationActive={false} />
            ))
          )}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function CumulativeChart({ data, height = 220 }: { data: { period: string; acumulado: number }[]; height?: number }) {
  return (
    <div className="w-full" style={{ height }} role="img" aria-label="Gráfico do PnL líquido acumulado">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="period" fontSize={12} minTickGap={16} />
          <YAxis fontSize={12} width={80} tickFormatter={(v: number) => v.toLocaleString("pt-BR")} />
          <Tooltip contentStyle={tooltipStyle} formatter={(v: number) => money(v)} />
          <ReferenceLine y={0} stroke="#64748b" />
          <Line type="monotone" dataKey="acumulado" name="PnL líquido acumulado" stroke="#22c55e" dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
