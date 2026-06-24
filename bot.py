import os, time, hmac, hashlib, requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

# ── Config ─────────────────────────────────────────────
TG_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY       = os.environ.get("INDODAX_API_KEY", "")
API_SECRET    = os.environ.get("INDODAX_SECRET_KEY", "")

TOP_N_PAIRS   = int(os.environ.get("TOP_N_PAIRS", "300"))
MIN_SIGNALS   = int(os.environ.get("MIN_SIGNALS", "4"))   # dari 4 sinyal utama
MAX_TRADES    = int(os.environ.get("MAX_TRADES", "3"))
TP_PCT        = float(os.environ.get("TP_PCT", "7"))       # Take Profit 7%
TRAIL_PCT     = float(os.environ.get("TRAIL_PCT", "3"))    # Trailing Stop 3%
TIME_STOP_HR  = int(os.environ.get("TIME_STOP_HR", "96")) # Time Stop 96 jam
BLACKLIST_HR  = int(os.environ.get("BLACKLIST_HR", "24")) # Blacklist 24 jam
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "10"))

WIB = timezone(timedelta(hours=7))

# ── State ───────────────────────────────────────────────
prices         = {}
price_history  = {}
volume_history = {}
open_positions = {}
blacklist      = {}
pending_confirm= {}  # coin menunggu konfirmasi kamu
last_update_id = 0
modal          = 0.0
total_profit   = 0.0
total_trades   = 0
daily_profit   = 0.0
daily_trades   = 0
last_summary   = ""
all_pairs      = []
pair_labels    = {}

INTEGER_COINS = {
    "sats", "shib", "floki", "pepe", "bonk", "lunc",
    "trollsol", "jellyjelly", "fartcoin", "neiro"
}

SKIP_PAIRS = {
    "usdt_idr", "usdc_idr", "busd_idr",
    "doge_idr", "rvn_idr", "shib_idr"
}

# ── Helpers ─────────────────────────────────────────────
def now_str():
    return datetime.now(WIB).strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)

def fmt(val):
    if abs(val) >= 1_000_000:
        return f"Rp {val/1_000_000:.2f}jt"
    elif abs(val) >= 1000:
        return f"Rp {val/1000:.1f}rb"
    return f"Rp {val:.0f}"

def send_telegram(msg, parse_mode="Markdown"):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    text = f"🤖 *IndoBot v9*\n{msg}\n⏰ {datetime.now(WIB).strftime('%d/%m/%Y %H:%M:%S')}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10
        )
    except Exception as e:
        log(f"TG Error: {e}")

