"""Five strategies. Each keeps its own bankroll, positions and P&L so the tabs
are genuinely independent experiments rather than five views of one book.

They are deliberately uncorrelated in what they depend on:

  S1 EDGE      forecast vs price. The base case. Dies if the model is wrong.
  S2 CONSENSUS trades only when independent models agree. Fewer, better trades.
  S3 DUTCH     pure order-book arithmetic. Needs no forecast at all, so it keeps
               earning when the weather API is down or the model is miscalibrated.
  S4 DRIFT     forecast *revisions* near settlement, when skill is highest and
               the book is thinnest.
  S5 QUOTE     posts two-sided quotes and earns the spread instead of predicting.

If you only keep two, keep S3 and S4: one is structural, one has the strongest
information edge. S1 is the honest benchmark to measure the others against.
"""
import time

from . import model

INF = float("inf")


def spec():
    return {k: dict(name=v["name"], tagline=v["tagline"], detail=v["detail"],
                    params=v["params"], hue=v["hue"])
            for k, v in STRATS.items()}


def defaults(sid):
    return {k: v["default"] for k, v in STRATS[sid]["params"].items()}


# --------------------------------------------------------------- helpers
def _sizes(p, price, bankroll, prm, book_sz):
    f = model.kelly(p, price) * prm["kelly_fraction"]
    usd = min(bankroll * f, prm["max_position_usd"])
    qty = usd / max(price, 0.01)
    return min(qty, book_sz * prm.get("book_take", 0.5))


def _hours(m):
    return model.hours_left(m.get("resolve_ts") or m["end_ts"])


def _wide(bk, prm):
    """Spread test, absolute and relative.

    An 8c spread is 16% of a 50c contract and 66% of a 12c one. Judging it in
    cents alone waves through exactly the cheap tail contracts where crossing
    costs more than any edge you could have.
    """
    sp = bk["ask"] - bk["bid"]
    if sp > prm.get("max_spread", 0.10):
        return True
    mid = (bk["ask"] + bk["bid"]) / 2
    if mid > 0 and sp / mid > prm.get("max_spread_frac", 0.20):
        return True
    return False


def _tradeable(m, bk, prm, now):
    if not bk or bk["ask"] >= 1.0 or bk["bid"] <= 0.0:
        return False
    h = _hours(m)
    if h < prm.get("min_hours", 1) or h > prm.get("max_hours", 240):
        return False
    if _wide(bk, prm):
        return False
    if min(bk["bid_sz"], bk["ask_sz"]) < prm.get("min_book_size", 20):
        return False
    return True


def _net(edge, price, m):
    """Edge net of the taker fee. On weather markets that fee is
    0.05 * min(p, 1-p) per share -- 1.4c at a 28c price -- so a 5-point gross
    edge can easily be negative once you have paid to take it."""
    return model.net_edge(edge, price, m)


def _sane(price, edge, prm):
    """Two guards that stop the classic way a forecast bot bleeds out.

    Deep tails: a 3c contract the model calls 25% looks like enormous edge, but
    at that price the model error dominates the signal and the payout profile
    is exactly the one you lose on.

    Absurd edge: if the model claims more than max_edge, the likelier
    explanation is a bad bucket parse or a stale forecast, not a mispriced book.
    """
    if price < prm.get("min_market_price", 0.02):
        return False, "price below tail floor"
    if edge > prm.get("max_edge", 0.35):
        return False, "edge implausible, treating as model error"
    return True, ""


def _intent(action, m, outcome, price, qty, fair, mp, reason):
    return dict(action=action, market=m, outcome=outcome, price=price,
                qty=qty, fair=fair, market_p=mp, edge=fair - mp, reason=reason)


# --------------------------------------------------------------- S1
def s1_edge(ctx):
    prm, out = ctx["params"], []
    for m in ctx["markets"]:
        bk, fair = m.get("book"), m.get("fair")
        if fair is None or not _tradeable(m, bk, prm, ctx["now"]):
            continue
        e = _net(fair - bk["ask"], bk["ask"], m)
        good, _ = _sane(bk["ask"], e, prm)
        if e > prm["min_edge"] and good:
            qty = _sizes(fair, bk["ask"], ctx["bankroll"], prm, bk["ask_sz"])
            if qty > 1:
                out.append(_intent("BUY", m, "YES", bk["ask"], qty, fair, bk["ask"],
                                   f"model {fair:.0%} vs ask {bk['ask']:.0%}, "
                                   f"edge {e:+.1%}"))
        ask_no = 1 - bk["bid"]
        e_no = _net((1 - fair) - ask_no, ask_no, m)
        good_no, _ = _sane(ask_no, e_no, prm)
        if e_no > prm["min_edge"] and good_no:
            qty = _sizes(1 - fair, ask_no, ctx["bankroll"], prm, bk["bid_sz"])
            if qty > 1:
                out.append(_intent("BUY", m, "NO", ask_no, qty, fair, bk["bid"],
                                   f"model {1-fair:.0%} NO vs ask {ask_no:.0%}, "
                                   f"edge {(1-fair)-ask_no:+.1%}"))
    return out


