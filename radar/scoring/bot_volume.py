""""Likely bot volume" flag v0: many trades, flat price.

Organic buying and selling moves a meme token's price. Hundreds of trades in an
hour that leave the price where it started usually mean the same wallets trading
back and forth to inflate the volume and transaction counts.

The rule only looks at the latest snapshot: trades in the last hour, and the
price change over the last hour and the last 5 minutes. A price that ran up and
came all the way back within the hour looks flat to it, so treat the flag as a
prompt to look closer, not a verdict.

Only tokens under a market cap limit are judged. A large, established token is
busy and flat for honest reasons: its pools are deep enough to absorb the trades.
"""
from __future__ import annotations

from typing import Any, Mapping


def likely_bot_volume(snap: Mapping[str, Any], cfg: Mapping[str, Any]) -> bool | None:
    """True/False, or None when the snapshot lacks the figures to tell."""
    buys, sells = snap.get("buys_h1"), snap.get("sells_h1")
    chg_h1, chg_m5, mcap = snap.get("price_chg_h1"), snap.get("price_chg_m5"), snap.get("mcap")
    if buys is None or sells is None or chg_h1 is None or mcap is None:
        return None
    return (
        mcap < cfg["max_mcap_usd"]
        and buys + sells >= cfg["min_txns_h1"]
        and abs(chg_h1) <= cfg["max_abs_price_chg_h1"]
        and abs(chg_m5 or 0.0) <= cfg["max_abs_price_chg_m5"]
    )