# ── Indodax API ─────────────────────────────────────────
def indodax_request(method, params={}):
    try:
        params["method"]     = method
        params["timestamp"]  = str(int(time.time() * 1000))
        params["recvWindow"] = "5000"
        body    = urlencode(params)
        sig     = hmac.new(API_SECRET.encode(), body.encode(), hashlib.sha512).hexdigest()
        headers = {"Key": API_KEY, "Sign": sig}
        r = requests.post("https://indodax.com/tapi", data=params, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        log(f"API Error: {e}")
        return None

    if result and result.get("success") == 1:
        balances = result.get("return", {}).get("balance", {})
        for key in [coin, coin.lower(), coin.upper()]:
            val = float(balances.get(key, 0))
            if val > 0:
                return val, key
    return 0, coin

def get_coin_balance(coin):
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        balances = result.get("return", {}).get("balance", {})
        for key in [coin, coin.lower(), coin.upper()]:
            val = float(str(balances.get(key, "0")).replace(",", ""))
            if val > 0:
                return val, key
    return 0, coin

def format_qty(coin_key, qty):
    if coin_key.lower() in INTEGER_COINS:
        return str(int(qty))
    return f"{qty:.8f}"

# ── Indikator Teknikal ──────────────────────────────────
def calc_ema(arr, period):
    if len(arr) < period:
        return arr[-1] if arr else 0
    k = 2 / (period + 1)
    ema = arr[0]
    for p in arr[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(arr, period=14):
    if len(arr) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(arr)):
        d = arr[i] - arr[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    rs = ag / al
    return 100 - (100 / (1 + rs))

def calc_macd(arr):
    if len(arr) < 26:
        return 0, 0, 0
    ema12  = calc_ema(arr, 12)
    ema26  = calc_ema(arr, 26)
    macd   = ema12 - ema26
    signal = calc_ema([macd], 9)
    return macd, signal, macd - signal

def calc_bollinger(arr, period=20):
    if len(arr) < period:
        return arr[-1], arr[-1], arr[-1]
    sl   = arr[-period:]
    mid  = sum(sl) / period
    std  = (sum((x - mid) ** 2 for x in sl) / period) ** 0.5
    return mid + 2*std, mid, mid - 2*std

# ── 5 Sinyal Utama ──────────────────────────────────────

def calc_alligator(arr):
    """Williams Alligator — 3 smoothed moving averages"""
    if len(arr) < 13:
        return False, "Data kurang"
    jaw   = calc_ema(arr, 13)  # Jaw (biru) — period 13
    teeth = calc_ema(arr, 8)   # Teeth (merah) — period 8
    lips  = calc_ema(arr, 5)   # Lips (hijau) — period 5
    # Bullish: lips > teeth > jaw (mulut terbuka ke atas)
    bullish = lips > teeth > jaw
    # Bearish: lips < teeth < jaw (mulut terbuka ke bawah)
    bearish = lips < teeth < jaw
    if bullish:
        return True, f"Alligator Bullish🐊✅"
    elif bearish:
        return False, f"Alligator Bearish🐊"
    return False, f"Alligator Sideways"

def check_signals(pair_id):
    """
    Swing trading entry logic:
    1. RSI < 40 (oversold — koreksi sehat)
    2. Harga dekat LOW 24j (< 35% dari range)
    3. Alligator tidak bearish (masih ada harapan naik)
    4. Spread < 2% (likuid)
    """
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 14:
        return 0, [], {}

    sinyal  = 0
    reasons = []
    details = {}

    curr = hist[-1]

    # 1. RSI < 40 — oversold, kemungkinan balik naik
    rsi = calc_rsi(hist)
    details["rsi"] = rsi
    if rsi < 40:
        sinyal += 1
        reasons.append(f"RSI={rsi:.0f}✅ (oversold)")
    else:
        return 0, [], details  # wajib oversold

    # 2. Harga dekat Low 24j — beli murah
    try:
        r = requests.get(f"https://indodax.com/api/ticker/{pair_id}", timeout=5)
        ticker = r.json().get("ticker", {})
        high = float(ticker.get("high", curr))
        low  = float(ticker.get("low", curr))
        pos_hl = (curr - low) / (high - low) * 100 if high != low else 50
        details["pos_hl"] = pos_hl
        details["high"]   = high
        details["low"]    = low
        if pos_hl < 35:
            sinyal += 1
            reasons.append(f"Dekat LOW {pos_hl:.0f}%✅")
        else:
            return 0, [], details  # wajib dekat low
    except:
        return 0, [], details

    # 3. Alligator tidak bearish
    alligator_bull, alligator_desc = calc_alligator(hist)
    details["alligator"] = alligator_bull
    if alligator_bull:
        sinyal += 1
        reasons.append(alligator_desc)
    else:
        reasons.append("Alligator Sideways")

    # 4. Volume tidak spike besar (belum pump)
    if len(vol_h) >= 5:
        avg_vol = sum(vol_h[:-1]) / (len(vol_h) - 1)
        if avg_vol > 0:
            vol_ratio = vol_h[-1] / avg_vol
            if vol_ratio < 3:  # volume tidak sedang pump
                sinyal += 1
                reasons.append(f"Volume normal {vol_ratio:.1f}x✅")

    return sinyal, reasons, details

def check_exit_signal(pair_id):
    hist = price_history.get(pair_id, [])
    if len(hist) < 26:
        return False, ""

    exits = []
    ema9  = calc_ema(hist, 9)
    ema21 = calc_ema(hist, 21)
    ema9_prev  = calc_ema(hist[:-1], 9)
    ema21_prev = calc_ema(hist[:-1], 21)

    # EMA Death Cross
    if ema9_prev >= ema21_prev and ema9 < ema21:
        exits.append("EMA Death Cross⚠️")

    # Alligator Bearish
    alligator_bull, alligator_desc = calc_alligator(hist)
    if not alligator_bull:
        exits.append(alligator_desc)

    # RSI < 50
    rsi = calc_rsi(hist)
    if rsi < 50:
        exits.append(f"RSI={rsi:.0f}<50")

    # MACD turun
    macd, signal, _ = calc_macd(hist)
    if macd < signal:
        exits.append("MACD turun")

    return len(exits) >= 2, " | ".join(exits)

# ── Fetch Prices ────────────────────────────────────────
def fetch_all_pairs():
    global all_pairs, pair_labels
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=15)
        data = r.json().get("tickers", {})
        pairs = []
        for pair_id, info in data.items():
            if not pair_id.endswith("_idr"):
                continue
            if pair_id in SKIP_PAIRS:
                continue
            vol = float(info.get("vol_idr", 0))
            pairs.append((pair_id, vol))
            coin = pair_id.replace("_idr", "").upper()
            pair_labels[pair_id] = f"{coin}/IDR"

        pairs.sort(key=lambda x: x[1], reverse=True)
        all_pairs = [p[0] for p in pairs[:TOP_N_PAIRS]]
        log(f"📋 {len(all_pairs)} pair dimuat")
    except Exception as e:
        log(f"⚠️ Gagal fetch pairs: {e}")

def fetch_prices():
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=15)
        data = r.json().get("tickers", {})
        for pair_id in all_pairs:
            info = data.get(pair_id, {})
            curr = float(info.get("last", 0))
            vol  = float(info.get("vol_idr", 0))
            if curr > 0:
                prices[pair_id] = curr
                if pair_id not in price_history:
                    price_history[pair_id]  = []
                    volume_history[pair_id] = []
                price_history[pair_id].append(curr)
                volume_history[pair_id].append(vol)
                if len(price_history[pair_id]) > 200:
                    price_history[pair_id]  = price_history[pair_id][-200:]
                    volume_history[pair_id] = volume_history[pair_id][-200:]
    except Exception as e:
        log(f"⚠️ Gagal fetch prices: {e}")

