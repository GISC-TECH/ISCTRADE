"use client";

import { useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { EmptyState, PageHeader, Pager } from "@/components/common";
import { AuditTable, LIMIT, usePaged } from "@/components/paged";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { dateTime, pnlClass, qty, usd } from "@/lib/format";
import type { Account, Order, Trade } from "@/lib/types";

function OrdersTab({ accountId, symbol }: { accountId: string; symbol: string }) {
  const { page, error, offset, go } = usePaged<Order>(
    (o) => api.orders({ account_id: accountId || undefined, symbol: symbol || undefined, limit: LIMIT, offset: o }),
    [accountId, symbol]
  );
  if (error) return <Alert variant="destructive">{error}</Alert>;
  if (!page) return <EmptyState>Carregando...</EmptyState>;
  if (!page.items.length) return <EmptyState>Nenhuma ordem.</EmptyState>;
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Data</TableHead>
            <TableHead>Símbolo</TableHead>
            <TableHead>Lado</TableHead>
            <TableHead>Tipo</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Qtd. (exec.)</TableHead>
            <TableHead>Preço médio</TableHead>
            <TableHead>Stop</TableHead>
            <TableHead>Obs.</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {page.items.map((o) => (
            <TableRow key={o.id}>
              <TableCell className="text-xs">{dateTime(o.created_at)}</TableCell>
              <TableCell>
                {o.symbol} {o.is_paper && <Badge variant="outline">paper</Badge>}
              </TableCell>
              <TableCell className={o.side === "BUY" ? "text-green-500" : "text-red-500"}>{o.side}</TableCell>
              <TableCell className="text-xs">
                {o.type}
                {o.reduce_only && " · RO"}
              </TableCell>
              <TableCell>
                <Badge variant={o.status === "REJECTED" ? "destructive" : o.status === "FILLED" ? "success" : "secondary"}>
                  {o.status}
                </Badge>
              </TableCell>
              <TableCell>
                {qty(o.quantity)} ({qty(o.executed_qty)})
              </TableCell>
              <TableCell>{o.avg_price ? qty(o.avg_price) : o.price ? qty(o.price) : "—"}</TableCell>
              <TableCell>{o.stop_loss ? qty(o.stop_loss) : "—"}</TableCell>
              <TableCell className="max-w-xs truncate text-xs text-muted-foreground" title={o.reason ?? ""}>
                {o.reason ?? ""}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pager total={page.total} limit={LIMIT} offset={offset} onChange={go} />
    </>
  );
}

function TradesTab({ accountId, symbol }: { accountId: string; symbol: string }) {
  const { page, error, offset, go } = usePaged<Trade>(
    (o) => api.trades({ account_id: accountId || undefined, symbol: symbol || undefined, limit: LIMIT, offset: o }),
    [accountId, symbol]
  );
  if (error) return <Alert variant="destructive">{error}</Alert>;
  if (!page) return <EmptyState>Carregando...</EmptyState>;
  if (!page.items.length) return <EmptyState>Nenhuma execução.</EmptyState>;
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Data</TableHead>
            <TableHead>Símbolo</TableHead>
            <TableHead>Lado</TableHead>
            <TableHead>Qtd.</TableHead>
            <TableHead>Preço</TableHead>
            <TableHead>Taxa</TableHead>
            <TableHead>PnL realizado</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {page.items.map((t) => (
            <TableRow key={t.id}>
              <TableCell className="text-xs">{dateTime(t.executed_at)}</TableCell>
              <TableCell>
                {t.symbol} {t.is_paper && <Badge variant="outline">paper</Badge>}
              </TableCell>
              <TableCell className={t.side === "BUY" ? "text-green-500" : "text-red-500"}>{t.side}</TableCell>
              <TableCell>{qty(t.quantity)}</TableCell>
              <TableCell>{qty(t.price)}</TableCell>
              <TableCell>{usd(t.fee, 4)}</TableCell>
              <TableCell className={pnlClass(t.realized_pnl)}>{usd(t.realized_pnl)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <Pager total={page.total} limit={LIMIT} offset={offset} onChange={go} />
    </>
  );
}

export default function HistoryPage() {
  const { workspace } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountId, setAccountId] = useState("");
  const [symbol, setSymbol] = useState("");
  const canAudit = workspace?.role === "admin" || workspace?.role === "trader";

  useEffect(() => {
    api.listAccounts().then(setAccounts).catch(() => undefined);
  }, [workspace?.id]);

  return (
    <div>
      <PageHeader title="Histórico" description="Ordens, execuções e trilha de auditoria do workspace." />
      <div className="mb-4 flex flex-wrap gap-3">
        <div className="space-y-1">
          <Label htmlFor="h-account">Conta</Label>
          <Select id="h-account" className="w-56" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
            <option value="">Todas</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.label}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="h-symbol">Símbolo</Label>
          <Input
            id="h-symbol"
            className="w-40"
            placeholder="BTCUSDT"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          />
        </div>
      </div>
      <Card>
        <CardContent className="pt-6">
          <Tabs defaultValue="orders">
            <TabsList>
              <TabsTrigger value="orders">Ordens</TabsTrigger>
              <TabsTrigger value="trades">Execuções</TabsTrigger>
              {canAudit && <TabsTrigger value="audit">Auditoria</TabsTrigger>}
            </TabsList>
            <TabsContent value="orders" className="pt-3">
              <OrdersTab accountId={accountId} symbol={symbol} />
            </TabsContent>
            <TabsContent value="trades" className="pt-3">
              <TradesTab accountId={accountId} symbol={symbol} />
            </TabsContent>
            {canAudit && (
              <TabsContent value="audit" className="pt-3">
                <AuditTable
                  fetcher={(o) => api.audit({ account_id: accountId || undefined, limit: LIMIT, offset: o })}
                  deps={[accountId, workspace?.id]}
                />
              </TabsContent>
            )}
          </Tabs>
        </CardContent>
      </Card>
    </div>
  );
}
