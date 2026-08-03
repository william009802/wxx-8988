"""The loop. Fetch, price, evaluate, risk-check, execute, mark, persist."""
import asyncio
import math
import time
from datetime import datetime, timezone

from . import config, db, model, pmclient, strategies, weather

STATE = dict(running=False, halted=False, halt_reason="", tick=0,
             last_tick=0, last_forecast=0, last_discover=0,
             markets=0, events=0, errors=0, db_mb=0.0, started=time.time())


# ------------------------------------------------------------------ config
def params(sid):
    saved = db.cfg_get(f"params:{sid}") or {}
    p = strategies.defaults(sid)
    p.update({k: v for k, v in saved.items() if k in p})
    return p


def enabled(sid):
    """Saved toggle if the dashboard has ever set one, otherwise the strategy's
    own default. S6 ships off because its premise was measured and found false;
    a strategy that has to be remembered to be switched off will be running."""
    v = db.cfg_get(f"enabled:{sid}")
    if v is None:
        return bool(strategies.STRATS[sid].get("enabled", True))
    return bool(v)


def bias_known():
    """Cities whose gridpoint-to-station gap has actually been configured.

    A non-zero bias_f means somebody measured it. Zero means either 'measured and
    genuinely zero' or, far more often, 'never looked' -- and the two are
    indistinguishable from here, so both count as unknown. That is the
    conservative reading and it is the one S6 needs.
    """
    return {k: bool(v.get("bias_f")) for k, v in config.CITIES.items()}


def alloc(sid):
    v = db.cfg_get(f"alloc:{sid}")
    return float(v) if v is not None else config.START_BANKROLL / len(strategies.ORDER)


# ------------------------------------------------------------------ books
def cash(sid):
    t = db.one("SELECT COALESCE(SUM(cash),0) c FROM trades WHERE strat=?", (sid,))["c"]
    s = db.one("SELECT COALESCE(SUM(payout),0) p FROM settlements WHERE strat=?",
               (sid,))["p"]
    return alloc(sid) + t + s


def latest_marks():
    """Last seen top-of-book per market, so equity is marked to market even
    outside the trading loop (the dashboard calls this on every refresh)."""
    rows = db.q("""SELECT q.market_id, q.bid, q.ask FROM quotes q
                   JOIN (SELECT market_id, MAX(ts) t FROM quotes GROUP BY market_id) x
                     ON x.market_id=q.market_id AND x.t=q.ts""")
    return {r["market_id"]: dict(bid=r["bid"], ask=r["ask"]) for r in rows}


def positions(sid, marks=None):
    rows = db.q("""SELECT p.*, m.question, m.end_ts, m.city, m.target_date, m.closed
                   FROM positions p LEFT JOIN markets m ON m.id=p.market_id
                   WHERE p.strat=? AND ABS(p.qty)>1e-6""", (sid,))
    for r in rows:
        mk = (marks or {}).get(r["market_id"])
        if mk and mk.get("ask", 1) > mk.get("bid", 0):
            # mark at mid. Buying at the ask and marking at the bid charges the
            # spread twice and reports a loss that has not been taken -- it made
            # a flat book look like it was down 8% within minutes.
            mid = (mk["bid"] + mk["ask"]) / 2
            price = mid if r["outcome"] == "YES" else 1 - mid
        elif mk:
            price = mk.get("bid", r["avg_price"])
        else:
            price = r["avg_price"]
        r["mark"] = price
        r["value"] = r["qty"] * price
        r["upnl"] = r["qty"] * (price - r["avg_price"])
        # cost basis: what was actually paid, not what it marks at now. This is
        # what the position-size cap has to check. Using current mark instead
        # let the cap silently widen as a position went against it -- a losing
        # bucket's mark value shrinks, so the cap saw "room" and let the bot buy
        # more of the same market at a worse price. That is averaging down into
        # a losing thesis while believing a $5 cap is being enforced.
        r["cost"] = abs(r["qty"]) * r["avg_price"]
    return rows