# ── Balance ─────────────────────────────────────────────
def get_idr_balance():
    global modal
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        balances = result.get("return", {}).get("balance", {})
        idr_raw = str(balances.get("idr", "0")).replace(",", "")
        modal = float(idr_raw) if idr_raw else 0
        log(f"💵 Balance IDR: {modal}")
    return modal

# ── Buy/Sell ────────────────────────────────────────────
def place_buy(pair_id, price, idr_amount):
    market_price = int(price * 1.03)
    log(f"📤 BUY {pair_id} | IDR:{int(idr_amount)} | Harga:{market_price}")
    result = indodax_request("trade", {
        "pair":  pair_id,
        "type":  "buy",
        "price": str(market_price),
        "idr":   str(int(idr_amount)),
    })
    if result and result.get("success") == 1:
        log(f"✅ BUY berhasil")
    elif result:
        log(f"⚠️ BUY gagal: {result.get('error', '')}")
    return result

def place_sell(pair_id, price, qty_to_sell):
    coin = pair_id.replace("_idr", "")
    actual_qty, coin_key = get_coin_balance(coin)
    if actual_qty <= 0:
        return {"success": 1, "zero_balance": True}
    sell_qty     = min(qty_to_sell, actual_qty)
    qty_str      = format_qty(coin_key, sell_qty)
    market_price = int(price * 0.97)
    log(f"📤 SELL {pair_id} | {coin_key}:{qty_str} | Harga:{market_price}")
    result = indodax_request("trade", {
        "pair":   pair_id,
        "type":   "sell",
        "price":  str(market_price),
        coin_key: qty_str,
    })
    if result and "decimal" in str(result.get("error", "")):
        qty_str = str(int(sell_qty))
        INTEGER_COINS.add(coin_key.lower())
        result = indodax_request("trade", {
            "pair":   pair_id,
            "type":   "sell",
            "price":  str(market_price),
            coin_key: qty_str,
        })
    # Verifikasi terisi
    if result and result.get("success") == 1:
        time.sleep(2)
        new_qty, _ = get_coin_balance(coin)
        if new_qty >= actual_qty * 0.95:
            log(f"⚠️ SELL {pair_id} belum terisi → cancel")
            order_id = result.get("return", {}).get("order_id")
            if order_id:
                indodax_request("cancelOrder", {
                    "pair": pair_id, "order_id": str(order_id), "type": "sell"
                })
            return {"success": 0, "error": "Order tidak terisi"}
    return result

