# Progress

Last updated: 2026-10-09, late evening (Mountain time). Phase 1 and Phase 2 are built.
Next in the plan is Phase 2b (shadow bot), which needs open decision 2 answered first.

## Start the dashboard

In a terminal:

```
cd ~/meme-radar
uv run python -m radar
```

Then open http://localhost:8000. Press Ctrl+C in that terminal to stop.
If the terminal says `uv: command not found`, use `~/.local/bin/uv run python -m radar`.

Pages: Dashboard (`/`), Data (`/data`), Patterns (`/patterns`), Shadow Bot (`/shadow`).

**A restart is pending** for the social rule of social_safety and full_stack (2026-10-13) and
the daily database backup (2026-10-14). Everything else below has been live since the restart
on 2026-10-13 at 21:11.

## What exists

| Area | Built | Where |
|---|---|---|
| Live dashboard: New launches, Rising, token drawer, Social placeholder | 2026-10-08 | `static/index.html`, `radar/collectors/` |
| Fixes from the first 20-hour run | 2026-10-09 | see below |
| Outcome tracking (follow-up checks, outcomes) and the Data page | 2026-10-09 | `radar/lab/followups.py`, `outcomes.py`, `static/data.html` |
| Peak and fate verdicts | 2026-10-09 | `radar/lab/outcomes.py` |
| Data retention | 2026-10-09 | `radar/lab/retention.py` |
| Theme tags | 2026-10-09 | `radar/scoring/themes.py`, `config/themes.yaml` |
| Patterns page | 2026-10-09 | `radar/lab/stats.py`, `static/patterns.html` |
| "Why it's rising" | 2026-10-09 | `radar/lab/why.py` |
| Description fetch | 2026-10-09 | `radar/collectors/metadata.py` |
| Mint and freeze authority fix | 2026-10-09 | `radar/collectors/rugcheck.py` |
| Routine on-chain authority check | 2026-10-09 | `radar/collectors/solana_rpc.py` |
| PumpPortal outage tracking | 2026-10-10 | `radar/lab/outages.py` |
| Stall watchdog and sleep detection | 2026-10-11 | `radar/collectors/pumpportal.py` |
| Shadow bot, Lane B (paper trades) | 2026-10-13 | `radar/lab/shadow.py`, `strategies/`, `static/shadow.html` |

157 tests pass (`uv run pytest`). All thresholds are in `config/settings.yaml`. Nothing here
trades, signs or holds keys: the shadow bot only writes rows to the database.

## When each page will have enough data

Times are Mountain time. The first token was recorded on 2026-10-08 at 18:15, and an outcome
needs the 7-day follow-up before it can be complete.

| Page or panel | What it is waiting for | Ready about |
|---|---|---|
| Data: first complete outcomes; "dead" and "alive" fates appear | first tokens reach day 7 | Oct 15, evening |
| Data: retention starts dropping snapshots | first tokens reach day 7 | Oct 15, evening |
| Patterns: by theme | 5,000 complete outcomes | Oct 16 |
| Patterns: by launch hour | a full day of launches a week old | Oct 17 |
| Patterns: by launch day | every weekday seen once | Oct 23 |
| Why it's rising: base rates, all Rising tokens together | 100 complete outcomes among tokens recorded on Rising since Oct 9, 22:21 | Oct 17 (estimate) |
| Why it's rising: base rates for one specific pattern | 100 complete outcomes with that pattern | the week of Oct 19 for common patterns (estimate); rare ones later |

The first readings will be thin: one day per launch hour, one of each weekday. The pages say so.
Any hours the collector is off become gaps ("skipped" checks) and push these dates back.

## Phase 1: live dashboard (2026-10-08)

- Collectors in one app: PumpPortal (new pump.fun tokens and graduations, one connection),
  DexScreener (batched prices, profiles, paid boosts), GeckoTerminal (new and trending pools,
  chart candles), RugCheck (safety reports).
- Shared rate limiter, cache and backoff per source (`radar/http.py`). SQLite at `data/radar.db`.
- Momentum score v0, documented on the page; weights in `config/settings.yaml`.

