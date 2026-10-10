"""On-chain check of mint and freeze authority for tokens on Rising: the chain wins over RugCheck."""
import json
import time

import httpx
import pytest

from radar.collectors import solana_rpc
from radar.config import load_settings
from radar.db.store import Store
from radar.http import SourceError
from radar.lab import why
from radar.view import authorities, view_row
from tests.test_ingest import run

CFG = load_settings()
AUTH = "BCD75RNBHrJJpW4dXVagL5mPjzRLnVZq4YirJdjEYMV7"


def mint_account(mint_authority=None, freeze_authority=None, program="spl-token"):
    return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"program": program, "parsed": {
        "type": "mint", "info": {"mintAuthority": mint_authority, "freezeAuthority": freeze_authority, "decimals": 6}}}}


def rpc(*accounts):
    return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": list(accounts)}}


def test_parse_accounts():
    wallet = {"owner": "11111111111111111111111111111111", "data": ["", "base64"]}                  # not a token mint
    token_account = {"data": {"program": "spl-token", "parsed": {"type": "account", "info": {}}}}  # a holder's account
    found = solana_rpc.parse_accounts(
        ["LIVE", "FROZEN", "CLEAN", "T22", "GONE", "WALLET", "HOLDER"],
        rpc(mint_account(AUTH), mint_account(None, AUTH), mint_account(), mint_account(AUTH, AUTH, "spl-token-2022"), None, wallet, token_account),
    )
    assert found == {"LIVE": (1, 0), "FROZEN": (0, 1), "CLEAN": (0, 0), "T22": (1, 1), "GONE": None, "WALLET": None, "HOLDER": None}
    with pytest.raises(SourceError):
        solana_rpc.parse_accounts(["A"], {"jsonrpc": "2.0", "error": {"code": -32005, "message": "Too many requests"}})
    with pytest.raises(SourceError):
        solana_rpc.parse_accounts(["A", "B"], rpc(mint_account()))     # one answer for two questions


def test_each_token_is_checked_at_most_every_30_minutes():
    cfg, now = CFG["onchain"], 100_000.0
    rows = [{"mint": "NEW", "chain_checked_at": None}, {"mint": "RECENT", "chain_checked_at": now - 600},
            {"mint": "STALE", "chain_checked_at": now - 1801}, {"mint": "EDGE", "chain_checked_at": now - 1800}]
    assert cfg["recheck_s"] == 1800
    assert [r["mint"] for r in solana_rpc.due(rows, now, cfg)] == ["NEW", "STALE", "EDGE"]
    many = [{"mint": f"M{i}", "chain_checked_at": None} for i in range(250)]
    assert len(solana_rpc.due(many, now, cfg)) == 100                  # one call's worth


def test_the_chain_wins_over_rugcheck():
    base = {"safety_ts": 5.0, "mint_authority_enabled": 0, "freeze_authority_enabled": 0, "authority_conflict": 0}
    only_rugcheck = authorities(base)
    assert only_rugcheck["authority_source"] == "rugcheck" and not only_rugcheck["chain_disagrees"]
    agrees = authorities({**base, "chain_checked_at": 9.0, "chain_mint_authority": 0, "chain_freeze_authority": 0})
    assert agrees["authority_source"] == "chain" and not agrees["chain_disagrees"] and agrees["mint_authority_enabled"] == 0
    wrong = authorities({**base, "chain_checked_at": 9.0, "chain_mint_authority": 1, "chain_freeze_authority": 0})
    assert wrong["mint_authority_enabled"] == 1 and wrong["chain_disagrees"] is True
    # a self-contradicting report stops being "Unverified" once the chain has spoken
    settled = authorities({**base, "authority_conflict": 1, "chain_checked_at": 9.0, "chain_mint_authority": 0, "chain_freeze_authority": 0})
    assert settled["authority_conflict"] == 0 and authorities({**base, "authority_conflict": 1})["authority_conflict"] == 1
    # no RugCheck report at all: the chain still gives an answer, and there is nothing to disagree with
    alone = authorities({"safety_ts": None, "chain_checked_at": 9.0, "chain_mint_authority": 1, "chain_freeze_authority": 1})
    assert alone["mint_authority_enabled"] == 1 and alone["chain_disagrees"] is False
    # an account we could not read leaves RugCheck's reading in place
    unreadable = authorities({**base, "chain_checked_at": 9.0, "chain_mint_authority": None, "chain_freeze_authority": None})
    assert unreadable["authority_source"] == "rugcheck"