# --------------------------------------------------------------- S2
def s2_consensus(ctx):
    """Only fire when the ensemble is tight AND the five deterministic models
    agree. On those days the forecast is worth far more than the market thinks,
    so we concentrate into the single modal bucket instead of spraying."""
    prm, out = ctx["params"], []
    for ev_id, group in ctx["events"].items():
        fc = group[0].get("fc")
        if not fc:
            continue
        ms = fc.get("model_spread")
        if fc["sd"] > prm["max_ensemble_sd"]:
            continue
        if ms is not None and ms > prm["max_model_spread"]:
            continue
        scored = [g for g in group if g.get("fair") is not None and g.get("book")]
        if not scored:
            continue
        scored.sort(key=lambda g: -g["fair"])
        for m in scored[:int(prm["top_n_buckets"])]:
            bk, fair = m["book"], m["fair"]
            if not _tradeable(m, bk, prm, ctx["now"]) or fair < prm["min_confidence"]:
                continue
            e2 = _net(fair - bk["ask"], bk["ask"], m)
            good, _ = _sane(bk["ask"], e2, prm)
            if e2 > prm["min_edge"] and good:
                qty = _sizes(fair, bk["ask"], ctx["bankroll"], prm, bk["ask_sz"])
                if qty > 1:
                    out.append(_intent(
                        "BUY", m, "YES", bk["ask"], qty, fair, bk["ask"],
                        f"models agree (ens sd {fc['sd']:.1f}F"
                        + (f", cross-model {ms:.1f}F" if ms is not None else "")
                        + f"); modal bucket at {bk['ask']:.0%}"))
    return out


# --------------------------------------------------------------- S3
def s3_dutch(ctx):
    """Buckets on one event are mutually exclusive and exhaustive. Buying every
    bucket for a combined cost under $1 pays exactly $1 whatever the weather
    does. Needs no forecast, so it is the strategy that survives model error."""
    prm, out = ctx["params"], []
    for ev_id, group in ctx["events"].items():
        legs = [g for g in group if g.get("book")]
        if len(legs) < int(prm["min_legs"]):
            continue
        if any(_hours(g) < prm["min_hours"] for g in legs):
            continue

        cost = sum(g["book"]["ask"] for g in legs)
        fees = sum(model.fee_per_share(g["book"]["ask"], g.get("fee_rate"),
                                       g.get("fee_exp")) for g in legs)
        if cost + fees < 1.0 - prm["buffer"]:
            sz = min(g["book"]["ask_sz"] for g in legs)
            qty = min(sz * 0.8, prm["max_position_usd"] / max(cost, 0.05),
                      ctx["bankroll"] * prm["max_event_frac"] / max(cost, 0.05))
            if qty >= prm["min_qty"]:
                for g in legs:
                    out.append(_intent(
                        "BUY", g, "YES", g["book"]["ask"], qty,
                        g.get("fair") or g["book"]["ask"], g["book"]["ask"],
                        f"dutch book: {len(legs)} legs cost {cost:.3f} "
                        f"+ {fees:.3f} fees, locked {1-cost-fees:+.3f}/set"))

        credit = sum(g["book"]["bid"] for g in legs)
        no_cost = len(legs) - credit          # buying NO on every leg
        no_fees = sum(model.fee_per_share(1 - g["book"]["bid"], g.get("fee_rate"),
                                          g.get("fee_exp")) for g in legs)
        no_cost += no_fees
        if credit > 1.0 + prm["buffer"] and no_cost < len(legs) - 1:
            sz = min(g["book"]["bid_sz"] for g in legs)
            qty = min(sz * 0.8, prm["max_position_usd"] / max(no_cost, 0.05),
                      ctx["bankroll"] * prm["max_event_frac"] / max(no_cost, 0.05))
            if qty >= prm["min_qty"]:
                for g in legs:
                    out.append(_intent(
                        "BUY", g, "NO", 1 - g["book"]["bid"], qty,
                        1 - (g.get("fair") or g["book"]["bid"]), 1 - g["book"]["bid"],
                        f"inverse dutch: bids sum {credit:.3f}, "
                        f"locked {credit-1:+.3f}/set"))
    return out


