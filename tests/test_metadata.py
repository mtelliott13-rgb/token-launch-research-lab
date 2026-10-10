"""Fetching token descriptions from creator-supplied metadata links."""
import time

import httpx

from radar import rising
from radar.collectors import dexscreener, metadata
from radar.collectors.common import want_description
from radar.config import load_settings
from radar.lab import backfill
from radar.scoring.themes import Tagger
from tests.test_ingest import run
from tests.test_parsers import DEX_CURVE, DEX_GRADUATED, FIX, FRANK

CFG = load_settings()["descriptions"]
CID = "QmNRPLRXYniL45zySjt5imzdXRo7Y5BHFrFPuaxViHrSaT"
CID_V1 = "bafkreic6nvbwfl356ab4ybcdqrfpl6fwj64zdhr3lyaqf2vjq3q4yicnze"
FILEBASE, PINATA = f"https://ipfs.filebase.io/ipfs/{CID}", f"https://gateway.pinata.cloud/ipfs/{CID}"


def test_ipfs_links_go_through_our_gateways_whatever_host_the_creator_wrote():
    assert metadata.fetch_urls(f"https://ipfs.io/ipfs/{CID}", CFG) == [FILEBASE, PINATA]
    assert metadata.fetch_urls(f"https://evil.example/ipfs/{CID}", CFG) == [FILEBASE, PINATA]      # only the cid is used
    assert metadata.fetch_urls(f"https://pump.mypinata.cloud/ipfs/{CID_V1}", CFG)[0] == f"https://ipfs.filebase.io/ipfs/{CID_V1}"
    assert metadata.fetch_urls("https://metadata.j7tracker.io/metadata/abc.json", CFG) == ["https://metadata.j7tracker.io/metadata/abc.json"]


def test_links_we_have_no_reason_to_trust_are_not_fetched():
    refused = [
        "https://unknown-host.example/meta.json",                # host not on the list
        "http://201.78.143.16:3000/meta/1",                      # plain http, bare address
        "http://metadata.j7tracker.io/metadata/abc.json",        # allowed host, but not https
        "https://metadata.j7tracker.io:8443/metadata/abc.json",  # odd port
        "https://user@metadata.j7tracker.io/x",                  # credentials in the link
        "https://localhost/ipfs/short",                          # not a real cid
        f"https://ipfs.io/ipfs/{CID}/../../admin",               # path tricks after the cid
        f"https://ipfs.io/ipfs/{CID}?redirect=http://10.0.0.1",
        "file:///etc/passwd", "javascript:alert(1)", "", None, 42, "https://[bad",
    ]
    assert [u for u in refused if metadata.fetch_urls(u, CFG)] == []


def test_description_is_cleaned_and_capped():
    assert metadata.parse_description({"description": "  The official cat.  "}) == "The official cat."
    assert len(metadata.parse_description({"description": "x" * 5000})) == 500
    assert metadata.parse_description({"description": ""}) is None and metadata.parse_description({"description": {"a": 1}}) is None
    assert metadata.parse_description(["not", "a", "dict"]) is None


def serve(files):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return files.get(str(request.url), httpx.Response(404))
    return handler, calls


def seeded(ctx, mint, uri, name="Mystery", **state):
    ctx.store.tagger = Tagger.from_file()
    ctx.store.upsert_token(mint, source="pumpportal", first_seen_at=time.time(), name=name, metadata_uri=uri)
    ctx.store.update_state(mint, **state)
    return ctx.store.row(mint)


def test_fetch_saves_the_description_and_retags_the_token():
    handler, calls = serve({FILEBASE: httpx.Response(200, json={"name": "Mystery", "description": "A coin for cat lovers", "image": "x"})})

    async def flow(ctx):
        row = seeded(ctx, FRANK, f"https://ipfs.io/ipfs/{CID}", desc_state="wanted")
        before = row["theme"]
        return before, await metadata.fetch_one(ctx, row, time.time())

    (before, result), ctx, events = run(handler, flow)
    row = ctx.store.row(FRANK)
    assert (before, result) == ("other", "done") and calls == [FILEBASE]
    assert row["description"] == "A coin for cat lovers" and row["theme"] == "animal" and row["desc_state"] == "done"
    assert events[-1][1][0]["theme"] == "animal"
    assert ctx.store.db.execute("SELECT payload FROM raw_events WHERE kind = 'description'").fetchone()[0] == '{"description":"A coin for cat lovers"}'


