"use client";

import { useEffect, useMemo, useState } from "react";

import {
  DEFAULT_EVENT_PAUSE,
  DEFAULT_NEWS_FILTER,
  EventPauseFields,
  NewsFilterFields,
  withDefaults,
} from "@/components/bot-options";
import { EmptyState, PageHeader } from "@/components/common";
import { EquityChart } from "@/components/equity-chart-lazy";
import { ParamFields, defaultParams } from "@/components/param-fields";
import { PresetPicker } from "@/components/preset-picker";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, errorMessage } from "@/lib/api";
import { INTERVALS } from "@/lib/constants";
import { dateTime, num, pct, pnlClass, qty, usd } from "@/lib/format";
import type {
  BacktestConfig,
  BacktestFilters,
  BacktestFundingOptions,
  BacktestRun,
  BotPreset,
  StrategyInfo,
  WalkForwardOptions,
} from "@/lib/types";

const DEFAULT_WF: WalkForwardOptions = {
  folds: 4,
  train_ratio: 0.7,
  anchored: false,
  objective: "sharpe",
  min_trades: 1,
};

const FUNDING_LABELS: Record<string, string> = {
  auto: "Automático (exchange → arquivo)",
  exchange: "Histórico da exchange",
  archive: "Arquivo data.binance.vision",
  csv: "CSV próprio",
  constant: "Taxa constante a cada 8 h",
  off: "Desligado",
};

const DEFAULT_CONFIG: BacktestConfig = {
  initial_balance: 10000,
  fee_rate: 0.0004,
  slippage_bps: 2,
  leverage: 1,
  max_risk_per_trade_pct: 1,
  max_leverage: 5,
  max_daily_loss_pct: 3,
  max_drawdown_pct: 15,
  stop_on_kill_switch: true,
};

const CONFIG_LABELS: Record<keyof BacktestConfig, string> = {
  initial_balance: "Saldo inicial",
  fee_rate: "Taxa (fração)",
  slippage_bps: "Slippage (bps)",
  leverage: "Alavancagem",
  max_risk_per_trade_pct: "Risco/trade (%)",
  max_leverage: "Alavancagem máx.",
  max_daily_loss_pct: "Perda diária máx. (%)",
  max_drawdown_pct: "Drawdown máx. (%)",
  stop_on_kill_switch: "Parar no kill switch",
};

function isoDaysAgo(days: number) {
  return new Date(Date.now() - days * 86400_000).toISOString().slice(0, 10);
}