Fixes after the first 20-hour run (2026-10-09):

- **RugCheck retry loop.** A token RugCheck refused was re-requested every 10 seconds. A refusal
  is now saved and retried after 10 minutes, then 1 hour, then not at all; the dashboard shows
  "No RugCheck report". None of the addresses we sent were pool addresses.
- **Logging.** A repeating failure is logged once, then with a count every 10 minutes, then once
  on recovery (`radar/faillog.py`).
- **Holders column** on New launches and Rising, from RugCheck.
- **"Likely bot volume" badge**: at least 300 trades in the last hour, price within 1% over the
  last hour and last 5 minutes, market cap under $1M.
- **Rising is for meme coins only.** Tokens above $50M market cap and a fixed list of stablecoins
  and majors are left out. They are still recorded.
- **Environment rebuilt** after the folder moved out of Documents, so `uv run pytest` works.
- **Stray "radar 2/3/4" database files** were leftovers from Oct 8 test runs, created before the
  move. They are in `data/archive/` and none has appeared since.

## Phase 2: outcome tracking (2026-10-09)

- **Follow-up checks.** Every token is looked up again on DexScreener 1m, 5m, 15m, 1h, 4h, 24h,
  72h and 7d after we first saw it, including tokens the live refresh has dropped. Each check is
  saved in `followups` as done, no_data, skipped or backfilled, with price, market cap,
  liquidity, volume, buys, sells and price change at that moment.
- **Budget.** About 43,500 new tokens a day and about 1,050 graduations. Following all of them
  is about 8 requests a minute against our own cap of 120. When behind: graduated first, then
  tokens at $15,000 market cap or on Rising, trending or watched, then the rest.
- **Outcomes.** `outcomes` holds p0 (the first DexScreener price, usually about 90 seconds after
  launch), the peak multiple in the first 24 hours, time to peak, multiples at 1h, 24h, 72h and
  7d, drawdown before the peak, a rugged flag and a dead-at-day-7 flag. `final` means day 7 has
  passed; `complete` means final with nothing missing; `missing` lists what is not there.
- **Existing tokens** had their past checkpoints filled from snapshots where the snapshots
  supported it, and marked skipped where they did not.
- **Data page** (`/data`): tokens recorded, peak and fate breakdown, which outcome figures are
  filled, follow-up coverage per checkpoint, retention counts, collector health.

### Peak and fate

The single outcome label was replaced by two verdicts, so a token that pumped and then rugged
shows both:

- **peak**: moonshot (10x p0 or more in the first 24 hours), runner (3x to 10x), flat (under 3x,
  once the 24 hours are over).
- **fate**: rugged (price or pool liquidity fell 90% within an hour, or RugCheck flagged it),
  dead (24h volume under $1,000 at day 7, or no longer listed), alive.

Either is empty until it can be told, and both are provisional until day 7.

### Data retention

A token is judged once, when it is 7 days old and its outcome is final:

- **kept**: graduated, reached $30,000 market cap, or watched. All snapshots stay.
- **early**: appeared on Rising or reached $10,000 market cap, win or lose. The first 4 hours
  of snapshots stay at full detail.
- **pruned**: never reached any of those. All snapshots are deleted.

Whatever the verdict, the token, its outcome, its checkpoint rows, its raw launch event and its
safety reports stay. Deleted snapshots cannot be recovered. Expected growth is about 1.6 GB a
week with this policy (about 2.1 GB without it); the first week grows at the full rate, and the
file never shrinks because freed space is reused. The database was 350 MB on the evening of
Oct 9.

## Theme tags and the Patterns page (2026-10-09)

- Every token gets one theme: political, celebrity, AI character, animal or other. The rules are
  keyword lists in `config/themes.yaml`, matched against name and description, tried in that
  order with the first match winning. All tokens are re-tagged on start when the file changes.
