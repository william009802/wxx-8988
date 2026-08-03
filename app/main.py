"""HTTP surface: dashboard, controls, export."""
import asyncio
import json
import math
import os
import time
from datetime import datetime

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from . import config, db, engine, model, pmclient, strategies

app = FastAPI(title="wxalpha")
HERE = os.path.dirname(__file__)
_task = None


@app.on_event("startup")
async def _start():
    global _task
    try:
        db.connect()
    except db.EphemeralStorage as e:
        # Loud and unmissable, because the alternative is a container that boots
        # fine and throws away two weeks of data at the next deploy. Railway
        # shows stdout; it does not show a FastAPI startup traceback nearly as
        # clearly, and this message is the remediation.
        print("\n" + "=" * 68 + f"\nSTORAGE NOT DURABLE\n{e}\n" + "=" * 68 + "\n",
              flush=True)
        raise
    if db.cfg_get("day_key") is None:
        db.cfg_set("day_key", time.strftime("%Y-%m-%d"))
        db.cfg_set("day_open_equity", config.START_BANKROLL)
    _task = asyncio.create_task(engine.loop())


@app.on_event("shutdown")
async def _stop():
    engine.STATE["running"] = False
    if _task:
        _task.cancel()


@app.get("/")
def index():
    """Served with the build stamped in, so a plain page fetch shows which
    version is live. Guessing at that has cost us several rounds."""
    with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__VERSION__", config.VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-store",
                                       "X-Wxalpha-Build": config.VERSION})


@app.get("/version.txt")
def version_txt():
    """Plain text, no JSON, nothing to parse."""
    return PlainTextResponse(
        f"wxalpha {config.VERSION}\n"
        f"strategies {len(strategies.ORDER)}: {' '.join(strategies.ORDER)}\n"
        f"cities {len(config.CITIES)}\n"
        f"fee_rate {config.FEE_RATE if hasattr(config, 'FEE_RATE') else 0.05}\n"
        f"live {config.LIVE_TRADING}\n"
        f"db {config.DB_PATH} ({db.db_size_mb()} mb)\n"
        f"tick {engine.STATE['tick']} running {engine.STATE['running']}\n"
        f"features {' '.join(config.FEATURES)}\n")


@app.get("/health")
def health():
    """Fails when the trading loop has stopped ticking, not merely when the
    process has died.

    Railway's healthcheck restarts the container on a non-200. The old version
    returned ok unconditionally, so an asyncio loop wedged on a socket that never
    times out looked perfectly healthy while the bot collected nothing -- the one
    failure an unattended two-week run cannot afford to be invisible.

    The grace period is deliberate: the loop has not ticked at all before the
    first pass completes, and failing the check during startup would restart the
    container forever.
    """
    age = time.time() - (engine.STATE["last_tick"] or engine.STATE["started"])
    up = time.time() - engine.STATE["started"]
    stale = age > config.HEALTH_MAX_TICK_AGE_S and up > config.HEALTH_MAX_TICK_AGE_S
    body = {"ok": not stale, "tick": engine.STATE["tick"],
            "last_tick_age_s": round(age), "running": engine.STATE["running"],
            "halted": engine.STATE["halted"], "db_mb": db.db_size_mb(),
            "version": config.VERSION}
    if stale:
        body["error"] = (f"no tick for {age / 60:.1f} min "
                         f"(limit {config.HEALTH_MAX_TICK_AGE_S / 60:.0f} min)")
        return Response(json.dumps(body), status_code=503,
                        media_type="application/json")
    return body


@app.get("/api/version")
def version():
    """What is actually running. Check this before debugging anything else."""
    return dict(version=config.VERSION, features=config.FEATURES,
                live=config.LIVE_TRADING, db_path=config.DB_PATH,
                db_mb=db.db_size_mb(), start_bankroll=config.START_BANKROLL,
                strategies=strategies.ORDER, cities=sorted(config.CITIES),
                poll_seconds=config.POLL_SECONDS,
                fee_rate=model.FEE_RATE,
                uptime_h=round((time.time() - engine.STATE["started"]) / 3600, 2),
                tick=engine.STATE["tick"], running=engine.STATE["running"])


def clean(o):
    """Strip infinities and NaN before anything is serialised.

    Open-ended buckets store lo=-inf or hi=+inf. Python's json module happily
    emits the token `Infinity`, which is not legal JSON, so FastAPI raises and
    the endpoint 500s. The dashboard fetches state and ribbon together, so one
    bad float took the whole page down with "server unreachable".
    """
    if isinstance(o, float):
        return None if (math.isinf(o) or math.isnan(o)) else o
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


