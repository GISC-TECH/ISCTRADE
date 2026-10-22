"use client";

import dynamic from "next/dynamic";

const placeholder = (h: number) => {
  const Loading = () => <div className="w-full animate-pulse rounded bg-muted/30" style={{ height: h }} />;
  return Loading;
};

export const PnlBarChart = dynamic(() => import("@/components/pnl-charts").then((m) => m.PnlBarChart), {
  ssr: false,
  loading: placeholder(300),
});

export const CumulativeChart = dynamic(() => import("@/components/pnl-charts").then((m) => m.CumulativeChart), {
  ssr: false,
  loading: placeholder(220),
});
