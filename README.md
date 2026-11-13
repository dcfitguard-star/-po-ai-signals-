# PO AI Signals — Telegram bot

A Telegram **signal-only** bot. It analyzes external market candles and returns
BUY / SELL / WAIT. It does not connect to a Pocket Option account or place trades.

## What it supports

- `/signal EUR/USD 1m` — one symbol and timeframe
- `/scan EUR/USD` — scans all configured provider intervals
- `/timeframes` — lists intervals
- `/help`

Configured intervals: 1m, 5m, 15m, 30m, 45m, 1h, 2h, 4h, 8h, 1d, 1w, 1mo.

The market-data provider's documented intervals do not include 5-second or
15-second candles. Actual symbol availability and API quotas depend on your
Twelve Data account/plan.

## Setup

1. In Telegram, open **@BotFather**, send `/newbot`, follow the prompts, and copy
   the bot token. Keep it private.
2. Create a Twelve Data account and copy your API key.
3. Install Python 3.10+.
4. Unzip this folder and open a terminal in it.
5. Install dependencies:

   `python -m venv .venv`

   Windows:
   `.venv\\Scripts\\activate`

   macOS/Linux:
   `source .venv/bin/activate`

   `pip install -r requirements.txt`

6. Set environment variables. Easiest option: copy `.env.example` to `.env`,
   then load the values into your shell (the script intentionally does not read
   `.env` automatically).

   macOS/Linux:
   `export TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_TOKEN"`
   `export TWELVE_DATA_API_KEY="YOUR_TWELVE_DATA_KEY"`
   `export ALLOWED_USER_IDS="YOUR_NUMERIC_TELEGRAM_USER_ID"`

   PowerShell:
   `$env:TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_TOKEN"`
   `$env:TWELVE_DATA_API_KEY="YOUR_TWELVE_DATA_KEY"`
   `$env:ALLOWED_USER_IDS="YOUR_NUMERIC_TELEGRAM_USER_ID"`

   To find your numeric Telegram ID, message a trusted ID-lookup bot or add a
   temporary `/whoami` handler yourself. Do not share your bot token.

7. Run:

   `python bot.py`

8. Open your new Telegram bot, press Start, then try:
   `/signal EUR/USD 5m`
   `/scan EUR/USD`

## Signal method

The bot uses five directional votes:
- EMA 9/21/50 alignment
- MACD histogram direction and momentum
- RSI zone
- Close relative to the 20-period Bollinger middle band
- Candle direction plus previous-close confirmation

4 or 5 bullish votes = BUY; 4 or 5 bearish votes = SELL; otherwise WAIT.
The displayed agreement is the share of rule votes pointing in one direction.
It is **not** a measured probability of profit, a backtest result, or a promise.

The newest candle is discarded because it may still be forming. The signal is
therefore based on the most recent completed candle, and may arrive after the
start of a new candle.

## Important limitations and safety

- Twelve Data is an external feed, not Pocket Option's own quote stream.
  Pocket Option OTC prices can differ, especially outside normal market hours.
- This code does not submit trades, log into Pocket Option, or use unofficial
  account endpoints.
- Pocket Option's public offer says it may cancel/revise operations performed
  with unauthorized bot software. Review the current terms before using any
  automation on the platform.
- Start with demo/paper observation. Log signals and outcomes over a meaningful
  sample before considering any real-money use.
- No indicator combination guarantees profitable signals. Binary options can
  lose the full stake quickly.
- For personal use, set `ALLOWED_USER_IDS` so strangers cannot use your bot or
  consume your market-data quota.

## Files

- `bot.py` — Telegram bot
- `requirements.txt` — Python dependencies
- `.env.example` — environment-variable template
