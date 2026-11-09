"""Moving average crossover trading bot for an Alpaca PAPER trading account.

    python bot.py            # check for a crossover and place an order if found
    python bot.py --dry-run  # same check, but never places an order
    python bot.py --at-open  # scheduled mode: trade at the 9:30 AM ET open, once per day

Settings live in config.json; API keys live in .env; every run is logged to bot.log.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
MARKET_TZ = "America/New_York"
LAST_RUN_FILE = BASE_DIR / "last_run.txt"  # ET date of the last completed --at-open run
MAX_WAIT_FOR_OPEN = 15 * 60  # seconds the bot will wait if started just before the open

log = logging.getLogger("ma_bot")


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    handlers = [logging.FileHandler(BASE_DIR / "bot.log", encoding="utf-8")]
    if sys.stdout is not None:  # None when run windowless via pythonw (Task Scheduler)
        handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(fmt)
        log.addHandler(handler)


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    required = ("symbol", "short_window", "long_window", "trade_size")
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(f"config.json is missing: {', '.join(missing)}")

    cfg["symbol"] = str(cfg["symbol"]).upper().strip()
    cfg["short_window"] = int(cfg["short_window"])
    cfg["long_window"] = int(cfg["long_window"])
    cfg["trade_size"] = int(cfg["trade_size"])
    cfg["history_days"] = int(cfg.get("history_days", 100))

    if cfg["short_window"] >= cfg["long_window"]:
        raise ValueError("short_window must be smaller than long_window")
    if cfg["trade_size"] <= 0:
        raise ValueError("trade_size must be at least 1")
    # Need two days with a valid long average to compare consecutive days.
    if cfg["history_days"] < cfg["long_window"] + 1:
        raise ValueError("history_days must be at least long_window + 1")
    return cfg


# --------------------------------------------------------------------------- #
# Market data and signal
# --------------------------------------------------------------------------- #
def fetch_closes(symbol: str, days: int) -> pd.Series:
    """Return the last `days` completed daily closing prices from yfinance."""
    # ~252 trading days per 365 calendar days, plus a buffer for holidays.
    start = (pd.Timestamp.now(tz=MARKET_TZ) - timedelta(days=int(days * 1.6) + 10)).date()
    data = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d", auto_adjust=True)
    if data.empty:
        raise RuntimeError(f"yfinance returned no data for {symbol}")

    closes = data["Close"].dropna()

    # While the market is open, yfinance includes today's unfinished bar.
    # A crossover must be between two *completed* days, so drop it.
    now_et = pd.Timestamp.now(tz=MARKET_TZ)
    if closes.index[-1].date() == now_et.date() and now_et.hour < 16:
        closes = closes.iloc[:-1]

    closes = closes.tail(days)
    if len(closes) < days:
        log.warning("Only %d trading days of data available (wanted %d)", len(closes), days)
    return closes


def add_moving_averages(closes: pd.Series, short: int, long: int) -> pd.DataFrame:
    df = pd.DataFrame({"close": closes})
    df["sma_short"] = df["close"].rolling(short).mean()
    df["sma_long"] = df["close"].rolling(long).mean()
    df["diff"] = df["sma_short"] - df["sma_long"]
    return df


def find_crossovers(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where the short MA crossed the long MA versus the previous day.

    buy:  short was at/below long yesterday and is above it today.
    sell: short was at/above long yesterday and is below it today.
    """
    diff = df["diff"].dropna()
    prev = diff.shift(1)
    signal = pd.Series(None, index=diff.index, dtype=object)
    signal[(prev <= 0) & (diff > 0)] = "buy"
    signal[(prev >= 0) & (diff < 0)] = "sell"
    out = df.loc[signal.dropna().index].copy()
    out["signal"] = signal.dropna()
    return out


# --------------------------------------------------------------------------- #
# Alpaca
# --------------------------------------------------------------------------- #
def get_trading_client():
    from alpaca.trading.client import TradingClient

    load_dotenv(BASE_DIR / ".env")
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret or key.startswith("your_") or secret.startswith("your_"):
        raise SystemExit("Add your Alpaca paper API keys to .env (see .env.example).")
    # paper=True is hard-coded: this bot only ever talks to the paper account.
    return TradingClient(key, secret, paper=True)


def shares_held(client, symbol: str) -> float:
    from alpaca.common.exceptions import APIError

    try:
        return float(client.get_open_position(symbol).qty)
    except APIError:
        return 0.0  # no open position


def existing_order_since(client, symbol: str, side, since: pd.Timestamp):
    """An order for this symbol/side placed on or after the crossover day, if any.

    Stops the bot from placing the same trade twice if it runs more than once.
    """
    from alpaca.trading.enums import OrderStatus, QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol],
                           after=since.to_pydatetime(), limit=100)
    dead = {OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
    for order in client.get_orders(filter=req):
        if order.side == side and order.status not in dead:
            return order
    return None


def place_order(client, symbol: str, qty: int, side):
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    req = MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY)
    return client.submit_order(order_data=req)


def wait_for_fill(client, order_id, timeout: int = 60):
    """Poll the order for up to `timeout` seconds; return its latest state."""
    from alpaca.trading.enums import OrderStatus

    final = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
    deadline = time.monotonic() + timeout
    order = client.get_order_by_id(order_id)
    while order.status not in final and time.monotonic() < deadline:
        time.sleep(3)
        order = client.get_order_by_id(order_id)
    return order


