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
TAKE_PROFIT  = float(os.environ.get("TAKE_PROFIT", "1000"))
TRAIL_PCT    = float(os.environ.get("TRAIL_PCT", "1.5"))     # trailing stop 1.5%
HARD_STOP    = float(os.environ.get("HARD_STOP", "5.0"))     # hard stop loss 5%
MAX_MODAL    = float(os.environ.get("MAX_MODAL", "2000000"))
MAX_TRADES   = int(os.environ.get("MAX_TRADES", "6"))
TOP_N_PAIRS  = int(os.environ.get("TOP_N_PAIRS", "20"))
MIN_SIGNALS  = int(os.environ.get("MIN_SIGNALS", "10"))
BLACKLIST_HR = int(os.environ.get("BLACKLIST_HR", "48"))
UPTREND_PCT  = float(os.environ.get("UPTREND_PCT", "50"))
SELL_RETRY   = int(os.environ.get("SELL_RETRY", "3"))
VOL_SPIKE    = float(os.environ.get("VOL_SPIKE", "3.0"))     # volume spike 3x normal
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
    text = f"🤖 *IndoBot v6*\n{msg}\n⏰ {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
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

# ── Balance ────────────────────────────────────────────
def get_idr_balance():
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr = float(result["return"]["balance"].get("idr", 0))
        return min(idr, MAX_MODAL)
    return 0

def get_coin_balance(coin):
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        return float(result["return"]["balance"].get(coin, 0))
    return 0

# ── Coin name mapping ──────────────────────────────────
COIN_MAP = {
    "trollsol_idr":   "trollsol",
    "jellyjelly_idr": "jellyjelly",
    "whitewhale_idr": "whitewhale",
    "fartcoin_idr":   "fartcoin",
    "solayer_idr":    "solayer",
    "zerebro_idr":    "zerebro",
    "useless_idr":    "useless",
    "sundog_idr":     "sundog",
    "pippin_idr":     "pippin",
    "siren_idr":      "siren",
}

def get_coin_name(pair_id):
    if pair_id in COIN_MAP:
        return COIN_MAP[pair_id]
    return pair_id.replace("_idr", "")

# ── Test API ───────────────────────────────────────────
def test_api():
    global modal
    log("🔑 Test koneksi API Indodax...")
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        idr   = float(result["return"]["balance"].get("idr", 0))
        modal = min(idr, MAX_MODAL)
        log(f"✅ API OK! Saldo IDR: {fmt(idr)} | Modal bot: {fmt(modal)}")
        send_telegram(f"✅ *API Indodax Konek!*\nSaldo IDR: {fmt(idr)}\nModal bot: {fmt(modal)}")
        return True
    log(f"❌ API gagal: {result}")
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
        log(f"📡 Top {TOP_N_PAIRS} pair: {', '.join(names)}")
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

# ── Predictive signals — deteksi coin AKAN naik ───────
def check_momentum(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 5 or len(vol_h) < 5:
        return False, ""

    # 1. Volume Spike — volume naik 3x dari rata-rata
    avg_vol  = sum(vol_h[:-1]) / (len(vol_h) - 1) if len(vol_h) > 1 else 0
    curr_vol = vol_h[-1]
    vol_spike = avg_vol > 0 and curr_vol >= avg_vol * VOL_SPIKE

    # 2. Price momentum — harga naik 3 candle berturut
    price_up = len(hist) >= 3 and hist[-1] > hist[-2] > hist[-3]

    # 3. MACD hampir crossover (selisih kecil)
    if len(hist) >= 26:
        macd, signal, _ = calc_macd(hist)
        macd_cross = macd > signal * 0.95  # hampir atau sudah crossover
    else:
        macd_cross = False

    # 4. Bollinger squeeze — band menyempit (breakout akan terjadi)
    if len(hist) >= 20:
        upper, mid, lower = calc_bollinger(hist)
        band_width = (upper - lower) / mid * 100 if mid > 0 else 100
        bb_squeeze = band_width < 3.0  # band sangat sempit
    else:
        bb_squeeze = False

    # 5. RSI momentum — RSI naik dari oversold
    rsi = calc_rsi(hist)
    rsi_momentum = 30 <= rsi <= 50  # naik dari oversold, belum overbought

    signals = []
    if vol_spike:    signals.append(f"Vol Spike {curr_vol/avg_vol:.1f}x🚀")
    if price_up:     signals.append("Price Up 3✅")
    if macd_cross:   signals.append("MACD Cross✅")
    if bb_squeeze:   signals.append("BB Squeeze✅")
    if rsi_momentum: signals.append(f"RSI={rsi:.0f}↑✅")

    # Butuh minimal 2 sinyal momentum
    is_momentum = len(signals) >= 2
    return is_momentum, " | ".join(signals)

# ── Deteksi indikasi turun — untuk exit lebih cepat ───
def check_exit_signal(pair_id, buy_price):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 5:
        return False, ""

    exit_signals = []

    # RSI overbought
    rsi = calc_rsi(hist)
    if rsi > 70:
        exit_signals.append(f"RSI={rsi:.0f} overbought")

    # Volume turun (momentum habis)
    if len(vol_h) >= 3:
        if vol_h[-1] < vol_h[-2] < vol_h[-3]:
            exit_signals.append("Volume turun")

    # MACD berbalik turun
    if len(hist) >= 26:
        macd, signal, hist_val = calc_macd(hist)
        if hist_val < 0:
            exit_signals.append("MACD turun")

    # Harga turun 2 candle berturut setelah peak
    if len(hist) >= 3 and hist[-1] < hist[-2] < hist[-3]:
        exit_signals.append("Harga turun 2x")

    should_exit = len(exit_signals) >= 2
    return should_exit, " | ".join(exit_signals)

# ── 12 Indikator standar ───────────────────────────────
def check_signals(pair_id):
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 30:
        return 0, []
    price = prices.get(pair_id, 0)
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

def choose_best_pairs():
    candidates = []
    for pair_id in all_pairs:
        if pair_id in open_positions: continue
        if is_blacklisted(pair_id): continue

        # Cek sinyal standar
        count, reasons = check_signals(pair_id)

        # Cek momentum prediktif
        has_momentum, momentum_desc = check_momentum(pair_id)

        # Masuk kalau sinyal standar cukup ATAU ada momentum kuat
        if count >= MIN_SIGNALS:
            candidates.append((pair_id, count, reasons, "standard", ""))
        elif has_momentum and count >= 6:
            # Momentum entry — sinyal lebih sedikit tapi ada momentum
            candidates.append((pair_id, count, reasons, "momentum", momentum_desc))

    # Prioritaskan momentum entry
    candidates.sort(key=lambda x: (x[3] == "momentum", x[1]), reverse=True)
    return candidates

# ── Order ──────────────────────────────────────────────
def place_buy(pair_id, price, idr_amount):
    log(f"📤 BUY {pair_id} | IDR:{int(idr_amount)}")
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "buy",
        "price": str(int(price * 1.01)),
        "idr":   str(int(idr_amount)),
    })

