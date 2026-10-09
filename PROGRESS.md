# Progress

Last updated: 2026-10-09, after the first 20-hour run.

## Start the dashboard

In a terminal:

```
cd ~/meme-radar
uv run python -m radar
```

Then open http://localhost:8000. Press Ctrl+C in that terminal to stop.
If the terminal says `uv: command not found`, use `~/.local/bin/uv run python -m radar`.

## Done (Phase 1)

Phase 1's "done when" test is met: one command starts everything, new tokens appear live,
the Rising list is ranked, and token details work.

- All data sources were called and checked before any client code was written. The findings
  are in `README.md` and at the top of each file in `radar/collectors/`.
- Collectors, all running inside the one app:
  - PumpPortal: new pump.fun tokens and graduations, one connection, reconnects with backoff.
  - DexScreener: batched price/liquidity/volume refresh on a tiered schedule; profiles; paid boosts.
  - GeckoTerminal: new pools, trending pools, 1-minute chart candles.
  - RugCheck: full safety report for Rising tokens, graduated tokens, and any token opened.
- Shared rate limiter, cache and 429 backoff per source (`radar/http.py`).
- SQLite database at `data/radar.db` (WAL mode): tokens, snapshots, migrations, safety,
  raw events, plus an empty `trades` table.
- Dashboard: New launches with the four filters, Rising with the score explained on the page,
  token drawer (chart, market figures, safety report, links, Watch), Social placeholder,
  collector health chips.
- Momentum score v0, weights in `config/settings.yaml`.
- 63 tests passing (`uv run pytest`).
- Structured logs in `data/logs/radar.log`.

## Fixed after the first 20-hour run (2026-10-09)

- **RugCheck retry loop.** The Rising list asked for a safety check on every 10-second pass for
  any token without a saved report, and a refused request saved nothing, so one token was
  re-requested 92 times in 51 minutes. A "no report" answer (HTTP 400) is now saved per token
  and retried after 10 minutes, then 1 hour, then not at all. An address RugCheck calls
  "invalid token mint" is dropped after one try. If RugCheck is unreachable, the token is left
  alone for 2 minutes. The dashboard shows "No RugCheck report" instead of "Unchecked".
- **Pool addresses.** Checked the database: none of the addresses sent to RugCheck is a pool,
  pair or bonding-curve address. The token in the loop is a real mint that RugCheck does not know.
- **Logging.** A failure that repeats is logged once, then one line with the count every
  10 minutes, then one line when it recovers (`radar/faillog.py`).
- **Holders column** on New launches and Rising, from the latest RugCheck report, so it is
  empty for tokens that have not had a safety check.
- **"Likely bot volume" badge**, version 0: at least 300 trades in the last hour with the price
  within 1% over both the last hour and the last 5 minutes, and a market cap under $1M (large
  tokens are busy and flat for honest reasons). Thresholds in `config/settings.yaml`.
- **Rising is for meme coins only.** Anything above $50M market cap is left out, and so is a
  fixed list of stablecoins and major tokens (USDC, USDT, SOL, JUP, PUMP, BOME, BONK, WIF, TRUMP,
  PENGU, Fartcoin, WBTC, cbBTC, WETH) whatever their size. Both are under `rising:` in
  `config/settings.yaml`. These tokens are still recorded in the database.

## Left in Phase 1

Nothing required is missing. These are loose ends:

- **Longer run.** The app has only been run for about 15 minutes at a time. It needs a run of
  several hours to confirm it stays healthy and to see real disk growth (estimated at roughly
  200 MB per day, extrapolated from those short runs).
- **Rate-limit backoff seen live.** It is covered by tests, but no source actually rate-limited
  us during the live runs.
- **"LP locked %"** is our own calculation from the full RugCheck report. It was close to
  RugCheck's own figure on the one multi-pool token compared (24.6% vs 25.0%), not identical.
- **Light color theme** has only been looked at on an empty page. Dark was checked properly.
- **Chart color accessibility check** was not run (the checker lives outside the project folder).
- **Stray files in `data/`**: see open decision 1.

Deliberately left for later phases: the disabled X/Twitter provider and the `social_posts`
table (Phase 3), the `outcomes` table and follow-up snapshots (Phase 2).

## Open decisions

1. **Stray database files: resolved, pending a decision to delete.** The `radar 2/3/4`
   `.db-wal` / `.db-shm` pairs were made on 2026-10-08 between 18:15 and 18:28, while the folder
   was still in Documents; each one's creation time matches an app start in the log. None has
   appeared in the 20 hours of running since the move, which fits the file-syncing explanation
   (not confirmed: checking the Mac's sync settings means looking outside this folder). They are
   leftovers from short test runs, not part of the live database, and now sit in `data/archive/`.
   They can be deleted whenever the user says so.

2. **PumpPortal trade data (needed before Phase 2b).** PumpPortal now refuses per-token trade
   subscriptions without an API key funded with at least 0.02 SOL, so there is no free
   trade-by-trade stream. Today "Watch" refreshes price and buy/sell counts every 10 seconds
   from DexScreener, and the `trades` table is empty. The shadow bot's early lane (Lane A)
   needs individual trades. Options:
   - a PumpPortal key funded with about 0.02 SOL (the key is tied to a wallet they hold, which
     conflicts with the no-wallet rule);
   - decode trades ourselves from a free Solana RPC key such as Helius (more work, stays free);
   - limit Phase 2b to the post-graduation lane (Lane B), which works with the data we have.

3. **Momentum score v0.** It favours very young tokens with one-sided buying. Leave as is
   until Phase 2 outcome data exists to tune it, or adjust now.

## Environment notes

- uv is installed at `~/.local/bin/uv`. Project libraries are in `.venv/`.
- The folder was moved from `~/Documents/meme-radar` to `~/meme-radar` on 2026-10-08 at 19:07.
  `.venv/` was rebuilt on 2026-10-09 so its launchers point at the new location.
- Git name and email are set for this project only (in `.git/config`).
- The working rules for this project are in `CLAUDE.md` under "Working rules".
