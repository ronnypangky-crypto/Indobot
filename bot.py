import os
import time
import hmac
import hashlib
import requests
import urllib.parse
from datetime import datetime

# ── Konfigurasi ────────────────────────────────────────
# Indodax
INDODAX_API_KEY    = os.environ.get("INDODAX_API_KEY", "")
INDODAX_SECRET_KEY = os.environ.get("INDODAX_SECRET_KEY", "")

# Tokocrypto
TOKO_API_KEY       = os.environ.get("TOKO_API_KEY", "")
TOKO_SECRET_KEY    = os.environ.get("TOKO_SECRET_KEY", "")

# Telegram
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Trading config
TAKE_PROFIT  = float(os.environ.get("TAKE_PROFIT", "1000"))
TRAIL_PCT    = float(os.environ.get("TRAIL_PCT", "1.5"))
HARD_STOP    = float(os.environ.get("HARD_STOP", "5.0"))
MAX_MODAL    = float(os.environ.get("MAX_MODAL", "2000000"))
MAX_TRADES   = int(os.environ.get("MAX_TRADES", "3"))
TOP_N_PAIRS  = int(os.environ.get("TOP_N_PAIRS", "20"))
MIN_SIGNALS  = int(os.environ.get("MIN_SIGNALS", "10"))
BLACKLIST_HR = int(os.environ.get("BLACKLIST_HR", "48"))
UPTREND_PCT  = float(os.environ.get("UPTREND_PCT", "50"))
SELL_RETRY   = int(os.environ.get("SELL_RETRY", "3"))
VOL_SPIKE    = float(os.environ.get("VOL_SPIKE", "3.0"))
SCAN_INTERVAL = 60

INDODAX_TAPI = "https://indodax.com/tapi"
TOKO_BASE    = "https://api.tokocrypto.com"

# ── State ──────────────────────────────────────────────
indodax_modal  = 0.0
toko_modal     = 0.0
total_profit   = 0.0
total_trades   = 0

# Posisi per exchange
indodax_positions = {}  # {pair_id: {buy_price, qty, idr, peak_price, ...}}
toko_positions    = {}

price_history  = {}
volume_history = {}
common_pairs   = []     # pair yang ada di kedua exchange
indodax_labels = {}
toko_labels    = {}
indodax_prices = {}
toko_prices    = {}
blacklist      = {}
daily_loss     = 0.0
daily_start    = time.time()

# ── Helpers ────────────────────────────────────────────
def fmt(n):
    if n >= 1e9:  return f"Rp {n/1e9:.2f}M"
    if n >= 1e6:  return f"Rp {n/1e6:.2f}jt"
    if n >= 1e3:  return f"Rp {n/1e3:.0f}rb"
    return f"Rp {n:.0f}"

def now_str():
    return datetime.now().strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)

# ── Telegram ───────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    text = f"🤖 *IndoBot v7*\n{msg}\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log(f"TG Error: {e}")

def send_balance_update():
    """Kirim update modal kedua exchange ke Telegram"""
    send_telegram(
        f"💼 *Update Modal*\n"
        f"Indodax: {fmt(indodax_modal)}\n"
        f"Tokocrypto: {fmt(toko_modal)}\n"
        f"Total: {fmt(indodax_modal + toko_modal)}\n"
        f"Total Profit: {fmt(total_profit)}\n"
        f"Total Trade: {total_trades}"
    )

