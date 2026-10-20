"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader, StatusBadge } from "@/components/common";
import { ParamFields, defaultParams } from "@/components/param-fields";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { INTERVALS } from "@/lib/constants";
import { dateTime } from "@/lib/format";
import type { Account, Bot, StrategyInfo } from "@/lib/types";


function CreateBotDialog({
  accounts,
  strategies,
  onCreated,
}: {
  accounts: Account[];
  strategies: StrategyInfo[];
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const [name, setName] = useState("");
  const [strategyKey, setStrategyKey] = useState(strategies[0]?.key ?? "");
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [interval, setBotInterval] = useState("15m");
  const [leverage, setLeverage] = useState(2);
  const strategy = useMemo(() => strategies.find((s) => s.key === strategyKey), [strategies, strategyKey]);
  const [params, setParams] = useState<Record<string, unknown>>(() => defaultParams(strategies[0]));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setParams(defaultParams(strategy)), [strategy]);
  useEffect(() => {
    if (!accountId && accounts[0]) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.createBot({ account_id: accountId, name, strategy_key: strategyKey, symbol, interval, leverage, params });
      setOpen(false);
      setName("");
      onCreated();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button disabled={!accounts.length}>Novo bot</Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Novo bot</DialogTitle>
          <DialogDescription>Todas as ordens passam pelo gerenciador de risco da conta.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2 space-y-1">
              <Label htmlFor="bot-name">Nome</Label>
              <Input id="bot-name" required value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label htmlFor="bot-account">Conta</Label>
              <Select id="bot-account" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="bot-strategy">Estratégia</Label>
              <Select id="bot-strategy" value={strategyKey} onChange={(e) => setStrategyKey(e.target.value)}>
                {strategies.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.name}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="bot-symbol">Símbolo</Label>
              <Input
                id="bot-symbol"
                required
                value={symbol}
                onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="bot-interval">Intervalo</Label>
              <Select id="bot-interval" value={interval} onChange={(e) => setBotInterval(e.target.value)}>
                {INTERVALS.map((i) => (
                  <option key={i}>{i}</option>
                ))}
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="bot-lev">Alavancagem</Label>
              <Input
                id="bot-lev"
                type="number"
                min={1}
                max={125}
                value={leverage}
                onChange={(e) => setLeverage(Number(e.target.value))}
              />
            </div>
          </div>
          {strategy && (
            <>
              <p className="text-xs text-muted-foreground">{strategy.description}</p>
              <ParamFields strategy={strategy} values={params} onChange={setParams} />
            </>
          )}
          {error && <Alert variant="destructive">{error}</Alert>}
          <DialogFooter>
            <Button type="submit">Criar</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function BotsPage() {
  const { workspace } = useAuth();
  const [bots, setBots] = useState<Bot[] | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const canTrade = workspace?.role === "admin" || workspace?.role === "trader";

  const load = useCallback(() => {
    Promise.all([api.listBots(), api.listAccounts(), api.listStrategies()])
      .then(([b, a, s]) => {
        setBots(b);
        setAccounts(a);
        setStrategies(s);
      })
      .catch((e) => setError(errorMessage(e)));
  }, []);
  useEffect(load, [load, workspace?.id]);

  async function action(id: string, a: Parameters<typeof api.botAction>[1]) {
    setError(null);
    try {
      await api.botAction(id, a);
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  const accountName = (id: string) => accounts.find((a) => a.id === id)?.label ?? id.slice(0, 8);

  return (
    <div>
      <PageHeader title="Bots" description="Bots executam a cada candle fechado (tarefa Celery tick-bots).">
        {canTrade && strategies.length > 0 && (
          <CreateBotDialog accounts={accounts} strategies={strategies} onCreated={load} />
        )}
      </PageHeader>
      {accounts.length === 0 && bots !== null && (
        <Alert variant="warning" className="mb-4">
          Conecte uma conta antes de criar bots.
        </Alert>
      )}
      {error && <Alert variant="destructive" className="mb-4">{error}</Alert>}
      <Card>
        <CardContent className="pt-6">
          {bots === null ? (
            <EmptyState>Carregando...</EmptyState>
          ) : bots.length === 0 ? (
            <EmptyState>Nenhum bot criado.</EmptyState>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Bot</TableHead>
                  <TableHead>Conta</TableHead>
                  <TableHead>Mercado</TableHead>
                  <TableHead>Estado</TableHead>
                  <TableHead>Último tick</TableHead>
                  <TableHead className="text-right">Ações</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {bots.map((b) => (
                  <TableRow key={b.id}>
                    <TableCell>
                      <div className="font-medium">{b.name}</div>
                      <div className="text-xs text-muted-foreground">{b.strategy_key}</div>
                    </TableCell>
                    <TableCell>{accountName(b.account_id)}</TableCell>
                    <TableCell>
                      {b.symbol} · {b.interval} · {b.leverage}x
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={b.state} />
                      {b.last_error && <div className="mt-1 max-w-xs truncate text-xs text-red-400">{b.last_error}</div>}
                    </TableCell>
                    <TableCell className="text-xs">{dateTime(b.last_tick_at)}</TableCell>
                    <TableCell className="space-x-1 whitespace-nowrap text-right">
                      {canTrade && (
                        <>
                          {["idle", "stopped"].includes(b.state) && (
                            <Button size="sm" onClick={() => action(b.id, "start")}>
                              Iniciar
                            </Button>
                          )}
                          {b.state === "running" && (
                            <Button size="sm" variant="outline" onClick={() => action(b.id, "pause")}>
                              Pausar
                            </Button>
                          )}
                          {b.state === "paused" && (
                            <Button size="sm" variant="outline" onClick={() => action(b.id, "resume")}>
                              Retomar
                            </Button>
                          )}
                          {["running", "paused", "starting"].includes(b.state) && (
                            <Button size="sm" variant="outline" onClick={() => action(b.id, "stop")}>
                              Parar
                            </Button>
                          )}
                          {["error", "killed"].includes(b.state) && (
                            <Button size="sm" variant="outline" onClick={() => action(b.id, "reset-state")}>
                              Resetar
                            </Button>
                          )}
                          <Button
                            size="sm"
                            variant="destructive"
                            onClick={async () => {
                              if (!confirm(`Remover o bot "${b.name}"?`)) return;
                              try {
                                await api.deleteBot(b.id);
                                load();
                              } catch (e) {
                                setError(errorMessage(e));
                              }
                            }}
                          >
                            Remover
                          </Button>
                        </>
                      )}
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
