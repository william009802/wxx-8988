"""Runtime configuration. Everything overridable by environment variable."""
import os

# Bump on every change you deploy. /api/version tells you what is actually
# running, which is the first thing to check when behaviour does not match the
# code you think you shipped.
VERSION = "0.16.1"
FEATURES = ["maker-fill-caps", "cost-basis-exposure", "mark-to-mid", "relative-spread-cap", "location-sanity-guard", "market-anchored-width", "side-concentration-cap", "forecast-fallback", "batched-weather", "region-correlation-cap", "nowcast-ramp", "city-whitelist", "8-strategies", "shape-ladder", "23-cities", "adaptive-slug-probe", "no-phantom-fills", "word-boundary-cities", "json-safe", "celsius-markets", "portfolio-cap", "slug-discovery", "per-tick-gamma-prices", "weather-fees",
            "s5-maker", "stdout-logs", "discovery-audit",
            # 0.16.0 -- everything below exists so a long unattended run
            # produces data you can actually trust at the end of it.
            "gamma-only-settlement", "settlement-repair", "stale-market-sweep",
            "volume-checked-maker-fills", "persistent-halt", "durable-db-guard",
            "tick-watchdog-health", "calibration-log", "s6-gated-off",
            # 0.16.1 -- the first live run settled nothing for 21 hours because
            # the only path that could book a winner asked Gamma a question it
            # answers with an empty list. Discovery now books what it sees.
            "settlement-from-discovery", "closure-query-fallback",
            "late-resolution-rescue"]


def _b(k, d=False):
    return os.getenv(k, str(d)).strip().lower() in ("1", "true", "yes", "on")


def _f(k, d):
    try:
        return float(os.getenv(k, d))
    except (TypeError, ValueError):
        return float(d)


def _i(k, d):
    try:
        return int(float(os.getenv(k, d)))
    except (TypeError, ValueError):
        return int(d)


# --- storage -----------------------------------------------------------------
# Sized for a Railway free-tier volume. The rule applied throughout: prune only
# what can be rebuilt or was never looked at; keep forever anything the
# end-of-run analysis needs. Raw book ticks for the ~900 markets we merely watch
# are the bulk of the file and the least useful part of it, so they get a short
# window and an hourly rollup. Everything we actually traded is kept in full.
DB_PATH = os.getenv("DB_PATH", "/data/wxalpha.db")
QUOTE_RETENTION_H = _i("QUOTE_RETENTION_H", 6)       # raw ticks, ALL markets
EQUITY_RETENTION_D = _i("EQUITY_RETENTION_D", 90)
FORECAST_RETENTION_D = _i("FORECAST_RETENTION_D", 45)
SIGNAL_RETENTION_D = _i("SIGNAL_RETENTION_D", 3)     # untaken signals only
LOG_ROWS = _i("LOG_ROWS", 3000)
ARCHIVE_ROLLED = _b("ARCHIVE_ROLLED", True)          # keep hourly rollups forever

# Held positions get their own high-resolution price track, kept for the entire
# run. This is what answers "would cashing out early have beaten holding to
# settlement" -- a question the old schema could not answer at all, because raw
# quotes were rolled to hourly averages after 48h and the exit path was gone.
# Written per market rather than per position: quantities and cost basis come
# from the trades table, so storing them again per tick would be redundant.
MARK_EVERY_S = _f("MARK_EVERY_S", 300)               # cadence per held market
MARK_MIN_MOVE = _f("MARK_MIN_MOVE", 0.005)           # ...or sooner if mid moves

# On a free-tier volume a full disk stops every write. Warn early, act at the
# ceiling, and never let a two-week run die silently at day nine.
MAX_DB_MB = _f("MAX_DB_MB", 400)
VACUUM_EVERY_H = _f("VACUUM_EVERY_H", 12)

# A multi-week run only means something if the database survives the container.
# On Railway that requires a volume mounted at the DB_PATH directory. Without
# one, /data is ordinary ephemeral overlay storage: writes succeed, nothing
# errors, and every row is destroyed by the next deploy. Losing a fortnight of
# data that way is the most expensive failure available here, so refuse to start
# instead. Set REQUIRE_DURABLE_DB=false only for a throwaway local run.
REQUIRE_DURABLE_DB = _b("REQUIRE_DURABLE_DB", True)

