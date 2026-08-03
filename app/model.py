"""Turning a forecast into a fair price for a temperature bucket.

Polymarket temperature markets resolve on the integer high recorded at one
station. So a bucket labelled "84-85F" pays if the rounded high is 84 or 85,
which as a continuous interval is [83.5, 85.5). The half-degree continuity
correction matters more than it looks: on a tight bucket it moves fair value
by several cents.

The distribution is a kernel density over ensemble members rather than a single
normal. Ensembles go bimodal on frontal-passage days -- a normal fit prices the
gap between the modes as the most likely outcome, which is exactly backwards,
and that is the kind of day the market misprices too.
"""
import math
import os
import re
from datetime import datetime

INF = float("inf")

# Kernel width in F. Ensemble spread understates true error, because every
# member shares the same model physics and the same gridpoint-to-station gap.
# These three numbers decide how much edge the bot thinks it has, so they are
# the first thing to recalibrate against settled markets. Too narrow and it
# overtrades; too wide and it starts buying tails that never come in.
KERNEL_BASE = float(os.getenv("KERNEL_BASE", 0.65))
KERNEL_PER_DAY = float(os.getenv("KERNEL_PER_DAY", 0.30))
SPREAD_INFLATION = float(os.getenv("SPREAD_INFLATION", 1.15))
SPREAD_WEIGHT = float(os.getenv("SPREAD_WEIGHT", 0.55))


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


BELOW = ("or below", "or less", "or lower", "or cooler", "or colder", "below",
         "under", "at most", "less than", "lower than", "cooler than", "colder than")
ABOVE = ("or above", "or more", "or higher", "or hotter", "or warmer", "above",
         "over", "at least", "greater", "more than", "higher than", "hotter than",
         "warmer than", "exceed")
CELSIUS = re.compile(r"\d\s*(?:\u00b0\s*)?c\b|\bcelsius\b|\u00b0c")


def detect_unit(text: str):
    """'31C' -> C, '84-85F' -> F.

    London, Paris, Moscow and Seoul list their buckets in Celsius. Pricing a
    Celsius bucket against a Fahrenheit forecast makes every bucket look
    impossible and manufactures 30-point edges out of nothing, which is exactly
    what it did.
    """
    return "C" if CELSIUS.search((text or "").lower()) else "F"


def f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|\u2013|\u2014|to|through|and)\s*(\d+(?:\.\d+)?)")


def parse_bucket(text: str):
    """'84-85F' -> (84,85). '83 or below' -> (-inf,83). '90+' -> (90,inf)."""
    if not text:
        return None
    t = text.lower().replace("\u00b0", " ").replace("\u2212", "-")
    t = re.sub(r"(\d)\s*[fc]\b", r"\1", t)         # strip the unit, keep the digits
    t = t.strip()

    m = RANGE.search(t)                             # ranges first: the hyphen in
    if m:                                           # "84-85" is not a minus sign
        a, b = float(m.group(1)), float(m.group(2))
        return (min(a, b), max(a, b))

    nums = re.findall(r"-?\d+(?:\.\d+)?", t)
    if not nums:
        return None
    n = [float(x) for x in nums]
    if any(w in t for w in BELOW):
        return (-INF, n[0])
    if any(w in t for w in ABOVE) or t.endswith("+"):
        return (n[0], INF)
    if len(n) >= 2:
        return (min(n), max(n))
    return (n[0], n[0])