# --------------------------------------------------------------- S4
def s4_drift(ctx):
    """The last hours before settlement. Forecast error collapses, but the book
    thins out and stale limit orders sit there. Also trades the direction of the
    forecast revision itself, which the market reprices with a lag."""
    prm, out = ctx["params"], []
    held = ctx["positions"]
    for m in ctx["markets"]:
        bk, fair = m.get("book"), m.get("fair")
        if fair is None or not bk:
            continue
        h = _hours(m)
        if h > prm["window_hours"] or h < prm["min_hours"]:
            continue
        if _wide(bk, prm):
            continue

        pos = held.get((m["id"], "YES"))
        if pos and pos["qty"] > 0 and fair < prm["exit_confidence"]:
            out.append(_intent("SELL", m, "YES", bk["bid"], pos["qty"], fair,
                               bk["bid"], f"forecast fell to {fair:.0%}, cutting"))
            continue

        drift = m.get("drift_f")               # F of revision since last forecast
        toward = drift is not None and abs(drift) >= prm["min_revision_f"] and (
            (drift > 0 and m["lo"] != -INF and m["lo"] >= (m.get("fc_mean") or 0) - 2)
            or (drift < 0 and m["hi"] != INF and m["hi"] <= (m.get("fc_mean") or 0) + 2))

        conviction = fair >= prm["entry_confidence"]
        if not (conviction or toward):
            continue
        need = prm["min_edge"] * (0.5 if toward and conviction else 1.0)
        e4 = _net(fair - bk["ask"], bk["ask"], m)
        if e4 <= need:
            continue
        good, _ = _sane(bk["ask"], e4, prm)
        if not good:
            continue
        qty = _sizes(fair, bk["ask"], ctx["bankroll"], prm, bk["ask_sz"])
        if qty > 1:
            why = f"T-{h:.1f}h, model {fair:.0%} vs {bk['ask']:.0%}"
            if toward:
                why += f", forecast revised {drift:+.1f}F toward this bucket"
            out.append(_intent("BUY", m, "YES", bk["ask"], qty, fair, bk["ask"], why))
    return out


# --------------------------------------------------------------- S5
def s5_quote(ctx):
    """A real maker: posts resting orders and never crosses the spread.

    This matters because of the fee schedule. The weather taker fee is
    0.05 * min(p, 1-p) -- 1.4c a share at 28c -- and it is takerOnly. A quoter
    that lifts offers pays that on every fill and cannot earn it back from a 2c
    spread. Resting orders pay nothing, and orders within rewardsMaxSpread of
    the midpoint (4.5c on these markets, minimum 20 shares) collect liquidity
    rewards on top.

    So the half-spread is capped at the reward window rather than floored at the
    fee, and the strategy emits QUOTE intents that the engine keeps on the book
    and re-prices only when fair value has actually moved. Filling is passive:
    somebody has to come to us.
    """
    prm, out = ctx["params"], []
    held = ctx["positions"]
    for m in ctx["markets"]:
        bk, fair = m.get("book"), m.get("fair")
        if fair is None or not bk:
            continue
        h = _hours(m)
        if h < prm["pull_hours"] or h > prm["max_hours"]:
            continue
        if not (prm["min_price"] < fair < prm["max_price"]):
            continue
        unc = m.get("prob_sd") or 0.05
        if unc > prm["max_uncertainty"]:
            continue

        # stay inside the reward window, or the rebate is forfeited
        reward_half = (m.get("reward_spread") or 4.5) / 100.0
        half = prm["base_half_spread"] + prm["uncertainty_mult"] * unc
        half = max(prm["min_half_spread"], min(half, reward_half, prm["max_half_spread"]))

        pos = held.get((m["id"], "YES"))
        inv = pos["qty"] if pos else 0.0
        inv_cap = max(1.0, prm["max_inventory_usd"] / max(fair, 0.05))
        skew = prm["inventory_skew"] * half * max(-1.0, min(1.0, inv / inv_cap))
        centre = fair - skew
        tick = m.get("tick") or 0.01

        my_bid = model.round_tick(centre - half, tick)
        my_ask = model.round_tick(centre + half, tick)

        # never cross: a quote at or through the touch is a taker order
        if my_bid >= bk["ask"]:
            my_bid = model.round_tick(bk["ask"] - tick, tick)
        if my_ask <= bk["bid"]:
            my_ask = model.round_tick(bk["bid"] + tick, tick)

        size = max(prm["clip_usd"] / max(fair, 0.05),
                   m.get("reward_min_size") or 20.0)

        if inv < inv_cap and my_bid > tick:
            out.append(dict(action="QUOTE", side="BUY", market=m, outcome="YES",
                            price=my_bid, qty=min(size, inv_cap - inv),
                            fair=fair, market_p=bk["bid"], edge=fair - my_bid,
                            reason=f"bid {my_bid:.3f} / ask {my_ask:.3f}, half "
                                   f"{half:.3f} inside {reward_half:.3f} reward "
                                   f"window, inv {inv:.0f}"))
        if inv > prm["min_inventory_to_offer"] and my_ask < 1 - tick:
            out.append(dict(action="QUOTE", side="SELL", market=m, outcome="YES",
                            price=my_ask, qty=min(inv, size),
                            fair=fair, market_p=bk["ask"], edge=my_ask - fair,
                            reason=f"offering {min(inv, size):.0f} at "
                                   f"{my_ask:.3f} against basis "
                                   f"{(pos['avg_price'] if pos else 0):.3f}"))
    return out


