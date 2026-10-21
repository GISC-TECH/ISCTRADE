"use client";

import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { BotPreset } from "@/lib/types";

const RISK_VARIANT = { conservador: "success", moderado: "secondary", agressivo: "warning" } as const;

/** Select of ready-made bots ("robôs prontos"). `value = ""` means manual configuration. */
export function PresetPicker({
  id,
  presets,
  value,
  onChange,
  label = "Robô pronto (opcional)",
}: {
  id: string;
  presets: BotPreset[];
  value: string;
  onChange: (preset: BotPreset | null) => void;
  label?: string;
}) {
  const selected = presets.find((p) => p.key === value);
  if (!presets.length) return null;
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <Select id={id} value={value} onChange={(e) => onChange(presets.find((p) => p.key === e.target.value) ?? null)}>
        <option value="">Configuração manual</option>
        {presets.map((p) => (
          <option key={p.key} value={p.key}>
            {p.name}
          </option>
        ))}
      </Select>
      {selected && (
        <div className="space-y-1 rounded-md border p-2 text-xs text-muted-foreground">
          <div className="flex flex-wrap items-center gap-1">
            <Badge variant={RISK_VARIANT[selected.risk_level] ?? "secondary"}>{selected.risk_level}</Badge>
            {selected.experimental && <Badge variant="warning">experimental</Badge>}
            <Badge variant="outline">paper</Badge>
            <span>
              {selected.strategy_name} · {selected.symbol} · {selected.interval} · {selected.leverage}x
            </span>
          </div>
          <p>{selected.description}</p>
          <p>
            Parâmetros e opções abaixo foram preenchidos pelo robô e podem ser ajustados. Resultados passados ou de
            backtest não garantem lucro.
          </p>
        </div>
      )}
    </div>
  );
}