# ── Blacklist ────────────────────────────────────────────

POSITIONS_FILE = "/tmp/positions.json"

def save_positions():
    """Simpan posisi ke file supaya tidak hilang saat restart"""
    try:
        import json
        with open(POSITIONS_FILE, "w") as f:
            json.dump(open_positions, f)
        log("💾 Posisi tersimpan")
    except Exception as e:
        log(f"⚠️ Gagal simpan posisi: {e}")

def load_positions():
    """Load posisi dari file saat startup"""
    global open_positions
    try:
        import json, os
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, "r") as f:
                open_positions = json.load(f)
            if open_positions:
                log(f"📂 Restored {len(open_positions)} posisi dari file")
                pos_list = ""
                for pid, pos in open_positions.items():
                    label = pair_labels.get(pid, pid)
                    hours = (time.time() - pos.get("entry_time", time.time())) / 3600
                    pos_list += f"  • {label} — hold {hours:.1f}j\n"
                send_telegram(
                    f"📂 *Posisi Restored:*\n{pos_list}"
                    f"Total: {len(open_positions)} posisi"
                )
    except Exception as e:
        log(f"⚠️ Gagal load posisi: {e}")

def restore_from_wallet():
    """Restore posisi dari wallet Indodax saat startup"""
    global open_positions
    try:
        result = indodax_request("getInfo")
        if not result or result.get("success") != 1:
            return
        balances = result.get("return", {}).get("balance", {})
        restored = 0
        for coin, qty in balances.items():
            qty = float(qty)
            if qty <= 0:
                continue
            if coin in ["idr", "usdt", "usdc"]:
                continue
            pair_id = f"{coin}_idr"
            if pair_id in SKIP_PAIRS:
                continue
            if pair_id in open_positions:
                continue
            # Ambil harga sekarang
            curr = prices.get(pair_id, 0)
            if curr <= 0:
                try:
                    r = requests.get(f"https://indodax.com/api/ticker/{pair_id}", timeout=10)
                    curr = float(r.json().get("ticker", {}).get("last", 0))
                except:
                    continue
            if curr <= 0:
                continue
            idr_val = curr * qty
            if idr_val < 5000:  # skip dust
                continue
            open_positions[pair_id] = {
                "buy_price":  curr,  # estimasi harga beli = harga sekarang
                "qty":        qty,
                "idr":        idr_val,
                "peak":       curr,
                "entry_time": time.time(),
            }
            label = pair_labels.get(pair_id, f"{coin.upper()}/IDR")
            log(f"📂 Restore: {label} | {qty:.4f} | ~{fmt(idr_val)}")
            restored += 1

        if restored > 0:
            save_positions()
            pos_list = ""
            for pid, pos in open_positions.items():
                label = pair_labels.get(pid, pid)
                pos_list += f"  • {label}: ~{fmt(pos['idr'])}\n"
            send_telegram(
                f"📂 *Wallet Restored!*\n{pos_list}"
                f"Total: {restored} posisi\n"
                f"⚠️ Harga beli = harga sekarang (estimasi)"
            )
    except Exception as e:
        log(f"⚠️ Gagal restore wallet: {e}")
    blacklist[pair_id] = time.time()
    label = pair_labels.get(pair_id, pair_id)
    log(f"🚫 Blacklist {label} {BLACKLIST_HR} jam")
    send_telegram(f"🚫 *Blacklist {label}*\nSkip {BLACKLIST_HR} jam")

def is_blacklisted(pair_id):
    if pair_id in blacklist:
        if time.time() - blacklist[pair_id] < BLACKLIST_HR * 3600:
            return True
        del blacklist[pair_id]
    return False

# ── Scan & Analisa ──────────────────────────────────────
def cleanup_pending():
    """Hapus pending confirm yang sudah lebih dari 10 menit tidak dijawab"""
    expired = [p for p, d in pending_confirm.items() 
               if time.time() - d.get("time", 0) > 600]
    for p in expired:
        label = pair_labels.get(p, p)
        log(f"⏰ Pending {label} expired — tidak ada konfirmasi 10 menit")
        send_telegram(f"⏰ *Kandidat {label} expired* — tidak ada konfirmasi 10 menit, dilewati.")
        del pending_confirm[p]