- On the first 48,500 tokens: 79% other, 14% animal, 5% AI character, 2% celebrity, 1% political.
- **Patterns page** (`/patterns`) compares themes, launch hour and launch day by graduation
  rate, peak and fate, with the token count on every row. It counts only pump.fun launches we
  saw being created whose outcome is complete. Rows under 300 tokens are greyed out, and the
  page shows a "not ready" note with dates until 5,000 outcomes are complete.

### Description fetch

- For tokens that appear on Rising, graduate or reach $10,000 market cap, the description is
  fetched once from the token's metadata link and the token is tagged again.
- About 2,900 requests a day, capped at 20 a minute. About 3,000 tokens that already qualified
  are being worked through; 297 had been processed by 22:38 on Oct 9.
- ipfs.io, where 86% of the links point, no longer serves direct requests. Files on IPFS are
  read through `ipfs.filebase.io`, then `gateway.pinata.cloud`. Neither states a rate limit.
- The links are written by anonymous creators. We only fetch IPFS files (through our own choice
  of gateway) and https links on two launch-tool hosts; about 3% of links are skipped. No
  redirects are followed and at most 64 KB is read.
- So far about 6 in 10 fetched files have a description, and few of those change a tag.

## "Why it's rising" (2026-10-09)

- Every token on Rising gets a one-line reason in the table and a full panel in the drawer:
  drivers with their numbers and how unusual each is among the tokens on Rising at that moment,
  warning signs, a three-sentence explanation, base rates and takeaways.
- The explanation always ends by saying we cannot see X posts, Telegram calls or individual
  trades. Launch takeaways only draw on holder breadth, links being in place, graduation timing
  and paid promotion; warning signs appear under "Not to copy", never as tactics.
- **Base rates** need history that began on Oct 9 at 22:21. The first time a token appears on
  Rising, its price and pattern (stage / two strongest driver families / warnings) are saved in
  `rising_entries`. A pattern is shown at 100 complete outcomes; before that the panel falls
  back to a wider group or says "not enough history yet". "Median peak" is measured from the
  price when the token appeared on Rising, over the next 24 hours.
- All 50 Rising tokens get RugCheck safety checks (was the top 25).

## Mint and freeze authority (2026-10-09)

- **The bug.** RugCheck sometimes gives a live authority as a block of account details instead
  of an address. We only recognised addresses, so a live authority was read as revoked.
- **Checked against the chain.** For all 43 tokens where our panel and RugCheck's risk list
  disagreed, the chain agreed with the risk list. 42 had shown "Authorities revoked" wrongly and
  1 was missing its freeze badge, out of 2,864 tokens with a safety report. Most are bridged
  assets and tokenized stocks. The saved reports were corrected.
- **"Unverified".** If a report's fields and its risk list disagree, the token shows
  "Unverified" instead of "Authorities revoked", and the authority filter hides it.
- **Routine on-chain check.** Every token on Rising has its mint
  and freeze authority read from the chain through the free public Solana RPC, batched, at most
  every 30 minutes per token. The chain wins: where it differs from RugCheck, the badges, the
  filter and "Why it's rising" follow the chain, and the token gets a "RugCheck was wrong"
  badge, a warning and a log line. At most 1,440 calls a day (one a minute).

## PumpPortal outages (2026-10-10)

- The wait between reconnect attempts now tops out at 30 seconds (was 60) and the loop never
  gives up.
- **What counts as an outage:** the time between two launches we received, if the connection
  dropped in between, the app was restarted in between, or the silence lasted 60 seconds or more
  (launches normally arrive every second or two). That one rule covers dropped connections,
  stalled streams, the computer sleeping and the app being off. Code: `radar/lab/outages.py`.
- Each outage is logged with its length when it ends, saved in the `outages` table, and listed
  under "Missed periods" on the Data page with an estimate of the launches missed.
- **Tokens launched during an outage are missing data.** If we later meet such a token another
  way, its outcome lists "launch" as missing and is never complete, so it is left out of Patterns
  and of the "Why it's rising" base rates. The same applies to a pump.fun token we only know
  from its graduation. The flag is applied at each token's next follow-up check.
