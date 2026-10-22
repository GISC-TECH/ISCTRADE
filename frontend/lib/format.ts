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

/** Percentage for limits/settings stored as Decimal strings ("1.0000000000" -> "1%", "0.25" -> "0,25%"). */
export function pctLimit(v: string | number | null | undefined, maxDigits = 4): string {
  if (v === null || v === undefined || v === "") return "—";
  return `${num(v).toLocaleString("pt-BR", { minimumFractionDigits: 0, maximumFractionDigits: maxDigits })}%`;
}

/** Plain number in pt-BR without trailing zeros (Decimal strings from the API). */
export function decimal(v: string | number | null | undefined, maxDigits = 8): string {
  if (v === null || v === undefined || v === "") return "—";
  return num(v).toLocaleString("pt-BR", { maximumFractionDigits: maxDigits });
}

export function dateOnly(v: string | null | undefined): string {
  if (!v) return "—";
  const [y, m, d] = v.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
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
