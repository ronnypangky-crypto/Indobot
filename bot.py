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

# ── Konfigurasi ────────────────────────────────────────
API_KEY      = os.environ.get("INDODAX_API_KEY", "")
SECRET_KEY   = os.environ.get("INDODAX_SECRET_KEY", "")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_KEY   = os.environ.get("CLAUDE_API_KEY", "")
TAKE_PROFIT  = float(os.environ.get("TAKE_PROFIT", "3000"))
TRAIL_PCT    = float(os.environ.get("TRAIL_PCT", "2"))
HARD_STOP    = float(os.environ.get("HARD_STOP", "3.0"))
MAX_MODAL    = float(os.environ.get("MAX_MODAL", "2000000"))
MAX_TRADES   = int(os.environ.get("MAX_TRADES", "5"))
TOP_N_PAIRS  = int(os.environ.get("TOP_N_PAIRS", "100"))
MIN_SIGNALS  = int(os.environ.get("MIN_SIGNALS", "15"))
AI_MIN_SCORE = int(os.environ.get("AI_MIN_SCORE", "65"))
BLACKLIST_HR = int(os.environ.get("BLACKLIST_HR", "12"))
UPTREND_PCT  = float(os.environ.get("UPTREND_PCT", "50"))
SELL_RETRY   = int(os.environ.get("SELL_RETRY", "3"))
VOL_SPIKE    = float(os.environ.get("VOL_SPIKE", "3.0"))
SCAN_INTERVAL = 10
PRICE_SPIKE_PCT = float(os.environ.get("PRICE_SPIKE_PCT", "3.0"))  # skip kalau naik > 3% dalam 5 menit
SLIPPAGE_TOLERANCE = float(os.environ.get("SLIPPAGE_TOLERANCE", "2.0"))  # % tolerance untuk price slippage

INDODAX_TAPI = "https://indodax.com/tapi"

# ── State ──────────────────────────────────────────────
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
daily_start    = time.time()
fg_cache       = {"value": 50, "label": "Neutral", "timestamp": 0}

# ── Daily Summary State ────────────────────────────────
daily_profit   = 0.0
daily_trades   = 0
last_summary   = ""
win_streak     = 0
spike_notified = {}  # {pair_id: timestamp} — catat coin yang sudah dinotif spike

# ── Helpers ────────────────────────────────────────────
def fmt(n):
    if n >= 1e9:  return f"Rp {n/1e9:.2f}M"
    if n >= 1e6:  return f"Rp {n/1e6:.2f}jt"
    if n >= 1e3:  return f"Rp {n/1e3:.0f}rb"
    return f"Rp {n:.0f}"

def now_str():
    return datetime.now(WIB).strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)

# ── Telegram ───────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    text = f"🤖 *IndoBot v9*\n{msg}\n⏰ {datetime.now(WIB).strftime('%d/%m/%Y %H:%M:%S')}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log(f"TG Error: {e}")

# ── Daily Summary ──────────────────────────────────────
def check_daily_summary():
    global daily_profit, daily_trades, last_summary
    now       = datetime.now(WIB)
    report_hours = [6, 12, 18, 21]
    hour_key  = f"{now.strftime('%Y-%m-%d')}-{now.hour}"
    if now.hour in report_hours and last_summary != hour_key:
        last_summary = hour_key
        fg_value, fg_label = get_fear_greed()
        posisi_aktif = len(open_positions)
        emoji = "🟢" if daily_profit >= 0 else "🔴"
        label_report = "📊 *Daily Report*" if now.hour == 21 else "📊 *Update Report*"
        send_telegram(
            f"{label_report} — {now.strftime('%d/%m/%Y %H:%M')} WIB\n"
            f"Trade: {daily_trades}x\n"
            f"Profit: {emoji} {fmt(daily_profit)}\n"
            f"Total Profit: {fmt(total_profit)}\n"
            f"Modal: {fmt(modal)}\n"
            f"Posisi aktif: {posisi_aktif}/{MAX_TRADES}\n"
            f"😱 Fear & Greed: {fg_value} ({fg_label})\n"
            f"Total Trade: {total_trades}"
        )
        if now.hour == 21:
            daily_profit = 0.0
            daily_trades = 0
            log("📊 Daily summary terkirim & reset!")
        else:
            log(f"📊 Update report jam {now.hour}:00 terkirim!")

# ── Indodax API ────────────────────────────────────────
def indodax_request(method, params=None):
    if params is None:
        params = {}
    data = {
        "method":     method,
        "timestamp":  str(int(time.time() * 1000)),
        "recvWindow": "5000",
    }
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

# ── Balance ────────────────────────────────────────────
def get_idr_balance():
    global modal
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr   = float(result["return"]["balance"].get("idr", 0))
        modal = min(idr, MAX_MODAL)
        return idr
    return 0

def get_all_balances():
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        return result["return"]["balance"]
    return {}

def get_coin_balance(coin):
    balances = get_all_balances()
    for key in [coin, coin.lower(), coin.upper()]:
        if key in balances:
            val = float(balances.get(key, 0))
            if val > 0:
                return val, key
    return 0, coin

# ── Format qty ─────────────────────────────────────────
INTEGER_COINS = {
    "trollsol", "jellyjelly", "pippin", "whitewhale", "fartcoin",
    "sundog", "zerebro", "siren", "pepe", "shib", "floki",
    "babydoge", "bonk", "myro", "popcat", "neiro", "turbo",
    "moodeng", "pnut", "pengu", "useless", "doge",
    "hart", "aura", "giga", "mew", "looks", "buildon",
    "strm", "pols", "degen", "islm", "trx", "molt", "ub",
    "bananas31", "banana", "rats", "slerf", "bome", "wen",
    "samo", "cope", "orca", "mngo", "step", "media",
    "hxro", "maps", "kin", "tulip", "slim", "like", "larix",
    "ray", "star", "liq", "wifi", "rope", "bop",
}

def format_qty(coin, qty):
    if coin.lower() in INTEGER_COINS:
        return str(int(qty))
    return f"{qty:.8f}"

# ── Test API ───────────────────────────────────────────
def test_api():
    global modal
    log("🔑 Test koneksi API Indodax...")
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr   = float(result["return"]["balance"].get("idr", 0))
        modal = min(idr, MAX_MODAL)
        log(f"✅ API OK! Saldo IDR: {fmt(idr)} | Modal bot: {fmt(modal)}")
        send_telegram(
            f"✅ *API Indodax Konek!*\n"
            f"Saldo IDR: {fmt(idr)}\n"
            f"Modal bot: {fmt(modal)}\n"
            f"AI analisa: {'✅ Aktif' if CLAUDE_KEY else '🧠 Internal AI'}\n"
            f"Min sinyal: {MIN_SIGNALS}/17\n"
            f"AI min score: {AI_MIN_SCORE}%"
        )
        return True
    log(f"❌ API gagal!")
    send_telegram(f"❌ *API Gagal!*")
    return False

