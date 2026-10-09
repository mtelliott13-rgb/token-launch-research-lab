# Meme Radar

A local, read-only research dashboard for Solana meme coins. It watches new launches, ranks
rising tokens by a momentum score, and records everything it sees for later backtesting.
It does not trade and holds no wallet keys. The full plan is in `CLAUDE.md`; this is Phase 1.

## Run it

```
uv run python -m radar
```

Then open http://localhost:8000. Press Ctrl+C in the terminal to stop. The first run creates
`.venv/` (the Python libraries) and `data/radar.db` (the database). Leave it running as much
as you can: the later strategy work depends on how much data has been collected.

Run the tests with `uv run pytest`.

## What is on the page

- **New launches**: every new pump.fun token as it is created, plus new pools from GeckoTerminal,
  newest first. Filters: minimum liquidity, maximum top-10 holder share, hide tokens whose mint or
  freeze authority is still enabled, graduated only.
- **Rising**: tokens ranked by momentum score v0 (open "How the score works" on the page).
  Paid DexScreener boosts are flagged and never raise the score.
- **Token drawer** (click any token): 1-minute price chart, market figures, RugCheck safety
  report, links, and a Watch button.
- **Social**: placeholder showing links from DexScreener token profiles.
- **Header chips**: the health of each data collector. Hover one for details.

## The Data page

http://localhost:8000/data shows what has been collected for the strategy lab: how many tokens
are recorded, how many have outcomes and which label each got (rug, dead, flat, runner,
moonshot), how complete the follow-up checks are, and collector health. Every token is looked
up again 1m, 5m, 15m, 1h, 4h, 24h, 72h and 7d after it is first seen, so outcomes only become
complete once a token is a week old.

## Data sources (all free, no keys)

| Source | Used for |
|---|---|
| PumpPortal websocket | New pump.fun tokens and graduations |
| DexScreener | Price, liquidity, volume, buys and sells; profiles; paid boosts |
| GeckoTerminal | New and trending pools; chart candles |
| RugCheck | Safety reports |

Things that differ from the original plan, found when the sources were checked on 2026-10-08:

- **No trade-by-trade data.** PumpPortal's per-token trade stream now needs an API key funded
  with SOL. "Watch" instead refreshes a token's price and buy/sell counts every 10 seconds.
  The `trades` table exists but stays empty.
- **Safety uses RugCheck's full report**, because the summary endpoint has no authority or
  holder data. Holder percentages exclude liquidity pools and the bonding curve.
- **Liquidity comes from DexScreener only.** It reports none while a token is on a bonding
  curve, so those show "on curve". GeckoTerminal's figure can include curve reserves; it is
  kept in the history (`snapshots.source = 'gecko'`) but not shown.
- **Token age** for pump.fun tokens is measured from when the app received the creation
  event, since the event carries no timestamp.

## Settings

`config/settings.yaml` holds poll intervals, rate limits, the refresh schedule, score weights
and the Rising requirements. Restart the app after editing it.

## Where things are

- `radar/collectors/` one module per data source
- `radar/http.py` the shared rate limiter, cache and backoff every REST call goes through
- `radar/scoring/` the momentum score and the refresh schedule
- `radar/db/schema.sql` the database tables
- `radar/api/app.py` the web server; `static/index.html` the dashboard
- `data/` the database and logs (not in git)
