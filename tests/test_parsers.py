"""Parser tests. Fixtures are trimmed copies of real responses captured on 2026-10-08."""
import json

from radar.collectors.dexscreener import best_pairs, pair_token_info, parse_links, parse_pair, sol_price_from_pairs
from radar.collectors.geckoterminal import parse_ohlcv, parse_pools
from radar.collectors.rugcheck import parse_report
from radar.view import clean_url, is_graduated, is_mint, view_row

FRANK = "HbPDWSqu8hpVMX6gMjwMDGe5rVgicWo3Qh3Jaojypump"
FIX = "HixHQHo65Y4U979X2xzMtKp5FhXp8LuXeye5zAMbZdQN"
SOL = "So11111111111111111111111111111111111111112"
POOL = "745ATaSSWxvry5qt1XVv9eXuG884hDvNJfvA2o82dNBi"

DEX_GRADUATED = {
    "chainId": "solana", "dexId": "pumpswap", "pairAddress": POOL,
    "baseToken": {"address": FRANK, "name": "Frank Ashford", "symbol": "Frank"},
    "quoteToken": {"address": SOL, "name": "Wrapped SOL", "symbol": "SOL"},
    "priceNative": "0.000005", "priceUsd": "0.00055",
    "txns": {"m5": {"buys": 75, "sells": 44}, "h1": {"buys": 1795, "sells": 1082},
             "h6": {"buys": 9693, "sells": 5605}, "h24": {"buys": 35170, "sells": 21939}},
    "volume": {"h24": 1615173.02, "h6": 439903.91, "h1": 70188.03, "m5": 6292.08},
    "priceChange": {"m5": -6.92, "h1": -6.18, "h6": -40.52, "h24": -5.91},
    "liquidity": {"usd": 75380.78, "base": 69746549, "quote": 333.1546},
    "fdv": 527397, "marketCap": 527397, "pairCreatedAt": 1791152156000,
    "boosts": {"active": 30},
    "info": {"imageUrl": "https://cdn.dexscreener.com/cms/images/abc",
             "websites": [{"url": "https://frank.example", "label": "Website"}],
             "socials": [{"url": "https://x.com/frank", "type": "twitter"},
                         {"url": "https://t.me/frank", "type": "telegram"}]},
}
# Still on the bonding curve: no liquidity, boosts or info keys at all.
DEX_CURVE = {
    "chainId": "solana", "dexId": "pumpfun", "pairAddress": "PtCqvP74jDAYukgca1GMBgh52gzFoSuuUXG3fAzJVqu",
    "baseToken": {"address": FIX, "name": "FIX", "symbol": "FIX"},
    "quoteToken": {"address": SOL, "name": "Wrapped SOL", "symbol": "SOL"},
    "priceNative": "0.00000003", "priceUsd": "0.0000033",
    "txns": {"m5": {"buys": 1, "sells": 0}, "h1": {"buys": 1, "sells": 0}},
    "volume": {"h24": 22.85, "h1": 22.85, "m5": 22.85},
    "priceChange": {}, "fdv": 3282.62, "marketCap": 3282.62, "pairCreatedAt": 1791502924000,
}


def test_parse_pair_graduated():
    snap = parse_pair(DEX_GRADUATED)
    assert snap["price_usd"] == 0.00055
    assert snap["mcap"] == 527397 and snap["liquidity_usd"] == 75380.78
    assert (snap["buys_m5"], snap["sells_m5"], snap["buys_h1"], snap["sells_h1"]) == (75, 44, 1795, 1082)
    assert (snap["vol_m5"], snap["vol_h1"], snap["vol_h24"]) == (6292.08, 70188.03, 1615173.02)
    assert snap["price_chg_m5"] == -6.92
    assert snap["boosted"] == 1 and snap["boosts_active"] == 30
    assert snap["dex_id"] == "pumpswap" and snap["pair_address"] == POOL


def test_parse_pair_on_bonding_curve_has_no_liquidity_or_boost():
    snap = parse_pair(DEX_CURVE)
    assert snap["liquidity_usd"] is None
    assert snap["boosted"] == 0 and snap["boosts_active"] == 0
    assert snap["price_chg_m5"] is None and snap["buys_h24"] is None