# ── Blacklist ──────────────────────────────────────────
def add_blacklist(pair_id, loss):
    global daily_loss
    blacklist[pair_id] = time.time()
    daily_loss += abs(loss)
    label = pair_labels.get(pair_id, pair_id)
    log(f"🚫 Blacklist {label} {BLACKLIST_HR} jam")
    send_telegram(f"🚫 *Blacklist {label}*\nSkip {BLACKLIST_HR} jam\nLoss hari ini: {fmt(daily_loss)}")

def is_blacklisted(pair_id):
    if pair_id not in blacklist:
        return False
    if time.time() - blacklist[pair_id] > BLACKLIST_HR * 3600:
        del blacklist[pair_id]
        return False
    return True

def reset_daily():
    global daily_loss, daily_start
    now_wib  = datetime.now(WIB)
    today    = now_wib.strftime("%Y-%m-%d")
    if not hasattr(reset_daily, "last_date"):
        reset_daily.last_date = today
    if today != reset_daily.last_date:
        reset_daily.last_date = today
        daily_loss  = 0.0
        daily_start = time.time()
        log("🔄 Reset harian WIB")
        send_telegram("🔄 *Reset Harian* — Daily loss dikosongkan!")

# ── Market trend ───────────────────────────────────────
def check_market_trend():
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=10)
        d = r.json()
        tickers = d.get("tickers", {})
        total = naik = turun = 0
        for key, val in tickers.items():
            if not key.endswith("_idr"):
                continue
            last   = float(val.get("last", 0))
            open_p = float(val.get("open", 0))
            vol    = float(val.get("vol_idr", 0))
            if last <= 0 or open_p <= 0 or vol < 1e8:
                continue
            total += 1
            pct = (last - open_p) / open_p * 100
            if pct > 0:   naik  += 1
            elif pct < 0: turun += 1
        if total == 0:
            return True
        pct_naik = naik / total * 100
        is_up = pct_naik >= UPTREND_PCT
        log(f"📊 Market: {total} coin | Naik:{naik}({pct_naik:.0f}%) | {'UPTREND ✅' if is_up else 'DOWNTREND ❌'}")
        return is_up
    except Exception as e:
        log(f"⚠️ Gagal cek market: {e}")
        return True

# ── Fear & Greed Index ─────────────────────────────────
def get_fear_greed():
    global fg_cache
    if time.time() - fg_cache["timestamp"] < 3600:
        return fg_cache["value"], fg_cache["label"]
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=10)
        d = r.json()
        value = int(d["data"][0]["value"])
        label = d["data"][0]["value_classification"]
        fg_cache = {"value": value, "label": label, "timestamp": time.time()}
        log(f"😱 Fear & Greed: {value} ({label})")
        return value, label
    except Exception as e:
        log(f"⚠️ Gagal ambil Fear & Greed: {e}")
        return fg_cache["value"], fg_cache["label"]

# ── Fetch pairs ────────────────────────────────────────
def fetch_all_pairs():
    global all_pairs, pair_labels, prices, price_history, volume_history
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=10)
        d = r.json()
        tickers = d.get("tickers", {})
        pair_list = []
        for key, val in tickers.items():
            if not key.endswith("_idr"):
                continue
            last = float(val.get("last", 0))
            vol  = float(val.get("vol_idr", 0))
            if last <= 0 or vol <= 0:
                continue
            pair_list.append((key, last, vol))
        pair_list.sort(key=lambda x: x[2], reverse=True)
        all_pairs = []
        for key, last, vol in pair_list[:TOP_N_PAIRS]:
            label = key.replace("_idr", "").upper() + "/IDR"
            all_pairs.append(key)
            pair_labels[key]    = label
            prices[key]         = last
            volume_history[key] = [vol]
            if key not in price_history:
                price_history[key] = []
            price_history[key].append(last)
        names = [pair_labels[p] for p in all_pairs]
        log(f"📡 {len(all_pairs)} pair dimuat")
        send_telegram(f"📡 *{len(all_pairs)} Pair Dimuat*\nTop: {', '.join(names[:10])}...")
        return True
    except Exception as e:
        log(f"⚠️ Gagal load pair: {e}")
        return False

# ── Refresh harga ──────────────────────────────────────
def fetch_prices():
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=10)
        d = r.json()
        tickers = d.get("tickers", {})
        for pair_id in all_pairs:
            if pair_id not in tickers:
                continue
            last = float(tickers[pair_id].get("last", 0))
            vol  = float(tickers[pair_id].get("vol_idr", 0))
            if last <= 0:
                continue
            prices[pair_id] = last
            price_history[pair_id].append(last)
            if len(price_history[pair_id]) > 100:
                price_history[pair_id].pop(0)
            volume_history[pair_id].append(vol)
            if len(volume_history[pair_id]) > 20:
                volume_history[pair_id].pop(0)
    except Exception as e:
        log(f"⚠️ Gagal refresh harga: {e}")

# ── Get Current Price (Real-time) ──────────────────────
def get_current_price(pair_id):
    """Fetch harga terbaru dari Indodax untuk cek slippage"""
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=10)
        d = r.json()
        tickers = d.get("tickers", {})
        if pair_id in tickers:
            return float(tickers[pair_id].get("last", 0))
    except Exception as e:
        log(f"⚠️ Gagal fetch current price {pair_id}: {e}")
    return prices.get(pair_id, 0)

def check_slippage(pair_id, scan_price):
    """Check apakah harga berubah > SLIPPAGE_TOLERANCE sejak scan"""
    current_price = get_current_price(pair_id)
    if current_price <= 0 or scan_price <= 0:
        return True, 0
    
    slippage_pct = abs(current_price - scan_price) / scan_price * 100
    is_acceptable = slippage_pct <= SLIPPAGE_TOLERANCE
    
    label = pair_labels.get(pair_id, pair_id)
    if not is_acceptable:
        log(f"⚠️ SLIPPAGE REJECT {label}: scan={scan_price}, current={current_price}, slippage={slippage_pct:.2f}% > {SLIPPAGE_TOLERANCE}%")
    
    return is_acceptable, slippage_pct

