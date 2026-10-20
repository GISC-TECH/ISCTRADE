"use client";

import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { StrategyInfo } from "@/lib/types";

export function defaultParams(strategy?: StrategyInfo): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const p of strategy?.params ?? []) if (p.default !== null) out[p.name] = p.default;
  return out;
}

export function ParamFields({
  strategy,
  values,
  onChange,
}: {
  strategy: StrategyInfo;
  values: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      {strategy.params.map((p) => {
        const id = `param-${p.name}`;
        const value = values[p.name];
        return (
          <div key={p.name} className="space-y-1">
            <Label htmlFor={id} title={p.description}>
              {p.name}
              {p.required && " *"}
            </Label>
            {p.type === "boolean" ? (
              <Select
                id={id}
                value={value ? "1" : "0"}
                onChange={(e) => onChange({ ...values, [p.name]: e.target.value === "1" })}
              >
                <option value="1">Sim</option>
                <option value="0">Não</option>
              </Select>
            ) : (
              <Input
                id={id}
                type="number"
                required={p.required}
                step={p.type === "integer" ? 1 : "any"}
                min={p.min ?? undefined}
                max={p.max ?? undefined}
                value={value === undefined || value === null ? "" : String(value)}
                onChange={(e) => {
                  const next = { ...values };
                  if (e.target.value === "") delete next[p.name];
                  else next[p.name] = Number(e.target.value);
                  onChange(next);
                }}
              />
            )}
            <p className="text-[11px] leading-tight text-muted-foreground">{p.description}</p>
          </div>
        );
      })}
    </div>
  );
}
