"use client";

import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { AutoSymbolsOptions, BotOptions, EventPauseOptions, NewsFilterOptions } from "@/lib/types";

export const DEFAULT_NEWS_FILTER: NewsFilterOptions = {
  mode: "off",
  opposing_threshold: 0.4,
  min_confidence: 0.3,
  high_impact_minutes: 120,
  size_multiplier: 0.5,
};

export const DEFAULT_EVENT_PAUSE: EventPauseOptions = {
  enabled: false,
  minutes_before: 30,
  minutes_after: 30,
  min_impact: "high",
  countries: ["USD"],
  tighten_minutes: 60,
  risk_multiplier: 1,
};

export const DEFAULT_BOT_OPTIONS: BotOptions = {
  auto_symbols: { enabled: false, top_n: 3, max_positions: 2, min_score: 0, prefer_signals: true },
  news_filter: DEFAULT_NEWS_FILTER,
  event_pause: DEFAULT_EVENT_PAUSE,
};

export function withDefaults(raw: Partial<BotOptions> | undefined): BotOptions {
  return {
    auto_symbols: { ...DEFAULT_BOT_OPTIONS.auto_symbols, ...(raw?.auto_symbols ?? {}) },
    news_filter: { ...DEFAULT_NEWS_FILTER, ...(raw?.news_filter ?? {}) },
    event_pause: { ...DEFAULT_EVENT_PAUSE, ...(raw?.event_pause ?? {}) },
  };
}

function NumberField({
  id,
  label,
  value,
  min,
  max,
  step = 1,
  onChange,
  title,
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (v: number) => void;
  title?: string;
}) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id} title={title}>
        {label}
      </Label>
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}

export function AutoSymbolsFields({
  value: a,
  onChange,
}: {
  value: AutoSymbolsOptions;
  onChange: (v: AutoSymbolsOptions) => void;
}) {
  const set = (patch: Partial<AutoSymbolsOptions>) => onChange({ ...a, ...patch });
  return (
    <fieldset className="space-y-3 rounded-md border p-3">
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" checked={a.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        Seleção automática de símbolos (scanner)
      </label>
      <p className="text-xs text-muted-foreground">
        O bot opera os N melhores símbolos do último scan (o campo Símbolo é ignorado), sempre pelo gerenciador de
        risco e com limite de posições simultâneas.
      </p>
      {a.enabled && (
        <div className="grid grid-cols-3 gap-3">
          <NumberField id="auto-top" label="Top N" value={a.top_n} min={1} max={20} onChange={(v) => set({ top_n: v })} />
          <NumberField
            id="auto-max"
            label="Máx. posições"
            value={a.max_positions}
            min={1}
            max={20}
            onChange={(v) => set({ max_positions: v })}
          />
          <NumberField
            id="auto-score"
            label="Score mínimo"
            value={a.min_score}
            min={0}
            max={100}
            step={0.5}
            onChange={(v) => set({ min_score: v })}
          />
          <label className="col-span-3 flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={a.prefer_signals}
              onChange={(e) => set({ prefer_signals: e.target.checked })}
            />
            Priorizar símbolos com sinal ativo da estratégia deste bot
          </label>
        </div>
      )}
    </fieldset>
  );
}

