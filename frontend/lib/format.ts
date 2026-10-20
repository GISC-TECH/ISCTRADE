export function num(v: string | number | null | undefined): number {
  if (v === null || v === undefined || v === "") return 0;
  return typeof v === "number" ? v : Number(v);
}

export function usd(v: string | number | null | undefined, digits = 2): string {
  return num(v).toLocaleString("pt-BR", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function qty(v: string | number | null | undefined): string {
  return num(v).toLocaleString("pt-BR", { maximumFractionDigits: 8 });
}

export function pct(v: string | number | null | undefined, digits = 2): string {
  return `${num(v).toLocaleString("pt-BR", { maximumFractionDigits: digits, minimumFractionDigits: digits })}%`;
}

export function dateTime(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const d = new Date(v);
  return d.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

export function pnlClass(v: string | number | null | undefined): string {
  const n = num(v);
  return n > 0 ? "text-green-500" : n < 0 ? "text-red-500" : "";
}
