# Token Launch Research Lab — Solana token launch research & alert dashboard

## For the human (how to start)
1. Make an empty folder (e.g. `token-launch-research-lab`) and put this file in it as `CLAUDE.md`.
2. Open Claude Code in that folder.
3. Say: **"Read CLAUDE.md and build Phase 1. Walk me through anything I need to install."**
4. Leave the collector running as much as possible — the strategy work gets better the more data it has collected.

---

## Project goal
A local web dashboard (runs on the user's computer, opened at http://localhost:8000) that shows:
- **New launches** on Solana in real time, with safety flags
- **Rising / trending** coins, ranked by a momentum score we compute ourselves
- **Social feed** of posts from sources the user chooses, matched to tickers and contract addresses
- **Strategy lab**: a dataset of every token we observed and what happened to it, plus a backtester to test which early signals predicted good vs. bad outcomes

Order of evolution: research + backtesting → alerts the user acts on manually → (much later, only with explicit approval) automated trading.

## Ground rules (always follow)
- **No trading code, no wallet private keys, no transaction signing** until the user explicitly starts Phase 6. Do not ask for or store private keys.
- **Free data sources only** for now. Build the X/Twitter integration as a disabled, pluggable provider.
- **Respect rate limits.** Every external call goes through a shared rate limiter + cache per source. Back off on 429s. Never open multiple websocket connections to the same source.
- **Store everything raw.** Keep raw API responses (or the fields we use) with timestamps. Dead coins disappear from "trending" APIs, so the only way to avoid survivorship bias later is to record tokens *as we see them*, including all the ones that fail.
- **Verify endpoints before coding against them.** These APIs change. Before writing a client, hit each endpoint once (curl or a tiny script), look at the real response shape, and code to that. If an endpoint below is gone or changed, tell the user and propose the closest alternative.
- Secrets go in `.env` (git-ignored). Add `.env.example` with blank keys.
- Explain to the user, in plain language, any command they need to run.

## Working rules (set by the user on 2026-10-08, apply for the whole project)
1. Stay inside this project folder. Don't read, change or delete anything outside it. Temporary files go in the git-ignored `data/` folder.
2. Project-level installs (uv, Python packages for this project) are fine. Ask before installing anything system-wide (Homebrew, global npm/pip packages) or changing system settings.
3. Commit to git after every step that works, with a clear message, so any change can be rolled back.
4. No wallets, private keys, transaction signing or trading code. Stop and ask if anything heads that way.
5. Never put API keys or secrets in code. Use `.env` and keep it git-ignored.
6. At the end of each phase, give a plain-language summary: what was built, what was installed and where, how to run it, and anything that didn't work.

## Stack
- Python 3.11+, managed with `uv` (fall back to `venv` + `pip` if uv isn't available)
- FastAPI backend, `asyncio` collectors running in the same process as background tasks
- SQLite database (`data/radar.db`), WAL mode. Simple migrations via a `schema.sql` or SQLModel.
- Frontend: a single `static/index.html` + vanilla JS (no build step). Live updates via Server-Sent Events from FastAPI. Charts via TradingView `lightweight-charts` from a CDN.
- One command to run everything: `uv run python -m radar` (or equivalent), then open http://localhost:8000.

## Free data sources (Solana)
Confirm each one works before relying on it.

| Source | What we use it for | Access | Notes |
|---|---|---|---|
| **PumpPortal websocket** `wss://pumpportal.fun/api/data` | Real-time pump.fun token creations (`subscribeNewToken`), migrations/graduations (`subscribeMigration`), trades for watched tokens (`subscribeTokenTrade` with `keys: [mints]`) | Free, no key | Unofficial third party. Use ONE connection and add/remove subscriptions on it. Reconnect with backoff. |
| **DexScreener API** `https://api.dexscreener.com` | Pair data for any token: price, liquidity, FDV/mcap, volume & txns by m5/h1/h6/h24, price change, socials. Also latest token profiles and boosts. | Free, no key | ~60 req/min on profile/boost endpoints, ~300 req/min on pair/token endpoints. Batch lookups (`/tokens/v1/solana/{comma-separated addresses}`, up to 30). Boosts are **paid promotion** — show them, but flag as paid, never treat as organic interest. Docs: docs.dexscreener.com/api/reference |
| **GeckoTerminal API** `https://api.geckoterminal.com/api/v2` | `/networks/solana/new_pools`, `/networks/solana/trending_pools`, pool OHLCV (`/networks/solana/pools/{pool}/ohlcv/minute?aggregate=1`) for charts and backtests | Free, no key | Documented at 30 calls/min, but keyless traffic may get less — budget for ~10/min and cache aggressively. |
| **RugCheck API** `https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary` | Safety: mint/freeze authority, top-holder concentration, LP lock, risk list + score | Free, no key | Only call for tokens that pass basic filters; cache results; re-check occasionally. |
| **Telegram** (Phase 3, optional) via `telethon` | Read public channels the user lists, for the social feed | Free; user creates their own `api_id`/`api_hash` at my.telegram.org | Read-only. Session file is a secret — git-ignore it. |
| **X/Twitter** (disabled) | Social feed | Paid per post read | Build the provider interface + config only. Do not enable without the user's go-ahead. |
| **Solana RPC** (optional, Phase 2) e.g. a free Helius key | Holder counts, largest accounts, creator wallet history | Free tier with key | Only if RugCheck/DexScreener don't cover what we need. |

## Database (starting point — adjust to what the APIs actually return)
- `tokens` — mint (PK), name, symbol, creator, created_at, first_seen_at, source (pumpportal/gecko/dexscreener), metadata_uri, twitter, telegram, website, launchpad
- `snapshots` — mint, ts, price_usd, mcap, fdv, liquidity_usd, vol_m5, vol_h1, vol_h24, buys_m5, sells_m5, buys_h1, sells_h1, price_chg_m5, price_chg_h1, boosted (bool), source
- `trades` — mint, ts, side, sol_amount, token_amount, trader, mcap_sol (only for tokens on the watch list; prune old rows if size gets large)
- `migrations` — mint, ts, pool
- `safety` — mint, ts, mint_authority_enabled, freeze_authority_enabled, top10_pct, top_holder_pct, lp_locked_pct, risk_score, risks_json
- `social_posts` — id, source, author, text, url, ts, matched_mints_json
- `outcomes` — mint, p0_price, p0_ts, peak_multiple_24h, time_to_peak_min, multiple_1h, multiple_24h, multiple_72h, multiple_7d, max_drawdown_before_peak, rugged (bool), dead_at_7d (bool), label

## Phase 1 — Live dashboard (build first)
**Collectors**
- PumpPortal: insert every new token; record migrations.
- GeckoTerminal: poll Solana `new_pools` and `trending_pools` on a schedule that fits the rate limit.
- DexScreener: poll latest profiles and boosts; batch-refresh pair data for tokens on the dashboard.
- RugCheck: fetch summary for tokens that hit the "Rising" list or that the user opens.

**Dashboard panels**
1. **New launches** — live stream, newest first. Columns: age, name/symbol, mcap, liquidity, buys/sells, safety badges. Filters: min liquidity, max top-10 holder %, hide if mint or freeze authority enabled, graduated only.
2. **Rising** — ranked by our momentum score (below), with a "paid boost" badge where applicable.
3. **Token drawer** — click any token: chart (GeckoTerminal OHLCV), safety report, socials/links, links out to DexScreener and Solscan, and a "watch" button that subscribes to its trades.
4. **Social** — placeholder panel showing DexScreener profile links for now.

**Momentum score v0** (document it in the UI; we'll tune it with backtests later)
- volume acceleration = (vol_m5 × 12) / max(vol_h1, small floor) — above 1 means speeding up
- buy pressure = buys_m5 / (buys_m5 + sells_m5)
- transaction growth = txns in the last 5 min vs. the 5-min average over the last hour
- require: liquidity ≥ user-set minimum, age ≥ a few minutes
- score = weighted combination, weights in a config file

**Done when:** the user runs one command, opens localhost:8000, and sees new tokens appearing live, a ranked Rising list, and working token details. Collectors survive disconnects and rate-limit errors without crashing.

## Phase 2 — Outcome tracking (feeds the strategy lab)
- For every token that clears minimal filters (e.g. graduated, or liquidity above a threshold), schedule follow-up snapshots at roughly 1m, 5m, 15m, 1h, 4h, 24h, 72h, 7d after first seen. Prioritize within rate limits; log what was skipped.
- Compute `outcomes` and labels: **rug** (liquidity or price down ≥90% fast), **dead** (24h volume under ~$1k at day 7), **flat**, **runner** (peak 3–10× from p0), **moonshot** (≥10×). Thresholds in config.
- Add a simple "Data" page: how many tokens collected, label distribution, collector health.

## Phase 2b — Shadow bot (auto-buy / auto-sell, paper only — no wallet)
Purpose: answer "would a bot that gets in early and auto-sells have made money?" with realistic execution, before any real money is involved. Start as soon as Phase 1 collectors are stable.

**Two lanes, run side by side**
- **Lane A — early (bonding curve, first minutes):** for each new pump.fun token, subscribe to its trades for its first ~15 minutes (single PumpPortal connection; check their limits on how many mints one connection can watch and drop old subscriptions). Candidate signals: unique buyers and SOL inflow in the first 60s, deployer history, share of supply bought in the creation slot (bundle detection), concentration among the first ~20 buyers, whether early buyers are already selling.
- **Lane B — post-graduation (minutes to hours):** tokens that migrated. Candidate signals: momentum score, buyer growth, safety pass, time since migration, early buyers not exiting, liquidity stable.

**Deployer reputation (free, built from our own data):** creator wallet → number of past launches, how many graduated, how many rugged. Serial deployers with zero graduations are a strong negative signal.

**Execution model (be pessimistic)**
- Entry = first actual trade price at least `latency_s` after the signal. Run every strategy at several latencies (e.g. 1s, 3s, 10s, 30s) to show how speed-dependent the result is.
- Look up and apply current pump.fun / PumpSwap trading fees, plus a priority fee per transaction.
- Slippage from position size vs. bonding-curve or pool depth.
- A configurable share of entries fail (dropped transactions) and a share of exits land late.

**Exit engine** (rules in `strategies/*.yaml`): take-profit ladder (sell portions at set multiples), trailing stop, hard stop, time stop, and risk exits (dev/creator wallet sells, top early buyers exiting, liquidity drops sharply). Exit parameters get tuned on Phase 2 outcome data with walk-forward splits — never tuned and tested on the same period.

**Output:** `paper_trades` table; a "Shadow bot" dashboard page with equity curve per lane × latency, trade count, hit rate, median and mean return per trade, worst losing streak, max drawdown, and % of profit from the single best trade.

**Go / no-go gate for Phase 6** (show this status on the page): at least 3–4 weeks and ~200+ paper trades per lane; positive after fees at the latency the user's real setup can actually achieve; median trade not deeply negative; still profitable with the best 1–2 trades removed.

## Phase 3 — Social feed (free sources)
- Provider interface: `fetch_new_posts() -> list[Post]`.
- Telegram provider for channels in `config/sources.yaml`.
- Match posts to tokens by contract address (base58, 32–44 chars) and `$TICKER`; contract-address matches are high-confidence, ticker matches are low-confidence (tickers collide constantly).
- Record the time a token was first mentioned by each source — this becomes a feature for backtests ("did a mention from source X before the move matter?").
- X provider: written, disabled, with a cost estimate shown in the UI before enabling.

## Phase 4 — Strategy lab (backtesting)
- Feature extraction per token at decision time *t* using only data available at *t* (no look-ahead): age, mcap, liquidity, momentum components, buyer counts, safety fields, creator history, social mentions, migration status/timing.
- Backtester with realistic assumptions:
  - entry happens at the next snapshot after a signal, never at the signal price
  - slippage that scales with trade size vs. liquidity; platform + priority fees
  - exits by rule only (take-profit ladder, trailing stop, time stop) — never "sell at the peak"
- Evaluate with walk-forward splits (tune on earlier weeks, test on later weeks). Report trade count, hit rate, **median** and mean return per trade, profit factor, max drawdown, and the share of total profit coming from the single best trade (launch-token returns are extremely lopsided; a strategy carried by one outlier is not a strategy).
- Strategy rules live in `strategies/*.yaml` so they can be added without code changes. Starter rules will come from a separate research write-up.

## Phase 5 — Alerts + paper trading
- When a live token matches a strategy that passed backtesting, send an alert (desktop notification and/or a Telegram bot message to the user only).
- Paper-trading ledger: log hypothetical entries/exits with the same slippage/fee model, and compare live paper results to backtest expectations.

## Phase 6 — Automated trading (NOT in scope; requires explicit user approval)
Only after a lane passes the Phase 2b go/no-go gate and the user explicitly approves. Would require a dedicated burner wallet with small funds, hard per-trade and daily loss limits, a kill switch, and keys stored locally only. Live trading reuses the exact same signal and exit code as the shadow bot, so paper and live results stay comparable.

## Conventions
- Package layout: `radar/collectors/`, `radar/api/`, `radar/db/`, `radar/scoring/`, `radar/lab/`, `static/`, `config/`, `data/` (git-ignored).
- Config in `config/settings.yaml` (thresholds, weights, poll intervals).
- Structured logging to `data/logs/`. Each collector reports health (last success, error count) to the dashboard.
- Small, tested functions for scoring and labeling (pytest). Run tests before telling the user something is done.