def test_best_pairs_prefers_most_liquid_pair_and_ignores_unwanted_tokens():
    thin = {**DEX_GRADUATED, "pairAddress": "thin", "liquidity": {"usd": 10.0}}
    best = best_pairs([thin, DEX_GRADUATED, DEX_CURVE, "junk"], {FRANK})
    assert set(best) == {FRANK}
    assert best[FRANK]["pairAddress"] == POOL


def test_pair_token_info_and_links():
    info = pair_token_info(DEX_GRADUATED)
    assert info["name"] == "Frank Ashford" and info["symbol"] == "Frank"
    assert info["created_at"] == 1791152156.0
    assert info["twitter"] == "https://x.com/frank"
    assert info["telegram"] == "https://t.me/frank"
    assert info["website"] == "https://frank.example"
    assert pair_token_info(DEX_CURVE)["twitter"] is None


def test_links_drop_anything_that_is_not_a_web_link():
    links = parse_links([
        {"type": "twitter", "url": "javascript:alert(1)"},
        {"label": "Website", "url": "https://ok.example"},
        {"type": "tiktok", "url": "https://tiktok.com/@x"},
        "junk",
    ])
    assert links == {"twitter": None, "telegram": None, "website": "https://ok.example"}
    assert clean_url("data:text/html,x") is None
    assert clean_url(" https://a.example ") == "https://a.example"


def test_sol_price_from_pairs():
    assert round(sol_price_from_pairs([DEX_GRADUATED]), 2) == 110.0


