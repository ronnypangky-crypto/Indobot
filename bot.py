import os
import time
import hmac
import hashlib
import requests
import urllib.parse
import json
from datetime import datetime, timezone, timedelta

# WIB = UTC+7
WIB = timezone(timedelta(hours=7))

# ── Konfigurasi (Ambil dari Environment Variables) ────────────────────
API_KEY      = os.environ.get("INDODAX_API_KEY", "")
SECRET_KEY   = os.environ.get("INDODAX_SECRET_KEY", "")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Trading Parameters
TAKE_PROFIT  = float(os.environ.get("TAKE_PROFIT", "3000")) # Rp
TRAIL_PCT    = float(os.environ.get("TRAIL_PCT", "1.5"))    # %
HARD_STOP    = float(os.environ.get("HARD_STOP", "2.5"))    # %
MAX_MODAL    = float(os.environ.get("MAX_MODAL", "2000000"))
MAX_TRADES   = int(os.environ.get("MAX_TRADES", "5"))
TOP_N_PAIRS  = int(os.environ.get("TOP_N_PAIRS", "100"))
MIN_AI_SCORE = int(os.environ.get("AI_MIN_SCORE", "65"))    # Skor minimal untuk BUY
BLACKLIST_HR = int(os.environ.get("BLACKLIST_HR", "12"))
SCAN_INTERVAL = 30 # Detik (Lebih cepat untuk scalping)

INDODAX_TAPI = "https://indodax.com/tapi"

# ── State ─────────────────────────────────────────────────────────────
modal          = 0.0
total_profit   = 0.0
total_trades   = 0
open_positions = {}
price_history  = {}
volume_history = {}
all_pairs      = []
pair_labels    = {}
prices         = {}
blacklist      = {}
sold_prices    = {}
daily_loss     = 0.0
daily_profit   = 0.0
daily_trades   = 0
win_streak     = 0
spike_notified = {}

# ── Helpers ───────────────────────────────────────────────────────────
def fmt(n):
    if n >= 1e9:  return f"Rp {n/1e9:.2f}M"
    if n >= 1e6:  return f"Rp {n/1e6:.2f}jt"
    if n >= 1e3:  return f"Rp {n/1e3:.0f}rb"
    return f"Rp {n:.0f}"

def now_str():
    return datetime.now(WIB).strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID: return
    text = f"🤖 *IndoBot Scalper*\n{msg}\n⏰ {datetime.now(WIB).strftime('%H:%M:%S')}"
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e: log(f"TG Error: {e}")