- **History so far** (found from gaps in the recorded launches): 8 outages totalling 3h 15m, 8%
  of the 40.5 hours collected, about 5,100 launches missed. Two were the app being stopped
  (39 minutes on Oct 8, 64 minutes on Oct 9). The other six, mid-morning on Oct 9 and Oct 10,
  happened while the app was running: the Mac went to sleep.
- **Cause of the morning outages, confirmed by the user on 2026-10-10:** the Mac was unplugged
  to charge a phone, and on battery it slept. It will be kept plugged in from now on. No
  reconnect setting can help while the computer is asleep, because the whole app is frozen.
  If "Missed periods" shows new outages with the reason "no launches arrived (stalled stream,
  or the computer was asleep)", check the power cable first.

### Stall watchdog and the afternoon of 2026-10-11

- **Watchdog (needs the restart):** connected but no token creations for 60 seconds
  (`stall_after_s`) closes the connection and reconnects at once, with no backoff wait. The old
  rule only reconnected after 120 seconds with no messages of any kind.
- **Sleep is now named.** The app compares the wall clock with a clock that stops during sleep.
  If they drift 30 seconds or more (`sleep_min_s`), the outage reason is "the computer was
  asleep (about N)" instead of a connection error.
- **2026-10-11:** 13 outages, 3h 1m lost, all but one minute of it between 15:11 and 18:40,
  about 3,000 launches. DexScreener went silent in exactly the same periods (15-minute blocks
  with a few seconds of activity between them), and neither the 20-second ping check nor the
  120-second silence timer fired. So the whole app was frozen: the Mac was asleep and waking
  briefly about every 15 minutes. `caffeinate -i` stops idle sleep only; closing the lid still
  sleeps the Mac. The watchdog cannot help with this. To confirm, run
  `pmset -g log | grep -E "Sleep|Wake" | tail -40`.
- **One copy only:** checked 2026-10-11 18:45, one collector process, one PumpPortal connection.
- **Backup launch sources (researched, not built):** see open decision 10.

## Phase 2b: shadow bot, Lane B (2026-10-13)

Paper trading forward in time on tokens we saw graduate. No wallet, no keys, no orders.

- **Strategies** are files in `strategies/`: `every_graduation.yaml` (first price at least 2
  minutes after graduation), `safety_only.yaml` (mint and freeze revoked on-chain, top-10 under
  30%, no bot-volume flag, within an hour of graduation), `momentum_safety.yaml` (the same plus
  on the Rising list, within 6 hours).
- **Exits, the same for all three:** sell half at 2x, then a 30% trailing stop on the rest; hard
  stop at -40%; time stop at 24 hours. 0.5 SOL per trade, one trade per strategy per token.
  The trailing stop only starts after the half is sold ("on the rest"); before that the hard
  stop is the only stop. Change `trailing_after_take_profit` to make it apply from entry.
- **Confirmed by the user on 2026-10-13:** trailing stop only after the half sells at 2x; safety
  must pass within 1 hour of graduation; no retry on dropped entries; only tokens graduating
  after the restart are traded; -10% median in the gate. The 6-hour window for reaching Rising
  was not mentioned and stays as built.
- **Pessimism** (`shadow` in `config/settings.yaml`): entry and exit at the next price after
  the signal or stop; PumpSwap's published fee table (1.25% for small tokens down to 0.30%,
  pump.fun/docs/fees dated 2026-10-08) each way; 0.001 SOL priority fee per transaction;
  slippage = twice the pool price impact plus 1% each way; 10% of entries dropped; 10% of exits
  one price late; a position whose price vanishes is written off at zero.
- **Refresh:** tokens with a paper position are re-priced every 20 seconds for the first hour
  and every 60 seconds after, ahead of everything except watched tokens.
- **On-chain check extended:** every token we see graduate is now checked once on-chain (it
  used to be Rising tokens only), because the safety rules need it.
- **Page:** `/shadow` shows per strategy the trade count, hit rate, median and mean return,
  profit, profit without the best trade, max drawdown, the equity curve, how trades ended, the
  latest trades and the go/no-go gate (21 days, 200 closed trades, profit after fees, median no
  worse than -10%, still profitable without the best 2 trades).