# ── Indikator ──────────────────────────────────────────
def calc_ema(arr, period):
    if len(arr) < period:
        return arr[-1] if arr else 0
    k = 2 / (period + 1)
    ema = sum(arr[:period]) / period
    for p in arr[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_sma(arr, period):
    if len(arr) < period:
        return arr[-1] if arr else 0
    return sum(arr[-period:]) / period

def calc_rsi(arr, period=14):
    if len(arr) < period + 1:
        return 50
    gains = losses = 0
    for i in range(len(arr) - period, len(arr)):
        d = arr[i] - arr[i-1]
        if d >= 0: gains += d
        else:      losses += -d
    rs = (gains / period) / (losses / period if losses > 0 else 0.001)
    return 100 - 100 / (1 + rs)

def calc_macd(arr):
    if len(arr) < 26:
        return 0, 0, 0
    ema12  = calc_ema(arr, 12)
    ema26  = calc_ema(arr, 26)
    macd   = ema12 - ema26
    signal = macd * 0.9
    return macd, signal, macd - signal

def calc_bollinger(arr, period=20):
    if len(arr) < period:
        return 0, 0, 0
    sma    = calc_sma(arr, period)
    recent = arr[-period:]
    std    = (sum((x - sma) ** 2 for x in recent) / period) ** 0.5
    return sma + 2 * std, sma, sma - 2 * std

def calc_stoch_rsi(arr, period=14):
    if len(arr) < period * 2:
        return 50
    rsi_vals = [calc_rsi(arr[:i+1], period) for i in range(period, len(arr))]
    if not rsi_vals:
        return 50
    min_rsi = min(rsi_vals[-period:])
    max_rsi = max(rsi_vals[-period:])
    curr    = rsi_vals[-1]
    return (curr - min_rsi) / (max_rsi - min_rsi) * 100 if max_rsi != min_rsi else 50

def calc_cci(arr, period=20):
    if len(arr) < period:
        return 0
    recent = arr[-period:]
    mean   = sum(recent) / period
    mad    = sum(abs(x - mean) for x in recent) / period
    return (arr[-1] - mean) / (0.015 * mad) if mad > 0 else 0

def calc_williams_r(arr, period=14):
    if len(arr) < period:
        return -50
    recent = arr[-period:]
    high, low = max(recent), min(recent)
    return (high - arr[-1]) / (high - low) * -100 if high != low else -50

def calc_roc(arr, period=10):
    if len(arr) < period + 1:
        return 0
    return (arr[-1] - arr[-period-1]) / arr[-period-1] * 100

def calc_atr(arr, period=14):
    if len(arr) < 2:
        return 0
    trs = [abs(arr[i] - arr[i-1]) for i in range(1, len(arr))]
    return sum(trs[-period:]) / min(len(trs), period)

def calc_obv(arr, vol_arr):
    if len(arr) < 2 or len(vol_arr) < 2:
        return 0
    obv = 0
    for i in range(1, min(len(arr), len(vol_arr))):
        if arr[i] > arr[i-1]:   obv += vol_arr[i]
        elif arr[i] < arr[i-1]: obv -= vol_arr[i]
    return obv

def calc_alligator(arr):
    if len(arr) < 13:
        return False, ""
    jaw   = calc_sma(arr[:-8]  if len(arr) > 8  else arr, 13)
    teeth = calc_sma(arr[:-5]  if len(arr) > 5  else arr, 8)
    lips  = calc_sma(arr[:-3]  if len(arr) > 3  else arr, 5)
    bullish = lips > teeth > jaw
    return bullish, f"Alligator: L={lips:.0f}>T={teeth:.0f}>J={jaw:.0f}"

def calc_parabolic_sar(arr, af_start=0.02, af_max=0.2):
    if len(arr) < 10:
        return False, ""
    rising = arr[-1] > arr[-5]
    sar    = min(arr[-5:]) if rising else max(arr[-5:])
    bullish = rising and arr[-1] > sar
    return bullish, f"PSAR={'↑' if bullish else '↓'}"

def calc_adx(arr, period=14):
    if len(arr) < period + 1:
        return 0, False
    moves = [abs(arr[i] - arr[i-1]) for i in range(1, len(arr))]
    avg_move = sum(moves[-period:]) / period
    total_range = max(arr[-period:]) - min(arr[-period:])
    adx = (avg_move / total_range * 100) if total_range > 0 else 0
    strong_trend = adx > 25
    return adx, strong_trend

def calc_momentum(arr, period=10):
    if len(arr) < period + 1:
        return 0
    return arr[-1] - arr[-period-1]

def calc_vwap(arr, vol_arr):
    if len(arr) < 5 or len(vol_arr) < 5:
        return arr[-1] if arr else 0
    n = min(len(arr), len(vol_arr), 20)
    prices_slice = arr[-n:]
    vols_slice   = vol_arr[-n:]
    total_vol    = sum(vols_slice)
    if total_vol == 0:
        return arr[-1]
    return sum(p * v for p, v in zip(prices_slice, vols_slice)) / total_vol

# ── RSI Divergence ─────────────────────────────────────
def calc_rsi_divergence(arr):
    """Bullish: harga turun tapi RSI naik = sinyal beli kuat"""
    if len(arr) < 15:
        return False
    rsi_now  = calc_rsi(arr)
    rsi_prev = calc_rsi(arr[:-5])
    return arr[-1] < arr[-5] and rsi_now > rsi_prev

# ── Orderbook Analysis ────────────────────────────────
def get_orderbook_pressure(pair_id):
    """
    Estimasi buy pressure dari volume history
    karena endpoint /depth Indodax diblokir dari Railway
    """
    try:
        vol_h = volume_history.get(pair_id, [])
        hist  = price_history.get(pair_id, [])
        if len(vol_h) < 5 or len(hist) < 5:
            return 0, 0, "Tidak tersedia"

        # Volume naik + harga naik = buy pressure
        # Volume naik + harga turun = sell pressure
        avg_vol  = sum(vol_h[:-1]) / (len(vol_h) - 1)
        curr_vol = vol_h[-1]
        price_up = hist[-1] >= hist[-2]

        if avg_vol <= 0:
            return 0, 0, "Tidak tersedia"

        vol_ratio = curr_vol / avg_vol

        if vol_ratio >= 1.5 and price_up:
            buy_pct = 70
            return 70, 30, f"Buy pressure {vol_ratio:.1f}x vol 🟢"
        elif vol_ratio >= 1.5 and not price_up:
            return 30, 70, f"Sell pressure {vol_ratio:.1f}x vol 🔴"
        else:
            return 50, 50, f"Volume normal {vol_ratio:.1f}x"
    except:
        return 0, 0, "Tidak tersedia"


def is_price_spiking(pair_id):
    """
    Return True kalau harga naik terlalu cepat (> PRICE_SPIKE_PCT dalam 5 menit)
    Hindari beli di pucuk!
    """
    hist = price_history.get(pair_id, [])
    if len(hist) < 5:
        return False
    price_5_ago = hist[-5]
    price_now   = hist[-1]
    if price_5_ago <= 0:
        return False
    change_pct = (price_now - price_5_ago) / price_5_ago * 100
    if change_pct > PRICE_SPIKE_PCT:
        label = pair_labels.get(pair_id, pair_id)
        log(f"⛔ Skip {label} — harga naik {change_pct:.1f}% dalam 5 menit (spike!)")
        return True
    return False

# ── Pre-Spike Detection ────────────────────────────────
def check_presike_entry(pair_id):
    """
    Deteksi coin yang MUNGKIN mau spike:
    1. Harga minimal (no upper limit) — catch semua altcoin
    2. Volume mulai naik tapi harga belum bergerak banyak
    3. Minimal 4/5 sinyal basic
    """
    curr_price = prices.get(pair_id, 0)
    if curr_price <= 0:
        return False, 0, 0, ""

    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 5 or len(vol_h) < 3:
        return False, 0, 0, ""

    avg_vol  = sum(vol_h[:-1]) / (len(vol_h) - 1)
    curr_vol = vol_h[-1]
    if avg_vol <= 0:
        return False, 0, 0, ""

    vol_ratio = curr_vol / avg_vol

    if vol_ratio >= 5:
        ob_tier = 3
        ob_label = f"Volume spike {vol_ratio:.1f}x 🐋🐋🐋"
    elif vol_ratio >= 3:
        ob_tier = 2
        ob_label = f"Volume naik {vol_ratio:.1f}x 🐋🐋"
    elif vol_ratio >= 1.5:
        ob_tier = 1
        ob_label = f"Volume mulai {vol_ratio:.1f}x 🐋"
    else:
        return False, 0, 0, ""

    if len(hist) >= 5:
        price_change = (hist[-1] - hist[-5]) / hist[-5] * 100
        # Pre-spike hanya terima kalau harga belum naik banyak (< 3%) dan tidak turun >5%
        if price_change > 3:  # Sudah ketinggalan momentum
            return False, 0, 0, ""
        if price_change < -5:  # Terlalu turun, avoid buying at bottom loss
            return False, 0, 0, ""

    sinyal = 0
    sinyal_list = []

    rsi = calc_rsi(hist)
    if rsi < 50: sinyal += 1; sinyal_list.append(f"RSI={rsi:.0f}✅")

    if vol_ratio >= 1.5: sinyal += 1; sinyal_list.append(f"Vol{vol_ratio:.1f}x✅")

    macd, signal, _ = calc_macd(hist)
    if macd > signal * 0.95: sinyal += 1; sinyal_list.append("MACD✅")

    alligator_bull, _ = calc_alligator(hist)
    if alligator_bull: sinyal += 1; sinyal_list.append("Alligator✅")

    if len(hist) >= 20:
        upper, mid, lower = calc_bollinger(hist)
        bw = (upper - lower) / mid * 100 if mid > 0 else 100
        if bw < 5.0: sinyal += 1; sinyal_list.append("BB Squeeze✅")

    if sinyal < 4:
        return False, ob_tier, sinyal, ""

    desc = f"{ob_label} | {' | '.join(sinyal_list)}"
    bypass_ai = ob_tier >= 2  # bypass AI kalau volume tier 2 atau 3
    if bypass_ai:
        desc = "BYPASS_AI|" + desc
    log(f"🐋 Pre-Spike detected: {pair_labels.get(pair_id, pair_id)} | {desc}")
    return True, ob_tier, sinyal, desc

def check_spike_alerts():
    """
    Deteksi coin yang naik >20% dalam 1 jam terakhir
    Kirim notif Telegram sebagai peluang pullback
    """
    global spike_notified
    now = time.time()

    for pair_id in all_pairs:
        if pair_id in open_positions:
            continue
        hist = price_history.get(pair_id, [])
        if len(hist) < 60:
            continue

        price_now  = hist[-1]
        price_1h   = hist[-60]  # 60 menit lalu (1 tick = 1 menit)
        if price_1h <= 0:
            continue

        change_pct = (price_now - price_1h) / price_1h * 100

        if change_pct >= 20:
            # Cek sudah dinotif belum dalam 2 jam terakhir
            last_notif = spike_notified.get(pair_id, 0)
            if now - last_notif < 7200:
                continue

            label = pair_labels.get(pair_id, pair_id)
            spike_notified[pair_id] = now
            log(f"🚀 SPIKE ALERT: {label} naik {change_pct:.1f}% dalam 1 jam! (log only)")


def check_signals(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 30:
        return 0, [], {}

    price = prices.get(pair_id, 0)
    count, reasons = 0, []
    details = {}

    rsi = calc_rsi(hist)
    details["rsi"] = rsi
    if rsi < 40: count += 1; reasons.append(f"RSI={rsi:.0f}✅")

    sma5 = calc_sma(hist, 5); sma20 = calc_sma(hist, 20)
    details["sma5"] = sma5; details["sma20"] = sma20
    if sma5 > sma20: count += 1; reasons.append("SMA↑✅")

    macd, signal, hist_val = calc_macd(hist)
    details["macd"] = macd; details["macd_signal"] = signal
    if macd > signal: count += 1; reasons.append("MACD↑✅")

    upper, mid, lower = calc_bollinger(hist)
    details["bb_lower"] = lower; details["bb_upper"] = upper
    if price <= lower * 1.01: count += 1; reasons.append("BB✅")

    ema9 = calc_ema(hist, 9); ema21 = calc_ema(hist, 21)
    details["ema9"] = ema9; details["ema21"] = ema21
    if ema9 > ema21: count += 1; reasons.append("EMA↑✅")

    if len(vol_h) >= 5 and vol_h[-1] > (sum(vol_h[:-1])/(len(vol_h)-1)) * 1.2:
        count += 1; reasons.append("Vol↑✅")

    stoch = calc_stoch_rsi(hist)
    details["stoch_rsi"] = stoch
    if stoch < 20: count += 1; reasons.append("StochRSI✅")

    cci = calc_cci(hist)
    details["cci"] = cci
    if cci < -100: count += 1; reasons.append("CCI✅")

    wr = calc_williams_r(hist)
    details["williams_r"] = wr
    if wr < -80: count += 1; reasons.append("WR✅")

    roc = calc_roc(hist)
    details["roc"] = roc
    if roc > 0: count += 1; reasons.append("ROC↑✅")

    atr = calc_atr(hist)
    details["atr"] = atr
    if atr > price * 0.005: count += 1; reasons.append("ATR✅")

    obv = calc_obv(hist, vol_h)
    details["obv"] = obv
    if obv > 0: count += 1; reasons.append("OBV↑✅")

    alligator_bull, alligator_desc = calc_alligator(hist)
    details["alligator"] = alligator_bull
    if alligator_bull: count += 1; reasons.append("Alligator✅")

    psar_bull, psar_desc = calc_parabolic_sar(hist)
    details["psar"] = psar_bull
    if psar_bull: count += 1; reasons.append("PSAR✅")

    adx, strong_trend = calc_adx(hist)
    details["adx"] = adx
    if strong_trend: count += 1; reasons.append(f"ADX={adx:.0f}✅")

    momentum = calc_momentum(hist)
    details["momentum"] = momentum
    if momentum > 0: count += 1; reasons.append("MOM↑✅")

    vwap = calc_vwap(hist, vol_h)
    details["vwap"] = vwap
    if price < vwap: count += 1; reasons.append("VWAP✅")

    return count, reasons, details

# ── AI Analisa Internal ────────────────────────────────
def ai_analyze(pair_id, count, reasons, details):
    label = pair_labels.get(pair_id, pair_id)
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])

    if len(hist) < 10:
        return 0, "Data tidak cukup"

    score = 0
    analysis = []

    signal_score = min(count / 17 * 40, 40)
    score += signal_score
    analysis.append(f"Sinyal: {count}/17 (+{signal_score:.0f})")

    rsi = details.get("rsi", 50)
    if 25 <= rsi <= 35:
        score += 15; analysis.append("RSI sangat oversold (+15)")
    elif 35 < rsi <= 40:
        score += 10; analysis.append("RSI oversold (+10)")
    elif rsi > 70:
        score -= 20; analysis.append("RSI overbought (-20)")

    adx = details.get("adx", 0)
    if adx > 40:
        score += 15; analysis.append(f"Trend sangat kuat ADX={adx:.0f} (+15)")
    elif adx > 25:
        score += 8; analysis.append(f"Trend kuat ADX={adx:.0f} (+8)")

    if len(vol_h) >= 5:
        avg_vol  = sum(vol_h[:-1]) / (len(vol_h) - 1)
        curr_vol = vol_h[-1]
        if avg_vol > 0:
            vol_ratio = curr_vol / avg_vol
            if vol_ratio >= 3:
                score += 10; analysis.append(f"Volume spike {vol_ratio:.1f}x (+10)")
            elif vol_ratio >= 1.5:
                score += 5; analysis.append(f"Volume tinggi {vol_ratio:.1f}x (+5)")

    if len(hist) >= 5:
        recent_change = (hist[-1] - hist[-5]) / hist[-5] * 100
        if -3 <= recent_change <= -0.5:
            score += 10; analysis.append(f"Harga turun sehat {recent_change:.1f}% (+10)")
        elif recent_change > 5:
            score -= 10; analysis.append(f"Harga naik cepat {recent_change:.1f}% (-10)")

    if details.get("alligator"):
        score += 5; analysis.append("Alligator bullish (+5)")

    if details.get("vwap"):
        score += 5; analysis.append("Di bawah VWAP (+5)")

    if calc_rsi_divergence(hist):
        score += 10; analysis.append("RSI Divergence Bullish (+10) 🎯")

    # ── Faktor 9: Orderbook Pressure (10 poin max) ────
    buy_pct, sell_pct, ob_desc = get_orderbook_pressure(pair_id)
    if buy_pct >= 65:
        score += 10; analysis.append(f"Orderbook bullish {buy_pct:.0f}% (+10) 📊")
    elif buy_pct >= 55:
        score += 5; analysis.append(f"Orderbook cenderung buy {buy_pct:.0f}% (+5) 📊")
    elif sell_pct >= 65:
        score -= 10; analysis.append(f"Orderbook bearish {sell_pct:.0f}% (-10) 📊")
    elif sell_pct >= 55:
        score -= 5; analysis.append(f"Orderbook cenderung sell {sell_pct:.0f}% (-5) 📊")

    fg_value, fg_label = get_fear_greed()
    if fg_value <= 25:
        score += 10; analysis.append(f"Extreme Fear {fg_value} (+10) 😱")
    elif fg_value <= 45:
        score += 5; analysis.append(f"Fear {fg_value} (+5) 😰")
    elif fg_value >= 75:
        score -= 10; analysis.append(f"Extreme Greed {fg_value} (-10) 🤑")
    elif fg_value >= 55:
        score -= 5; analysis.append(f"Greed {fg_value} (-5) 😏")

    score = max(0, min(100, score))

    if score >= 80:
        verdict = "🟢 SANGAT YAKIN BELI"
    elif score >= AI_MIN_SCORE:
        verdict = "🟡 YAKIN BELI"
    elif score >= 50:
        verdict = "🟠 KURANG YAKIN — SKIP"
    else:
        verdict = "🔴 TIDAK YAKIN — SKIP"

    summary = f"{verdict} | Skor: {score:.0f}%\n" + " | ".join(analysis[:4])
    log(f"🧠 AI [{label}]: {score:.0f}% — {verdict}")
    return score, summary

