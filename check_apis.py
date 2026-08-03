#!/usr/bin/env python3
"""Which of the bot's data sources can this machine actually reach?

Standard library only, no install needed:

    python3 check_apis.py

Run it on your PC and again on the server. They can differ — the bot only needs
the *server* to have access.
"""
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

TESTS = [
    ("Gamma  (market discovery)", "REQUIRED",
     "https://gamma-api.polymarket.com/events?limit=1&active=true&closed=false"),
    ("CLOB   (order book depth)", "optional",
     "https://clob.polymarket.com/ok"),
    ("Polymarket website", "not used by the bot",
     "https://polymarket.com/"),
    ("Open-Meteo ensemble", "REQUIRED",
     "https://ensemble-api.open-meteo.com/v1/ensemble"
     "?latitude=40.78&longitude=-73.87&hourly=temperature_2m&models=gfs025"
     "&forecast_days=1"),
    ("Open-Meteo forecast", "REQUIRED",
     "https://api.open-meteo.com/v1/forecast"
     "?latitude=40.78&longitude=-73.87&daily=temperature_2m_max&forecast_days=1"),
]


def resolve(host):
    try:
        return socket.gethostbyname(host)
    except socket.gaierror as e:
        return f"DNS FAILED ({e.strerror or e})"


def probe(url, timeout=15):
    host = url.split("/")[2]
    ip = resolve(host)
    if isinstance(ip, str) and ip.startswith("DNS FAILED"):
        return "BLOCKED", ip, None
    req = urllib.request.Request(url, headers={
        "User-Agent": "wxalpha-check/1.0", "Accept": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400)
            ms = int((time.time() - t0) * 1000)
            return "OK", f"http {r.status}, {ms} ms, via {ip}", body
    except urllib.error.HTTPError as e:
        ms = int((time.time() - t0) * 1000)
        # 403 and 407 are what a filtering proxy or a geoblock returns. DNS
        # resolving and TCP connecting proves nothing on its own -- a block page
        # answers just as fast as the real API.
        if e.code in (403, 407, 451):
            return "BLOCKED", (f"http {e.code} - geoblock, proxy or firewall "
                               f"(host resolved to {ip})"), None
        state = "REACH" if e.code < 500 else "ERROR"
        return state, f"http {e.code}, {ms} ms, via {ip}", None
    except (urllib.error.URLError, ssl.SSLError, socket.timeout, OSError) as e:
        reason = getattr(e, "reason", e)
        return "BLOCKED", f"{type(e).__name__}: {reason} (dns ok, ip {ip})", None


def gamma_only():
    """Exit 0 if the market API is reachable, 1 if not. For scripts."""
    state, detail, _ = probe(TESTS[0][2], timeout=12)
    print(f"Polymarket API: {state} - {detail}")
    return 0 if state == "OK" else 1


def main():
    if "--gamma-only" in sys.argv:
        return gamma_only()
    print("\nwxalpha connectivity check")
    print("=" * 68)
    results = {}
    for name, need, url in TESTS:
        state, detail, body = probe(url)
        results[name] = (state, need)
        mark = {"OK": " ok  ", "BLOCKED": "BLOCK", "REACH": "reach",
                "ERROR": " err "}[state]
        print(f"[{mark}] {name:<28} {need}")
        print(f"         {detail}")
        if state == "OK" and body and "gamma" in url:
            try:
                data = json.loads(body.decode("utf-8", "replace") + "]"
                                  if body.rstrip().endswith(b",") else body)
                if isinstance(data, list) and data:
                    print(f"         sample event: "
                          f"{str(data[0].get('title'))[:44]}")
            except (ValueError, AttributeError, TypeError):
                pass
    print("=" * 68)

    gamma_ok = results["Gamma  (market discovery)"][0] == "OK"
    wx_ok = all(results[k][0] == "OK" for k in results
                if k.startswith("Open-Meteo"))

    if gamma_ok and wx_ok:
        print("\nThis machine can run the bot. Start it with:")
        print("  DB_PATH=./wxalpha.db uvicorn app.main:app --port 8000")
    elif wx_ok and not gamma_ok:
        print("\nWeather works, Polymarket does not reach from here.")
        print("The bot cannot discover markets on this machine, but that is a")
        print("property of this network, not of the code. Options:")
        print("  1. Run selftest.py locally to validate the code offline, and")
        print("     do all live verification on the deployed server instead.")
        print("  2. Check whether Polymarket is restricted in your jurisdiction")
        print("     before funding anything -- that is a legal question about")
        print("     your situation, not a technical one.")
    elif gamma_ok and not wx_ok:
        print("\nPolymarket reachable, weather APIs are not. Check that")
        print("USER_AGENT is set; Open-Meteo throttles anonymous traffic.")
    else:
        print("\nNothing reachable. Check your internet connection first.")
    print()
    return 0 if (gamma_ok and wx_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