def place_sell(pair_id, price):
    coin       = get_coin_name(pair_id)
    actual_qty = get_coin_balance(coin)
    if actual_qty <= 0:
        log(f"⚠️ Saldo {coin} = 0, hapus posisi")
        return {"success": 1, "zero_balance": True}
    log(f"📤 SELL {pair_id} | {coin}:{actual_qty:.8f}")
    return indodax_request("trade", {
        "pair":  pair_id,
        "type":  "sell",
        "price": str(int(price * 0.99)),
        coin:    f"{actual_qty:.8f}",
    })

# ── Bot Logic ──────────────────────────────────────────
def bot_tick():
    global modal, total_profit, total_trades

    reset_daily()
    modal = get_idr_balance()

    for pair_id in list(open_positions.keys()):
        pos       = open_positions[pair_id]
        buy_price = pos["buy_price"]
        qty       = pos["qty"]
        idr_in    = pos["idr"]
        label     = pair_labels.get(pair_id, pair_id)
        curr      = prices.get(pair_id, buy_price)
        pl        = (curr - buy_price) * qty
        pl_pct    = (curr - buy_price) / buy_price * 100

        # Update peak
        if curr > pos["peak_price"]:
            open_positions[pair_id]["peak_price"] = curr

        peak       = open_positions[pair_id]["peak_price"]
        trail_drop = (peak - curr) / peak * 100

        log(f"📊 {label} | Beli:{fmt(buy_price)} | Kini:{fmt(curr)} | Peak:{fmt(peak)} | P/L:{fmt(pl)} ({pl_pct:.2f}%)")

        should_sell = False
        sell_reason = ""

        # Cek indikasi turun untuk exit lebih cepat
        has_exit, exit_desc = check_exit_signal(pair_id, buy_price)

        # Take profit + trailing
        if pl >= TAKE_PROFIT and trail_drop >= TRAIL_PCT:
            should_sell = True
            sell_reason = f"💰 Profit {fmt(pl)} | Trailing {trail_drop:.1f}%"

        # Exit cepat kalau ada indikasi turun + sudah profit
        elif pl > 0 and has_exit:
            should_sell = True
            sell_reason = f"📉 Exit sinyal turun: {exit_desc} | P/L:{fmt(pl)}"

        # Hard stop loss
        elif pl_pct <= -HARD_STOP:
            should_sell = True
            sell_reason = f"🚨 Hard Stop Loss {pl_pct:.2f}%"

        # Trailing stop
        elif trail_drop >= TRAIL_PCT and pl_pct <= -1:
            should_sell = True
            sell_reason = f"🛑 Trailing Stop {trail_drop:.1f}%"

        if should_sell:
            last_sell = pos.get("last_sell_time", 0)
            attempts  = pos.get("sell_attempts", 0)

            if attempts >= SELL_RETRY:
                log(f"⚠️ {label} gagal jual {attempts}x — jual manual!")
                send_telegram(f"⚠️ *{label} gagal jual {attempts}x!*\nSilakan jual manual di Indodax!")
                del open_positions[pair_id]
                continue

            if time.time() - last_sell < 300 and attempts > 0:
                continue

            result = place_sell(pair_id, curr)
            if result and result.get("success") == 1:
                modal        = get_idr_balance()
                total_profit += pl
                total_trades += 1
                del open_positions[pair_id]
                if not result.get("zero_balance"):
                    log(f"{sell_reason} | {label} | P/L:{fmt(pl)}")
                    send_telegram(
                        f"{sell_reason}\n*{label}*\n"
                        f"Harga: {fmt(curr)}\n"
                        f"P/L: {fmt(pl)}\n"
                        f"Total Profit: {fmt(total_profit)}\n"
                        f"Modal: {fmt(modal)}"
                    )
                    if pl < 0:
                        add_blacklist(pair_id, pl)
            else:
                open_positions[pair_id]["sell_attempts"] = attempts + 1
                open_positions[pair_id]["last_sell_time"] = time.time()
                log(f"⚠️ Gagal jual {label} (attempt {attempts+1}/{SELL_RETRY})")

    # Buka posisi baru
    slots = MAX_TRADES - len(open_positions)
    if slots <= 0:
        log(f"📊 {len(open_positions)}/{MAX_TRADES} posisi penuh")
        return

    if not check_market_trend():
        log("⛔ Market DOWNTREND — skip beli")
        return

    candidates = choose_best_pairs()
    if not candidates:
        log(f"⏳ Scan {TOP_N_PAIRS} pair — menunggu sinyal... ({len(open_positions)}/{MAX_TRADES} posisi)")
        return

    idr_per_trade = (modal * 0.95) / MAX_TRADES

    for pair_id, count, reasons, entry_type, momentum_desc in candidates[:slots]:
        if idr_per_trade < 10000:
            log(f"⚠️ Modal per trade terlalu kecil: {fmt(idr_per_trade)}")
            break

        price  = prices.get(pair_id, 0)
        label  = pair_labels.get(pair_id, pair_id)

        if entry_type == "momentum":
            reason = f"🚀 MOMENTUM [{count}/12] {momentum_desc}"
        else:
            reason = f"[{count}/12] " + " | ".join(reasons)

        qty = idr_per_trade / price

        result = place_buy(pair_id, price, idr_per_trade)
        if result and result.get("success") == 1:
            modal = get_idr_balance()
            total_trades += 1
            open_positions[pair_id] = {
                "buy_price":     price,
                "qty":           qty,
                "idr":           idr_per_trade,
                "peak_price":    price,
                "sell_attempts": 0,
                "last_sell_time": 0,
                "entry_type":    entry_type
            }
            log(f"🛒 BELI {label} | {fmt(price)} | {qty:.6f} unit | {reason}")
            send_telegram(
                f"🛒 *BELI {label}*\n"
                f"Harga: {fmt(price)}\n"
                f"Unit: {qty:.6f}\n"
                f"Modal/trade: {fmt(idr_per_trade)}\n"
                f"Entry: {reason}\n"
                f"Posisi: {len(open_positions)}/{MAX_TRADES}"
            )
            time.sleep(2)
        else:
            log(f"⚠️ Gagal beli {label}: {result}")

