import { EquityChart, type EquityPoint } from "@/components/equity-chart";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

// Mock data — substituir por chamadas a lib/api.ts (accountDashboard / history/equity).
const summary = { balance: 10250.42, pnlToday: 125.37, pnlTotal: 250.42, openPositions: 2 };

const positions = [
  { symbol: "BTCUSDT", side: "LONG", size: 0.01, entry: 64250.0, pnl: 42.1 },
  { symbol: "ETHUSDT", side: "SHORT", size: 0.2, entry: 3120.5, pnl: -8.4 },
];

const equity: EquityPoint[] = Array.from({ length: 30 }, (_, i) => ({
  date: `D${i + 1}`,
  equity: Math.round((10000 + i * 9 + Math.sin(i / 2) * 60) * 100) / 100,
}));

const usd = (v: number) => v.toLocaleString("pt-BR", { style: "currency", currency: "USD" });

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="text-sm text-muted-foreground">Dados de exemplo (mock). Nenhuma conta conectada.</p>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader>
            <CardDescription>Saldo (USDT)</CardDescription>
            <CardTitle>{usd(summary.balance)}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>PnL hoje / total</CardDescription>
            <CardTitle className={summary.pnlToday >= 0 ? "text-green-500" : "text-red-500"}>
              {usd(summary.pnlToday)}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">Total: {usd(summary.pnlTotal)}</CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardDescription>Posições abertas</CardDescription>
            <CardTitle>{summary.openPositions}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Curva de patrimônio</CardTitle>
          <CardDescription>Placeholder — últimos 30 dias</CardDescription>
        </CardHeader>
        <CardContent>
          <EquityChart data={equity} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Posições abertas</CardTitle>
        </CardHeader>
        <CardContent>
          <table className="w-full text-sm">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-2">Símbolo</th>
                <th>Lado</th>
                <th>Tamanho</th>
                <th>Entrada</th>
                <th>PnL</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol} className="border-t">
                  <td className="py-2">{p.symbol}</td>
                  <td>{p.side}</td>
                  <td>{p.size}</td>
                  <td>{p.entry}</td>
                  <td className={p.pnl >= 0 ? "text-green-500" : "text-red-500"}>{usd(p.pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}