def day_open_equity(sid):
    """Equity at the first mark of the current UTC day, for today's P&L."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                               microsecond=0).timestamp()
    r = db.one("SELECT equity FROM equity WHERE strat=? AND ts>=? ORDER BY ts LIMIT 1",
               (sid, start))
    return r["equity"] if r else None


def book_summary(sid, marks=None):
    pos = positions(sid, marks)
    c = cash(sid)
    pv = sum(p["value"] for p in pos)
    real = db.one("SELECT COALESCE(SUM(pnl),0) p FROM settlements WHERE strat=?",
                  (sid,))["p"]
    fees = db.one("SELECT COALESCE(SUM(fee),0) f FROM trades WHERE strat=?", (sid,))["f"]
    return dict(cash=c, pos_value=pv, equity=c + pv, realized=real - fees,
                unrealized=sum(p["upnl"] for p in pos), positions=pos,
                start=alloc(sid))


# ------------------------------------------------------------------ pricing
def _lead_days(m):
    """Lead time is time until the market resolves, not the calendar date. A
    same-day market closing in four hours carries a fraction of the uncertainty
    of one closing tomorrow, even though both share a target date."""
    h = model.hours_left(m.get("resolve_ts") or m["end_ts"])
    if h <= 0:
        return 0.05
    return max(0.05, h / 24.0)


def price_events(markets, books):
    """Attach book, fair value, uncertainty and forecast revision to each market."""
    events = {}
    for m in markets:
        m["book"] = books.get(m["id"])
        events.setdefault(m["event_id"], []).append(m)

    for ev, group in events.items():
        g0 = group[0]
        fc = weather.get(g0["city"], g0["target_date"])
        if not fc:
            continue
        lead = _lead_days(g0)

        # buckets are quoted in the market's own unit. Convert the forecast to
        # match rather than the buckets, so the half-degree continuity
        # correction stays in the unit the market actually resolves on.
        unit = (g0.get("unit") or "F").upper()
        fc_local = fc
        if unit == "C":
            fc_local = dict(fc, members=[model.f_to_c(x) for x in fc["members"]],
                            mean=model.f_to_c(fc["mean"]), sd=fc["sd"] * 5.0 / 9.0)
        step = 5.0 / 9.0 if unit == "C" else 1.0

        buckets = {m["id"]: (m["lo"], m["hi"]) for m in group}
        fairs, sd = model.fair_prices(buckets, fc_local, lead)

        # anchor the kernel width to what the book is implying
        gap = None
        prices = {}
        for m in group:
            bk = m.get("book")
            if bk and bk["ask"] > bk["bid"]:
                prices[m["id"]] = (bk["bid"] + bk["ask"]) / 2
        tot = sum(prices.values())
        if tot > 0.5:
            prices = {k: v / tot for k, v in prices.items()}
            # location sanity: a large gap between our mean and the book's is a
            # data problem, not an opportunity
            pts = [(model.bucket_centre(*buckets[k]), prices[k]) for k in prices]
            pts = [(c, p) for c, p in pts if c is not None]
            if pts:
                kmean = sum(c * p for c, p in pts) / max(1e-9,
                                                         sum(p for _, p in pts))
                gap = fc_local["mean"] - kmean
                if unit == "C":
                    gap *= 9.0 / 5.0
                if abs(gap) > config.MAX_LOCATION_GAP_F:
                    for m in group:
                        m["fair"] = None
                        m["blocked"] = (f"model mean is {gap:+.1f}F from the "
                                        f"book's; treating as a data problem")
                    db.log("warn", f"{g0['city']} {g0['target_date']}: model mean "
                                   f"{gap:+.1f}F from the market's. Not trading it "
                                   f"— check the station coordinates.")
                    continue
            mkt_sd = model.market_implied_sd(buckets, prices)
            mem2, sd2, before, after = model.calibrate_width(
                fc_local["members"], sd, mkt_sd)
            if abs(after - before) > 1e-6:
                fairs = {k: model.bucket_prob(lo, hi, mem2, sd2)
                         for k, (lo, hi) in buckets.items()}
                fairs = {k: v for k, v in fairs.items() if v is not None}
                t2 = sum(fairs.values())
                if t2 > 0.75:
                    fairs = {k: v / t2 for k, v in fairs.items()}
                fc_local = dict(fc_local, members=mem2)
                sd = sd2
                width_note = (before, after, mkt_sd)

        shifted = {}
        for delta in (-step, step):
            f2 = dict(fc_local,
                      members=[x + delta for x in fc_local["members"]])
            shifted[delta], _ = model.fair_prices(buckets, f2, lead)

        obs = weather.observed(g0["city"])
        width_note = None
        prev = weather.previous(g0["city"], g0["target_date"])
        drift = (fc["mean"] - prev) if prev is not None else None

        for m in group:
            m["fair"] = fairs.get(m["id"])
            m["loc_gap"] = gap
            m["fc"] = fc_local
            m["fc_mean"] = fc_local["mean"]
            m["kernel_sd"] = sd
            m["drift_f"] = drift
            m["obs"] = obs
            m["unit"] = unit
            m["fc_local"] = fc_local
            m["width"] = width_note
            up = shifted[step].get(m["id"], m["fair"] or 0)
            dn = shifted[-step].get(m["id"], m["fair"] or 0)
            m["prob_sd"] = abs(up - dn) / 2
    return events


# ------------------------------------------------------------------ risk
def risk_check(sid, intent, prm, bankroll, free_cash, exposure, regions=None,
               sides=None):
    m, mid = intent["market"], intent["market"]["id"]
    if config.CITY_WHITELIST and m.get("city") not in config.CITY_WHITELIST:
        return None, "city not in the whitelist"
    cost = intent["qty"] * intent["price"]
    if intent["action"] == "BUY":
        # A per-market cap does not stop the book going all-in across many
        # markets. With 200 buckets on offer and a $12 cap each, a $100 book is
        # fully invested inside a few ticks and has nothing left for a better
        # trade an hour later.
        cap_total = bankroll * prm.get("max_total_exposure", 0.55)
        used_total = sum(exposure.values())
        if used_total >= cap_total:
            return None, (f"portfolio cap: {used_total:.0f} of "
                          f"{cap_total:.0f} already at risk")
        if used_total + cost > cap_total:
            room = cap_total - used_total
            intent["qty"] = room / max(intent["price"], 0.01)
            cost = intent["qty"] * intent["price"]

        # a book that is 75% NO is not diversified, it is one view repeated
        if sides is not None:
            side = intent["outcome"]
            tot_side = sum(sides.values()) + cost
            if tot_side > 1:
                share = (sides.get(side, 0.0) + cost) / tot_side
                if share > prm.get("max_side_exposure", 0.75) and tot_side > bankroll * 0.1:
                    return None, (f"{share:.0%} of the book would sit on {side}; "
                                  f"that is one view, not a portfolio")

        # every city under one air mass on one day is a single bet
        if regions is not None:
            key = (config.region(m.get("city")), m.get("target_date"))
            cap_r = bankroll * prm.get("max_region_exposure", 0.20)
            used_r = regions.get(key, 0.0)
            if used_r >= cap_r:
                return None, (f"{key[0]} already carries {used_r:.0f} of "
                              f"{cap_r:.0f} for {key[1]}")
            if used_r + cost > cap_r:
                intent["qty"] = (cap_r - used_r) / max(intent["price"], 0.01)
                cost = intent["qty"] * intent["price"]
        if cost > free_cash:
            intent["qty"] = max(0.0, (free_cash * 0.98) / max(intent["price"], 0.01))
            cost = intent["qty"] * intent["price"]
        cap = prm.get("max_position_usd", 100)
        used = exposure.get(mid, 0.0)
        if used + cost > cap:
            room = cap - used
            if room <= 1:
                return None, "position cap"
            intent["qty"] = room / max(intent["price"], 0.01)
        if intent["qty"] * intent["price"] < 1.0:
            return None, "below min notional"
    if intent["qty"] < config.MIN_SHARES:
        need = config.MIN_SHARES * intent["price"]
        return None, (f"below exchange minimum: {intent['qty']:.1f} shares, needs "
                      f"{config.MIN_SHARES:.0f}. Raise max_position_usd to "
                      f"${need:.2f}+ to trade this price")
    else:
        if intent["qty"] <= 0:
            return None, "nothing to sell"
    return intent, None


def set_halted(on, reason=""):
    """Halt state has to outlive the process.

    Railway restarts containers -- deploys, platform maintenance, OOM -- and the
    old halt lived only in the STATE dict. So a bot that hit its daily loss limit
    came back up trading, having forgotten why it stopped, which over a two-week
    unattended run makes the loss limit decorative.
    """
    STATE["halted"] = bool(on)
    STATE["halt_reason"] = reason if on else ""
    db.cfg_set("halt", dict(on=bool(on), reason=reason,
                            day=datetime.now(timezone.utc).strftime("%Y-%m-%d")))


def restore_halt():
    h = db.cfg_get("halt") or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if h.get("on") and h.get("day") == today:
        STATE["halted"] = True
        STATE["halt_reason"] = h.get("reason", "halted before restart")
        db.log("warn", f"still halted from before the restart: "
                       f"{STATE['halt_reason']}")


def check_halt():
    day = db.cfg_get("day_key")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mk = latest_marks()
    total = sum(book_summary(s, mk)["equity"] for s in strategies.ORDER)
    if day != today:
        db.cfg_set("day_key", today)
        db.cfg_set("day_open_equity", total)
        set_halted(False)
        return
    open_eq = db.cfg_get("day_open_equity") or total
    if open_eq > 0 and total < open_eq * (1 - config.DAILY_LOSS_LIMIT):
        if not STATE["halted"]:
            set_halted(True, f"daily loss limit: {total:.2f} vs open {open_eq:.2f}")
            db.log("error", STATE["halt_reason"])


# ------------------------------------------------------------------ quotes
def sync_quotes(sid, intents, markets_by_id):
    """Keep the book of resting orders in line with what the strategy wants.

    Re-pricing every tick would surrender queue position for nothing, so a quote
    only moves when fair value has drifted past requote_ticks. Quotes the
    strategy no longer wants are cancelled.
    """
    prm = params(sid)
    want = {}
    for it in intents:
        want[(it["market"]["id"], it["outcome"], it["side"])] = it

    live = {(q["market_id"], q["outcome"], q["side"]): q
            for q in db.q("SELECT * FROM quotes_open WHERE strat=?", (sid,))}

    posted = replaced = cancelled = 0
    for key, q in live.items():
        if key not in want:
            pmclient.cancel_quote(sid, q["market_id"], q["outcome"], q["side"],
                                  q["order_id"])
            cancelled += 1

    tolerance = float(prm.get("requote_ticks", 1))
    for key, it in want.items():
        cur = live.get(key)
        tick = it["market"].get("tick") or 0.01
        if cur and abs(cur["price"] - it["price"]) <= tolerance * tick + 1e-9:
            continue                      # close enough, keep our place in line
        pmclient.post_quote(sid, it["market"], it["side"], it["outcome"],
                            it["price"], it["qty"], it["reason"])
        replaced += 1 if cur else 0
        posted += 0 if cur else 1
    return posted, replaced, cancelled


def _capacity(sid, market_id, outcome, side, cost, prm, bs, sides, committed):
    """The same gates risk_check applies to a BUY, applied at the moment a
    resting quote actually fills.

    QUOTE intents from S5 skip risk_check entirely on the way to the book --
    correctly, since a resting order that nobody has taken has committed no
    capital. But the old code never checked anything at the OTHER end either,
    when a fill turns that resting order into real capital at risk. The result
    was $239.91 of BUY notional recorded against a $100 book: every portfolio
    cap existed on paper and none of them applied to the strategy that traded
    the most.
    """
    if side != "BUY":
        return True, None
    cap_total = bs["equity"] * prm.get("max_total_exposure", 1.0)
    if committed[0] + cost > cap_total:
        return False, f"portfolio cap: {committed[0]:.0f} of {cap_total:.0f}"
    if bs["cash"] < cost:
        return False, f"insufficient cash: {bs['cash']:.2f} < {cost:.2f}"
    cap_side = bs["equity"] * prm.get("max_side_exposure", 1.0)
    used_side = sides.get(outcome, 0.0)
    tot_side = sum(sides.values()) + cost
    if tot_side > bs["equity"] * 0.1 and used_side + cost > cap_side:
        return False, f"{outcome} side cap"
    return True, None


def simulate_maker_fills(sid, markets_by_id):
    """Passive fills. We only get filled if somebody actually trades with us.

    The previous version filled on any *change* in Gamma's lastTradePrice while
    our quote was at the touch and through the print. That produced 378 fills in
    three days and made S5 the best strategy on the board, on fills that were
    largely fictional: a price that ticks 0.31 -> 0.32 -> 0.31 in a market
    trading two shares an hour satisfied every condition, repeatedly, and a paper
    maker with no queue and no size limit was filled on all of it.

    Three gates now, and the new one is volume. Gamma reports a cumulative
    volume figure per market; a fill requires it to have actually increased since
    we posted, which is the only evidence available here that a trade occurred at
    all. Then the traded quantity is capped by that increase, so one small print
    cannot fill a large resting order, and a quote must have rested at least
    min_rest_s before it is eligible -- being first in a queue you joined four
    seconds ago is not something to assume.

    This is still optimistic. It assumes we are at the front of the queue and
    that the observed volume traded against us rather than past us. It is now
    optimistic by a bounded amount rather than unboundedly.
    """
    if config.LIVE_TRADING:
        return 0                          # the exchange reports real fills
    n = 0
    prm = params(sid)
    bs = book_summary(sid, {m["id"]: m.get("book") for m in markets_by_id.values()
                            if m.get("book")})
    sides = {"YES": 0.0, "NO": 0.0}
    committed = [0.0]                     # running BUY total across this call
    for p in bs["positions"]:
        sides[p["outcome"]] = sides.get(p["outcome"], 0.0) + p["cost"]
        committed[0] += p["cost"]
    min_rest = prm.get("min_rest_s", 90)
    take = prm.get("volume_share", 0.25)
    need_vol = prm.get("require_volume", 1)
    no_vol = 0
    for q in db.q("SELECT * FROM quotes_open WHERE strat=?", (sid,)):
        m = markets_by_id.get(q["market_id"])
        if not m:
            continue
        bk = m.get("book")
        last = m.get("last_trade")
        if not bk or last is None:
            continue
        keys = q.keys()
        posted = q["posted_ts"] if "posted_ts" in keys else None
        if posted and time.time() - float(posted) < min_rest:
            continue                      # not long enough in the queue to assume a fill
        vol = m.get("volume_num")
        seen_vol = q["seen_volume"] if "seen_volume" in keys else None
        traded = None
        if vol is None or seen_vol is None:
            # No volume figure means no evidence a trade happened, and the whole
            # point of this rewrite is to stop inferring fills from the absence of
            # evidence. Refusing is the honest default: an S5 with no fills is
            # obvious in the data, an S5 full of imaginary ones is not.
            if need_vol:
                no_vol += 1
                continue
        else:
            traded = float(vol) - float(seen_vol)
            if traded <= 0:
                continue                  # nobody traded this market since we posted
        seen = q["seen_last"] if "seen_last" in keys else None
        if seen is not None and abs(float(seen) - float(last)) < 1e-9:
            continue                      # no new print since we posted
        if q["side"] == "BUY":
            at_touch = q["price"] >= bk["bid"] - 1e-9
            crossed = last <= q["price"] + 1e-9
        else:
            at_touch = q["price"] <= bk["ask"] + 1e-9
            crossed = last >= q["price"] - 1e-9
        if not (at_touch and crossed):
            db.ex("UPDATE quotes_open SET seen_last=?, seen_volume=? WHERE strat=? "
                  "AND market_id=? AND outcome=? AND side=?",
                  (last, vol, sid, q["market_id"], q["outcome"], q["side"]))
            continue

        # a print of 8 shares does not fill a resting 60. Take a share of what
        # actually traded, and leave the rest of the order on the book.
        qty = q["qty"]
        if traded is not None:
            fillable = max(0.0, traded * take)
            if fillable < config.MIN_SHARES:
                db.ex("UPDATE quotes_open SET seen_last=?, seen_volume=? WHERE strat=? "
                      "AND market_id=? AND outcome=? AND side=?",
                      (last, vol, sid, q["market_id"], q["outcome"], q["side"]))
                continue
            qty = min(qty, fillable)
        q = dict(q, qty=qty)

        cost = q["qty"] * q["price"]
        ok, why = _capacity(sid, q["market_id"], q["outcome"], q["side"], cost,
                            prm, bs, sides, committed)
        if not ok:
            pmclient.cancel_quote(sid, q["market_id"], q["outcome"], q["side"],
                                  q["order_id"])
            db.log("info", f"quote at capacity, pulled: {why}", sid)
            continue
        pmclient.fill_maker(sid, m, q["side"], q["outcome"], q["price"], q["qty"],
                            f"resting {q['side'].lower()} filled at {q['price']:.3f}")
        pmclient.cancel_quote(sid, q["market_id"], q["outcome"], q["side"],
                              q["order_id"])
        if q["side"] == "BUY":
            bs["cash"] -= cost
            sides[q["outcome"]] = sides.get(q["outcome"], 0.0) + cost
            committed[0] += cost
        n += 1
    if no_vol and time.time() - _no_vol_warned[0] > 3600:
        _no_vol_warned[0] = time.time()
        db.log("warn", f"{no_vol} resting quotes cannot fill: Gamma reported no "
                       f"volume figure for those markets. If this never clears, "
                       f"S5 will book nothing all run -- set require_volume=0 to "
                       f"fall back to the print-only test.", sid)
    return n


_no_vol_warned = [0.0]


# ------------------------------------------------------------------ mark track
_mark_seen = {}     # market_id -> (ts, mid) of the last row written


def write_mark_ticks(markets, now):
    """Keep a full-resolution price track for every market we are holding.

    Raw quotes for all ~900 markets are averaged into hourly bars within hours,
    which is fine for the ones we only watch and useless for the ones we own: an
    hourly mean cannot tell you what bid was available at 14:20, so it cannot
    answer whether cashing out would have beaten holding. That question is worth
    more than any other the data can settle -- most of the last run's damage was
    positions held into a resolution that went against them -- so the markets we
    have money in get their own table, kept for the whole run.

    Held markets only, and only when the price has moved or the cadence has
    elapsed. That is roughly sixty markets rather than nine hundred, at a fifth
    of the tick rate, which is what makes it affordable on a free-tier volume.
    """
    held = {r["market_id"] for r in db.q(
        "SELECT DISTINCT market_id FROM positions WHERE ABS(qty)>1e-6")}
    # A resting quote can fill at any moment, so track what it would fill into.
    held |= {r["market_id"] for r in db.q("SELECT DISTINCT market_id FROM quotes_open")}
    if not held:
        return 0
    rows = []
    for m in markets:
        if m["id"] not in held:
            continue
        bk = m.get("book")
        if not bk:
            continue
        mid = bk.get("mid")
        prev = _mark_seen.get(m["id"])
        if prev and mid is not None and prev[1] is not None:
            if (now - prev[0] < config.MARK_EVERY_S
                    and abs(mid - prev[1]) < config.MARK_MIN_MOVE):
                continue
        _mark_seen[m["id"]] = (now, mid)
        rows.append((now, m["id"], bk["bid"], bk["ask"], mid, m.get("fair"),
                     bk.get("bid_sz"), bk.get("ask_sz"),
                     round(model.hours_left(m.get("resolve_ts") or m["end_ts"]), 3)))
    if rows:
        with db.tx() as c:
            c.executemany("INSERT INTO mark_ticks(ts,market_id,bid,ask,mid,fair,"
                          "bid_sz,ask_sz,hours_left) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    # positions close and quotes get pulled; do not leak their entries forever
    for mid_ in [k for k in _mark_seen if k not in held]:
        _mark_seen.pop(mid_, None)
    return len(rows)


# ------------------------------------------------------------------ settlement
async def record_observed_highs():
    """Fetch and store the temperature actually recorded, for calibration only.

    This used to settle markets, and that was the most expensive bug in the bot.
    Open-Meteo publishes its daily maximum hours before Polymarket resolves, so
    this ran first, wrote closed=1 with a winner derived from a gridpoint
    forecast, and the authoritative Gamma verdict then had nothing left to
    correct. Across a three-day run it scored 38 of 180 settlements against the
    wrong winner and reported +70.6% on a book that was really down 25.8%.

    Two independent errors fed it. The gridpoint is not the station Polymarket
    resolves on -- Houston, Tokyo, Moscow and Seoul missed the winning bucket on
    every single observed day. And the endpoint used here disagrees with the
    hourly one S6 reads by 1-3F, so even the two Open-Meteo numbers did not
    agree with each other.

    So it no longer decides anything. The recorded high is exactly what per-city
    bias calibration needs, which is why it is still fetched and stored -- it is
    evidence, not a verdict.
    """
    due = db.q("""SELECT DISTINCT city, target_date FROM markets
                  WHERE resolve_ts IS NOT NULL AND resolve_ts < ?
                    AND target_date NOT IN (SELECT target_date FROM settled_highs
                                            WHERE city = markets.city)""",
               (time.time() - 3600,))
    n = 0
    for d in due:
        if await weather.actual_high(d["city"], d["target_date"]) is not None:
            n += 1
    return n


def _snapshot(m):
    """Model and book state for a market, as last seen. Written into resolutions
    so the day-15 analysis does not depend on raw quote ticks still existing."""
    q = db.one("SELECT bid,ask,mid,fair FROM quotes WHERE market_id=? "
               "ORDER BY ts DESC LIMIT 1", (m["id"],)) or {}
    if q.get("mid") is None:
        h = db.one("SELECT bid,ask,mid,fair FROM quotes_hourly WHERE market_id=? "
                   "ORDER BY hour DESC LIMIT 1", (m["id"],)) or {}
        q = q or h
        for k in ("bid", "ask", "mid", "fair"):
            if q.get(k) is None:
                q[k] = h.get(k)
    fc = db.one("SELECT ts,mean_f,sd_f FROM forecasts WHERE city=? AND target_date=? "
                "ORDER BY ts LIMIT 1", (m["city"], m["target_date"])) or {}
    lead = None
    if fc.get("ts") and m.get("resolve_ts"):
        lead = round((m["resolve_ts"] - fc["ts"]) / 3600.0, 2)
    hi = db.one("SELECT high_f FROM settled_highs WHERE city=? AND target_date=?",
                (m["city"], m["target_date"])) or {}
    return dict(mid=q.get("mid"), fair=q.get("fair"), mean=fc.get("mean_f"),
                sd=fc.get("sd_f"), lead=lead, high=hi.get("high_f"))


def record_resolutions():
    """One durable row per market Polymarket has resolved.

    Written as the verdict lands rather than derived later, because the inputs
    are perishable: raw quotes are rolled up after hours and the in-memory
    forecast cache dies with the container. Reconstructing this table afterwards
    is exactly the archaeology that made the last run's data so hard to trust.
    """
    rows = db.q("""SELECT * FROM markets
                   WHERE closed=1 AND winner IS NOT NULL
                     AND id NOT IN (SELECT market_id FROM resolutions)""")
    for m in rows:
        s = _snapshot(m)
        db.ex("""INSERT OR REPLACE INTO resolutions(market_id,ts,city,target_date,
                 question,unit,lo,hi,winner,winner_src,observed_high_f,fc_mean_f,
                 fc_sd_f,fc_lead_h,model_fair,market_mid,kernel_sd)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (m["id"], m.get("winner_ts") or time.time(), m["city"],
               m["target_date"], m["question"], m.get("unit") or "F",
               None if m["lo"] is None or math.isinf(m["lo"]) else m["lo"],
               None if m["hi"] is None or math.isinf(m["hi"]) else m["hi"],
               m["winner"], m.get("winner_src") or "unknown", s["high"],
               s["mean"], s["sd"], s["lead"], s["fair"], s["mid"], None))
    return len(rows)


