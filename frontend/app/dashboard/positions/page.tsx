"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader } from "@/components/common";
import { useRealtimeEvent } from "@/components/realtime-provider";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { dateTime, decimal, num, pnlClass, qty, usd } from "@/lib/format";
import type { Account, PositionOut } from "@/lib/types";

type Row = PositionOut & { account_id: string; is_paper?: boolean; updated_at?: string };

function Stat({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className={`text-2xl font-semibold tabular-nums ${className ?? ""}`}>{value}</p>
      </CardContent>
    </Card>
  );
}

export default function PositionsPage() {
  const { workspace } = useAuth();
  const [rows, setRows] = useState<Row[] | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [mode, setMode] = useState<"all" | "live" | "paper">("all");
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);

  const fetchAll = useCallback(
    () =>
      Promise.all([api.positions(), api.listAccounts()]).then(([pos, accs]) => {
        setRows(pos);
        setAccounts(accs);
        setUpdatedAt(new Date());
        setError(null);
      }),
    []
  );

  useEffect(() => {
    fetchAll().catch((e) => setError(errorMessage(e)));
    const t = setInterval(() => fetchAll().catch(() => undefined), 15_000);
    return () => clearInterval(t);
  }, [fetchAll, workspace?.id]);

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useRealtimeEvent(["position", "account_update", "order_update", "manual"], () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => fetchAll().catch(() => undefined), 500);
  });

  const byId = useMemo(() => new Map(accounts.map((a) => [a.id, a])), [accounts]);
  const visible = useMemo(
    () =>
      (rows ?? []).filter((r) => {
        const live = byId.get(r.account_id)?.execution_mode === "live";
        return mode === "all" || (mode === "live" ? live : !live);
      }),
    [rows, byId, mode]
  );
  const totals = useMemo(() => {
    let notional = 0;
    let upnl = 0;
    let unprotected = 0;
    for (const r of visible) {
      notional += Math.abs(num(r.quantity)) * num(r.mark_price ?? r.entry_price);
      upnl += num(r.unrealized_pnl);
      if (!r.stop_loss) unprotected += 1;
    }
    return { notional, upnl, unprotected };
  }, [visible]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Posições consolidadas"
        description={`Todas as posições abertas do workspace${workspace ? ` “${workspace.name}”` : ""}, em todas as contas.`}
      >
        <div className="flex items-end gap-2">
          <div className="space-y-1">
            <Label htmlFor="pos-mode" className="text-xs">
              Modo
            </Label>
            <Select id="pos-mode" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)} className="h-9 w-36">
              <option value="all">Todas</option>
              <option value="live">Somente reais</option>
              <option value="paper">Somente paper</option>
            </Select>
          </div>
          <Button variant="outline" onClick={() => fetchAll().catch((e) => setError(errorMessage(e)))}>
            Atualizar
          </Button>
        </div>
      </PageHeader>
      {error && <Alert variant="destructive">{error}</Alert>}

      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Stat label="Posições abertas" value={String(visible.length)} />
        <Stat label="Nocional total (marcação)" value={usd(totals.notional)} />
        <Stat label="PnL não realizado" value={usd(totals.upnl)} className={pnlClass(totals.upnl)} />
        <Stat
          label="Sem stop registrado"
          value={String(totals.unprotected)}
          className={totals.unprotected ? "text-red-400" : ""}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Posições</CardTitle>
          <CardDescription>
            Fonte: banco da plataforma (sincronizado pelo reconciliador para contas reais).{" "}
            {updatedAt && `Atualizado às ${updatedAt.toLocaleTimeString("pt-BR")}.`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {rows === null ? (
            <EmptyState>Carregando...</EmptyState>
          ) : visible.length === 0 ? (
            <EmptyState>Sem posições abertas.</EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Conta</TableHead>
                    <TableHead>Símbolo</TableHead>
                    <TableHead>Lado</TableHead>
                    <TableHead className="text-right">Quantidade</TableHead>
                    <TableHead className="text-right">Entrada</TableHead>
                    <TableHead className="text-right">Marcação</TableHead>
                    <TableHead className="text-right">Nocional</TableHead>
                    <TableHead className="text-right">PnL não realizado</TableHead>
                    <TableHead className="text-right">Alav.</TableHead>
                    <TableHead className="text-right">Stop / Alvo</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {visible.map((r) => {
                    const acc = byId.get(r.account_id);
                    const q = num(r.quantity);
                    const notional = Math.abs(q) * num(r.mark_price ?? r.entry_price);
                    return (
                      <TableRow key={`${r.account_id}-${r.symbol}`}>
                        <TableCell>
                          <div className="font-medium">{acc?.label ?? "—"}</div>
                          <div className="flex gap-1 pt-0.5">
                            <Badge variant={acc?.execution_mode === "live" ? "destructive" : "secondary"}>
                              {acc?.execution_mode === "live" ? "Real" : "Paper"}
                            </Badge>
                            {acc?.is_testnet && <Badge variant="outline">Testnet</Badge>}
                          </div>
                        </TableCell>
                        <TableCell className="font-medium">
                          <Link href="/dashboard/live" className="hover:underline">
                            {r.symbol}
                          </Link>
                        </TableCell>
                        <TableCell>
                          <Badge variant={q > 0 ? "success" : "destructive"}>{q > 0 ? "Comprado" : "Vendido"}</Badge>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{qty(Math.abs(q))}</TableCell>
                        <TableCell className="text-right tabular-nums">{decimal(r.entry_price, 6)}</TableCell>
                        <TableCell className="text-right tabular-nums">{decimal(r.mark_price, 6)}</TableCell>
                        <TableCell className="text-right tabular-nums">{usd(notional)}</TableCell>
                        <TableCell className={`text-right tabular-nums ${pnlClass(r.unrealized_pnl)}`}>
                          {usd(r.unrealized_pnl)}
                        </TableCell>
                        <TableCell className="text-right">{r.leverage}x</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {r.stop_loss ? decimal(r.stop_loss, 6) : <Badge variant="destructive">sem stop</Badge>}
                          <span className="text-muted-foreground"> / {decimal(r.take_profit, 6)}</span>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
          {rows && rows.length > 0 && (
            <p className="mt-3 text-xs text-muted-foreground">
              Última atualização das posições: {dateTime(rows.map((r) => r.updated_at ?? "").sort().at(-1) || null)}
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
