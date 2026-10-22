"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader, StatusBadge } from "@/components/common";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
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
import { dateTime, pctLimit, usd } from "@/lib/format";
import type { Account, AccountCreate, ExchangeInfo } from "@/lib/types";

const EMPTY: AccountCreate = {
  label: "",
  exchange: "binance_usdm",
  api_key: "",
  api_secret: "",
  is_testnet: true,
  execution_mode: "paper",
  paper_initial_balance: 10000,
  max_leverage: 5,
  max_risk_per_trade_pct: 1,
  max_daily_loss_pct: 3,
  max_drawdown_pct: 10,
};

const EXCHANGE_LABELS: Record<string, string> = { binance_usdm: "Binance", bybit: "Bybit" };

function ConnectDialog({ onCreated, totpEnabled }: { onCreated: () => void; totpEnabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<AccountCreate>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [exchanges, setExchanges] = useState<ExchangeInfo[] | null>(null);
  const exchange = exchanges?.find((x) => x.id === form.exchange) ?? null;

  // Exchanges come from the API (experimental adapters only when EXPERIMENTAL_EXCHANGES_ENABLED on the server).
  function onOpenChange(next: boolean) {
    setOpen(next);
    if (next && exchanges === null) {
      api
        .listExchanges()
        .then((list) => {
          setExchanges(list);
          if (list.length && !list.some((x) => x.id === form.exchange)) set("exchange", list[0].id);
        })
        .catch((e) => setError(errorMessage(e)));
    }
  }

  function selectExchange(id: string) {
    const info = exchanges?.find((x) => x.id === id);
    setForm((f) => ({ ...f, exchange: id, is_testnet: info && !info.testnet ? false : f.is_testnet }));
  }

  const set = <K extends keyof AccountCreate>(k: K, v: AccountCreate[K]) => setForm((f) => ({ ...f, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.connectAccount(form);
      setForm(EMPTY);
      setOpen(false);
      onCreated();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const numField = (k: keyof AccountCreate, label: string, step = "1") => (
    <div className="space-y-1">
      <Label htmlFor={k}>{label}</Label>
      <Input
        id={k}
        type="number"
        step={step}
        value={String(form[k])}
        onChange={(e) => set(k, Number(e.target.value) as never)}
      />
    </div>
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button>Conectar conta</Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Conectar conta de futuros</DialogTitle>
          <DialogDescription>
            As chaves são criptografadas (AES-256-GCM) antes de serem salvas e nunca são exibidas novamente. Use chaves
            sem permissão de saque.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="exchange">Corretora</Label>
            <Select
              id="exchange"
              value={form.exchange}
              disabled={!exchanges}
              onChange={(e) => selectExchange(e.target.value)}
              aria-describedby="exchange-notes"
            >
              {(exchanges ?? [{ id: "binance_usdm", name: "Binance USDT-M Futures", experimental: false } as ExchangeInfo]).map(
                (x) => (
                  <option key={x.id} value={x.id}>
                    {x.name}
                    {x.experimental ? " (experimental)" : ""}
                  </option>
                )
              )}
            </Select>
            {exchange && (exchange.experimental || exchange.notes.length > 0) && (
              <div id="exchange-notes" className="space-y-1 text-xs text-muted-foreground">
                {exchange.experimental && (
                  <p className="font-medium text-yellow-500">
                    Adaptador experimental: use apenas em paper/testnet até validar.
                  </p>
                )}
                {exchange.notes.map((n) => (
                  <p key={n}>{n}</p>
                ))}
              </div>
            )}
          </div>
          <div className="space-y-1">
            <Label htmlFor="label">Nome</Label>
            <Input id="label" required value={form.label} onChange={(e) => set("label", e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="api_key">API key</Label>
            <Input
              id="api_key"
              required
              autoComplete="off"
              value={form.api_key}
              onChange={(e) => set("api_key", e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="api_secret">API secret</Label>
            <Input
              id="api_secret"
              type="password"
              required
              autoComplete="off"
              value={form.api_secret}
              onChange={(e) => set("api_secret", e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="mode">Execução</Label>
              <Select
                id="mode"
                value={form.execution_mode}
                onChange={(e) => set("execution_mode", e.target.value as AccountCreate["execution_mode"])}
              >
                <option value="paper">Paper (simulado)</option>
                <option value="live">Real</option>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="net">Rede</Label>
              <Select id="net" value={form.is_testnet ? "1" : "0"} onChange={(e) => set("is_testnet", e.target.value === "1")}>
                <option value="1" disabled={exchange ? !exchange.testnet : false}>
                  Testnet
                </option>
                <option value="0">Mainnet</option>
              </Select>
            </div>
            {numField("paper_initial_balance", "Saldo paper inicial (USDT)", "any")}
            {numField("max_leverage", "Alavancagem máx.")}
            {numField("max_risk_per_trade_pct", "Risco por trade (%)", "0.1")}
            {numField("max_daily_loss_pct", "Perda diária máx. (%)", "0.1")}
            {numField("max_drawdown_pct", "Drawdown máx. / kill switch (%)", "0.1")}
          </div>
          {form.execution_mode === "live" && (
            <Alert variant="warning">
              Modo real só envia ordens se o servidor tiver <code>LIVE_TRADING_ENABLED=true</code>; caso contrário, o
              início da conta é recusado. Contas reais exigem 2FA ativo.{" "}
              {!totpEnabled && (
                <Link href="/dashboard/security" className="underline">
                  Ativar 2FA agora
                </Link>
              )}
            </Alert>
          )}
          {error && <Alert variant="destructive">{error}</Alert>}
          <DialogFooter>
            <Button type="submit" disabled={busy}>
              {busy ? "Conectando..." : "Conectar"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function AccountsPage() {
  const { workspace, user } = useAuth();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const canTrade = workspace?.role === "admin" || workspace?.role === "trader";

  const load = useCallback(() => {
    api
      .listAccounts()
      .then(setAccounts)
      .catch((e) => setError(errorMessage(e)));
  }, []);
  useEffect(load, [load, workspace?.id]);

  async function act(fn: () => Promise<unknown>, msg?: string) {
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (msg) setNotice(msg);
      load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function verify(a: Account) {
    setError(null);
    try {
      const r = await api.verifyAccount(a.id);
      setNotice(
        r.ok
          ? `${a.label}: credenciais válidas. Saldo ${usd(r.wallet_balance)} · trading ${r.can_trade ? "habilitado" : "desabilitado"}.`
          : `${a.label}: falha na verificação — ${r.error ?? "erro desconhecido"}`
      );
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div>
      <PageHeader title="Contas" description="Contas de futuros (USDT-M) conectadas a este workspace.">
        {canTrade && <ConnectDialog onCreated={load} totpEnabled={!!user?.totp_enabled} />}
      </PageHeader>
      {error && <Alert variant="destructive" className="mb-4">{error}</Alert>}
      {notice && <Alert variant="success" className="mb-4">{notice}</Alert>}
      <Card>
        <CardContent className="pt-6">
          {accounts === null ? (
            <EmptyState>Carregando...</EmptyState>
          ) : accounts.length === 0 ? (
            <EmptyState>Nenhuma conta conectada.</EmptyState>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Conta</TableHead>
                  <TableHead>Modo</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Limites</TableHead>
                  <TableHead>Patrimônio</TableHead>
                  <TableHead>Criada</TableHead>
                  <TableHead className="text-right">Ações</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {accounts.map((a) => (
                  <TableRow key={a.id}>
                    <TableCell>
                      <div className="font-medium">{a.label}</div>
                      <div className="text-xs text-muted-foreground">
                        {EXCHANGE_LABELS[a.exchange ?? "binance_usdm"] ?? a.exchange} · ••••{a.api_key_last4}
                      </div>
                    </TableCell>
                    <TableCell className="space-x-1">
                      <Badge variant={a.execution_mode === "live" ? "destructive" : "secondary"}>
                        {a.execution_mode === "live" ? "Real" : "Paper"}
                      </Badge>
                      {a.is_testnet && <Badge variant="outline">Testnet</Badge>}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={a.status} />
                      {a.status_reason && <div className="mt-1 text-xs text-muted-foreground">{a.status_reason}</div>}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {a.max_leverage}x · risco {pctLimit(a.max_risk_per_trade_pct)} · diária {pctLimit(a.max_daily_loss_pct)} · DD{" "}
                      {pctLimit(a.max_drawdown_pct)}
                    </TableCell>
                    <TableCell>{usd(a.last_equity ?? (a.execution_mode === "paper" ? a.paper_balance : null))}</TableCell>
                    <TableCell className="text-xs">{dateTime(a.created_at)}</TableCell>
                    <TableCell className="space-x-1 whitespace-nowrap text-right">
                      <Button size="sm" variant="outline" onClick={() => verify(a)}>
                        Verificar
                      </Button>
                      {canTrade &&
                        (a.status === "running" ? (
                          <Button size="sm" variant="outline" onClick={() => act(() => api.stopAccount(a.id), "Conta parada.")}>
                            Parar
                          </Button>
                        ) : a.status === "killed" ? (
                          workspace?.role === "admin" && (
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => act(() => api.resetKillSwitch(a.id), "Kill switch resetado.")}
                            >
                              Resetar kill switch
                            </Button>
                          )
                        ) : (
                          <Button size="sm" onClick={() => act(() => api.startAccount(a.id), "Conta iniciada.")}>
                            Iniciar
                          </Button>
                        ))}
                      {workspace?.role === "admin" && (
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() =>
                            confirm(`Remover a conta "${a.label}"? Bots e histórico vinculados serão removidos.`) &&
                            act(() => api.deleteAccount(a.id), "Conta removida.")
                          }
                        >
                          Remover
                        </Button>
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