def scan_candidates():
    """Scan pair dan kirim kandidat terbaik ke Telegram untuk konfirmasi"""
    global modal

    # Hitung slot aktif = posisi + pending konfirmasi
    active_slots = len(open_positions) + len(pending_confirm)
    if active_slots >= MAX_TRADES:
        log(f"⏳ Slot penuh {active_slots}/{MAX_TRADES} (posisi:{len(open_positions)} + pending:{len(pending_confirm)})")
        return

    get_idr_balance()
    idr_per_trade = modal / MAX_TRADES
    if idr_per_trade < 3000:
        log(f"⚠️ Modal per trade terlalu kecil: {fmt(idr_per_trade)}")
        return

    best_pair  = None
    best_score = 0
    best_data  = {}

    for pair_id in all_pairs:
        if pair_id in open_positions:
            continue
        if is_blacklisted(pair_id):
            continue
        curr = prices.get(pair_id, 0)
        if curr <= 0:
            continue

        sinyal, reasons, details = check_signals(pair_id)
        if sinyal < MIN_SIGNALS:
            continue

        # Pilih yang sinyalnya paling banyak
        if sinyal > best_score:
            best_score = sinyal
            best_pair  = pair_id
            best_data  = {"sinyal": sinyal, "reasons": reasons, "details": details, "price": curr}

    if not best_pair:
        log(f"⏳ Scan {len(all_pairs)} pair — tidak ada kandidat ({len(open_positions)}/{MAX_TRADES} posisi)")
        return

    # Kirim analisa ke Telegram untuk konfirmasi
    pair_id = best_pair
    label   = pair_labels.get(pair_id, pair_id)
    curr    = best_data["price"]
    reasons = best_data["reasons"]
    details = best_data["details"]
    sinyal  = best_data["sinyal"]
    rsi     = details.get("rsi", 0)
    ema9    = details.get("ema9", 0)
    ema21   = details.get("ema21", 0)

    # Analisa ticker
    try:
        r = requests.get(f"https://indodax.com/api/ticker/{pair_id}", timeout=10)
        ticker = r.json().get("ticker", {})
        high   = float(ticker.get("high", curr))
        low    = float(ticker.get("low", curr))
        vol    = float(ticker.get("vol_idr", 0))
        buy    = float(ticker.get("buy", curr))
        sell   = float(ticker.get("sell", curr))
        spread = (sell - buy) / buy * 100 if buy > 0 else 0
        pos_hl = (curr - low) / (high - low) * 100 if high != low else 50
    except:
        high = low = curr
        vol = spread = pos_hl = 0

    if pos_hl < 30:
        posisi_label = "🟢 Dekat LOW — peluang bagus"
    elif pos_hl > 70:
        posisi_label = "🔴 Dekat HIGH — risiko tinggi"
    else:
        posisi_label = "🟡 Tengah range"

    spread_label = f"🔴 {spread:.1f}% (tinggi)" if spread > 2 else f"🟢 {spread:.1f}% (aman)"

    msg = (
        f"🔔 *Kandidat Beli: {label}*\n"
        f"💰 Harga: {fmt(curr)}\n"
        f"📉 Low 24j: {fmt(low)} — 📈 High 24j: {fmt(high)}\n"
        f"📦 Volume: {fmt(vol)}\n"
        f"🔀 Spread: {spread_label}\n"
        f"📌 {posisi_label}\n\n"
        f"*Sinyal Teknikal ({sinyal}/5):*\n"
        f"{' | '.join(reasons)}\n"
        f"EMA9: {fmt(ema9)} | EMA21: {fmt(ema21)}\n"
        f"RSI: {rsi:.0f}\n\n"
        f"Modal trade: {fmt(idr_per_trade)}\n"
        f"TP: +{TP_PCT}% | Trail: {TRAIL_PCT}% | Time Stop: {TIME_STOP_HR}j\n\n"
        f"Ketik /ok untuk beli atau /batal untuk skip"
    )

    pending_confirm[pair_id] = {
        "price": curr,
        "idr":   idr_per_trade,
        "time":  time.time()
    }

    log(f"🔔 Kandidat: {label} | {sinyal}/5 sinyal")
    send_telegram(msg)