CITY_LABEL = None


def label_rows(rows):
    """Attach 'London 27 Jul' to every row that only carries a bucket label.

    Six of the sixteen live cities quote in Celsius, so a positions table full of
    bare '25C' entries is unreadable -- you cannot tell London from Seoul from
    Toronto.
    """
    global CITY_LABEL
    if CITY_LABEL is None:
        CITY_LABEL = {k: v["name"] for k, v in config.CITIES.items()}
    ids = {r.get("market_id") for r in rows if r.get("market_id")}
    if not ids:
        return rows
    q = ",".join("?" * len(ids))
    meta = {m["id"]: m for m in db.q(
        f"SELECT id, city, target_date, unit FROM markets WHERE id IN ({q})",
        tuple(ids))}
    for r in rows:
        m = meta.get(r.get("market_id"))
        if not m:
            continue
        r["city"] = CITY_LABEL.get(m["city"], m["city"])
        r["target_date"] = m["target_date"]
        r["unit"] = m["unit"] or "F"
        try:
            d = datetime.strptime(m["target_date"], "%Y-%m-%d").strftime("%-d %b")
        except (ValueError, TypeError):
            d = m["target_date"]
        r["label"] = f"{r['city']} {d} · {r.get('question') or ''}".strip(" ·")
    return rows


def _series(sid, limit=400):
    rows = db.q("SELECT ts,equity FROM equity WHERE strat=? ORDER BY ts DESC LIMIT ?",
                (sid, limit))
    return [[r["ts"], round(r["equity"], 2)] for r in reversed(rows)]


def _stats(sid, marks=None):
    b = engine.book_summary(sid, marks if marks is not None else engine.latest_marks())
    s = db.one("""SELECT COUNT(*) n, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) w,
                  COALESCE(SUM(pnl),0) p FROM settlements WHERE strat=?""", (sid,))
    n_trades = db.one("SELECT COUNT(*) n FROM trades WHERE strat=?", (sid,))["n"]
    start = b["start"]
    dayopen = engine.day_open_equity(sid)
    return dict(
        id=sid, name=strategies.STRATS[sid]["name"],
        tagline=strategies.STRATS[sid]["tagline"],
        hue=strategies.STRATS[sid]["hue"], enabled=engine.enabled(sid),
        equity=round(b["equity"], 2), cash=round(b["cash"], 2), start=round(start, 2),
        pnl=round(b["equity"] - start, 2),
        ret=round((b["equity"] / start - 1) * 100, 2) if start else 0,
        realized=round(b["realized"], 2), unrealized=round(b["unrealized"], 2),
        day_pnl=round(b["equity"] - dayopen, 2) if dayopen is not None else None,
        open_risk=round(b["equity"] - b["cash"], 2),
        open_positions=len(b["positions"]), n_trades=n_trades,
        settled=s["n"] or 0,
        win_rate=round(100 * (s["w"] or 0) / s["n"], 1) if s["n"] else None)


@app.get("/api/spec")
def spec():
    return dict(order=strategies.ORDER, strategies=strategies.spec(),
                live=config.LIVE_TRADING)


@app.get("/api/state")
def state():
    mk = engine.latest_marks()
    per = [_stats(s, mk) for s in strategies.ORDER]
    tot_eq = sum(p["equity"] for p in per)
    tot_start = sum(p["start"] for p in per)
    # Build the engine block by update(), not dict(**STATE, ...). The splat
    # form raises TypeError the moment STATE gains a key this endpoint also
    # supplies -- which is exactly what maintain() writing STATE["db_mb"] did,
    # and it took the whole dashboard down 30 minutes into a run.
    eng = dict(engine.STATE)
    eng.update(live=config.LIVE_TRADING, poll=config.POLL_SECONDS,
               db_mb=db.db_size_mb(),
               uptime_h=round((time.time() - engine.STATE["started"]) / 3600, 2))
    return clean(dict(
        engine=eng,
        total=dict(equity=round(tot_eq, 2), start=round(tot_start, 2),
                   pnl=round(tot_eq - tot_start, 2),
                   ret=round((tot_eq / tot_start - 1) * 100, 2) if tot_start else 0),
        strategies=per))


