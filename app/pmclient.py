"""Polymarket access: Gamma for discovery, CLOB for books and orders."""
import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import httpx

from . import config, db, model

_live = None
_live_err = None


# ---------------------------------------------------------------- discovery
def _iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def slug_candidates(days=None, cities=None):
    """Every (slug, city, date) the daily temperature series should be using
    over the next few days. Deterministic, so no discovery guesswork."""
    out = []
    pool = cities if cities is not None else list(config.CITIES)
    today = datetime.now(timezone.utc).date()
    for d in range(days if days is not None else config.SLUG_DAYS):
        day = today + timedelta(days=d)
        fields = dict(month=MONTHS[day.month - 1], day=day.day, year=day.year)
        for key in pool:
            c = config.CITIES[key]
            for token in c.get("slug") or [key]:
                for tpl in config.SLUG_TEMPLATES:
                    out.append((tpl.format(city=token, **fields), key,
                                day.strftime("%Y-%m-%d")))
    return out


def _live_cities(pass_no):
    """Probe every city occasionally, but the productive ones every time.

    23 cities x 4 days x 2 templates is 248 requests. Most cities never have a
    market. Once a city has produced one we keep checking it every pass; the
    rest get swept every fourth pass, which keeps discovery quick without ever
    going permanently blind to a new listing.
    """
    hits = db.cfg_get("slug_hits") or {}
    cutoff = time.time() - 72 * 3600
    known = [c for c in config.CITIES if float(hits.get(c, 0)) > cutoff]
    if not known or pass_no % 4 == 0:
        return list(config.CITIES)
    return known


def _record_hit(city):
    hits = db.cfg_get("slug_hits") or {}
    hits[city] = time.time()
    db.cfg_set("slug_hits", hits)


async def _by_slug(cl, take):
    """Direct slug lookups, eight at a time. Needs no search, no tags and no
    pagination, so it is the path least likely to break."""
    async def one(slug):
        try:
            r = await cl.get(f"{config.GAMMA}/events", params={"slug": slug})
            if r.status_code != 200:
                return []
            data = r.json()
            return data if isinstance(data, list) else [data]
        except (httpx.HTTPError, ValueError, TypeError):
            return []

    pass_no = int(db.cfg_get("discover_pass") or 0)
    db.cfg_set("discover_pass", pass_no + 1)
    cities = _live_cities(pass_no)
    cand = slug_candidates(cities=cities)
    hits = 0
    for i in range(0, len(cand), 8):
        batch = cand[i:i + 8]
        for (slug, city, _d), res in zip(
                batch, await asyncio.gather(*(one(c[0]) for c in batch),
                                            return_exceptions=True)):
            if isinstance(res, Exception):
                continue
            for ev in res or []:
                if ev and ev.get("id"):
                    _record_hit(city)
                    if take(ev):
                        hits += 1
    if len(cities) < len(config.CITIES):
        db.log("info", f"slug probe: {len(cities)} known-live cities "
                       f"({len(cand)} lookups); full sweep every 4th pass")
    return hits


async def _tag_ids(cl):
    """Resolve Gamma tag slugs to ids for a category sweep."""
    ids = []
    try:
        r = await cl.get(f"{config.GAMMA}/tags", params={"limit": config.GAMMA_PAGE})
        if r.status_code != 200:
            return ids
        data = r.json()
        data = data if isinstance(data, list) else data.get("data", [])
        for t in data or []:
            blob = f"{t.get('slug', '')} {t.get('label', '')}".lower()
            if any(h in blob for h in config.TAG_HINTS) and t.get("id") is not None:
                ids.append(str(t["id"]))
    except (httpx.HTTPError, ValueError, TypeError) as e:
        db.log("warn", f"gamma tags: {e}")
    return ids[:6]


