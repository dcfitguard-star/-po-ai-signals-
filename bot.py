"""
PO AI Signals - Telegram market-analysis bot.
Signals only: BUY / SELL / WAIT. It does NOT place trades.
Data source: Twelve Data (external candles; not Pocket Option OTC quotes).
"""
import os
import logging
from typing import Optional

import pandas as pd
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
ALLOWED_USER_IDS = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

API_URL = "https://api.twelvedata.com/time_series"
TIMEFRAMES = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "45m": "45min", "1h": "1h", "2h": "2h", "4h": "4h",
    "8h": "8h", "1d": "1day", "1w": "1week", "1mo": "1month",
}
DEFAULT_SYMBOL = "EUR/USD"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("po-ai-signals")


def authorized(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # Set ALLOWED_USER_IDS to restrict access to your Telegram ID.
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper().replace("-", "/")
    if not symbol or len(symbol) > 30:
        raise ValueError("Enter a valid symbol, for example EUR/USD or BTC/USD.")
    return symbol


def fetch_candles(symbol: str, interval: str) -> pd.DataFrame:
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 180,
        "order": "ASC",
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    }
    response = requests.get(API_URL, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if "values" not in payload:
        message = payload.get("message") or payload.get("code") or str(payload)
        raise RuntimeError(f"Market-data provider error: {message}")

    df = pd.DataFrame(payload["values"])
    required = {"datetime", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise RuntimeError("Market data did not contain OHLC candle fields.")

    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)

    # Ignore the newest candle because it may still be forming.
    if len(df) > 1:
        df = df.iloc[:-1].copy()
    if len(df) < 60:
        raise RuntimeError(f"Only {len(df)} completed candles returned; at least 60 are needed.")
    return df


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def analyze(symbol: str, timeframe_label: str) -> dict:
    interval = TIMEFRAMES[timeframe_label]
    df = fetch_candles(symbol, interval)
    close = df["close"]

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal

    rsi_now = float(rsi(close).iloc[-1])
    last = df.iloc[-1]
    prev = df.iloc[-2]
    mid = close.rolling(20).mean().iloc[-1]
    std = close.rolling(20).std(ddof=0).iloc[-1]
    upper = mid + 2 * std
    lower = mid - 2 * std

    votes = []
    votes.append(1 if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1]
                 else -1 if ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1] else 0)
    votes.append(1 if macd_hist.iloc[-1] > 0 and macd_hist.iloc[-1] > macd_hist.iloc[-2]
                 else -1 if macd_hist.iloc[-1] < 0 and macd_hist.iloc[-1] < macd_hist.iloc[-2] else 0)
    votes.append(1 if 50 < rsi_now < 70 else -1 if 30 < rsi_now < 50 else 0)
    votes.append(1 if last["close"] > mid else -1 if last["close"] < mid else 0)
    votes.append(1 if last["close"] > last["open"] and last["close"] > prev["close"]
                 else -1 if last["close"] < last["open"] and last["close"] < prev["close"] else 0)

    score = sum(votes)
    if score >= 4:
        signal = "BUY 🟢"
    elif score <= -4:
        signal = "SELL 🔴"
    else:
        signal = "WAIT 🟡"

    # A score is rule agreement, NOT a statistical probability of winning.
    agreement = round(abs(score) / len(votes) * 100)
    return {
        "symbol": symbol,
        "timeframe": timeframe_label,
        "signal": signal,
        "score": score,
        "agreement": agreement,
        "price": float(last["close"]),
        "rsi": rsi_now,
        "ema9": float(ema9.iloc[-1]),
        "ema21": float(ema21.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "macd_hist": float(macd_hist.iloc[-1]),
        "bb_mid": float(mid),
        "bb_upper": float(upper),
        "bb_lower": float(lower),
        "candle_time": last["datetime"].strftime("%Y-%m-%d %H:%M UTC"),
        "votes": votes,
    }


def format_signal(result: dict) -> str:
    return (
        f"📊 *PO AI SIGNALS — {result['symbol']}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Signal:* {result['signal']}\n"
        f"*Chart timeframe:* {result['timeframe']}\n"
        f"*Rule agreement:* {result['agreement']}% "
        f"({result['score']}/5 votes)\n"
        f"*Last completed candle:* {result['candle_time']}\n"
        f"*Close:* `{result['price']:.6g}`\n\n"
        f"*Indicator readings*\n"
        f"• RSI(14): `{result['rsi']:.1f}`\n"
        f"• EMA 9 / 21 / 50: `{result['ema9']:.6g}` / "
        f"`{result['ema21']:.6g}` / `{result['ema50']:.6g}`\n"
        f"• MACD histogram: `{result['macd_hist']:.6g}`\n"
        f"• Bollinger middle: `{result['bb_mid']:.6g}`\n\n"
        f"⚠️ *Analysis only, not a guaranteed trade.* Rule agreement is not "
        f"a win probability. Verify the price/feed and candle on Pocket Option; "
        f"OTC quotes can differ from this external data source. No trade is placed."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Welcome to PO AI Signals.\n\n"
        "Commands:\n"
        "/signal EUR/USD 5m — analyze one timeframe\n"
        "/scan EUR/USD — analyze all supported timeframes\n"
        "/timeframes — show supported intervals\n"
        "/help — show help\n\n"
        "Signals are BUY, SELL, or WAIT. This bot never places trades."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Examples:\n"
        "/signal EUR/USD 1m\n"
        "/signal GBP/JPY 15m\n"
        "/signal BTC/USD 1h\n"
        "/scan EUR/USD\n\n"
        "Supported intervals: " + ", ".join(TIMEFRAMES.keys()) +
        "\nUse symbols accepted by your Twelve Data plan. Data availability and "
        "rate limits depend on the provider/account."
    )


async def timeframes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Supported timeframes:\n" + ", ".join(TIMEFRAMES.keys()) +
        "\n\nThe provider does not supply every possible seconds-based chart. "
        "These are the intervals this bot can request."
    )


