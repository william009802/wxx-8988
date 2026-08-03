"""Ensemble forecast retrieval.

Two independent reads per city:
  1. Ensemble members (ECMWF-IFS / GFS / ICON perturbations) -> the probability
     distribution the pricing model consumes.
  2. Deterministic runs of five separate models -> a cross-model spread number,
     which is what strategy 2 gates on. Ensemble spread and model spread
     disagree often, and that disagreement is itself information.
"""
import asyncio
import statistics
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db

_cache = {}   # city -> {"ts":, "days": {date: {...}}}
_fail = {}    # city -> why the last refresh failed
_obs = {}     # city -> {"date":, "high":, "hour":, "ts":}


def _by_day(times, values):
    out = {}
    for t, v in zip(times, values):
        if v is None:
            continue
        out.setdefault(t[:10], []).append(v)
    return {d: max(vs) for d, vs in out.items() if vs}


def _members_from(payload):
    """Open-Meteo returns one object per model, each with member columns."""
    blocks = payload if isinstance(payload, list) else [payload]
    per_day = {}
    for blk in blocks:
        h = blk.get("hourly") or {}
        times = h.get("time") or []
        for key, vals in h.items():
            if not key.startswith("temperature_2m"):
                continue
            for d, mx in _by_day(times, vals).items():
                per_day.setdefault(d, []).append(mx)
    return per_day


async def fetch_city(client: httpx.AsyncClient, key: str):
    """One city's forecast, ensemble first with a deterministic fallback.

    The ensemble endpoint is the expensive one: three models over seven days of
    hourly data is ~120 members per city, and firing sixteen of those at once
    gets most of them throttled. So the window is short, and when the ensemble
    fails we build a small ensemble out of the five deterministic model runs
    instead. Five members is a coarser distribution than 120, but a coarse
    forecast prices a market and a missing one does not.
    """
    c = config.CITIES[key]
    common = dict(latitude=c["lat"], longitude=c["lon"], timezone=c["tz"],
                  temperature_unit="fahrenheit", forecast_days=config.FORECAST_DAYS)

    ens_task = client.get(config.ENSEMBLE_URL, params={
        **common, "hourly": "temperature_2m", "models": config.ENSEMBLE_MODELS})
    det_task = client.get(config.FORECAST_URL, params={
        **common, "daily": "temperature_2m_max", "models": config.DETERMINISTIC_MODELS})

    ens_r, det_r = await asyncio.gather(ens_task, det_task, return_exceptions=True)

    if isinstance(ens_r, Exception):
        _fail[key] = f"ensemble: {type(ens_r).__name__}"
    elif ens_r.status_code != 200:
        _fail[key] = f"ensemble http {ens_r.status_code}"

    days = {}
    if not isinstance(ens_r, Exception) and ens_r.status_code == 200:
        for d, members in _members_from(ens_r.json()).items():
            if len(members) < 3:
                continue
            adj = [m + c["bias_f"] for m in members]
            days[d] = dict(members=adj, mean=statistics.fmean(adj),
                           sd=statistics.pstdev(adj) if len(adj) > 1 else 1.5,
                           n=len(adj), model_spread=None)

    # cross-model spread, and the fallback distribution when the ensemble failed
    det = {}
    if not isinstance(det_r, Exception) and det_r.status_code == 200:
        blocks = det_r.json()
        blocks = blocks if isinstance(blocks, list) else [blocks]
        for blk in blocks:
            dd = blk.get("daily") or {}
            for t, v in zip(dd.get("time", []), dd.get("temperature_2m_max", [])):
                if v is not None:
                    det.setdefault(t, []).append(v + c["bias_f"])
    elif not isinstance(det_r, Exception):
        _fail[key] = _fail.get(key, "") + f" | deterministic http {det_r.status_code}"

    for d, vals in det.items():
        spread = statistics.pstdev(vals) if len(vals) > 1 else None
        if d in days:
            days[d]["model_spread"] = spread
            days[d]["det_mean"] = statistics.fmean(vals)
        elif len(vals) >= 2:
            # ensemble unavailable: treat the individual model runs as members
            days[d] = dict(members=vals, mean=statistics.fmean(vals),
                           sd=max(1.0, spread or 1.5), n=len(vals),
                           model_spread=spread, source="deterministic")

    if days:
        _fail.pop(key, None)

    if days:
        _cache[key] = {"ts": time.time(), "days": days}
        ts = time.time()
        with db.tx() as conn:
            for d, o in days.items():
                # members_json is by far the largest thing this bot writes: ~120
                # members per city-day, every refresh, for a week of forecast
                # days. Open-Meteo only publishes a new ensemble every few hours,
                # so most of those rows are byte-identical to the one before and
                # cost free-tier volume for nothing. An unchanged forecast is
                # skipped; the previous row already says what the model thought
                # at that time, and nothing in the calibration analysis counts
                # how many times we asked.
                prev = conn.execute(
                    "SELECT mean_f, sd_f, members FROM forecasts WHERE city=? AND "
                    "target_date=? ORDER BY ts DESC LIMIT 1", (key, d)).fetchone()
                if (prev and prev["members"] == o["n"]
                        and abs((prev["mean_f"] or 0) - o["mean"]) < 0.01
                        and abs((prev["sd_f"] or 0) - o["sd"]) < 0.01):
                    continue
                conn.execute(
                    "INSERT INTO forecasts(ts,city,target_date,mean_f,sd_f,members,"
                    "model_spread,members_json) VALUES(?,?,?,?,?,?,?,?)",
                    (ts, key, d, o["mean"], o["sd"], o["n"], o["model_spread"],
                     ",".join(f"{m:.1f}" for m in o["members"])))
    return key, bool(days)


