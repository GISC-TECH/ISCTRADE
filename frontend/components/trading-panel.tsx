"use client";

// Manual order ticket. The backend applies the same pipeline as bots (kill switches -> filters -> RiskManager ->
// executor of the account's mode); this form only pre-validates and always asks for explicit confirmation.
import { useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Select } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage } from "@/lib/api";
import type { Account, ManualOrderCreate } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface TicketCheck {
  ok: boolean;
  errors: string[];
  entry: number;
  risk: number;
  riskPct: number;
  notional: number;
  margin: number;
  rr: number | null;
}

/** Client-side pre-checks (the RiskManager on the server is authoritative). */
export function checkTicket(
  t: { side: "BUY" | "SELL"; type: "MARKET" | "LIMIT"; quantity: string; price: string; stop: string; tp: string; leverage: number },
  marketPrice: number | null,
  equity: number,
  maxLeverage: number
): TicketCheck {
  const errors: string[] = [];
  const qty = Number(t.quantity);
  const entry = t.type === "LIMIT" ? Number(t.price) : (marketPrice ?? NaN);
  const stop = Number(t.stop);
  const tp = t.tp ? Number(t.tp) : null;
  if (!(qty > 0)) errors.push("Quantidade deve ser maior que zero");
  if (t.type === "LIMIT" && !(Number(t.price) > 0)) errors.push("Ordem LIMIT exige preço");
  if (!Number.isFinite(entry) || entry <= 0) errors.push("Preço de mercado indisponível");
  if (!t.stop || !(stop > 0)) errors.push("Stop loss é obrigatório");
  else if (Number.isFinite(entry)) {
    if (t.side === "BUY" && stop >= entry) errors.push("Stop de compra deve ficar abaixo da entrada");
    if (t.side === "SELL" && stop <= entry) errors.push("Stop de venda deve ficar acima da entrada");
  }
  if (tp !== null && Number.isFinite(entry)) {
    if (!(tp > 0)) errors.push("Take profit inválido");
    else if (t.side === "BUY" && tp <= entry) errors.push("Take profit de compra deve ficar acima da entrada");
    else if (t.side === "SELL" && tp >= entry) errors.push("Take profit de venda deve ficar abaixo da entrada");
  }
  if (t.leverage < 1 || t.leverage > maxLeverage) errors.push(`Alavancagem deve estar entre 1x e ${maxLeverage}x`);
  const risk = qty > 0 && stop > 0 && entry > 0 ? qty * Math.abs(entry - stop) : 0;
  const notional = qty > 0 && entry > 0 ? qty * entry : 0;
  return {
    ok: errors.length === 0,
    errors,
    entry,
    risk,
    riskPct: equity > 0 ? (risk / equity) * 100 : 0,
    notional,
    margin: t.leverage > 0 ? notional / t.leverage : notional,
    rr: tp !== null && risk > 0 ? (qty * Math.abs(tp - entry)) / risk : null,
  };
}

const fmt = (v: number, d = 2) =>
  Number.isFinite(v) ? v.toLocaleString("pt-BR", { maximumFractionDigits: d, minimumFractionDigits: 0 }) : "—";