async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /signal EUR/USD 5m")
        return
    try:
        symbol = normalize_symbol(context.args[0])
        tf = context.args[1].lower()
        if tf not in TIMEFRAMES:
            raise ValueError("Unsupported timeframe. Use /timeframes.")
        await update.message.reply_text(f"Analyzing {symbol} on {tf}…")
        result = analyze(symbol, tf)
        await update.message.reply_text(format_signal(result), parse_mode="Markdown")
    except Exception as exc:
        log.exception("Signal request failed")
        await update.message.reply_text(f"Could not generate signal: {exc}")


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    raw_symbol = context.args[0] if context.args else DEFAULT_SYMBOL
    try:
        symbol = normalize_symbol(raw_symbol)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    await update.message.reply_text(
        f"Scanning {symbol} across {len(TIMEFRAMES)} timeframes. "
        "This may take a little while and use your market-data API quota."
    )
    results = []
    for tf in TIMEFRAMES:
        try:
            item = analyze(symbol, tf)
            results.append(
                f"{tf:>3}  {item['signal']}  "
                f"agreement {item['agreement']}%  "
                f"RSI {item['rsi']:.1f}"
            )
        except Exception as exc:
            log.warning("Scan failed for %s %s: %s", symbol, tf, exc)
            results.append(f"{tf:>3}  unavailable ({str(exc)[:70]})")

    message = (
        f"📡 *MULTI-TIMEFRAME SCAN — {symbol}*\n"
        "```\n" + "\n".join(results) + "\n```\n"
        "Rule agreement is not a win probability. External prices may differ "
        "from Pocket Option/OTC prices. Verify before making any decision."
    )
    await update.message.reply_text(message, parse_mode="Markdown")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")
    if not TWELVE_DATA_API_KEY:
        raise SystemExit("Missing TWELVE_DATA_API_KEY environment variable.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("timeframes", timeframes))
    app.add_handler(CommandHandler("signal", signal_command))
    app.add_handler(CommandHandler("scan", scan_command))
    log.info("PO AI Signals bot is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
