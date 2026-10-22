import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PresetsPage from "@/app/dashboard/presets/page";
import { api } from "@/lib/api";
import type { BotPreset, StrategyInfo } from "@/lib/types";

const auth = vi.hoisted(() => ({ role: "admin" as string }));
vi.mock("@/components/auth-provider", () => ({
  useAuth: () => ({ workspace: { id: "ws1", name: "WS", role: auth.role } }),
}));

const preset = (over: Partial<BotPreset>): BotPreset =>
  ({
    key: "trend",
    name: "Tendência BTC",
    description: "",
    strategy_key: "ema_cross",
    strategy_name: "EMA Cross",
    interval: "1h",
    symbol: "BTCUSDT",
    leverage: 1,
    params: {},
    options: {},
    risk_level: "moderado",
    experimental: false,
    execution_mode: "paper",
    tags: [],
    source: "builtin",
    editable: false,
    ...over,
  }) as BotPreset;

const builtin = preset({});
const mine = preset({ key: "custom:p1", id: "p1", name: "Meu preset", source: "custom", editable: true });
const strategies = [{ key: "ema_cross", name: "EMA Cross", description: "", params: [] }] as StrategyInfo[];

beforeEach(() => {
  vi.spyOn(api, "listPresets").mockResolvedValue([mine, builtin]);
  vi.spyOn(api, "listStrategies").mockResolvedValue(strategies);
});

describe("PresetsPage", () => {
  it("lets a workspace admin clone a built-in", async () => {
    auth.role = "admin";
    const clone = vi.spyOn(api, "clonePreset").mockResolvedValue({ ...mine, name: "Tendência BTC (cópia)" });
    render(<PresetsPage />);
    const u = userEvent.setup();
    await u.click(await screen.findByRole("button", { name: "Clonar Tendência BTC" }));
    expect(clone).toHaveBeenCalledWith("trend");
    expect(await screen.findByText(/Tendência BTC \(cópia\)" criado/)).toBeInTheDocument();
  });

  it("traders cannot clone built-ins but can edit their own presets", async () => {
    auth.role = "trader";
    render(<PresetsPage />);
    expect(await screen.findByRole("button", { name: "Editar Meu preset" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clonar Tendência BTC" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Novo preset" })).toBeEnabled();
  });

  it("viewers only read", async () => {
    auth.role = "viewer";
    render(<PresetsPage />);
    expect(await screen.findByText("Meu preset")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Novo preset" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Clonar/ })).not.toBeInTheDocument();
  });
});
