"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export type EquityPoint = { date: string; equity: number };

export function EquityChart({ data, height = 288 }: { data: EquityPoint[]; height?: number }) {
  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3} />
              <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="date" fontSize={12} minTickGap={24} />
          <YAxis fontSize={12} domain={["auto", "auto"]} width={70} />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
            formatter={(v: number) => v.toLocaleString("pt-BR", { maximumFractionDigits: 2 })}
          />
          <Area type="monotone" dataKey="equity" stroke="#22c55e" fill="url(#eq)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