# ── Manage Positions ────────────────────────────────────
def manage_positions():
    global total_profit, total_trades, daily_profit, daily_trades, modal

    for pair_id in list(open_positions.keys()):
        pos       = open_positions[pair_id]
        buy_price = pos["buy_price"]
        qty       = pos["qty"]
        peak      = pos.get("peak", buy_price)
        entry_time= pos.get("entry_time", time.time())
        label     = pair_labels.get(pair_id, pair_id)
        curr      = prices.get(pair_id, buy_price)

        if curr > peak:
            open_positions[pair_id]["peak"] = curr
            peak = curr

        sell_price_est = curr * 0.97
        pl      = (sell_price_est - buy_price) * qty
        pl_pct  = (sell_price_est - buy_price) / buy_price * 100
        idr_in  = buy_price * qty
        fee_est = idr_in * 0.003 + sell_price_est * qty * 0.003
        pl_bersih = pl - fee_est

        hours_held  = (time.time() - entry_time) / 3600
        trail_drop  = (peak - curr) / peak * 100 if peak > 0 else 0
        has_exit, exit_desc = check_exit_signal(pair_id)

        should_sell = False
        sell_reason = ""

        # 1. Take Profit 7%
        if pl_pct >= TP_PCT:
            should_sell = True
            sell_reason = f"🎯 TP +{pl_pct:.1f}% | P/L:{fmt(pl)}"

        # 2. Trailing Stop 3% dari peak
        elif pl_pct > 0 and trail_drop >= TRAIL_PCT:
            should_sell = True
            sell_reason = f"📉 Trailing Stop {trail_drop:.1f}% | P/L:{fmt(pl)}"

        # 3. Exit sinyal teknikal (hanya kalau profit sudah nutup fee)
        elif has_exit and pl > fee_est:
            should_sell = True
            sell_reason = f"📊 Exit: {exit_desc} | P/L:{fmt(pl)}"

        # 4. Time Stop 48 jam
        elif hours_held >= TIME_STOP_HR:
            should_sell = True
            sell_reason = f"⏰ Time Stop {hours_held:.0f}j | P/L:{fmt(pl)}"

        if should_sell:
            result = place_sell(pair_id, curr, qty)
            if result and result.get("success") == 1:
                emoji = "🟢" if pl_bersih >= 0 else "🔴"
                total_profit += pl_bersih
                daily_profit += pl_bersih
                total_trades += 1
                daily_trades += 1
                get_idr_balance()

                send_telegram(
                    f"{emoji} *{sell_reason}*\n"
                    f"*{label}*\n"
                    f"Harga: {fmt(curr)}\n"
                    f"P/L Kotor: {fmt(pl)}\n"
                    f"Fee: {fmt(fee_est)}\n"
                    f"P/L Bersih: {fmt(pl_bersih)}\n"
                    f"Hold: {hours_held:.1f} jam\n"
                    f"Total Profit: {fmt(total_profit)}\n"
                    f"Modal: {fmt(modal)}\n"
                    f"Total Trade: {total_trades}"
                )
                del open_positions[pair_id]
                save_positions()

                if pl_bersih < 0:
                    add_blacklist(pair_id)
            else:
                log(f"⚠️ Gagal jual {label}")

        else:
            log(f"📊 {label} | Beli:{fmt(buy_price)} | Kini:{fmt(curr)} | P/L:{pl_pct:.1f}% | Hold:{hours_held:.1f}j")

# ── Daily Report ────────────────────────────────────────
def check_daily_summary():
    global daily_profit, daily_trades, last_summary
    now      = datetime.now(WIB)
    hour_key = f"{now.strftime('%Y-%m-%d')}-{now.hour}"
    report_hours = [6, 12, 18, 21]
    if now.hour in report_hours and last_summary != hour_key:
        last_summary = hour_key
        posisi = len(open_positions)
        emoji  = "🟢" if daily_profit >= 0 else "🔴"
        label  = "📊 *Daily Report*" if now.hour == 21 else "📊 *Update Report*"
        send_telegram(
            f"{label} — {now.strftime('%d/%m/%Y %H:%M')} WIB\n"
            f"Trade: {daily_trades}x\n"
            f"Profit: {emoji} {fmt(daily_profit)}\n"
            f"Total Profit: {fmt(total_profit)}\n"
            f"Modal: {fmt(modal)}\n"
            f"Posisi aktif: {posisi}/{MAX_TRADES}\n"
            f"Total Trade: {total_trades}"
        )
        if now.hour == 21:
            daily_profit = 0.0
            daily_trades = 0