def sweep_stale():
    """Retire markets Polymarket never resolved.

    Nothing is booked and no winner is invented. The market is only lifted out
    of the live set so it stops being priced, quoted and marked, and any position
    left in it is reported rather than silently valued at its last mid forever.
    """
    cut = time.time() - config.STALE_MARKET_H * 3600
    rows = db.q("""SELECT id,question,city,target_date FROM markets
                   WHERE closed=0 AND COALESCE(resolve_ts, end_ts) < ?""", (cut,))
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    ph = ",".join("?" * len(ids))
    stuck = db.q(f"""SELECT strat, COUNT(*) n, SUM(qty*avg_price) cost FROM positions
                     WHERE market_id IN ({ph}) AND ABS(qty)>1e-6
                     GROUP BY strat""", tuple(ids))
    with db.tx() as c:
        c.execute(f"UPDATE markets SET closed=1, winner_src='stale' "
                  f"WHERE id IN ({ph})", tuple(ids))
    db.log("warn", f"{len(rows)} markets past {config.STALE_MARKET_H:.0f}h with no "
                   f"Polymarket resolution; retired unsettled, winner left NULL")
    for s in stuck:
        db.log("warn", f"{s['n']} position(s) worth ${s['cost']:.2f} stranded in "
                       f"markets Gamma never resolved", s["strat"])
    return len(rows)


