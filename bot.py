"""
PO AI Signals - Telegram market-analysis bot.
Signals only: BUY / SELL / WAIT. It does NOT place trades.
Data source: Twelve Data (external candles; not Pocket Option OTC quotes).

Improvements:
- Request pacing to reduce Twelve Data rate-limit errors.
- Retry/backoff for temporary HTTP 429 responses.
- In-memory candle cache to avoid duplicate API requests.
- Async handlers run analysis in a worker thread so Telegram stays responsive.
"""
import os
import asyncio
import time
import logging
import threading
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
# Default to one request every 8.5 seconds (about 7 requests/minute).
# Adjust this only if your Twelve Data plan allows a higher request rate.
API_MIN_INTERVAL_SECONDS = max(
    1.0, float(os.getenv("API_MIN_INTERVAL_SECONDS", "8.5"))
)
API_MAX_RETRIES = 2

TIMEFRAMES = {
    "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
    "45m": "45min", "1h": "1h", "2h": "2h", "4h": "4h",
    "8h": "8h", "1d": "1day", "1w": "1week", "1mo": "1month",
}
DEFAULT_SYMBOL = "EUR/USD"

# Cache each interval for a fraction of its candle duration. This avoids
# repeatedly downloading the same candles while still allowing refreshes.
CACHE_TTL_SECONDS = {
    "1min": 20,
    "5min": 60,
    "15min": 120,
    "30min": 180,
    "45min": 240,
    "1h": 300,
    "2h": 600,
    "4h": 900,
    "8h": 1800,
    "1day": 3600,
    "1week": 14400,
    "1month": 86400,
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("po-ai-signals")

# Locks protect the shared cache and serialize outbound API requests.
_cache_lock = threading.Lock()
_request_lock = threading.Lock()
_candle_cache = {}
_last_api_request_at = 0.0


class RateLimitError(RuntimeError):
    """Twelve Data rate limit was reached after retry attempts."""


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


def _cached_candles(symbol: str, interval: str) -> Optional[pd.DataFrame]:
    key = (symbol, interval)
    now = time.time()
    with _cache_lock:
        item = _candle_cache.get(key)
        if item and now - item["saved_at"] <= CACHE_TTL_SECONDS.get(interval, 60):
            return item["data"].copy()
    return None


def _save_candles(symbol: str, interval: str, df: pd.DataFrame) -> None:
    with _cache_lock:
        _candle_cache[(symbol, interval)] = {
            "saved_at": time.time(),
            "data": df.copy(),
        }


def _get_api_response(params: dict) -> dict:
    """Make a paced API request and retry temporary 429 responses."""
    global _last_api_request_at

    for attempt in range(API_MAX_RETRIES + 1):
        with _request_lock:
            wait_for = API_MIN_INTERVAL_SECONDS - (time.time() - _last_api_request_at)
            if wait_for > 0:
                time.sleep(wait_for)

            _last_api_request_at = time.time()
            response = requests.get(API_URL, params=params, timeout=25)

        if response.status_code == 429:
            if attempt >= API_MAX_RETRIES:
                raise RateLimitError(
                    "Twelve Data rate limit reached after retries. "
                    "Wait before scanning again or check your API plan quota."
                )
            retry_header = response.headers.get("Retry-After", "")
            try:
                delay = max(1.0, float(retry_header))
            except (TypeError, ValueError):
                delay = 20.0 * (attempt + 1)
            log.warning("Twelve Data returned HTTP 429; retrying in %.1f seconds.", delay)
            time.sleep(delay)
            continue

        response.raise_for_status()
        payload = response.json()

        # Some API errors are returned as JSON rather than HTTP 429.
        code = str(payload.get("code", ""))
        status = str(payload.get("status", "")).lower()
        if code == "429" or (status == "error" and "rate limit" in
                             str(payload.get("message", "")).lower()):
            if attempt >= API_MAX_RETRIES:
                raise RateLimitError(
                    "Twelve Data rate limit reached after retries. "
                    "Wait before scanning again or check your API plan quota."
                )
            delay = 20.0 * (attempt + 1)
            log.warning("Twelve Data reported a rate limit; retrying in %.1f seconds.", delay)
            time.sleep(delay)
            continue

        return payload

    raise RateLimitError("Twelve Data rate limit reached.")


def fetch_candles(symbol: str, interval: str) -> pd.DataFrame:
    cached = _cached_candles(symbol, interval)
    if cached is not None:
        log.info("Using cached candles for %s %s.", symbol, interval)
        return cached

    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("Market-data API key is not configured.")

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 180,
        "order": "ASC",
        "timezone": "UTC",
        "apikey": TWELVE_DATA_API_KEY,
    }
    payload = _get_api_response(params)

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
        raise RuntimeError(
            f"Only {len(df)} completed candles returned; at least 60 are needed."
        )

    _save_candles(symbol, interval, df)
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