# ── Telegram Commands ────────────────────────────────────
def get_tg_updates():
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 5},
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            return data.get("result", [])
    except:
        pass
    return []

def handle_command(text, chat_id):
    text = text.strip().lower()

    # Aliases
    if text == "/cancel": text = "/batal"
    elif text.startswith("/buy "): text = text.replace("/buy ", "/beli ", 1)
    elif text.startswith("/sell "): text = text.replace("/sell ", "/jual ", 1)

    if text == "/status":
        posisi = len(open_positions)
        pos_list = ""
        for pid, pos in open_positions.items():
            curr    = prices.get(pid, pos["buy_price"])
            pl_pct  = (curr * 0.97 - pos["buy_price"]) / pos["buy_price"] * 100
            hours   = (time.time() - pos.get("entry_time", time.time())) / 3600
            lbl     = pair_labels.get(pid, pid)
            emoji   = "🟢" if pl_pct >= 0 else "🔴"
            pos_list += f"{emoji} {lbl}: {pl_pct:.1f}% ({hours:.1f}j)\n"

        send_telegram(
            f"🤖 *IndoBot v9 Status*\n"
            f"💵 Modal: {fmt(modal)}\n"
            f"📊 Posisi: {posisi}/{MAX_TRADES}\n"
            f"{pos_list}"
            f"💰 Total Profit: {fmt(total_profit)}\n"
            f"📈 Total Trade: {total_trades}\n\n"
            f"*Command:*\n"
            f"/status — cek status\n"
            f"/beli COIN — beli manual\n"
            f"/jual COIN — jual manual\n"
            f"/pause — pause bot\n"
            f"/resume — hidupkan bot\n"
            f"/batal — cancel"
        )

    elif text == "/pause":
        global bot_paused
        bot_paused = True
        send_telegram("⏸️ *Bot di-PAUSE!*\nTidak akan scan kandidat baru.\nKetik /resume untuk hidupkan.")

    elif text == "/resume":
        bot_paused = False
        send_telegram("▶️ *Bot di-RESUME!*\nAktif scan kembali.")

    elif text.startswith("/beli "):
        coin = text.replace("/beli ", "").strip()
        pair = coin + "_idr"
        curr = prices.get(pair, 0)
        if curr <= 0:
            try:
                r = requests.get(f"https://indodax.com/api/ticker/{pair}", timeout=10)
                curr = float(r.json().get("ticker", {}).get("last", 0))
            except:
                pass
        if curr <= 0:
            send_telegram(f"❌ Coin {coin.upper()} tidak ditemukan!")
            return
        get_idr_balance()
        idr = modal * 0.9
        pending_confirm[pair] = {"price": curr, "idr": idr, "time": time.time(), "manual": True}
        sinyal, reasons, _ = check_signals(pair)
        send_telegram(
            f"🔔 *Manual Beli: {coin.upper()}/IDR*\n"
            f"💰 Harga: {fmt(curr)}\n"
            f"Sinyal: {sinyal}/5 — {' | '.join(reasons) if reasons else 'Tidak ada'}\n"
            f"Modal: {fmt(idr)}\n\n"
            f"Ketik /ok untuk beli atau /batal untuk cancel"
        )

    elif text.startswith("/jual "):
        coin = text.replace("/jual ", "").strip()
        pair = coin + "_idr"
        curr = prices.get(pair, 0)
        if curr <= 0:
            try:
                r = requests.get(f"https://indodax.com/api/ticker/{pair}", timeout=10)
                curr = float(r.json().get("ticker", {}).get("last", 0))
            except:
                curr = 0
        if curr <= 0:
            # Coba ambil dari wallet langsung
            qty, coin_key = get_coin_balance(coin)
            if qty <= 0:
                send_telegram(f"❌ Coin {coin.upper()} tidak ditemukan atau saldo 0!")
                return
            curr = 1  # harga tidak diketahui, set 1 supaya bisa jual
        pending_confirm[pair] = {"price": curr, "action": "sell", "time": time.time()}
        send_telegram(
            f"🔔 *Manual Jual: {coin.upper()}/IDR*\n"
            f"💰 Harga: {fmt(curr)}\n\n"
            f"Ketik /ok untuk jual atau /batal untuk cancel"
        )

    elif text == "/ok":
        if not pending_confirm:
            send_telegram("❌ Tidak ada aksi yang menunggu konfirmasi.")
            return
        pair_id = list(pending_confirm.keys())[0]
        confirm = pending_confirm.pop(pair_id)
        label   = pair_labels.get(pair_id, pair_id)

        if confirm.get("action") == "sell":
            curr   = confirm["price"]
            result = place_sell(pair_id, curr, 999999)
            if result and result.get("success") == 1:
                send_telegram(f"✅ *JUAL {label} BERHASIL!*")
                if pair_id in open_positions:
                    del open_positions[pair_id]
            else:
                send_telegram(f"❌ *JUAL {label} GAGAL!*")
        else:
            curr   = confirm["price"]
            idr    = confirm["idr"]
            result = place_buy(pair_id, curr, idr)
            if result and result.get("success") == 1:
                qty = idr / (curr * 1.03)
                open_positions[pair_id] = {
                    "buy_price":  int(curr * 1.03),
                    "qty":        qty,
                    "idr":        idr,
                    "peak":       curr,
                    "entry_time": time.time(),
                }
                save_positions()
                send_telegram(
                    f"✅ *BELI {label} BERHASIL!*\n"
                    f"Harga: {fmt(curr)}\n"
                    f"Modal: {fmt(idr)}\n"
                    f"TP: +{TP_PCT}% | Time Stop: {TIME_STOP_HR}j"
                )
            else:
                err = result.get("error", "Unknown") if result else "No response"
                send_telegram(f"❌ *BELI {label} GAGAL!*\nError: {err}")

    elif text == "/batal":
        if pending_confirm:
            pair_id = list(pending_confirm.keys())[0]
            pending_confirm.pop(pair_id)
            label = pair_labels.get(pair_id, pair_id)
            # Skip coin ini 1 jam supaya tidak muncul lagi segera
            blacklist[pair_id] = time.time() - (BLACKLIST_HR - 1) * 3600
            send_telegram(f"✅ *{label} dilewati* — tidak akan muncul 1 jam.")
        else:
            send_telegram("Tidak ada aksi yang perlu dibatalkan.")

    else:
        send_telegram(
            f"❓ Command tidak dikenal.\n\n"
            f"*Command:*\n"
            f"/status — cek status\n"
            f"/beli COIN — beli manual\n"
            f"/jual COIN — jual manual\n"
            f"/pause — pause bot\n"
            f"/resume — hidupkan bot\n"
            f"/batal — cancel"
        )