- **Sleep ruins this.** While the Mac sleeps no price is fetched and no stop is applied. The
  outage log shows the Mac asleep for about 18 of the 24 hours before the build.
- **Affected by outage (2026-10-13, live since the 21:11 restart):** a trade that went 5 minutes or more
  without a price while open (`shadow.outage_gap_s`) is marked "affected by outage". Those
  trades have their own table on the page and are left out of the headline numbers, the equity
  curve and the gate. Nothing is deleted: the flag comes from `paper_trades.max_gap_s`, and the
  page also shows all trades together. Caveat: long-running trades meet outages more often than
  quick stop-outs, so leaving them out under-counts long holds, which include most big winners.
- **Rules version 2 (2026-10-13, live since the 21:11 restart).** After the first review:
  - *Slippage limit:* no entry when the pool holds under $5,000 or the modelled price impact is
    over 10%; recorded as failed, reason "slippage limit" (`shadow.entry_limits`).
  - *Real graduations only.* PumpPortal sends "migrate" whenever anyone opens a PumpSwap pool
    for any token. That is how USDC, WBTC, IBM, tOpenAI, ROOT, SAPCAT and PSOL were "graduating",
    some of them many times; their prices then came from their big Orca, Raydium or Meteora
    pools. A token is now traded only if we watched it launch on pump.fun's bonding curve, this
    is its first migration, and it is not on the Rising exclusion list; and the entry price must
    come from the PumpSwap pool quoted in SOL (else failed, "not the launchpad pool"). This
    also drops real graduations whose launch we missed (38 of the first 88 entries), mostly
    because the Mac was asleep at launch.
  - *The $1 to $100 pools* were real pump.fun tokens in Mayhem Mode: all 9 whose launch we saw
    had `is_mayhem_mode` set, and they reached PumpSwap at a market cap of a few dollars. Why
    Mayhem Mode tokens migrate like that was not researched. The slippage limit stops them;
    Mayhem Mode itself is not excluded, and is saved with each trade.
  - *Rules versions, per strategy and automatic (2026-10-13):* each strategy has its own
    version, started by itself when its file in `strategies/`, a shared rule setting (fees,
    slippage, entry limits, fail and late shares, entry wait, price misses, Rising freshness)
    or the engine's own rules (`ENGINE_RULES` in `radar/lab/shadow.py`) changes. Adding or
    changing one strategy leaves the others' go/no-go clocks alone. Versions are in the
    `strategy_versions` table; each trade carries its strategy's `rules_version`. The first 186
    trades are version 1 of each strategy; version 2 began at the 21:11 restart on 2026-10-13. Renaming a
    strategy's `name` or `description` starts nothing; renaming its file makes it a new strategy.
  - *Entry-time values:* `signal_json` and `entry_json` on each trade hold exactly what the
    engine saw (safety, on-chain authority, bot flag, Rising, score, pool, volumes). The export
    uses them and marks each row "saved" or "rebuilt".
- **Export:** `uv run python -m radar.lab.export` writes `data/exports/paper_trades_<time>.csv`,
  `open_positions_<time>.csv` and a README. Read-only.
- **Not built:** Lane A (needs trade data), several latencies (we only have one: the next
  price), risk exits on creator or early-buyer selling.

### Second batch of strategies (2026-10-13, live since the 21:11 restart)

The user's restart at 21:11 came after every engine file was saved, so all ten strategies
started then: version 2 of the first three, version 1 of the new ones. All the new ones use
the slippage limit, real graduations only, the Safety only rules and 0.5 SOL.

- `social_safety` (4): at least two of Telegram, X link, website (rule `min_social_links: 2`;
  was "Telegram and (X or website)" until the evening of 2026-10-13, which had made no trades). `clean_deployer` (5): the
  creator launched 3 or fewer tokens in the last 7 days (this one included) or has an earlier
  graduation, and never 10+ launches with no graduation. `after_boost` (6): from 10 to 30
  minutes after graduation, price at least the first PumpSwap price (within 3 minutes of
  graduating), 5-minute buys at least sells, liquidity within 20% of its peak since graduation.
  `buyers_not_leaving` (7): from 2 to 5 minutes, 5-minute buys at least 1.5x sells and the
  5-minute price change above zero. `full_stack` (8): 4 + 5 + 7 in 7's window.
