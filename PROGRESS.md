# Progress

Last updated: 2026-10-09, Phase 2 built, with peak/fate verdicts, data retention, theme tags, the Patterns page and "Why it's rising".

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
- 115 tests passing (`uv run pytest`), including Phase 2.
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
table (Phase 3).

## Done (Phase 2, built 2026-10-09)

- **Follow-up checks.** Every token is looked up again on DexScreener 1m, 5m, 15m, 1h, 4h, 24h,
  72h and 7d after we first saw it, including tokens the live refresh has given up on. Each
  check is saved in the `followups` table as done, no_data (DexScreener did not list it),
  skipped (not made in time) or backfilled. Code: `radar/lab/followups.py` and
  `radar/collectors/followup.py`.
- **Budget, measured on the 20.5-hour run.** About 43,500 new tokens a day (not 30,000), about
  1,050 graduations a day. Following all of them is about 348,000 lookups a day, roughly
  8 requests a minute, against our own cap of 120 a minute. The live refresh already makes most
  early checks for free. Follow-ups are capped at about 20 requests a minute. When behind:
  graduated tokens first, then tokens at or above $15k market cap or on Rising, trending or
  watched, then the rest.
- **Outcomes.** The `outcomes` table holds p0, peak multiple in the first 24h, time to peak,
  multiples at 1h, 24h, 72h and 7d, drawdown before the peak, rugged, dead at day 7, and two
  verdicts: `peak` (moonshot / runner / flat, from the 24h peak) and `fate` (rugged / dead /
  alive at day 7), so a token that pumped and then rugged shows both. Either is empty until it
  can be told. Rules are documented at the top of `radar/lab/outcomes.py`; thresholds are
  under `outcomes:` in `config/settings.yaml`. `final` means day 7 has passed, `complete` means
  final with nothing missing, and `missing` lists what is not there.
- **Existing tokens.** On the first start with this code, tokens already in the database get
  their past checkpoints filled from snapshots where the snapshots support it, and marked
  skipped where they do not (`radar/lab/backfill.py`). It runs once and took about 6 seconds on a
  copy of the database. Nothing is complete yet: no token is 7 days old.
- **Data page** at http://localhost:8000/data: tokens recorded, label breakdown, which outcome
  fields are filled, follow-up coverage per checkpoint, collector health.

Things to know about Phase 2 data:

- p0 is the first DexScreener price we see, usually about 90 seconds after launch, because
  DexScreener does not list a new pump.fun token sooner. The "1m" check is accepted up to 2m.
- The peak is only as fine as our snapshots. A spike between two looks is not seen.
- A few tokens show absurd peaks (thousands of times p0 or more). These are real DexScreener
  prices from pools with almost no liquidity, not something anyone could have traded. Phase 4
  needs a liquidity filter before these are counted as wins.

## Data retention (added 2026-10-09, three tiers)

Once a token is 7 days old and its outcome is final, it is judged once
(`radar/lab/retention.py`, settings under `retention:`):

- **kept**: it graduated, reached a $30,000 market cap, or is watched. All snapshots stay.
- **early**: a strategy could plausibly have picked it (it appeared on Rising or reached a
  $10,000 market cap) but it is not in "kept". Its first 4 hours of snapshots stay at full
  detail, win or lose; later ones are deleted.
- **pruned**: it never reached any of those. All its snapshots are deleted.

Whatever the verdict, the token, its outcome, its follow-up checkpoint rows, its raw launch
event and its safety reports stay. Checkpoint rows also hold 5-minute and 1-hour volume, buys,
sells and price change, so a pruned token still has its signal inputs at 1m, 5m, 15m, 1h, ...

- Simulated on a copy of one day of data (39,350 tokens): 4,114 kept, 1,818 early, 33,374
  pruned; 50% of snapshot rows remain; the database went from 253 MB to 186 MB.
- Expected growth: about 2.1 GB a week with no retention, about 1.6 GB a week with this policy.
  The first week grows at the full rate, because nothing is 7 days old until 2026-10-15. The
  file never shrinks; freed space is reused.
- Graduated tokens keep everything, not only their first 4 hours, so exit rules can be tested
  over hours and days. Cutting them to 4 hours as well would save roughly 0.15 GB a week more.

## Theme tags and the Patterns page (added 2026-10-09)

- Every token gets one theme: political, celebrity, AI character, animal or other. The rules are
  keyword lists in `config/themes.yaml`, matched against the name and description; themes are
  tried in that order and the first match wins. Code: `radar/scoring/themes.py`. All tokens are
  re-tagged on the next start after the file changes.