@app.get("/api/strategy/{sid}")
def strategy(sid: str):
    if sid not in strategies.STRATS:
        raise HTTPException(404, "unknown strategy")
    mk = engine.latest_marks()
    b = engine.book_summary(sid, mk)
    return clean(dict(
        **_stats(sid, mk),
        detail=strategies.STRATS[sid]["detail"],
        params=engine.params(sid), schema=strategies.STRATS[sid]["params"],
        equity_series=_series(sid),
        positions=label_rows([{k: (round(v, 4) if isinstance(v, float) else v)
                               for k, v in p.items()} for p in b["positions"]]),
        trades=label_rows(db.q("SELECT * FROM trades WHERE strat=? ORDER BY ts DESC "
                               "LIMIT 60", (sid,))),
        signals=label_rows(db.q("SELECT * FROM signals WHERE strat=? ORDER BY ts DESC "
                                "LIMIT 60", (sid,))),
        settlements=label_rows(db.q("SELECT * FROM settlements WHERE strat=? "
                                    "ORDER BY ts DESC LIMIT 40", (sid,)))))


@app.get("/api/ribbon")
def ribbon():
    """Fair vs market across the buckets of the nearest live event."""
    row = db.one("SELECT event_id FROM markets WHERE closed=0 AND end_ts>? "
                 "ORDER BY end_ts LIMIT 1", (time.time(),))
    if not row:
        return dict(buckets=[])
    ms = db.q("SELECT * FROM markets WHERE event_id=? ORDER BY lo", (row["event_id"],))
    out = []
    for m in ms:
        q = db.one("SELECT bid,ask,mid,fair FROM quotes WHERE market_id=? "
                   "ORDER BY ts DESC LIMIT 1", (m["id"],))
        out.append(dict(label=m["question"], lo=m["lo"], hi=m["hi"],
                        fair=(q or {}).get("fair"), mid=(q or {}).get("mid"),
                        bid=(q or {}).get("bid"), ask=(q or {}).get("ask")))
    return clean(dict(title=ms[0]["event_title"] if ms else "", city=ms[0]["city"],
                      date=ms[0]["target_date"], unit=ms[0].get("unit") or "F",
                      buckets=out))


@app.get("/api/markets")
def markets():
    return clean(db.q("""
        SELECT m.*,
          (SELECT bid  FROM quotes q WHERE q.market_id=m.id ORDER BY q.ts DESC LIMIT 1) bid,
          (SELECT ask  FROM quotes q WHERE q.market_id=m.id ORDER BY q.ts DESC LIMIT 1) ask,
          (SELECT fair FROM quotes q WHERE q.market_id=m.id ORDER BY q.ts DESC LIMIT 1) fair
        FROM markets m WHERE m.closed=0 ORDER BY m.end_ts LIMIT 300"""))


@app.get("/api/discovery")
def discovery():
    """Every weather event the scanner has seen and what it did with it. If a
    market you expected is missing, the reason is in here."""
    rows = db.q("SELECT * FROM discovery ORDER BY accepted DESC, ts DESC LIMIT 200")
    return clean(dict(accepted=[r for r in rows if r["accepted"]],
                      skipped=[r for r in rows if not r["accepted"]],
                      cities=sorted(config.CITIES),
                      keywords=config.MARKET_KEYWORDS))


@app.get("/api/health/calibration")
def calibration():
    """Is the model disagreeing with the market in many places, or in one place
    many times? The second is a single bet wearing a diversified costume."""
    rows = db.q("""SELECT q.market_id, q.bid, q.ask, q.fair, m.event_id
                   FROM quotes q JOIN markets m ON m.id=q.market_id
                   WHERE q.fair IS NOT NULL AND q.ts > ?""", (time.time() - 900,))
    ev = {}
    for r in rows:
        ev.setdefault(r["event_id"], {})[r["market_id"]] = r
    ranks, m_conc, k_conc = {}, [], []
    for group in ev.values():
        rs = sorted(group.values(),
                    key=lambda r: -((r["bid"] + r["ask"]) / 2))
        if len(rs) < 6:
            continue
        f = [r["fair"] for r in rs]
        p = [(r["bid"] + r["ask"]) / 2 for r in rs]
        tp = sum(p) or 1
        p = [x / tp for x in p]
        m_conc.append(1 / sum(x * x for x in f if x) if any(f) else None)
        k_conc.append(1 / sum(x * x for x in p if x))
        for i, r in enumerate(rs[:4]):
            ranks.setdefault(i, []).append(r["fair"] - p[i])
    m_conc = [x for x in m_conc if x]
    avg = lambda v: round(sum(v) / len(v), 3) if v else None
    top = avg(ranks.get(0, []))
    blocked = db.q("SELECT ts,msg FROM logs WHERE msg LIKE '%from the market%' "
                   "ORDER BY id DESC LIMIT 8")
    return dict(
        events=len(m_conc),
        quarantined=[b["msg"] for b in blocked],
        model_spread_buckets=avg(m_conc), market_spread_buckets=avg(k_conc),
        model_minus_market_by_rank={f"rank_{i+1}": avg(v) for i, v in ranks.items()},
        verdict=("not enough data" if not top else
                 "model systematically under-prices the favourite - one bet repeated"
                 if top < -0.10 else
                 "model systematically over-prices the favourite - one bet repeated"
                 if top > 0.10 else "disagreements look event-specific"))