- Exit experiments `early_derisk` (E1) and `no_progress_timeout` (E2) use `entry: {same_as:
  safety_only}`: the same entry rules, the same moments and the same simulated dropped entries,
  so they hold exactly what Safety only holds. Changing `safety_only.yaml` restarts them too.
- New strategy-file parts: `rules_checked_from_minutes` (rules are not looked at before this),
  `same_as`, `exits.no_progress`, and the rules `has_telegram`, `has_x_or_website`,
  `clean_deployer`, `min_buys_per_sell_m5`, `price_up_m5`, `min_price_vs_graduation`,
  `max_liquidity_drop_pct`. Parts a strategy does not use do not enter its fingerprint.
- **Deployer table:** the `deployers` view (creator wallet, launches, graduations, first and
  last launch), built from PumpPortal create events, so it is always current. History starts
  2026-10-08 and misses launches made while the app was off or the Mac asleep. On the existing
  data 2,349 of 2,738 graduates would pass the deployer rule; 277 came from 10+ launch wallets
  with no earlier graduation.
- **Social links:** only DexScreener's links were stored, and only 27 of 2,738 graduates had a
  Telegram link. The metadata reader now also keeps the creator's twitter, telegram and website
  links (right kind of host only, stored as text, never fetched), and re-reads a token's file
  when it graduates. Even so, 0 of 20 recent graduates' metadata files had a Telegram link, so
  strategies 4 and 8 will trade very rarely.
- **Safety timing:** strategies 7 and 8 have three minutes to see the safety rules pass. When
  the Mac is awake the on-chain check usually lands within a minute of graduation, but about
  half of Safety only's version 1 signals came after 5 minutes (sleep inflates that figure).
- **Gate:** two more checks. Beat `safety_only` (`shadow.gate.benchmark`) on mean return per
  trade over the time both ran their current rules; and a profit in each half of the strategy's
  own test period. Safety only is the benchmark and skips the first.
- **Saved with each trade:** social links, creator record, graduation price and liquidity peak,
  in `signal_json` and `entry_json`; exported as `has_telegram` ... `liquidity_peak_since_graduation_usd`.
- **Page:** a side-by-side table against Safety only; one small equity chart per strategy with
  Safety only behind it on the same scale (three colours cannot tell ten lines apart); a "No
  progress" column; tables now scroll inside their box on a phone.
- **DexScreener load:** a token is refreshed once however many strategies hold it, and every
  strategy trades only graduates that Every graduation already watches, so the extra load is
  small. Graduations ran at 25 an hour (median), 50 at most. Worst case, every graduate held
  24 hours: about 1,300 lookups a minute, about 44 requests, inside our 120. With the average
  hold of about 23 minutes it is about 1 to 3 requests a minute.

## One rule for "graduated" (2026-10-13, live since the 21:11 restart)

`radar/lab/graduation.py`. A graduation is a token we watched launch on pump.fun's bonding
curve that then moved to PumpSwap: either we got its first "migrate" message (that sets
`migrated_at`, and later messages for the same token change nothing), or we got no message
(asleep) but DexScreener now prices it from a PumpSwap pool. Having a pool is not enough.

- Applied to: the dashboard "Graduated" badge and filter, follow-up priority, the Patterns
  graduation rate (now pump.fun launches only), the Rising stage in "Why it's rising" (a new
  stage, "trading in a liquidity pool, not a pump.fun graduation we saw"), retention tiers, the
  on-chain check queue, the refresh schedule and the shadow bot.
- At the first start, every recorded "migrate" message is rechecked once. Dry run on a copy:
  2,687 real, 881 cleared, 20 graduation times put back to the first message, 2,043 Rising
  entries relabelled from "graduated" to "pool". The messages stay in `migrations`.