# ── INDODAX API ────────────────────────────────────────
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
    sign = hmac.new(INDODAX_SECRET_KEY.encode(), body.encode(), hashlib.sha512).hexdigest()
    headers = {"Key": INDODAX_API_KEY, "Sign": sign}
    try:
        r = requests.post(INDODAX_TAPI, data=data, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        log(f"Indodax API Error [{method}]: {e}")
        return None

def get_indodax_balance():
    global indodax_modal
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr = float(result["return"]["balance"].get("idr", 0))
        indodax_modal = min(idr, MAX_MODAL)
        return result["return"]["balance"]
    return {}

def get_indodax_coin_balance(coin):
    bal = get_indodax_balance()
    for key in [coin, coin.lower(), coin.upper()]:
        if key in bal and float(bal[key]) > 0:
            return float(bal[key]), key
    return 0, coin

# ── TOKOCRYPTO API ─────────────────────────────────────
def toko_request(endpoint, params=None, method="GET"):
    if params is None:
        params = {}
    params["timestamp"] = str(int(time.time() * 1000))
    params["recvWindow"] = "5000"
    query = urllib.parse.urlencode(params)
    sign  = hmac.new(TOKO_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sign
    headers = {"X-MBX-APIKEY": TOKO_API_KEY}
    try:
        url = f"{TOKO_BASE}{endpoint}"
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=15)
        else:
            r = requests.post(url, params=params, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        log(f"Tokocrypto API Error [{endpoint}]: {e}")
        return None

def get_toko_balance():
    global toko_modal
    result = toko_request("/open/v1/account/spot", method="GET")
    if result and result.get("code") == 0:
        balances = result.get("data", {}).get("accountAssets", [])
        for asset in balances:
            if asset.get("asset") == "IDR":
                idr = float(asset.get("free", 0))
                toko_modal = min(idr, MAX_MODAL)
                return balances
    return []

def get_toko_coin_balance(coin):
    result = toko_request("/open/v1/account/spot", method="GET")
    if result and result.get("code") == 0:
        balances = result.get("data", {}).get("accountAssets", [])
        for asset in balances:
            if asset.get("asset", "").upper() == coin.upper():
                return float(asset.get("free", 0))
    return 0

# ── Test koneksi kedua exchange ────────────────────────
def test_apis():
    global indodax_modal, toko_modal

    # Test Indodax
    log("🔑 Test Indodax API...")
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr = float(result["return"]["balance"].get("idr", 0))
        indodax_modal = min(idr, MAX_MODAL)
        log(f"✅ Indodax OK! Saldo: {fmt(idr)}")
    else:
        log(f"❌ Indodax API gagal!")
        return False

    # Test Tokocrypto
    log("🔑 Test Tokocrypto API...")
    toko_bal = get_toko_balance()
    if toko_modal >= 0:
        log(f"✅ Tokocrypto OK! Saldo: {fmt(toko_modal)}")
    else:
        log(f"❌ Tokocrypto API gagal!")
        return False

    send_telegram(
        f"✅ *Kedua Exchange Konek!*\n"
        f"Indodax IDR: {fmt(indodax_modal)}\n"
        f"Tokocrypto IDR: {fmt(toko_modal)}\n"
        f"Total Modal: {fmt(indodax_modal + toko_modal)}"
    )
    return True

# ── Fetch pair yang ada di KEDUA exchange ──────────────
def fetch_common_pairs():
    global common_pairs, indodax_labels, toko_labels, indodax_prices, toko_prices, price_history, volume_history

    # Fetch Indodax pairs
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=10)
        d = r.json()
        indodax_tickers = d.get("tickers", {})
    except:
        log("⚠️ Gagal fetch Indodax pairs")
        return False

    # Fetch Tokocrypto pairs
    try:
        r = requests.get(f"{TOKO_BASE}/open/v1/market/tickers", timeout=10)
        d = r.json()
        toko_tickers = {}
        if d.get("code") == 0:
            for item in d.get("data", []):
                symbol = item.get("symbol", "")
                if symbol.endswith("IDR"):
                    coin = symbol.replace("IDR", "").lower()
                    toko_tickers[f"{coin}_idr"] = {
                        "last":   float(item.get("lastPrice", 0)),
                        "vol_idr": float(item.get("quoteVolume", 0)),
                        "open":   float(item.get("openPrice", 0)),
                    }
    except Exception as e:
        log(f"⚠️ Gagal fetch Tokocrypto pairs: {e}")
        return False

    # Cari pair yang ada di KEDUA exchange
    indodax_set = set(k for k in indodax_tickers if k.endswith("_idr"))
    toko_set    = set(toko_tickers.keys())
    both        = indodax_set & toko_set

    # Sort by volume Indodax
    pair_list = []
    for pair_id in both:
        vol = float(indodax_tickers[pair_id].get("vol_idr", 0))
        pair_list.append((pair_id, vol))
    pair_list.sort(key=lambda x: x[1], reverse=True)

    common_pairs = []
    for pair_id, vol in pair_list[:TOP_N_PAIRS]:
        label = pair_id.replace("_idr", "").upper() + "/IDR"
        common_pairs.append(pair_id)
        indodax_labels[pair_id] = label

        # Harga Indodax
        indodax_prices[pair_id] = float(indodax_tickers[pair_id].get("last", 0))

        # Harga Tokocrypto
        toko_prices[pair_id] = toko_tickers[pair_id]["last"]

        if pair_id not in price_history:
            price_history[pair_id]  = []
            volume_history[pair_id] = []

        price_history[pair_id].append(indodax_prices[pair_id])
        volume_history[pair_id].append(vol)

    names = [indodax_labels[p] for p in common_pairs]
    log(f"📡 {len(common_pairs)} pair di kedua exchange: {', '.join(names)}")
    send_telegram(f"📡 *{len(common_pairs)} Pair di Indodax & Tokocrypto:*\n{', '.join(names)}")
    return True

# ── Refresh harga kedua exchange ───────────────────────
def fetch_prices():
    try:
        # Indodax
        r  = requests.get("https://indodax.com/api/summaries", timeout=10)
        d  = r.json()
        td = d.get("tickers", {})
        for pair_id in common_pairs:
            if pair_id in td:
                last = float(td[pair_id].get("last", 0))
                vol  = float(td[pair_id].get("vol_idr", 0))
                if last > 0:
                    indodax_prices[pair_id] = last
                    price_history[pair_id].append(last)
                    if len(price_history[pair_id]) > 100:
                        price_history[pair_id].pop(0)
                    volume_history[pair_id].append(vol)
                    if len(volume_history[pair_id]) > 20:
                        volume_history[pair_id].pop(0)

        # Tokocrypto
        r2 = requests.get(f"{TOKO_BASE}/open/v1/market/tickers", timeout=10)
        d2 = r2.json()
        if d2.get("code") == 0:
            for item in d2.get("data", []):
                symbol = item.get("symbol", "")
                if symbol.endswith("IDR"):
                    coin    = symbol.replace("IDR", "").lower()
                    pair_id = f"{coin}_idr"
                    if pair_id in common_pairs:
                        toko_prices[pair_id] = float(item.get("lastPrice", 0))
    except Exception as e:
        log(f"⚠️ Gagal refresh harga: {e}")

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

def check_signals(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 30:
        return 0, []
    price = indodax_prices.get(pair_id, 0)
    count, reasons = 0, []
    rsi = calc_rsi(hist)
    if rsi < 40: count += 1; reasons.append(f"RSI={rsi:.0f}✅")
    sma5 = calc_sma(hist, 5); sma20 = calc_sma(hist, 20)
    if sma5 > sma20: count += 1; reasons.append("SMA↑✅")
    macd, signal, _ = calc_macd(hist)
    if macd > signal: count += 1; reasons.append("MACD↑✅")
    upper, mid, lower = calc_bollinger(hist)
    if price <= lower * 1.01: count += 1; reasons.append("BB✅")
    ema9 = calc_ema(hist, 9); ema21 = calc_ema(hist, 21)
    if ema9 > ema21: count += 1; reasons.append("EMA↑✅")
    if len(vol_h) >= 5 and vol_h[-1] > (sum(vol_h[:-1])/(len(vol_h)-1)) * 1.2: count += 1; reasons.append("Vol↑✅")
    if calc_stoch_rsi(hist) < 20: count += 1; reasons.append("StochRSI✅")
    if calc_cci(hist) < -100: count += 1; reasons.append("CCI✅")
    if calc_williams_r(hist) < -80: count += 1; reasons.append("WR✅")
    if calc_roc(hist) > 0: count += 1; reasons.append("ROC↑✅")
    if calc_atr(hist) > price * 0.005: count += 1; reasons.append("ATR✅")
    if calc_obv(hist, vol_h) > 0: count += 1; reasons.append("OBV↑✅")
    return count, reasons

def check_momentum(pair_id):
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
        band_width = (upper - lower) / mid * 100 if mid > 0 else 100
        if band_width < 3.0:
            signals.append("BB Squeeze✅")
    rsi = calc_rsi(hist)
    if 30 <= rsi <= 50:
        signals.append(f"RSI={rsi:.0f}↑✅")
    return len(signals) >= 2, " | ".join(signals)

def check_exit_signal(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 5:
        return False, ""
    exit_signals = []
    rsi = calc_rsi(hist)
    if rsi > 70: exit_signals.append(f"RSI={rsi:.0f} overbought")
    if len(vol_h) >= 3 and vol_h[-1] < vol_h[-2] < vol_h[-3]: exit_signals.append("Volume turun")
    if len(hist) >= 26:
        macd, signal, hist_val = calc_macd(hist)
        if hist_val < 0: exit_signals.append("MACD turun")
    if len(hist) >= 3 and hist[-1] < hist[-2] < hist[-3]: exit_signals.append("Harga turun 2x")
    return len(exit_signals) >= 2, " | ".join(exit_signals)

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

# ── Blacklist ──────────────────────────────────────────
def add_blacklist(pair_id, loss):
    global daily_loss
    blacklist[pair_id] = time.time()
    daily_loss += abs(loss)
    label = indodax_labels.get(pair_id, pair_id)
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
    if time.time() - daily_start >= 86400:
        daily_loss  = 0.0
        daily_start = time.time()
        send_telegram("🔄 *Reset Harian* — Daily loss dikosongkan!")

# ── Format qty ─────────────────────────────────────────
INTEGER_COINS = {
    "trollsol", "jellyjelly", "pippin", "whitewhale", "fartcoin",
    "sundog", "zerebro", "siren", "pepe", "shib", "floki",
    "babydoge", "bonk", "myro", "popcat", "neiro", "turbo",
    "moodeng", "pnut", "pengu", "useless", "doge", "anoa",
    "hart", "aura", "giga", "mew", "looks", "buildon"
}

def format_qty(coin, qty):
    if coin.lower() in INTEGER_COINS:
        return str(int(qty))
    return f"{qty:.8f}"

# ── Pilih pair terbaik ─────────────────────────────────
def choose_best_pairs():
    candidates = []
    for pair_id in common_pairs:
        if pair_id in indodax_positions: continue
        if is_blacklisted(pair_id): continue
        count, reasons = check_signals(pair_id)
        has_momentum, momentum_desc = check_momentum(pair_id)
        if count >= MIN_SIGNALS:
            candidates.append((pair_id, count, reasons, "standard", ""))
        elif has_momentum and count >= MIN_SIGNALS - 2:
            candidates.append((pair_id, count, reasons, "momentum", momentum_desc))
    candidates.sort(key=lambda x: (x[3] == "standard", x[1]), reverse=True)
    return candidates

# ── INDODAX Order ──────────────────────────────────────
def indodax_buy(pair_id, price, idr_amount):
    log(f"📤 [INDODAX] BUY {pair_id} | IDR:{int(idr_amount)}")
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "buy",
        "price": str(int(price * 1.01)),
        "idr":   str(int(idr_amount)),
    })

def indodax_sell(pair_id, price, qty):
    coin = pair_id.replace("_idr", "")
    # Auto-detect nama coin dari balance
    _, actual_qty = get_indodax_coin_balance(coin)
    if actual_qty <= 0:
        return {"success": 1, "zero_balance": True}
    sell_qty = min(qty, actual_qty)
    qty_str  = format_qty(coin, sell_qty)
    log(f"📤 [INDODAX] SELL {pair_id} | {coin}:{qty_str}")
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "sell",
        "price": str(int(price * 0.99)),
        coin:    qty_str,
    })

