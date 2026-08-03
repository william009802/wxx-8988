"""SQLite store. Small on disk, complete on export."""
import csv
import io
import json
import os
import sqlite3
import time
import zipfile
from contextlib import contextmanager

from . import config

_conn = None

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS markets(
  id TEXT PRIMARY KEY, event_id TEXT, event_title TEXT, question TEXT,
  city TEXT, target_date TEXT, lo REAL, hi REAL,
  token_yes TEXT, token_no TEXT, end_ts REAL, closed INTEGER DEFAULT 0,
  winner INTEGER, winner_src TEXT, winner_ts REAL, seen_ts REAL, updated_ts REAL,
  resolve_ts REAL, g_bid REAL, g_ask REAL, tick REAL, min_size REAL,
  fee_rate REAL, fee_exp REAL, neg_risk INTEGER,
  last_trade REAL, reward_spread REAL, reward_min_size REAL, unit TEXT);

CREATE TABLE IF NOT EXISTS quotes(
  ts REAL, market_id TEXT, bid REAL, ask REAL, mid REAL,
  bid_sz REAL, ask_sz REAL, fair REAL);
CREATE INDEX IF NOT EXISTS ix_q ON quotes(market_id, ts);

CREATE TABLE IF NOT EXISTS quotes_hourly(
  hour INTEGER, market_id TEXT, bid REAL, ask REAL, mid REAL, fair REAL, n INTEGER,
  PRIMARY KEY(hour, market_id));

-- Full-resolution price track for markets we are actually holding, kept for the
-- whole run and never rolled up. Two questions need it and nothing else can
-- answer them after the fact:
--   1. Would cashing out early have beaten holding to settlement? That needs
--      the bid we could have hit at each point, not an hourly average of it.
--   2. Where did a position's mark actually travel between entry and
--      resolution? An hourly mean hides the excursion entirely.
-- Only markets with an open position are recorded, and only every MARK_EVERY_S
-- or when the mid moves by MARK_MIN_MOVE, which is what keeps this affordable
-- on a free-tier volume while raw quotes for the other ~900 markets do not.
CREATE TABLE IF NOT EXISTS mark_ticks(
  ts REAL, market_id TEXT, bid REAL, ask REAL, mid REAL, fair REAL,
  bid_sz REAL, ask_sz REAL, hours_left REAL);
CREATE INDEX IF NOT EXISTS ix_mt ON mark_ticks(market_id, ts);

CREATE TABLE IF NOT EXISTS forecasts(
  ts REAL, city TEXT, target_date TEXT, mean_f REAL, sd_f REAL,
  members INTEGER, model_spread REAL, members_json TEXT);
CREATE INDEX IF NOT EXISTS ix_f ON forecasts(city, target_date, ts);

CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, strat TEXT, market_id TEXT,
  question TEXT, fair REAL, market_p REAL, edge REAL, action TEXT,
  size REAL, taken INTEGER, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_s ON signals(strat, ts);

CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, strat TEXT, market_id TEXT,
  question TEXT, side TEXT, outcome TEXT, price REAL, qty REAL, cash REAL,
  fee REAL, mode TEXT, note TEXT, order_id TEXT);
CREATE INDEX IF NOT EXISTS ix_t ON trades(strat, ts);

CREATE TABLE IF NOT EXISTS positions(
  strat TEXT, market_id TEXT, outcome TEXT, qty REAL, avg_price REAL,
  opened_ts REAL, PRIMARY KEY(strat, market_id, outcome));

CREATE TABLE IF NOT EXISTS quotes_open(
  strat TEXT, market_id TEXT, outcome TEXT, side TEXT, price REAL, qty REAL,
  ts REAL, order_id TEXT, PRIMARY KEY(strat, market_id, outcome, side));

CREATE TABLE IF NOT EXISTS equity(
  ts REAL, strat TEXT, cash REAL, pos_value REAL, equity REAL,
  realized REAL, unrealized REAL);
CREATE INDEX IF NOT EXISTS ix_e ON equity(strat, ts);

CREATE TABLE IF NOT EXISTS settlements(
  ts REAL, strat TEXT, market_id TEXT, question TEXT, outcome TEXT,
  qty REAL, avg_price REAL, payout REAL, pnl REAL);

CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, strat TEXT, msg TEXT);