async def fetch_observed(client: httpx.AsyncClient, key: str):
    """Highest temperature recorded so far today, in the city's local day.

    This is the hard floor under a same-day market: whatever else happens, the
    settled high cannot come in below what has already been measured. Buckets
    underneath it are worth zero, and the book is usually slow to notice.

    Open-Meteo backfills past hours with analysis rather than the raw station
    report, so treat it as close-but-not-exact and keep a safety margin.
    """
    c = config.CITIES[key]
    try:
        r = await client.get(config.FORECAST_URL, params=dict(
            latitude=c["lat"], longitude=c["lon"], timezone=c["tz"],
            temperature_unit="fahrenheit", past_days=1, forecast_days=1,
            hourly="temperature_2m"))
        if r.status_code != 200:
            return
        j = r.json()
    except (httpx.HTTPError, ValueError):
        return
    off = j.get("utc_offset_seconds", 0)
    local = datetime.fromtimestamp(time.time() + off, tz=timezone.utc)
    today = local.strftime("%Y-%m-%d")
    h = j.get("hourly") or {}
    seen = [v for t, v in zip(h.get("time", []), h.get("temperature_2m", []))
            if v is not None and t.startswith(today) and int(t[11:13]) <= local.hour]
    if not seen:
        return
    _obs[key] = dict(date=today, high=max(seen) + c["bias_f"],
                     hour=local.hour, ts=time.time())


def observed(city: str):
    o = _obs.get(city)
    if not o:
        return None
    return dict(o, age_min=(time.time() - o["ts"]) / 60)