# ── TOKOCRYPTO Order ───────────────────────────────────
def toko_buy(pair_id, price, idr_amount):
    coin   = pair_id.replace("_idr", "").upper()
    symbol = f"{coin}IDR"
    qty    = idr_amount / price
    qty_str = format_qty(coin, qty)
    log(f"📤 [TOKO] BUY {symbol} | qty:{qty_str}")
    return toko_request("/open/v1/orders", {
        "symbol":    symbol,
        "side":      "BUY",
        "type":      "LIMIT",
        "price":     str(int(price * 1.01)),
        "quantity":  qty_str,
    }, method="POST")

def toko_sell(pair_id, price, qty):
    coin    = pair_id.replace("_idr", "").upper()
    symbol  = f"{coin}IDR"
    actual  = get_toko_coin_balance(coin)
    if actual <= 0:
        return {"code": 0, "zero_balance": True}
    sell_qty = min(qty, actual)
    qty_str  = format_qty(coin, sell_qty)
    log(f"📤 [TOKO] SELL {symbol} | qty:{qty_str}")
    return toko_request("/open/v1/orders", {
        "symbol":    symbol,
        "side":      "SELL",
        "type":      "LIMIT",
        "price":     str(int(price * 0.99)),
        "quantity":  qty_str,
    }, method="POST")