CITY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
MONTHS = {m.lower(): i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def parse_date(text: str, fallback_year=None):
    m = CITY_DATE.search(text or "")
    if m:
        return m.group(1)
    t = (text or "").lower()
    for name, num in MONTHS.items():
        for token in (name, name[:3]):
            mm = re.search(rf"{token}\w*\s+(\d{{1,2}})", t)
            if mm:
                y = fallback_year or datetime.utcnow().year
                return f"{y}-{num:02d}-{int(mm.group(1)):02d}"
    return None


def match_city(text: str):
    """Word-boundary match, so the alias 'la' cannot match Atlanta or Dallas."""
    from . import config
    t = (text or "").lower()
    for key, c in config.CITIES.items():
        for a in c["aliases"]:
            a = a.strip().lower()
            if a and re.search(r"\b" + re.escape(a) + r"\b", t):
                return key
    return None


def detect_metric(text: str):
    """'high' or 'low'. Polymarket runs both and the titles are near-identical."""
    q = (text or "").lower()
    if any(w in q for w in ("highest", "high temp", "warmest", "hottest")):
        return "high"
    if any(w in q for w in ("lowest", "low temp", "coolest", "coldest",
                            "minimum", "overnight", "min temp")):
        return "low"
    return None


def kernel_sd(ens_sd, lead_days, extra=0.0):
    lead = max(0.0, lead_days)
    return max(0.5, KERNEL_BASE + KERNEL_PER_DAY * lead + extra
               + SPREAD_WEIGHT * (ens_sd or 0))


def bucket_prob(lo, hi, members, sd):
    """P(rounded station high lands in [lo,hi]) under the ensemble kernel."""
    if not members:
        return None
    a = -INF if lo == -INF else lo - 0.5
    b = INF if hi == INF else hi + 0.5
    acc = 0.0
    for m in members:
        left = 0.0 if a == -INF else norm_cdf((a - m) / sd)
        right = 1.0 if b == INF else norm_cdf((b - m) / sd)
        acc += max(0.0, right - left)
    return min(1.0, max(0.0, acc / len(members)))


def fair_prices(buckets, fc, lead_days, extra_sd=0.0):
    """buckets: {market_id: (lo,hi)} -> {market_id: prob}, normalised to sum 1
    when the buckets tile the whole line (they always do on these events)."""
    sd = kernel_sd((fc.get("sd") or 1.5) * SPREAD_INFLATION, lead_days, extra_sd)
    raw = {k: bucket_prob(lo, hi, fc["members"], sd) for k, (lo, hi) in buckets.items()}
    raw = {k: v for k, v in raw.items() if v is not None}
    tot = sum(raw.values())
    if tot > 0.75:                       # buckets tile the outcome space
        raw = {k: v / tot for k, v in raw.items()}
    return raw, sd


MARKET_SD_WEIGHT = float(os.getenv("MARKET_SD_WEIGHT", 0.5))


def bucket_centre(lo, hi):
    """A representative temperature for a bucket, including open-ended ones."""
    if lo == -INF and hi == INF:
        return None
    if lo == -INF:
        return hi - 1.0
    if hi == INF:
        return lo + 1.0
    return (lo + hi) / 2.0


def market_implied_sd(buckets, prices):
    """The spread the order book is implying, in the market's own unit.

    Treat the normalised prices as a distribution over bucket centres and take
    its standard deviation. Needs at least three priced buckets to mean anything.
    """
    pts = []
    for k, (lo, hi) in buckets.items():
        p = prices.get(k)
        c = bucket_centre(lo, hi)
        if p is None or c is None or p <= 0:
            continue
        pts.append((c, p))
    tot = sum(p for _, p in pts)
    if len(pts) < 3 or tot <= 0:
        return None
    mean = sum(c * p for c, p in pts) / tot
    var = sum(p * (c - mean) ** 2 for c, p in pts) / tot
    return math.sqrt(max(var, 1e-6))


def total_sd(members, kernel):
    """Width of the whole predictive distribution, not just the smoothing kernel.

    The distribution is a kernel density over ensemble members, so its variance
    is the members' variance plus the kernel's. Shrinking only the kernel barely
    moves it -- which is exactly the mistake the first attempt at this made.
    """
    if not members:
        return kernel
    mean = sum(members) / len(members)
    var = sum((m - mean) ** 2 for m in members) / len(members)
    return math.sqrt(var + kernel * kernel)


def calibrate_width(members, kernel, market_sd, weight=None):
    """Scale the whole distribution toward the width the book implies.

    Member deviations shrink toward their own mean by the same factor as the
    kernel, so the ensemble *mean* -- the part carrying real information -- is
    untouched while the spread moves. Returns (members, kernel, before, after).
    """
    before = total_sd(members, kernel)
    if market_sd is None or before <= 1e-6 or not members:
        return members, kernel, before, before
    w = MARKET_SD_WEIGHT if weight is None else weight
    target = max(0.5, (1.0 - w) * before + w * market_sd)
    f = target / before
    if abs(f - 1.0) < 1e-6:
        return members, kernel, before, before
    mean = sum(members) / len(members)
    return ([mean + f * (m - mean) for m in members], kernel * f, before, target)


def blend_sd(model_sd, market_sd, weight=None):
    """Shrink the kernel toward the spread the market is implying.

    The model's genuine information is the ensemble *mean* -- where the
    distribution sits. Its width is a parameter I guessed, and guessing it too
    wide makes the model short every favourite in every event simultaneously:
    one bet on one number, dressed up as many independent trades.

    So until settled markets say otherwise, the width is anchored halfway to the
    market's. Set MARKET_SD_WEIGHT=0 to trust the prior completely, or 1 to
    adopt the market's width outright and trade only on location.
    """
    if market_sd is None:
        return model_sd
    w = MARKET_SD_WEIGHT if weight is None else weight
    return max(0.4, (1.0 - w) * model_sd + w * market_sd)


def kelly(p, price, side_yes=True):
    """Fraction of bankroll for a binary paying 1. price is the cost per share."""
    if price <= 0.001 or price >= 0.999:
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, min(1.0, f))


# Polymarket charges a taker fee on weather markets:
#   fee_per_share = rate * min(p, 1-p) ** exponent
# The live feeSchedule reads rate 0.05, takerOnly, exponent 1. At a 28c price
# that is 1.4c a share -- comparable to the entire edge most strategies look
# for, and more than the spread a market maker earns. Ignoring it does not make
# the bot slightly optimistic, it inverts the sign of several strategies.
FEE_RATE = float(os.getenv("FEE_RATE", 0.05))
FEE_EXP = float(os.getenv("FEE_EXP", 1.0))


def fee_per_share(price, rate=None, exponent=None):
    p = min(max(float(price), 0.0), 1.0)
    r = FEE_RATE if rate is None else rate
    e = FEE_EXP if exponent is None else exponent
    return r * (min(p, 1.0 - p) ** e)


def net_edge(edge, price, m=None):
    """Edge after the fee you will actually pay to take it."""
    if m:
        return edge - fee_per_share(price, m.get("fee_rate"), m.get("fee_exp"))
    return edge - fee_per_share(price)


def round_tick(price, tick):
    t = tick or 0.01
    return max(t, min(1 - t, round(price / t) * t))


def hours_left(end_ts):
    return max(0.0, (end_ts - datetime.utcnow().timestamp()) / 3600.0)