- Effect, measured on a copy on 2026-10-13: tokens shown as graduated 12,675 -> 2,864 (about
  9,100 of the difference are GeckoTerminal and DexScreener tokens that simply have a pool).
  Among the 107,143 pump.fun launches we saw: 2,905 -> 2,864 (2.71% -> 2.67%). No outcome is
  final yet, so no Patterns or outcome figure that has been shown changes.
- 177 graduations are "untimed" (no message, PumpSwap pool seen). They count everywhere except
  the shadow bot, which needs the moment.
- Retention: a token with a pool but no graduation is no longer kept in full for that reason
  alone; it needs the $30K peak (or $10K or Rising for its first 4 hours), like any other.

## Daily database backup (2026-10-14, needs the restart)

`radar/backup.py`, settings under `backup:`. Once a day the app copies the database with
SQLite's own backup command (a consistent moment, while the collectors keep writing), checks
the copy, and gzips it into `~/meme-radar-backups/` (the user asked for this folder outside the
project). Kept: the newest 3 in `daily/`, and the first backup of each week in `weekly/`, newest
4 (a hard link while it is also a daily, so no extra space). Old files are removed only after a
new backup succeeded. It is skipped, with an error on the Data page, if the disk would be left
with under 2 GB free. A Mac that slept makes its backup on waking (it is due 24 hours after the
newest one).

- First backup, made by hand on 2026-10-14 at 13:27: database 1,244 MB -> 557 MB in 75 seconds,
  restored to a scratch copy and checked (169,378 tokens, 1,716 paper trades).
- Disk: at most 6 distinct files (3 daily + 4 weekly, one shared), about 3.3 GB at today's size,
  growing with the database, plus a temporary uncompressed copy (about 1.2 GB) for a minute
  during each backup. The Mac had 13 GB free on 2026-10-14.
- `uv run python -m radar.backup list` shows them, `... now` makes one, and
  `... restore FILE` puts one back (refuses while the app is running; the replaced database is
  moved to `data/archive/radar-before-restore-<time>/`).

## Outside requests, per day

All free, no keys.

| Source | Roughly | Our cap |
|---|---|---|
| DexScreener prices (live refresh and follow-ups) | up to 60 a minute | 120 a minute |
| DexScreener prices for paper positions (part of the line above) | about 10 to 20 a minute, about 45 at worst (1,000 positions open) | shares the 120 a minute |
| DexScreener profiles and boosts | 3 a minute | 20 a minute |
| GeckoTerminal | about 2 a minute plus charts on demand | 8 a minute |
| RugCheck | a few a minute | 12 a minute |
| PumpPortal | one websocket connection | one |
| Metadata links (descriptions and social links, plus one re-read per graduation) | about 3,500 a day | 20 a minute |
| Solana public RPC | up to 1,440 a day | 6 a minute |

## Open decisions

1. **Social rule, decided 2026-10-13.** Of 2,925 pump.fun graduates since Oct 8, the links we
   had stored (DexScreener only) gave 13.4% with two or more of Telegram, X and website, and
   16.9% with X or a website. A sample of 200, adding each token's metadata file (176
   readable): 59.7% two or more, 68.8% X or website, 4.5% Telegram. Two of three is above the
   10% bar, so strategies 4 and 8 use it; in practice it mostly means X and a website.

2. **PumpPortal trade data (blocks Phase 2b, Lane A).** PumpPortal refuses per-token trade
   subscriptions without an API key funded with about 0.02 SOL, so there is no free
   trade-by-trade stream and the `trades` table is empty. Options:
   - a funded PumpPortal key (tied to a wallet they hold, which conflicts with the no-wallet rule);
   - decode trades ourselves from a free Solana RPC key such as Helius (more work, stays free);
   - limit Phase 2b to the post-graduation lane (Lane B), which works with the data we have.

3. **Graduated tokens: keep all snapshots, or only the first 4 hours?** They currently keep
   everything so exit rules can be tested over hours and days. Cutting them to 4 hours would
   save roughly 0.15 GB a week. Decide before Oct 15, when the first deletions happen.