async def _gather_events(cl):
    """Collect candidate weather events, deduplicated by event id.

    Three paths, cheapest and most reliable first:
      1. direct slug lookup, which is exact
      2. tag sweep
      3. volume-ordered sweep of active events

    Gamma caps a page at 100 no matter what limit you send, so pagination
    continues while a page comes back full rather than comparing against the
    requested size. Sorting by endDate is not supported; volume_24hr is, and
    the daily temperature markets trade enough to sit near the top of it.
    """
    events, seen = [], set()

    def take(ev):
        eid = str(ev.get("id"))
        if not eid or eid in seen:
            return False
        seen.add(eid)
        events.append(ev)
        return True

    async def page(params, pages=1):
        got_any = 0
        for i in range(pages):
            try:
                r = await cl.get(f"{config.GAMMA}/events", params={
                    "active": "true", "closed": "false",
                    "limit": config.GAMMA_PAGE, "offset": i * config.GAMMA_PAGE,
                    "order": "volume_24hr", "ascending": "false", **params})
                if r.status_code != 200:
                    return r.status_code, got_any
                data = r.json()
                data = data if isinstance(data, list) else data.get("data", [])
                n = len(data or [])
                got_any += n
                for ev in data or []:
                    take(ev)
                if n < config.GAMMA_PAGE:      # a short page means the end
                    return 200, got_any
            except (httpx.HTTPError, ValueError) as e:
                db.log("warn", f"gamma {params or 'sweep'}: {e}")
                return None, got_any
        return 200, got_any

    by_slug = await _by_slug(cl, take)
    from_tags = 0
    for tid in await _tag_ids(cl):
        _, n = await page({"tag_id": tid}, pages=3)
        from_tags += n
    if not from_tags:
        # tag_id has never returned anything on this account. The older bot used
        # tag_slug / tag, so try those before giving up on the category sweep --
        # it is the only path that can surface a city we have not configured.
        for variant in ({"tag_slug": "weather"}, {"tag": "weather"},
                        {"tag_slug": "daily-temperature"}):
            _, n = await page(variant, pages=2)
            from_tags += n
            if n:
                db.log("info", f"category sweep works via {list(variant)[0]}")
                break
    before = len(events)
    await page({}, pages=config.SWEEP_PAGES)

    db.log("info", f"gamma: {by_slug} by slug, {from_tags} from tags, "
                   f"{len(events) - before} more from sweep, "
                   f"{len(events)} unique candidates")
    return events


def _date_from_close(ev, city):
    end = _iso(ev.get("endDate"))
    if not end:
        return None
    dt = datetime.fromtimestamp(end, tz=timezone.utc) - timedelta(hours=6)
    tz = config.CITIES[city]["tz"]
    if ZoneInfo:
        try:
            dt = dt.astimezone(ZoneInfo(tz))
        except Exception:
            pass
    return dt.strftime("%Y-%m-%d")


def _outcome_yes(m):
    """Polymarket's own probability for the YES outcome, or None.

    `outcomePrices` is the resolution field: once UMA settles, it reads
    ["1","0"] or ["0","1"]. Before that it carries a live mid, which is why
    the caller still has to check convergence -- this only parses it.
    """
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError, IndexError):
        return None


ALREADY = "already"


def book_resolution(mid, p_yes, conv=None):
    """Write a winner, but only on a decisive outcomePrices.

    Returns 1 or 0 when it books one, None when the market is closed but the
    price has not converged, and ALREADY when the verdict was recorded on an
    earlier pass. Callers need that last case separated out: discovery re-reports
    every resolved market on every scan, so counting them as fresh settlements
    would print the same "N settled" line every cycle for the rest of the run.

    None means "closed but the market is still arguing about it" -- that row
    stays open and keeps marking to market rather than being booked wrong.
    """
    conv = config.SETTLE_MIN_CONVICTION if conv is None else conv
    win = 1 if p_yes > conv else (0 if p_yes < 1 - conv else None)
    if win is None:
        return None
    row = db.one("SELECT winner_src FROM markets WHERE id=?", (str(mid),))
    if not row or (row.get("winner_src") or "") == "gamma":
        return ALREADY          # unknown market, or already Polymarket-settled
    db.ex("UPDATE markets SET closed=1, winner=?, winner_src='gamma', "
          "winner_ts=? WHERE id=?", (win, time.time(), str(mid)))
    return win


