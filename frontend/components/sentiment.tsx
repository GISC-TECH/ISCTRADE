import { Badge } from "@/components/ui/badge";
import type { Impact } from "@/lib/types";

export function sentimentLabel(v: number | null | undefined): { label: string; variant: "success" | "destructive" | "secondary" } {
  if (v === null || v === undefined) return { label: "sem análise", variant: "secondary" };
  if (v >= 0.15) return { label: `positivo ${v.toFixed(2)}`, variant: "success" };
  if (v <= -0.15) return { label: `negativo ${v.toFixed(2)}`, variant: "destructive" };
  return { label: `neutro ${v.toFixed(2)}`, variant: "secondary" };
}

export function SentimentBadge({ value }: { value: number | null | undefined }) {
  const s = sentimentLabel(value);
  return <Badge variant={s.variant}>{s.label}</Badge>;
}

const IMPACT: Record<string, { label: string; variant: "destructive" | "warning" | "secondary" | "outline" }> = {
  high: { label: "alto impacto", variant: "destructive" },
  medium: { label: "médio impacto", variant: "warning" },
  low: { label: "baixo impacto", variant: "secondary" },
  holiday: { label: "feriado", variant: "outline" },
};

export function ImpactBadge({ impact }: { impact: Impact | "holiday" | null | undefined }) {
  if (!impact) return null;
  const i = IMPACT[impact] ?? IMPACT.low;
  return <Badge variant={i.variant}>{i.label}</Badge>;
}

/** Semicircle gauge for a -1..1 score. */
export function SentimentGauge({ score, confidence, label }: { score: number; confidence: number; label: string }) {
  const clamped = Math.max(-1, Math.min(1, score));
  const angle = Math.PI * (1 - (clamped + 1) / 2); // pi (left, -1) .. 0 (right, +1)
  const x = 50 + 38 * Math.cos(angle);
  const y = 50 - 38 * Math.sin(angle);
  const color = clamped >= 0.15 ? "#16a34a" : clamped <= -0.15 ? "#dc2626" : "#a1a1aa";
  return (
    <div className="flex flex-col items-center" title={`sentimento ${clamped.toFixed(2)} · confiança ${confidence.toFixed(2)}`}>
      <svg viewBox="0 0 100 58" className="h-16 w-28" role="img" aria-label={`${label}: ${clamped.toFixed(2)}`}>
        <path d="M 12 50 A 38 38 0 0 1 88 50" fill="none" stroke="currentColor" strokeOpacity={0.15} strokeWidth={8} />
        <path d="M 12 50 A 38 38 0 0 1 31 17" fill="none" stroke="#dc2626" strokeOpacity={0.5} strokeWidth={8} />
        <path d="M 69 17 A 38 38 0 0 1 88 50" fill="none" stroke="#16a34a" strokeOpacity={0.5} strokeWidth={8} />
        <line x1={50} y1={50} x2={x} y2={y} stroke={color} strokeWidth={3} strokeLinecap="round" />
        <circle cx={50} cy={50} r={3} fill={color} />
      </svg>
      <div className="text-sm font-medium">{label}</div>
      <div className="text-xs" style={{ color }}>
        {clamped >= 0 ? "+" : ""}
        {clamped.toFixed(2)}
        <span className="text-muted-foreground"> · conf. {Math.round(confidence * 100)}%</span>
      </div>
    </div>
  );
}
