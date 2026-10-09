from radar.scoring.bot_volume import likely_bot_volume

CFG = {"min_txns_h1": 300, "max_abs_price_chg_h1": 1.0, "max_abs_price_chg_m5": 1.0, "max_mcap_usd": 1_000_000}


def snap(buys=200, sells=200, h1=0.2, m5=0.0, mcap=80_000):
    return {"buys_h1": buys, "sells_h1": sells, "price_chg_h1": h1, "price_chg_m5": m5, "mcap": mcap}


def test_many_trades_and_a_flat_price_is_flagged():
    assert likely_bot_volume(snap(), CFG) is True
    assert likely_bot_volume(snap(h1=-1.0), CFG) is True


def test_a_moving_price_or_few_trades_is_not_flagged():
    assert likely_bot_volume(snap(h1=8.0), CFG) is False
    assert likely_bot_volume(snap(h1=-3.5), CFG) is False
    assert likely_bot_volume(snap(m5=4.0), CFG) is False
    assert likely_bot_volume(snap(buys=100, sells=100), CFG) is False


def test_missing_figures_give_no_verdict():
    assert likely_bot_volume({}, CFG) is None
    assert likely_bot_volume(snap(h1=None), CFG) is None
    assert likely_bot_volume(snap(mcap=None), CFG) is None
    assert likely_bot_volume(snap(m5=None), CFG) is True


def test_only_tokens_under_the_market_cap_limit_are_judged():
    assert likely_bot_volume(snap(mcap=999_999), CFG) is True
    assert likely_bot_volume(snap(mcap=1_000_000), CFG) is False
    assert likely_bot_volume(snap(mcap=60_000_000_000), CFG) is False   # USDC: busy and flat, not a bot
