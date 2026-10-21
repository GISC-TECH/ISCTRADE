"use client";

// recharts is ~100 kB of First Load JS; load it in a separate chunk after the page shell renders.
import dynamic from "next/dynamic";

export const EquityChart = dynamic(() => import("@/components/equity-chart").then((m) => m.EquityChart), {
  ssr: false,
  loading: () => <div className="h-72 w-full animate-pulse rounded bg-muted/30" />,
});