def _parse_event(ev):
    """(rows, reason, resolved). rows is empty when the event is not a usable
    weather market; reason always explains the verdict so nothing is skipped
    silently; resolved carries (market_id, p_yes) for buckets Gamma now reports
    closed, which is how this build learns that a market settled."""
    title = ev.get("title") or ev.get("question") or ""
    low = title.lower()
    if any(k in low for k in config.EXCLUDE_KEYWORDS):
        return [], "daily-minimum market, and the model only forecasts the max", []
    if not any(k in low for k in config.MARKET_KEYWORDS):
        return [], "not a temperature market", []
    city = model.match_city(title)
    if not city:
        return [], "city not in the configured list", []
    date = model.parse_date(title)
    if not date:
        # Titles like "…in Chicago today?" carry no date. The close time does:
        # these markets end just after local midnight of the observation day, so
        # step back a few hours and read the date off the city's own clock.
        date = _date_from_close(ev, city)
    if not date:
        return [], "no target date in the title and no usable close time", []

    resolve_ts = None
    if ZoneInfo:
        try:
            d = datetime.strptime(date, "%Y-%m-%d")
            tz = ZoneInfo(config.CITIES[city]["tz"])
            # the high is settled at local midnight; Gamma's endDate on these
            # events is noon UTC on the observation day, which is *before* the
            # afternoon peak. Timing off that makes the late strategies quit
            # hours early.
            resolve_ts = datetime(d.year, d.month, d.day, 23, 59,
                                  tzinfo=tz).timestamp()
        except (ValueError, KeyError):
            resolve_ts = None

    rows, bad, resolved = [], 0, []
    units = set()
    for m in ev.get("markets", []) or []:
        if m.get("closed") or m.get("archived"):
            # This is the moment the bot most reliably learns a market has
            # resolved, and the first build threw it away: the event scan is
            # the only call that keeps returning these, while querying them by
            # id afterwards comes back empty. Capture the verdict here.
            p_yes = _outcome_yes(m)
            if p_yes is not None:
                resolved.append((str(m.get("id")), p_yes))
            continue
        label = m.get("groupItemTitle") or m.get("question") or ""
        rng = model.parse_bucket(label)
        if not rng:
            bad += 1
            continue
        units.add(model.detect_unit(label))
        try:
            toks = m.get("clobTokenIds")
            toks = json.loads(toks) if isinstance(toks, str) else (toks or [])
        except (json.JSONDecodeError, TypeError):
            toks = []
        if len(toks) < 2:
            bad += 1
            continue
        def num(k, d=None):
            v = m.get(k)
            try:
                return float(v)
            except (TypeError, ValueError):
                return d

        fs = m.get("feeSchedule") or {}
        end = _iso(m.get("endDate") or ev.get("endDate")) or 0
        rows.append((str(m.get("id")), str(ev.get("id")), title, label, city, date,
                     rng[0], rng[1], str(toks[0]), str(toks[1]), end,
                     time.time(), time.time(),
                     max(resolve_ts or 0, end) or end,
                     num("bestBid", 0.0), num("bestAsk", 1.0),
                     num("orderPriceMinTickSize", 0.01), num("orderMinSize", 5.0),
                     num("lastTradePrice"), num("rewardsMaxSpread", 4.5),
                     num("rewardsMinSize", 20.0), model.detect_unit(label),
                     float(fs.get("rate", model.FEE_RATE))
                     if m.get("feesEnabled") is not False else 0.0,
                     float(fs.get("exponent", model.FEE_EXP)),
                     1 if m.get("negRisk") else 0))
    if not rows:
        if not (ev.get("markets") or []):
            # Polymarket keeps empty placeholder events for each recurring
            # series. They are expected and there is nothing to fix, so they do
            # not deserve a warning on every pass.
            return [], "empty series placeholder, no markets attached", resolved
        if resolved and not bad:
            # Yesterday's ladder, fully settled. Nothing to store and nothing
            # wrong -- do not let it read as a parser failure.
            return [], f"all {len(resolved)} buckets already resolved", resolved
        return [], f"no parseable buckets ({bad} rejected)", resolved
    note = f"{len(rows)} buckets, {city} {date}, {'/'.join(sorted(units)) or '?'}"
    if not model.parse_date(title):
        note += " (date inferred from close time)"
    return rows, note + (f", {bad} bucket(s) unparsed" if bad else ""), resolved


async def discover():
    """Find open weather events and their mutually exclusive bucket markets."""
    async with httpx.AsyncClient(timeout=25,
                                 headers={"User-Agent": config.USER_AGENT}) as cl:
        events = await _gather_events(cl)
    return _store(events)


