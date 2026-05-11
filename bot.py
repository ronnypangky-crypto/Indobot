import os
import time
import hmac
import hashlib
import requests
import urllib.parse
from datetime import datetime

# ── Konfigurasi ────────────────────────────────────────
API_KEY      = os.environ.get("INDODAX_API_KEY", "")
SECRET_KEY   = os.environ.get("INDODAX_SECRET_KEY", "")
TG_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TAKE_PROFIT  = float(os.environ.get("TAKE_PROFIT", "2000"))
TRAIL_PCT    = float(os.environ.get("TRAIL_PCT", "1.0"))     # trailing stop 1%
MAX_MODAL    = float(os.environ.get("MAX_MODAL", "2000000")) # max Rp 2jt
MAX_TRADES   = int(os.environ.get("MAX_TRADES", "6"))        # 6 posisi sekaligus
TOP_N_PAIRS  = int(os.environ.get("TOP_N_PAIRS", "20"))      # top 20 pair
MIN_SIGNALS  = int(os.environ.get("MIN_SIGNALS", "10"))      # min 10/12 sinyal
MIN_PRICE    = float(os.environ.get("MIN_PRICE", "0"))      # tidak ada filter harga
SCAN_INTERVAL = 60

INDODAX_TAPI = "https://indodax.com/tapi"

# ── State ──────────────────────────────────────────────
modal          = 0.0
total_profit   = 0.0
total_trades   = 0
open_positions = {}   # {pair_id: {buy_price, qty, idr, peak_price}}
price_history  = {}
volume_history = {}
all_pairs      = []
pair_labels    = {}
prices         = {}

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
    text = f"🤖 *IndoBot v5*\n{msg}\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
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

# ── Auto baca saldo IDR dari Indodax ───────────────────
def get_idr_balance():
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr = float(result["return"]["balance"].get("idr", 0))
        return min(idr, MAX_MODAL)  # max Rp 2jt
    return 0

def get_coin_balance(coin):
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        return float(result["return"]["balance"].get(coin, 0))
    return 0

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
            f"Max modal: {fmt(MAX_MODAL)}"
        )
        return True
    else:
        log(f"❌ API gagal: {result}")
        send_telegram(f"❌ *API Gagal!*\nResponse: {result}")
        return False

# ── Fetch TOP 20 pair volume tertinggi ─────────────────
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
            if last < MIN_PRICE:  # filter coin murah
                continue
            pair_list.append((key, last, vol))
        pair_list.sort(key=lambda x: x[2], reverse=True)
        top_pairs = pair_list[:TOP_N_PAIRS]
        all_pairs = []
        for key, last, vol in top_pairs:
            label = key.replace("_idr", "").upper() + "/IDR"
            all_pairs.append(key)
            pair_labels[key]    = label
            prices[key]         = last
            volume_history[key] = [vol]
            if key not in price_history:
                price_history[key] = []
            price_history[key].append(last)
        names = [pair_labels[p] for p in all_pairs]
        log(f"📡 Top {TOP_N_PAIRS} pair (min Rp {MIN_PRICE}): {', '.join(names)}")
        send_telegram(f"📡 *Top {TOP_N_PAIRS} Pair:*\n{', '.join(names)}")
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

# ── 12 Indikator ───────────────────────────────────────
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
    ema12     = calc_ema(arr, 12)
    ema26     = calc_ema(arr, 26)
    macd      = ema12 - ema26
    signal    = macd * 0.9
    histogram = macd - signal
    return macd, signal, histogram

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
    rsi_vals = []
    for i in range(period, len(arr)):
        rsi_vals.append(calc_rsi(arr[:i+1], period))
    if not rsi_vals:
        return 50
    min_rsi = min(rsi_vals[-period:])
    max_rsi = max(rsi_vals[-period:])
    curr    = rsi_vals[-1]
    if max_rsi == min_rsi:
        return 50
    return (curr - min_rsi) / (max_rsi - min_rsi) * 100

def calc_cci(arr, period=20):
    if len(arr) < period:
        return 0
    recent = arr[-period:]
    mean   = sum(recent) / period
    mad    = sum(abs(x - mean) for x in recent) / period
    if mad == 0:
        return 0
    return (arr[-1] - mean) / (0.015 * mad)

def calc_williams_r(arr, period=14):
    if len(arr) < period:
        return -50
    recent   = arr[-period:]
    high     = max(recent)
    low      = min(recent)
    curr     = arr[-1]
    if high == low:
        return -50
    return (high - curr) / (high - low) * -100

def calc_roc(arr, period=10):
    if len(arr) < period + 1:
        return 0
    return (arr[-1] - arr[-period-1]) / arr[-period-1] * 100

def calc_atr(arr, period=14):
    if len(arr) < period + 1:
        return 0
    trs = [abs(arr[i] - arr[i-1]) for i in range(1, len(arr))]
    return sum(trs[-period:]) / period

def calc_obv(arr, vol_arr):
    if len(arr) < 2 or len(vol_arr) < 2:
        return 0
    obv = 0
    for i in range(1, min(len(arr), len(vol_arr))):
        if arr[i] > arr[i-1]:
            obv += vol_arr[i]
        elif arr[i] < arr[i-1]:
            obv -= vol_arr[i]
    return obv

