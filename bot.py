import os
import time
import hmac
import hashlib
import requests
import urllib.parse
from datetime import datetime

# ── Konfigurasi ────────────────────────────────────────
API_KEY    = os.environ.get("INDODAX_API_KEY", "")
SECRET_KEY = os.environ.get("INDODAX_SECRET_KEY", "")
TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MODAL      = float(os.environ.get("MODAL", "100000"))
TARGET     = float(os.environ.get("TARGET_PROFIT", "20000"))
STOP_LOSS  = float(os.environ.get("STOP_LOSS_PCT", "3"))

INDODAX_TAPI = "https://indodax.com/tapi"
SCAN_INTERVAL = 60

# ── State ──────────────────────────────────────────────
modal         = MODAL
total_profit  = 0.0
daily_profit  = 0.0
total_trades  = 0
open_position = None
price_history = {}
all_pairs     = []
pair_labels   = {}
prices        = {}

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
    text = f"🤖 *IndoBot*\n{msg}\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log(f"TG Error: {e}")

# ── Indodax Private API ────────────────────────────────
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

# ── Test koneksi API ───────────────────────────────────
def test_api():
    log("🔑 Test koneksi API Indodax...")
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr = result["return"]["balance"].get("idr", 0)
        log(f"✅ API OK! Saldo IDR: {fmt(float(idr))}")
        send_telegram(f"✅ *API Indodax Konek!*\nSaldo IDR: {fmt(float(idr))}")
        return True
    else:
        log(f"❌ API gagal: {result}")
        send_telegram(f"❌ *API Gagal!*\nResponse: {result}")
        return False

# ── Fetch semua pair ───────────────────────────────────
def fetch_all_pairs():
    global all_pairs, pair_labels, prices, price_history
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=10)
        d = r.json()
        tickers = d.get("tickers", {})
        all_pairs = []
        for key, val in tickers.items():
            if not key.endswith("_idr"):
                continue
            last = float(val.get("last", 0))
            if last <= 0:
                continue
            label = key.replace("_idr", "").upper() + "/IDR"
            all_pairs.append(key)
            pair_labels[key] = label
            prices[key] = last
            if key not in price_history:
                price_history[key] = []
            price_history[key].append(last)
        log(f"📡 Load {len(all_pairs)} pair dari Indodax")
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
            if last <= 0:
                continue
            prices[pair_id] = last
            price_history[pair_id].append(last)
            if len(price_history[pair_id]) > 50:
                price_history[pair_id].pop(0)
    except Exception as e:
        log(f"⚠️ Gagal refresh harga: {e}")

# ── Indikator ──────────────────────────────────────────
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

def calc_sma(arr, period):
    if len(arr) < period:
        return arr[-1] if arr else 0
    return sum(arr[-period:]) / period

def check_signals(pair_id):
    hist = price_history.get(pair_id, [])
    if len(hist) < 5:
        return 0, []
    rsi   = calc_rsi(hist)
    sma5  = calc_sma(hist, 5)
    sma20 = calc_sma(hist, 20)
    recent = hist[-10:] if len(hist) >= 10 else hist
    mx, mn = max(recent), min(recent)
    vol = (mx - mn) / mn * 100 if mn > 0 else 0
    count, reasons = 0, []
    if rsi < 40:     count += 1; reasons.append(f"RSI={rsi:.1f}")
    if sma5 > sma20: count += 1; reasons.append("SMA bullish")
    if vol > 0.5:    count += 1; reasons.append(f"vol={vol:.1f}%")
    return count, reasons

def choose_best_pair():
    best_pair, best_score = None, -1
    for pair_id in all_pairs:
        count, _ = check_signals(pair_id)
        if count < 2:
            continue
        price = prices.get(pair_id, 0)
        if modal * 0.9 < price * 0.0001:
            continue
        if count > best_score:
            best_score = count
            best_pair  = pair_id
    return best_pair

# ── Order REAL ke Indodax (Market Order) ──────────────
def place_buy(pair_id, price, idr_amount):
    log(f"📤 MARKET BUY {pair_id} | IDR:{int(idr_amount)}")
    # Market order = pasang harga sedikit lebih tinggi supaya langsung terisi
    market_price = int(price * 1.01)  # 1% di atas harga pasar
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "buy",
        "price": str(market_price),
        "idr":   str(int(idr_amount)),
    })

def place_sell(pair_id, price, qty):
    coin = pair_id.replace("_idr", "")
    log(f"📤 MARKET SELL {pair_id} | {coin}:{qty:.8f}")
    # Market order = pasang harga sedikit lebih rendah supaya langsung terisi
    market_price = int(price * 0.99)  # 1% di bawah harga pasar
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "sell",
        "price": str(market_price),
        coin:    f"{qty:.8f}",
    })