async def refresh(cities):
    res = []
    async with httpx.AsyncClient(timeout=30,
                                 headers={"User-Agent": config.USER_AGENT}) as cl:
        # small batches with a pause: sixteen simultaneous ensemble requests is a
        # burst the free tier rejects, and a rejected city cannot be priced at all
        for i in range(0, len(cities), config.WX_BATCH):
            batch = cities[i:i + config.WX_BATCH]
            res += await asyncio.gather(*(fetch_city(cl, c) for c in batch),
                                        return_exceptions=True)
            if i + config.WX_BATCH < len(cities):
                await asyncio.sleep(config.WX_PAUSE)
        for i in range(0, len(cities), config.WX_BATCH):
            await asyncio.gather(*(fetch_observed(cl, c) for c in
                                   cities[i:i + config.WX_BATCH]),
                                 return_exceptions=True)
    ok = sum(1 for r in res if isinstance(r, tuple) and r[1])
    src = {}
    for c in cities:
        blk = _cache.get(c)
        if blk:
            for d in blk["days"].values():
                k = d.get("source", "ensemble")
                src[k] = src.get(k, 0) + 1
                break
    db.log("info", f"forecast refresh: {ok}/{len(cities)} cities {src}, "
                   f"{len(_obs)} with live observations")
    if ok < len(cities):
        missing = [c for c in cities if c not in _cache]
        for c in missing[:6]:
            db.log("warn", f"no forecast for {c}: {_fail.get(c, 'unknown')}")
    return ok


def get(city: str, target_date: str):
    """Latest forecast for a city/date, or None."""
    blk = _cache.get(city)
    if blk and target_date in blk["days"]:
        o = dict(blk["days"][target_date])
        o["age_min"] = (time.time() - blk["ts"]) / 60
        return o
    row = db.one("SELECT * FROM forecasts WHERE city=? AND target_date=? "
                 "ORDER BY ts DESC LIMIT 1", (city, target_date))
    if not row:
        return None
    members = [float(x) for x in (row["members_json"] or "").split(",") if x]
    if not members:
        return None
    return dict(members=members, mean=row["mean_f"], sd=row["sd_f"], n=row["members"],
                model_spread=row["model_spread"], age_min=(time.time() - row["ts"]) / 60)


def previous(city: str, target_date: str, min_age_s=3000):
    """An earlier forecast for the same date, for revision/drift detection."""
    row = db.one("SELECT mean_f FROM forecasts WHERE city=? AND target_date=? AND ts<? "
                 "ORDER BY ts DESC LIMIT 1",
                 (city, target_date, time.time() - min_age_s))
    return row["mean_f"] if row else None


_actuals = {}


async def actual_high(city: str, date: str):
    """The daily maximum actually recorded, for a day that has finished.

    This is a *calibration* number, not a settlement number. It is the recorded
    high at the forecast gridpoint, which is not the station Polymarket resolves
    on, and it is a different Open-Meteo endpoint from fetch_observed, which
    disagrees with it by 1-3 F. Settlement comes from Gamma alone. What this is
    for is measuring the gridpoint-to-station bias per city, which is why it is
    cached permanently: once published it never changes.
    """
    key = (city, date)
    if key in _actuals:
        return _actuals[key]
    row = db.one("SELECT high_f FROM settled_highs WHERE city=? AND target_date=?",
                 (city, date))
    if row:
        _actuals[key] = row["high_f"]
        return row["high_f"]

    c = config.CITIES.get(city)
    if not c:
        return None
    try:
        async with httpx.AsyncClient(timeout=20,
                                     headers={"User-Agent": config.USER_AGENT}) as cl:
            r = await cl.get(config.FORECAST_URL, params=dict(
                latitude=c["lat"], longitude=c["lon"], timezone=c["tz"],
                temperature_unit="fahrenheit", daily="temperature_2m_max",
                past_days=7, forecast_days=1))
            if r.status_code != 200:
                return None
            d = (r.json() or {}).get("daily") or {}
            times, vals = d.get("time") or [], d.get("temperature_2m_max") or []
            if date not in times:
                return None
            v = vals[times.index(date)]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        db.log("warn", f"actual high {city} {date}: {e}")
        return None
    if v is None:
        return None
    v = float(v) + c["bias_f"]
    _actuals[key] = v
    db.ex("INSERT OR REPLACE INTO settled_highs(city,target_date,high_f,ts) "
          "VALUES(?,?,?,?)", (city, date, v, time.time()))
    return v


def upcoming_dates(city, n=5):
    tz_now = datetime.utcnow()
    return [(tz_now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