# ── Indodax API ───────────────────────────────────────────────────────
def indodax_request(method, params=None):
    if params is None: params = {}
    data = {"method": method, "timestamp": str(int(time.time() * 1000)), "recvWindow": "5000"}
    data.update(params)
    body = urllib.parse.urlencode(data)
    sign = hmac.new(SECRET_KEY.encode(), body.encode(), hashlib.sha512).hexdigest()
    headers = {"Key": API_KEY, "Sign": sign}
    try:
        r = requests.post(INDODAX_TAPI, data=data, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        log(f"API Error [{method}]: {e}")
        return None

def get_idr_balance():
    global modal
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr = float(result["return"]["balance"].get("idr", 0))
        modal = min(idr, MAX_MODAL)
        return idr
    return 0

def get_all_balances():
    result = indodax_request("getInfo")
    return result["return"]["balance"] if result and result.get("success") == 1 else {}

# ── Technical Indicators ───────────────────────────────────────────────
def calc_ema(arr, period):
    if len(arr) < period: return arr[-1] if arr else 0
    k = 2 / (period + 1)
    ema = sum(arr[:period]) / period
    for p in arr[period:]: ema = p * k + ema * (1 - k)
    return ema

def calc_sma(arr, period):
    if len(arr) < period: return arr[-1] if arr else 0
    return sum(arr[-period:]) / period

def calc_rsi(arr, period=14):
    if len(arr) < period + 1: return 50
    gains = losses = 0
    for i in range(len(arr) - period, len(arr)):
        d = arr[i] - arr[i-1]
        if d >= 0: gains += d
        else: losses += -d
    rs = (gains / period) / (losses / period if losses > 0 else 0.001)
    return 100 - 100 / (1 + rs)

def calc_macd(arr):
    if len(arr) < 26: return 0, 0, 0
    ema12 = calc_ema(arr, 12)
    ema26 = calc_ema(arr, 26)
    macd = ema12 - ema26
    signal = macd * 0.9
    return macd, signal, macd - signal

def calc_bollinger(arr, period=20):
    if len(arr) < period: return 0, 0, 0
    sma = calc_sma(arr, period)
    recent = arr[-period:]
    std = (sum((x - sma) ** 2 for x in recent) / period) ** 0.5
    return sma + 2 * std, sma, sma - 2 * std

def calc_atr(arr, period=14):
    if len(arr) < 2: return 0
    trs = [abs(arr[i] - arr[i-1]) for i in range(1, len(arr))]
    return sum(trs[-period:]) / min(len(trs), period)

# ── Expert System Brain ───────────────────────────────────────────────
def ai_analyze(pair_id, details):
    """
    EXPERT SYSTEM (No external API)
    Weighting: Trend 35%, Momentum 25%, Volatility 20%, Volume 20%
    """
    hist = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 30: return 0, "Data insufficient"

    # 1. TREND (35%)
    trend_score = 0
    trend_signals = 0
    if details.get("ema9", 0) > details.get("ema21", 0): trend_score += 1; trend_signals += 1
    if details.get("sma5", 0) > details.get("sma20", 0): trend_score += 1; trend_signals += 1
    if details.get("macd", 0) > details.get("macd_signal", 0): trend_score += 1; trend_signals += 1
    final_trend = (trend_score / trend_signals * 100) if trend_signals > 0 else 0

    # 2. MOMENTUM (25%)
    mom_score = 0
    mom_signals = 0
    if details.get("rsi", 50) < 40: mom_score += 1; mom_signals += 1
    if details.get("rsi", 50) > 70: mom_score -= 1; mom_signals += 1 # Oversold logic
    if details.get("macd", 0) > details.get("macd_signal", 0): mom_score += 1; mom_signals += 1
    final_mom = (mom_score / mom_signals * 100) if mom_signals > 0 else 0

    # 3. VOLATILITY (20%)
    volat_score = 0
    volat_signals = 0
    price = prices.get(pair_id, 0)
    if details.get("bb_lower", 0) > 0 and price <= details.get("bb_lower", 0) * 1.01:
        volat_score += 1; volat_signals += 1
    if details.get("vwap", 0) > 0 and price < details.get("vwap", 0):
        volat_score += 1; volat_signals += 1
    final_volat = (volat_score / volat_signals * 100) if volat_signals > 0 else 0

    # 4. VOLUME (20%)
    vol_score = 0
    avg_vol = sum(vol_h[:-1]) / (len(vol_h) - 1) if len(vol_h) > 1 else 1
    if vol_h[-1] > avg_vol * 1.5: vol_score = 100
    elif vol_h[-1] > avg_vol * 1.1: vol_score = 50
    else: vol_score = 0

    # TOTAL SCORE
    total_score = (final_trend * 0.35) + (final_mom * 0.25) + (final_volat * 0.20) + (vol_score * 0.20)

    # Conflict Detection
    if final_trend > 60 and final_mom < 40:
        total_score -= 20 # Trend up tapi momentum mati
        verdict = "⚠️ WARNING: Trend Up, Momentum Weak"
    else:
        verdict = "✅ Signal Valid" if total_score >= 60 else "❌ Weak Signal"

    if total_score >= 75: verdict = "🟢 STRONG BUY"
    elif total_score >= 60: verdict = "🟡 BUY"
    elif total_score < 45: verdict = "🔴 SKIP"

    return total_score, f"{verdict} | {verdict}"

# ── Order Functions (Scalping Optimized) ───────────────────────────────
def place_buy(pair_id, price, idr_amount):
    # Tight Limit Buy: 0.3% above market to ensure execution in scalping
    buy_price = int(price * 1.003)
    log(f"📤 BUY {pair_id} @ {buy_price}")
    return indodax_request("trade", {"pair": pair_id, "type": "buy", "price": str(buy_price), "idr": str(int(idr_amount))})

def place_sell(pair_id, price, qty):
    # Tight Limit Sell: 0.3% below market for fast exit
    coin_key = pair_id.replace("_idr", "").upper()
    # Handle common integer coins if necessary
    sell_price = int(price * 0.997)
    log(f"📤 SELL {pair_id} @ {sell_price}")
    
    # Get actual balance to avoid decimal errors
    balances = get_all_balances()
    actual_qty = float(balances.get(coin_key, 0))
    if actual_qty <= 0: return {"success": 0, "error": "No balance"}
    
    qty_str = f"{actual_qty:.8f}" # Default format
    # Sederhana: coba pakai qty dari parameter, jika gagal balik ke full balance
    result = indodax_request("trade", {"pair": pair_id, "type": "sell", "price": str(sell_price), coin_key: qty_str})
    return result

# ── Core Logic ────────────────────────────────────────────────────────
def fetch_market_data():
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=10)
        d = r.json()
        tickers = d.get("tickers", {})
        global all_pairs, pair_labels, prices, price_history, volume_history
        
        current_pairs = []
        for key, val in tickers.items():
            if not key.endswith("_idr"): continue
            last = float(val.get("last", 0))
            vol = float(val.get("vol_idr", 0))
            if last <= 0 or vol <= 0: continue
            
            current_pairs.append(key)
            prices[key] = last
            if key not in price_history: price_history[key] = []
            price_history[key].append(last)
            if len(price_history[key]) > 100: price_history[key].pop(0)
            
            if key not in volume_history: volume_history[key] = []
            volume_history[key].append(vol)
            if len(volume_history[key]) > 50: volume_history[key].pop(0)
            
            if key not in pair_labels:
                pair_labels[key] = key.replace("_idr", "").upper() + "/IDR"

        # Sort by volume for TOP_N
        all_pairs = sorted(current_pairs, key=lambda x: tickers[x].get("vol_idr", 0), reverse=True)[:TOP_N_PAIRS]
    except Exception as e:
        log(f"Error fetching market: {e}")