export function NewsFilterFields({
  value: n,
  onChange,
  idPrefix = "nf",
}: {
  value: NewsFilterOptions;
  onChange: (v: NewsFilterOptions) => void;
  idPrefix?: string;
}) {
  const set = (patch: Partial<NewsFilterOptions>) => onChange({ ...n, ...patch });
  return (
    <fieldset className="space-y-3 rounded-md border p-3">
      <div className="space-y-1">
        <Label htmlFor={`${idPrefix}-mode`}>Filtro de notícias / sentimento</Label>
        <Select
          id={`${idPrefix}-mode`}
          value={n.mode}
          onChange={(e) => set({ mode: e.target.value as NewsFilterOptions["mode"] })}
        >
          <option value="off">Desligado</option>
          <option value="filter">Filtrar (bloqueia entradas contra o sentimento)</option>
          <option value="reduce_risk">Reduzir risco (diminui o tamanho)</option>
        </Select>
      </div>
      <p className="text-xs text-muted-foreground">
        Notícias são um <b>filtro</b>, nunca um gatilho: só bloqueiam ou reduzem novas entradas. Saídas e stops nunca
        são afetados.
      </p>
      {n.mode !== "off" && (
        <div className="grid grid-cols-2 gap-3">
          <NumberField
            id={`${idPrefix}-thr`}
            label="Sentimento contrário ≥"
            title="Magnitude do sentimento agregado contra a direção do trade (0,05–1)"
            value={n.opposing_threshold}
            min={0.05}
            max={1}
            step={0.05}
            onChange={(v) => set({ opposing_threshold: v })}
          />
          <NumberField
            id={`${idPrefix}-conf`}
            label="Confiança mínima"
            title="Peso agregado mínimo (0–1) antes de confiar no sentimento"
            value={n.min_confidence}
            min={0}
            max={1}
            step={0.05}
            onChange={(v) => set({ min_confidence: v })}
          />
          <NumberField
            id={`${idPrefix}-hi`}
            label="Janela alto impacto (min)"
            title="Notícia negativa de alto impacto nesta janela bloqueia/reduz entradas (0 = ignora)"
            value={n.high_impact_minutes}
            min={0}
            max={1440}
            onChange={(v) => set({ high_impact_minutes: v })}
          />
          {n.mode === "reduce_risk" && (
            <NumberField
              id={`${idPrefix}-mult`}
              label="Multiplicador de tamanho"
              value={n.size_multiplier}
              min={0.05}
              max={1}
              step={0.05}
              onChange={(v) => set({ size_multiplier: v })}
            />
          )}
        </div>
      )}
    </fieldset>
  );
}

export function EventPauseFields({
  value: ev,
  onChange,
  idPrefix = "ep",
}: {
  value: EventPauseOptions;
  onChange: (v: EventPauseOptions) => void;
  idPrefix?: string;
}) {
  const set = (patch: Partial<EventPauseOptions>) => onChange({ ...ev, ...patch });
  return (
    <fieldset className="space-y-3 rounded-md border p-3">
      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" checked={ev.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        Pausar novas entradas perto de eventos econômicos
      </label>
      {ev.enabled && (
        <div className="grid grid-cols-2 gap-3">
          <NumberField
            id={`${idPrefix}-before`}
            label="Minutos antes"
            value={ev.minutes_before}
            min={0}
            max={720}
            onChange={(v) => set({ minutes_before: v })}
          />
          <NumberField
            id={`${idPrefix}-after`}
            label="Minutos depois"
            value={ev.minutes_after}
            min={0}
            max={720}
            onChange={(v) => set({ minutes_after: v })}
          />
          <div className="space-y-1">
            <Label htmlFor={`${idPrefix}-impact`}>Impacto mínimo</Label>
            <Select
              id={`${idPrefix}-impact`}
              value={ev.min_impact}
              onChange={(e) => set({ min_impact: e.target.value as EventPauseOptions["min_impact"] })}
            >
              <option value="high">Alto</option>
              <option value="medium">Médio ou alto</option>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor={`${idPrefix}-countries`}>Moedas/países</Label>
            <Input
              id={`${idPrefix}-countries`}
              value={ev.countries.join(",")}
              placeholder="USD,EUR"
              onChange={(e) =>
                set({
                  countries: e.target.value
                    .split(",")
                    .map((c) => c.trim().toUpperCase())
                    .filter(Boolean),
                })
              }
            />
          </div>
          <NumberField
            id={`${idPrefix}-tighten`}
            label="Janela extra (min)"
            title="Janela adicional antes/depois da pausa com tamanho reduzido"
            value={ev.tighten_minutes}
            min={0}
            max={1440}
            onChange={(v) => set({ tighten_minutes: v })}
          />
          <NumberField
            id={`${idPrefix}-mult`}
            label="Multiplicador na janela extra"
            title="1 = não reduz"
            value={ev.risk_multiplier}
            min={0.05}
            max={1}
            step={0.05}
            onChange={(v) => set({ risk_multiplier: v })}
          />
        </div>
      )}
    </fieldset>
  );
}

export function BotOptionsFields({ value, onChange }: { value: BotOptions; onChange: (v: BotOptions) => void }) {
  return (
    <div className="space-y-3">
      <AutoSymbolsFields value={value.auto_symbols} onChange={(a) => onChange({ ...value, auto_symbols: a })} />
      <NewsFilterFields value={value.news_filter} onChange={(n) => onChange({ ...value, news_filter: n })} />
      <EventPauseFields value={value.event_pause} onChange={(e) => onChange({ ...value, event_pause: e })} />
    </div>
  );
}