# --------------------------------------------------------------- S6
def s6_observed(ctx):
    """The high already recorded today is a hard floor on the settled high.

    In principle. In practice the floor was not a floor, and this was the single
    worst-performing idea in the bot: 12 of its 16 "impossible bucket" trades
    named a temperature above the day's actual high, and two of them alone
    accounted for ~$295 of a ~$565 phantom profit. Moscow was declared to have
    already reached 19C at midnight local time.

    The premise fails for two compounding reasons. `fetch_observed` reads the
    hourly analysis endpoint, which disagrees with the daily-maximum endpoint by
    1-3F -- so the bot's own two sources for "what happened" contradict each
    other. And both are gridpoint values, while Polymarket resolves on a station
    that ran up to 2.8F different in the measured cities. A floor built from
    those is not a floor; it is a forecast with unusually high confidence
    attached, and it was used to declare live buckets worthless.

    So the impossible-bucket leg now demands the floor clear the bucket top by
    hard_margin_f (default 2.5F, more than the largest measured station gap) on
    top of the ordinary margin, and refuses cities whose bias has never been
    measured. Both gates are deliberately conservative: the leg's whole appeal
    was that it looked risk-free, and it was not.

    Disabled by default -- see STRATS["s6"]["enabled"]. Turn it on once per-city
    bias is measured and BIAS_* is populated.
    """
    prm, out = ctx["params"], []
    for ev_id, group in ctx["events"].items():
        g0 = group[0]
        obs, fc = g0.get("obs"), g0.get("fc")
        if not obs or not fc or obs["date"] != g0["target_date"]:
            continue
        if obs["age_min"] > prm["max_obs_age_min"]:
            continue
        # An unmeasured city is one whose gridpoint-to-station gap is unknown.
        # Trading a "hard floor" there is trading an unquantified error.
        if prm.get("require_measured_bias", 1) and not ctx.get("bias_known", {}).get(
                g0.get("city")):
            continue

        unit = (g0.get("unit") or "F").upper()
        high = model.f_to_c(obs["high"]) if unit == "C" else obs["high"]
        margin = prm["margin_f"] * (5.0 / 9.0 if unit == "C" else 1.0)
        floor = high - margin
        # ramp the tightening in rather than snapping it on at a fixed hour --
        # the day's high firms up gradually through the afternoon, and a cliff
        # edge makes the model lurch at 16:00 for no physical reason
        hr = obs["hour"]
        lo_h, hi_h = prm["ramp_from_hour"], prm["late_hour"]
        if hr <= lo_h:
            mult = 1.0
        elif hr >= hi_h:
            mult = prm["late_sd_mult"]
        else:
            mult = 1.0 - (1.0 - prm["late_sd_mult"]) * (hr - lo_h) / max(1.0, hi_h - lo_h)
        late = hr >= hi_h
        sd = (g0.get("kernel_sd") or 1.0) * mult
        cond = [max(floor, x) for x in fc["members"]]

        legs = [g for g in group if g.get("book")]
        raw = {g["id"]: model.bucket_prob(g["lo"], g["hi"], cond, sd) for g in legs}
        tot = sum(v for v in raw.values() if v)
        if tot > 0.75:
            raw = {k: (v / tot if v else 0.0) for k, v in raw.items()}

        for m in legs:
            bk = m["book"]
            h = _hours(m)
            if h < prm["min_hours"] or h > prm["max_hours"]:
                continue
            if _wide(bk, prm):
                continue

            # already impossible: sell it to whoever is still bidding.
            # hard_margin is on top of the ordinary safety margin already baked
            # into `floor`, and it is sized to exceed the worst station gap
            # measured so far (Tokyo, 2.8F). Below that the bucket is not
            # impossible, it is merely unlikely -- which is the other leg's job.
            hard = prm.get("hard_margin_f", 2.5) * (5.0 / 9.0 if unit == "C" else 1.0)
            if m["hi"] != INF and m["hi"] + 0.5 < floor - hard:
                if bk["bid"] >= prm["min_profit"] and bk["bid_sz"] >= prm["min_book_size"]:
                    px = 1 - bk["bid"]
                    qty = min(prm["max_position_usd"] / max(px, 0.05),
                              bk["bid_sz"] * prm["book_take"])
                    if qty >= 1:
                        out.append(_intent(
                            "BUY", m, "NO", px, qty, 0.0, bk["bid"],
                            f"{high:.0f}{unit} recorded by {obs['hour']:02d}:00 local "
                            f"(gridpoint); this bucket caps at {m['hi']:.0f}{unit}, "
                            f"{floor - hard - m['hi']:.1f}{unit} clear of even the "
                            f"station-gap-adjusted floor, yet it bids {bk['bid']:.0%}"))
                continue

            fair = raw.get(m["id"])
            if fair is None:
                continue
            e = _net(fair - bk["ask"], bk["ask"], m)
            good, _ = _sane(bk["ask"], e, prm)
            if e > prm["min_edge"] and good and bk["ask_sz"] >= prm["min_book_size"]:
                qty = _sizes(fair, bk["ask"], ctx["bankroll"], prm, bk["ask_sz"])
                if qty > 1:
                    out.append(_intent(
                        "BUY", m, "YES", bk["ask"], qty, fair, bk["ask"],
                        f"floor {floor:.0f}{unit} from {high:.0f}{unit} at "
                        f"{obs['hour']:02d}:00{' (past the peak)' if late else ''}; "
                        f"conditioned model {fair:.0%} vs ask {bk['ask']:.0%}"))
    return out


