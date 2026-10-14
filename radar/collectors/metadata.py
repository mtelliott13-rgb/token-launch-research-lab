"""Token descriptions, from the metadata file each token's creator uploaded.

Verified 2026-10-09. A pump.fun token's `uri` points at a small JSON file:
  {name, symbol, description, image, createdOn, twitter, telegram, website, ...}
About 86% of the links are https://ipfs.io/ipfs/<cid>. ipfs.io and dweb.link no longer serve
direct requests (HTTP 429, "switching to a service worker gateway only"), so files stored on
IPFS are read through other public gateways instead; the content is identified by the <cid>
in the link, whichever gateway serves it:
  https://ipfs.filebase.io/ipfs/<cid>      200, fast
  https://gateway.pinata.cloud/ipfs/<cid>  200, slow (several seconds)
The rest are launch tools' own servers (metadata.j7tracker.io, meta.uxento.io, ...).

The links are written by anonymous creators, so this module never fetches a link as given
unless its host is on the allow-list: https only, no redirects, a size cap, and nothing but
the description and the social links (twitter, telegram, website) is kept. The links are
stored as text and never fetched. It runs only for tokens that earned a look (see
`want_description` in common.py): once per token, and once more when it graduates, so the
shadow bot sees its links.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from radar.collectors.common import publish_tokens
from radar.context import Ctx
from radar.http import SourceError
from radar.view import clean_text, clean_url

log = logging.getLogger(__name__)

NAME = "metadata"
MAX_DESCRIPTION = 500
# /ipfs/<cid> on any gateway. A cid is letters and digits only, which also keeps anything
# odd (dots, slashes, query strings) out of the URL we build.
_IPFS_PATH = re.compile(r"^/ipfs/([A-Za-z0-9]{40,80})$")


def fetch_urls(uri: Any, cfg: Mapping[str, Any]) -> list[str]:
    """Where to fetch this metadata link from, in order of preference. Empty = do not fetch."""
    if not isinstance(uri, str) or len(uri) > 300:
        return []
    try:
        parts = urlsplit(uri.strip())
    except ValueError:
        return []
    if parts.scheme != "https" or parts.username or parts.port not in (None, 443):
        return []
    ipfs = _IPFS_PATH.match(parts.path)
    if ipfs and not parts.query:
        return [gateway + ipfs.group(1) for gateway in cfg["ipfs_gateways"]]
    if parts.hostname in cfg["allowed_hosts"]:
        return [uri.strip()]
    return []


def parse_description(payload: Any) -> str | None:
    return clean_text(payload.get("description"), MAX_DESCRIPTION) if isinstance(payload, dict) else None


# Which hosts a link must be on to count as that kind of link. A website may be anywhere.
SOCIAL_HOSTS = {"twitter": ("x.com", "twitter.com"), "telegram": ("t.me", "telegram.me", "telegram.dog")}


def parse_links(payload: Any) -> dict[str, str]:
    """The creator's twitter / telegram / website links, where they are plain web links of the right kind."""
    out: dict[str, str] = {}
    if not isinstance(payload, dict):
        return out
    for key in ("twitter", "telegram", "website"):
        url = clean_url(payload.get(key))
        if not url:
            continue
        try:
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue
        if key in SOCIAL_HOSTS and host not in SOCIAL_HOSTS[key]:
            continue
        out[key] = url
    return out


async def fetch_one(ctx: Ctx, row: Mapping[str, Any], now: float) -> str:
    """Fetch one token's description. Returns what happened: done / none / skipped / failed / retrying."""
    cfg, store, mint = ctx.cfg["descriptions"], ctx.store, row["mint"]
    urls = fetch_urls(row.get("metadata_uri"), cfg)
    if not urls:
        store.update_state(mint, desc_state="skipped")
        return "skipped"
    payload, might_pass = None, False
    for url in urls:
        try:
            payload = await ctx.metadata.get_json(url, max_bytes=cfg["max_bytes"])
            break
        except SourceError as exc:
            might_pass = might_pass or exc.status is None   # no status: timeout, server error, rate limit
    if payload is None:
        tries = (row.get("desc_tries") or 0) + 1
        if might_pass and tries < cfg["max_tries"]:
            store.update_state(mint, desc_tries=tries, desc_retry_at=now + cfg["retry_s"])
            return "retrying"
        store.update_state(mint, desc_state="failed", desc_tries=tries)
        return "failed"
    description, links = parse_description(payload), parse_links(payload)
    with store.tx():
        if description or links:
            store.add_raw_event(now, NAME, "description", mint, {"description": description, **links})
        if description and description != row.get("description"):
            store.set_description(mint, description)
        if links:
            # Fills only links we do not have yet; DexScreener's, when it has them, came first.
            store.upsert_token(mint, source=row["source"], first_seen_at=row["first_seen_at"], **links)
        store.update_state(mint, desc_state="done" if description else "none")
    if description or links:
        publish_tokens(ctx, [mint], now)
    return "done" if description else "none"


async def run(ctx: Ctx) -> None:
    cfg = ctx.cfg["descriptions"]
    if not cfg["enabled"]:
        ctx.health.note(NAME, enabled=False)
        return await asyncio.Event().wait()
    while True:
        rows = ctx.store.description_candidates(time.time(), cfg["batch"])
        for row in rows:
            ctx.description_counts[await fetch_one(ctx, row, time.time())] += 1
        ctx.health.note(NAME, **ctx.description_counts, **ctx.metadata.stats())
        if len(rows) < cfg["batch"]:
            await asyncio.sleep(cfg["cycle_s"])
