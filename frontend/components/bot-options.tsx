"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { BotOptions } from "@/lib/types";

export const DEFAULT_BOT_OPTIONS: BotOptions = {
  auto_symbols: { enabled: false, top_n: 3, max_positions: 2, min_score: 0, prefer_signals: true },
};

export function withDefaults(raw: Partial<BotOptions> | undefined): BotOptions {
  return {
    ...DEFAULT_BOT_OPTIONS,
    ...(raw ?? {}),
    auto_symbols: { ...DEFAULT_BOT_OPTIONS.auto_symbols, ...(raw?.auto_symbols ?? {}) },
  } as BotOptions;
}

function NumberField({
  id,
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
  disabled,
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}

export function BotOptionsFields({ value, onChange }: { value: BotOptions; onChange: (v: BotOptions) => void }) {
  const a = value.auto_symbols;
  const setAuto = (patch: Partial<BotOptions["auto_symbols"]>) =>
    onChange({ ...value, auto_symbols: { ...a, ...patch } });
  return (
    <div className="space-y-3 rounded-md border p-3">
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" checked={a.enabled} onChange={(e) => setAuto({ enabled: e.target.checked })} />
        Seleção automática de símbolos (scanner)
      </label>
      <p className="text-xs text-muted-foreground">
        O bot opera os N melhores símbolos do último scan (o campo Símbolo é ignorado), sempre pelo gerenciador de
        risco e com limite de posições simultâneas.
      </p>
      {a.enabled && (
        <div className="grid grid-cols-3 gap-3">
          <NumberField id="auto-top" label="Top N" value={a.top_n} min={1} max={20} onChange={(v) => setAuto({ top_n: v })} />
          <NumberField
            id="auto-max"
            label="Máx. posições"
            value={a.max_positions}
            min={1}
            max={20}
            onChange={(v) => setAuto({ max_positions: v })}
          />
          <NumberField
            id="auto-score"
            label="Score mínimo"
            value={a.min_score}
            min={0}
            max={100}
            step={0.5}
            onChange={(v) => setAuto({ min_score: v })}
          />
          <label className="col-span-3 flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={a.prefer_signals}
              onChange={(e) => setAuto({ prefer_signals: e.target.checked })}
            />
            Priorizar símbolos com sinal ativo da estratégia deste bot
          </label>
        </div>
      )}
    </div>
  );
}