# --------------------------------------------------------------- S7
def s7_fade(ctx):
    """Retail overpays for the outer buckets. That favourite-longshot bias is
    one of the most durable findings in prediction-market research, and weather
    tails are where it should show up hardest: an 8-degree miss makes a better
    story than a 1-degree one.

    So this sells the wings — buys NO on outer buckets bidding well above model
    fair. It is short volatility by construction: many small wins and the
    occasional bad heatwave, so the open-fade count is the cap that matters
    more than the per-market one. Check it against S1 before trusting it; if
    the bias is not there on these markets, this bleeds quietly.
    """
    prm, out = ctx["params"], []
    open_fades = sum(1 for (_, oc) in ctx["positions"] if oc == "NO")
    for ev_id, group in ctx["events"].items():
        ranked = sorted([g for g in group if g.get("book") and g.get("fair") is not None],
                        key=lambda g: g["lo"] if g["lo"] != -INF else -1e9)
        n = int(prm["edge_buckets"])
        if len(ranked) < max(int(prm["min_legs"]), 2 * n + 1):
            continue          # otherwise every bucket counts as a wing
        tails = {id(g): g for g in ranked[:n] + ranked[-n:]}.values()

        for m in tails:
            if open_fades >= prm["max_open_fades"]:
                break
            bk, fair = m["book"], m["fair"]
            if fair > prm["max_fair"]:
                continue
            h = _hours(m)
            if h < prm["min_hours"] or h > prm["max_hours"]:
                continue
            if _wide(bk, prm):
                continue
            if bk["bid"] < prm["min_bid"] or bk["bid_sz"] < prm["min_book_size"]:
                continue
            if bk["bid"] < fair * prm["overprice_mult"]:
                continue
            edge = _net(bk["bid"] - fair, 1 - bk["bid"], m)
            if edge < prm["min_edge"]:
                continue
            px = 1 - bk["bid"]
            good, _ = _sane(px, edge, prm)
            if not good:
                continue
            qty = _sizes(1 - fair, px, ctx["bankroll"], prm, bk["bid_sz"])
            if qty > 1:
                out.append(_intent(
                    "BUY", m, "NO", px, qty, fair, bk["bid"],
                    f"wing bucket bids {bk['bid']:.0%} against model {fair:.0%}, "
                    f"{bk['bid']/max(fair, .001):.1f}x rich"))
                open_fades += 1
    return out


# --------------------------------------------------------------- S8
def s8_shape(ctx):
    """Buy buckets the market has priced below both of their neighbours.

    A daily temperature distribution is unimodal. Ordered by temperature, the
    true bucket probabilities rise to a peak and fall away -- there is no
    physical mechanism that makes 84-85F genuinely less likely than both 82-83F
    and 86-87F. So a dip in the middle of the ladder is a pricing artefact:
    thin books, a stale order, someone dumping one bucket.

    Two things make this worth its own book. It needs no forecast, so like the
    dutch book it survives the model being wrong -- but unlike the dutch book it
    fires often. And its edge is bounded and legible: the bucket is worth at
    least roughly the lower of its two neighbours, which is a claim about shape
    rather than about the weather.

    It deliberately ignores the two open-ended end buckets, where a genuine
    monotonic tail looks identical to a dip.
    """
    prm, out = ctx["params"], []
    for ev_id, group in ctx["events"].items():
        legs = sorted([g for g in group if g.get("book")],
                      key=lambda g: g["lo"] if g["lo"] != -INF else -1e9)
        if len(legs) < int(prm["min_legs"]):
            continue
        mids = [(g["book"]["bid"] + g["book"]["ask"]) / 2 for g in legs]

        for i in range(1, len(legs) - 1):
            m = legs[i]
            bk = m["book"]
            left, right = mids[i - 1], mids[i + 1]
            floor = min(left, right)
            dip = floor - mids[i]
            if dip < prm["min_dip"]:
                continue
            if floor < prm["min_neighbour"]:
                continue          # a dip between two near-worthless buckets is noise
            h = _hours(m)
            if h < prm["min_hours"] or h > prm["max_hours"]:
                continue
            if _wide(bk, prm):
                continue
            if bk["ask_sz"] < prm["min_book_size"] or bk["ask"] < prm["min_market_price"]:
                continue

            # value it conservatively at a fraction of the lower neighbour
            target = floor * prm["neighbour_frac"]
            edge = _net(target - bk["ask"], bk["ask"], m)
            if edge < prm["min_edge"]:
                continue
            good, _ = _sane(bk["ask"], edge, prm)
            if not good:
                continue
            qty = _sizes(target, bk["ask"], ctx["bankroll"], prm, bk["ask_sz"])
            if qty > 1:
                out.append(_intent(
                    "BUY", m, "YES", bk["ask"], qty, target, bk["ask"],
                    f"ladder dip: {mids[i]:.0%} sits below both neighbours "
                    f"({left:.0%} and {right:.0%}); unimodal shape puts it near "
                    f"{target:.0%}"))
    return out


# --------------------------------------------------------------- registry
def P(default, lo, hi, step, label, help=""):
    return dict(default=default, min=lo, max=hi, step=step, label=label, help=help)