def check_signals(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 30:
        return 0, []

    price = prices.get(pair_id, 0)
    count, reasons = 0, []

    # 1. RSI < 40 (oversold)
    rsi = calc_rsi(hist)
    if rsi < 40:
        count += 1; reasons.append(f"RSI={rsi:.0f}✅")

    # 2. SMA bullish
    sma5 = calc_sma(hist, 5); sma20 = calc_sma(hist, 20)
    if sma5 > sma20:
        count += 1; reasons.append("SMA↑✅")

    # 3. MACD bullish
    macd, signal, hist_val = calc_macd(hist)
    if macd > signal:
        count += 1; reasons.append("MACD↑✅")

    # 4. Bollinger Bands oversold
    upper, mid, lower = calc_bollinger(hist)
    if price <= lower * 1.01:
        count += 1; reasons.append("BB✅")

    # 5. EMA bullish
    ema9 = calc_ema(hist, 9); ema21 = calc_ema(hist, 21)
    if ema9 > ema21:
        count += 1; reasons.append("EMA↑✅")

    # 6. Volume tinggi
    if len(vol_h) >= 5 and vol_h[-1] > (sum(vol_h[:-1])/(len(vol_h)-1)) * 1.2:
        count += 1; reasons.append("Vol↑✅")

    # 7. Stochastic RSI < 20 (oversold)
    stoch = calc_stoch_rsi(hist)
    if stoch < 20:
        count += 1; reasons.append(f"StochRSI✅")

    # 8. CCI < -100 (oversold)
    cci = calc_cci(hist)
    if cci < -100:
        count += 1; reasons.append("CCI✅")

    # 9. Williams %R < -80 (oversold)
    wr = calc_williams_r(hist)
    if wr < -80:
        count += 1; reasons.append("WR✅")

    # 10. ROC positif (momentum naik)
    roc = calc_roc(hist)
    if roc > 0:
        count += 1; reasons.append("ROC↑✅")

    # 11. ATR — volatilitas cukup untuk profit
    atr = calc_atr(hist)
    if atr > price * 0.005:  # ATR > 0.5% harga
        count += 1; reasons.append("ATR✅")

    # 12. OBV positif (buying pressure)
    obv = calc_obv(hist, vol_h)
    if obv > 0:
        count += 1; reasons.append("OBV↑✅")

    return count, reasons

# ── Pilih pair terbaik ─────────────────────────────────
def choose_best_pairs():
    candidates = []
    for pair_id in all_pairs:
        if pair_id in open_positions:
            continue
        price = prices.get(pair_id, 0)
        if price < MIN_PRICE:
            continue
        count, reasons = check_signals(pair_id)
        if count < MIN_SIGNALS:
            continue
        candidates.append((pair_id, count, reasons))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates

# ── Order ──────────────────────────────────────────────
def place_buy(pair_id, price, idr_amount):
    log(f"📤 BUY {pair_id} | IDR:{int(idr_amount)}")
    market_price = int(price * 1.01)
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "buy",
        "price": str(market_price),
        "idr":   str(int(idr_amount)),
    })

def place_sell(pair_id, price):
    coin       = pair_id.replace("_idr", "")
    actual_qty = get_coin_balance(coin)
    if actual_qty <= 0:
        log(f"⚠️ Saldo {coin} kosong!")
        return {"success": 0, "error": "Zero balance"}
    log(f"📤 SELL {pair_id} | {coin}:{actual_qty:.8f}")
    market_price = int(price * 0.99)
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "sell",
        "price": str(market_price),
        coin:    f"{actual_qty:.8f}",
    })

