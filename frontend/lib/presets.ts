import { withDefaults } from "@/components/bot-options";
import type { BotPreset, PresetWrite } from "@/lib/types";

/** "a, b ,a,, c" -> ["a", "b", "c"] (trimmed, unique, max 10 tags of 30 chars, like the API). */
export function splitTags(raw: string): string[] {
  const out: string[] = [];
  for (const part of raw.split(",")) {
    const tag = part.trim().slice(0, 30);
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out.slice(0, 10);
}

/** Editable fields of a preset (for the edit form or a clone body). */
export function presetToWrite(p: BotPreset): PresetWrite {
  return {
    name: p.name,
    description: p.description,
    strategy_key: p.strategy_key,
    interval: p.interval,
    symbol: p.symbol,
    leverage: p.leverage,
    params: { ...p.params },
    options: withDefaults(p.options),
    risk_level: p.risk_level,
    tags: [...p.tags],
  };
}