COMMON_RISK = {
    "max_total_exposure": P(0.55, 0.05, 1.0, 0.05, "Max share of bankroll at risk",
                            "Per-market caps do not stop it going all-in across many markets"),
    "max_region_exposure": P(0.18, 0.02, 1.0, 0.02, "Max per region per day",
                             "Cities under one air mass lose together"),
    "max_side_exposure": P(0.75, 0.30, 1.0, 0.05, "Max share on one outcome side",
                           "A book that is all NO is one view repeated, not a portfolio"),
    "max_spread_frac": P(0.15, 0.02, 1.0, 0.01, "Max spread as share of price",
                         "8c is 16% of a 50c contract but 66% of a 12c one"),
    "min_market_price": P(0.04, 0.0, 0.30, 0.01, "Tail floor", "Skip contracts cheaper than this"),
    "max_edge": P(0.35, 0.05, 0.95, 0.01, "Edge sanity cap", "Above this, assume the model is wrong"),
    "max_position_usd": P(5, 1, 2000, 1, "Max per market ($)",
                          "Total exposure, not per fill. Below $5 the exchange's "
                          "5-share minimum blocks every bucket priced over $0.60, "
                          "which is every NO trade."),
    "min_book_size": P(25, 0, 1000, 5, "Min resting size (shares)"),
}