export function TradingPanel({
  account,
  symbol,
  marketPrice,
  equity,
  globalKill,
  canTrade,
  onDone,
}: {
  account: Account;
  symbol: string;
  marketPrice: number | null;
  equity: number;
  globalKill: boolean;
  canTrade: boolean;
  onDone: () => void;
}) {
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [type, setType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [stop, setStop] = useState("");
  const [tp, setTp] = useState("");
  const [leverage, setLeverage] = useState(1);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  const accountKilled = account.status === "killed";
  const blocked = accountKilled || globalKill || !canTrade;
  const check = checkTicket(
    { side, type, quantity, price, stop, tp, leverage },
    marketPrice,
    equity,
    account.max_leverage
  );
  const isLive = account.execution_mode === "live";

  const submit = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const body: ManualOrderCreate = {
        account_id: account.id,
        symbol,
        side,
        type,
        quantity,
        stop_loss: stop,
        leverage,
        confirm: true,
        ...(type === "LIMIT" ? { price } : {}),
        ...(tp ? { take_profit: tp } : {}),
      };
      const res = await api.placeManualOrder(body);
      if (res.ok) {
        const what = res.status === "NEW" ? "Ordem LIMIT registrada" : "Ordem executada";
        setMessage({ ok: true, text: `${what} (${res.mode === "paper" ? "simulada" : "real"}): ${res.status}` });
        setQuantity("");
      } else {
        setMessage({ ok: false, text: `Rejeitada pelo gerenciador de risco: ${res.reason}` });
      }
      onDone();
    } catch (e) {
      setMessage({ ok: false, text: errorMessage(e) });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const applyLeverage = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await api.setLeverage(account.id, symbol, leverage);
      setMessage({ ok: true, text: `Alavancagem de ${symbol} ajustada para ${leverage}x` });
      onDone();
    } catch (e) {
      setMessage({ ok: false, text: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={isLive ? "destructive" : "secondary"}>{isLive ? "REAL" : "PAPER (simulado)"}</Badge>
        {account.is_testnet && <Badge variant="outline">testnet</Badge>}
        <span className="text-xs text-muted-foreground">{symbol}</span>
      </div>
      {accountKilled && (
        <Alert variant="destructive">Kill switch da conta ativo: apenas fechamento de posições é permitido.</Alert>
      )}
      {globalKill && <Alert variant="destructive">Kill switch global ativo: novas entradas bloqueadas.</Alert>}
      {!canTrade && <Alert>Seu papel no workspace é somente leitura.</Alert>}

      <div className="grid grid-cols-2 gap-2">
        <Button
          type="button"
          variant={side === "BUY" ? "default" : "outline"}
          className={cn(side === "BUY" && "bg-green-600 hover:bg-green-600/90")}
          onClick={() => setSide("BUY")}
        >
          Comprar / Long
        </Button>
        <Button
          type="button"
          variant={side === "SELL" ? "default" : "outline"}
          className={cn(side === "SELL" && "bg-red-600 hover:bg-red-600/90")}
          onClick={() => setSide("SELL")}
        >
          Vender / Short
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div>
          <Label htmlFor="t-type">Tipo</Label>
          <Select id="t-type" value={type} onChange={(e) => setType(e.target.value as "MARKET" | "LIMIT")}>
            <option value="MARKET">Mercado</option>
            <option value="LIMIT">Limite</option>
          </Select>
        </div>
        <div>
          <Label htmlFor="t-qty">Quantidade</Label>
          <Input id="t-qty" inputMode="decimal" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </div>
        {type === "LIMIT" && (
          <div className="col-span-2">
            <Label htmlFor="t-price">Preço limite</Label>
            <Input id="t-price" inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
          </div>
        )}
        <div>
          <Label htmlFor="t-sl">Stop loss *</Label>
          <Input id="t-sl" inputMode="decimal" required value={stop} onChange={(e) => setStop(e.target.value)} />
        </div>
        <div>
          <Label htmlFor="t-tp">Take profit</Label>
          <Input id="t-tp" inputMode="decimal" value={tp} onChange={(e) => setTp(e.target.value)} />
        </div>
        <div>
          <Label htmlFor="t-lev">Alavancagem (máx. {account.max_leverage}x)</Label>
          <Input
            id="t-lev"
            type="number"
            min={1}
            max={account.max_leverage}
            value={leverage}
            onChange={(e) => setLeverage(Math.max(1, Math.round(Number(e.target.value) || 1)))}
          />
        </div>
        <div className="flex items-end">
          <Button type="button" variant="outline" className="w-full" disabled={blocked || busy} onClick={applyLeverage}>
            Aplicar alavancagem
          </Button>
        </div>
      </div>

      <div className="rounded-md border p-2 text-xs text-muted-foreground">
        <div className="flex justify-between">
          <span>Entrada estimada</span>
          <span>{fmt(check.entry, 6)}</span>
        </div>
        <div className="flex justify-between">
          <span>Risco até o stop</span>
          <span>
            {fmt(check.risk)} USDT ({fmt(check.riskPct)}% · limite {account.max_risk_per_trade_pct}%)
          </span>
        </div>
        <div className="flex justify-between">
          <span>Nocional / margem</span>
          <span>
            {fmt(check.notional)} / {fmt(check.margin)} USDT
          </span>
        </div>
        {check.rr !== null && (
          <div className="flex justify-between">
            <span>Retorno/risco</span>
            <span>{fmt(check.rr)}R</span>
          </div>
        )}
      </div>
      {quantity && check.errors.length > 0 && (
        <ul className="list-disc pl-5 text-xs text-red-500">
          {check.errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      <Button className="w-full" disabled={blocked || busy || !check.ok} onClick={() => setConfirming(true)}>
        Revisar ordem
      </Button>
      {message && <Alert variant={message.ok ? "default" : "destructive"}>{message.text}</Alert>}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirmar ordem {isLive ? "REAL" : "simulada (paper)"}</DialogTitle>
            <DialogDescription>
              A ordem passa pelo gerenciador de risco antes de ser executada{isLive ? " na Binance" : ""}.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1 text-sm">
            <p>
              <strong>{side === "BUY" ? "COMPRA" : "VENDA"}</strong> {quantity} {symbol} ·{" "}
              {type === "LIMIT" ? `limite ${price}` : "a mercado"} · {leverage}x
            </p>
            <p>
              Stop loss: <strong>{stop}</strong>
              {tp ? ` · Take profit: ${tp}` : ""}
            </p>
            <p className="text-muted-foreground">
              Risco estimado: {fmt(check.risk)} USDT ({fmt(check.riskPct)}% do patrimônio)
            </p>
            {isLive && (
              <Alert variant="destructive">Conta em modo REAL: esta ordem usa dinheiro de verdade.</Alert>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirming(false)} disabled={busy}>
              Cancelar
            </Button>
            <Button onClick={submit} disabled={busy}>
              {busy ? "Enviando…" : "Confirmar"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