def test_second_gateway_is_tried_and_failures_end_in_a_recorded_state():
    handler, calls = serve({
        PINATA: httpx.Response(200, json={"description": ""}),
        "https://meta.uxento.io/data/gone": httpx.Response(404),
        "https://meta.uxento.io/data/down": httpx.Response(503),
    })

    async def flow(ctx):
        now = time.time()
        a = seeded(ctx, "A", f"https://ipfs.io/ipfs/{CID}")
        b = seeded(ctx, "B", "https://meta.uxento.io/data/gone")
        c = seeded(ctx, "C", "https://meta.uxento.io/data/down")
        d = seeded(ctx, "D", "https://unknown-host.example/meta.json")
        first = [await metadata.fetch_one(ctx, row, now) for row in (a, b, c, d)]
        waiting = [r["mint"] for r in ctx.store.description_candidates(now, 10)]
        c2 = await metadata.fetch_one(ctx, ctx.store.row("C"), now + 1000)
        c3 = await metadata.fetch_one(ctx, ctx.store.row("C"), now + 2000)
        return first, waiting, c2, c3

    (first, waiting, c2, c3), ctx, _ = run(handler, flow)
    assert first == ["none", "failed", "retrying", "skipped"]      # empty description / gone for good / may pass / not fetched
    assert calls[:2] == [FILEBASE, PINATA] and not any("unknown-host" in c for c in calls)
    assert waiting == [] and (c2, c3) == ("retrying", "failed")     # three tries, then it stops
    states = {m: ctx.store.row(m)["desc_state"] for m in "ABCD"}
    assert states == {"A": "none", "B": "failed", "C": "failed", "D": "skipped"}


def test_only_tokens_that_earned_it_are_queued():
    quiet, busy = {**DEX_CURVE, "marketCap": 4_000}, {**DEX_CURVE, "marketCap": 12_500}
    pairs = {FIX: quiet}

    def handler(request):
        return httpx.Response(200, json=[p for m, p in pairs.items() if m in str(request.url)])

    async def flow(ctx):
        s = ctx.store
        for mint in (FIX, FRANK, "RISER", "NOLINK"):
            s.upsert_token(mint, source="pumpportal", first_seen_at=time.time(), created_at=time.time() - 600,
                           metadata_uri=None if mint == "NOLINK" else f"https://ipfs.io/ipfs/{CID}")
        states = lambda: {m: s.row(m)["desc_state"] for m in (FIX, FRANK, "RISER", "NOLINK")}  # noqa: E731
        await dexscreener.refresh_batch(ctx, [s.row(FIX)])                 # $4k on the curve: not worth a request
        low = states()
        pairs[FIX] = busy
        pairs[FRANK] = DEX_GRADUATED
        await dexscreener.refresh_batch(ctx, [s.row(FIX), s.row(FRANK)])   # passed $10k / graduated
        for mint in ("RISER", "NOLINK"):                                   # on the Rising list
            s.update_state(mint, score=80.0, snapshot_ts=time.time())
        rows = rising.rising_rows(ctx, time.time())
        with s.tx():
            for row in rows:
                want_description(ctx, row)
        return low, states()

    (low, after), ctx, _ = run(handler, flow)
    assert low == {FIX: None, FRANK: None, "RISER": None, "NOLINK": None}
    assert after == {FIX: "wanted", FRANK: "wanted", "RISER": "wanted", "NOLINK": None}


def test_existing_tokens_that_already_qualified_are_queued_once():
    async def flow(ctx):
        s = ctx.store
        uri = f"https://ipfs.io/ipfs/{CID}"
        for mint, state in (("ROSE", {"rising_seen_at": 5.0}), ("GRAD", {"migrated_at": 5.0}), ("PEAKED", {"mcap": 3_000.0}),
                            ("DEAD", {"mcap": 3_000.0}), ("HASDESC", {"migrated_at": 5.0})):
            s.upsert_token(mint, source="pumpportal", first_seen_at=1.0, metadata_uri=uri,
                           description="already known" if mint == "HASDESC" else None)
            s.update_state(mint, **state)
        s.add_snapshot("PEAKED", 2.0, "dexscreener", {"price_usd": 1.0, "mcap": 25_000.0})   # was above $10k once
        return backfill.queue_descriptions(s, ctx.cfg, 10.0), backfill.queue_descriptions(s, ctx.cfg, 20.0)

    (first, again), ctx, _ = run(lambda request: None, flow)
    assert (first, again) == (3, None)
    assert sorted(r["mint"] for r in ctx.store.description_candidates(100.0, 10)) == ["GRAD", "PEAKED", "ROSE"]
    assert {m: ctx.store.row(m)["desc_state"] for m in ("ROSE", "GRAD", "PEAKED", "DEAD", "HASDESC")} == {
        "ROSE": "wanted", "GRAD": "wanted", "PEAKED": "wanted", "DEAD": None, "HASDESC": None}