STRATS = {
    "s1": dict(
        name="Edge", hue="deep", fn=s1_edge,
        tagline="Forecast versus price",
        detail="Prices every bucket off the ensemble and buys whatever the book "
               "has mispriced by more than the edge threshold. Sized by fractional "
               "Kelly. This is the benchmark the other four have to beat.",
        params={
            "min_edge": P(0.06, 0.01, 0.30, 0.005, "Min edge", "Model probability minus ask"),
            "kelly_fraction": P(0.25, 0.02, 1.0, 0.01, "Kelly fraction"),
            "max_spread": P(0.08, 0.01, 0.40, 0.005, "Max bid-ask"),
            "min_hours": P(2, 0, 72, 1, "Min hours to close"),
            "max_hours": P(96, 6, 360, 6, "Max hours to close"),
            "book_take": P(0.5, 0.05, 1.0, 0.05, "Max share of top level"),
            **COMMON_RISK}),

    "s2": dict(
        name="Consensus", hue="ice", fn=s2_consensus,
        tagline="Trade only when the models agree",
        detail="Gates on both ensemble spread and cross-model spread. When five "
               "independent models land within a degree of each other the forecast "
               "is far better than the market prices it, so this concentrates into "
               "the modal bucket. It will sit idle for days at a time. That is the "
               "point.",
        params={
            "max_ensemble_sd": P(1.8, 0.3, 6.0, 0.1, "Max ensemble spread (F)"),
            "max_model_spread": P(1.2, 0.2, 6.0, 0.1, "Max cross-model spread (F)"),
            "min_confidence": P(0.40, 0.10, 0.95, 0.01, "Min model probability"),
            "min_edge": P(0.04, 0.005, 0.30, 0.005, "Min edge"),
            "top_n_buckets": P(2, 1, 5, 1, "Buckets per event"),
            "kelly_fraction": P(0.45, 0.02, 1.0, 0.01, "Kelly fraction"),
            "max_spread": P(0.10, 0.01, 0.40, 0.005, "Max bid-ask"),
            "min_hours": P(3, 0, 72, 1, "Min hours to close"),
            "max_hours": P(72, 6, 360, 6, "Max hours to close"),
            "book_take": P(0.6, 0.05, 1.0, 0.05, "Max share of top level"),
            **COMMON_RISK}),

    "s3": dict(
        name="Dutch", hue="cold", fn=s3_dutch,
        tagline="Order-book arithmetic, no forecast",
        detail="Buckets on one event are mutually exclusive and exhaustive, so "
               "their asks should sum to one. When they sum to less, buying the "
               "full set pays a dollar regardless of the weather. Rare, small, "
               "and close to risk-free. Raise the buffer if fills are slipping.",
        params={
            "buffer": P(0.03, 0.002, 0.40, 0.002, "Required discount",
                       "On top of fees, which are now priced in per leg"),
            "min_legs": P(3, 2, 12, 1, "Min buckets in event"),
            "max_event_frac": P(0.25, 0.02, 1.0, 0.01, "Max bankroll per set"),
            "min_qty": P(5, 1, 500, 1, "Min sets"),
            "min_hours": P(0.5, 0, 48, 0.5, "Min hours to close"),
            "max_spread_frac": P(0.25, 0.02, 1.0, 0.01, "Max spread as share of price"),
            "max_total_exposure": P(0.60, 0.05, 1.0, 0.05, "Max share of bankroll at risk"),
            "max_position_usd": P(10, 1, 5000, 1, "Max per set ($)"),
            "min_book_size": P(20, 0, 1000, 5, "Min resting size (shares)")}),

    "s4": dict(
        name="Drift", hue="mild", fn=s4_drift,
        tagline="Late revisions, thin book",
        detail="Only wakes up inside the closing window, when forecast error has "
               "collapsed but limit orders have gone stale. Also reads the "
               "revision between successive model runs and trades the direction "
               "before the book catches up. Cuts positions the forecast turns "
               "against instead of holding to settlement.",
        params={
            "window_hours": P(14, 1, 72, 1, "Trade inside T-minus (h)"),
            "min_hours": P(0.5, 0, 12, 0.25, "Stop before close (h)"),
            "entry_confidence": P(0.40, 0.15, 0.99, 0.01, "Conviction threshold",
                                 "A 1F bucket rarely exceeds 0.50 even when the forecast is excellent"),
            "exit_confidence": P(0.26, 0.05, 0.90, 0.01, "Cut below"),
            "min_revision_f": P(1.0, 0.2, 6.0, 0.1, "Min forecast revision (F)"),
            "min_edge": P(0.05, 0.005, 0.30, 0.005, "Min edge"),
            "kelly_fraction": P(0.40, 0.02, 1.0, 0.01, "Kelly fraction"),
            "max_spread": P(0.12, 0.01, 0.40, 0.005, "Max bid-ask"),
            "book_take": P(0.7, 0.05, 1.0, 0.05, "Max share of top level"),
            **COMMON_RISK}),

    "s5": dict(
        name="Quote", hue="neutral", fn=s5_quote,
        tagline="Post, don't take",
        detail="Rests two-sided quotes around model fair value and never crosses "
               "the spread, so it pays no taker fee and stays inside the reward "
               "window to collect liquidity rebates. The spread widens with "
               "forecast uncertainty and skews against inventory so the book "
               "leans itself flat. It re-prices only when fair value moves, "
               "because churning quotes costs queue position. Fills are passive: "
               "someone has to come to it -- and the simulator now demands "
               "evidence that somebody did, which is min_rest_s and "
               "volume_share below.",
        params={
            # The fill simulator's honesty controls. The old version filled any
            # resting quote the moment lastTradePrice touched it, front of queue,
            # in full: 378 fills in three days and every one of them free. These
            # require the order to have rested first, and cap the fill at a share
            # of the volume that actually traded while it sat there.
            "min_rest_s": P(90, 0, 900, 15, "Min seconds resting before a fill",
                            "A quote posted this tick is at the back of the queue"),
            "volume_share": P(0.25, 0.02, 1.0, 0.01, "Share of traded volume we win",
                              "Of the volume that printed while the order rested"),
            "require_volume": P(1, 0, 1, 1, "Require a volume figure to fill",
                                "Off falls back to the print-only test, which is "
                                "how the fictional fills happened"),
            "base_half_spread": P(0.020, 0.002, 0.15, 0.001, "Base half-spread"),
            "min_half_spread": P(0.010, 0.001, 0.10, 0.001, "Min half-spread"),
            "uncertainty_mult": P(0.6, 0.0, 4.0, 0.05, "Widen per unit uncertainty"),
            "max_half_spread": P(0.045, 0.005, 0.20, 0.005, "Max half-spread",
                                 "Capped again by the market's reward window"),
            "requote_ticks": P(1, 0, 10, 1, "Re-price after this many ticks",
                               "Churning quotes loses queue position"),
            "min_inventory_to_offer": P(5, 1, 200, 1, "Min shares before offering"),
            "inventory_skew": P(0.8, 0.0, 3.0, 0.05, "Inventory skew"),
            "max_inventory_usd": P(6, 2, 3000, 1, "Max inventory ($)"),
            "clip_usd": P(3, 1, 500, 1, "Clip size ($)"),
            "pull_hours": P(6, 0.5, 48, 0.5, "Pull quotes inside (h)"),
            "max_hours": P(120, 6, 360, 6, "Max hours to close"),
            "max_uncertainty": P(0.15, 0.02, 0.60, 0.01, "Max model uncertainty",
                                "Stand aside when a 1F miss moves fair by more than this"),
            "min_market_price": P(0.06, 0.0, 0.40, 0.01, "Tail floor"),
            "max_spread_frac": P(0.25, 0.02, 1.0, 0.01, "Max spread as share of price"),
            "max_total_exposure": P(0.50, 0.05, 1.0, 0.05, "Max share of bankroll at risk"),
            "min_price": P(0.05, 0.01, 0.45, 0.01, "Don't quote below"),
            "max_price": P(0.95, 0.55, 0.99, 0.01, "Don't quote above"),
            "min_book_size": P(30, 0, 1000, 5, "Min resting size (shares)"),
            "max_position_usd": P(6, 1, 3000, 1, "Max per market ($)")}),

    "s6": dict(
        name="Observed", hue="warm", fn=s6_observed,
        tagline="Trade what has already happened",
        enabled=False,
        detail="OFF by default, and it earned that. It reads the highest "
               "temperature recorded so far today and treats it as a floor, but "
               "the reading is a gridpoint analysis and Polymarket resolves on a "
               "station: 12 of its 16 'impossible bucket' trades named a "
               "temperature above the day's actual high, and two of them "
               "produced $295 of phantom profit. It now needs the floor to clear "
               "the bucket top by a full station-gap margin and refuses cities "
               "whose bias has never been measured. Enable it once BIAS_* is "
               "populated from a real run and you want to test whether anything "
               "is left.",
        params={
            "margin_f": P(0.8, 0.0, 5.0, 0.1, "Safety margin (F)",
                          "Gap between analysis and the resolving station"),
            "hard_margin_f": P(2.5, 0.0, 8.0, 0.1, "Extra margin to call a bucket dead",
                               "On top of the safety margin. Must exceed the worst "
                               "gridpoint-to-station gap you have measured, or "
                               "'impossible' buckets keep resolving yes"),
            "require_measured_bias": P(1, 0, 1, 1, "Only cities with measured bias",
                                       "A hard floor in a city whose station gap is "
                                       "unknown is an unquantified error"),
            "max_obs_age_min": P(60, 10, 360, 5, "Max observation age (min)"),
            "min_profit": P(0.03, 0.005, 0.40, 0.005, "Min bid on a dead bucket"),
            "ramp_from_hour": P(11, 6, 20, 1, "Start tightening at local hour"),
            "late_hour": P(16, 10, 22, 1, "Fully tightened by local hour"),
            "late_sd_mult": P(0.5, 0.1, 1.0, 0.05, "Shrink uncertainty after the peak"),
            "min_edge": P(0.05, 0.005, 0.30, 0.005, "Min edge"),
            "kelly_fraction": P(0.40, 0.02, 1.0, 0.01, "Kelly fraction"),
            "max_spread": P(0.12, 0.01, 0.40, 0.005, "Max bid-ask"),
            "min_hours": P(0.5, 0, 24, 0.25, "Min hours to close"),
            "max_hours": P(24, 2, 96, 1, "Max hours to close"),
            "book_take": P(0.6, 0.05, 1.0, 0.05, "Max share of top level"),
            **COMMON_RISK}),

    "s7": dict(
        name="Fade", hue="hot", fn=s7_fade,
        tagline="Sell the wings",
        detail="Buys NO on the outermost buckets when they bid well above what "
               "the model says they are worth. It is short volatility, so it "
               "wins small and often and loses on the days that surprise "
               "everyone. Watch the open-fade cap rather than the win rate, and "
               "compare it against Edge before you trust it.",
        params={
            "edge_buckets": P(2, 1, 5, 1, "Wing buckets per side"),
            "max_fair": P(0.12, 0.02, 0.40, 0.01, "Only fade below this model prob"),
            "overprice_mult": P(1.8, 1.1, 6.0, 0.1, "Market must be this many x model"),
            "min_bid": P(0.04, 0.01, 0.30, 0.005, "Min bid worth fading"),
            "min_edge": P(0.03, 0.005, 0.30, 0.005, "Min edge"),
            "max_open_fades": P(6, 1, 40, 1, "Max concurrent short tails",
                                "These all lose together on a surprise day"),
            "min_legs": P(4, 2, 12, 1, "Min buckets in event"),
            "kelly_fraction": P(0.20, 0.02, 1.0, 0.01, "Kelly fraction"),
            "max_spread": P(0.10, 0.01, 0.40, 0.005, "Max bid-ask"),
            "min_hours": P(1, 0, 72, 0.5, "Min hours to close"),
            "max_hours": P(72, 2, 360, 6, "Max hours to close"),
            "book_take": P(0.4, 0.05, 1.0, 0.05, "Max share of top level"),
            **COMMON_RISK}),

    "s8": dict(
        name="Shape", hue="ember", fn=s8_shape,
        tagline="Unimodal ladder, no forecast",
        detail="A temperature distribution has one peak, so no bucket should be "
               "worth less than both of its neighbours. When one is, that is a "
               "pricing artefact rather than weather, and it can be bought "
               "without any view on the forecast at all. Ignores the two "
               "open-ended end buckets, where a real tail looks the same as a "
               "dip. Forecast-free like Dutch, but fires far more often.",
        params={
            "min_dip": P(0.04, 0.005, 0.40, 0.005, "Min dip below neighbours"),
            "min_market_price": P(0.04, 0.0, 0.30, 0.01, "Tail floor"),
            "neighbour_frac": P(0.95, 0.30, 1.00, 0.01, "Value as x lower neighbour",
                                "Unimodality implies at least the lower neighbour; "
                                "the haircut covers the argument being imperfect"),
            "min_neighbour": P(0.08, 0.01, 0.50, 0.01, "Ignore dips below this"),
            "min_legs": P(5, 3, 15, 1, "Min buckets in event"),
            "min_edge": P(0.02, 0.005, 0.30, 0.005, "Min edge", "Net of fees"),
            "kelly_fraction": P(0.30, 0.02, 1.0, 0.01, "Kelly fraction"),
            "max_spread": P(0.08, 0.01, 0.40, 0.005, "Max bid-ask"),
            "min_hours": P(1, 0, 72, 0.5, "Min hours to close"),
            "max_hours": P(96, 2, 360, 6, "Max hours to close"),
            "book_take": P(0.5, 0.05, 1.0, 0.05, "Max share of top level"),
            **COMMON_RISK}),
}

ORDER = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"]