# ── Bot Logic ──────────────────────────────────────────
def bot_tick():
    global modal, total_profit, daily_profit, total_trades, open_position

    if daily_profit >= TARGET:
        log(f"🎯 Target tercapai! {fmt(daily_profit)}")
        send_telegram(f"🎯 *TARGET TERCAPAI!*\nProfit: {fmt(daily_profit)}\nTotal trade: {total_trades}\nModal: {fmt(modal)}")
        return False

    if open_position:
        pair_id   = open_position["pair"]
        buy_price = open_position["buy_price"]
        qty       = open_position["qty"]
        idr_in    = open_position["idr"]
        label     = pair_labels.get(pair_id, pair_id)
        curr      = prices.get(pair_id, buy_price)
        pl        = (curr - buy_price) * qty
        pl_pct    = (curr - buy_price) / buy_price * 100

        log(f"📊 {label} | Beli:{fmt(buy_price)} | Kini:{fmt(curr)} | P/L:{fmt(pl)}")

        if pl >= TARGET * 0.4:
            result = place_sell(pair_id, curr, qty)
            if result and result.get("success") == 1:
                modal += idr_in + pl
                total_profit += pl
                daily_profit += pl
                total_trades += 1
                open_position = None
                log(f"💰 JUAL {label} | P/L: {fmt(pl)}")
                send_telegram(f"💰 *JUAL {label}*\nHarga: {fmt(curr)}\nP/L: {fmt(pl)}\nProfit: {fmt(daily_profit)}\nModal: {fmt(modal)}")
            else:
                log(f"⚠️ Gagal jual: {result}")
                send_telegram(f"⚠️ Gagal JUAL {label}\n{result}")

        elif pl_pct <= -STOP_LOSS:
            result = place_sell(pair_id, curr, qty)
            if result and result.get("success") == 1:
                modal += idr_in + pl
                total_profit += pl
                daily_profit += pl
                total_trades += 1
                open_position = None
                log(f"🛑 STOP LOSS {label} | Loss: {fmt(pl)}")
                send_telegram(f"🛑 *STOP LOSS {label}*\nHarga: {fmt(curr)}\nLoss: {fmt(pl)}\nModal: {fmt(modal)}")
            else:
                log(f"⚠️ Gagal stop loss: {result}")
        return True

    pair_id = choose_best_pair()
    if not pair_id:
        log("⏳ Scan pasar — menunggu 2/3 sinyal...")
        return True

    count, reasons = check_signals(pair_id)
    price  = prices.get(pair_id, 0)
    label  = pair_labels.get(pair_id, pair_id)
    reason = f"[{count}/3] " + " + ".join(reasons)
    idr_in = modal * 0.85
    qty    = idr_in / price

    result = place_buy(pair_id, price, idr_in)
    if result and result.get("success") == 1:
        modal -= idr_in
        total_trades += 1
        open_position = {"pair": pair_id, "buy_price": price, "qty": qty, "idr": idr_in}
        log(f"🛒 BELI {label} | {fmt(price)} | {qty:.6f} unit | {reason}")
        send_telegram(f"🛒 *BELI {label}*\nHarga: {fmt(price)}\nUnit: {qty:.6f}\nModal: {fmt(idr_in)}\nAlasan: {reason}")
    else:
        log(f"⚠️ Gagal beli {label}: {result}")
        send_telegram(f"⚠️ Gagal BELI {label}\n{result}")

    return True

# ── Main ───────────────────────────────────────────────
def main():
    global modal, daily_profit
    log("🚀 IndoBot dimulai...")

    while not test_api():
        log("⏳ Retry test API dalam 30 detik...")
        time.sleep(30)

    while not fetch_all_pairs():
        log("⏳ Retry load pair dalam 30 detik...")
        time.sleep(30)

    send_telegram(f"🚀 *IndoBot AKTIF*\nModal: {fmt(MODAL)}\nTarget: {fmt(TARGET)}\nStop Loss: {STOP_LOSS}%\nScan: {len(all_pairs)} pair")
    log(f"✅ Bot aktif | {len(all_pairs)} pair")

    while True:
        try:
            fetch_prices()
            running = bot_tick()
            if not running:
                daily_profit = 0
                log("🔄 Reset — lanjut besok...")
                time.sleep(3600)
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            send_telegram(f"🔴 *IndoBot STOP*\nProfit: {fmt(total_profit)}\nTrade: {total_trades}")
            break
        except Exception as e:
            log(f"⚠️ Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
