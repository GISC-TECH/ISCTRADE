"""Map news text to crypto assets and Binance USDT-M symbols."""

from __future__ import annotations

import re
from collections.abc import Iterable

# Common names -> base asset. Matched case-insensitively on word boundaries.
ASSET_NAMES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "btc", "xbt"),
    "ETH": ("ethereum", "ether", "eth"),
    "BNB": ("binance coin", "bnb"),
    "SOL": ("solana",),
    "XRP": ("xrp", "ripple"),
    "DOGE": ("dogecoin", "doge"),
    "ADA": ("cardano",),
    "AVAX": ("avalanche", "avax"),
    "TRX": ("tron",),
    "TON": ("toncoin",),
    "DOT": ("polkadot",),
    "LINK": ("chainlink",),
    "MATIC": ("polygon",),
    "POL": ("polygon ecosystem token",),
    "LTC": ("litecoin", "ltc"),
    "BCH": ("bitcoin cash",),
    "SHIB": ("shiba inu",),
    "PEPE": ("pepe",),
    "ARB": ("arbitrum",),
    "OP": ("optimism",),
    "SUI": ("sui network",),
    "APT": ("aptos",),
    "ATOM": ("cosmos",),
    "NEAR": ("near protocol",),
    "FIL": ("filecoin",),
    "UNI": ("uniswap",),
    "AAVE": ("aave",),
    "ETC": ("ethereum classic",),
    "XLM": ("stellar",),
    "HBAR": ("hedera",),
    "INJ": ("injective",),
    "TIA": ("celestia",),
    "SEI": ("sei network",),
    "WLD": ("worldcoin",),
    "ENA": ("ethena",),
    "HYPE": ("hyperliquid",),
}

# Tickers matched case-sensitively as whole words (or with a $ prefix).
KNOWN_TICKERS: frozenset[str] = frozenset(ASSET_NAMES) | frozenset(
    {"FET", "RNDR", "RENDER", "STX", "IMX", "MKR", "LDO", "CRV", "JUP", "PYTH", "WIF", "BONK", "FLOKI", "ORDI", "TAO"}
)
# Tickers that are also ordinary words: only accepted with a "$" prefix.
AMBIGUOUS: frozenset[str] = frozenset(
    {"ONE", "GAS", "NEAR", "MASK", "PEOPLE", "BOND", "HIGH", "LOW", "ID", "T", "OP", "SUI", "SEI", "TON", "UNI", "DOT"}
)
# Uppercase words common in headlines that must never be read as tickers.
STOPWORDS: frozenset[str] = frozenset(
    {"SEC", "ETF", "ETFS", "USD", "CEO", "CFO", "IPO", "FED", "FOMC", "CPI", "PPI", "GDP", "NFT", "DEX", "CEX", "API",
     "USA", "UK", "EU", "NEW", "THE", "AND", "FOR", "NOT", "ALL", "TOP", "BIG", "AI", "DAO", "TVL", "ATH", "OTC",
     "KYC", "AML", "DOJ", "CFTC", "IMF", "ECB", "BOJ", "FBI", "NYSE", "LIVE", "NOW", "WHY", "HOW", "WHAT"}
)  # fmt: skip
# Assets whose USDT-M contract is quoted per 1000 units.
THOUSAND_PREFIX: frozenset[str] = frozenset({"PEPE", "SHIB", "BONK", "FLOKI", "LUNC", "XEC", "SATS", "RATS", "CAT"})

_NAME_PATTERNS = [
    (asset, re.compile(r"(?<![\w$])(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.IGNORECASE))
    for asset, names in ASSET_NAMES.items()
]
_TICKER = re.compile(r"(?<![\w])(\$?)([A-Z][A-Z0-9]{1,9})(?![\w])")


def extract_assets(text: str, extra_tickers: Iterable[str] = ()) -> list[str]:
    """Base assets mentioned in `text`, in order of first appearance."""
    found: dict[str, int] = {}
    for asset, pattern in _NAME_PATTERNS:
        m = pattern.search(text)
        if m:
            found.setdefault(asset, m.start())
    tickers = KNOWN_TICKERS | {t.upper() for t in extra_tickers if len(t) >= 3}
    tickers -= STOPWORDS
    for m in _TICKER.finditer(text):
        dollar, tick = m.group(1), m.group(2)
        if tick not in tickers:
            continue
        if tick in AMBIGUOUS and not dollar:
            continue
        found.setdefault(tick, m.start())
    return [a for a, _ in sorted(found.items(), key=lambda kv: kv[1])]


def symbol_for(asset: str, universe: set[str] | None = None) -> str | None:
    asset = asset.upper()
    candidates = [f"{asset}USDT", f"1000{asset}USDT"]
    if asset in THOUSAND_PREFIX:
        candidates.reverse()
    if universe:
        return next((c for c in candidates if c in universe), None)
    return candidates[0]


def base_asset(symbol: str) -> str:
    base = symbol.upper().removesuffix("USDT")
    return base[4:] if base.startswith("1000") and len(base) > 4 else base


def tag(text: str, hints: Iterable[str] = (), universe: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Return (assets, symbols) for a news text plus source-provided ticker hints."""
    universe_bases = {base_asset(s) for s in universe} if universe else set()
    assets = []
    for a in [*(h.upper() for h in hints), *extract_assets(text, universe_bases)]:
        if a and a not in assets:
            assets.append(a)
    symbols = []
    for a in assets:
        s = symbol_for(a, universe)
        if s and s not in symbols:
            symbols.append(s)
    return assets[:10], symbols[:10]