# ── Claude API ─────────────────────────────────────────
def claude_analyze(pair_id, count, reasons, details):
    if not CLAUDE_KEY:
        return ai_analyze(pair_id, count, reasons, details)

    label = pair_labels.get(pair_id, pair_id)
    fg_value, fg_label = get_fear_greed()

    prompt = f"""Kamu adalah analis crypto trading profesional untuk pasar Indonesia (Indodax).
Analisa apakah ini waktu yang tepat untuk BUY {label}:

Data teknikal:
- Sinyal aktif: {count}/17 indikator
- RSI: {details.get('rsi', 50):.1f}
- MACD: {details.get('macd', 0):.4f} vs Signal: {details.get('macd_signal', 0):.4f}
- ADX: {details.get('adx', 0):.1f}
- Momentum: {details.get('momentum', 0):.2f}
- Alligator bullish: {details.get('alligator', False)}
- VWAP: harga {'di bawah' if details.get('vwap') else 'di atas'} VWAP
- Fear & Greed Index: {fg_value} ({fg_label})
- Sinyal aktif: {', '.join(reasons[:8])}

Berikan:
1. Skor keyakinan BUY (0-100)
2. Alasan singkat (max 2 kalimat)

Format jawaban HANYA:
SKOR: [angka]
ALASAN: [alasan]"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        data = r.json()
        text = data["content"][0]["text"]
        lines = text.strip().split("\n")
        score  = 50
        alasan = "Tidak ada analisa"
        for line in lines:
            if line.startswith("SKOR:"):
                try: score = int(line.replace("SKOR:", "").strip())
                except: pass
            elif line.startswith("ALASAN:"):
                alasan = line.replace("ALASAN:", "").strip()
        score = max(0, min(100, score))
        verdict = "🟢 YAKIN BELI" if score >= AI_MIN_SCORE else "🔴 SKIP"
        summary = f"{verdict} | Skor: {score}%\n{alasan}"
        log(f"🤖 Claude [{label}]: {score}% — {verdict}")
        return score, summary
    except Exception as e:
        log(f"⚠️ Claude API error: {e}, pakai internal AI")
        return ai_analyze(pair_id, count, reasons, details)

# ── Momentum prediktif ─────────────────────────────────
def check_momentum_entry(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 5 or len(vol_h) < 5:
        return False, ""
    signals = []
    avg_vol  = sum(vol_h[:-1]) / (len(vol_h) - 1) if len(vol_h) > 1 else 0
    curr_vol = vol_h[-1]
    if avg_vol > 0 and curr_vol >= avg_vol * VOL_SPIKE:
        signals.append(f"Vol Spike {curr_vol/avg_vol:.1f}x🚀")
    if len(hist) >= 3 and hist[-1] > hist[-2] > hist[-3]:
        signals.append("Price Up 3✅")
    if len(hist) >= 26:
        macd, signal, _ = calc_macd(hist)
        if macd > signal * 0.95:
            signals.append("MACD Cross✅")
    if len(hist) >= 20:
        upper, mid, lower = calc_bollinger(hist)
        bw = (upper - lower) / mid * 100 if mid > 0 else 100
        if bw < 3.0:
            signals.append("BB Squeeze✅")
    rsi = calc_rsi(hist)
    if 30 <= rsi <= 50:
        signals.append(f"RSI={rsi:.0f}↑✅")
    return len(signals) >= 2, " | ".join(signals)

# ── Exit signal ────────────────────────────────────────
def check_exit_signal(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 5:
        return False, ""
    exit_signals = []
    rsi = calc_rsi(hist)
    if rsi > 70: exit_signals.append(f"RSI={rsi:.0f} overbought")
    if len(vol_h) >= 3 and vol_h[-1] < vol_h[-2] < vol_h[-3]:
        exit_signals.append("Volume turun")
    if len(hist) >= 26:
        macd, signal, hist_val = calc_macd(hist)
        if hist_val < 0: exit_signals.append("MACD turun")
    if len(hist) >= 3 and hist[-1] < hist[-2] < hist[-3]:
        exit_signals.append("Harga turun 2x")
    alligator_bull, _ = calc_alligator(hist)
    if not alligator_bull: exit_signals.append("Alligator bearish")
    return len(exit_signals) >= 2, " | ".join(exit_signals)

# ── Pilih pair terbaik ─────────────────────────────────
def choose_best_pairs():
    fg_value, fg_label = get_fear_greed()
    if fg_value >= 80:
        log(f"⛔ Extreme Greed ({fg_value}) — skip semua beli!")
        send_telegram(f"⛔ *Extreme Greed {fg_value}*\nMarket terlalu greedy — skip beli dulu!")
        return []

    SKIP_PAIRS = {"usdt_idr", "usdc_idr", "busd_idr"}

    candidates = []
    for pair_id in all_pairs:
        if pair_id in SKIP_PAIRS: continue
        if pair_id in open_positions: continue
        if is_blacklisted(pair_id): continue

        # ── Filter: Skip kalau harga naik terlalu cepat (beli di pucuk) ──
        if is_price_spiking(pair_id): continue

        curr_price = prices.get(pair_id, 0)

        if pair_id in sold_prices:
            last_sell = sold_prices[pair_id]
            if curr_price > last_sell:
                log(f"⛔ Skip {pair_labels.get(pair_id, pair_id)} — harga {fmt(curr_price)} > harga jual {fmt(last_sell)}")
                continue
            else:
                log(f"✅ {pair_labels.get(pair_id, pair_id)} harga turun ke {fmt(curr_price)} ≤ {fmt(last_sell)} → boleh beli!")
                del sold_prices[pair_id]

        count, reasons, details = check_signals(pair_id)
        has_momentum, momentum_desc = check_momentum_entry(pair_id)

        # ── Pre-Spike Entry (Batch 2) ──────────────────
        layak, ob_tier, ps_sinyal, ps_desc = check_presike_entry(pair_id)
        if layak and ob_tier >= 1:
            candidates.append((pair_id, ps_sinyal, reasons, details, "presike", ps_desc))
            continue

        if count >= MIN_SIGNALS:
            candidates.append((pair_id, count, reasons, details, "standard", ""))
        elif has_momentum and count >= MIN_SIGNALS - 2:
            candidates.append((pair_id, count, reasons, details, "momentum", momentum_desc))
    candidates.sort(key=lambda x: (x[4] == "standard", x[1]), reverse=True)
    return candidates

# ── Order ──────────────────────────────────────────────
def place_buy(pair_id, price, idr_amount):
    market_price = int(price * 1.03)
    log(f"📤 BUY {pair_id} | IDR:{int(idr_amount)} | Harga:{market_price} (market)")
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "buy",
        "price": str(market_price),
        "idr":   str(int(idr_amount)),
 # Check slippage sebelum execute
scan_price = prices.get(pair_id, 0)
is_acceptable, slippage = check_slippage(pair_id, scan_price)
if not is_acceptable:
    log(f"❌ BUY REJECTED {pair_labels.get(pair_id, pair_id)} - slippage {slippage:.2f}% > {SLIPPAGE_TOLERANCE}%")
    return False       
    })

def place_sell(pair_id, price, qty_to_sell):
    coin = pair_id.replace("_idr", "")
    actual_qty, coin_key = get_coin_balance(coin)
    if actual_qty <= 0:
        log(f"⚠️ Saldo {coin} = 0, hapus posisi")
        return {"success": 1, "zero_balance": True}
    sell_qty     = min(qty_to_sell, actual_qty)
    qty_str      = format_qty(coin_key, sell_qty)
    market_price = int(price * 0.97)
    log(f"📤 SELL {pair_id} | {coin_key}:{qty_str} | Harga:{market_price} (market)")
    result = indodax_request("trade", {
        "pair":    pair_id,
        "type":    "sell",
        "price":   str(market_price),
        coin_key:  qty_str,
    })
    if result and "decimal" in str(result.get("error", "")):
        qty_str_int = str(int(sell_qty))
        log(f"⚠️ Decimal error → retry integer: {coin_key}:{qty_str_int}")
        INTEGER_COINS.add(coin_key.lower())
        result = indodax_request("trade", {
            "pair":    pair_id,
            "type":    "sell",
            "price":   str(market_price),
            coin_key:  qty_str_int,
# Check slippage sebelum execute
scan_price = prices.get(pair_id, 0)
is_acceptable, slippage = check_slippage(pair_id, scan_price)
if not is_acceptable:
    log(f"❌ SELL REJECTED {pair_labels.get(pair_id, pair_id)} - slippage {slippage:.2f}% > {SLIPPAGE_TOLERANCE}%")
    return False            
        })
    return result

# ── Cancel open orders ─────────────────────────────────
def cancel_all_open_orders():
    log("🗑️ Cek dan cancel open orders yang stuck...")
    result = indodax_request("openOrders", {"pair": ""})
    if not result or result.get("success") != 1:
        return
    orders = result.get("return", {}).get("orders", {})
    count = 0
    if isinstance(orders, list):
        for order in orders:
            pair_id  = order.get("pair", "")
            order_id = order.get("order_id")
            if order_id and pair_id:
                cancel = indodax_request("cancelOrder", {
                    "pair":     pair_id,
                    "order_id": str(order_id),
                    "type":     order.get("type", "buy"),
                })
                if cancel and cancel.get("success") == 1:
                    count += 1
                    log(f"✅ Cancel order {order_id} {pair_id}")
    elif isinstance(orders, dict):
        for pair_id, order_list in orders.items():
            if isinstance(order_list, dict):
                order_list = [order_list]
            for order in order_list:
                order_id = order.get("order_id")
                if order_id:
                    cancel = indodax_request("cancelOrder", {
                        "pair":     pair_id,
                        "order_id": str(order_id),
                        "type":     order.get("type", "buy"),
                    })
                    if cancel and cancel.get("success") == 1:
                        count += 1
                        log(f"✅ Cancel order {order_id} {pair_id}")
    if count > 0:
        log(f"✅ {count} open order berhasil dibatalkan")
        send_telegram(f"🗑️ *{count} Open Order Dibatalkan*\nSemua order stuck sudah dibersihkan!")

# ── Restore wallet ─────────────────────────────────────
def restore_positions_from_wallet():
    global open_positions
    log("💼 Baca wallet Indodax untuk restore posisi...")
    balances = get_all_balances()
    if not balances:
        return
    restored = []
    for coin, qty in balances.items():
        if coin == "idr":
            continue
        qty = float(qty)
        if qty <= 0:
            continue
        pair_id = f"{coin}_idr"
        if pair_id not in all_pairs:
            continue
        curr_price = prices.get(pair_id, 0)
        if curr_price <= 0:
            continue
        est_value = qty * curr_price
        if est_value < 5000:
            continue
        open_positions[pair_id] = {
            "buy_price":      curr_price,
            "qty":            qty,
            "idr":            est_value,
            "peak_price":     curr_price,
            "sell_attempts":  0,
            "last_sell_time": 0,
            "entry_type":     "restored",
            "ai_score":       0
        }
        restored.append(f"{coin.upper()}: {qty:.4f} (~{fmt(est_value)})")
        log(f"✅ Restore posisi: {coin.upper()} {qty:.4f} @ {fmt(curr_price)}")
    if restored:
        send_telegram(
            f"💼 *Posisi Restored dari Wallet:*\n" +
            "\n".join(restored) +
            f"\nTotal: {len(restored)} posisi"
        )

# ── Bot Logic ──────────────────────────────────────────
def bot_tick():
    global modal, total_profit, total_trades, daily_profit, daily_trades, win_streak

    reset_daily()
    get_idr_balance()
    check_daily_summary()
    check_spike_alerts()

    for pair_id in list(open_positions.keys()):
        pos       = open_positions[pair_id]
        buy_price = pos["buy_price"]
        qty       = pos["qty"]
        idr_in    = pos["idr"]
        label     = pair_labels.get(pair_id, pair_id)
        curr      = prices.get(pair_id, buy_price)
        pl        = (curr - buy_price) * qty
        pl_pct    = (curr - buy_price) / buy_price * 100

        if curr > pos["peak_price"]:
            open_positions[pair_id]["peak_price"] = curr

        peak       = open_positions[pair_id]["peak_price"]
        trail_drop = (peak - curr) / peak * 100

        log(f"📊 {label} | Beli:{fmt(buy_price)} | Kini:{fmt(curr)} | Peak:{fmt(peak)} | P/L:{fmt(pl)} ({pl_pct:.2f}%)")

        should_sell = False
        sell_reason = ""

        has_exit, exit_desc = check_exit_signal(pair_id)

        # ATR Trailing Stop — dinamis berdasarkan volatilitas
        atr = calc_atr(price_history.get(pair_id, []))
        atr_trail_pct = (atr * 2 / peak) * 100 if peak > 0 else TRAIL_PCT
        effective_trail = max(atr_trail_pct, TRAIL_PCT)

        # Pre-Spike TP khusus — jual saat naik 10%
        entry_type_pos = pos.get("entry_type", "standard")
        if entry_type_pos == "presike" and pl_pct >= 10:
            should_sell = True
            sell_reason = f"🐋 Pre-Spike TP {pl_pct:.1f}% | P/L:{fmt(pl)}"
        elif pl >= TAKE_PROFIT and trail_drop >= effective_trail:
            should_sell = True
            sell_reason = f"💰 Profit {fmt(pl)} | ATR Trail {trail_drop:.1f}%"
        elif pl > (idr_in * 0.006) and has_exit:  # exit hanya kalau profit sudah nutup fee
            should_sell = True
            sell_reason = f"📉 Exit: {exit_desc} | P/L:{fmt(pl)}"
        elif pl_pct <= -HARD_STOP:
            should_sell = True
            sell_reason = f"🚨 Hard Stop {pl_pct:.2f}%"
        elif trail_drop >= effective_trail and pl_pct <= -1:
            should_sell = True
            sell_reason = f"🛑 ATR Trailing Stop {trail_drop:.1f}%"

        if should_sell:
            attempts  = pos.get("sell_attempts", 0)
            last_sell = pos.get("last_sell_time", 0)

            if attempts >= SELL_RETRY:
                log(f"⚠️ {label} gagal jual {attempts}x!")
                send_telegram(
                    f"⚠️ *{label} gagal jual {attempts}x!*\n"
                    f"Jual manual di Indodax!\n"
                    f"Qty: {format_qty(pair_id.replace('_idr',''), qty)} {pair_id.replace('_idr','').upper()}"
                )
                del open_positions[pair_id]
                continue

            if time.time() - last_sell < 300 and attempts > 0:
                log(f"⏳ {label} tunggu 5 menit retry...")
                continue

            result = place_sell(pair_id, curr, qty)
            if result and result.get("success") == 1:
                get_idr_balance()
                total_profit += pl
                total_trades += 1
                daily_profit += pl
                daily_trades += 1
                del open_positions[pair_id]
                if not result.get("zero_balance"):
                    log(f"{sell_reason} | {label} | P/L:{fmt(pl)}")
                    sold_prices[pair_id] = curr
                    fee_beli  = idr_in * 0.003
                    fee_jual  = (curr * qty) * 0.003
                    pl_bersih = pl - fee_beli - fee_jual
                    if pl > 0:
                        win_streak += 1
                        log(f"🏆 Win streak: {win_streak}")
                    else:
                        win_streak = 0
                    send_telegram(
                        f"{sell_reason}\n*{label}*\n"
                        f"Harga: {fmt(curr)}\n"
                        f"P/L Kotor: {fmt(pl)}\n"
                        f"Fee: {fmt(fee_beli + fee_jual)}\n"
                        f"P/L Bersih: {fmt(pl_bersih)}\n"
                        f"Win Streak: {win_streak}🏆\n"
                        f"Total Profit: {fmt(total_profit)}\n"
                        f"Modal: {fmt(modal)}\n"
                        f"Total Trade: {total_trades}"
                    )
                    if pl < 0:
                        add_blacklist(pair_id, pl)
            else:
                open_positions[pair_id]["sell_attempts"] = attempts + 1
                open_positions[pair_id]["last_sell_time"] = time.time()
                log(f"⚠️ Gagal jual {label} attempt {attempts+1}/{SELL_RETRY}: {result}")

    slots = MAX_TRADES - len(open_positions)
    if slots <= 0:
        log(f"📊 {len(open_positions)}/{MAX_TRADES} posisi penuh")
        return

    if not check_market_trend():
        log("⛔ Market DOWNTREND — skip beli")
        return

    candidates = choose_best_pairs()
    if not candidates:
        log(f"⏳ Scan {TOP_N_PAIRS} pair — menunggu {MIN_SIGNALS}/17 sinyal... ({len(open_positions)}/{MAX_TRADES} posisi)")
        return

    idr_per_trade = (modal * 0.95) / MAX_TRADES

    # Anti-Martingale — naikkan modal kalau lagi profit streak
    am_multiplier = min(1.0 + (win_streak * 0.1), 1.5)
    idr_per_trade = idr_per_trade * am_multiplier
    if win_streak > 0:
        log(f"🏆 Anti-Martingale aktif | Win streak: {win_streak} | Multiplier: {am_multiplier:.1f}x")

    for pair_id, count, reasons, details, entry_type, momentum_desc in candidates[:slots]:
        if idr_per_trade < 10000:
            log(f"⚠️ Modal per trade terlalu kecil: {fmt(idr_per_trade)}")
            break

        label = pair_labels.get(pair_id, pair_id)

        log(f"🧠 AI menganalisa {label}...")
        ai_score, ai_summary = claude_analyze(pair_id, count, reasons, details)

        # Pre-spike entry pakai AI threshold 50%, standard/momentum pakai AI_MIN_SCORE
        # Pre-spike: bypass AI kalau volume IDR sangat besar (tier 2-3)
        if entry_type == "presike" and momentum_desc.startswith("BYPASS_AI|"):
            log(f"🐋 Pre-Spike BYPASS AI — volume besar, langsung beli {label}!")
        else:
            ai_threshold = 50 if entry_type == "presike" else AI_MIN_SCORE
            if ai_score < ai_threshold:
                log(f"🔴 AI skip {label} | Skor: {ai_score}% < {ai_threshold}%")
                continue

        price = prices.get(pair_id, 0)
        qty   = idr_per_trade / price

        if entry_type == "presike":
            clean_desc = momentum_desc.replace("BYPASS_AI|", "")
            reason = f"🐋 PRE-SPIKE | {clean_desc}"
        elif entry_type == "momentum":
            reason = f"🚀 MOMENTUM [{count}/17] {momentum_desc}"
        else:
            reason = f"[{count}/17] " + " | ".join(reasons[:6])

        result = place_buy(pair_id, price, idr_per_trade)
        if result and result.get("success") == 1:
            get_idr_balance()
            total_trades += 1
            open_positions[pair_id] = {
                "buy_price":      price,
                "qty":            qty,
                "idr":            idr_per_trade,
                "peak_price":     price,
                "sell_attempts":  0,
                "last_sell_time": 0,
                "entry_type":     entry_type,
                "ai_score":       ai_score
            }
            log(f"🛒 BELI {label} | {fmt(price)} | {qty:.8f} unit | AI:{ai_score}%")
            buy_pct, sell_pct, ob_desc = get_orderbook_pressure(pair_id)
            send_telegram(
                f"🛒 *BELI {label}*\n"
                f"Harga: {fmt(price)}\n"
                f"Unit: {qty:.8f}\n"
                f"Modal/trade: {fmt(idr_per_trade)}\n"
                f"Sinyal: {reason}\n"
                f"🧠 AI: {ai_summary}\n"
                f"📊 Orderbook: {ob_desc}\n"
                f"Posisi: {len(open_positions)}/{MAX_TRADES}\n"
                f"Modal: {fmt(modal)}"
            )
            time.sleep(2)
        else:
            log(f"⚠️ Gagal beli {label}: {result}")

# ── Main ───────────────────────────────────────────────
def main():
    log("🚀 IndoBot v9 AI dimulai...")

    while not test_api():
        log("⏳ Retry API..."); time.sleep(30)

    while not fetch_all_pairs():
        log("⏳ Retry load pair..."); time.sleep(30)

    fetch_prices()
    cancel_all_open_orders()
    restore_positions_from_wallet()

    fg_value, fg_label = get_fear_greed()

    send_telegram(
        f"🚀 *IndoBot v9 AI AKTIF*\n"
        f"Modal: Auto dari Indodax (max {fmt(MAX_MODAL)})\n"
        f"Take Profit: {fmt(TAKE_PROFIT)} per trade\n"
        f"Trailing Stop: {TRAIL_PCT}%\n"
        f"Hard Stop Loss: {HARD_STOP}%\n"
        f"Max Posisi: {MAX_TRADES} coin\n"
        f"Scan: Top {TOP_N_PAIRS} pair\n"
        f"Min sinyal: {MIN_SIGNALS}/17\n"
        f"🧠 AI min score: {AI_MIN_SCORE}%\n"
        f"Uptrend: {UPTREND_PCT}%\n"
        f"Blacklist: {BLACKLIST_HR} jam\n"
        f"Order: Market style (instant) ⚡\n"
        f"Price Ceiling: Aktif 🔒\n"
        f"Anti-Spike Filter: Aktif 🛡️\n"
        f"Orderbook Analysis: Aktif 📊\n"
        f"Pre-Spike Detection: Aktif 🐋\n"
        f"AI: {'Claude API' if CLAUDE_KEY else 'Internal AI (Gratis)'}\n"
        f"😱 Fear & Greed: {fg_value} ({fg_label})\n"
        f"Daily Report: Jam 21:00 WIB 📊\n"
        f"Mode: Non-stop seumur hidup! 🔄"
    )

    while True:
        try:
            fetch_prices()
            bot_tick()
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            send_telegram(
                f"🔴 *IndoBot STOP*\n"
                f"Total Profit: {fmt(total_profit)}\n"
                f"Total Trade: {total_trades}\n"
                f"Modal: {fmt(modal)}"
            )
            break
        except Exception as e:
            log(f"⚠️ Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