# ── Cek dan handle posisi ──────────────────────────────
def handle_position(positions, pair_id, exchange_name, sell_fn, price_dict):
    global total_profit, total_trades, indodax_modal, toko_modal

    if pair_id not in positions:
        return

    pos       = positions[pair_id]
    buy_price = pos["buy_price"]
    qty       = pos["qty"]
    idr_in    = pos["idr"]
    label     = indodax_labels.get(pair_id, pair_id)
    curr      = price_dict.get(pair_id, buy_price)
    pl        = (curr - buy_price) * qty
    pl_pct    = (curr - buy_price) / buy_price * 100

    if curr > pos["peak_price"]:
        positions[pair_id]["peak_price"] = curr

    peak       = positions[pair_id]["peak_price"]
    trail_drop = (peak - curr) / peak * 100

    log(f"📊 [{exchange_name}] {label} | Beli:{fmt(buy_price)} | Kini:{fmt(curr)} | P/L:{fmt(pl)} ({pl_pct:.2f}%)")

    should_sell = False
    sell_reason = ""

    has_exit, exit_desc = check_exit_signal(pair_id)

    if pl >= TAKE_PROFIT and trail_drop >= TRAIL_PCT:
        should_sell = True
        sell_reason = f"💰 Profit {fmt(pl)} | Trailing {trail_drop:.1f}%"
    elif pl > 0 and has_exit:
        should_sell = True
        sell_reason = f"📉 Exit: {exit_desc} | P/L:{fmt(pl)}"
    elif pl_pct <= -HARD_STOP:
        should_sell = True
        sell_reason = f"🚨 Hard Stop {pl_pct:.2f}%"
    elif trail_drop >= TRAIL_PCT and pl_pct <= -1:
        should_sell = True
        sell_reason = f"🛑 Trailing Stop {trail_drop:.1f}%"

    if should_sell:
        attempts  = pos.get("sell_attempts", 0)
        last_sell = pos.get("last_sell_time", 0)

        if attempts >= SELL_RETRY:
            log(f"⚠️ [{exchange_name}] {label} gagal jual {attempts}x!")
            send_telegram(
                f"⚠️ *[{exchange_name}] {label} gagal jual {attempts}x!*\n"
                f"Jual manual di {exchange_name}!\n"
                f"Qty: {qty:.8f}"
            )
            del positions[pair_id]
            return

        if time.time() - last_sell < 300 and attempts > 0:
            return

        result = sell_fn(pair_id, curr, qty)
        success = result and (result.get("success") == 1 or result.get("code") == 0)

        if success:
            total_profit += pl
            total_trades += 1
            del positions[pair_id]

            # Update modal
            if exchange_name == "Indodax":
                indodax_modal += idr_in + pl
            else:
                toko_modal += idr_in + pl

            if not result.get("zero_balance"):
                log(f"{sell_reason} | [{exchange_name}] {label} | P/L:{fmt(pl)}")
                send_telegram(
                    f"{sell_reason}\n"
                    f"*[{exchange_name}] {label}*\n"
                    f"Harga: {fmt(curr)}\n"
                    f"P/L: {fmt(pl)}\n"
                    f"💼 Indodax: {fmt(indodax_modal)} | Tokocrypto: {fmt(toko_modal)}\n"
                    f"Total Profit: {fmt(total_profit)}"
                )
                if pl < 0:
                    add_blacklist(pair_id, pl)
        else:
            positions[pair_id]["sell_attempts"] = attempts + 1
            positions[pair_id]["last_sell_time"] = time.time()
            log(f"⚠️ [{exchange_name}] Gagal jual {label} attempt {attempts+1}: {result}")