GECKO = {
    "data": [
        {"id": f"solana_{POOL}", "type": "pool",
         "attributes": {
             "base_token_price_usd": "0.000561739519228566", "quote_token_price_usd": "110.1737",
             "address": POOL, "name": "Frank / SOL", "pool_created_at": "2026-10-04T22:15:56Z",
             "fdv_usd": "527580.82", "market_cap_usd": None,
             "price_change_percentage": {"m5": "-6.928", "h1": "-6.18", "h24": "-5.916"},
             "transactions": {"m5": {"buys": 75, "sells": 44, "buyers": 71, "sellers": 44},
                              "h1": {"buys": 1795, "sells": 1082, "buyers": 1262, "sellers": 722},
                              "h24": {"buys": 35170, "sells": 21939, "buyers": 3962, "sellers": 3759}},
             "volume_usd": {"m5": "6292.08", "h1": "70188.03", "h6": "439903.91", "h24": "1615173.02"},
             "reserve_in_usd": "75169.1781"},
         "relationships": {"base_token": {"data": {"id": f"solana_{FRANK}", "type": "token"}},
                           "quote_token": {"data": {"id": f"solana_{SOL}", "type": "token"}},
                           "dex": {"data": {"id": "pumpswap", "type": "dex"}}}},
        # A pool whose base token is SOL itself must be skipped.
        {"id": "solana_x", "type": "pool", "attributes": {"address": POOL, "name": "SOL / USDC"},
         "relationships": {"base_token": {"data": {"id": f"solana_{SOL}", "type": "token"}},
                           "quote_token": {"data": {"id": "solana_EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "type": "token"}},
                           "dex": {"data": {"id": "orca", "type": "dex"}}}},
    ],
    "included": [
        {"id": f"solana_{FRANK}", "type": "token",
         "attributes": {"address": FRANK, "name": "Frank Ashford", "symbol": "Frank", "decimals": 6,
                        "image_url": "https://assets.geckoterminal.com/abc"}},
        {"id": "pumpswap", "type": "dex", "attributes": {"name": "PumpSwap"}},
    ],
}


def test_parse_pools():
    pools = parse_pools(GECKO)
    assert [p["mint"] for p in pools] == [FRANK]
    pool = pools[0]
    assert pool["token"]["name"] == "Frank Ashford" and pool["token"]["symbol"] == "Frank"
    assert pool["token"]["pool_address"] == POOL and pool["token"]["dex_id"] == "pumpswap"
    assert pool["token"]["created_at"] == 1791152156.0
    snap = pool["snap"]
    assert snap["mcap"] == 527580.82  # market_cap_usd is null, so fall back to FDV
    assert snap["liquidity_usd"] == 75169.1781
    assert (snap["buys_m5"], snap["sells_h1"], snap["vol_h1"]) == (75, 1082, 70188.03)
    assert pool["sol_price_usd"] == 110.1737


def test_parse_pools_tolerates_junk():
    assert parse_pools(None) == []
    assert parse_pools({"data": [{"attributes": {}, "relationships": {}}]}) == []


def test_parse_ohlcv_sorts_oldest_first_and_merges_duplicate_timestamps():
    payload = {"data": {"attributes": {"ohlcv_list": [
        [1791503400, 4.0, 4.5, 3.9, 4.1, 9.4],
        [1791503400, 4.1, 4.2, 3.5, 4.1, 9.4],
        [1791503280, 3.0, 4.0, 2.9, 4.0, 5.0],
        [1791503220, None, 1, 1, 1, 1],
    ]}}}
    candles = parse_ohlcv(payload)
    assert [c["time"] for c in candles] == [1791503280, 1791503400]
    assert candles[1] == {"time": 1791503400, "open": 4.0, "high": 4.5, "low": 3.5, "close": 4.1, "volume": 9.4}
    assert parse_ohlcv({}) == []


RUGCHECK = {
    "mint": FRANK, "creator": "5J8KZ6YAEv35ZZsaXxnrpp88uTarrLQUWSRCUtkLnoMR", "creatorBalance": 0,
    "mintAuthority": None, "freezeAuthority": None,
    "token": {"mintAuthority": None, "freezeAuthority": None, "supply": 950564845282040, "decimals": 6},
    "topHolders": [
        {"address": "7q7Gr5gYW8LfJWBmxBqPFBZ9i49PMkshiCmW5R8t9Yex", "owner": POOL, "pct": 7.32, "insider": False},
        {"address": "EhUXv55i2GQdhs6GrfVNKjxRPWK2kKA41HCJeSJvVawt", "owner": "Fib52Hb4jMxLZaKeQwoobaa93KXewGCAprUMiDLXz8Qt", "pct": 3.41, "insider": False},
        {"address": "holder3", "owner": "owner3", "pct": 2.0, "insider": True},
    ],
    "knownAccounts": {POOL: {"name": "Pump Fun AMM", "type": "AMM"},
                      "5J8KZ6YAEv35ZZsaXxnrpp88uTarrLQUWSRCUtkLnoMR": {"name": "Creator", "type": "CREATOR"}},
    "markets": [{"pubkey": POOL, "marketType": "pump_fun_amm", "liquidityAAccount": "liqA", "liquidityBAccount": "liqB",
                 "lp": {"quoteUSD": 36751.24, "baseUSD": 38958.52, "lpLockedPct": 100, "lpLockedUSD": 75709.76}}],
    "risks": [{"name": "Mutable metadata", "value": "", "description": "Token metadata can be changed by the owner", "score": 100, "level": "warn"}],
    "score": 101, "score_normalised": 7, "rugged": False, "totalHolders": 7897, "graphInsidersDetected": 244,
}


def test_parse_report_excludes_the_pool_from_holder_concentration():
    parsed = parse_report(RUGCHECK)
    assert parsed["mint_authority_enabled"] == 0 and parsed["freeze_authority_enabled"] == 0
    assert parsed["top_holder_pct"] == 3.41       # not the pool's 7.32
    assert parsed["top10_pct"] == 5.41
    assert parsed["lp_locked_pct"] == 100.0
    assert parsed["risk_score"] == 101 and parsed["risk_score_normalised"] == 7
    assert parsed["rugged"] == 0 and parsed["total_holders"] == 7897 and parsed["insiders_detected"] == 244
    assert json.loads(parsed["risks_json"])[0]["level"] == "warn"
    assert [h["pct"] for h in json.loads(parsed["top_holders_json"])] == [3.41, 2.0]


def test_parse_report_flags_live_authorities():
    report = {**RUGCHECK, "mintAuthority": "5J8KZ6YAEv35ZZsaXxnrpp88uTarrLQUWSRCUtkLnoMR",
              "token": {"freezeAuthority": "5J8KZ6YAEv35ZZsaXxnrpp88uTarrLQUWSRCUtkLnoMR"}}
    parsed = parse_report(report)
    assert parsed["mint_authority_enabled"] == 1 and parsed["freeze_authority_enabled"] == 1


def test_parse_report_tolerates_object_valued_market_accounts():
    # Seen live: some market types give liquidity accounts as objects, not addresses.
    report = {**RUGCHECK, "markets": [{"pubkey": POOL, "liquidityAAccount": {"mint": "x"}, "liquidityBAccount": None, "lp": None}],
              "knownAccounts": {POOL: None}}
    parsed = parse_report(report)
    assert parsed["top_holder_pct"] == 3.41 and parsed["lp_locked_pct"] is None


def test_parse_report_with_no_holder_or_market_data_reports_unknown():
    parsed = parse_report({"score": 1})
    assert parsed["top10_pct"] is None and parsed["top_holder_pct"] is None and parsed["lp_locked_pct"] is None


def test_view_helpers():
    assert is_mint(FRANK) and not is_mint("0xabc") and not is_mint(None)
    assert is_graduated({"migrated_at": 5.0})
    # A pool alone is not a graduation (USDC has pools): we must have watched the launch on pump.fun.
    assert not is_graduated({"dex_id": "pumpswap", "liquidity_usd": 50_000.0})
    launched = {"source": "pumpportal", "launchpad": "pump.fun", "bonding_curve": "Curve"}
    assert is_graduated({**launched, "dex_id": "pumpswap", "liquidity_usd": 50_000.0})    # graduated while we were not listening
    assert not is_graduated({**launched, "dex_id": "orca", "liquidity_usd": 50_000.0})
    assert not is_graduated({**launched, "dex_id": "pumpswap", "liquidity_usd": 50_000.0, "launchpad": "bonk"})
    assert not is_graduated({**launched, "dex_id": "pumpfun"}) and not is_graduated({"dex_id": "bags", "liquidity_usd": None})
    assert not is_graduated({})
    row = view_row({"mint": FRANK, "boosts_active": 0, "boost_seen_at": 100.0, "dex_id": "pumpswap", "liquidity_usd": 1.0}, now=200.0)
    assert row["paid_boost"] is True and row["graduated"] is False and row["has_pool"] is True
    assert view_row({"mint": FRANK, "migrated_at": 5.0}, now=200.0)["graduated"] is True
    assert view_row({"mint": FRANK}, now=200.0)["paid_boost"] is False


ACCOUNT_OBJECT = {"lamports": 1132498392, "owner": "11111111111111111111111111111111", "data": ["", "base64"], "executable": False, "space": 0}
MINT_RISK = {"name": "Mint Authority still enabled", "value": "", "description": "More tokens can be minted by the owner", "level": "danger"}
FREEZE_RISK = {"name": "Freeze Authority still enabled", "value": "", "description": "Tokens can be frozen", "level": "danger"}


def test_parse_report_reads_an_authority_given_as_an_account_object():
    """RugCheck sometimes gives the top-level authority as the authority's account, not its address (WBTC, 2026-10-09)."""
    report = {**RUGCHECK, "mintAuthority": ACCOUNT_OBJECT, "freezeAuthority": None,
              "token": {"mintAuthority": "BCD75RNBHrJJpW4dXVagL5mPjzRLnVZq4YirJdjEYMV7", "freezeAuthority": None},
              "risks": [MINT_RISK]}
    parsed = parse_report(report)
    assert (parsed["mint_authority_enabled"], parsed["freeze_authority_enabled"], parsed["authority_conflict"]) == (1, 0, 0)
    # the object alone is enough, and so is the address under "token" alone
    assert parse_report({**report, "token": {}})["mint_authority_enabled"] == 1
    assert parse_report({**report, "mintAuthority": None})["mint_authority_enabled"] == 1
    both = parse_report({**report, "freezeAuthority": ACCOUNT_OBJECT, "risks": [MINT_RISK, FREEZE_RISK]})
    assert (both["mint_authority_enabled"], both["freeze_authority_enabled"], both["authority_conflict"]) == (1, 1, 0)


def test_parse_report_marks_a_conflict_when_fields_and_risk_list_disagree():
    assert parse_report(RUGCHECK)["authority_conflict"] == 0                                  # both say revoked
    fields_only = parse_report({**RUGCHECK, "mintAuthority": "5J8KZ6YAEv35ZZsaXxnrpp88uTarrLQUWSRCUtkLnoMR"})
    assert fields_only["mint_authority_enabled"] == 1 and fields_only["authority_conflict"] == 1   # fields say enabled, list silent
    list_only = parse_report({**RUGCHECK, "risks": [*RUGCHECK["risks"], FREEZE_RISK]})
    assert list_only["freeze_authority_enabled"] == 0 and list_only["authority_conflict"] == 1     # list says enabled, fields say revoked
    assert parse_report({"score": 1})["authority_conflict"] == 0