def test_check_saves_the_chain_reading_in_one_call_and_flags_disagreement(caplog):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        answers = {"WRONG": mint_account(AUTH), "RIGHT": mint_account(), "GONE": None}
        return httpx.Response(200, json=rpc(*[answers[m] for m in body["params"][0]]))

    async def flow(ctx):
        s, now = ctx.store, time.time()
        for mint in ("WRONG", "RIGHT", "GONE"):
            s.upsert_token(mint, source="gecko", first_seen_at=now, created_at=now, symbol=mint)
            s.update_state(mint, safety_ts=now, mint_authority_enabled=0, freeze_authority_enabled=0)
        rows = s.rows(["WRONG", "RIGHT", "GONE"])
        disagreed = await solana_rpc.check(ctx, rows, now)
        return disagreed, await solana_rpc.check(ctx, s.rows(["WRONG"]), now + 1800)   # same answer later: not counted twice

    (first, again), ctx, events = run(handler, flow)
    s = ctx.store
    assert (first, again) == (1, 0) and len(calls) == 2 and calls[0]["method"] == "getMultipleAccounts"
    assert sorted(calls[0]["params"][0]) == ["GONE", "RIGHT", "WRONG"] and calls[0]["params"][1]["encoding"] == "jsonParsed"
    wrong, right, gone = (view_row(s.row(m), time.time()) for m in ("WRONG", "RIGHT", "GONE"))
    assert wrong["mint_authority_enabled"] == 1 and wrong["chain_disagrees"] and wrong["authority_source"] == "chain"
    assert right["mint_authority_enabled"] == 0 and not right["chain_disagrees"] and right["authority_source"] == "chain"
    assert gone["authority_source"] == "rugcheck" and s.row("GONE")["chain_checked_at"] is not None   # not asked again for 30 minutes
    assert s.row("WRONG")["mint_authority_enabled"] == 0                 # RugCheck's own reading is kept as it was
    assert ctx.onchain_counts == {"checked": 3, "unreadable": 1, "disagreed": 1}
    assert "has mint authority enabled and freeze authority revoked on-chain; RugCheck's report said revoked / revoked" in caplog.text
    pushed = {r["mint"]: r for event, rows in events if event == "tokens" for r in rows}
    assert pushed["WRONG"]["chain_disagrees"] is True


def test_filter_and_why_panel_follow_the_chain():
    s = Store(":memory:")
    for mint in ("WRONG", "CLEARED", "PLAIN"):
        s.upsert_token(mint, source="gecko", first_seen_at=1.0, created_at=1.0, symbol=mint)
    s.update_state("WRONG", safety_ts=5.0, mint_authority_enabled=0, freeze_authority_enabled=0,
                   chain_checked_at=9.0, chain_mint_authority=1, chain_freeze_authority=0)
    s.update_state("CLEARED", safety_ts=5.0, mint_authority_enabled=1, freeze_authority_enabled=0, authority_conflict=1,
                   chain_checked_at=9.0, chain_mint_authority=0, chain_freeze_authority=0)
    s.update_state("PLAIN", safety_ts=5.0, mint_authority_enabled=0, freeze_authority_enabled=0)
    assert sorted(r["mint"] for r in s.launches(limit=10, hide_authority=True)) == ["CLEARED", "PLAIN"]

    def warnings(mint):
        row = s.row(mint)
        measured = why.measure(row, {"safety": {"top_holders_json": "[]", "risks_json": "[]"}}, 100.0, CFG["why"], CFG["momentum"])
        return {w["key"]: w for w in why.find_warnings(measured, CFG["why"])}

    wrong = warnings("WRONG")
    assert set(wrong) == {"chain_disagrees", "mint_authority"} and wrong["chain_disagrees"]["level"] == "high"
    assert wrong["chain_disagrees"]["text"] == ("On-chain, mint authority is enabled and freeze authority is revoked. "
                                                "RugCheck's report says otherwise; we go by the chain.")
    cleared = warnings("CLEARED")
    assert set(cleared) == {"chain_disagrees"} and cleared["chain_disagrees"]["level"] == "warn" and not cleared["chain_disagrees"]["own"]
    assert warnings("PLAIN") == {}
