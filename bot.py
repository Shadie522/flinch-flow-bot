import os
import asyncio
import logging
import sqlite3
import requests
import datetime
import pandas as pd

from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
CHANNEL_ID = -1003737318075

# Account & Risk Parameters for Dynamic Sizing
ACCOUNT_BALANCE = 10000.0  # Base account balance in USD
RISK_PERCENT = 1.0         # Risk 1% per trade

# Market-data symbols
PAIRS = {
    "BTCUSD": "BTC/USD",
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "ETHUSD": "ETH/USD",
}

INTERVAL = "1h"
HIGHER_INTERVAL = "4h"
CANDLE_COUNT = 100
SCAN_SECONDS = 3600

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

last_processed = {}


def init_db():
    conn = sqlite3.connect("signals.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            pair TEXT,
            direction TEXT,
            entry REAL,
            stop_loss REAL,
            tp1 REAL,
            rsi REAL,
            status TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_signal_to_db(signal):
    try:
        conn = sqlite3.connect("signals.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO signals (timestamp, pair, direction, entry, stop_loss, tp1, rsi, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal["candle_time"],
            signal["pair"],
            signal["direction"],
            signal["entry"],
            signal["stop"],
            signal["targets"][0],
            signal["rsi"],
            "ACTIVE"
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Database logging failed: %s", e)


def is_market_open(pair):
    if "BTC" in pair or "ETH" in pair:
        return True
    now = datetime.datetime.now(datetime.timezone.utc)
    weekday = now.weekday()
    if weekday == 5:
        return False
    if weekday == 6 and now.hour < 22:
        return False
    if weekday == 4 and now.hour >= 22:
        return False
    return True


def check_economic_calendar():
    """Checks for high-impact USD economic events in the current hour window."""
    try:
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        response = requests.get(
            "https://api.twelvedata.com/economic_calendar",
            params={"date": today_str, "apikey": API_KEY},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            events = data.get("result", [])
            current_hour = datetime.datetime.now(datetime.timezone.utc).hour
            for event in events:
                if event.get("impact") == "High" and "USD" in event.get("currency", ""):
                    time_str = event.get("time", "00:00")
                    event_hour = int(time_str.split(":")[0])
                    if abs(event_hour - current_hour) <= 1:
                        log.info("High-impact economic event detected (%s). Pausing signals.", event.get("event"))
                        return False
    except Exception as e:
        log.warning("Economic calendar check skipped due to API limitation: %s", e)
    return True


def get_candles(symbol, interval=INTERVAL):
    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": CANDLE_COUNT,
            "apikey": API_KEY,
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise ValueError(data.get("message", "No candle data returned"))

    df = pd.DataFrame(data["values"])
    if len(df) < 40:
        raise ValueError("Not enough candles for indicator calculations")

    df["datetime"] = pd.to_datetime(df["datetime"])
    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna().sort_values("datetime").reset_index(drop=True)
    df = df.iloc[:-1].copy()

    if len(df) < 30:
        raise ValueError("Not enough completed candles")

    df["ema9"] = EMAIndicator(close=df["close"], window=9).ema_indicator()
    df["ema21"] = EMAIndicator(close=df["close"], window=21).ema_indicator()
    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()
    df["atr"] = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=14).average_true_range()

    return df.dropna().reset_index(drop=True)


def check_higher_timeframe_trend(symbol, direction):
    """Multi-timeframe confirmation using 4h chart."""
    try:
        df_4h = get_candles(symbol, interval=HIGHER_INTERVAL)
        current_4h = df_4h.iloc[-1]
        if direction == "BUY" and current_4h["ema9"] > current_4h["ema21"]:
            return True
        if direction == "SELL" and current_4h["ema9"] < current_4h["ema21"]:
            return True
    except Exception as e:
        log.warning("Higher timeframe check failed for %s: %s", symbol, e)
    return True  # Fallback to allow trade if 4h data is temporarily unavailable


def analyze_pair(pair, symbol):
    df = get_candles(symbol, interval=INTERVAL)
    current = df.iloc[-1]
    previous = df.iloc[-4]

    candle_time = str(current["datetime"])
    if last_processed.get(pair) == candle_time:
        return None

    last_processed[pair] = candle_time

    entry = float(current["close"])
    atr = float(current["atr"])
    ema9 = float(current["ema9"])
    ema21 = float(current["ema21"])
    rsi = float(current["rsi"])

    if atr <= 0:
        return None

    buy_trend = (
        ema9 > ema21
        and current["close"] > ema9
        and current["ema9"] > previous["ema9"]
        and current["ema21"] > previous["ema21"]
        and 52 <= rsi <= 68
    )

    sell_trend = (
        ema9 < ema21
        and current["close"] < ema9
        and current["ema9"] < previous["ema9"]
        and current["ema21"] < previous["ema21"]
        and 32 <= rsi <= 48
    )

    if not buy_trend and not sell_trend:
        return None

    direction = "BUY" if buy_trend else "SELL"

    # Multi-timeframe confirmation filter
    if not check_higher_timeframe_trend(symbol, direction):
        log.info("Signal for %s filtered out by 4h macro trend.", pair)
        return None

    recent = df.iloc[-10:]

    if direction == "BUY":
        stop = min(float(recent["low"].min()), entry - atr * 1.5)
        if stop >= entry:
            return None
        risk = entry - stop
        targets = [entry + risk * multiple for multiple in (3, 4, 5, 6)]
    else:
        stop = max(float(recent["high"].max()), entry + atr * 1.5)
        if stop <= entry:
            return None
        risk = stop - entry
        targets = [entry - risk * multiple for multiple in (3, 4, 5, 6)]

    # Dynamic Position Sizing Calculation
    dollar_risk = ACCOUNT_BALANCE * (RISK_PERCENT / 100.0)
    position_size = dollar_risk / risk if risk > 0 else 0

    signal = {
        "pair": pair,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "rsi": rsi,
        "candle_time": candle_time,
        "dollar_risk": dollar_risk,
        "position_size": position_size,
    }

    log_signal_to_db(signal)
    return signal


def format_signal(signal):
    pair = signal["pair"]
    direction = signal["direction"]
    entry = signal["entry"]
    stop = signal["stop"]
    targets = signal["targets"]

    return (
        "🚨 FLINCH-FLOW ADVANCED SIGNAL 🚨\n\n"
        f"📊 Pair: {pair}\n"
        f"📈 Direction: {direction}\n"
        f"⏱ Timeframe: {INTERVAL} (4h Trend Confirmed)\n"
        f"🕯 Candle: {signal['candle_time']}\n\n"
        f"🎯 Entry: {entry:.5f}\n"
        f"🛑 Stop Loss: {stop:.5f}\n\n"
        f"✅ TP1 (1:3): {targets[0]:.5f}\n"
        f"✅ TP2 (1:4): {targets[1]:.5f}\n"
        f"✅ TP3 (1:5): {targets[2]:.5f}\n"
        f"✅ TP4 (1:6): {targets[3]:.5f}\n\n"
        f"💵 Risk Amount: ${signal['dollar_risk']:.2f} ({RISK_PERCENT}%)\n"
        f"⚖️ Est. Position Size: {signal['position_size']:.2f} units\n"
        f"📉 RSI(14): {signal['rsi']:.2f}\n\n"
        "⚠️ Automated risk-managed system. Verify before execution."
    )


async def scan_markets(bot):
    if not check_economic_calendar():
        log.info("Skipping market scan due to high-impact economic news filter.")
        return

    for pair, symbol in PAIRS.items():
        if not is_market_open(pair):
            log.info("Market is closed for %s. Skipping scan.", pair)
            continue
        try:
            signal = await asyncio.to_thread(analyze_pair, pair, symbol)

            if signal:
                keyboard = [
                    [
                        InlineKeyboardButton("📊 View Chart", url=f"https://www.tradingview.com/chart/?symbol={pair}"),
                        InlineKeyboardButton("✅ Acknowledge", callback_data="ack_signal")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=format_signal(signal),
                    reply_markup=reply_markup,
                )
                log.info("Advanced signal posted for %s", pair)
            else:
                log.info("No qualifying signal for %s", pair)

        except Exception as error:
            log.error("Analysis failed for %s: %s", pair, error)


async def automatic_scans(application):
    while True:
        await scan_markets(application.bot)
        await asyncio.sleep(SCAN_SECONDS)


async def post_init(application):
    init_db()
    application.bot_data["scan_task"] = asyncio.create_task(
        automatic_scans(application)
    )


async def post_shutdown(application):
    task = application.bot_data.get("scan_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Welcome to FLINCH-FLOW Advanced Signals! 📈\n"
        "All filters, multi-timeframe analysis, and risk calculators are active."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "FLINCH-FLOW BOT COMMANDS\n\n"
        "/start - Start bot\n"
        "/help - Help info\n"
        "/pairs - Supported pairs\n"
        "/scan - Manual market scan\n"
        "/stats - View recent database signals"
    )


async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Monitored instruments:\n" + "\n".join(PAIRS.keys())
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        conn = sqlite3.connect("signals.db")
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, pair, direction, entry FROM signals ORDER BY id DESC LIMIT 5")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            await update.effective_message.reply_text("No signals recorded in database yet.")
            return

        msg = "📊 **Recent Signals Logged:**\n\n"
        for row in rows:
            msg += f"• {row[0]} | {row[1]} {row[2]} @ {row[3]}\n"
        await update.effective_message.reply_text(msg)
    except Exception as e:
        await update.effective_message.reply_text(f"Could not retrieve stats: {e}")


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Manual scan initiated with all filters...")
    await scan_markets(context.bot)


def main():
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("pairs", pairs_command))
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("stats", stats_command))

    print("FLINCH-FLOW advanced bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()