def settle():
    """Book P&L, and only ever against Polymarket's own resolution.

    The winner_src guard is the whole fix. A row reaches here only if
    refresh_closures saw Gamma report the market closed with a converged
    outcomePrices; a temperature reading can no longer put a position through
    settlement, and neither can the stale sweep.
    """
    rows = db.q("""SELECT p.*, m.question, m.winner, m.winner_src, m.city, m.target_date
                   FROM positions p JOIN markets m ON m.id=p.market_id
                   WHERE m.closed=1 AND m.winner IS NOT NULL
                     AND m.winner_src='gamma' AND ABS(p.qty)>1e-6""")
    for r in rows:
        won = (r["winner"] == 1) if r["outcome"] == "YES" else (r["winner"] == 0)
        payout = r["qty"] * (1.0 if won else 0.0)
        pnl = payout - r["qty"] * r["avg_price"]
        hi = db.one("SELECT high_f FROM settled_highs WHERE city=? AND target_date=?",
                    (r["city"], r["target_date"]))
        db.ex("""INSERT INTO settlements(ts,strat,market_id,question,outcome,qty,
                 avg_price,payout,pnl,actual_high,winner_src)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (time.time(), r["strat"], r["market_id"], r["question"], r["outcome"],
               r["qty"], r["avg_price"], payout, pnl,
               hi["high_f"] if hi else None, r["winner_src"]))
        db.ex("DELETE FROM positions WHERE strat=? AND market_id=? AND outcome=?",
              (r["strat"], r["market_id"], r["outcome"]))
    if rows:
        db.log("info", f"settled {len(rows)} positions on Polymarket's resolution")
    return len(rows)


def repair_settlements():
    """Re-book settlements whose winner was never Polymarket's.

    Runs once at startup against whatever is already in the database. Rows
    settled by the old observation path carry no winner_src; where the book has
    since converged decisively the other way, the winner and every settlement
    row derived from it are corrected in place. cash() sums settlements.payout,
    so fixing these rows repairs each strategy's equity with no separate
    adjusting entry.
    """
    if db.cfg_get("repair:0.16.0"):
        return 0
    conv = config.SETTLE_MIN_CONVICTION
    bad = db.q("""SELECT id, winner, g_bid, g_ask, question, city, target_date
                  FROM markets WHERE closed=1 AND winner IS NOT NULL
                    AND g_bid IS NOT NULL AND g_ask IS NOT NULL
                    AND COALESCE(winner_src,'') <> 'gamma'""")
    fixed = rebooked = 0
    delta = {}
    for m in bad:
        mid = (m["g_bid"] + m["g_ask"]) / 2
        truth = 1 if mid > conv else (0 if mid < 1 - conv else None)
        if truth is None or truth == m["winner"]:
            continue
        db.ex("UPDATE markets SET winner=?, winner_src='gamma-repair' WHERE id=?",
              (truth, m["id"]))
        fixed += 1
        for s in db.q("SELECT rowid,* FROM settlements WHERE market_id=?", (m["id"],)):
            won = (truth == 1) if s["outcome"] == "YES" else (truth == 0)
            payout = s["qty"] * (1.0 if won else 0.0)
            pnl = payout - s["qty"] * s["avg_price"]
            delta[s["strat"]] = delta.get(s["strat"], 0.0) + (pnl - (s["pnl"] or 0.0))
            db.ex("UPDATE settlements SET payout=?, pnl=?, winner_src='gamma-repair' "
                  "WHERE rowid=?", (payout, pnl, s["rowid"]))
            rebooked += 1
    db.cfg_set("repair:0.16.0", True)
    if fixed:
        db.log("warn", f"settlement repair: {fixed} markets had a winner the book "
                       f"decisively disagrees with; re-booked {rebooked} settlements")
        for sid, d in sorted(delta.items(), key=lambda kv: kv[1]):
            db.log("warn", f"equity corrected by {d:+.2f}", sid)
    else:
        db.log("info", "settlement repair: nothing to correct")
    return fixed


# ------------------------------------------------------------------ tick
async def tick():
    STATE["tick"] += 1
    t = STATE["tick"]

    if t == 1 or time.time() - STATE["last_discover"] > config.DISCOVER_EVERY * config.POLL_SECONDS:
        await pmclient.discover()
        # Order matters, and getting it wrong is what produced a fabricated
        # +70.6%. The authoritative verdict has to land before anything books a
        # settlement, and the temperature reading is now only evidence for
        # calibration, so it cannot pre-empt anything either way.
        await pmclient.refresh_closures()
        await record_observed_highs()
        settle()
        record_resolutions()
        sweep_stale()
        STATE["last_discover"] = time.time()

    markets = db.q("""SELECT * FROM markets WHERE closed=0
                      AND COALESCE(resolve_ts, end_ts) > ?
                      ORDER BY COALESCE(resolve_ts, end_ts)""", (time.time(),))
    STATE["markets"] = len(markets)
    if not markets:
        return

    cities = sorted({m["city"] for m in markets})
    if time.time() - STATE["last_forecast"] > config.FORECAST_EVERY * config.POLL_SECONDS:
        await weather.refresh(cities)
        STATE["last_forecast"] = time.time()

    fresh = await pmclient.refresh_prices(markets)
    for m in markets:
        f = fresh.get(m["id"])
        if f:
            m["last_trade"] = f.get("last")
    books = await pmclient.books(markets, fresh)
    srcs = {}
    for b in books.values():
        srcs[b.get("src", "?")] = srcs.get(b.get("src", "?"), 0) + 1
    if STATE["tick"] % 20 == 1:
        db.log("info", f"book sources: {srcs}")
    events = price_events(markets, books)
    STATE["events"] = len(events)

    now = time.time()
    with db.tx() as c:
        for m in markets:
            bk = m.get("book")
            if bk:
                c.execute("INSERT INTO quotes(ts,market_id,bid,ask,mid,bid_sz,ask_sz,"
                          "fair) VALUES(?,?,?,?,?,?,?,?)",
                          (now, m["id"], bk["bid"], bk["ask"], bk["mid"],
                           bk["bid_sz"], bk["ask_sz"], m.get("fair")))
    write_mark_ticks(markets, now)

    check_halt()

    for sid in strategies.ORDER:
        if not enabled(sid) or STATE["halted"]:
            if db.one("SELECT COUNT(*) c FROM quotes_open WHERE strat=?", (sid,))["c"]:
                for q in db.q("SELECT * FROM quotes_open WHERE strat=?", (sid,)):
                    pmclient.cancel_quote(sid, q["market_id"], q["outcome"],
                                          q["side"], q["order_id"])
                db.log("info", "paused, resting quotes pulled", sid)
            continue
        prm = params(sid)
        bs = book_summary(sid, books)
        pos_map = {(p["market_id"], p["outcome"]): p for p in bs["positions"]}
        exposure, regions, sides = {}, {}, {"YES": 0.0, "NO": 0.0}
        for p in bs["positions"]:
            exposure[p["market_id"]] = exposure.get(p["market_id"], 0) + p["cost"]
            rk = (config.region(p.get("city")), p.get("target_date"))
            regions[rk] = regions.get(rk, 0) + p["cost"]
            sides[p["outcome"]] = sides.get(p["outcome"], 0) + p["cost"]

        ctx = dict(markets=[m for m in markets if m.get("book")], events=events,
                   params=prm, bankroll=max(1.0, bs["equity"]), positions=pos_map,
                   now=now, cash=bs["cash"], bias_known=bias_known())
        try:
            intents = strategies.STRATS[sid]["fn"](ctx) or []
        except Exception as e:
            STATE["errors"] += 1
            db.log("error", f"{sid} evaluate: {e}", sid)
            continue

        quotes = [i for i in intents if i.get("action") == "QUOTE"]
        if quotes:
            by_id = {m["id"]: m for m in markets}
            p_, r_, c_ = sync_quotes(sid, quotes, by_id)
            if p_ or r_ or c_:
                db.log("info", f"quotes: +{p_} new, {r_} re-priced, {c_} pulled", sid)
            simulate_maker_fills(sid, by_id)
            intents = [i for i in intents if i.get("action") != "QUOTE"]

        free = bs["cash"]
        for it in intents:
            ok, why = risk_check(sid, it, prm, bs["equity"], free, exposure,
                                 regions, sides)
            db.ex("""INSERT INTO signals(ts,strat,market_id,question,fair,market_p,
                     edge,action,size,taken,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (now, sid, it["market"]["id"], it["market"]["question"],
                   it["fair"], it["market_p"], it["edge"],
                   f"{it['action']} {it['outcome']}", it["qty"], 1 if ok else 0,
                   it["reason"] if ok else f"{it['reason']} | skipped: {why}"))
            if not ok:
                continue
            res = pmclient.place(sid, it["market"], it["action"], it["outcome"],
                                 it["price"], it["qty"], it["market"].get("book"),
                                 it["reason"][:200])
            if res:
                free += res["cash"]
                mid = it["market"]["id"]
                exposure[mid] = exposure.get(mid, 0) + abs(res["cash"])
                rk = (config.region(it["market"].get("city")),
                      it["market"].get("target_date"))
                regions[rk] = regions.get(rk, 0) + abs(res["cash"])
                sides[it["outcome"]] = sides.get(it["outcome"], 0) + abs(res["cash"])

    with db.tx() as c:
        for sid in strategies.ORDER:
            b = book_summary(sid, books)
            c.execute("INSERT INTO equity(ts,strat,cash,pos_value,equity,realized,"
                      "unrealized) VALUES(?,?,?,?,?,?,?)",
                      (now, sid, b["cash"], b["pos_value"], b["equity"],
                       b["realized"], b["unrealized"]))

    if t % 40 == 0:
        db.compact()
        STATE["db_mb"] = db.maintain()
    STATE["last_tick"] = now


async def loop():
    STATE["running"] = True
    db.log("info", f"wxalpha {config.VERSION} starting, "
                   f"mode={'LIVE' if config.LIVE_TRADING else 'paper'}, "
                   f"{len(strategies.ORDER)} strategies, db={config.DB_PATH} "
                   f"({db.db_size_mb()} mb)")
    restore_halt()
    repair_settlements()
    while STATE["running"]:
        try:
            await tick()
        except Exception as e:
            STATE["errors"] += 1
            db.log("error", f"tick failed: {e}")
        await asyncio.sleep(config.POLL_SECONDS)