def _store(events):
    rows, audit, kept, resolved = [], [], 0, []
    for ev in events:
        r, reason, res = _parse_event(ev)
        rows += r
        resolved += res
        if r:
            kept += 1
        audit.append((time.time(), str(ev.get("id")),
                      (ev.get("title") or "")[:180], 1 if r else 0, len(r), reason))

    with db.tx() as c:
        for r in rows:
            c.execute("""INSERT INTO markets(id,event_id,event_title,question,city,
              target_date,lo,hi,token_yes,token_no,end_ts,seen_ts,updated_ts,
              resolve_ts,g_bid,g_ask,tick,min_size,last_trade,reward_spread,
              reward_min_size,unit,fee_rate,fee_exp,neg_risk)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET end_ts=excluded.end_ts,
              updated_ts=excluded.updated_ts,
              -- never un-close a market Polymarket has already resolved
              closed=CASE WHEN markets.winner_src='gamma' THEN 1 ELSE 0 END,
              resolve_ts=excluded.resolve_ts, g_bid=excluded.g_bid,
              g_ask=excluded.g_ask, tick=excluded.tick,
              min_size=excluded.min_size, last_trade=excluded.last_trade,
              reward_spread=excluded.reward_spread,
              reward_min_size=excluded.reward_min_size, unit=excluded.unit,
              fee_rate=excluded.fee_rate,
              fee_exp=excluded.fee_exp, neg_risk=excluded.neg_risk""", r)
        for a in audit:
            c.execute("""INSERT INTO discovery(ts,event_id,title,accepted,buckets,reason)
              VALUES(?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
              ts=excluded.ts, accepted=excluded.accepted, buckets=excluded.buckets,
              reason=excluded.reason""", a)

    booked = pending = 0
    for mid, p_yes in resolved:
        r = book_resolution(mid, p_yes)
        if r is None:
            pending += 1
        elif r is not ALREADY:
            booked += 1
    if booked:
        db.log("info", f"resolutions from discovery: {booked} settled by Polymarket"
                       + (f", {pending} closed but not yet converged" if pending else ""))

    near = [a for a in audit if not a[3] and a[5] != "not a temperature market"]
    near = [a for a in near
            if "daily-minimum" not in a[5] and "placeholder" not in a[5]
            and "already resolved" not in a[5]]
    db.log("info", f"discovery: {len(events)} events scanned, {kept} usable, "
                   f"{len(rows)} buckets stored, {len(near)} weather-ish but skipped")
    for a in near[:6]:
        db.log("warn", f"skipped '{a[2][:70]}' - {a[5]}")
    if events and not kept and not resolved:
        # `not resolved` matters: a scan that finds only settled ladders is a
        # normal early-morning result, not a broken slug template.
        ex = slug_candidates(1)[0][0]
        db.log("error", f"no usable weather markets in {len(events)} events. "
                        f"Tried slugs like '{ex}'. Compare that against a live "
                        f"polymarket.com/event/<slug> URL and set SLUG_TEMPLATES "
                        f"if the pattern has changed.")
    return len(rows)


_closure_quiet = [0]


async def _markets_by_id(cl, chunk):
    """Gamma's /markets for a list of ids, asking twice if it has to.

    The plain id query is what the first build used, and against a resolved
    market it came back empty -- /markets defaults to live ones -- so a day of
    settlements was missed. closed=true fixes that, but this bot cannot verify
    Gamma's filter semantics from a host that can reach it, so it asks with the
    filter and falls back to the bare query rather than betting the whole
    settlement path on one parameter. Returns (rows, http_failed).
    """
    for params in ([("closed", "true")], []):
        r = await cl.get(f"{config.GAMMA}/markets",
                         params=[("id", i) for i in chunk] + params
                         + [("limit", 20)])
        if r.status_code != 200:
            return [], True
        data = r.json()
        data = data if isinstance(data, list) else data.get("data", [])
        if data:
            return data, False
    return [], False


