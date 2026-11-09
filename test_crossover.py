"""Checks the crossover logic on made-up prices. Run: python test_crossover.py"""

import pandas as pd

from bot import add_moving_averages, find_crossovers


def series(values):
    idx = pd.date_range("2026-01-01", periods=len(values), freq="B", tz="America/New_York")
    return pd.Series(values, index=idx, dtype=float)


def test_buy_then_sell():
    # Flat, then a rally (short MA crosses above), then a drop (crosses below).
    prices = [100] * 10 + list(range(101, 121)) + list(range(119, 90, -1))
    df = add_moving_averages(series(prices), 3, 8)
    signals = find_crossovers(df)["signal"].tolist()
    assert signals == ["buy", "sell"], signals


def test_no_crossover_when_trend_continues():
    prices = list(range(100, 160))  # steady rise: short stays above long
    df = add_moving_averages(series(prices), 3, 8)
    assert find_crossovers(df).empty


def test_crossover_on_last_day_is_detected():
    prices = [100] * 10 + [90] * 5 + [130]  # sharp jump on the final day
    df = add_moving_averages(series(prices), 3, 8)
    cross = find_crossovers(df)
    assert not cross.empty and cross.index[-1] == df.index[-1]
    assert cross["signal"].iloc[-1] == "buy"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    print("all crossover tests passed")