def _direction(result: Optional[dict]) -> str:
    if not result:
        return "UNAVAILABLE"
    return str(result.get("signal", "WAIT")).split()[0]


def apply_confirmation(result: dict, five_minute: Optional[dict],
                       fifteen_minute: Optional[dict]) -> dict:
    """Suppress BUY/SELL unless both 5m and 15m agree with that direction."""
    checked = dict(result)
    original = _direction(result)
    five_direction = _direction(five_minute)
    fifteen_direction = _direction(fifteen_minute)
    confirmed = (five_direction == original and fifteen_direction == original
                 and original in ("BUY", "SELL"))

    if original in ("BUY", "SELL") and not confirmed:
        checked["signal"] = "WAIT 🟡"
        checked["confirmation"] = (
            f"BLOCKED — 5m {five_direction}, 15m {fifteen_direction}"
        )
    elif original in ("BUY", "SELL"):
        checked["confirmation"] = f"CONFIRMED — 5m {five_direction}, 15m {fifteen_direction}"
    else:
        checked["confirmation"] = f"Not triggered — 5m {five_direction}, 15m {fifteen_direction}"
    return checked


def analyze_with_confirmation(symbol: str, timeframe_label: str) -> dict:
    result = analyze(symbol, timeframe_label)
    if _direction(result) not in ("BUY", "SELL"):
        result["confirmation"] = "Not triggered — primary signal is WAIT"
        return result

    confirmations = {}
    for tf in ("5m", "15m"):
        if timeframe_label == tf:
            confirmations[tf] = result
            continue
        try:
            confirmations[tf] = analyze(symbol, tf)
        except RateLimitError:
            confirmations[tf] = None
            break
        except Exception as exc:
            log.warning("Could not obtain %s confirmation for %s: %s", tf, symbol, exc)
            confirmations[tf] = None

    return apply_confirmation(result, confirmations.get("5m"), confirmations.get("15m"))


def format_signal(result: dict) -> str:
    confirmation_line = (
        f"*5m/15m confirmation:* {result.get('confirmation', 'Not checked')}\n"
    )
    return (
        f"📊 *PO AI SIGNALS — {result['symbol']}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Signal:* {result['signal']}\n"
        f"*Chart timeframe:* {result['timeframe']}\n"
        f"*Rule agreement:* {result['agreement']}% "
        f"({result['score']}/5 votes)\n"
        + confirmation_line
        + f"*Last completed candle:* {result['candle_time']}\n"
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
        result = await asyncio.to_thread(analyze_with_confirmation, symbol, tf)
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
        "Requests are paced to respect the data provider's limits; "
        "this may take a few minutes."
    )

    raw_results = {}
    unavailable = {}
    for tf in TIMEFRAMES:
        try:
            raw_results[tf] = await asyncio.to_thread(analyze, symbol, tf)
        except RateLimitError as exc:
            log.warning("Scan paused at %s because of provider rate limit: %s", tf, exc)
            unavailable[tf] = "rate limit"
            remaining = list(TIMEFRAMES.keys())[list(TIMEFRAMES.keys()).index(tf) + 1:]
            for later in remaining:
                unavailable[later] = "not requested (rate limit)"
            break
        except Exception as exc:
            log.warning("Scan failed for %s %s: %s", symbol, tf, exc)
            unavailable[tf] = str(exc)[:70]

    five_minute = raw_results.get("5m")
    fifteen_minute = raw_results.get("15m")
    results = []
    for tf in TIMEFRAMES:
        if tf in raw_results:
            item = apply_confirmation(raw_results[tf], five_minute, fifteen_minute)
            results.append(
                f"{tf:>3}  {item['signal']}  "
                f"agreement {item['agreement']}%  "
                f"RSI {item['rsi']:.1f}"
            )
        else:
            results.append(f"{tf:>3}  unavailable ({unavailable.get(tf, 'not checked')})")

    five_direction = _direction(five_minute)
    fifteen_direction = _direction(fifteen_minute)
    if five_direction == "UNAVAILABLE" or fifteen_direction == "UNAVAILABLE":
        confirmation_summary = "5m/15m confirmation unavailable — BUY/SELL signals suppressed."
    elif five_direction == fifteen_direction and five_direction in ("BUY", "SELL"):
        confirmation_summary = f"5m + 15m confirmation: {five_direction} — matching direction."
    else:
        confirmation_summary = (
            f"5m/15m confirmation: NOT MET (5m {five_direction}, 15m {fifteen_direction}) — "
            "BUY/SELL signals suppressed."
        )

    message = (
        f"📡 *MULTI-TIMEFRAME SCAN — {symbol}*\n"
        "```\n" + "\n".join(results) + "\n```\n"
        f"*Confirmation rule:* {confirmation_summary}\n"
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