async def refresh_closures():
    """Record Polymarket's own verdict, and nothing else.

    This is the only function in the bot allowed to declare a winner. Two gates
    before it will: Gamma must report the market closed, and outcomePrices must
    have actually converged. The second gate matters because a closed-but-
    unresolved market keeps reporting a live mid, and `> 0.5` on a mid of 0.55
    reads as a confident YES when the market is still arguing about it. Requiring
    convergence means an ambiguous market stays open and marks to market instead
    of being booked wrong -- worth far more than settling it a few hours sooner.
    """
    # 'stale' rows are included deliberately. sweep_stale() retires a market at
    # 72h with winner_src='stale' and a NULL winner, which leaves any position in
    # it stranded; if Polymarket resolves it late there is no reason not to take
    # the verdict and pay out. The lower bound keeps this query from growing for
    # the length of the run -- after a week it really is never coming.
    open_ids = [r["id"] for r in db.q(
        "SELECT id FROM markets WHERE (closed=0 OR winner_src='stale') "
        "AND COALESCE(resolve_ts, end_ts) BETWEEN ? AND ?",
        (time.time() - 7 * 86400, time.time() + 3600))]
    if not open_ids:
        return 0
    n = pending = 0
    asked = seen = http_bad = 0
    async with httpx.AsyncClient(timeout=25,
                                 headers={"User-Agent": config.USER_AGENT}) as cl:
        for chunk in [open_ids[i:i + 20] for i in range(0, len(open_ids), 20)]:
            asked += len(chunk)
            try:
                data, failed = await _markets_by_id(cl, chunk)
                if failed:
                    http_bad += 1
                    continue
                seen += len(data)
                for m in data:
                    if not m.get("closed"):
                        continue
                    p_yes = _outcome_yes(m)
                    if p_yes is None:
                        continue
                    res = book_resolution(m.get("id"), p_yes)
                    if res is None:
                        pending += 1        # closed but still unresolved: leave it
                    elif res is not ALREADY:
                        n += 1
            except (httpx.HTTPError, ValueError, TypeError) as e:
                db.log("warn", f"closure check: {e}")
    if n or pending:
        db.log("info", f"resolutions: {n} settled by Polymarket"
                       + (f", {pending} closed but not yet converged" if pending else ""))
    if http_bad:
        db.log("warn", f"closure check: {http_bad} chunk(s) returned a non-200")
    # A silent zero here is what hid the bug for a full day. Say so out loud.
    if asked and not seen and not _closure_quiet[0] % 20:
        db.log("warn", f"closure check: asked Gamma about {asked} past-close markets "
                       f"and it returned none. Settlement now depends entirely on the "
                       f"discovery pass; check the id query if this persists.")
    _closure_quiet[0] += 1
    return n


# ---------------------------------------------------------------- order book
async def refresh_prices(markets):
    """Fresh top-of-book straight from Gamma, ~3 calls for 220 markets.

    Gamma prices otherwise only move on the discovery pass, every 15 minutes.
    The late strategies exist to trade the final hours; running them on
    quarter-hour-old quotes is worse than not running them, because they would
    be taking prices that have already moved.
    """
    ids = [m["id"] for m in markets]
    fresh = {}
    if not ids:
        return fresh
    async with httpx.AsyncClient(timeout=20,
                                 headers={"User-Agent": config.USER_AGENT}) as cl:
        for chunk in [ids[i:i + 100] for i in range(0, len(ids), 100)]:
            try:
                r = await cl.get(f"{config.GAMMA}/markets",
                                 params=[("id", i) for i in chunk] + [("limit", 100)])
                if r.status_code != 200:
                    db.log("warn", f"gamma price refresh http {r.status_code}")
                    continue
                data = r.json()
                data = data if isinstance(data, list) else data.get("data", [])
                for m in data or []:
                    def g(k, d=None):
                        try:
                            return float(m.get(k))
                        except (TypeError, ValueError):
                            return d
                    mid = str(m.get("id"))
                    bb, ba = g("bestBid", 0.0), g("bestAsk", 1.0)
                    if ba is None or bb is None or ba <= bb:
                        continue
                    # cumulative traded volume: the only evidence available here
                    # that anybody actually traded, which is what the paper maker
                    # needs before it may claim a fill
                    fresh[mid] = dict(bid=bb, ask=ba, last=g("lastTradePrice"),
                                      volume=g("volumeNum", g("volume")))
            except (httpx.HTTPError, ValueError, TypeError) as e:
                db.log("warn", f"gamma price refresh: {e}")
    if fresh:
        with db.tx() as c:
            for mid, v in fresh.items():
                c.execute("UPDATE markets SET g_bid=?, g_ask=?, last_trade=?, "
                          "volume_num=COALESCE(?, volume_num), updated_ts=? "
                          "WHERE id=?",
                          (v["bid"], v["ask"], v["last"], v.get("volume"),
                           time.time(), mid))
    return fresh


async def books(markets, fresh=None):
    return await _books_live(markets, fresh)