function Metric({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-lg font-semibold ${className ?? ""}`}>{value}</div>
    </div>
  );
}

function Results({ run }: { run: BacktestRun }) {
  const m = run.metrics;
  const curve = (run.equity_curve ?? []).map((p) => ({ date: dateTime(p.time), equity: p.equity }));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">
          Resultado — {run.strategy_key} · {run.symbol} · {run.interval}
        </CardTitle>
        <CardDescription>
          {m.start && `${dateTime(m.start)} → ${dateTime(m.end)} · `}
          {m.candles} candles · {dateTime(run.created_at)}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {run.error && <Alert variant="destructive">{run.error}</Alert>}
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-6">
          <Metric label="Patrimônio final" value={usd(m.final_equity)} />
          <Metric label="PnL líquido" value={usd(m.net_pnl)} className={pnlClass(m.net_pnl)} />
          <Metric label="Retorno" value={pct(m.return_pct)} className={pnlClass(m.return_pct)} />
          <Metric label="Buy & hold" value={pct(m.buy_hold_return_pct)} />
          <Metric label="Drawdown máx." value={pct(m.max_drawdown_pct)} />
          <Metric label="Sharpe" value={num(m.sharpe).toFixed(2)} />
          <Metric label="Win rate" value={pct(m.win_rate_pct)} />
          <Metric label="Profit factor" value={m.profit_factor === null ? "—" : num(m.profit_factor).toFixed(2)} />
          <Metric label="Trades fechados" value={String(m.num_closed_trades)} />
          <Metric label="Execuções" value={String(m.num_fills)} />
          <Metric label="Taxas" value={usd(m.total_fees)} />
          <Metric
            label="Funding (+ recebido / − pago)"
            value={m.total_funding === undefined ? "—" : usd(m.total_funding)}
            className={pnlClass(m.total_funding)}
          />
          {m.folds ? (
            <>
              <Metric label="Retorno IS médio" value={pct(m.is_avg_return_pct)} />
              <Metric label="Retorno OOS médio" value={pct(m.oos_avg_return_pct)} className={pnlClass(m.oos_avg_return_pct)} />
              <Metric label="Folds OOS positivos" value={`${m.oos_positive_folds ?? 0}/${m.folds.length}`} />
              <Metric label="Eficiência OOS/IS" value={m.efficiency == null ? "—" : num(m.efficiency).toFixed(2)} />
              <Metric label="Estabilidade parâm." value={num(m.param_stability).toFixed(2)} />
            </>
          ) : (
            <Metric label="Kill switch" value={m.killed_at ? dateTime(m.killed_at) : "Não"} />
          )}
        </div>
        {m.folds && (
          <div className="overflow-x-auto">
            <p className="mb-2 text-sm text-muted-foreground">
              {m.mode === "walk_forward" ? "Walk-forward" : "Validação"}: métricas agregadas acima são só fora da amostra
              (OOS). Eficiência ≈ 1 indica robustez; perto de 0 ou negativa indica overfitting.
            </p>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Fold</TableHead>
                  <TableHead>In-sample</TableHead>
                  <TableHead>Fora da amostra</TableHead>
                  <TableHead>Parâmetros escolhidos</TableHead>
                  <TableHead>Retorno IS</TableHead>
                  <TableHead>Retorno OOS</TableHead>
                  <TableHead>DD OOS</TableHead>
                  <TableHead>Trades OOS</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {m.folds.map((f) => (
                  <TableRow key={f.index}>
                    <TableCell>{f.index}</TableCell>
                    <TableCell className="text-xs">
                      {dateTime(f.is_start)} → {dateTime(f.is_end)}
                    </TableCell>
                    <TableCell className="text-xs">
                      {dateTime(f.oos_start)} → {dateTime(f.oos_end)}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{JSON.stringify(f.best_params)}</TableCell>
                    <TableCell className={pnlClass(f.is.return_pct)}>{pct(f.is.return_pct)}</TableCell>
                    <TableCell className={pnlClass(f.oos.return_pct)}>{pct(f.oos.return_pct)}</TableCell>
                    <TableCell>{pct(f.oos.max_drawdown_pct)}</TableCell>
                    <TableCell>{f.oos.num_closed_trades}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {m.rejections && Object.keys(m.rejections).length > 0 && (
          <div className="flex flex-wrap gap-1 text-xs">
            <span className="text-muted-foreground">Ordens rejeitadas pelo risco:</span>
            {Object.entries(m.rejections).map(([k, v]) => (
              <Badge key={k} variant="outline">
                {k}: {v}
              </Badge>
            ))}
          </div>
        )}
        {curve.length > 0 && <EquityChart data={curve} />}
        {run.trades && run.trades.length > 0 && (
          <div className="max-h-96 overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Data</TableHead>
                  <TableHead>Lado</TableHead>
                  <TableHead>Qtd.</TableHead>
                  <TableHead>Preço</TableHead>
                  <TableHead>Taxa</TableHead>
                  <TableHead>PnL realizado</TableHead>
                  <TableHead>Motivo</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {run.trades.slice(0, 500).map((t, i) => (
                  <TableRow key={i}>
                    <TableCell className="text-xs">{dateTime(t.time)}</TableCell>
                    <TableCell>{t.side}</TableCell>
                    <TableCell>{qty(t.quantity)}</TableCell>
                    <TableCell>{qty(t.price)}</TableCell>
                    <TableCell>{usd(t.fee, 4)}</TableCell>
                    <TableCell className={pnlClass(t.realized_pnl)}>{usd(t.realized_pnl)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{t.tag}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {run.trades.length > 500 && (
              <p className="mt-2 text-xs text-muted-foreground">Exibindo 500 de {run.trades.length} execuções.</p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function StrategiesPage() {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [key, setKey] = useState("");
  const strategy = useMemo(() => strategies.find((s) => s.key === key), [strategies, key]);
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [interval, setBtInterval] = useState("1h");
  const [source, setSource] = useState<"binance" | "archive" | "csv">("binance");
  const [funding, setFunding] = useState<BacktestFundingOptions>({ mode: "auto", constant_rate: 0.0001 });
  const [wfEnabled, setWfEnabled] = useState(false);
  const [wf, setWf] = useState<WalkForwardOptions>(DEFAULT_WF);
  const [grid, setGrid] = useState("");
  const [start, setStart] = useState(isoDaysAgo(30));
  const [end, setEnd] = useState(isoDaysAgo(0));
  const [testnetData, setTestnetData] = useState(false);
  const [csv, setCsv] = useState("");
  const [config, setConfig] = useState<BacktestConfig>(DEFAULT_CONFIG);
  const [filters, setFilters] = useState<BacktestFilters>({
    news_filter: DEFAULT_NEWS_FILTER,
    event_pause: DEFAULT_EVENT_PAUSE,
  });
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BacktestRun | null>(null);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [presets, setPresets] = useState<BotPreset[]>([]);
  const [presetKey, setPresetKey] = useState("");

  useEffect(() => {
    api
      .listStrategies()
      .then((s) => {
        setStrategies(s);
        if (s[0]) {
          setKey(s[0].key);
          setParams(defaultParams(s[0]));
        }
      })
      .catch((e) => setError(errorMessage(e)));
    api.listBacktests().then(setRuns).catch(() => undefined);
    api.listPresets().then(setPresets).catch(() => setPresets([]));
  }, []);

  function selectStrategy(k: string) {
    setKey(k);
    setPresetKey("");
    setParams(defaultParams(strategies.find((s) => s.key === k)));
  }

  function applyPreset(preset: BotPreset | null) {
    setPresetKey(preset?.key ?? "");
    if (!preset) return;
    setKey(preset.strategy_key);
    setParams({ ...preset.params });
    setSymbol(preset.symbol);
    setBtInterval(preset.interval);
    const opts = withDefaults(preset.options);
    setFilters({ news_filter: opts.news_filter, event_pause: opts.event_pause });
  }

  async function onFile(file: File | undefined) {
    if (file) setCsv(await file.text());
  }

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setRunning(true);
    setError(null);
    try {
      const body = {
        strategy_key: key,
        params,
        symbol,
        interval,
        config,
        filters,
        funding,
        ...(source === "csv"
          ? { candles_csv: csv }
          : {
              start: `${start}T00:00:00Z`,
              end: `${end}T23:59:59Z`,
              use_testnet_data: source === "archive" ? false : testnetData,
              data_source: source === "archive" ? ("archive" as const) : ("exchange" as const),
            }),
      };
      let paramGrid: Record<string, unknown[]> = {};
      if (wfEnabled && grid.trim()) {
        try {
          paramGrid = JSON.parse(grid);
        } catch {
          throw new Error('Grid de parâmetros inválido: use JSON, ex.: {"fast": [9, 12], "slow": [26, 50]}');
        }
      }
      const res = wfEnabled
        ? await api.runWalkForward({ ...body, param_grid: paramGrid, walk_forward: wf })
        : await api.runBacktest(body);
      setResult(res);
      api.listBacktests().then(setRuns).catch(() => undefined);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRunning(false);
    }
  }

  async function openRun(id: string) {
    try {
      setResult(await api.getBacktest(id));
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Estratégias & Backtest"
        description="Simule estratégias com taxas, slippage e as mesmas regras de risco usadas ao vivo."
      />
      <Card>
        <CardContent className="pt-6">
          <form onSubmit={run} className="space-y-4">
            <PresetPicker
              id="bt-preset"
              presets={presets}
              value={presetKey}
              onChange={applyPreset}
              label="Testar um robô pronto (opcional)"
            />
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div className="space-y-1">
                <Label htmlFor="bt-strategy">Estratégia</Label>
                <Select id="bt-strategy" value={key} onChange={(e) => selectStrategy(e.target.value)}>
                  {strategies.map((s) => (
                    <option key={s.key} value={s.key}>
                      {s.name}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="bt-symbol">Símbolo</Label>
                <Input id="bt-symbol" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="bt-interval">Intervalo</Label>
                <Select id="bt-interval" value={interval} onChange={(e) => setBtInterval(e.target.value)}>
                  {INTERVALS.map((i) => (
                    <option key={i}>{i}</option>
                  ))}
                </Select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="bt-source">Dados</Label>
                <Select
                  id="bt-source"
                  value={source}
                  onChange={(e) => setSource(e.target.value as "binance" | "archive" | "csv")}
                >
                  <option value="binance">Binance (histórico público)</option>
                  <option value="archive">Arquivo Binance (data.binance.vision)</option>
                  <option value="csv">CSV próprio</option>
                </Select>
              </div>
            </div>
            {strategy && <p className="text-sm text-muted-foreground">{strategy.description}</p>}

            <Tabs defaultValue="data">
              <TabsList>
                <TabsTrigger value="data">Dados</TabsTrigger>
                <TabsTrigger value="params">Parâmetros</TabsTrigger>
                <TabsTrigger value="risk">Custos & risco</TabsTrigger>
                <TabsTrigger value="filters">Notícias & eventos</TabsTrigger>
                <TabsTrigger value="wf">Walk-forward</TabsTrigger>
              </TabsList>
              <TabsContent value="wf" className="space-y-3 pt-3">
                <p className="text-sm text-muted-foreground">
                  Divide o período em janelas: otimiza o grid na parte in-sample e mede fora da amostra (OOS). Sem grid, valida
                  os parâmetros atuais.
                </p>
                <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-6">
                  <div className="space-y-1">
                    <Label htmlFor="wf-on">Modo</Label>
                    <Select id="wf-on" value={wfEnabled ? "1" : "0"} onChange={(e) => setWfEnabled(e.target.value === "1")}>
                      <option value="0">Backtest simples</option>
                      <option value="1">Walk-forward</option>
                    </Select>
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="wf-folds">Folds</Label>
                    <Input
                      id="wf-folds"
                      type="number"
                      min={1}
                      max={12}
                      value={wf.folds}
                      onChange={(e) => setWf({ ...wf, folds: Number(e.target.value) })}
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="wf-ratio">In-sample (fração)</Label>
                    <Input
                      id="wf-ratio"
                      type="number"
                      step="0.05"
                      value={wf.train_ratio}
                      onChange={(e) => setWf({ ...wf, train_ratio: Number(e.target.value) })}
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="wf-obj">Objetivo</Label>
                    <Select
                      id="wf-obj"
                      value={wf.objective}
                      onChange={(e) => setWf({ ...wf, objective: e.target.value as WalkForwardOptions["objective"] })}
                    >
                      <option value="sharpe">Sharpe</option>
                      <option value="return_pct">Retorno</option>
                      <option value="profit_factor">Profit factor</option>
                      <option value="calmar">Retorno / drawdown</option>
                    </Select>
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="wf-anch">Janela</Label>
                    <Select
                      id="wf-anch"
                      value={wf.anchored ? "1" : "0"}
                      onChange={(e) => setWf({ ...wf, anchored: e.target.value === "1" })}
                    >
                      <option value="0">Deslizante</option>
                      <option value="1">Ancorada</option>
                    </Select>
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="wf-min">Mín. trades IS</Label>
                    <Input
                      id="wf-min"
                      type="number"
                      min={0}
                      value={wf.min_trades}
                      onChange={(e) => setWf({ ...wf, min_trades: Number(e.target.value) })}
                    />
                  </div>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="wf-grid">Grid de parâmetros (JSON, até 200 combinações; vazio = só validação)</Label>
                  <Textarea
                    id="wf-grid"
                    rows={3}
                    className="font-mono text-xs"
                    value={grid}
                    onChange={(e) => setGrid(e.target.value)}
                    placeholder='{"fast": [9, 12, 20], "slow": [26, 50]}'
                  />
                </div>
              </TabsContent>
              <TabsContent value="data" className="pt-3">
                {source !== "csv" ? (
                  <div className="grid gap-3 sm:grid-cols-3">
                    <div className="space-y-1">
                      <Label htmlFor="bt-start">Início</Label>
                      <Input id="bt-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
                    </div>
                    <div className="space-y-1">
                      <Label htmlFor="bt-end">Fim</Label>
                      <Input id="bt-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
                    </div>
                    {source === "binance" ? (
                      <div className="space-y-1">
                        <Label htmlFor="bt-net">Fonte</Label>
                        <Select
                          id="bt-net"
                          value={testnetData ? "1" : "0"}
                          onChange={(e) => setTestnetData(e.target.value === "1")}
                        >
                          <option value="0">Mainnet (dados públicos)</option>
                          <option value="1">Testnet</option>
                        </Select>
                      </div>
                    ) : (
                      <p className="self-end text-xs text-muted-foreground">
                        Arquivo público da mainnet (funciona onde a API REST é bloqueada por região).
                      </p>
                    )}
                  </div>
                ) : (
                  <div className="space-y-2">
                    <Label htmlFor="bt-csv">
                      CSV com colunas open_time, open, high, low, close, volume (cabeçalho opcional)
                    </Label>
                    <Input id="bt-file" type="file" accept=".csv,text/csv" onChange={(e) => onFile(e.target.files?.[0])} />
                    <Textarea
                      id="bt-csv"
                      rows={6}
                      className="font-mono text-xs"
                      value={csv}
                      onChange={(e) => setCsv(e.target.value)}
                      placeholder="open_time,open,high,low,close,volume"
                    />
                  </div>
                )}
              </TabsContent>
              <TabsContent value="params" className="pt-3">
                {strategy ? (
                  <ParamFields strategy={strategy} values={params} onChange={setParams} />
                ) : (
                  <EmptyState>Selecione uma estratégia.</EmptyState>
                )}
              </TabsContent>
              <TabsContent value="filters" className="space-y-3 pt-3">
                <Alert variant="warning">
                  Os filtros usam apenas notícias e eventos já coletados por esta instância (não há arquivo histórico de
                  notícias). Períodos anteriores à coleta rodam sem filtro. Deixe desligado para um backtest puro da
                  estratégia.
                </Alert>
                <NewsFilterFields
                  idPrefix="bt-nf"
                  value={filters.news_filter}
                  onChange={(n) => setFilters({ ...filters, news_filter: n })}
                />
                <EventPauseFields
                  idPrefix="bt-ep"
                  value={filters.event_pause}
                  onChange={(e) => setFilters({ ...filters, event_pause: e })}
                />
              </TabsContent>
              <TabsContent value="risk" className="space-y-3 pt-3">
                <div className="grid gap-3 sm:grid-cols-3">
                  <div className="space-y-1">
                    <Label htmlFor="bt-funding">Funding</Label>
                    <Select
                      id="bt-funding"
                      value={funding.mode}
                      onChange={(e) => setFunding({ ...funding, mode: e.target.value as BacktestFundingOptions["mode"] })}
                    >
                      {Object.entries(FUNDING_LABELS).map(([k, label]) => (
                        <option key={k} value={k}>
                          {label}
                        </option>
                      ))}
                    </Select>
                  </div>
                  {funding.mode === "constant" && (
                    <div className="space-y-1">
                      <Label htmlFor="bt-funding-rate">Taxa por 8 h (fração)</Label>
                      <Input
                        id="bt-funding-rate"
                        type="number"
                        step="any"
                        value={String(funding.constant_rate ?? 0.0001)}
                        onChange={(e) => setFunding({ ...funding, constant_rate: Number(e.target.value) })}
                      />
                    </div>
                  )}
                  {funding.mode === "csv" && (
                    <div className="space-y-1 sm:col-span-2">
                      <Label htmlFor="bt-funding-csv">CSV fundingTime,fundingRate</Label>
                      <Textarea
                        id="bt-funding-csv"
                        rows={3}
                        className="font-mono text-xs"
                        value={funding.csv ?? ""}
                        onChange={(e) => setFunding({ ...funding, csv: e.target.value })}
                      />
                    </div>
                  )}
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  {(Object.keys(DEFAULT_CONFIG) as (keyof BacktestConfig)[]).map((k) => (
                    <div key={k} className="space-y-1">
                      <Label htmlFor={`cfg-${k}`}>{CONFIG_LABELS[k]}</Label>
                      {k === "stop_on_kill_switch" ? (
                        <Select
                          id={`cfg-${k}`}
                          value={config[k] ? "1" : "0"}
                          onChange={(e) => setConfig({ ...config, [k]: e.target.value === "1" })}
                        >
                          <option value="1">Sim</option>
                          <option value="0">Não</option>
                        </Select>
                      ) : (
                        <Input
                          id={`cfg-${k}`}
                          type="number"
                          step="any"
                          value={String(config[k])}
                          onChange={(e) => setConfig({ ...config, [k]: Number(e.target.value) })}
                        />
                      )}
                    </div>
                  ))}
                </div>
              </TabsContent>
            </Tabs>

            {error && <Alert variant="destructive">{error}</Alert>}
            <Button type="submit" disabled={running || !key}>
              {running ? "Executando..." : wfEnabled ? "Executar walk-forward" : "Executar backtest"}
            </Button>
          </form>
        </CardContent>
      </Card>

      {result && <Results run={result} />}

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Backtests anteriores</CardTitle>
        </CardHeader>
        <CardContent>
          {runs.length === 0 ? (
            <EmptyState>Nenhum backtest executado.</EmptyState>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Data</TableHead>
                  <TableHead>Estratégia</TableHead>
                  <TableHead>Mercado</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Retorno</TableHead>
                  <TableHead>Drawdown</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell className="text-xs">{dateTime(r.created_at)}</TableCell>
                    <TableCell>{r.strategy_key}</TableCell>
                    <TableCell>
                      {r.symbol} · {r.interval}
                    </TableCell>
                    <TableCell>
                      <Badge variant={r.status === "completed" ? "success" : r.status === "failed" ? "destructive" : "secondary"}>
                        {{ completed: "Concluído", failed: "Falhou", running: "Executando", pending: "Pendente" }[r.status]}
                      </Badge>
                    </TableCell>
                    <TableCell className={pnlClass(r.metrics?.return_pct)}>{pct(r.metrics?.return_pct)}</TableCell>
                    <TableCell>{pct(r.metrics?.max_drawdown_pct)}</TableCell>
                    <TableCell className="text-right">
                      <Button size="sm" variant="outline" onClick={() => openRun(r.id)}>
                        Ver
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
