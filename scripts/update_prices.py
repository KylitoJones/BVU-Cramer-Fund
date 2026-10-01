"""
Refresh data/prices.json with one year of daily closing prices for every
ticker the site needs. Run by .github/workflows/update-prices.yml each
weekday after the close, or by hand:  python scripts/update_prices.py

Tickers are discovered from index.html so there is nothing to keep in sync:
  - every  sym:"XXXX"  entry in the HOLDINGS array
  - every ticker traded in the TRADES ledger within the last 400 days
  - SPY (benchmark)
"""
import datetime as dt
import json
import pathlib
import re
import sys
import time

import pandas as pd
import yfinance as yf

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "index.html"
OUT = ROOT / "data" / "prices.json"


def discover_tickers(html: str) -> list[str]:
    syms = set(re.findall(r'sym:"([^"]+)"', html))
    m = re.search(r"const TRADES = (\[.*?\]);", html, re.S)
    if m:
        cutoff = (dt.date.today() - dt.timedelta(days=400)).isoformat()
        for trade in json.loads(m.group(1)):
            if trade[0] >= cutoff:
                syms.add(trade[2])
    syms.add("SPY")
    return sorted(syms)


def to_yahoo(sym: str) -> str:
    return sym.replace("/", "-")  # BRK/B -> BRK-B


def fetch_batch(yahoo_syms: list[str]) -> dict[str, pd.Series]:
    """Split-adjusted daily closes (not dividend-adjusted) for the last year."""
    out: dict[str, pd.Series] = {}
    df = yf.download(
        yahoo_syms, period="1y", interval="1d", auto_adjust=False,
        group_by="ticker", threads=True, progress=False,
    )
    if df is None or df.empty:
        return out
    if len(yahoo_syms) == 1:  # single-ticker frames are not grouped
        s = df["Close"].dropna()
        if len(s):
            out[yahoo_syms[0]] = s
        return out
    for ys in yahoo_syms:
        try:
            s = df[ys]["Close"].dropna()
        except KeyError:
            continue
        if len(s):
            out[ys] = s
    return out


def fetch_single(ys: str) -> pd.Series | None:
    for attempt in range(3):
        try:
            h = yf.Ticker(ys).history(period="1y", interval="1d", auto_adjust=False)
            s = h["Close"].dropna()
            if len(s):
                return s
        except Exception as e:  # noqa: BLE001
            print(f"  {ys}: attempt {attempt + 1} failed: {e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return None


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    tickers = discover_tickers(html)
    mapping = {t: to_yahoo(t) for t in tickers}
    print(f"Fetching {len(tickers)} tickers: {' '.join(tickers)}")

    series = fetch_batch(list(mapping.values()))
    missing = [ys for ys in mapping.values() if ys not in series]
    if missing:
        print(f"Retrying {len(missing)} individually: {' '.join(missing)}")
        for ys in missing:
            s = fetch_single(ys)
            if s is not None:
                series[ys] = s

    if "SPY" not in series:
        print("ERROR: no SPY data — aborting without writing", file=sys.stderr)
        return 1

    # Master calendar = union of all trading days seen.
    all_days = sorted({d.date() for s in series.values() for d in s.index})
    dates = [d.isoformat() for d in all_days]
    idx = {d: i for i, d in enumerate(all_days)}

    out_tickers: dict[str, list] = {}
    for sym, ys in mapping.items():
        s = series.get(ys)
        if s is None:
            print(f"WARNING: no data for {sym}", file=sys.stderr)
            continue
        row: list = [None] * len(dates)
        for d, v in s.items():
            row[idx[d.date()]] = round(float(v), 4)
        out_tickers[sym] = row

    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance via yfinance; split-adjusted daily closes",
        "dates": dates,
        "tickers": out_tickers,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUT} — {len(out_tickers)} tickers, {len(dates)} days, latest {dates[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
