import { describe, expect, it } from "vitest";

import { presetToWrite, splitTags } from "@/lib/presets";
import type { BotPreset } from "@/lib/types";

const preset: BotPreset = {
  key: "trend_conservative_btc_4h",
  name: "Tendência",
  description: "desc",
  strategy_key: "ema_adx_trend",
  strategy_name: "EMA + ADX",
  interval: "4h",
  symbol: "BTCUSDT",
  leverage: 1,
  params: { risk_fraction: 0.5 },
  options: {} as BotPreset["options"],
  risk_level: "conservador",
  experimental: false,
  execution_mode: "paper",
  tags: ["tendência"],
  source: "builtin",
};

describe("presets helpers", () => {
  it("splits, trims and de-duplicates tags like the API (max 10)", () => {
    expect(splitTags(" a, b ,a,, c ")).toEqual(["a", "b", "c"]);
    expect(splitTags(Array.from({ length: 15 }, (_, i) => `t${i}`).join(","))).toHaveLength(10);
    expect(splitTags("x".repeat(40))[0]).toHaveLength(30);
  });

  it("copies editable fields and fills option defaults (never an execution mode)", () => {
    const w = presetToWrite(preset);
    expect(w).toMatchObject({ name: "Tendência", strategy_key: "ema_adx_trend", leverage: 1, tags: ["tendência"] });
    expect(w.options.news_filter).toBeDefined();
    expect(w).not.toHaveProperty("execution_mode");
    w.params.risk_fraction = 1;
    expect(preset.params.risk_fraction).toBe(0.5); // no shared references
  });
});