# ── Main ───────────────────────────────────────────────
def main():
    log("🚀 IndoBot v6 dimulai...")

    while not test_api():
        log("⏳ Retry API..."); time.sleep(30)

    while not fetch_all_pairs():
        log("⏳ Retry load pair..."); time.sleep(30)

    send_telegram(
        f"🚀 *IndoBot v6 AKTIF*\n"
        f"Modal: Auto dari Indodax (max {fmt(MAX_MODAL)})\n"
        f"Take Profit: {fmt(TAKE_PROFIT)} per trade\n"
        f"Trailing Stop: {TRAIL_PCT}%\n"
        f"Hard Stop Loss: {HARD_STOP}%\n"
        f"Max Posisi: {MAX_TRADES} coin sekaligus\n"
        f"Scan: Top {TOP_N_PAIRS} pair volume tertinggi\n"
        f"Min sinyal: {MIN_SIGNALS}/12\n"
        f"Uptrend: {UPTREND_PCT}% coin naik\n"
        f"Blacklist: {BLACKLIST_HR} jam\n"
        f"🆕 Predictive Entry: Volume Spike {VOL_SPIKE}x\n"
        f"🆕 Exit otomatis saat indikasi turun\n"
        f"Mode: Non-stop seumur hidup! 🔄"
    )

    while True:
        try:
            fetch_prices()
            bot_tick()
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            send_telegram(f"🔴 *IndoBot STOP*\nTotal Profit: {fmt(total_profit)}\nTotal Trade: {total_trades}")
            break
        except Exception as e:
            log(f"⚠️ Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
