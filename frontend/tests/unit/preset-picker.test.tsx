import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PresetPicker } from "@/components/preset-picker";
import type { BotPreset } from "@/lib/types";

const make = (key: string, name: string, source: "builtin" | "custom"): BotPreset =>
  ({
    key,
    name,
    description: `${name} desc`,
    strategy_key: "ema_cross",
    strategy_name: "EMA Cross",
    interval: "1h",
    symbol: "BTCUSDT",
    leverage: 2,
    params: {},
    options: {},
    risk_level: "moderado",
    experimental: false,
    execution_mode: "paper",
    tags: [],
    source,
  }) as unknown as BotPreset;

const presets = [make("custom:1", "Meu preset", "custom"), make("trend", "Tendência BTC", "builtin")];

describe("PresetPicker", () => {
  it("groups workspace presets before built-ins and reports the choice", async () => {
    const onChange = vi.fn();
    render(<PresetPicker id="p" presets={presets} value="" onChange={onChange} />);
    const select = screen.getByLabelText("Robô pronto (opcional)");
    const groups = within(select).getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("label"))).toEqual(["Personalizados do workspace", "Robôs prontos"]);
    await userEvent.setup().selectOptions(select, "custom:1");
    expect(onChange).toHaveBeenCalledWith(presets[0]);
  });

  it("describes the selected preset and always shows paper", () => {
    render(<PresetPicker id="p" presets={presets} value="trend" onChange={vi.fn()} />);
    expect(screen.getByText("Tendência BTC desc")).toBeInTheDocument();
    expect(screen.getByText("paper")).toBeInTheDocument();
    expect(screen.getByText(/EMA Cross · BTCUSDT · 1h · 2x/)).toBeInTheDocument();
  });

  it("renders nothing without presets", () => {
    const { container } = render(<PresetPicker id="p" presets={[]} value="" onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