# The export is the whole point of the run, so it defaults to no compression:
# one click, plain CSV, openable without unpacking anything. Add ?compress=1 if
# you are pulling it over a slow connection.
EXPORT_COMPRESS = _b("EXPORT_COMPRESS", False)

# --- execution ---------------------------------------------------------------
LIVE_TRADING = _b("LIVE_TRADING", False)
START_BANKROLL = _f("START_BANKROLL", 800.0)         # split evenly across the strategies
MIN_SHARES = _f("MIN_SHARES", 5.0)                   # Polymarket rejects smaller orders
ASSUMED_SIZE = _f("ASSUMED_SIZE", 40.0)              # shares assumed at top of book
                                                     # when CLOB depth is unavailable
SLIPPAGE = _f("SLIPPAGE", 0.005)                     # paper fill penalty
POLL_SECONDS = _i("POLL_SECONDS", 45)
DISCOVER_EVERY = _i("DISCOVER_EVERY", 20)            # ticks between market discovery
FORECAST_EVERY = _i("FORECAST_EVERY", 25)            # ticks between weather refresh
DAILY_LOSS_LIMIT = _f("DAILY_LOSS_LIMIT", 0.12)      # halt at -12% of day-open equity

# --- settlement --------------------------------------------------------------
# Only Polymarket resolves a Polymarket market. The bot used to settle from the
# Open-Meteo daily maximum instead, because that number arrives hours earlier --
# and it wrote closed=1 before the authoritative verdict could land, so the real
# one never overwrote it. Over a 3-day run that scored 38 of 180 settlements
# against the wrong winner and turned a real -25.8% into a reported +70.6%.
#
# A settlement is now only booked when Gamma reports the market closed AND
# outcomePrices has converged. Until then the position stays open and marks to
# market, which is both honest and self-correcting.
SETTLE_MIN_CONVICTION = _f("SETTLE_MIN_CONVICTION", 0.95)
# A market Gamma has forgotten about. Nothing is booked -- it is only lifted out
# of the live set so it stops being priced and quoted.
STALE_MARKET_H = _f("STALE_MARKET_H", 72)

# --- health ------------------------------------------------------------------
# Railway restarts an unhealthy container. A wedged asyncio loop keeps answering
# HTTP perfectly while trading nothing, so /health has to assert that the tick
# is actually advancing, not merely that the process is alive.
HEALTH_MAX_TICK_AGE_S = _f("HEALTH_MAX_TICK_AGE_S", 900)

# --- polymarket --------------------------------------------------------------
GAMMA = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_URL", "https://clob.polymarket.com")
PK = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER = os.getenv("POLYMARKET_FUNDER", "")
SIG_TYPE = _i("POLYMARKET_SIG_TYPE", 1)              # 1 = email/magic proxy wallet
CHAIN_ID = _i("CHAIN_ID", 137)

# --- weather -----------------------------------------------------------------
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_MODELS = os.getenv("ENSEMBLE_MODELS", "ecmwf_ifs025,gfs025")
# The ensemble endpoint bills by models x members x hours. Seven days of hourly
# data across three models is ~120 members per city; sixteen of those at once
# gets most of them throttled, and a throttled city cannot be priced at all.
FORECAST_DAYS = _i("FORECAST_DAYS", 4)
WX_BATCH = _i("WX_BATCH", 4)
WX_PAUSE = _f("WX_PAUSE", 1.5)
DETERMINISTIC_MODELS = os.getenv(
    "DETERMINISTIC_MODELS", "ecmwf_ifs025,gfs_seamless,icon_seamless,gem_seamless,jma_seamless"
)
USER_AGENT = os.getenv("USER_AGENT", "wxalpha/1.0 (contact: set USER_AGENT env)")