CREATE TABLE IF NOT EXISTS discovery(
  ts REAL, event_id TEXT, title TEXT, accepted INTEGER, buckets INTEGER, reason TEXT,
  PRIMARY KEY(event_id));

CREATE TABLE IF NOT EXISTS settled_highs(
  city TEXT, target_date TEXT, high_f REAL, ts REAL,
  PRIMARY KEY(city, target_date));

-- One row per market Polymarket has actually resolved, written at the moment
-- the verdict lands. This is the calibration artefact the whole observation run
-- exists to produce: what the model said, what the book said, and what won --
-- captured together so none of it has to be reconstructed afterwards from
-- retained quote ticks that may already have been rolled up.
CREATE TABLE IF NOT EXISTS resolutions(
  market_id TEXT PRIMARY KEY, ts REAL, city TEXT, target_date TEXT,
  question TEXT, unit TEXT, lo REAL, hi REAL, winner INTEGER, winner_src TEXT,
  observed_high_f REAL, fc_mean_f REAL, fc_sd_f REAL, fc_lead_h REAL,
  model_fair REAL, market_mid REAL, kernel_sd REAL);
CREATE INDEX IF NOT EXISTS ix_r ON resolutions(city, target_date);

CREATE TABLE IF NOT EXISTS cfg(k TEXT PRIMARY KEY, v TEXT);
"""


MIGRATIONS = [
    ("markets", "resolve_ts REAL"), ("markets", "g_bid REAL"),
    ("markets", "g_ask REAL"), ("markets", "tick REAL"),
    ("markets", "min_size REAL"), ("markets", "fee_rate REAL"),
    ("markets", "fee_exp REAL"), ("markets", "neg_risk INTEGER"),
    ("trades", "fee_rate REAL"), ("trades", "role TEXT"),
    ("markets", "last_trade REAL"), ("markets", "reward_spread REAL"),
    ("markets", "reward_min_size REAL"), ("markets", "unit TEXT"),
    ("markets", "metric TEXT"), ("quotes_open", "seen_last REAL"),
    ("settlements", "actual_high REAL"),
    # 0.16.0: where a winner came from, so an observation-derived guess can
    # never again be mistaken for Polymarket's own resolution.
    ("markets", "winner_src TEXT"), ("markets", "winner_ts REAL"),
    ("settlements", "winner_src TEXT"),
    # 0.16.0: maker fills now need evidence somebody actually traded.
    ("quotes_open", "seen_volume REAL"), ("quotes_open", "posted_ts REAL"),
    ("markets", "volume_num REAL"),
]


def migrate(conn):
    """Add columns to databases created by an earlier version. CREATE TABLE IF
    NOT EXISTS will not do it, and silently running without them is worse."""
    for table, col in MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass          # already there
    conn.commit()


class EphemeralStorage(RuntimeError):
    """The database directory is not backed by anything that survives a restart."""


def _durable(path):
    """Is this path on a filesystem that outlives the container?

    On Railway a volume must be mounted at the DB directory. If it is not, the
    old code happily created /data on the overlay filesystem -- makedirs
    succeeds, sqlite opens, every write lands, and the whole database is
    discarded by the next deploy. Nothing anywhere reported a problem. For a
    two-week unattended run that is the worst possible failure mode, because you
    only discover it at the end, when the data you were collecting is gone.

    A mounted volume has a different device number from the image root. That is
    the check: same device as "/" means ephemeral.
    """
    d = os.path.dirname(os.path.abspath(path)) or "/"
    if not os.path.isdir(d):
        return False, f"{d} does not exist"
    try:
        if os.stat(d).st_dev == os.stat("/").st_dev:
            return False, (f"{d} is on the same device as /, so it is container "
                           f"filesystem, not a mounted volume")
    except OSError as e:
        return False, f"cannot stat {d}: {e}"
    if not os.access(d, os.W_OK):
        return False, f"{d} is not writable"
    return True, ""


def connect():
    global _conn
    if _conn:
        return _conn
    path = config.DB_PATH
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            path = "./wxalpha.db"
            config.DB_PATH = path
    if config.REQUIRE_DURABLE_DB:
        ok, why = _durable(path)
        if not ok:
            raise EphemeralStorage(
                f"refusing to start: {why}.\n"
                f"  DB_PATH={path}\n"
                f"  On Railway: add a Volume to this service and set its mount "
                f"path to '{os.path.dirname(os.path.abspath(path)) or '/data'}'.\n"
                f"  Without it the database is destroyed on every deploy, and a "
                f"long data-collection run silently produces nothing.\n"
                f"  Set REQUIRE_DURABLE_DB=false to override (local runs only).")
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _conn.commit()
    migrate(_conn)
    return _conn


@contextmanager
def tx():
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def q(sql, args=()):
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = connect().execute(sql, args).fetchone()
    return dict(r) if r else None


def ex(sql, args=()):
    with tx() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def log(level, msg, strat=None):
    """Write to the database and to stdout.

    The dashboard reads the table; Railway's log viewer only sees stdout. Losing
    half your diagnostics depending on where you happen to be looking is a bad
    way to run a bot.
    """
    text = str(msg)[:600]
    ex("INSERT INTO logs(ts,level,strat,msg) VALUES(?,?,?,?)",
       (time.time(), level, strat, text))
    try:
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        tag = f"[{strat}] " if strat else ""
        print(f"{stamp} {level.upper():<5} {tag}{text}", flush=True)
    except Exception:
        pass


# --- config kv ---------------------------------------------------------------
def cfg_get(k, default=None):
    r = one("SELECT v FROM cfg WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def cfg_set(k, v):
    ex("INSERT INTO cfg(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
       (k, json.dumps(v)))


# --- retention ---------------------------------------------------------------
# Never pruned, at any age. These are the run's output rather than its telemetry,
# and a two-week collection run that quietly deleted its own findings on day
# eight would be worse than one that never started.
PERMANENT = ["trades", "positions", "settlements", "resolutions", "settled_highs",
             "mark_ticks", "quotes_hourly", "discovery", "cfg"]


def compact():
    """Roll raw ticks into hourly bars, then drop the raw ticks.

    Retention is set by what the end-of-run analysis needs, not by disk. What
    gets pruned is the raw book tick for every market we merely watched: ~900
    markets at 45s intervals is the overwhelming bulk of the file, it is
    averaged into quotes_hourly before deletion, and nothing in the analysis
    reads it. What survives untouched is everything we traded (trades,
    mark_ticks, settlements, resolutions) and everything needed to calibrate
    (forecasts, settled_highs).
    """
    now = time.time()
    cut = now - config.QUOTE_RETENTION_H * 3600
    with tx() as c:
        if config.ARCHIVE_ROLLED:
            c.execute("""
              INSERT INTO quotes_hourly(hour,market_id,bid,ask,mid,fair,n)
              SELECT CAST(ts/3600 AS INTEGER), market_id, AVG(bid), AVG(ask),
                     AVG(mid), AVG(fair), COUNT(*)
              FROM quotes WHERE ts < ? GROUP BY 1,2
              ON CONFLICT(hour,market_id) DO UPDATE SET
                bid=excluded.bid, ask=excluded.ask, mid=excluded.mid,
                fair=excluded.fair, n=quotes_hourly.n+excluded.n""", (cut,))
        c.execute("DELETE FROM quotes WHERE ts < ?", (cut,))
        c.execute("DELETE FROM equity WHERE ts < ?",
                  (now - config.EQUITY_RETENTION_D * 86400,))
        c.execute("DELETE FROM forecasts WHERE ts < ?",
                  (now - config.FORECAST_RETENTION_D * 86400,))
        # Signals that were acted on are kept forever -- they are the audit trail
        # behind every trade. Only the rejected ones age out.
        c.execute("DELETE FROM signals WHERE taken=0 AND ts < ?",
                  (now - config.SIGNAL_RETENTION_D * 86400,))
        c.execute("DELETE FROM logs WHERE id NOT IN "
                  "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (config.LOG_ROWS,))


_last_vacuum = [0.0]


def maintain():
    """Return freed pages to the filesystem, and complain before it fills.

    SQLite does not shrink the file when rows are deleted; the pages are only
    marked reusable. Compaction therefore keeps the tables small while the file
    on the volume keeps growing, and a full free-tier volume stops every write
    mid-run. WAL truncation matters for the same reason: a busy write loop can
    leave a -wal file larger than the database itself.
    """
    mb = db_size_mb()
    if time.time() - _last_vacuum[0] < config.VACUUM_EVERY_H * 3600:
        return mb
    _last_vacuum[0] = time.time()
    try:
        c = connect()
        c.commit()
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # VACUUM needs room for a second copy, so skip it if that would be the
        # thing that fills the volume. Checkpointing alone still helps.
        if mb < config.MAX_DB_MB / 2:
            c.execute("VACUUM")
        after = db_size_mb()
        if after < mb - 1:
            log("info", f"vacuum: {mb} -> {after} MB")
        mb = after
    except sqlite3.Error as e:
        log("warn", f"vacuum: {e}")
    if mb > config.MAX_DB_MB:
        log("error", f"database is {mb} MB, past the {config.MAX_DB_MB:.0f} MB "
                     f"ceiling. Lower QUOTE_RETENTION_H (currently "
                     f"{config.QUOTE_RETENTION_H}h) or enlarge the volume; "
                     f"writes stop when it fills.")
    elif mb > config.MAX_DB_MB * 0.75:
        log("warn", f"database is {mb} MB of a {config.MAX_DB_MB:.0f} MB budget")
    return mb


def db_size_mb():
    try:
        return round(os.path.getsize(config.DB_PATH) / 1e6, 2)
    except OSError:
        return 0.0


# --- export ------------------------------------------------------------------
TABLES = ["markets", "resolutions", "mark_ticks", "quotes", "quotes_hourly",
          "forecasts", "signals", "trades", "positions", "quotes_open", "equity",
          "settlements", "settled_highs", "discovery", "logs", "cfg"]


def export_zip(compress=None) -> bytes:
    """Every row of every table as CSV, plus a raw copy of the database.

    Stored uncompressed by default. The archive is the deliverable at the end of
    a two-week run, and an uncompressed one opens instantly in anything --
    Excel, pandas, a text editor -- with no unpacking step between you and the
    numbers. Pass compress=True over a slow link; CSV deflates roughly tenfold.
    """
    if compress is None:
        compress = config.EXPORT_COMPRESS
    connect().commit()
    try:
        connect().execute("PRAGMA wal_checkpoint(FULL)")   # fold the -wal in first
    except sqlite3.Error:
        pass
    buf = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    kw = dict(compresslevel=6) if compress else {}
    counts = {}
    with zipfile.ZipFile(buf, "w", mode, **kw) as z:
        for t in TABLES:
            s = io.StringIO()
            try:
                cur = connect().execute(f"SELECT * FROM {t}")
            except sqlite3.Error:
                continue                       # table from a future/older schema
            w = csv.writer(s)
            w.writerow([d[0] for d in cur.description])
            n = 0
            for row in cur:
                w.writerow(list(row))
                n += 1
            counts[t] = n
            z.writestr(f"csv/{t}.csv", s.getvalue())
        try:
            with open(config.DB_PATH, "rb") as f:
                z.writestr("wxalpha.db", f.read())
        except OSError:
            pass
        rows = "\n".join(f"  {t:<14} {counts.get(t, 0):>9,} rows" for t in TABLES)
        z.writestr("README.txt",
                   "wxalpha export\n"
                   f"generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
                   f"compression: {'deflate' if compress else 'none (stored)'}\n\n"
                   f"{rows}\n\n"
                   "csv/       one file per table, complete\n"
                   "wxalpha.db raw SQLite file, open with any client\n\n"
                   "READ THESE FIRST\n"
                   "  resolutions   one row per market Polymarket actually resolved:\n"
                   "                winning bucket, the forecast at the time, the\n"
                   "                book's mid, and where the verdict came from.\n"
                   "                winner_src='gamma' is authoritative. Anything\n"
                   "                else was never Polymarket's own answer.\n"
                   "  mark_ticks    full-resolution price track for every market we\n"
                   "                held. Join to trades to reconstruct what any\n"
                   "                cash-out rule would have returned instead of\n"
                   "                holding to settlement.\n"
                   "  settlements   realised P&L per strategy per market.\n"
                   "  settled_highs the temperature actually recorded per city per\n"
                   "                day. Join to forecasts for per-city bias.\n\n"
                   "SUPPORTING\n"
                   "  trades        every fill, with role=maker/taker and fees\n"
                   "  signals       every evaluation; taken=1 became a trade, and\n"
                   "                taken=0 rows age out after a few days\n"
                   "  forecasts     ensemble mean/sd and raw members per run\n"
                   "  quotes        raw book ticks, recent window, ALL markets\n"
                   "  quotes_hourly hourly rollup of raw ticks once they age out\n"
                   "  discovery     every weather event seen and why it was taken\n")
    return buf.getvalue()
