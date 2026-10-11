import os
import asyncio
import logging
import sqlite3
import requests
import datetime
import pandas as pd

from ta.trend import EMAIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
CHANNEL_ID = -1003737318075

# --- ADMIN CONFIGURATION ---
ADMIN_TELEGRAM_ID = 6475916836  # <--- REPLACE THIS with your actual numeric Telegram User ID (e.g. from @userinfobot)
ADMIN_USERNAME = "@YourTelegramHandle"  # Your Telegram username for screenshots/support

# Updated with your M-Pesa number
PAYMENT_INSTRUCTIONS = (
    "💳 **HOW TO PAY:**\n\n"
    "1. **M-Pesa (Send Money):** **0793295506**\n"
    "2. **Crypto (USDT - TRC20):** Send to your preferred wallet address\n\n"
    "After paying, send a screenshot of your M-Pesa confirmation message to admin "
    f"{ADMIN_USERNAME} to instantly activate your VIP access!"
)

# Account & Risk Parameters
ACCOUNT_BALANCE = 10000.0  
RISK_PERCENT = 1.0         

PAIRS = {
    "BTCUSD": "BTC/USD",
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "USDJPY": "USD/JPY",
    "ETHUSD": "ETH/USD",
}

DEFAULT_INTERVAL = "1h"
HIGHER_INTERVAL = "4h"
CANDLE_COUNT = 100
SCAN_SECONDS = 3600
VALID_INTERVALS = ["1min", "5min", "15min", "30min", "1h", "4h", "1day"]

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
            interval TEXT,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            free_attempts INTEGER DEFAULT 3,
            tier TEXT DEFAULT 'FREE',
            expires_at TEXT,
            signals_today INTEGER DEFAULT 0,
            last_reset_date TEXT
        )
    """)
    conn.commit()
    conn.close()


def check_and_update_user_access(user_id):
    # --- ADMIN BYPASS: Free and unlimited for you ---
    if user_id == ADMIN_TELEGRAM_ID:
        return True, "👑 **Admin Access:** Unlimited & Free."

    conn = sqlite3.connect("signals.db")
    cursor = conn.cursor()
    cursor.execute("SELECT free_attempts, tier, expires_at, signals_today, last_reset_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    if not row:
        cursor.execute("INSERT INTO users (user_id, free_attempts, tier, expires_at, signals_today, last_reset_date) VALUES (?, 3, 'FREE', '', 0, ?)", (user_id, today_str))
        conn.commit()
        conn.close()
        return True, "🎁 Welcome! You have **3 free signal attempts** remaining."

    free_attempts, tier, expires_at, signals_today, last_reset_date = row

    if last_reset_date != today_str:
        signals_today = 0
        cursor.execute("UPDATE users SET signals_today = 0, last_reset_date = ? WHERE user_id = ?", (today_str, user_id))
        conn.commit()

    if free_attempts > 0:
        cursor.execute("UPDATE users SET free_attempts = free_attempts - 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return True, f"🎁 Free Trial Used. You have **{free_attempts - 1} free attempts** left."

    if tier != 'FREE' and expires_at:
        try:
            exp_date = datetime.datetime.fromisoformat(expires_at)
            if datetime.datetime.now(datetime.timezone.utc) < exp_date:
                if tier == '1_SIGNAL_DAY' and signals_today >= 1:
                    conn.close()
                    return False, "❌ You have reached your **1 signal daily limit** for your $6 plan."
                if tier == '4_SIGNALS_DAY' and signals_today >= 4:
                    conn.close()
                    return False, "❌ You have reached your **4 signals daily limit** for your $15 plan."

                cursor.execute("UPDATE users SET signals_today = signals_today + 1 WHERE user_id = ?", (user_id,))
                conn.commit()
                conn.close()
                return True, f"✅ VIP Access Active ({tier})."
        except Exception as e:
            log.error("Date parse error: %s", e)

    conn.close()
    return False, "❌ **Free trials exhausted!** Please subscribe to continue receiving professional signals."


def log_signal_to_db(signal):
    try:
        conn = sqlite3.connect("signals.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO signals (timestamp, pair, direction, entry, stop_loss, tp1, rsi, interval, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal["candle_time"],
            signal["pair"],
            signal["direction"],
            signal["entry"],
            signal["stop"],
            signal["targets"][0],
            signal["rsi"],
            signal["interval"],
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
                        return False
    except Exception as e:
        log.warning("Economic calendar check skipped: %s", e)
    return True


def get_candles(symbol, interval=DEFAULT_INTERVAL):
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
    
    macd = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd_diff"] = macd.macd_diff()
    
    bb = BollingerBands(close=df["close"], window=20, window_dev=2)
    df["bb_mid"] = bb.bollinger_mavg()

    return df.dropna().reset_index(drop=True)


def check_higher_timeframe_trend(symbol, direction):
    try:
        df_4h = get_candles(symbol, interval=HIGHER_INTERVAL)
        current_4h = df_4h.iloc[-1]
        if direction == "BUY" and current_4h["ema9"] > current_4h["ema21"]:
            return True
        if direction == "SELL" and current_4h["ema9"] < current_4h["ema21"]:
            return True
    except Exception as e:
        log.warning("Higher timeframe check failed for %s: %s", symbol, e)
    return True


def analyze_pair(pair, symbol, interval=DEFAULT_INTERVAL):
    df = get_candles(symbol, interval=interval)
    current = df.iloc[-1]
    previous = df.iloc[-4]

    entry = float(current["close"])
    atr = float(current["atr"])
    ema9 = float(current["ema9"])
    ema21 = float(current["ema21"])
    rsi = float(current["rsi"])
    macd_diff = float(current["macd_diff"])

    if atr <= 0:
        return None

    buy_trend = (
        ema9 > ema21
        and current["close"] > ema9
        and current["ema9"] > previous["ema9"]
        and current["ema21"] > previous["ema21"]
        and 52 <= rsi <= 68
        and macd_diff > 0
        and current["close"] > current["bb_mid"]
    )

    sell_trend = (
        ema9 < ema21
        and current["close"] < ema9
        and current["ema9"] < previous["ema9"]
        and current["ema21"] < previous["ema21"]
        and 32 <= rsi <= 48
        and macd_diff < 0
        and current["close"] < current["bb_mid"]
    )

    if not buy_trend and not sell_trend:
        return None

    direction = "BUY" if buy_trend else "SELL"

    if interval in ["1h", "4h", "1day"]:
        if not check_higher_timeframe_trend(symbol, direction):
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

    dollar_risk = ACCOUNT_BALANCE * (RISK_PERCENT / 100.0)
    position_size = dollar_risk / risk if risk > 0 else 0

    signal = {
        "pair": pair,
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "rsi": rsi,
        "candle_time": str(current["datetime"]),
        "interval": interval,
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
    interval = signal["interval"]

    return (
        f"🚨 FLINCH-FLOW {interval.upper()} SIGNAL 🚨\n\n"
        f"📊 Pair: {pair}\n"
        f"📈 Direction: {direction}\n"
        f"⏱ Timeframe: {interval}\n"
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


async def scan_markets_background(application):
    while True:
        try:
            if check_economic_calendar():
                for pair, symbol in PAIRS.items():
                    if is_market_open(pair):
                        signal = await asyncio.to_thread(analyze_pair, pair, symbol, DEFAULT_INTERVAL)
                        if signal:
                            keyboard = [
                                [
                                    InlineKeyboardButton("📊 View Chart", url=f"https://www.tradingview.com/chart/?symbol={pair}"),
                                    InlineKeyboardButton("✅ Acknowledge", callback_data="ack_signal")
                                ]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            await application.bot.send_message(
                                chat_id=CHANNEL_ID,
                                text=format_signal(signal),
                                reply_markup=reply_markup,
                            )
        except Exception as error:
            log.error("Background scan error: %s", error)
        await asyncio.sleep(SCAN_SECONDS)


async def post_init(application):
    init_db()
    application.bot_data["scan_task"] = asyncio.create_task(
        scan_markets_background(application)
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
        "You get **3 free signal attempts**! After that, choose a VIP subscription.\n"
        "Use `/signal` to get started or `/subscribe` to view pricing plans."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "FLINCH-FLOW BOT COMMANDS\n\n"
        "/start - Start bot\n"
        "/signal [PAIR] [TF] - Request signal (e.g., /signal EURUSD 5min)\n"
        "/subscribe - View VIP subscription pricing plans\n"
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
        cursor.execute("SELECT timestamp, pair, direction, entry, interval FROM signals ORDER BY id DESC LIMIT 5")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            await update.effective_message.reply_text("No signals recorded in database yet.")
            return

        msg = "📊 **Recent Signals Logged:**\n\n"
        for row in rows:
            msg += f"• {row[0]} | {row[1]} {row[2]} @ {row[3]} ({row[4]})\n"
        await update.effective_message.reply_text(msg)
    except Exception as e:
        await update.effective_message.reply_text(f"Could not retrieve stats: {e}")


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_TELEGRAM_ID:
        await update.effective_message.reply_text("❌ You are not authorized to use this command.")
        return

    if not context.args or len(context.args) < 3:
        await update.effective_message.reply_text(
            "⚠️ Usage: `/grant <user_id> <tier> <days>`\n"
            "Tiers available: `1_SIGNAL_DAY`, `4_SIGNALS_DAY`, `VIP_UNLIMITED`\n"
            "Example: `/grant 123456789 1_SIGNAL_DAY 30`"
        )
        return

    target_user_id = context.args[0]
    tier = context.args[1].upper()
    days = int(context.args[2])

    expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).isoformat()

    try:
        conn = sqlite3.connect("signals.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (user_id, free_attempts, tier, expires_at, signals_today, last_reset_date)
            VALUES (?, 0, ?, ?, 0, '')
            ON CONFLICT(user_id) DO UPDATE SET tier=excluded.tier, expires_at=excluded.expires_at
        """, (target_user_id, tier, expires_at))
        conn.commit()
        conn.close()
        await update.effective_message.reply_text(f"✅ Successfully granted **{tier}** to User `{target_user_id}` for **{days} days**!")
    except Exception as e:
        await update.effective_message.reply_text(f"❌ Failed to grant access: {e}")


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🥉 $6 - 1 Signal / Day", callback_data="sub_6")],
        [InlineKeyboardButton("🥈 $15 - 4 Signals / Day", callback_data="sub_15")],
        [InlineKeyboardButton("🥇 $50 - 4 Months Access", callback_data="sub_50")],
        [InlineKeyboardButton("💎 $100 - Full Year Access", callback_data="sub_100")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text(
        "💎 **FLINCH-FLOW VIP SUBSCRIPTION PLANS** 💎\n\n"
        "Unlock professional signals and sniper setups:\n\