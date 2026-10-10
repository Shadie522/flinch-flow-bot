import os
import asyncio
import logging
import requests
import pandas as pd

from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
CHANNEL_ID = -1003737318075

# Market-data symbols supported by our initial tests
PAIRS = {
    "BTCUSD": "BTC/USD",
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "ETHUSD": "ETH/USD",
}

INTERVAL = "1h"
CANDLE_COUNT = 100
SCAN_SECONDS = 3600

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

last_processed = {}


def get_candles(symbol):
    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": INTERVAL,
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

    # Exclude the newest candle because it may still be forming.
    df = df.iloc[:-1].copy()

    if len(df) < 30:
        raise ValueError("Not enough completed candles")

    df["ema9"] = EMAIndicator(
        close=df["close"], window=9
    ).ema_indicator()

    df["ema21"] = EMAIndicator(
        close=df["close"], window=21
    ).ema_indicator()

    df["rsi"] = RSIIndicator(
        close=df["close"], window=14
    ).rsi()

    df["atr"] = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=14,
    ).average_true_range()

    return df.dropna().reset_index(drop=True)


def analyze_pair(pair, symbol):
    df = get_candles(symbol)
    current = df.iloc[-1]
    previous = df.iloc[-4]

    candle_time = str(current["datetime"])

    if last_processed.get(pair) == candle_time:
        return None

    # Mark this completed candle as checked, even if no trade exists.
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

    recent = df.iloc[-10:]

    if buy_trend:
        direction = "BUY"
        stop = min(float(recent["low"].min()), entry - atr * 1.5)

        if stop >= entry:
            return None

        risk = entry - stop
        targets = [entry + risk * multiple for multiple in (3, 4, 5, 6)]

    else:
        direction = "SELL"
        stop = max(float(recent["high"].max()), entry + atr * 1.5)

        if stop <= entry:
            return None

        risk = stop - entry
        targets = [entry - risk * multiple for multiple in (3, 4, 5, 6)]

    return {
        "pair": pair,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "rsi": rsi,
        "candle_time": candle_time,
    }


def format_signal(signal):
    pair = signal["pair"]
    direction = signal["direction"]
    entry = signal["entry"]
    stop = signal["stop"]
    targets = signal["targets"]

    return (
        "🚨 FLINCH-FLOW SIGNAL 🚨\n\n"
        f"📊 Pair: {pair}\n"
        f"📈 Direction: {direction}\n"
        f"⏱ Timeframe: {INTERVAL}\n"
        f"🕯 Candle: {signal['candle_time']}\n\n"
        f"🎯 Entry: {entry:.8f}\n"
        f"🛑 Stop Loss: {stop:.8f}\n\n"
        f"✅ TP1 (1:3): {targets[0]:.8f}\n"
        f"✅ TP2 (1:4): {targets[1]:.8f}\n"
        f"✅ TP3 (1:5): {targets[2]:.8f}\n"
        f"✅ TP4 (1:6): {targets[3]:.8f}\n\n"
        f"📉 RSI(14): {signal['rsi']:.2f}\n"
        "⚠️ Automated technical analysis, not a guarantee. "
        "Verify prices, costs, and risk before trading."
    )


async def scan_markets(bot):
    for pair, symbol in PAIRS.items():
        try:
            signal = await asyncio.to_thread(
                analyze_pair, pair, symbol
            )

            if signal:
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=format_signal(signal),
                )
                log.info("Signal posted for %s", pair)
            else:
                log.info("No new qualifying signal for %s", pair)

        except Exception as error:
            log.error("Analysis failed for %s: %s", pair, error)


async def automatic_scans(application):
    while True:
        await scan_markets(application.bot)
        await asyncio.sleep(SCAN_SECONDS)


async def post_init(application):
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
        "Welcome to FLINCH-FLOW Signals! 📈\n"
        "Use /help to view commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "FLINCH-FLOW BOT\n\n"
        "/start - Start the bot\n"
        "/help - Show help\n"
        "/pairs - List instruments\n"
        "/scan - Run a market scan\n\n"
        "Automatic scans run hourly. Signals require "
        "EMA, RSI, and trend conditions to agree."
    )


async def pairs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Supported instruments:\n" + "\n".join(PAIRS.keys())
    )


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Market scan started. Check FLINCH-FLOW and Terminal for results."
    )
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

    print("FLINCH-FLOW automatic signal bot is starting...")
    application.run_polling()


if __name__ == "__main__":
    main()