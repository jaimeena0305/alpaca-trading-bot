# Moving average crossover bot (Alpaca paper trading)

Fetches the last 100 trading days of prices from yfinance, computes a 20-day and
50-day simple moving average, and places a market order on your **Alpaca paper**
account when the 20-day crosses the 50-day between the two most recent completed days:

- crosses **above** → buy `trade_size` shares
- crosses **below** → sell up to `trade_size` shares you hold (never opens a short)

## Files

| File | What it's for |
|------|---------------|
| `config.json` | symbol, MA periods, trade size, days of history |
| `.env` | your Alpaca paper API key and secret (private, git-ignored) |
| `bot.py` | the bot |
| `bot.log` | log of every run (created on first run) |

## Setup (once)

1. Put your paper keys in `.env` (Alpaca dashboard → Paper account → API Keys):
   ```
   ALPACA_API_KEY=...
   ALPACA_SECRET_KEY=...
   ```
2. Install dependencies (already done in `.venv` if Claude set this up):
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

## Run

```
.venv\Scripts\python bot.py --dry-run   # shows the signal, never trades
.venv\Scripts\python bot.py             # trades on a crossover
.venv\Scripts\python bot.py --at-open   # scheduled mode (see below)
```

The bot compares the two most recent **completed** days, so at the 9:30 AM ET
open it checks for a crossover between the previous two trading days (Monday's
run checks Thursday vs Friday). If it runs twice for the same crossover, it sees
the earlier order and won't trade again.

## Automatic schedule

Windows Task Scheduler runs the task **AAPL MA Crossover Bot** Monday-Friday as
`pythonw bot.py --at-open` (no window). It has two start times because 9:30 AM ET
is **7:00 PM IST** during US daylight saving time (March-November) and
**8:00 PM IST** the rest of the year. In `--at-open` mode the bot:

- asks Alpaca whether the market is open; it waits if the open is up to 15 minutes
  away, and skips the run if the open is later (the other start time handles it),
  or on holidays
- trades at most once per trading day (`last_run.txt` records the day)
- logs the signal, any order and its fill price, and your equity, cash and shares
  held to `bot.log`
- runs late if the PC was off or asleep at 9:30 AM ET, as long as the market is
  still open

The PC must be on and you must be logged in to Windows.

Pause or remove it:
```
powershell -Command "Disable-ScheduledTask -TaskName 'AAPL MA Crossover Bot'"
powershell -Command "Unregister-ScheduledTask -TaskName 'AAPL MA Crossover Bot' -Confirm:$false"
```