async def _books_live(markets, fresh=None):
    """{market_id: {bid, ask, bid_sz, ask_sz, mid, src}}.

    Gamma already returns bestBid/bestAsk on every market, so that is the
    baseline and it always works. The CLOB is then layered on top for fresh
    prices and real depth. If the CLOB call fails the bot degrades to trading
    off slightly stale Gamma prices rather than going blind, and the source
    counts are logged so you can tell which happened.
    """
    out = {}
    fresh = fresh or {}
    for m in markets:
        f = fresh.get(m["id"]) or {}
        gb = f.get("bid", m.get("g_bid"))
        ga = f.get("ask", m.get("g_ask"))
        gb = 0.0 if gb is None else float(gb)
        ga = 1.0 if ga is None else float(ga)
        if ga > gb:
            out[m["id"]] = dict(bid=gb, ask=ga, mid=(gb + ga) / 2,
                                bid_sz=config.ASSUMED_SIZE,
                                ask_sz=config.ASSUMED_SIZE,
                                src="gamma" if f else "gamma-stale")

    idx = {m["token_yes"]: m["id"] for m in markets if m.get("token_yes")}
    payload = [{"token_id": t} for t in idx]
    live = 0
    if payload:
        async with httpx.AsyncClient(timeout=20,
                                     headers={"User-Agent": config.USER_AGENT}) as cl:
            for chunk in [payload[i:i + 40] for i in range(0, len(payload), 40)]:
                try:
                    r = await cl.post(f"{config.CLOB}/books", json=chunk)
                    if r.status_code != 200:
                        db.log("warn", f"clob /books http {r.status_code}")
                        continue
                    for b in r.json() or []:
                        mid_id = idx.get(str(b.get("asset_id") or b.get("token_id")))
                        if not mid_id:
                            continue
                        bids, asks = b.get("bids") or [], b.get("asks") or []
                        bb = max((float(x["price"]) for x in bids), default=0.0)
                        ba = min((float(x["price"]) for x in asks), default=1.0)
                        if ba <= bb:
                            continue
                        bsz = sum(float(x["size"]) for x in bids
                                  if abs(float(x["price"]) - bb) < 1e-9)
                        asz = sum(float(x["size"]) for x in asks
                                  if abs(float(x["price"]) - ba) < 1e-9)
                        out[mid_id] = dict(bid=bb, ask=ba, mid=(bb + ba) / 2,
                                           bid_sz=bsz, ask_sz=asz, src="clob")
                        live += 1
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                    db.log("warn", f"clob /books: {e}")
    if payload and not live:
        db.log("warn", f"clob depth unavailable; pricing {len(out)} markets off "
                       f"Gamma top-of-book (refreshed each discovery pass)")
    return out


# ---------------------------------------------------------------- execution
def live_client():
    """Lazily build the signing client. Returns None in paper mode."""
    global _live, _live_err
    if _live or not config.LIVE_TRADING:
        return _live
    if _live_err:
        return None
    try:
        from py_clob_client.client import ClobClient
        c = ClobClient(config.CLOB, key=config.PK, chain_id=config.CHAIN_ID,
                       signature_type=config.SIG_TYPE,
                       funder=config.FUNDER or None)
        c.set_api_creds(c.create_or_derive_api_creds())
        _live = c
        db.log("info", "live CLOB client ready")
    except ImportError:
        _live_err = "py-clob-client not installed"
        db.log("error", "LIVE_TRADING is on but py-clob-client is not installed. "
                        "Uncomment it in requirements.txt and redeploy. "
                        "Staying in paper mode; no real orders will be sent.")
    except Exception as e:                      # auth or network failure
        _live_err = str(e)
        db.log("error", f"live client unavailable, staying in paper: {e}")
    return _live