@app.get("/api/logs")
def logs(limit: int = 120, q: str = "", level: str = ""):
    """?q=gamma to filter, ?level=error for problems only."""
    sql = "SELECT * FROM logs WHERE 1=1"
    args = []
    if q:
        sql += " AND msg LIKE ?"
        args.append(f"%{q}%")
    if level:
        sql += " AND level=?"
        args.append(level)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(1000, limit)))
    return db.q(sql, tuple(args))


@app.get("/api/health/feeds")
def feeds():
    """One call that answers 'is it actually working'."""
    def latest(pattern):
        r = db.one("SELECT ts,msg FROM logs WHERE msg LIKE ? ORDER BY id DESC LIMIT 1",
                   (f"%{pattern}%",))
        return dict(msg=r["msg"], age_min=round((time.time() - r["ts"]) / 60, 1)) \
            if r else None
    open_mk = db.one("SELECT COUNT(*) c FROM markets WHERE closed=0")["c"]
    cities = db.q("SELECT city, COUNT(*) n FROM markets WHERE closed=0 "
                  "GROUP BY city ORDER BY n DESC")
    priced = db.one("SELECT COUNT(DISTINCT market_id) c FROM quotes WHERE ts>? "
                    "AND fair IS NOT NULL", (time.time() - 900,))["c"]
    quoted = db.one("SELECT COUNT(DISTINCT market_id) c FROM quotes WHERE ts>?",
                    (time.time() - 900,))["c"]
    return dict(
        version=config.VERSION,
        discovery=latest("gamma:"), book_sources=latest("book sources"),
        forecast=latest("forecast refresh"),
        errors=db.q("SELECT ts,strat,msg FROM logs WHERE level='error' "
                    "ORDER BY id DESC LIMIT 5"),
        open_markets=open_mk, quoted_last_15m=quoted, priced_last_15m=priced,
        cities_trading={c["city"]: c["n"] for c in cities},
        unconfigured=sorted({r["title"] for r in db.q(
            "SELECT title FROM discovery WHERE accepted=0 AND reason LIKE '%not in the%'"
        )})[:10],
        resting_quotes=db.one("SELECT COUNT(*) c FROM quotes_open")["c"],
        verdict=("no markets" if not open_mk else
                 "no quotes" if not quoted else
                 "quotes but no forecast prices" if not priced else "ok"))


@app.get("/api/health/settlement")
def settlement_health():
    """Is every booked P&L traceable to Polymarket's own resolution?

    This is the check that would have caught the last run's headline number. The
    reported +70.6% was 38 settlements scored against a temperature reading
    instead of the resolution, and nothing on the dashboard distinguished them.
    Now the source is recorded per row and shown here: anything outside
    winner_src='gamma' is not a real result.
    """
    src = {r["winner_src"] or "unknown": r["n"] for r in db.q(
        "SELECT winner_src, COUNT(*) n FROM settlements GROUP BY winner_src")}
    pnl = {r["winner_src"] or "unknown": round(r["p"], 2) for r in db.q(
        "SELECT winner_src, COALESCE(SUM(pnl),0) p FROM settlements "
        "GROUP BY winner_src")}
    mk = {r["winner_src"] or "unknown": r["n"] for r in db.q(
        "SELECT winner_src, COUNT(*) n FROM markets WHERE closed=1 GROUP BY winner_src")}
    pending = db.one("SELECT COUNT(*) c FROM markets WHERE closed=1 "
                     "AND winner IS NULL AND COALESCE(winner_src,'')<>'stale'")["c"]
    stale = db.one("SELECT COUNT(*) c FROM markets WHERE winner_src='stale'")["c"]
    stranded = db.q("""SELECT p.strat, COUNT(*) n, ROUND(SUM(p.qty*p.avg_price),2) cost
                       FROM positions p JOIN markets m ON m.id=p.market_id
                       WHERE m.winner_src='stale' AND ABS(p.qty)>1e-6
                       GROUP BY p.strat""")
    total = sum(src.values())
    trusted = src.get("gamma", 0) + src.get("gamma-repair", 0)
    return dict(
        settlements_by_source=src, pnl_by_source=pnl, closed_markets_by_source=mk,
        settled_total=total, settled_from_gamma=trusted,
        closed_awaiting_resolution=pending, retired_unresolved=stale,
        stranded_positions=stranded,
        resolutions_recorded=db.one("SELECT COUNT(*) c FROM resolutions")["c"],
        mark_ticks=db.one("SELECT COUNT(*) c FROM mark_ticks")["c"],
        verdict=("nothing settled yet" if not total else
                 "every settlement came from Polymarket's resolution"
                 if trusted == total else
                 f"{total - trusted} of {total} settlements were NOT scored on "
                 f"Polymarket's resolution -- treat their P&L as unverified"))


