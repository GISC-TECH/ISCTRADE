"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { BotOptionsFields, withDefaults } from "@/components/bot-options";
import { EmptyState, PageHeader } from "@/components/common";
import { ParamFields, defaultParams } from "@/components/param-fields";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Select, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage } from "@/lib/api";
import { INTERVALS } from "@/lib/constants";
import { dateTime } from "@/lib/format";
import { presetToWrite, splitTags } from "@/lib/presets";
import type { BotPreset, PresetWrite, RiskLevel, StrategyInfo } from "@/lib/types";

const RISK_VARIANT = { conservador: "success", moderado: "secondary", agressivo: "warning" } as const;

type Editing = { mode: "create" | "edit"; preset?: BotPreset; initial: PresetWrite } | null;

function PresetEditor({
  editing,
  strategies,
  onClose,
  onSaved,
}: {
  editing: NonNullable<Editing>;
  strategies: StrategyInfo[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<PresetWrite>(editing.initial);
  const [tags, setTags] = useState(editing.initial.tags.join(", "));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const strategy = strategies.find((s) => s.key === form.strategy_key);
  const set = <K extends keyof PresetWrite>(key: K, value: PresetWrite[K]) => setForm((f) => ({ ...f, [key]: value }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const data = { ...form, tags: splitTags(tags), symbol: form.symbol.trim().toUpperCase() };
    try {
      if (editing.mode === "edit" && editing.preset?.id) {
        const { strategy_key: _ignored, ...rest } = data; // strategy is fixed after creation
        void _ignored;
        await api.updatePreset(editing.preset.id, rest);
      } else {
        await api.createPreset(data);
      }
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{editing.mode === "edit" ? "Editar preset" : "Novo preset"}</DialogTitle>
          <DialogDescription>
            Presets só preenchem o formulário de bot. O modo (paper/live) vem sempre da conta e toda ordem passa pelo
            gerenciador de risco.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-4">
          {error && <Alert variant="destructive">{error}</Alert>}
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="preset-name">Nome</Label>
              <Input id="preset-name" required maxLength={120} value={form.name} onChange={(e) => set("name", e.target.value)} />
            </div>
            <div className="space-y-1 sm:col-span-2">
              <Label htmlFor="preset-description">Descrição</Label>
              <Textarea
                id="preset-description"
                maxLength={2000}
                value={form.description}
                onChange={(e) => set("description", e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="preset-strategy">Estratégia</Label>
              <Select
                id="preset-strategy"
                value={form.strategy_key}
                disabled={editing.mode === "edit"}
                onChange={(e) => {
                  const key = e.target.value;
                  setForm((f) => ({ ...f, strategy_key: key, params: defaultParams(strategies.find((s) => s.key === key)) }));
                }}
              >
                {strategies.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.name}
                  </option>
                ))}
              </Select>
              {editing.mode === "edit" && (
                <p className="text-xs text-muted-foreground">Para trocar a estratégia, crie um novo preset.</p>
              )}
            </div>
            <div className="space-y-1">
              <Label htmlFor="preset-symbol">Símbolo</Label>
              <Input
                id="preset-symbol"
                required
                pattern="[A-Za-z0-9]{2,30}"
                value={form.symbol}
                onChange={(e) => set("symbol", e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="preset-interval">Timeframe</Label>
              <Select id="preset-interval" value={form.interval} onChange={(e) => set("interval", e.target.value)}>
                {INTERVALS.map((i) => (
                  <option key={i} value={i}>
                    {i}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="preset-leverage">Alavancagem</Label>
              <Input
                id="preset-leverage"
                type="number"
                min={1}
                max={125}
                value={form.leverage}
                onChange={(e) => set("leverage", Number(e.target.value) || 1)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="preset-risk">Nível de risco</Label>
              <Select id="preset-risk" value={form.risk_level} onChange={(e) => set("risk_level", e.target.value as RiskLevel)}>
                <option value="conservador">Conservador</option>
                <option value="moderado">Moderado</option>
                <option value="agressivo">Agressivo</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="preset-tags">Tags (separadas por vírgula)</Label>
              <Input id="preset-tags" value={tags} onChange={(e) => setTags(e.target.value)} />
            </div>
          </div>
          {strategy && strategy.params.length > 0 && (
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Parâmetros da estratégia</legend>
              <ParamFields strategy={strategy} values={form.params} onChange={(p) => set("params", p)} />
            </fieldset>
          )}
          <BotOptionsFields value={form.options} onChange={(o) => set("options", o)} />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancelar
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? "Salvando..." : "Salvar preset"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PresetCard({
  preset,
  canClone,
  onClone,
  onEdit,
  onDelete,
}: {
  preset: BotPreset;
  canClone: boolean;
  onClone: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <Card className="flex flex-col">
      <CardHeader>
        <CardTitle className="text-base">{preset.name}</CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-1">
          <Badge variant={RISK_VARIANT[preset.risk_level] ?? "secondary"}>{preset.risk_level}</Badge>
          {preset.experimental && <Badge variant="warning">experimental</Badge>}
          <Badge variant="outline">{preset.source === "custom" ? "personalizado" : "embutido"}</Badge>
          <span>
            {preset.strategy_name} · {preset.symbol} · {preset.interval} · {preset.leverage}x
          </span>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex-1 space-y-2 text-sm text-muted-foreground">
        {preset.description && <p>{preset.description}</p>}
        {preset.tags.length > 0 && <p className="text-xs">Tags: {preset.tags.join(", ")}</p>}
        {preset.source === "custom" && (
          <p className="text-xs">
            {preset.based_on ? `Baseado em ${preset.based_on}. ` : ""}
            {preset.updated_at ? `Atualizado em ${dateTime(preset.updated_at)}.` : ""}
          </p>
        )}
      </CardContent>
      <CardFooter className="flex flex-wrap gap-2">
        {canClone && (
          <Button size="sm" variant="outline" onClick={onClone} aria-label={`Clonar ${preset.name}`}>
            Clonar
          </Button>
        )}
        {preset.editable && (
          <>
            <Button size="sm" variant="outline" onClick={onEdit} aria-label={`Editar ${preset.name}`}>
              Editar
            </Button>
            <Button size="sm" variant="destructive" onClick={onDelete} aria-label={`Excluir ${preset.name}`}>
              Excluir
            </Button>
          </>
        )}
      </CardFooter>
    </Card>
  );
}

export default function PresetsPage() {
  const { workspace } = useAuth();
  const [presets, setPresets] = useState<BotPreset[] | null>(null);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<Editing>(null);
  const role = workspace?.role ?? null;
  const isAdmin = role === "admin";
  const canWrite = role === "admin" || role === "trader";

  const load = useCallback(
    () =>
      Promise.all([api.listPresets(), api.listStrategies()])
        .then(([p, s]) => {
          setPresets(p);
          setStrategies(s);
        })
        .catch((e) => setError(errorMessage(e))),
    []
  );
  useEffect(() => {
    void load();
  }, [load, workspace?.id]);

  const [custom, builtin] = useMemo(() => {
    const list = presets ?? [];
    return [list.filter((p) => p.source === "custom"), list.filter((p) => p.source !== "custom")];
  }, [presets]);

  async function clone(p: BotPreset) {
    setError(null);
    try {
      const created = await api.clonePreset(p.key);
      setNotice(`"${created.name}" criado. Edite-o para personalizar.`);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function remove(p: BotPreset) {
    if (!p.id || !confirm(`Excluir o preset "${p.name}"? Bots já criados não são afetados.`)) return;
    setError(null);
    try {
      await api.deletePreset(p.id);
      setNotice(`"${p.name}" excluído.`);
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  function startCreate() {
    const s = strategies[0];
    if (!s) return;
    setEditing({
      mode: "create",
      initial: {
        name: "",
        description: "",
        strategy_key: s.key,
        interval: "1h",
        symbol: "BTCUSDT",
        leverage: 1,
        params: defaultParams(s),
        options: withDefaults(undefined),
        risk_level: "moderado",
        tags: [],
      },
    });
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Presets de bots"
        description="Robôs prontos embutidos e presets personalizados deste workspace. Usados para preencher o formulário de novo bot."
      >
        {canWrite && (
          <Button onClick={startCreate} disabled={!strategies.length}>
            Novo preset
          </Button>
        )}
      </PageHeader>
      {error && <Alert variant="destructive">{error}</Alert>}
      {notice && <Alert variant="success">{notice}</Alert>}
      <Alert variant="warning">
        Presets não são recomendação de investimento. Teste em paper/Testnet antes; resultados passados não garantem
        lucro.
      </Alert>
      {!presets ? (
        <EmptyState>Carregando...</EmptyState>
      ) : (
        <>
          <section aria-labelledby="custom-title" className="space-y-3">
            <h2 id="custom-title" className="text-lg font-semibold">
              Personalizados do workspace ({custom.length})
            </h2>
            {custom.length === 0 ? (
              <EmptyState>
                Nenhum preset personalizado.{" "}
                {isAdmin
                  ? "Clone um robô pronto abaixo ou crie um do zero."
                  : canWrite
                    ? "Crie um do zero (clonar robôs prontos é exclusivo de administradores do workspace)."
                    : ""}
              </EmptyState>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {custom.map((p) => (
                  <PresetCard
                    key={p.key}
                    preset={p}
                    canClone={canWrite}
                    onClone={() => clone(p)}
                    onEdit={() => setEditing({ mode: "edit", preset: p, initial: presetToWrite(p) })}
                    onDelete={() => remove(p)}
                  />
                ))}
              </div>
            )}
          </section>
          <section aria-labelledby="builtin-title" className="space-y-3">
            <h2 id="builtin-title" className="text-lg font-semibold">
              Robôs prontos embutidos ({builtin.length})
            </h2>
            <p className="text-sm text-muted-foreground">
              Somente leitura. {isAdmin ? "Para alterar um, clone-o e edite a cópia." : "Administradores do workspace podem cloná-los para personalizar."}
            </p>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {builtin.map((p) => (
                <PresetCard
                  key={p.key}
                  preset={p}
                  canClone={isAdmin}
                  onClone={() => clone(p)}
                  onEdit={() => undefined}
                  onDelete={() => undefined}
                />
              ))}
            </div>
          </section>
        </>
      )}
      {editing && (
        <PresetEditor
          key={editing.preset?.key ?? "new"}
          editing={editing}
          strategies={strategies}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setNotice(editing.mode === "edit" ? "Preset atualizado." : "Preset criado.");
            setEditing(null);
            void load();
          }}
        />
      )}
    </div>
  );
}