def check_tg_commands():
    updates = get_tg_updates()
    for update in updates:
        global last_update_id
        last_update_id = update["update_id"]
        msg     = update.get("message", {})
        text    = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if text and text.startswith("/") and chat_id == str(TG_CHAT_ID):
            log(f"📱 Command: {text}")
            handle_command(text, chat_id)

# ── Main ────────────────────────────────────────────────
bot_paused = False

def main():
    global bot_paused
    log("🚀 IndoBot v9 dimulai...")
    fetch_all_pairs()
    fetch_prices()  # fetch harga dulu sebelum restore
    get_idr_balance()
    load_positions()
    restore_from_wallet()  # restore coin yang ada di wallet

    send_telegram(
        f"🚀 *IndoBot v9 AI AKTIF*\n"
        f"Modal: {fmt(modal)}\n"
        f"TP: {TP_PCT}% | Trail: {TRAIL_PCT}%\n"
        f"Time Stop: {TIME_STOP_HR} jam\n"
        f"Blacklist: {BLACKLIST_HR} jam\n"
        f"Max Posisi: {MAX_TRADES}\n"
        f"Scan: {TOP_N_PAIRS} pair tiap {SCAN_INTERVAL}s\n"
        f"Min Sinyal: {MIN_SIGNALS}/5\n"
        f"Mode: Semi-Auto (konfirmasi via Telegram)\n"
        f"Mode: Non-stop seumur hidup! 🔄"
    )

    tick = 0
    while True:
        try:
            check_tg_commands()
            cleanup_pending()
            if tick % 5 == 0:
                fetch_prices()
            if not bot_paused:
                manage_positions()
                scan_candidates()
            check_daily_summary()
            tick += 1
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            log("Bot dihentikan.")
            break
        except Exception as e:
            log(f"❌ Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
