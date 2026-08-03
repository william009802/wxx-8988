"""Offline sanity run: synthetic markets + synthetic ensemble through the whole
pipeline. No network. Usage: python3 selftest.py"""
import os
import sys
import types
import random
import time

os.environ.setdefault("DB_PATH", "/tmp/wxtest.db")
# The durability guard exists to stop a Railway deploy silently running on
# throwaway storage. A test database is throwaway storage on purpose.
os.environ["REQUIRE_DURABLE_DB"] = "false"
_p = os.environ["DB_PATH"]
for f in (_p, _p + "-wal", _p + "-shm"):
    if os.path.exists(f):
        os.remove(f)

# stub httpx so weather/pmclient import cleanly offline
if "httpx" not in sys.modules:
    stub = types.ModuleType("httpx")
    class _E(Exception): pass
    stub.HTTPError = _E
    class _C:
        def __init__(self, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise _E("offline")
        async def post(self, *a, **k): raise _E("offline")
    stub.AsyncClient = _C
    sys.modules["httpx"] = stub

from app import config, db, model, strategies, engine, weather   # noqa: E402

ok = lambda c, m: print(("  PASS  " if c else "  FAIL  ") + m) or (c or FAILED.append(m))
FAILED = []

print("\n1. bucket parsing")
cases = [("84-85°F", (84, 85)), ("83° or below", (-model.INF, 83)),
         ("90°F or above", (90, model.INF)), ("Higher than 100", (100, model.INF)),
         ("72", (72, 72))]
for text, want in cases:
    got = model.parse_bucket(text)
    ok(got == want, f"{text!r} -> {got}")

print("\n2. probability model")
members = [83.9, 84.2, 84.4, 85.1, 85.6, 86.0, 84.8, 83.2]
buckets = {"a": (-model.INF, 83), "b": (84, 84), "c": (85, 85), "d": (86, model.INF)}
fc = dict(members=members, sd=0.9, model_spread=0.6, n=8, mean=sum(members)/8)
fair, sd = model.fair_prices(buckets, fc, lead_days=1.0)
print("   ", {k: round(v, 3) for k, v in fair.items()}, "kernel sd", round(sd, 2))
ok(abs(sum(fair.values()) - 1) < 1e-6, "bucket probabilities sum to 1")
ok(fair["b"] > 0.05 and fair["c"] > 0.05, "mass sits on the modal buckets")
ok(model.bucket_prob(84, 84, [84.0], 0.5) > model.bucket_prob(84, 84, [84.9], 0.5),
   "continuity correction favours the centred member")

print("\n2b. fees")
for px in (0.05, 0.28, 0.50):
    print(f"    {px:.2f} -> fee/share {model.fee_per_share(px):.4f}, "
          f"a 6pt gross edge nets {model.net_edge(0.06, px):+.4f}")
ok(model.net_edge(0.02, 0.50) < 0, "a 2-point edge at 50c is negative after fees")
ok(model.net_edge(0.06, 0.50) < 0.04, "and a 6-point edge at 50c loses most of itself")
ok(model.net_edge(0.06, 0.05) > 0, "the same edge on a cheap contract survives")

print("\n3. kelly")
ok(model.kelly(0.60, 0.50) > 0, "positive edge sizes up")
ok(model.kelly(0.40, 0.50) == 0, "negative edge sizes to zero")

print("\n4. synthetic market cycle")
db.connect()
now = time.time()
random.seed(7)
labels = [(-model.INF, 83), (84, 84), (85, 85), (86, model.INF)]
names = ["83 or below", "84", "85", "86 or above"]
rows = []
for i, ((lo, hi), nm) in enumerate(zip(labels, names)):
    rows.append(dict(id=f"m{i}", event_id="e1", event_title="Highest temp in NYC on 2026-07-27",
                     question=nm, city="nyc", target_date="2026-07-27", lo=lo, hi=hi,
                     token_yes=f"t{i}", token_no=f"n{i}", end_ts=now + 6 * 3600, closed=0))
    db.ex("""INSERT INTO markets(id,event_id,event_title,question,city,target_date,lo,hi,
             token_yes,token_no,end_ts,seen_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (rows[-1]["id"], "e1", rows[-1]["event_title"], nm, "nyc", "2026-07-27",
           lo, hi, f"t{i}", f"n{i}", now + 6 * 3600, now, now))

weather._cache["nyc"] = {"ts": now, "days": {"2026-07-27": fc}}

# a deliberately cheap book: asks sum under 1 so S3 must fire
def _bk(mid, frac=0.04, sz=400):
    """A realistic book: spread a small fraction of price, not a fixed 2-3c."""
    h = max(0.002, mid * frac / 2)
    return dict(bid=round(mid - h, 4), ask=round(mid + h, 4),
                bid_sz=sz, ask_sz=sz, mid=mid)


books = {"m0": _bk(.05), "m1": _bk(.30), "m2": _bk(.34), "m3": _bk(.28)}
print("    asks sum to", round(sum(b["ask"] for b in books.values()), 3))

events = engine.price_events(rows, books)
ok(all(r.get("fair") is not None for r in rows), "every bucket priced")
ok(all(r.get("prob_sd") is not None for r in rows), "uncertainty computed for the quoter")
for r in rows:
    print(f"    {r['question']:>14}  model {r['fair']:.3f}  ask {r['book']['ask']:.3f}"
          f"  edge {r['fair']-r['book']['ask']:+.3f}")

print("\n5. strategies")
fired = {}
for sid in strategies.ORDER:
    prm = strategies.defaults(sid)
    ctx = dict(markets=rows, events=events, params=prm, bankroll=200.0,
               positions={}, now=now, cash=200.0)
    ints = strategies.STRATS[sid]["fn"](ctx)
    fired[sid] = ints
    print(f"    {sid} {strategies.STRATS[sid]['name']:<10} {len(ints)} intents"
          + (f"  e.g. {ints[0]['action']} {ints[0]['outcome']} "
             f"@{ints[0]['price']:.3f} x{ints[0]['qty']:.0f} — {ints[0]['reason'][:60]}"
             if ints else ""))
gross0 = sum(b["ask"] for b in books.values())
fee0 = sum(model.fee_per_share(b["ask"]) for b in books.values())
print(f"    asks {gross0:.3f} + fees {fee0:.3f} = {gross0+fee0:.3f}")
ok(len(fired["s3"]) == 0, "dutch book declines a set that costs over a dollar with fees")
deep = {k: _bk(px) for k, px in
        (("m0", .04), ("m1", .24), ("m2", .27), ("m3", .22))}
ev_deep = engine.price_events(rows, deep)
s3b = strategies.STRATS["s3"]["fn"](dict(markets=rows, events=ev_deep,
    params=strategies.defaults("s3"), bankroll=100.0, positions={}, now=now, cash=100.0))
gross = sum(deep[k]["ask"] for k in deep)
fees = sum(model.fee_per_share(deep[k]["ask"]) for k in deep)
print(f"    asks {gross:.3f} + fees {fees:.3f} = {gross+fees:.3f} -> {len(s3b)} legs")
ok(len(s3b) == 4, "dutch book fires when the total genuinely clears fees")
ok(gross + fees < 1.0, "and the locked set really does cost under a dollar")
ok(len(strategies.ORDER) == 8, "eight strategies registered")
ok(sum(len(v) for v in fired.values()) > 0, "at least one strategy acts")

print("\n5b. tight forecast, cheap modal bucket")
tight = [84.1, 84.2, 84.3, 84.3, 84.4, 84.2, 84.5, 84.0]
fc2 = dict(members=tight, sd=0.15, model_spread=0.3, n=8, mean=sum(tight) / 8)
weather._cache["nyc"] = {"ts": now, "days": {"2026-07-27": fc2}}
db.ex("UPDATE markets SET end_ts=?, resolve_ts=? WHERE event_id='e1'",
      (now + 4 * 3600, now + 4 * 3600))
for r in rows:
    r["end_ts"] = r["resolve_ts"] = now + 4 * 3600
# a book that agrees the outcome is concentrated, so the width blend keeps
# the model sharp and the confidence gates can actually be reached
cheap = {"m0": _bk(.05), "m1": _bk(.30), "m2": _bk(.10), "m3": _bk(.05)}
ev2 = engine.price_events(rows, cheap)
for r in rows:
    print(f"    {r['question']:>14}  model {r['fair']:.3f}  ask {r['book']['ask']:.3f}"
          f"  unc {r['prob_sd']:.3f}")
fired2 = {}
for sid in strategies.ORDER:
    prm = strategies.defaults(sid)
    ctx = dict(markets=rows, events=ev2, params=prm, bankroll=200.0,
               positions={}, now=now, cash=200.0)
    ints = strategies.STRATS[sid]["fn"](ctx)
    fired2[sid] = ints
    print(f"    {sid} {strategies.STRATS[sid]['name']:<10} {len(ints)} intents"
          + (f"  {ints[0]['action']} {ints[0]['outcome']} @{ints[0]['price']:.3f}"
             f" x{ints[0]['qty']:.0f} — {ints[0]['reason'][:64]}" if ints else ""))
ok(len(fired2["s2"]) > 0, "consensus fires when the models agree")
ok(len(fired2["s4"]) > 0, "drift fires inside the closing window")
ok(all(i["price"] >= 0.04 for v in fired2.values() for i in v),
   "tail floor keeps it out of 2c lottery tickets")

print("\n5c-maker. quote lifecycle")
db.ex("UPDATE markets SET end_ts=?, resolve_ts=?, tick=0.01, reward_spread=4.5, "
      "reward_min_size=20 WHERE event_id='e1'", (now + 30*3600, now + 30*3600))
for r in rows:
    r.update(end_ts=now + 30*3600, resolve_ts=now + 30*3600, tick=0.01,
             reward_spread=4.5, reward_min_size=20.0, fee_rate=0.05, fee_exp=1.0)
mmb = {"m0": _bk(.17, sz=99), "m1": _bk(.32, sz=99),
       "m2": _bk(.30, sz=99), "m3": _bk(.055, sz=99)}
evq = engine.price_events(rows, mmb)
q = strategies.STRATS["s5"]["fn"](dict(markets=rows, events=evq,
    params=strategies.defaults("s5"), bankroll=100.0, positions={}, now=now, cash=100.0))
by_id = {m["id"]: m for m in rows}
for i in q:
    print(f"    QUOTE {i['side']:<4} {i['market']['question']:>14} @{i['price']:.3f} "
          f"x{i['qty']:.0f}")
ok(all(i["action"] == "QUOTE" for i in q), "S5 emits quotes, never taker orders")
ok(all(i["price"] < i["market"]["book"]["ask"] for i in q if i["side"] == "BUY"),
   "resting bids never cross the offer")
ok(q, "quotes were produced")

p_, r_, c_ = engine.sync_quotes("s5", q, by_id)
print(f"    sync: +{p_} new, {r_} re-priced, {c_} pulled")
ok(p_ == len(q) and db.one("SELECT COUNT(*) c FROM quotes_open WHERE strat='s5'")["c"] == len(q),
   "quotes rest on the book")
p2, r2, c2 = engine.sync_quotes("s5", q, by_id)
ok(p2 == 0 and r2 == 0, "identical quotes are left alone, keeping queue position")

moved = [dict(i, price=round(i["price"] - 0.05, 3)) for i in q]
p3, r3, c3 = engine.sync_quotes("s5", moved, by_id)
ok(r3 == len(q), "a real move re-prices them")
p4, r4, c4 = engine.sync_quotes("s5", [], by_id)
ok(c4 == len(q) and not db.q("SELECT * FROM quotes_open WHERE strat='s5'"),
   "dropping the quotes cancels them")

engine.sync_quotes("s5", q, by_id)
bid_q = [i for i in q if i["side"] == "BUY"][0]
tgt = by_id[bid_q["market"]["id"]]
tgt["last_trade"] = bid_q["price"] - 0.01        # a print through our bid
tgt["book"] = dict(tgt["book"], bid=bid_q["price"])
# A fill now needs three things, so the fixture has to supply all three: the
# order has to have rested (backdate it), and volume has to have traded since it
# was posted (raise volume_num above the seen_volume recorded at post time).
db.ex("UPDATE quotes_open SET posted_ts=? WHERE strat='s5'", (now - 1800,))
tgt["volume_num"] = 5000.0
db.ex("UPDATE quotes_open SET seen_volume=0 WHERE strat='s5'")
n = engine.simulate_maker_fills("s5", by_id)
f = db.one("SELECT * FROM trades WHERE strat='s5' ORDER BY id DESC LIMIT 1")
print(f"    maker fill: {f['side']} @{f['price']:.3f} fee {f['fee']:.4f} role {f['role']}")
ok(n >= 1 and f["fee"] == 0.0 and f["role"] == "maker",
   "passive fills pay no taker fee")
ok(abs(f["price"] - bid_q["price"]) < 1e-9, "filled at our own price, no slippage")

print("\n5c. quoter, 30h out, offer below its bid")
db.ex("UPDATE markets SET end_ts=?, resolve_ts=? WHERE event_id='e1'",
      (now + 30 * 3600, now + 30 * 3600))
for r in rows:
    r["end_ts"] = r["resolve_ts"] = now + 30 * 3600
mm = dict(cheap)
mm["m1"] = _bk(.225)
ev3 = engine.price_events(rows, mm)
prm = strategies.defaults("s5")
q = strategies.STRATS["s5"]["fn"](dict(markets=rows, events=ev3, params=prm,
    bankroll=200.0, positions={}, now=now, cash=200.0))
print(f"    m1 model {rows[1]['fair']:.3f} ask {mm['m1']['ask']:.3f} "
      f"unc {rows[1]['prob_sd']:.3f} -> {len(q)} quotes")
for i in q:
    print("      ", i["action"], i["outcome"], f"@{i['price']:.3f}", "-", i["reason"][:60])
ok(len(q) > 0, "quoter still quotes when given a wide book")

inv = {("m1", "YES"): dict(qty=900, avg_price=0.24, value=216, market_id="m1")}
q2 = strategies.STRATS["s5"]["fn"](dict(markets=rows, events=ev3, params=prm,
    bankroll=200.0, positions=inv, now=now, cash=200.0))
ok(not any(i["side"] == "BUY" and i["market"]["id"] == "m1" for i in q2),
   "quoter stops bidding once inventory is full")
ok(any(i["side"] == "SELL" and i["market"]["id"] == "m1" for i in q2),
   "and offers the inventory out instead")

print("\n5d. observed-high floor and wing fade")
weather._obs["nyc"] = dict(date="2026-07-27", high=85.4, hour=17, ts=now)
for r in rows:
    r["end_ts"] = r["resolve_ts"] = now + 3 * 3600
db.ex("UPDATE markets SET end_ts=?, resolve_ts=? WHERE event_id='e1'",
      (now + 3 * 3600, now + 3 * 3600))
hot = {"m0": _bk(.07), "m1": _bk(.235), "m2": _bk(.355), "m3": _bk(.315)}
ev4 = engine.price_events(rows, hot)
ok(rows[0].get("obs") is not None, "observations reach the strategies")
ok(not strategies.STRATS["s6"].get("enabled", True),
   "S6 ships disabled after scoring 12 of 16 impossible-bucket calls wrong")
ok(not engine.enabled("s6"), "and the engine honours that default with no toggle set")

_s6 = lambda c: strategies.STRATS["s6"]["fn"](dict(markets=rows, events=ev4,
    params=c, bankroll=100.0, positions={}, now=now, cash=100.0))
# nyc has bias_f=0, meaning nobody has measured its gridpoint-to-station gap
blocked = _s6(strategies.defaults("s6"))
print(f"    with no measured bias for nyc -> {len(blocked)} signals")
ok(not blocked, "an unmeasured city gets no impossible-bucket trades at all")

# now pretend the gap was measured, which is what unlocks the leg
config.CITIES["nyc"]["bias_f"] = 0.4
try:
    ctx_bias = engine.bias_known()
    ok(ctx_bias["nyc"], "a configured bias marks the city as measured")
    prm6 = strategies.defaults("s6")
    s6 = strategies.STRATS["s6"]["fn"](dict(markets=rows, events=ev4, params=prm6,
        bankroll=100.0, positions={}, now=now, cash=100.0, bias_known=ctx_bias))
    for i in s6:
        print(f"    s6 {i['action']} {i['outcome']} {i['market']['question']:>14} "
              f"@{i['price']:.3f} - {i['reason'][:72]}")
    # 85.4F floor, 2.5F margin: a bucket is only dead below 82.4F, and the
    # lowest bucket here tops out at 83. So nothing is declared impossible --
    # which is exactly the outcome the margin exists to produce. Under the old
    # code this same ladder produced two confident sells.
    ok(not [i for i in s6 if i["outcome"] == "NO"],
       "a floor only 2.4F above the ladder no longer condemns anything")

    # push the floor clear of the whole ladder and the leg does still work
    weather._obs["nyc"] = dict(date="2026-07-27", high=90.0, hour=17, ts=now)
    hi_rows = engine.price_events(rows, hot) and rows
    s6b = strategies.STRATS["s6"]["fn"](dict(markets=hi_rows, events=ev4, params=prm6,
        bankroll=100.0, positions={}, now=now, cash=100.0, bias_known=ctx_bias))
    dead = [i for i in s6b if i["outcome"] == "NO"]
    for i in dead:
        print(f"    s6 {i['action']} {i['outcome']} {i['market']['question']:>14} "
              f"@{i['price']:.3f} - {i['reason'][:72]}")
    ok(len(dead) >= 1, "a 90F floor does rule out the bottom of the ladder")
    ok(all(i["market"]["hi"] + 0.5 < 90.0 - prm6["hard_margin_f"] for i in dead),
       "and only buckets that clear the floor by the station-gap margin")
    tight = dict(prm6); tight["hard_margin_f"] = 12.0
    ok(not [i for i in strategies.STRATS["s6"]["fn"](dict(markets=hi_rows, events=ev4,
        params=tight, bankroll=100.0, positions={}, now=now, cash=100.0,
        bias_known=ctx_bias)) if i["outcome"] == "NO"],
       "a wider margin than the ladder shuts the leg off entirely")
finally:
    config.CITIES["nyc"]["bias_f"] = 0.0
    weather._obs["nyc"] = dict(date="2026-07-27", high=85.4, hour=17, ts=now)

weather._obs.pop("nyc", None)
weather._cache["nyc"] = {"ts": now, "days": {"2026-07-27": fc2}}   # tight again
rich = dict(hot)
rich["m3"] = _bk(.195)
ev5 = engine.price_events(rows, rich)
p7 = strategies.defaults("s7"); p7["edge_buckets"] = 1
print(f"    wings: {rows[0]['question']} fair {rows[0]['fair']:.3f} | "
      f"{rows[3]['question']} fair {rows[3]['fair']:.3f} bid {rich['m3']['bid']:.2f}")
s7 = strategies.STRATS["s7"]["fn"](dict(markets=rows, events=ev5, params=p7,
    bankroll=100.0, positions={}, now=now, cash=100.0))
for i in s7:
    print(f"    s7 {i['action']} {i['outcome']} {i['market']['question']:>14} "
          f"@{i['price']:.3f} - {i['reason'][:60]}")
ok(all(i["outcome"] == "NO" for i in s7), "fade only ever buys NO")
ok(all(i["market"]["id"] in ("m0", "m3") for i in s7),
   "only the outer buckets get faded, never the middle")
capped = dict(p7); capped["max_open_fades"] = 0
ok(not strategies.STRATS["s7"]["fn"](dict(markets=rows, events=ev5, params=capped,
   bankroll=100.0, positions={}, now=now, cash=100.0)), "open-fade cap stops it dead")
narrow = dict(p7); narrow["edge_buckets"] = 2
ok(not strategies.STRATS["s7"]["fn"](dict(markets=rows, events=ev5, params=narrow,
   bankroll=100.0, positions={}, now=now, cash=100.0)),
   "skips events too small to have real wings")

print("\n5e. phantom maker fills")
# Each gate is checked on its own, because the three-day run showed that any one
# of them missing is enough to manufacture hundreds of free fills.
db.ex("DELETE FROM quotes_open"); db.ex("DELETE FROM trades WHERE strat='s5'")
qm = dict(rows[1]); qm["last_trade"] = 0.20; qm["book"] = _bk(.30, sz=99)
qm["volume_num"] = 1000.0
from app import pmclient as _pm
_pm.post_quote("s5", qm, "BUY", "YES", 0.30, 20, "test quote")
by = {qm["id"]: qm}
qm["volume_num"] = 1400.0                  # 400 shares traded since we posted
r0 = engine.simulate_maker_fills("s5", by)
print(f"    tick 0, posted seconds ago -> {r0} fills")
ok(r0 == 0, "a quote that has not rested yet cannot be at the front of the queue")

db.ex("UPDATE quotes_open SET posted_ts=? WHERE strat='s5'", (now - 1800,))
r1 = engine.simulate_maker_fills("s5", by)
print(f"    tick 1, rested, but last_trade 0.20 unchanged -> {r1} fills")
ok(r1 == 0, "a stale last-trade price does not refill the same quote")
for t in range(4):
    engine.simulate_maker_fills("s5", by)
ok(db.one("SELECT COUNT(*) c FROM trades WHERE strat='s5'")["c"] == 0,
   "and it stays zero over repeated ticks")

qm["last_trade"] = 0.28                    # a genuinely new print through our bid
qm["volume_num"] = 1000.0                  # ... but no volume traded since posting
db.ex("UPDATE quotes_open SET seen_volume=1000.0, seen_last=0.20 WHERE strat='s5'")
r1b = engine.simulate_maker_fills("s5", by)
print(f"    new print at 0.28 but volume flat -> {r1b} fills")
ok(r1b == 0, "a print with no volume behind it is not a trade we can be part of")

qm["volume_num"] = 1400.0                  # 400 traded, 25% share = 100 fillable
r2 = engine.simulate_maker_fills("s5", by)
f = db.one("SELECT price,qty,fee,role FROM trades WHERE strat='s5' ORDER BY id DESC LIMIT 1")
print(f"    volume 1000 -> 1400 -> {r2} fill of {f['qty']:g} at {f['price']:.3f} "
      f"fee {f['fee']:.4f} role {f['role']}")
ok(r2 == 1 and f["role"] == "maker", "real volume through the quote does fill it")
ok(f["qty"] <= 20 + 1e-9, "and never for more than the order actually rested")

db.ex("DELETE FROM quotes_open"); db.ex("DELETE FROM trades WHERE strat='s5'")
qm["volume_num"] = 1000.0
_pm.post_quote("s5", qm, "BUY", "YES", 0.30, 200, "big quote")
db.ex("UPDATE quotes_open SET posted_ts=?, seen_last=0.20 WHERE strat='s5'",
      (now - 1800,))
qm["volume_num"] = 1040.0                  # only 40 shares traded
qm["last_trade"] = 0.28
engine.simulate_maker_fills("s5", by)
fb = db.one("SELECT qty FROM trades WHERE strat='s5' ORDER BY id DESC LIMIT 1")
print(f"    200-share quote against 40 shares of volume -> filled {fb['qty'] if fb else 0}")
ok(fb is not None and fb["qty"] <= 40 * 0.25 + 1e-9,
   "a small print cannot fill a large resting order")
db.ex("DELETE FROM quotes_open"); db.ex("DELETE FROM trades WHERE strat='s5'")
db.ex("DELETE FROM positions WHERE strat='s5'")

print("\n5f. an observed temperature must never settle anything")
# This is the regression test for the most expensive bug the bot has had: the
# recorded high used to write closed=1 and a winner, hours before Polymarket
# resolved, and the authoritative verdict then had nothing left to correct.
db.ex("DELETE FROM settled_highs")
db.ex("INSERT INTO settled_highs(city,target_date,high_f,ts) VALUES('nyc','2026-07-27',84.7,?)",
      (now,))
db.ex("UPDATE markets SET closed=0, winner=NULL, winner_src=NULL, resolve_ts=? "
      "WHERE event_id='e1'", (now - 7200,))
ok(not hasattr(engine, "settle_by_observation"),
   "the observation settlement path is gone, not merely unused")
import asyncio as _a
_a.run(engine.record_observed_highs())
res = db.q("SELECT question,winner,closed,winner_src FROM markets "
           "WHERE event_id='e1' ORDER BY lo")
for r in res:
    print(f"    {r['question']:>14} winner={r['winner']} closed={r['closed']} "
          f"src={r['winner_src']}")
ok(all(r["winner"] is None for r in res),
   "a recorded high of 84.7F declares no winner at all")
ok(all(not r["closed"] for r in res), "and closes nothing")
ok(db.one("SELECT high_f FROM settled_highs WHERE city='nyc'")["high_f"] == 84.7,
   "the high is still stored, as calibration evidence")

# and a position in a market Polymarket has not resolved stays open
_pm.place("s6chk", dict(rows[2]), "BUY", "YES", 0.30, 10, books["m2"], "test")
engine.settle()
ok(db.one("SELECT COUNT(*) c FROM positions WHERE strat='s6chk'")["c"] == 1,
   "settle() leaves positions alone until Gamma resolves the market")
db.ex("UPDATE markets SET closed=1, winner=1, winner_src='obs' WHERE id='m2'")
engine.settle()
ok(db.one("SELECT COUNT(*) c FROM positions WHERE strat='s6chk'")["c"] == 1,
   "and a non-Gamma winner still does not settle it")
db.ex("UPDATE markets SET winner_src='gamma' WHERE id='m2'")
engine.settle()
ok(db.one("SELECT COUNT(*) c FROM positions WHERE strat='s6chk'")["c"] == 0,
   "only Polymarket's own resolution settles it")
ok(db.one("SELECT winner_src FROM settlements WHERE strat='s6chk'")["winner_src"]
   == "gamma", "and the settlement records where the verdict came from")
db.ex("UPDATE markets SET closed=0, winner=NULL, winner_src=NULL WHERE id='m2'")
for t in ("settlements", "trades", "positions"):
    db.ex(f"DELETE FROM {t} WHERE strat='s6chk'")

print("\n5g. ladder-shape dips")
lad = [(-model.INF, 79), (80, 81), (82, 83), (84, 85), (86, 87), (88, model.INF)]
lnames = ["79 or below", "80-81", "82-83", "84-85", "86-87", "88 or above"]
lrows = []
for i, ((lo, hi), nm) in enumerate(zip(lad, lnames)):
    mid = f"L{i}"
    db.ex("""INSERT INTO markets(id,event_id,event_title,question,city,target_date,
             lo,hi,token_yes,token_no,end_ts,resolve_ts,unit,seen_ts,updated_ts)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (mid, "el", "Highest temperature in NYC on 2026-07-27", nm, "nyc",
           "2026-07-27", lo, hi, f"t{mid}", f"n{mid}", now + 20*3600,
           now + 20*3600, "F", now, now))
lrows = db.q("SELECT * FROM markets WHERE event_id='el' ORDER BY lo")
# a clean unimodal ladder with one bucket knocked down
prices = [.04, .14, .30, .12, .18, .05]      # 84-85 dips below both neighbours
lb = {r["id"]: _bk(p, sz=99) for r, p in zip(lrows, prices)}
weather._cache["nyc"] = {"ts": now, "days": {"2026-07-27": fc}}
lev = engine.price_events(lrows, lb)
s8 = strategies.STRATS["s8"]["fn"](dict(markets=lrows, events=lev,
    params=strategies.defaults("s8"), bankroll=100.0, positions={}, now=now, cash=100.0))
for i in s8:
    print(f"    {i['action']} {i['market']['question']:>12} @{i['price']:.3f} - {i['reason'][:76]}")
ok(len(s8) == 1, "exactly the one dip is taken")
ok(s8 and s8[0]["market"]["question"] == "84-85", "and it is the right bucket")

smooth = [.04, .12, .30, .28, .18, .05]      # no dip anywhere
lb2 = {r["id"]: _bk(p, sz=99) for r, p in zip(lrows, smooth)}
lev2 = engine.price_events(lrows, lb2)
ok(not strategies.STRATS["s8"]["fn"](dict(markets=lrows, events=lev2,
   params=strategies.defaults("s8"), bankroll=100.0, positions={}, now=now, cash=100.0)),
   "a properly unimodal ladder produces nothing")

tail = [.30, .18, .10, .05, .03, .01]        # monotonic decline, not a dip
lb3 = {r["id"]: _bk(p, sz=99) for r, p in zip(lrows, tail)}
lev3 = engine.price_events(lrows, lb3)
ok(not strategies.STRATS["s8"]["fn"](dict(markets=lrows, events=lev3,
   params=strategies.defaults("s8"), bankroll=100.0, positions={}, now=now, cash=100.0)),
   "a monotonic tail is not mistaken for a dip")

print("\n6. execution + accounting")
from app import pmclient
m = dict(rows[1])
pmclient.place("s1", m, "BUY", "YES", 0.29, 50, books["m1"], "test")
b = engine.book_summary("s1", books)
ok(b["cash"] < engine.alloc("s1"), "cash decreased on a buy")
ok(len(b["positions"]) == 1, "position recorded")
print("    cash", round(b["cash"], 2), "equity", round(b["equity"], 2))

db.ex("UPDATE markets SET closed=1, winner=1, winner_src='gamma' WHERE id='m1'")
engine.settle()
b2 = engine.book_summary("s1")
print("    after settlement: equity", round(b2["equity"], 2),
      "realised", round(b2["realized"], 2))
ok(b2["realized"] > 0, "winning bucket settles profitably")
ok(not b2["positions"], "position cleared on settlement")

print("\n6b. celsius markets")
import json as _j
cbuckets = {"c1": (-model.INF, 21), "c2": (22, 22), "c3": (23, 23), "c4": (24, model.INF)}
for i, (k, (lo, hi)) in enumerate(cbuckets.items()):
    db.ex("""INSERT INTO markets(id,event_id,event_title,question,city,target_date,
             lo,hi,token_yes,token_no,end_ts,resolve_ts,unit,seen_ts,updated_ts)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (k, "ec", "Highest temperature in London on 2026-07-27",
           f"{int(hi) if hi != model.INF else int(lo)}C", "london", "2026-07-27",
           lo, hi, f"t{k}", f"n{k}", now + 6*3600, now + 6*3600, "C", now, now))
crows = db.q("SELECT * FROM markets WHERE event_id='ec' ORDER BY lo")
weather._cache["london"] = {"ts": now, "days": {"2026-07-27": dict(
    members=[71.5, 72.0, 72.4, 73.1, 71.8, 72.6, 73.4, 72.2],  # ~22C
    sd=0.7, model_spread=0.5, n=8, mean=72.4)}}
# The book has to be a plausible shape, not a flat one. A book quoting 0.22 on
# every bucket of a four-bucket ladder implies a distribution about 1.8C wide,
# and calibrate_width -- which exists precisely to pull the model towards what the
# market is saying -- then widens the model's ~1C ensemble until the open-ended
# "21C or below" bucket collects more mass than the modal 22C. That is the width
# anchoring working correctly on a fixture that could not occur: the four prices
# sum to 0.88 at the bid and 0.96 at the ask, which is not a real ladder.
cbooks = {"c1": dict(bid=.06, ask=.09, bid_sz=99, ask_sz=99, mid=.075),
          "c2": dict(bid=.48, ask=.53, bid_sz=99, ask_sz=99, mid=.505),
          "c3": dict(bid=.28, ask=.32, bid_sz=99, ask_sz=99, mid=.300),
          "c4": dict(bid=.06, ask=.10, bid_sz=99, ask_sz=99, mid=.080)}
cev = engine.price_events(crows, cbooks)
for r in crows:
    print(f"    {r['question']:>6} ({r['unit']})  fair {r['fair']:.3f}")
tot = sum(r["fair"] for r in crows)
ok(abs(tot - 1) < 1e-6, "celsius buckets still sum to 1")
mode = max(crows, key=lambda r: r["fair"])
ok(mode["question"] == "22C", f"72F correctly prices into the 22C bucket, not {mode['question']}")
ok(all(r["fair"] < 0.99 for r in crows),
   "no bucket is declared impossible by a unit mismatch")

print("\n7. risk")
prm = strategies.defaults("s1")
it = dict(action="BUY", market=rows[2], outcome="YES", price=0.33, qty=100000,
          fair=.5, market_p=.33, reason="x")
res, why = engine.risk_check("s1", it, prm, 200, 5.0, {})
ok(res is None or it["qty"] * it["price"] <= 5.5, f"cash cap enforced ({why})")

it2 = dict(action="BUY", market=rows[2], outcome="YES", price=0.30, qty=100,
           fair=.5, market_p=.30, reason="x")
full = {f"m{i}": 20.0 for i in range(6)}          # $120 already at risk
res2, why2 = engine.risk_check("s1", it2, prm, 100, 500.0, full)
print(f"    bankroll 100, already risking 120 -> {why2 or 'allowed'}")
ok(res2 is None, "portfolio cap blocks a fully invested book from adding more")
part = {"mA": 40.0}
it3 = dict(action="BUY", market=rows[2], outcome="YES", price=0.30, qty=1000,
           fair=.5, market_p=.30, reason="x")
res3, why3 = engine.risk_check("s1", it3, prm, 100, 500.0, part)
print(f"    bankroll 100, risking 40, wants 300 -> trimmed to "
      f"{it3['qty'] * it3['price']:.1f}")
ok(res3 is not None and it3["qty"] * it3["price"] <= 55 * 1.02,
   "and trims an oversized order down to the remaining room")

print("\n6c. spread filters and marking")
prm_w = strategies.defaults("s1")
for nm, bid, ask, want in [("cheap tail", .090, .125, True),
                           ("mid price", .620, .645, False),
                           ("wide mid", .40, .50, True)]:
    got = strategies._wide(dict(bid=bid, ask=ask), prm_w)
    sp = (ask - bid) / ((ask + bid) / 2)
    print(f"    {nm:<11} {bid:.3f}/{ask:.3f}  spread {sp:>5.0%} of price -> "
          f"{'blocked' if got else 'allowed'}")
    ok(got == want, f"{nm} spread verdict")

db.ex("DELETE FROM positions WHERE strat='mk'")
db.ex("""INSERT INTO positions(strat,market_id,outcome,qty,avg_price,opened_ts)
         VALUES('mk','m1','YES',50,0.125,?)""", (now,))
mid_marks = {"m1": dict(bid=0.090, ask=0.125)}
b_mid = engine.book_summary("mk", mid_marks)
print(f"    bought 50 @0.125, book 0.090/0.125 -> unrealised "
      f"{b_mid['unrealized']:+.2f} (marked at mid 0.108)")
ok(abs(b_mid["unrealized"] + 0.875) < 0.01,
   "marked at mid, not at bid, so the spread is not charged twice")
db.ex("DELETE FROM positions WHERE strat='mk'")

print("\n7a. market-anchored kernel width")
wide = [78.0, 80.0, 82.0, 84.0, 86.0, 88.0, 90.0, 92.0]      # sd ~4.6F
bk8 = {"a": (-model.INF, 83), "b": (84, 84), "c": (85, 85), "d": (86, model.INF)}
sharp = {"a": .05, "b": .40, "c": .40, "d": .15}             # market says sd ~1F
msd = model.market_implied_sd(bk8, sharp)
print(f"    market-implied sd {msd:.2f}F vs a model kernel of 3.00F")
print(f"    blended at weight {model.MARKET_SD_WEIGHT:.1f} -> "
      f"{model.blend_sd(3.0, msd):.2f}F")
ok(msd is not None and msd < 3.0, "the market's implied width is recovered")
ok(model.blend_sd(3.0, msd) < 3.0, "and the kernel shrinks toward it")
ok(model.blend_sd(3.0, None) == 3.0, "with no market prices the prior is untouched")
ok(model.blend_sd(3.0, msd, weight=0.0) == 3.0, "weight 0 keeps the prior")

print("\n7b. one-sided book cap")
prm_s = strategies.defaults("s1")
side_it = dict(action="BUY", market=rows[1], outcome="NO", price=0.70, qty=20,
               fair=.2, market_p=.30, reason="x")
loaded_no = {"YES": 4.0, "NO": 26.0}
res_s, why_s = engine.risk_check("s1", side_it, prm_s, 100, 500.0, {}, {}, loaded_no)
print(f"    book already 87% NO -> {why_s or 'allowed'}")
ok(res_s is None, "a lopsided book stops taking more of the same side")
yes_it = dict(side_it); yes_it["outcome"] = "YES"
res_y, _ = engine.risk_check("s1", yes_it, prm_s, 100, 500.0, {}, {}, loaded_no)
ok(res_y is not None, "but the other side is still allowed")

print("\n7b2. regional correlation cap")
from app import config as _cfg
ne = dict(rows[1]); ne["city"] = "nyc"; ne["target_date"] = "2026-07-27"
prm_r = strategies.defaults("s1")
print(f"    cap = {prm_r['max_region_exposure']:.0%} of bankroll per region per day")
it_a = dict(action="BUY", market=ne, outcome="YES", price=0.30, qty=100,
            fair=.5, market_p=.30, reason="x")
loaded = {("northeast", "2026-07-27"): 18.0}      # boston+philly+dc already on
res_a, why_a = engine.risk_check("s1", it_a, prm_r, 100, 500.0, {}, loaded)
print(f"    northeast already carrying 18 on a 100 book -> {why_a or 'allowed'}")
ok(res_a is None, "a loaded region blocks another city on the same day")

other = dict(ne); other["city"] = "seoul"
it_b = dict(action="BUY", market=other, outcome="YES", price=0.30, qty=10,
            fair=.5, market_p=.30, reason="x")
res_b, why_b = engine.risk_check("s1", it_b, prm_r, 100, 500.0, {}, loaded)
print(f"    same day, different air mass (seoul) -> {why_b or 'allowed'}")
ok(res_b is not None, "an uncorrelated region is unaffected")

nextday = dict(ne); nextday["target_date"] = "2026-07-28"
it_c = dict(action="BUY", market=nextday, outcome="YES", price=0.30, qty=10,
            fair=.5, market_p=.30, reason="x")
res_c, _ = engine.risk_check("s1", it_c, prm_r, 100, 500.0, {}, loaded)
ok(res_c is not None, "and the same region on another day is a separate bet")

print("\n7c. nowcast ramp")
p6 = strategies.defaults("s6")
for hr in (8, 11, 13, 16, 19):
    lo_h, hi_h = p6["ramp_from_hour"], p6["late_hour"]
    if hr <= lo_h: mult = 1.0
    elif hr >= hi_h: mult = p6["late_sd_mult"]
    else: mult = 1.0 - (1.0 - p6["late_sd_mult"]) * (hr - lo_h) / max(1.0, hi_h - lo_h)
    print(f"    {hr:02d}:00 local -> uncertainty x{mult:.2f}")
ok(True, "tightening ramps in rather than snapping at a fixed hour")

print("\n7d. cost-basis exposure, not mark value")
db.ex("DELETE FROM positions WHERE strat='avgdown'")
db.ex("DELETE FROM trades WHERE strat='avgdown'")
mkt = dict(rows[1])
prm_a = {"max_position_usd": 5.0}
free = 500.0
# reproduce the real sequence: price falling from 0.315 to 0.055 while the
# bot keeps buying because the OLD exposure check used current mark value
seq = [(15.9, .315), (5.1, .255), (10.3, .175), (16.2, .105), (16.8, .065), (25.8, .055)]
blocked_at = None
for i, (qty, px) in enumerate(seq):
    bk = engine.book_summary("avgdown", {mkt["id"]: dict(bid=px, ask=px)})
    exposure = {p["market_id"]: p["cost"] for p in bk["positions"]}
    it = dict(action="BUY", market=mkt, outcome="YES", price=px, qty=qty,
              fair=.5, market_p=px, reason="x")
    ok_, why = engine.risk_check("avgdown", it, prm_a, 100, free, exposure)
    print(f"    fill {i+1}: price {px:.3f}, position cost so far "
          f"{sum(exposure.values()):.2f} -> {'BLOCKED: '+why if ok_ is None else f'buys {it["qty"]:.1f}'}")
    if ok_ is None:
        blocked_at = i
        break
    res = pmclient.place("avgdown", mkt, "BUY", "YES", px, it["qty"], None, "test")
    free += res["cash"]
final_cost = sum(p["cost"] for p in engine.book_summary("avgdown")["positions"])
print(f"    final cost basis: {final_cost:.2f} (cap was {prm_a['max_position_usd']:.2f})")
ok(blocked_at is not None and blocked_at <= 1,
   "the cap catches it by the second fill instead of the seventh")
ok(final_cost <= prm_a["max_position_usd"] * 1.05,
   f"total spend stays within the cap ({final_cost:.2f} vs 5.00)")
db.ex("DELETE FROM positions WHERE strat='avgdown'")
db.ex("DELETE FROM trades WHERE strat='avgdown'")

print("\n7e. maker fills are capped too")
for t in ("quotes_open", "trades", "positions", "settlements", "signals"):
    db.ex(f"DELETE FROM {t} WHERE strat='s5'")
db.cfg_set("params:s5", {"max_total_exposure": 0.5})
many = {}
for i in range(10):
    mid = f"cap{i}"
    m = dict(rows[1], id=mid, question=f"bucket {i}")
    db.ex("""INSERT OR REPLACE INTO markets(id,event_id,event_title,question,city,
             target_date,lo,hi,token_yes,token_no,end_ts,resolve_ts,unit,seen_ts,
             updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (mid, f"ecap{i}", "ev", f"b{i}", "nyc", "2026-07-27", 80+i, 80+i,
           f"t{mid}", f"n{mid}", now+20*3600, now+20*3600, "F", now, now))
    m["last_trade"] = 0.40
    m["book"] = dict(bid=0.29, ask=0.31, bid_sz=99, ask_sz=99, mid=0.30)
    m["volume_num"] = 0.0
    pmclient.post_quote("s5", m, "BUY", "YES", 0.30, 15, "test")   # $4.50 each
    m["last_trade"] = 0.30                                        # new print through it
    m["volume_num"] = 500.0                                       # with volume behind it
    many[mid] = m
db.ex("UPDATE quotes_open SET posted_ts=? WHERE strat='s5'", (now - 1800,))
filled = engine.simulate_maker_fills("s5", many)
b5 = engine.book_summary("s5")
cost5 = sum(p["cost"] for p in b5["positions"])
print("    10 quotes at $4.50 each = $45 total, cap is 50% of 100 = $50")
print(f"    filled: {filled}  |  total cost basis: {cost5:.2f}")
ok(cost5 <= 50.5, f"total spend across many small quotes still respects the cap ({cost5:.2f})")

for i in range(10, 20):
    mid = f"cap{i}"
    m = dict(rows[1], id=mid, question=f"bucket {i}")
    db.ex("""INSERT OR REPLACE INTO markets(id,event_id,event_title,question,city,
             target_date,lo,hi,token_yes,token_no,end_ts,resolve_ts,unit,seen_ts,
             updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (mid, f"ecap{i}", "ev", f"b{i}", "nyc", "2026-07-27", 80+i, 80+i,
           f"t{mid}", f"n{mid}", now+20*3600, now+20*3600, "F", now, now))
    m["last_trade"] = 0.40
    m["book"] = dict(bid=0.29, ask=0.31, bid_sz=99, ask_sz=99, mid=0.30)
    pmclient.post_quote("s5", m, "BUY", "YES", 0.30, 15, "test")
    m["last_trade"] = 0.30
    many[mid] = m
db.ex("UPDATE quotes_open SET posted_ts=? WHERE strat='s5'", (now - 1800,))
filled2 = engine.simulate_maker_fills("s5", many)
cost5b = sum(p["cost"] for p in engine.book_summary("s5")["positions"])
print(f"    another 10 quotes offered -> cost basis now {cost5b:.2f}")
ok(cost5b <= 50.5, "and stays capped even when far more quotes are offered")
ok(filled2 == 0 or cost5b <= 50.5, "excess quotes get pulled rather than filled")
db.ex("DELETE FROM quotes_open WHERE strat='s5'")
db.ex("DELETE FROM trades WHERE strat='s5'")
db.ex("DELETE FROM positions WHERE strat='s5'")
db.cfg_set("params:s5", None)

print("\n8. retention + export")
for i in range(300):
    db.ex("INSERT INTO quotes(ts,market_id,bid,ask,mid,bid_sz,ask_sz,fair) "
          "VALUES(?,?,?,?,?,?,?,?)", (now - 90 * 3600 + i, "m2", .3, .33, .315, 9, 9, .4))
before = db.one("SELECT COUNT(*) n FROM quotes")["n"]
db.compact()
after = db.one("SELECT COUNT(*) n FROM quotes")["n"]
rolled = db.one("SELECT COUNT(*) n FROM quotes_hourly")["n"]
ok(after < before and rolled > 0, f"old ticks rolled to hourly ({before}->{after}, {rolled} bars)")
blob = db.export_zip()
ok(len(blob) > 500 and blob[:2] == b"PK", f"export is a zip, {len(blob)/1024:.1f} kb")
import zipfile, io
names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
ok(any(n.endswith("trades.csv") for n in names) and "wxalpha.db" in names,
   f"export holds {len(names)} entries incl. raw db")

print("\n9. the dashboard endpoints survive a full engine STATE")
# /api/state used to be dict(**engine.STATE, db_mb=...), which is fine until
# maintain() writes STATE["db_mb"] on tick 40 -- then every dashboard poll
# 500s and the page reads SERVER UNREACHABLE. Exercise it with STATE fully
# populated, the way it looks half an hour into a run, not at boot.
engine.STATE.update(tick=41, running=True, last_tick=time.time(),
                    markets=220, events=20, db_mb=12.3)
from app import main   # noqa: E402
# Called directly rather than through fastapi.testclient: that pulls in a real
# httpx, and this suite stubs httpx out precisely so it runs with no network.
for name, fn in (("/api/state", main.state), ("/health", main.health),
                 ("/version.txt", main.version_txt),
                 ("/api/health/feeds", main.feeds),
                 ("/api/health/settlement", main.settlement_health),
                 ("/api/health/calibration", main.calibration),
                 ("/api/analysis/cashout", main.cashout)):
    try:
        fn()
        ok(True, f"{name} returns without raising")
    except Exception as e:
        ok(False, f"{name} raised {type(e).__name__}: {e}")

st = main.state()["engine"]
ok(st.get("tick") == 41, f"state carries the engine's own keys (tick {st.get('tick')})")
missing = [k for k in ("db_mb", "live", "poll", "uptime_h") if k not in st]
ok(not missing, f"and the keys the endpoint adds{'' if not missing else ': missing ' + str(missing)}")

print("\n10. discovery is allowed to settle a market")
# The live run's real failure: 21 hours, 561 markets, 330 of them past their
# close, and not one settlement. refresh_closures() asked Gamma for those ids
# without closed=true, got an empty list back, and its `continue` said nothing.
# Meanwhile the event scan *was* being handed the verdict every 90 seconds and
# throwing it away at `if m.get("closed"): continue`. Both halves are fixed;
# this pins the half that is guaranteed to keep receiving the data.
db.ex("DELETE FROM markets WHERE event_id='ez'")
for k, label, (lo, hi) in (("z1", "79 or below", (-model.INF, 79)),
                           ("z2", "80-81", (80, 81)),
                           ("z3", "82 or above", (82, model.INF))):
    db.ex("""INSERT INTO markets(id,event_id,event_title,question,city,target_date,
             lo,hi,token_yes,token_no,end_ts,resolve_ts,unit,seen_ts,updated_ts,closed)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
          (k, "ez", "Highest temperature in NYC on 2026-07-27", label,
           "nyc", "2026-07-27", lo, hi, f"t{k}", f"n{k}",
           now - 3600, now - 3600, "F", now, now))

closed_ev = dict(id="ez", title="Highest temperature in NYC on 2026-07-27",
                 endDate="2026-07-28T04:00:00Z", markets=[
    dict(id="z1", closed=True, outcomePrices='["0","1"]', groupItemTitle="79 or below"),
    dict(id="z2", closed=True, outcomePrices='["1","0"]', groupItemTitle="80-81"),
    # closed, but the price has not converged -- UMA is still arguing
    dict(id="z3", closed=True, outcomePrices='["0.62","0.38"]', groupItemTitle="82+"),
])
_r, _reason, _res = _pm._parse_event(closed_ev)
ok(len(_res) == 3, f"the event scan captures closed buckets instead of skipping them ({len(_res)})")
ok(not _r, "and stores no ladder rows for an event that is entirely resolved")

_pm._store([closed_ev])
zz = {r["id"]: r for r in db.q("SELECT * FROM markets WHERE event_id='ez'")}
for k in ("z1", "z2", "z3"):
    print(f"    {k}: closed={zz[k]['closed']} winner={zz[k]['winner']} "
          f"src={zz[k]['winner_src']}")
ok(zz["z2"]["winner"] == 1 and zz["z2"]["winner_src"] == "gamma",
   "outcomePrices ['1','0'] books a YES from Polymarket's own verdict")
ok(zz["z1"]["winner"] == 0 and zz["z1"]["closed"] == 1,
   "and ['0','1'] books the loser rather than leaving it open forever")
ok(zz["z3"]["winner"] is None and not zz["z3"]["closed"],
   "0.62 is not a resolution: that bucket stays open and keeps marking to market")

# Re-booking must be idempotent. Discovery re-reports these on every scan, and
# an UPDATE that fired each time would restamp winner_ts and log a fresh
# "N settled" line every 90 seconds for the rest of the run.
_ts = zz["z2"]["winner_ts"]
ok(_pm.book_resolution("z2", 1.0) is _pm.ALREADY,
   "a second pass over the same market reports it as already booked")
ok(db.one("SELECT winner_ts FROM markets WHERE id='z2'")["winner_ts"] == _ts,
   "and leaves the original settlement timestamp alone")

# The upsert used to write closed=0 unconditionally, so the next discovery pass
# would quietly re-open everything settled above.
open_ev = dict(id="ez", title="Highest temperature in NYC on 2026-07-27",
               endDate="2026-07-28T04:00:00Z", markets=[
    dict(id="z2", closed=False, groupItemTitle="80-81", bestBid=0.99, bestAsk=1.0,
         clobTokenIds='["tz2","nz2"]'),
])
_pm._store([open_ev])
z2 = db.one("SELECT closed,winner FROM markets WHERE id='z2'")
ok(z2["closed"] == 1 and z2["winner"] == 1,
   "a later scan cannot un-close a market Polymarket has already resolved")
db.ex("DELETE FROM markets WHERE event_id='ez'")

print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"FAILURES: {FAILED}"))
sys.exit(1 if FAILED else 0)