# ── Bot Logic ──────────────────────────────────────────
def bot_tick():
    global modal, total_profit, total_trades

    # Auto update modal dari Indodax setiap tick
    modal = get_idr_balance()

    # Cek semua posisi terbuka
    for pair_id in list(open_positions.keys()):
        pos       = open_positions[pair_id]
        buy_price = pos["buy_price"]
        qty       = pos["qty"]
        idr_in    = pos["idr"]
        label     = pair_labels.get(pair_id, pair_id)
        curr      = prices.get(pair_id, buy_price)
        pl        = (curr - buy_price) * qty
        pl_pct    = (curr - buy_price) / buy_price * 100

        # Update peak price untuk trailing stop
        if curr > pos["peak_price"]:
            open_positions[pair_id]["peak_price"] = curr

        peak      = open_positions[pair_id]["peak_price"]
        trail_drop = (peak - curr) / peak * 100

        log(f"📊 {label} | Beli:{fmt(buy_price)} | Kini:{fmt(curr)} | Peak:{fmt(peak)} | P/L:{fmt(pl)} ({pl_pct:.2f}%)")

        # Take profit Rp 2.000
        if pl >= TAKE_PROFIT:
            # Cek trailing stop — kalau sudah profit tapi turun dari peak
            if trail_drop >= TRAIL_PCT:
                result = place_sell(pair_id, curr)
                if result and result.get("success") == 1:
                    modal        = get_idr_balance()
                    total_profit += pl
                    total_trades += 1
                    del open_positions[pair_id]
                    log(f"💰 TRAILING SELL {label} | P/L:{fmt(pl)} | Drop dari peak:{trail_drop:.2f}%")
                    send_telegram(
                        f"💰 *JUAL {label} (Trailing)*\n"
                        f"Harga: {fmt(curr)}\n"
                        f"Peak: {fmt(peak)}\n"
                        f"P/L: {fmt(pl)}\n"
                        f"Total Profit: {fmt(total_profit)}\n"
                        f"Modal: {fmt(modal)}"
                    )
            else:
                log(f"⏳ {label} profit {fmt(pl)} — trailing aktif, tunggu peak...")

        # Trailing stop loss — kalau belum profit tapi turun dari peak
        elif trail_drop >= TRAIL_PCT and pl_pct <= -1:
            result = place_sell(pair_id, curr)
            if result and result.get("success") == 1:
                modal        = get_idr_balance()
                total_profit += pl
                total_trades += 1
                del open_positions[pair_id]
                log(f"🛑 TRAILING STOP {label} | Loss:{fmt(pl)}")
                send_telegram(
                    f"🛑 *STOP LOSS {label} (Trailing)*\n"
                    f"Harga: {fmt(curr)}\n"
                    f"Loss: {fmt(pl)}\n"
                    f"Modal: {fmt(modal)}"
                )

    # Buka posisi baru kalau slot masih ada
    slots = MAX_TRADES - len(open_positions)
    if slots <= 0:
        log(f"📊 {len(open_positions)}/{MAX_TRADES} posisi penuh")
        return

    candidates = choose_best_pairs()
    if not candidates:
        log(f"⏳ Scan {TOP_N_PAIRS} pair — menunggu {MIN_SIGNALS}/12 sinyal... ({len(open_positions)}/{MAX_TRADES} posisi)")
        return

    idr_per_trade = (modal * 0.95) / MAX_TRADES

    for pair_id, count, reasons in candidates[:slots]:
        if idr_per_trade < 10000:
            log(f"⚠️ Modal per trade terlalu kecil: {fmt(idr_per_trade)}")
            break

        price  = prices.get(pair_id, 0)
        label  = pair_labels.get(pair_id, pair_id)
        reason = f"[{count}/12] " + " | ".join(reasons)
        qty    = idr_per_trade / price

        result = place_buy(pair_id, price, idr_per_trade)
        if result and result.get("success") == 1:
            modal = get_idr_balance()
            total_trades += 1
            open_positions[pair_id] = {
                "buy_price":  price,
                "qty":        qty,
                "idr":        idr_per_trade,
                "peak_price": price  # trailing stop mulai dari harga beli
            }
            log(f"🛒 BELI {label} | {fmt(price)} | {qty:.6f} unit | {reason}")
            send_telegram(
                f"🛒 *BELI {label}*\n"
                f"Harga: {fmt(price)}\n"
                f"Unit: {qty:.6f}\n"
                f"Modal/trade: {fmt(idr_per_trade)}\n"
                f"Sinyal: {reason}\n"
                f"Posisi: {len(open_positions)}/{MAX_TRADES}"
            )
            time.sleep(2)
        else:
            log(f"⚠️ Gagal beli {label}: {result}")
            send_telegram(f"⚠️ Gagal BELI {label}\n{result}")

# ── Main ───────────────────────────────────────────────
def main():
    log("🚀 IndoBot v5 dimulai...")

    while not test_api():
        log("⏳ Retry test API dalam 30 detik...")
        time.sleep(30)

    while not fetch_all_pairs():
        log("⏳ Retry load pair dalam 30 detik...")
        time.sleep(30)

    send_telegram(
        f"🚀 *IndoBot v5 AKTIF*\n"
        f"Modal: Auto dari Indodax (max {fmt(MAX_MODAL)})\n"
        f"Take Profit: {fmt(TAKE_PROFIT)} per trade\n"
        f"Trailing Stop: {TRAIL_PCT}%\n"
        f"Max Posisi: {MAX_TRADES} coin sekaligus\n"
        f"Scan: Top {TOP_N_PAIRS} pair volume tertinggi\n"
        f"Min sinyal: {MIN_SIGNALS}/12\n"
        f"Filter harga: min Rp {MIN_PRICE:,.0f}\n"
        f"Mode: Non-stop seumur hidup! 🔄\n"
        f"Indikator: RSI+SMA+MACD+BB+EMA+Vol+StochRSI+CCI+WR+ROC+ATR+OBV"
    )
    log(f"✅ Bot v5 aktif | {len(all_pairs)} pair | {MAX_TRADES} posisi | {MIN_SIGNALS}/12 sinyal")

    while True:
        try:
            fetch_prices()
            bot_tick()
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            send_telegram(
                f"🔴 *IndoBot STOP*\n"
                f"Total Profit: {fmt(total_profit)}\n"
                f"Total Trade: {total_trades}"
            )
            break
        except Exception as e:
            log(f"⚠️ Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