def _exit_value(row, outcome):
    """What one share would actually fetch if sold into the book right now."""
    if outcome == "YES":
        p = row["bid"]
    else:
        p = None if row["ask"] is None else 1.0 - row["ask"]
    if p is None:
        return None
    return p - model.fee_per_share(max(0.0, min(1.0, p)))


@app.get("/api/analysis/cashout")
def cashout(take: float = 0.85, stop: float = 0.15, limit: int = 400):
    """Would selling early have beaten holding to settlement?

    Replays each settled position over its own mark_ticks track and compares
    three outcomes: what actually happened, what a fixed take-profit/stop-loss
    rule would have returned, and the best exit the book ever offered. The last
    is an upper bound nobody could hit in practice -- it is here to size the
    prize, because if perfect timing is only worth a few dollars there is no
    cash-out strategy to find and the honest answer is to stop looking.

    Exit prices are the bid (or 1-ask for a NO leg) net of the taker fee, since
    cashing out means crossing the spread and paying to do it.
    """
    take = max(0.5, min(0.99, take))
    stop = max(0.01, min(0.5, stop))
    rows = db.q("SELECT rowid,* FROM settlements ORDER BY ts DESC LIMIT ?",
                (max(1, min(2000, limit)),))
    out, no_track = [], 0
    for s in rows:
        entry = db.one("SELECT MIN(ts) t FROM trades WHERE strat=? AND market_id=? "
                       "AND outcome=?", (s["strat"], s["market_id"], s["outcome"]))
        t0 = (entry or {}).get("t") or 0
        track = db.q("SELECT ts,bid,ask,mid FROM mark_ticks WHERE market_id=? "
                     "AND ts BETWEEN ? AND ? ORDER BY ts",
                     (s["market_id"], t0, s["ts"]))
        if not track:
            no_track += 1
            continue
        cost = s["qty"] * s["avg_price"]
        best, best_ts, ruled, rule_ts, rule_why = None, None, None, None, "held"
        for r in track:
            v = _exit_value(r, s["outcome"])
            if v is None:
                continue
            if best is None or v > best:
                best, best_ts = v, r["ts"]
            if ruled is None:
                # the rule reads the mark it would actually have seen, in order
                raw = r["bid"] if s["outcome"] == "YES" else (
                    None if r["ask"] is None else 1.0 - r["ask"])
                if raw is not None and (raw >= take or raw <= stop):
                    ruled, rule_ts = v, r["ts"]
                    rule_why = "take" if raw >= take else "stop"
        out.append(dict(
            strat=s["strat"], market_id=s["market_id"], question=s["question"],
            outcome=s["outcome"], qty=round(s["qty"], 2),
            avg_price=round(s["avg_price"], 4), ticks=len(track),
            actual_pnl=round(s["pnl"] or 0.0, 2),
            rule_pnl=round((ruled * s["qty"] - cost) if ruled is not None
                           else (s["pnl"] or 0.0), 2),
            rule_action=rule_why,
            rule_ts=rule_ts, best_exit_pnl=round(best * s["qty"] - cost, 2)
            if best is not None else None, best_exit_ts=best_ts,
            best_exit_price=round(best, 4) if best is not None else None))
    by = {}
    for r in out:
        b = by.setdefault(r["strat"], dict(n=0, actual=0.0, rule=0.0, best=0.0,
                                           exits=0))
        b["n"] += 1
        b["actual"] += r["actual_pnl"]
        b["rule"] += r["rule_pnl"]
        b["best"] += r["best_exit_pnl"] or r["actual_pnl"]
        b["exits"] += 1 if r["rule_action"] != "held" else 0
    for b in by.values():
        for k in ("actual", "rule", "best"):
            b[k] = round(b[k], 2)
        b["rule_gain"] = round(b["rule"] - b["actual"], 2)
    tot = dict(actual=round(sum(b["actual"] for b in by.values()), 2),
               rule=round(sum(b["rule"] for b in by.values()), 2),
               best=round(sum(b["best"] for b in by.values()), 2))
    tot["rule_gain"] = round(tot["rule"] - tot["actual"], 2)
    tot["perfect_timing_gain"] = round(tot["best"] - tot["actual"], 2)
    return clean(dict(
        rule=dict(take_profit=take, stop_loss=stop),
        settlements_examined=len(rows), without_price_track=no_track,
        total=tot, by_strategy=by, positions=out[:120],
        note="best_exit_* is a hindsight upper bound, not an achievable strategy. "
             "If perfect_timing_gain is small there is no cash-out edge to chase. "
             "Positions with no price track predate mark_ticks or were never held "
             "across a tick."))