def bot_tick():
    global total_profit, total_trades, daily_profit, daily_trades, win_streak, modal

    get_idr_balance()
    fetch_market_data()

    # 1. Manage Open Positions (Exit Logic)
    for pair_id in list(open_positions.keys()):
        pos = open_positions[pair_id]
        curr_price = prices.get(pair_id, pos["buy_price"])
        pl = (curr_price - pos["buy_price"]) * pos["qty"]
        pl_pct = (curr_price - pos["buy_price"]) / pos["buy_price"] * 100

        # Update Peak for Trailing
        if curr_price > pos["peak_price"]: pos["peak_price"] = curr_price
        peak_drop = (pos["peak_price"] - curr_price) / pos["peak_price"] * 100

        should_sell = False
        reason = ""

        # EXIT STRATEGY
        if pl_pct <= -HARD_STOP:
            should_sell = True; reason = "🚨 Hard Stop"
        elif pl >= TAKE_PROFIT:
            should_sell = True; reason = "💰 Take Profit"
        elif peak_drop >= TRAIL_PCT:
            should_sell = True; reason = "📉 Trailing Stop"

        if should_sell:
            res = place_sell(pair_id, curr_price, pos["qty"])
            if res and res.get("success") == 1:
                total_profit += pl
                total_trades += 1
                daily_profit += pl
                if pl > 0: win_streak += 1
                else: win_streak = 0
                log(f"✅ SOLD {pair_id} | {reason} | P/L: {fmt(pl)}")
                send_telegram(f"✅ *SELL {pair_labels[pair_id]}*\nReason: {reason}\nP/L: {fmt(pl)} ({pl_pct:.2f}%)")
                del open_positions[pair_id]
            else:
                log(f"❌ Failed to sell {pair_id}")

    # 2. Entry Logic (Buy)
    if len(open_positions) < MAX_TRADES:
        for pair_id in all_pairs:
            if pair_id in open_positions: continue
            if pair_id in blacklist: continue

            # Get details for AI
            hist = price_history.get(pair_id, [])
            if len(hist) < 30: continue

            details = {
                "rsi": calc_rsi(hist),
                "ema9": calc_ema(hist, 9),
                "ema21": calc_ema(hist, 21),
                "sma5": calc_sma(hist, 5),
                "sma20": calc_sma(hist, 20),
                "macd": calc_macd(hist)[0],
                "macd_signal": calc_macd(hist)[1],
                "bb_lower": calc_bollinger(hist)[2],
                "vwap": calc_sma(hist, 20), # Simple VWAP approximation
                "psar": True # Placeholder
            }

            score, summary = ai_analyze(pair_id, details)

            if score >= MIN_AI_SCORE:
                idr_per_trade = (modal * 0.90) / MAX_TRADES
                price = prices.get(pair_id, 0)
                if price <= 0: continue
                
                res = place_buy(pair_id, price, idr_per_trade)
                if res and res.get("success") == 1:
                    qty = idr_per_trade / price
                    open_positions[pair_id] = {
                        "buy_price": price, "qty": qty, "peak_price": price, "idr": idr_per_trade
                    }
                    log(f"🛒 BUY {pair_labels[pair_id]} | Score: {score}%")
                    send_telegram(f"🛒 *BUY {pair_labels[pair_id]}*\nScore: {score}%\nPrice: {fmt(price)}")

def main():
    log("🚀 IndoBot Scalper (Expert System) Started")
    send_telegram("🚀 *IndoBot Scalper Online* (Mode: Expert System)")
    
    while True:
        try:
            bot_tick()
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()