# Cities Polymarket runs daily temperature markets on.
# bias_f = additive correction from gridpoint forecast to the resolving station.
# Tune these from the Data tab once you have a week of settled markets.
CITIES = {
    # lat/lon must be the RESOLVING STATION, not the city centre. Polymarket
    # settles NYC on LaGuardia (KLGA) via Wunderground, not Central Park; the
    # two routinely differ by 2-3F on a sea-breeze day.
    "nyc":     dict(slug=["nyc","new-york"], name="New York",    lat=40.7772, lon=-73.8726, tz="America/New_York",    bias_f=_f("BIAS_NYC", 0.0),  aliases=["new york", "nyc", "laguardia"]),
    "london":  dict(slug=["london"], name="London",      lat=51.5048, lon=0.0495,   tz="Europe/London",       bias_f=_f("BIAS_LON", 0.0),  aliases=["london"]),
    "moscow":  dict(slug=["moscow"], name="Moscow",      lat=55.7558, lon=37.6173,  tz="Europe/Moscow",       bias_f=_f("BIAS_MOW", 0.0),  aliases=["moscow"]),
    "seoul":   dict(slug=["seoul"], name="Seoul",       lat=37.5665, lon=126.9780, tz="Asia/Seoul",          bias_f=_f("BIAS_SEL", 0.0),  aliases=["seoul"]),
    "la":      dict(slug=["la","los-angeles"], name="Los Angeles", lat=33.9382, lon=-118.3866, tz="America/Los_Angeles", bias_f=_f("BIAS_LAX", 0.0), aliases=["los angeles", "la "]),
    "chicago": dict(slug=["chicago"], name="Chicago",     lat=41.9603, lon=-87.9316, tz="America/Chicago",     bias_f=_f("BIAS_ORD", 0.0),  aliases=["chicago"]),
    "miami":   dict(slug=["miami"], name="Miami",       lat=25.7906, lon=-80.3164, tz="America/New_York",    bias_f=_f("BIAS_MIA", 0.0),  aliases=["miami"]),
    "paris":   dict(slug=["paris"], name="Paris",       lat=48.7233, lon=2.3794,   tz="Europe/Paris",        bias_f=_f("BIAS_PAR", 0.0),  aliases=["paris"]),
    "atlanta":  dict(slug=["atlanta"], name="Atlanta", lat=33.6407, lon=-84.4277, tz="America/New_York", bias_f=_f("BIAS_ATL", 0.0), aliases=["atlanta"]),
    "dallas":   dict(slug=["dallas"], name="Dallas", lat=32.8998, lon=-97.0403, tz="America/Chicago", bias_f=_f("BIAS_DFW", 0.0), aliases=["dallas"]),
    "houston":  dict(slug=["houston"], name="Houston", lat=29.9902, lon=-95.3368, tz="America/Chicago", bias_f=_f("BIAS_IAH", 0.0), aliases=["houston"]),
    "phoenix":  dict(slug=["phoenix"], name="Phoenix", lat=33.4342, lon=-112.0116, tz="America/Phoenix", bias_f=_f("BIAS_PHX", 0.0), aliases=["phoenix"]),
    "denver":   dict(slug=["denver"], name="Denver", lat=39.8561, lon=-104.6737, tz="America/Denver", bias_f=_f("BIAS_DEN", 0.0), aliases=["denver"]),
    "boston":   dict(slug=["boston"], name="Boston", lat=42.3656, lon=-71.0096, tz="America/New_York", bias_f=_f("BIAS_BOS", 0.0), aliases=["boston"]),
    "dc":       dict(slug=["dc", "washington-dc", "washington"], name="Washington DC", lat=38.8512, lon=-77.0402, tz="America/New_York", bias_f=_f("BIAS_DCA", 0.0), aliases=["washington dc", "dc", "washington"]),
    "philly":   dict(slug=["philadelphia", "philly"], name="Philadelphia", lat=39.8729, lon=-75.2437, tz="America/New_York", bias_f=_f("BIAS_PHL", 0.0), aliases=["philadelphia", "philly"]),
    "seattle":  dict(slug=["seattle"], name="Seattle", lat=47.4502, lon=-122.3088, tz="America/Los_Angeles", bias_f=_f("BIAS_SEA", 0.0), aliases=["seattle"]),
    "sf":       dict(slug=["sf", "san-francisco"], name="San Francisco", lat=37.6213, lon=-122.3790, tz="America/Los_Angeles", bias_f=_f("BIAS_SFO", 0.0), aliases=["san francisco", "sf"]),
    "vegas":    dict(slug=["las-vegas", "vegas"], name="Las Vegas", lat=36.0840, lon=-115.1537, tz="America/Los_Angeles", bias_f=_f("BIAS_LAS", 0.0), aliases=["las vegas", "vegas"]),
    "tokyo":    dict(slug=["tokyo"], name="Tokyo", lat=35.5533, lon=139.7811, tz="Asia/Tokyo", bias_f=_f("BIAS_HND", 0.0), aliases=["tokyo"]),
    "berlin":   dict(slug=["berlin"], name="Berlin", lat=52.3667, lon=13.5033, tz="Europe/Berlin", bias_f=_f("BIAS_BER", 0.0), aliases=["berlin"]),
    "delhi":    dict(slug=["delhi", "new-delhi"], name="Delhi", lat=28.5665, lon=77.1031, tz="Asia/Kolkata", bias_f=_f("BIAS_DEL", 0.0), aliases=["delhi", "new delhi"]),
    "toronto":  dict(slug=["toronto"], name="Toronto", lat=43.6777, lon=-79.6248, tz="America/Toronto", bias_f=_f("BIAS_YYZ", 0.0), aliases=["toronto"]),
}