# ── Bot Tick ───────────────────────────────────────────
def bot_tick():
    global indodax_modal, toko_modal

    reset_daily()

    # Update modal kedua exchange
    get_indodax_balance()
    get_toko_balance()

    # Cek posisi Indodax
    for pair_id in list(indodax_positions.keys()):
        handle_position(indodax_positions, pair_id, "Indodax", indodax_sell, indodax_prices)

    # Cek posisi Tokocrypto
    for pair_id in list(toko_positions.keys()):
        handle_position(toko_positions, pair_id, "Tokocrypto", toko_sell, toko_prices)

    # Cek slot
    indodax_slots = MAX_TRADES - len(indodax_positions)
    toko_slots    = MAX_TRADES - len(toko_positions)

    if indodax_slots <= 0 and toko_slots <= 0:
        log(f"📊 Penuh: Indodax {len(indodax_positions)}/{MAX_TRADES} | Toko {len(toko_positions)}/{MAX_TRADES}")
        return

    if not check_market_trend():
        log("⛔ Market DOWNTREND — skip beli")
        return

    candidates = choose_best_pairs()
    if not candidates:
        log(f"⏳ Menunggu sinyal {MIN_SIGNALS}/12... (IDX:{len(indodax_positions)}/{MAX_TRADES} | TOKO:{len(toko_positions)}/{MAX_TRADES})")
        return

    # Modal per trade
    idr_per_indodax = (indodax_modal * 0.95) / MAX_TRADES
    idr_per_toko    = (toko_modal * 0.95) / MAX_TRADES

    for pair_id, count, reasons, entry_type, momentum_desc in candidates:
        label = indodax_labels.get(pair_id, pair_id)

        if entry_type == "momentum":
            reason = f"🚀 MOMENTUM [{count}/12] {momentum_desc}"
        else:
            reason = f"[{count}/12] " + " | ".join(reasons)

        indodax_price = indodax_prices.get(pair_id, 0)
        toko_price    = toko_prices.get(pair_id, 0)
        bought        = False

        # Beli di Indodax
        if pair_id not in indodax_positions and indodax_slots > 0 and idr_per_indodax >= 10000:
            qty    = idr_per_indodax / indodax_price
            result = indodax_buy(pair_id, indodax_price, idr_per_indodax)
            if result and result.get("success") == 1:
                indodax_modal -= idr_per_indodax
                total_trades  += 1
                indodax_slots -= 1
                indodax_positions[pair_id] = {
                    "buy_price": indodax_price, "qty": qty,
                    "idr": idr_per_indodax, "peak_price": indodax_price,
                    "sell_attempts": 0, "last_sell_time": 0
                }
                bought = True
                log(f"🛒 [INDODAX] BELI {label} | {fmt(indodax_price)} | {qty:.8f} unit")

        # Beli di Tokocrypto — coin yang sama
        if pair_id not in toko_positions and toko_slots > 0 and idr_per_toko >= 10000 and toko_price > 0:
            qty    = idr_per_toko / toko_price
            result = toko_buy(pair_id, toko_price, idr_per_toko)
            if result and result.get("code") == 0:
                toko_modal  -= idr_per_toko
                total_trades += 1
                toko_slots   -= 1
                toko_positions[pair_id] = {
                    "buy_price": toko_price, "qty": qty,
                    "idr": idr_per_toko, "peak_price": toko_price,
                    "sell_attempts": 0, "last_sell_time": 0
                }
                bought = True
                log(f"🛒 [TOKO] BELI {label} | {fmt(toko_price)} | {qty:.8f} unit")

        if bought:
            send_telegram(
                f"🛒 *BELI {label} di 2 Exchange*\n"
                f"Indodax: {fmt(indodax_price)}\n"
                f"Tokocrypto: {fmt(toko_price)}\n"
                f"Sinyal: {reason}\n"
                f"💼 IDX Modal: {fmt(indodax_modal)} | TOKO Modal: {fmt(toko_modal)}"
            )
            time.sleep(2)

        if indodax_slots <= 0 and toko_slots <= 0:
            break