def place(strat, market, side, outcome, price, qty, book, note=""):
    """side: BUY|SELL. outcome: YES|NO. price is limit price for that outcome."""
    mode = "paper"
    order_id = None
    fill_price = price

    if config.LIVE_TRADING:
        c = live_client()
        if c:
            try:
                from py_clob_client.clob_types import OrderArgs
                token = market["token_yes"] if outcome == "YES" else market["token_no"]
                args = OrderArgs(price=round(price, 3), size=round(qty, 2),
                                 side=side, token_id=token)
                resp = c.create_and_post_order(c.create_order(args))
                order_id = (resp or {}).get("orderID")
                mode = "live"
            except Exception as e:
                db.log("error", f"order rejected: {e}", strat)
                return None
    if mode == "paper":
        # cross the spread at the resting price plus slippage
        if side == "BUY":
            fill_price = min(0.999, price + config.SLIPPAGE)
        else:
            fill_price = max(0.001, price - config.SLIPPAGE)

    fee = model.fee_per_share(fill_price, market.get("fee_rate"),
                              market.get("fee_exp")) * qty
    cash = -(qty * fill_price) - fee if side == "BUY" else (qty * fill_price) - fee
    db.ex("""INSERT INTO trades(ts,strat,market_id,question,side,outcome,price,qty,
             cash,fee,mode,note,order_id,role) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (time.time(), strat, market["id"], market["question"], side, outcome,
           fill_price, qty, cash, fee, mode, note, order_id, "taker"))
    _apply_position(strat, market["id"], outcome, side, qty, fill_price)
    return dict(price=fill_price, qty=qty, cash=cash, mode=mode)


def post_quote(strat, market, side, outcome, price, qty, reason=""):
    """Rest a limit order. Paper mode just books it; live mode posts GTC."""
    order_id = None
    if config.LIVE_TRADING:
        c = live_client()
        if c:
            try:
                from py_clob_client.clob_types import OrderArgs
                token = market["token_yes"] if outcome == "YES" else market["token_no"]
                args = OrderArgs(price=round(price, 3), size=round(qty, 2),
                                 side=side, token_id=token)
                resp = c.create_and_post_order(c.create_order(args))
                order_id = (resp or {}).get("orderID")
            except Exception as e:
                db.log("error", f"quote rejected: {e}", strat)
                return None
    # seen_volume and posted_ts are the baseline the fill simulator measures
    # against: a fill needs traded volume to have risen since this moment, and
    # the order to have actually rested for a while first. Re-pricing resets
    # both, because a re-priced order goes to the back of the queue.
    now = time.time()
    db.ex("""INSERT INTO quotes_open(strat,market_id,outcome,side,price,qty,ts,
             order_id,seen_last,seen_volume,posted_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)
             ON CONFLICT(strat,market_id,outcome,side) DO UPDATE SET
             price=excluded.price, qty=excluded.qty, ts=excluded.ts,
             order_id=excluded.order_id, seen_last=excluded.seen_last,
             seen_volume=excluded.seen_volume, posted_ts=excluded.posted_ts""",
          (strat, market["id"], outcome, side, price, qty, now, order_id,
           market.get("last_trade"), market.get("volume_num"), now))
    return dict(price=price, qty=qty, order_id=order_id)


def cancel_quote(strat, mid, outcome, side, order_id=None):
    if config.LIVE_TRADING and order_id:
        c = live_client()
        if c:
            try:
                c.cancel(order_id)
            except Exception as e:
                db.log("warn", f"cancel failed: {e}", strat)
    db.ex("DELETE FROM quotes_open WHERE strat=? AND market_id=? AND outcome=? "
          "AND side=?", (strat, mid, outcome, side))


def fill_maker(strat, market, side, outcome, price, qty, note=""):
    """Record a passive fill: our price, no slippage, no taker fee."""
    cash = -(qty * price) if side == "BUY" else (qty * price)
    db.ex("""INSERT INTO trades(ts,strat,market_id,question,side,outcome,price,qty,
             cash,fee,mode,note,order_id,role) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (time.time(), strat, market["id"], market["question"], side, outcome,
           price, qty, cash, 0.0, "live" if config.LIVE_TRADING else "paper",
           note, None, "maker"))
    _apply_position(strat, market["id"], outcome, side, qty, price)
    return dict(price=price, qty=qty, cash=cash)


def _apply_position(strat, mid, outcome, side, qty, price):
    row = db.one("SELECT qty,avg_price FROM positions WHERE strat=? AND market_id=? "
                 "AND outcome=?", (strat, mid, outcome))
    signed = qty if side == "BUY" else -qty
    if not row:
        db.ex("INSERT INTO positions(strat,market_id,outcome,qty,avg_price,opened_ts)"
              " VALUES(?,?,?,?,?,?)", (strat, mid, outcome, signed, price, time.time()))
        return
    new_q = row["qty"] + signed
    if abs(new_q) < 1e-9:
        db.ex("DELETE FROM positions WHERE strat=? AND market_id=? AND outcome=?",
              (strat, mid, outcome))
        return
    if row["qty"] * signed > 0:                 # adding
        avg = (row["qty"] * row["avg_price"] + signed * price) / new_q
    else:                                       # reducing, keep basis
        avg = row["avg_price"]
    db.ex("UPDATE positions SET qty=?, avg_price=? WHERE strat=? AND market_id=? "
          "AND outcome=?", (new_q, avg, strat, mid, outcome))