# Cities under one air mass move together. Ten positions across the Northeast on
# the same day are one weather bet, not ten, and a dollar cap alone will not
# notice. Taken from the old bot's region correlation guard, which is the single
# most useful idea in it.
REGIONS = {
    "nyc": "northeast", "boston": "northeast", "philly": "northeast",
    "dc": "northeast", "toronto": "northeast",
    "atlanta": "southeast", "miami": "southeast",
    "chicago": "midwest",
    "dallas": "texas", "houston": "texas",
    "phoenix": "southwest", "vegas": "southwest", "denver": "mountain",
    "la": "westcoast", "sf": "westcoast", "seattle": "westcoast",
    "london": "westeurope", "paris": "westeurope", "berlin": "westeurope",
    "moscow": "easteurope",
    "seoul": "eastasia", "tokyo": "eastasia", "delhi": "southasia",
}


def region(city):
    return REGIONS.get(city, "other")


# Once calibration tells you which cities the ensemble actually has skill on,
# put them here and the bot trades nothing else. Empty means trade everything.
CITY_WHITELIST = [w.strip() for w in os.getenv("CITY_WHITELIST", "").split(",")
                  if w.strip()]

# If the model's mean and the market's implied mean differ by more than this,
# something is wrong with the data rather than with the price. A 10F gap is not
# a trading view, it is a misconfigured station or a grid cell covering the
# wrong terrain, and betting the book on it loses money in one direction.
MAX_LOCATION_GAP_F = _f("MAX_LOCATION_GAP_F", 3.5)

MARKET_KEYWORDS = [w.strip() for w in os.getenv(
    "MARKET_KEYWORDS", "highest temperature,warmest,hottest"
).split(",") if w.strip()]

# The pricing model forecasts the daily MAXIMUM. Polymarket also runs daily
# minimum markets with near-identical titles, and pricing those off a max
# forecast would be confidently wrong rather than merely wrong. Excluded until
# the model grows a daily-min path.
EXCLUDE_KEYWORDS = [w.strip() for w in os.getenv(
    "EXCLUDE_KEYWORDS", "lowest temperature,minimum temperature,coldest,low temp"
).split(",") if w.strip()]

# Gamma tag slugs worth sweeping directly; far more reliable than text search.
TAG_HINTS = [w.strip() for w in os.getenv(
    "TAG_HINTS", "weather,daily-temperature,temperature,climate"
).split(",") if w.strip()]
SWEEP_PAGES = _i("SWEEP_PAGES", 6)
GAMMA_PAGE = _i("GAMMA_PAGE", 100)      # Gamma caps a page at 100 regardless of limit
SLUG_DAYS = _i("SLUG_DAYS", 4)          # days ahead to probe by slug

# Polymarket event slugs are deterministic, e.g.
#   highest-temperature-in-nyc-on-july-27-2026
# so they can be constructed and looked up directly. That path needs no search,
# no tags and no pagination, which is why it is tried first.
SLUG_TEMPLATES = [w.strip() for w in os.getenv(
    "SLUG_TEMPLATES",
    "highest-temperature-in-{city}-on-{month}-{day}-{year}|"
    "highest-temperature-in-{city}-on-{month}-{day}"
).split("|") if w.strip()]