# ── Main ───────────────────────────────────────────────
def main():
    log("🚀 IndoBot v7 Dual Exchange dimulai...")

    while not test_apis():
        log("⏳ Retry API..."); time.sleep(30)

    while not fetch_common_pairs():
        log("⏳ Retry load pair..."); time.sleep(30)

    send_telegram(
        f"🚀 *IndoBot v7 Dual Exchange AKTIF*\n"
        f"💼 Indodax: {fmt(indodax_modal)}\n"
        f"💼 Tokocrypto: {fmt(toko_modal)}\n"
        f"Take Profit: {fmt(TAKE_PROFIT)} per trade\n"
        f"Trailing Stop: {TRAIL_PCT}%\n"
        f"Hard Stop Loss: {HARD_STOP}%\n"
        f"Max Posisi: {MAX_TRADES} per exchange\n"
        f"Scan: Top {TOP_N_PAIRS} pair di kedua exchange\n"
        f"Min sinyal: {MIN_SIGNALS}/12\n"
        f"Uptrend: {UPTREND_PCT}%\n"
        f"Blacklist: {BLACKLIST_HR} jam\n"
        f"Mode: Non-stop seumur hidup! 🔄"
    )

    while True:
        try:
            fetch_prices()
            bot_tick()
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            send_balance_update()
            send_telegram(f"🔴 *IndoBot STOP*\nTotal Profit: {fmt(total_profit)}\nTotal Trade: {total_trades}")
            break
        except Exception as e:
            log(f"⚠️ Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