- On the first 48,500 tokens: 79% other, 14% animal, 5% AI character, 2% celebrity, 1% political.
- Descriptions (added 2026-10-09): for tokens that appear on Rising, graduate or reach a $10,000
  market cap, the description is fetched once from the token's metadata link and the token is
  tagged again. Code: `radar/collectors/metadata.py`; settings under `descriptions:`.
  - About 2,900 tokens a day qualify, so about 2,900 requests a day (2 a minute), capped at
    20 a minute. The first start also works through about 3,000 tokens that already qualified.
  - ipfs.io, where 86% of the links point, no longer serves direct requests (HTTP 429). Files on
    IPFS are read through ipfs.filebase.io, then gateway.pinata.cloud; neither states a limit.
  - Links are written by anonymous creators. We only fetch IPFS files (through our own choice
    of gateway) and https links on two launch-tool hosts; about 3% of links are skipped. No
    redirects are followed and at most 64 KB is read.
  - In a 30-token trial, 16 had a description, 14 were empty and none changed theme. Expect
    descriptions to move few tags.
- http://localhost:8000/patterns compares themes, launch hour and launch day by graduation rate,
  peak and fate, with the token count on every row. It counts only pump.fun launches we saw being
  created whose outcome is complete. Until 5,000 are complete it shows a "not ready" note with
  dates: themes about 2026-10-16, launch hour about 2026-10-17, launch day about 2026-10-23.
  Settings under `patterns:`.
- "Complete" leaves out launches that are missing a figure (never priced, or a checkpoint was
  skipped while the collector was off). If those differ from the rest, the page is biased.

## "Why it's rising" (added 2026-10-09)

- Every token on Rising gets a one-line reason in the table and a full panel in the drawer:
  drivers with their numbers and how unusual each is among the tokens on Rising now, warning
  signs, a three-sentence explanation, base rates and takeaways. Code: `radar/lab/why.py`;
  thresholds under `why:` in `config/settings.yaml`. Free data only, all from our own tables.
- The explanation always ends by saying we cannot see X posts, Telegram calls or individual
  trades. Takeaways for a launch are only drawn from holder breadth, links being in place,
  graduation timing and paid promotion; warning signs are listed under "Not to copy".
- Base rates need history that starts with this version. The first time a token appears on
  Rising, its price and pattern (stage / two strongest driver families / warnings) are saved in
  `rising_entries`. Once its outcome is complete, it counts toward that pattern. A pattern is
  shown at 100 complete outcomes; before that the panel falls back to the wider group, or says
  "not enough history yet". Nothing can be complete before about 2026-10-17.
- "Median peak" in the base rates is measured from the price when the token first appeared on
  Rising, over the next 24 hours, not from p0.
- "How unusual" compares with the up to 50 tokens on Rising at that moment, not with history.
- Holder growth needs two safety reports, about 30 minutes apart, so it is usually blank when a
  token first appears. All 50 Rising tokens now get safety checks (was the top 25).

## Mint and freeze authority fix (2026-10-09)

- Cause: RugCheck sometimes gives the top-level `mintAuthority` / `freezeAuthority` as an object
  describing the authority's account instead of its address. We only recognised addresses, so a
  live authority was read as revoked.
- Checked against the chain with the public Solana RPC (`getMultipleAccounts`, 4 calls): for all
  43 tokens where our panel and RugCheck's risk list disagreed, the chain agreed with the risk
  list. 42 of them showed "Authorities revoked" wrongly and 1 was missing its freeze badge, out
  of 2,864 tokens with a safety report. They are mostly bridged assets and tokenized stocks
  (WBTC, WETH, cbBTC, NEAR, the xStocks), not pump.fun launches. 42 had been on Rising.
- Fixed in `radar/collectors/rugcheck.py`. Saved reports are corrected once on start from their
  stored risk lists (`backfill.fix_authority_flags`).
- From now on, if a report's fields and its risk list disagree in either direction, the token
  shows "Unverified" instead of "Authorities revoked", the "hide if authority enabled" filter
  hides it, and "Why it's rising" lists it as a warning.
- In a sample of 300 tokens where both parts of the report said revoked, 1 had a live authority
  on-chain. That gap is what the routine on-chain check below closes for Rising tokens.

## Routine on-chain authority check (added 2026-10-09)

- Every token on Rising has its mint and freeze authority read from the chain, through the free
  public Solana RPC (`getMultipleAccounts`, up to 100 tokens per call, no key). Each token is
  checked at most every 30 minutes; the time of the last check is saved, so this holds across
  restarts. Code: `radar/collectors/solana_rpc.py`; settings under `onchain:`.
- The chain wins. Where its reading differs from RugCheck's, the badges, the "hide if authority
  enabled" filter and "Why it's rising" all follow the chain, the token gets a "RugCheck was
  wrong" badge and a warning, and a line is written to the log. RugCheck's own reading stays in
  the database unchanged. A token the chain has confirmed never shows "Unverified".
- Cost: at most one call a minute, so at most 1,440 a day, and close to that in practice because
  a new token reaches Rising most minutes. The published limit is 40 calls per 10 seconds.
- Only Rising tokens are checked. A token that never reaches Rising still shows RugCheck's
  reading. Once checked, a token keeps its chain reading after it leaves Rising; "revoked" can
  never be undone on-chain, while "enabled" may by then be out of date.

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
