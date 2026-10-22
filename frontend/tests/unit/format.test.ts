import { describe, expect, it } from "vitest";

import { dateOnly, decimal, num, pct, pctLimit, pnlClass, usd } from "@/lib/format";

describe("format", () => {
  it("parses Decimal strings and empty values", () => {
    expect(num("1.5")).toBe(1.5);
    expect(num(null)).toBe(0);
    expect(num("")).toBe(0);
  });

  it("formats limits without trailing zeros (regression: 'limite 1.0000000000%')", () => {
    expect(pctLimit("1.0000000000")).toBe("1%");
    expect(pctLimit("0.25")).toBe("0,25%");
    expect(pctLimit(null)).toBe("—");
    expect(pct(1.5)).toBe("1,50%");
  });

  it("formats money, decimals, dates and PnL colours in pt-BR", () => {
    expect(usd("1234.5")).toMatch(/US\$\s?1\.234,50/);
    expect(decimal("0.00100000")).toBe("0,001");
    expect(dateOnly("2026-10-22T10:00:00Z")).toBe("22/10/2026");
    expect(pnlClass("-1")).toBe("text-red-500");
    expect(pnlClass(2)).toBe("text-green-500");
    expect(pnlClass(0)).toBe("");
  });
});