4. **What "complete outcomes only" leaves out.** Patterns and base rates skip tokens missing any
   figure: about 13% of tokens never had a price, and checks are skipped whenever the collector
   is off. If those tokens differ from the rest, the comparisons are skewed. The alternative is
   to count never-priced tokens as flat and dead.

5. **Absurd peaks.** About 20 tokens show a peak of 100x or more, a few of them thousands of
   times p0. These are real DexScreener prices from pools with almost no liquidity. Phase 4
   needs a liquidity filter before they count as wins; they also sit in the moonshot counts now.

6. **Momentum score v0.** It favours very young tokens with one-sided buying. Leave it until
   outcome data exists to tune it (about Oct 16 onward), or adjust now.

7. **Disk beyond retention.** At about 1.6 GB a week the database passes 20 GB in three months.
   The next largest items are checkpoint rows, token rows, raw launch events and safety reports.
   No plan yet for trimming or archiving those.

8. **Delete the archived stray files?** `data/archive/` holds 12.6 MB of leftovers that are safe
   to delete.

9. **Extend the on-chain check?** It covers Rising and fresh graduations only. Tokens that never reach Rising still
   show RugCheck's reading, which was wrong for about 1.5% of checked tokens.

Defaults chosen during the build, all changeable in `config/settings.yaml`: Rising cap of $50M;
bot-volume flag at 300 trades and 1%; retention thresholds of $30,000 and $10,000; 5,000 complete
outcomes and 300 per row on Patterns; 100 complete outcomes for a base rate; description fetch at
$10,000.

10. **Backup source for launches when PumpPortal stalls?** Options found 2026-10-11:
    the public Solana RPC websocket (`logsSubscribe` on the pump.fun program; free, no key,
    tested working, but about 0.6 MB a second, so only as a standby); Helius free key (same
    feed, 1M credits a month, which is roughly one day of that stream, so standby only);
    Moralis "new pump.fun tokens" list (free key, could fill gaps after an outage, quota not
    confirmed); Bitquery (free tier too small); pumpdev.io (claimed free, did not answer our
    test). Nothing built yet.
11. **Keep the Mac awake.** `caffeinate -i` is not preventing sleep (most likely the lid is
    closed). Options: leave the lid open, or change the system sleep setting (needs your
    say-so, it is a system setting).

## Known limits

- p0 is about 90 seconds after launch, not the launch price, because DexScreener does not list a
  new pump.fun token sooner.
- The peak is only as fine as our snapshots. A spike between two looks is not seen.
- For a pruned token, later analysis can only use the checkpoint moments, not the minutes between.
- "How unusual" in "Why it's rising" compares with the tokens on Rising at that moment, not
  with history. Holder growth needs two safety reports about 30 minutes apart.
- Theme tags are keyword matches and make mistakes ("Trumpet" is tagged political). The Patterns
  page shows recent examples per theme.
- The two IPFS gateways and the public Solana RPC are free services with no guarantee. If one
  stops, descriptions or on-chain checks stop and the collector health chips show it.
- Left over from Phase 1: "LP locked %" is our own calculation and was close to, not identical
  with, RugCheck's figure on the one token compared; the light colour theme was only checked on
  an empty page; the chart colour accessibility check was not run.
- Not built: the X/Twitter provider (disabled by design), Telegram and the social feed (Phase 3),
  the shadow bot (Phase 2b), backtesting (Phase 4), alerts (Phase 5).

## Environment notes

- uv is installed at `~/.local/bin/uv`. Project libraries are in `.venv/`, rebuilt on 2026-10-09
  after the folder moved from `~/Documents/meme-radar` to `~/meme-radar`.
- Git name and email are set for this project only (in `.git/config`).
- The working rules for this project are in `CLAUDE.md` under "Working rules".
- Changes to the code or `config/` only take effect when the collector is restarted. Pages in
  `static/` are read from disk, so a new page can be ahead of a collector that has not restarted.