def wait_for_open(client) -> bool:
    """True once the market is open. Waits if the open is a few minutes away."""
    clock = client.get_clock()
    if clock.is_open:
        return True

    wait = (clock.next_open - clock.timestamp).total_seconds()
    next_open_et = pd.Timestamp(clock.next_open).tz_convert(MARKET_TZ)
    if 0 < wait <= MAX_WAIT_FOR_OPEN:
        log.info("Market opens at %s ET; waiting %d seconds.", next_open_et.strftime("%H:%M"), wait)
        time.sleep(wait + 5)
        if client.get_clock().is_open:
            return True
        log.warning("Market still not open after waiting. Skipping this run.")
        return False

    now_et = pd.Timestamp(clock.timestamp).tz_convert(MARKET_TZ)
    if next_open_et.date() == now_et.date():
        log.info("Market opens later today (%s ET). The next scheduled run will trade.",
                 next_open_et.strftime("%H:%M"))
    else:
        log.info("Market is closed now (next open %s ET). Not trading.",
                 next_open_et.strftime("%a %Y-%m-%d %H:%M"))
    return False


def log_account_summary(client, symbol: str) -> None:
    account = client.get_account()
    held = shares_held(client, symbol)
    log.info("Account: equity %s | cash %s | %s shares held: %g",
             account.equity, account.cash, symbol, held)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(cfg: dict, dry_run: bool, client=None) -> None:
    symbol, short, long_ = cfg["symbol"], cfg["short_window"], cfg["long_window"]

    closes = fetch_closes(symbol, cfg["history_days"])
    df = add_moving_averages(closes, short, long_)
    crossovers = find_crossovers(df)

    last_day = df.index[-1]
    last = df.iloc[-1]
    log.info("%s  %d days of data, last completed day %s", symbol, len(df), last_day.date())
    log.info("Close %.2f | SMA%d %.2f | SMA%d %.2f | short-long %+.2f",
             last["close"], short, last["sma_short"], long_, last["sma_long"], last["diff"])

    if not crossovers.empty:
        recent = ", ".join(f"{d.date()} {s}" for d, s in crossovers["signal"].tail(5).items())
        log.info("Crossovers in this window: %s", recent)

    if crossovers.empty or crossovers.index[-1] != last_day:
        trend = "above" if last["diff"] > 0 else "below"
        log.info("No crossover on %s (SMA%d is %s SMA%d). Nothing to do.",
                 last_day.date(), short, trend, long_)
        return

    signal = crossovers["signal"].iloc[-1]
    log.info("CROSSOVER on %s: SMA%d crossed %s SMA%d -> %s signal",
             last_day.date(), short, "above" if signal == "buy" else "below", long_, signal.upper())

    if dry_run:
        log.info("Dry run: would place a market %s of %d %s. No order sent.",
                 signal, cfg["trade_size"], symbol)
        return

    from alpaca.trading.enums import OrderSide

    client = client or get_trading_client()
    account = client.get_account()
    log.info("Alpaca paper account %s | cash %s | buying power %s",
             account.account_number, account.cash, account.buying_power)

    side = OrderSide.BUY if signal == "buy" else OrderSide.SELL
    since = last_day.normalize()  # midnight ET on the crossover day
    previous = existing_order_since(client, symbol, side, since)
    if previous is not None:
        log.info("Already placed a %s order for this crossover (order %s, status %s). Skipping.",
                 signal, previous.id, previous.status.value)
        return

    qty = cfg["trade_size"]
    if side == OrderSide.SELL:
        held = shares_held(client, symbol)
        if held <= 0:
            log.info("Sell signal, but no %s shares are held. Skipping (the bot never opens a short).",
                     symbol)
            return
        qty = int(min(qty, held))

    order = place_order(client, symbol, qty, side)
    log.info("ORDER PLACED: %s %d %s (market, day) | order id %s | status %s",
             signal.upper(), qty, symbol, order.id, order.status.value)

    order = wait_for_fill(client, order.id)
    if order.filled_avg_price is not None:
        log.info("ORDER FILLED: %s %s %s at $%.2f", order.side.value.upper(), order.filled_qty,
                 symbol, float(order.filled_avg_price))
    else:
        log.info("Order status now %s (not filled yet; check bot.log tomorrow or the Alpaca dashboard).",
                 order.status.value)


def run_at_open(cfg: dict, dry_run: bool) -> None:
    """Scheduled mode: trade once per trading day, at the market open."""
    today_et = pd.Timestamp.now(tz=MARKET_TZ).date().isoformat()
    if LAST_RUN_FILE.exists() and LAST_RUN_FILE.read_text().strip() == today_et:
        log.info("Already ran today (%s ET). Nothing to do.", today_et)
        return

    client = get_trading_client()
    if not wait_for_open(client):
        return

    run(cfg, dry_run, client)
    log_account_summary(client, cfg["symbol"])
    if not dry_run:
        LAST_RUN_FILE.write_text(today_et)


def main() -> int:
    parser = argparse.ArgumentParser(description="Moving average crossover bot (Alpaca paper)")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"), help="path to config file")
    parser.add_argument("--dry-run", action="store_true", help="check for a signal but never place an order")
    parser.add_argument("--at-open", action="store_true",
                        help="scheduled mode: wait for the 9:30 AM ET open, trade once per day")
    args = parser.parse_args()

    setup_logging()
    log.info("----- run started%s%s -----",
             " (at-open)" if args.at_open else "", " (dry run)" if args.dry_run else "")
    try:
        cfg = load_config(Path(args.config))
        if args.at_open:
            run_at_open(cfg, dry_run=args.dry_run)
        else:
            run(cfg, dry_run=args.dry_run)
    except SystemExit as e:
        log.error("%s", e)
        return 1
    except Exception:
        log.exception("Bot run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
