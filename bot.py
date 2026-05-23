import os
import time
import hmac
import hashlib
import requests
import urllib.parse
import json
from datetime import datetime

# ── Konfigurasi ────────────────────────────────────────
API_KEY      = os.environ.get("INDODAX_API_KEY", "")
SECRET_KEY   = os.environ.get("INDODAX_SECRET_KEY", "")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_KEY   = os.environ.get("CLAUDE_API_KEY", "")
TAKE_PROFIT  = float(os.environ.get("TAKE_PROFIT", "2000"))
TRAIL_PCT    = float(os.environ.get("TRAIL_PCT", "1"))
HARD_STOP    = float(os.environ.get("HARD_STOP", "5.0"))
MAX_MODAL    = float(os.environ.get("MAX_MODAL", "2000000"))
MAX_TRADES   = int(os.environ.get("MAX_TRADES", "3"))
TOP_N_PAIRS  = int(os.environ.get("TOP_N_PAIRS", "50"))
MIN_SIGNALS  = int(os.environ.get("MIN_SIGNALS", "11"))
AI_MIN_SCORE = int(os.environ.get("AI_MIN_SCORE", "60"))
BLACKLIST_HR = int(os.environ.get("BLACKLIST_HR", "6"))
UPTREND_PCT  = float(os.environ.get("UPTREND_PCT", "50"))
SELL_RETRY   = int(os.environ.get("SELL_RETRY", "3"))
VOL_SPIKE    = float(os.environ.get("VOL_SPIKE", "3.0"))
SCAN_INTERVAL = 60

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
    text = f"🤖 *IndoBot v8*\n{msg}\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log(f"TG Error: {e}")

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
    "moodeng", "pnut", "pengu", "useless", "doge", "anoa",
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
    if time.time() - daily_start >= 86400:
        daily_loss  = 0.0
        daily_start = time.time()
        log("🔄 Reset harian")
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
            