# ------------------------------------------------------------------ controls
@app.post("/api/strategy/{sid}/params")
def set_params(sid: str, body: dict = Body(...)):
    if sid not in strategies.STRATS:
        raise HTTPException(404, "unknown strategy")
    schema = strategies.STRATS[sid]["params"]
    clean = {}
    for k, v in body.items():
        if k not in schema:
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        clean[k] = max(schema[k]["min"], min(schema[k]["max"], val))
    db.cfg_set(f"params:{sid}", clean)
    db.log("info", f"params updated: {clean}", sid)
    return dict(saved=clean)


@app.post("/api/strategy/{sid}/toggle")
def toggle(sid: str):
    new = not engine.enabled(sid)
    db.cfg_set(f"enabled:{sid}", new)
    db.log("info", f"{'enabled' if new else 'paused'}", sid)
    return dict(enabled=new)


@app.post("/api/strategy/{sid}/flatten")
def flatten(sid: str):
    """Sell every open position at the current bid."""
    n = 0
    for p in engine.book_summary(sid)["positions"]:
        m = db.one("SELECT * FROM markets WHERE id=?", (p["market_id"],))
        if not m or p["qty"] <= 0:
            continue
        q = db.one("SELECT bid,ask FROM quotes WHERE market_id=? ORDER BY ts DESC "
                   "LIMIT 1", (p["market_id"],)) or {"bid": p["avg_price"], "ask": 1}
        price = q["bid"] if p["outcome"] == "YES" else 1 - q["ask"]
        pmclient.place(sid, m, "SELL", p["outcome"], max(0.01, price), p["qty"],
                       None, "manual flatten")
        n += 1
    db.log("warn", f"flattened {n} positions by hand", sid)
    return dict(closed=n)


@app.post("/api/strategy/{sid}/reset")
def reset(sid: str):
    """Wipe this strategy's book back to its starting allocation. Paper only."""
    if config.LIVE_TRADING:
        raise HTTPException(400, "reset is disabled while live trading is on")
    for t in ("trades", "positions", "settlements", "signals", "equity"):
        db.ex(f"DELETE FROM {t} WHERE strat=?", (sid,))
    db.log("warn", "book reset to starting allocation", sid)
    return dict(reset=True)


@app.post("/api/halt")
def halt():
    # set_halted, not a STATE poke: a hand halt has to survive the next restart,
    # or it lasts only until Railway redeploys.
    engine.set_halted(True, "stopped by hand")
    db.log("warn", "trading halted by hand")
    return dict(halted=True)


@app.post("/api/resume")
def resume():
    engine.set_halted(False)
    mk = engine.latest_marks()
    db.cfg_set("day_open_equity",
               sum(engine.book_summary(s, mk)["equity"] for s in strategies.ORDER))
    db.log("info", "trading resumed, loss limit rebased")
    return dict(halted=False)


@app.get("/api/export.zip")
def export():
    data = db.export_zip()
    stamp = time.strftime("%Y%m%d-%H%M")
    return Response(data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="wxalpha-{stamp}.zip"'})
