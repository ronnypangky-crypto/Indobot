import os, time, hmac, hashlib, requests, json
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

RAILWAY_TOKEN  = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_SVC_ID = os.environ.get("RAILWAY_BOT_SERVICE_ID", "")

# ── Config ─────────────────────────────────────────────
TG_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY       = os.environ.get("INDODAX_API_KEY", "")
API_SECRET    = os.environ.get("INDODAX_SECRET_KEY", "")

TOP_N_PAIRS   = int(os.environ.get("TOP_N_PAIRS",   "400"))
MAX_TRADES    = int(os.environ.get("MAX_TRADES",    "20"))
TP_PCT        = float(os.environ.get("TP_PCT",      "25"))
TRAIL_PCT     = float(os.environ.get("TRAIL_PCT",   "10"))
TIME_STOP_HR  = int(os.environ.get("TIME_STOP_HR",  "0"))   # 0 = hold seumur hidup
BLACKLIST_HR  = int(os.environ.get("BLACKLIST_HR",  "2"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "10"))
MIN_SIGNALS   = int(os.environ.get("MIN_SIGNALS",   "4"))
MIN_VOL_IDR   = float(os.environ.get("MIN_VOL_IDR",  "30000000"))  # min volume 30jt
MIN_PRICE     = float(os.environ.get("MIN_PRICE",    "150"))          # min harga Rp 150
MAX_SPREAD    = float(os.environ.get("MAX_SPREAD",   "1.5"))         # max spread 1.5%
WARN_STOP_HR  = int(os.environ.get("WARN_STOP_HR",  "24"))          # warning sebelum time stop
POSITION_RPT  = int(os.environ.get("POSITION_RPT",  "60"))          # laporan posisi tiap 60 menit

WIB = timezone(timedelta(hours=7))
POSITIONS_FILE = "/tmp/positions.json"

SKIP_PAIRS = {
    "usdt_idr", "usdc_idr", "busd_idr",
    "doge_idr", "rvn_idr", "shib_idr"
}

INTEGER_COINS = {
    "sats", "shib", "floki", "pepe", "bonk", "lunc",
    "trollsol", "jellyjelly", "fartcoin", "neiro"
}

# ── State ───────────────────────────────────────────────
prices         = {}
price_history  = {}
volume_history = {}
open_positions = {}
blacklist      = {}
pending_confirm= {}
last_update_id = 0
modal          = 0.0
total_profit   = 0.0
total_trades   = 0
daily_profit   = 0.0
daily_trades   = 0
last_summary         = ""
last_position_report = 0
all_pairs      = []
pair_labels    = {}
bot_paused     = False

# ── Helpers ─────────────────────────────────────────────
def now_str():
    return datetime.now(WIB).strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)

def fmt(val):
    try:
        val = float(val)
        if abs(val) >= 1_000_000: return f"Rp {val/1_000_000:.2f}jt"
        if abs(val) >= 1000:      return f"Rp {val/1000:.1f}rb"
        return f"Rp {val:.0f}"
    except:
        return f"Rp {val}"

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

# ── Indodax API ─────────────────────────────────────────
def indodax_request(method, params={}):
    try:
        params["method"]     = method
        params["timestamp"]  = str(int(time.time() * 1000))
        params["recvWindow"] = "5000"
        body = urlencode(params)
        sig  = hmac.new(API_SECRET.encode(), body.encode(), hashlib.sha512).hexdigest()
        r = requests.post(
            "https://indodax.com/tapi",
            data=params,
            headers={"Key": API_KEY, "Sign": sig},
            timeout=15
        )
        return r.json()
    except Exception as e:
        log(f"API Error: {e}")
        return None

def get_idr_balance():
    global modal
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        raw   = str(result.get("return", {}).get("balance", {}).get("idr", "0"))
        # Hapus titik dan koma pemisah ribuan
        raw   = raw.replace(",", "").replace(".", "")
        modal = float(raw) if raw else 0
        log(f"💵 Balance IDR: {fmt(modal)}")
    return modal

def get_coin_balance(coin):
    result = indodax_request("getInfo")
    if result and result.get("success") == 1:
        balances = result.get("return", {}).get("balance", {})
        for key in [coin, coin.lower(), coin.upper()]:
            raw = str(balances.get(key, "0")).replace(",", "")
            val = float(raw) if raw else 0
            if val > 0:
                return val, key
    return 0, coin

def format_qty(coin_key, qty):
    if coin_key.lower() in INTEGER_COINS:
        return str(int(qty))
    return f"{qty:.8f}"

# ── Teknikal ────────────────────────────────────────────
def calc_ema(arr, period):
    if len(arr) < period:
        return arr[-1] if arr else 0
    k, ema = 2 / (period + 1), arr[0]
    for p in arr[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(arr, period=14):
    if len(arr) < period + 1:
        return 50
    gains = [max(arr[i]-arr[i-1], 0) for i in range(1, len(arr))]
    losses= [max(arr[i-1]-arr[i], 0) for i in range(1, len(arr))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100
    return 100 - (100 / (1 + ag/al))

def calc_macd(arr):
    if len(arr) < 26: return 0, 0, 0
    macd   = calc_ema(arr, 12) - calc_ema(arr, 26)
    signal = calc_ema([macd], 9)
    return macd, signal, macd - signal

def calc_alligator(arr):
    if len(arr) < 13: return False, "Data kurang"
    jaw   = calc_ema(arr, 13)
    teeth = calc_ema(arr, 8)
    lips  = calc_ema(arr, 5)
    if lips > teeth > jaw:  return True,  "Alligator Bullish 🐊✅"
    if lips < teeth < jaw:  return False, "Alligator Bearish 🐊"
    return False, "Alligator Sideways 🐊"

# ── Sinyal Entry Swing ──────────────────────────────────
def check_signals(pair_id):
    """
    5 sinyal swing trading:
    1. RSI < 40 (oversold) — WAJIB
    2. Harga < 35% dari range Low-High (dekat LOW) — WAJIB
    3. Harga di atas EMA 50 (uptrend) — WAJIB
    4. Alligator tidak bearish
    5. Volume tidak spike (belum pump)
    """
    hist  = price_history.get(pair_id, [])
    vol_h = volume_history.get(pair_id, [])
    if len(hist) < 50:  # butuh minimal 50 candle untuk EMA 50
        return 0, [], {}

    curr    = hist[-1]
    sinyal  = 0
    reasons = []
    details = {}

    # 1. RSI < 40 — WAJIB
    rsi = calc_rsi(hist)
    details["rsi"] = rsi
    if rsi >= 40:
        return 0, [], details
    sinyal += 1
    reasons.append(f"RSI={rsi:.0f}✅")

    # 2. Harga dekat Low 24j — WAJIB
    try:
        r = requests.get(f"https://indodax.com/api/ticker/{pair_id}", timeout=5)
        ticker = r.json().get("ticker", {})
        high   = float(ticker.get("high", curr))
        low    = float(ticker.get("low",  curr))
        buy    = float(ticker.get("buy",  curr))
        sell_p = float(ticker.get("sell", curr))
        spread = (sell_p - buy) / buy * 100 if buy > 0 else 99
        pos_hl = (curr - low) / (high - low) * 100 if high != low else 50
        vol_idr= float(ticker.get("vol_idr", 0))
        details.update({"high": high, "low": low, "spread": spread,
                        "pos_hl": pos_hl, "vol_idr": vol_idr})
    except:
        return 0, [], details

    if pos_hl >= 35:
        return 0, [], details
    sinyal += 1
    reasons.append(f"Dekat LOW {pos_hl:.0f}%✅")

    # Filter volume minimum
    if vol_idr < MIN_VOL_IDR:
        log(f"⏭️ {pair_id} volume terlalu kecil: {fmt(vol_idr)}")
        return 0, [], details

    # Filter spread maksimal
    if spread > MAX_SPREAD:
        log(f"⏭️ {pair_id} spread terlalu besar: {spread:.1f}%")
        return 0, [], details

    # Filter harga minimum
    if curr < MIN_PRICE:
        log(f"⏭️ {pair_id} harga terlalu murah: {fmt(curr)}")
        return 0, [], details

    # 3. EMA 50 — harga harus di atas EMA 50 (uptrend) — WAJIB
    ema50 = calc_ema(hist, 50)
    details["ema50"] = ema50
    if curr < ema50:
        log(f"⏭️ {pair_id} di bawah EMA50 — downtrend, skip!")
        return 0, [], details
    sinyal += 1
    reasons.append(f"EMA50✅ ({fmt(ema50)})")

    # 4. Alligator tidak bearish
    alligator_bull, alligator_desc = calc_alligator(hist)
    details["alligator"] = alligator_bull
    if alligator_bull:
        sinyal += 1
        reasons.append(alligator_desc)
    else:
        reasons.append(alligator_desc)

    # 5. Volume tidak spike (belum pump)
    if len(vol_h) >= 5:
        avg_vol = sum(vol_h[:-1]) / (len(vol_h) - 1)
        if avg_vol > 0:
            vol_ratio = vol_h[-1] / avg_vol
            details["vol_ratio"] = vol_ratio
            if vol_ratio < 3:
                sinyal += 1
                reasons.append(f"Volume normal {vol_ratio:.1f}x✅")
            else:
                reasons.append(f"Volume spike {vol_ratio:.1f}x⚠️")

    return sinyal, reasons, details

def check_exit(pair_id):
    """Exit kalau 2+ sinyal bearish muncul"""
    hist = price_history.get(pair_id, [])
    if len(hist) < 14: return False, ""
    exits = []
    rsi   = calc_rsi(hist)
    if rsi > 70: exits.append(f"RSI={rsi:.0f} overbought")
    macd, signal, _ = calc_macd(hist)
    if macd < signal: exits.append("MACD turun")
    alligator_bull, desc = calc_alligator(hist)
    if not alligator_bull: exits.append(desc)
    return len(exits) >= 3, " | ".join(exits)  # butuh 3 sinyal bearish untuk exit

# ── Fetch Data ──────────────────────────────────────────
def fetch_all_pairs():
    global all_pairs, pair_labels
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=15)
        data = r.json().get("tickers", {})
        pairs = []
        for pid, info in data.items():
            if not pid.endswith("_idr") or pid in SKIP_PAIRS: continue
            vol = float(str(info.get("vol_idr", "0")).replace(",", ""))
            pairs.append((pid, vol))
            pair_labels[pid] = f"{pid.replace('_idr','').upper()}/IDR"
        pairs.sort(key=lambda x: x[1], reverse=True)
        all_pairs = [p[0] for p in pairs[:TOP_N_PAIRS]]
        log(f"📋 {len(all_pairs)} pair dimuat")
    except Exception as e:
        log(f"⚠️ Gagal fetch pairs: {e}")

def fetch_prices():
    try:
        r = requests.get("https://indodax.com/api/summaries", timeout=15)
        data = r.json().get("tickers", {})
        for pid in all_pairs:
            info = data.get(pid, {})
            curr = float(str(info.get("last", "0")).replace(",", ""))
            vol  = float(str(info.get("vol_idr", "0")).replace(",", ""))
            if curr > 0:
                prices[pid] = curr
                if pid not in price_history:
                    price_history[pid] = []
                    volume_history[pid] = []
                price_history[pid].append(curr)
                volume_history[pid].append(vol)
                if len(price_history[pid]) > 200:
                    price_history[pid]  = price_history[pid][-200:]
                    volume_history[pid] = volume_history[pid][-200:]
    except Exception as e:
        log(f"⚠️ Gagal fetch prices: {e}")

# ── Save/Load Posisi ────────────────────────────────────
def save_to_railway(data_str):
    """Simpan posisi ke Railway Variable sebagai backup permanen"""
    if not RAILWAY_TOKEN or not RAILWAY_SVC_ID:
        return
    try:
        mutation = """
        mutation variableUpsert($input: VariableUpsertInput!) {
            variableUpsert(input: $input)
        }
        """
        requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={"query": mutation, "variables": {"input": {
                "serviceId": RAILWAY_SVC_ID,
                "name": "POSITIONS_BACKUP",
                "value": data_str
            }}},
            timeout=10
        )
        log("☁️ Posisi tersimpan ke Railway")
    except Exception as e:
        log(f"⚠️ Gagal simpan ke Railway: {e}")

def save_positions():
    try:
        # Simpan ke file lokal
        with open(POSITIONS_FILE, "w") as f:
            json.dump(open_positions, f)
        # Backup ke Railway Variable
        save_to_railway(json.dumps(open_positions))
    except Exception as e:
        log(f"⚠️ Gagal simpan posisi: {e}")

def load_positions():
    global open_positions
    loaded = False

    # Coba load dari file lokal dulu
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, "r") as f:
                data = json.load(f)
            if data:
                open_positions = data
                loaded = True
                log(f"📂 Loaded {len(open_positions)} posisi dari file lokal")
    except Exception as e:
        log(f"⚠️ Gagal load file lokal: {e}")

    # Kalau file lokal kosong/tidak ada, coba dari Railway Variable
    if not loaded:
        backup = os.environ.get("POSITIONS_BACKUP", "")
        if backup:
            try:
                data = json.loads(backup)
                if data:
                    open_positions = data
                    loaded = True
                    log(f"☁️ Loaded {len(open_positions)} posisi dari Railway backup!")
            except Exception as e:
                log(f"⚠️ Gagal load Railway backup: {e}")

    if loaded and open_positions:
        pos_list = "\n".join([
            f"• {pair_labels.get(p, p)}: beli {fmt(v.get('buy_price',0))} | hold {(time.time()-v.get('entry_time',time.time()))/3600:.1f}j"
            for p, v in open_positions.items()
        ])
        send_telegram(
            f"📂 *Posisi Restored ({len(open_positions)} posisi):*\n{pos_list}\n"
            f"⚠️ Harga beli & jam asli tersimpan!"
        )

def get_buy_price_from_history(pair_id):
    """Ambil harga beli terakhir dari trade history Indodax"""
    try:
        # Debug: cek raw response
        try:
            params = {
                "method": "tradeHistory",
                "pair":   pair_id,
                "type":   "buy",
                "count":  "5",
                "timestamp":  str(int(time.time() * 1000)),
                "recvWindow": "5000"
            }
            from urllib.parse import urlencode
            import hmac, hashlib
            body = urlencode(params)
            sig  = hmac.new(API_SECRET.encode(), body.encode(), hashlib.sha512).hexdigest()
            r = requests.post("https://indodax.com/tapi", data=params,
                            headers={"Key": API_KEY, "Sign": sig}, timeout=15)
            log(f"📋 tradeHistory raw status: {r.status_code}")
            log(f"📋 tradeHistory raw response: {r.text[:200]}")
            result = r.json() if r.text.strip() else None
        except Exception as e:
            log(f"⚠️ tradeHistory debug error: {e}")
            result = None
        if not result or result.get("success") != 1:
            log(f"⚠️ tradeHistory {pair_id} gagal: {result}")
            return 0, 0
        trades = result.get("return", {}).get("trades", [])
        if not trades:
            log(f"⚠️ tradeHistory {pair_id} kosong")
            return 0, 0
        latest = trades[0]
        price  = float(str(latest.get("price", "0")).replace(",", ""))
        qty    = float(str(latest.get("amount", "0")).replace(",", ""))
        total  = price * qty
        if price > 0 and qty > 0:
            log(f"📋 Harga beli {pair_id}: {fmt(price)} × {qty:.6f} = {fmt(total)}")
            return price, total
    except Exception as e:
        log(f"⚠️ Gagal ambil history {pair_id}: {e}")
    return 0, 0

def restore_from_wallet():
    """Restore coin dari wallet yang tidak ada di positions.json"""
    global open_positions
    # Kalau sudah ada posisi dari load_positions, skip restore wallet
    if open_positions:
        log(f"📂 Skip wallet restore — sudah ada {len(open_positions)} posisi dari backup")
        return
    try:
        result = indodax_request("getInfo")
        if not result or result.get("success") != 1: return
        balances = result.get("return", {}).get("balance", {})
        restored = 0
        for coin, qty_raw in balances.items():
            qty = float(str(qty_raw).replace(",", ""))
            if qty <= 0: continue
            if coin in ["idr", "usdt", "usdc", "idr_locked"]: continue
            pair_id = f"{coin}_idr"
            if pair_id in SKIP_PAIRS or pair_id in open_positions: continue
            curr = prices.get(pair_id, 0)
            if curr <= 0:
                try:
                    r = requests.get(f"https://indodax.com/api/ticker/{pair_id}", timeout=5)
                    curr = float(r.json().get("ticker", {}).get("last", 0))
                except: continue
            if curr <= 0: continue
            idr_val = curr * qty
            if idr_val < 10000: continue
            if curr < MIN_PRICE: continue

            # Coba ambil harga beli asli dari trade history
            real_price, _ = get_buy_price_from_history(pair_id)
            buy_price = real_price if real_price > 0 else curr
            idr_val   = buy_price * qty

            open_positions[pair_id] = {
                "buy_price":  buy_price,
                "qty":        qty,
                "idr":        idr_val,
                "peak":       max(buy_price, curr),
                "entry_time": time.time(),
                "estimated":  real_price <= 0,
            }
            log(f"📂 Restore wallet: {pair_labels.get(pair_id, pair_id)} ~{fmt(idr_val)} (estimasi)")
            restored += 1
        if restored > 0:
            save_positions()
            send_telegram(
                f"📂 *{restored} posisi restored dari wallet*\n"
                f"⚠️ Harga beli = estimasi harga sekarang\n"
                f"Gunakan /restore COIN HARGA untuk input harga beli asli!"
            )
    except Exception as e:
        log(f"⚠️ Gagal restore wallet: {e}")

# ── Blacklist ────────────────────────────────────────────
def add_blacklist(pair_id, hours=None):
    if hours is None: hours = BLACKLIST_HR
    blacklist[pair_id] = time.time()
    log(f"🚫 Blacklist {pair_labels.get(pair_id, pair_id)} {hours} jam")

def is_blacklisted(pair_id):
    if pair_id in blacklist:
        if time.time() - blacklist[pair_id] < BLACKLIST_HR * 3600:
            return True
        del blacklist[pair_id]
    return False

# ── Buy/Sell ────────────────────────────────────────────
def place_buy(pair_id, price, idr_amount):
    market_price = int(price * 1.03)
    log(f"📤 BUY {pair_id} | IDR:{int(idr_amount)} | Harga:{market_price}")
    result = indodax_request("trade", {
        "pair": pair_id, "type": "buy",
        "price": str(market_price), "idr": str(int(idr_amount)),
    })
    if result and result.get("success") == 1:
        log(f"✅ BUY berhasil")
    else:
        err = result.get("error", "") if result else "no response"
        log(f"⚠️ BUY gagal: {err}")
    return result

def place_sell(pair_id, price, qty_to_sell=999999):
    coin = pair_id.replace("_idr", "")
    actual_qty, coin_key = get_coin_balance(coin)
    if actual_qty <= 0:
        log(f"⚠️ Saldo {coin} = 0")
        return {"success": 1, "zero_balance": True}
    sell_qty     = min(qty_to_sell, actual_qty)
    qty_str      = format_qty(coin_key, sell_qty)
    market_price = int(price * 0.97)
    log(f"📤 SELL {pair_id} | {coin_key}:{qty_str} | Harga:{market_price}")
    result = indodax_request("trade", {
        "pair": pair_id, "type": "sell",
        "price": str(market_price), coin_key: qty_str,
    })
    # Retry integer kalau decimal error
    if result and "decimal" in str(result.get("error", "")):
        INTEGER_COINS.add(coin_key.lower())
        result = indodax_request("trade", {
            "pair": pair_id, "type": "sell",
            "price": str(market_price), coin_key: str(int(sell_qty)),
        })
    # Log error detail
    if not result or result.get("success") != 1:
        err = result.get("error", "Unknown") if result else "No response"
        log(f"❌ SELL gagal: {err}")
    return result

# ── Scan Kandidat ────────────────────────────────────────
def cleanup_expired_pending():
    expired = [p for p, d in pending_confirm.items()
               if time.time() - d.get("time", 0) > 600]
    for p in expired:
        label = pair_labels.get(p, p)
        del pending_confirm[p]
        add_blacklist(p, 1)
        log(f"⏰ Pending {label} expired")
        send_telegram(f"⏰ *{label} expired* — tidak ada konfirmasi 10 menit.")

def scan_candidates():
    global modal
    active = len(open_positions) + len(pending_confirm)
    if active >= MAX_TRADES:
        log(f"⏳ Slot penuh {active}/{MAX_TRADES}")
        return

    get_idr_balance()
    slots_left    = MAX_TRADES - active
    idr_per_trade = modal / slots_left if slots_left > 0 else modal
    if idr_per_trade < 3000:
        log(f"⚠️ Modal per trade terlalu kecil: {fmt(idr_per_trade)}")
        return

    best_pair   = None
    best_sinyal = 0

    for pid in all_pairs:
        if pid in open_positions or pid in pending_confirm: continue
        if is_blacklisted(pid): continue
        curr = prices.get(pid, 0)
        if curr <= 0: continue

        sinyal, reasons, details = check_signals(pid)
        if sinyal >= MIN_SIGNALS and sinyal > best_sinyal:
            best_sinyal = sinyal
            best_pair   = (pid, sinyal, reasons, details)

    if not best_pair:
        log(f"⏳ Scan {len(all_pairs)} pair — tidak ada kandidat ({active}/{MAX_TRADES})")
        return

    pid, sinyal, reasons, details = best_pair
    label  = pair_labels.get(pid, pid)
    curr   = prices.get(pid, 0)
    high   = details.get("high", curr)
    low    = details.get("low",  curr)
    spread = details.get("spread", 0)
    pos_hl = details.get("pos_hl", 50)
    rsi    = details.get("rsi", 50)

    spread_label = f"🔴 {spread:.1f}% (tinggi)" if spread > 2 else f"🟢 {spread:.1f}% (aman)"
    if pos_hl < 30:   kondisi = "🟢 Dekat LOW — peluang bagus"
    elif pos_hl > 70: kondisi = "🔴 Dekat HIGH — risiko tinggi"
    else:             kondisi = "🟡 Tengah range"

    log(f"🔔 Auto beli: {label} | {sinyal}/{MIN_SIGNALS} sinyal")

    # Auto buy langsung tanpa konfirmasi
    result = place_buy(pid, curr, idr_per_trade)
    if result and result.get("success") == 1:
        qty = idr_per_trade / (curr * 1.03)
        open_positions[pid] = {
            "buy_price":  int(curr * 1.03),
            "qty":        qty,
            "idr":        idr_per_trade,
            "peak":       curr,
            "entry_time": time.time(),
        }
        save_positions()
        send_telegram(
            f"🛒 *AUTO BELI: {label}*\n"
            f"💰 Harga: {fmt(curr)}\n"
            f"📉 Low: {fmt(low)} — 📈 High: {fmt(high)}\n"
            f"🔀 Spread: {spread_label}\n"
            f"📌 {kondisi}\n\n"
            f"*Sinyal ({sinyal}/{MIN_SIGNALS}):*\n"
            f"{' | '.join(reasons)}\n"
            f"RSI: {rsi:.0f}\n\n"
            f"Modal: {fmt(idr_per_trade)}\n"
            f"TP: +{TP_PCT}% | Trail: {TRAIL_PCT}%\n"
            f"Hold: sampai profit! 💪"
        )
    else:
        err = result.get("error", "") if result else "no response"
        log(f"❌ Auto beli {label} gagal: {err}")

# ── Manage Posisi ────────────────────────────────────────
def manage_positions():
    global total_profit, total_trades, daily_profit, daily_trades

    for pid in list(open_positions.keys()):
        pos        = open_positions[pid]
        buy_price  = pos["buy_price"]
        qty        = pos["qty"]
        peak       = pos.get("peak", buy_price)
        entry_time = pos.get("entry_time", time.time())
        label      = pair_labels.get(pid, pid)
        curr       = prices.get(pid, buy_price)

        # Update peak
        if curr > peak:
            open_positions[pid]["peak"] = curr
            peak = curr

        sell_est  = curr  # pakai harga actual, bukan estimasi -3%
        pl        = (sell_est - buy_price) * qty
        pl_pct    = (sell_est - buy_price) / buy_price * 100
        fee_est   = (buy_price * qty * 0.003) + (sell_est * qty * 0.003)
        pl_bersih = pl - fee_est
        hours     = (time.time() - entry_time) / 3600
        trail_drop= (peak - curr) / peak * 100 if peak > 0 else 0
        has_exit, exit_desc = check_exit(pid)

        should_sell = False
        sell_reason = ""

        # Warning 24 jam sebelum time stop
        if TIME_STOP_HR - hours <= WARN_STOP_HR and not pos.get("warned"):
            open_positions[pid]["warned"] = True
            save_positions()
            send_telegram(
                f"⚠️ *Time Stop Warning: {label}*\n"
                f"Posisi akan di-cut dalam {TIME_STOP_HR - hours:.0f} jam!\n"
                f"P/L sekarang: {pl_pct:.1f}% ({fmt(pl_bersih)})\n"
                f"Ketik /jual {pid.replace('_idr','')} kalau mau jual manual"
            )

        # Auto-extend time stop kalau harga mulai naik
        if hours >= TIME_STOP_HR and pl_pct > -2 and pl_pct < 0:
            open_positions[pid]["entry_time"] = time.time() - (TIME_STOP_HR - 12) * 3600
            log(f"🔄 Auto-extend time stop {label} — harga mulai recovery")

        if pl_pct >= TP_PCT:
            should_sell = True
            sell_reason = f"🎯 TP +{pl_pct:.1f}%"
        elif pl_pct > 0 and trail_drop >= TRAIL_PCT:
            should_sell = True
            sell_reason = f"📉 Trail -{trail_drop:.1f}%"
        elif has_exit and pl_pct >= 3 and pl > fee_est:  # exit sinyal hanya kalau sudah profit 3%+
            should_sell = True
            sell_reason = f"📊 Exit: {exit_desc}"
        elif TIME_STOP_HR > 0 and hours >= TIME_STOP_HR:
            should_sell = True
            sell_reason = f"⏰ Time Stop {hours:.0f}j"

        if should_sell:
            result = place_sell(pid, curr)
            if result and result.get("success") == 1:
                emoji = "🟢" if pl_bersih >= 0 else "🔴"
                total_profit += pl_bersih
                daily_profit += pl_bersih
                total_trades += 1
                daily_trades += 1
                get_idr_balance()
                del open_positions[pid]
                save_positions()
                if pl_bersih < 0:
                    add_blacklist(pid)
                send_telegram(
                    f"{emoji} *{sell_reason}*\n"
                    f"*{label}*\n"
                    f"Harga: {fmt(curr)}\n"
                    f"P/L Kotor: {fmt(pl)}\n"
                    f"Fee: {fmt(fee_est)}\n"
                    f"P/L Bersih: {fmt(pl_bersih)}\n"
                    f"Hold: {hours:.1f} jam\n"
                    f"Total Profit: {fmt(total_profit)}\n"
                    f"Modal: {fmt(modal)}\n"
                    f"Total Trade: {total_trades}"
                )
        else:
            log(f"📊 {label} | {fmt(curr)} | P/L:{pl_pct:.1f}% | Hold:{hours:.1f}j")

# ── Daily Report ─────────────────────────────────────────
def check_position_report():
    """Kirim laporan posisi tiap POSITION_RPT menit"""
    global last_position_report
    if not open_positions:
        return
    if time.time() - last_position_report < POSITION_RPT * 60:
        return
    last_position_report = time.time()
    now = datetime.now(WIB)
    pos_list = ""
    for pid, pos in open_positions.items():
        curr  = prices.get(pid, pos["buy_price"])
        pl_pct= (curr - pos["buy_price"]) / pos["buy_price"] * 100
        hours = (time.time() - pos.get("entry_time", time.time())) / 3600
        pl    = (curr - pos["buy_price"]) * pos["qty"]
        emoji = "🟢" if pl_pct >= 0 else "🔴"
        pos_list += f"{emoji} {pair_labels.get(pid, pid)}: {pl_pct:.1f}% ({fmt(pl)}) | {hours:.0f}j/{TIME_STOP_HR}j\n"
    send_telegram(
        f"📊 *Update Posisi — {now.strftime('%H:%M')} WIB*\n"
        f"{pos_list}"
        f"Modal IDR: {fmt(modal)}"
    )

def check_daily_report():
    global daily_profit, daily_trades, last_summary
    now      = datetime.now(WIB)
    hour_key = f"{now.strftime('%Y-%m-%d')}-{now.hour}"
    if now.hour in [6, 12, 18, 21] and last_summary != hour_key:
        last_summary = hour_key
        emoji  = "🟢" if daily_profit >= 0 else "🔴"
        label  = "📊 *Daily Report*" if now.hour == 21 else "📊 *Update Report*"
        send_telegram(
            f"{label} — {now.strftime('%d/%m/%Y %H:%M')} WIB\n"
            f"Trade: {daily_trades}x\n"
            f"Profit: {emoji} {fmt(daily_profit)}\n"
            f"Total Profit: {fmt(total_profit)}\n"
            f"Modal IDR: {fmt(modal)}\n"
            f"Posisi: {len(open_positions)}/{MAX_TRADES}\n"
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
    except: pass
    return []

def handle_command(text, chat_id):
    global bot_paused, open_positions
    text = text.strip().lower()
    if text == "/cancel": text = "/batal"
    if text.startswith("/buy "): text = "/beli " + text[5:]
    if text.startswith("/sell "): text = "/jual " + text[6:]

    if text == "/status":
        pos_list = ""
        for pid, pos in open_positions.items():
            curr  = prices.get(pid, pos["buy_price"])
            pl_pct= (curr - pos["buy_price"]) / pos["buy_price"] * 100  # display tanpa fee
            hours = (time.time() - pos.get("entry_time", time.time())) / 3600
            emoji = "🟢" if pl_pct >= 0 else "🔴"
            pos_list += f"{emoji} {pair_labels.get(pid, pid)}: {pl_pct:.1f}% ({hours:.1f}j)\n"
        mode = "⏸️ PAUSED" if bot_paused else "▶️ AUTO"
        send_telegram(
            f"🤖 *Status IndoBot v9*\n"
            f"Mode: {mode}\n"
            f"💵 Modal IDR: {fmt(modal)}\n"
            f"📊 Posisi: {len(open_positions)}/{MAX_TRADES}\n"
            f"{pos_list}"
            f"💰 Total Profit: {fmt(total_profit)}\n"
            f"📈 Total Trade: {total_trades}\n\n"
            f"*Command:*\n"
            f"/beli COIN — beli manual\n"
            f"/jual COIN — jual manual\n"
            f"/skip COIN — hapus posisi tanpa jual (dust)\n"
        f"/restore COIN TOTAL\\_IDR — restore posisi manual\n"
        f"/resetposisi — reset & restore semua dari Indodax\n"
            f"/restore COIN HARGA — restore posisi harga asli\n"
            f"/pause — pause bot\n"
            f"/resume — aktifkan bot\n"
            f"/batal — cancel aksi"
        )

    elif text == "/pause":
        bot_paused = True
        send_telegram("⏸️ *Bot di-PAUSE!* Ketik /resume untuk aktifkan.")

    elif text == "/resume":
        bot_paused = False
        send_telegram("▶️ *Bot AKTIF kembali!*")

    elif text.startswith("/beli "):
        coin = text[6:].strip()
        pair = coin + "_idr"
        curr = prices.get(pair, 0)
        if curr <= 0:
            try:
                r = requests.get(f"https://indodax.com/api/ticker/{pair}", timeout=10)
                curr = float(r.json().get("ticker", {}).get("last", 0))
            except: pass
        if curr <= 0:
            send_telegram(f"❌ Coin {coin.upper()} tidak ditemukan!")
            return
        get_idr_balance()
        idr = modal * 0.9
        pending_confirm[pair] = {"price": curr, "idr": idr, "time": time.time()}
        sinyal, reasons, _ = check_signals(pair)
        send_telegram(
            f"🔔 *Manual Beli: {coin.upper()}/IDR*\n"
            f"💰 Harga: {fmt(curr)}\n"
            f"Modal: {fmt(idr)}\n"
            f"Sinyal: {sinyal}/{MIN_SIGNALS} — {' | '.join(reasons) or 'Tidak ada'}\n\n"
            f"Ketik /ok untuk beli atau /batal untuk cancel"
        )

    elif text.startswith("/jual "):
        coin = text[6:].strip()
        pair = coin + "_idr"
        curr = prices.get(pair, 0)
        if curr <= 0:
            try:
                r = requests.get(f"https://indodax.com/api/ticker/{pair}", timeout=10)
                curr = float(r.json().get("ticker", {}).get("last", 0))
            except: pass
        if curr <= 0: curr = 1  # fallback
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
        pair  = list(pending_confirm.keys())[0]
        conf  = pending_confirm.pop(pair)
        label = pair_labels.get(pair, pair)

        if conf.get("action") == "sell":
            result = place_sell(pair, conf["price"])
            if result and result.get("success") == 1:
                if pair in open_positions:
                    del open_positions[pair]
                    save_positions()
                send_telegram(f"✅ *JUAL {label} BERHASIL!*")
            else:
                err = result.get("error", "Unknown") if result else "No response"
                send_telegram(f"❌ *JUAL {label} GAGAL!*\nError: {err}")
        else:
            result = place_buy(pair, conf["price"], conf["idr"])
            if result and result.get("success") == 1:
                qty = conf["idr"] / (conf["price"] * 1.03)
                open_positions[pair] = {
                    "buy_price":  int(conf["price"] * 1.03),
                    "qty":        qty,
                    "idr":        conf["idr"],
                    "peak":       conf["price"],
                    "entry_time": time.time(),
                }
                save_positions()
                send_telegram(
                    f"✅ *BELI {label} BERHASIL!*\n"
                    f"Harga: {fmt(conf['price'])}\n"
                    f"Modal: {fmt(conf['idr'])}\n"
                    f"TP: +{TP_PCT}% | Trail: {TRAIL_PCT}% | Time Stop: {TIME_STOP_HR}j"
                )
            else:
                err = result.get("error", "Unknown") if result else "No response"
                send_telegram(f"❌ *BELI {label} GAGAL!*\nError: {err}")

    elif text == "/batal":
        if pending_confirm:
            pair = list(pending_confirm.keys())[0]
            pending_confirm.pop(pair)
            add_blacklist(pair, 1)  # skip 1 jam
            send_telegram(f"✅ *{pair_labels.get(pair, pair)} dilewati* — skip 1 jam.")
        else:
            send_telegram("Tidak ada aksi yang perlu dibatalkan.")

    elif text == "/resetposisi":
        send_telegram("🔄 *Reset posisi dimulai...*\nMengambil harga beli dari riwayat Indodax!")
        open_positions = {}
        # Hapus file lokal
        try:
            if os.path.exists(POSITIONS_FILE):
                os.remove(POSITIONS_FILE)
        except: pass
        # Restore ulang dari wallet + tradeHistory
        fetch_prices()
        restore_from_wallet()
        if open_positions:
            send_telegram(f"✅ *Reset selesai!* {len(open_positions)} posisi restored dengan harga beli akurat!")
        else:
            send_telegram("⚠️ Tidak ada posisi yang bisa di-restore dari wallet!")

    elif text.startswith("/restore "):
        parts = text.split()
        if len(parts) < 3:
            send_telegram(
                "Format: /restore COIN TOTAL\\_IDR\n"
                "Contoh: /restore hype 21210\n"
                "(input total IDR yang dipakai beli, bukan harga per coin)"
            )
            return
        coin = parts[1].strip()
        pair = coin + "_idr"
        try:
            total_idr = float(parts[2].replace(".", "").replace(",", ""))
        except:
            send_telegram("❌ Total IDR tidak valid!")
            return
        label = pair_labels.get(pair, f"{coin.upper()}/IDR")
        # Ambil qty dari wallet
        qty, coin_key = get_coin_balance(coin)
        if qty <= 0:
            send_telegram(f"❌ Saldo {coin.upper()} = 0 di wallet!")
            return
        # Hitung harga per coin dari total IDR
        buy_price = total_idr / qty
        curr      = prices.get(pair, 0)
        if curr <= 0:
            try:
                r = requests.get(f"https://indodax.com/api/ticker/{pair}", timeout=10)
                curr = float(r.json().get("ticker", {}).get("last", 0))
            except: curr = buy_price
        open_positions[pair] = {
            "buy_price":  buy_price,
            "qty":        qty,
            "idr":        total_idr,
            "peak":       max(buy_price, curr),
            "entry_time": time.time(),
        }
        save_positions()
        pl_pct = (curr - buy_price) / buy_price * 100  # display tanpa fee
        emoji  = "🟢" if pl_pct >= 0 else "🔴"
        send_telegram(
            f"✅ *{label} berhasil di-restore!*\n"
            f"Qty: {qty:.6f}\n"
            f"Total beli: {fmt(total_idr)}\n"
            f"Harga per coin: {fmt(buy_price)}\n"
            f"Harga sekarang: {fmt(curr)}\n"
            f"{emoji} P/L: {pl_pct:.1f}%\n"
            f"TP: +{TP_PCT}% | Time Stop: {TIME_STOP_HR}j"
        )

    elif text.startswith("/skip "):
        coin = text[6:].strip()
        pair = coin + "_idr"
        label = pair_labels.get(pair, f"{coin.upper()}/IDR")
        # Hapus dari posisi tanpa jual
        if pair in open_positions:
            del open_positions[pair]
            save_positions()
        # Blacklist permanent (99 jam)
        add_blacklist(pair, 99)
        send_telegram(
            f"⏭️ *{label} di-SKIP!*\n"
            f"Dihapus dari posisi tanpa jual.\n"
            f"Blacklist 99 jam — bot tidak akan beli lagi."
        )

    else:
        send_telegram(
            f"❓ Command tidak dikenal.\n\n"
            f"*Command:*\n"
            f"/status — cek status\n"
            f"/beli COIN — beli manual\n"
            f"/jual COIN — jual manual\n"
            f"/pause — pause bot\n"
            f"/resume — aktifkan bot\n"
            f"/skip COIN — skip dust coin tanpa jual\n"
            f"/batal atau /cancel — cancel aksi"
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

# ── Main ─────────────────────────────────────────────────
def main():
    log("🚀 IndoBot v9 dimulai...")
    fetch_all_pairs()
    fetch_prices()
    get_idr_balance()
    load_positions()
    restore_from_wallet()

    send_telegram(
        f"🚀 *IndoBot v9 AKTIF*\n"
        f"Modal: {fmt(modal)}\n"
        f"TP: {TP_PCT}% | Trail: {TRAIL_PCT}%\n"
        f"Time Stop: {TIME_STOP_HR} jam\n"
        f"Blacklist: {BLACKLIST_HR} jam\n"
        f"Max Posisi: {MAX_TRADES}\n"
        f"Scan: {TOP_N_PAIRS} pair tiap {SCAN_INTERVAL}s\n"
        f"Min Sinyal: {MIN_SIGNALS}/4\n"
        f"Strategi: Swing (beli saat oversold & dekat LOW)\n"
        f"Mode: Semi-Auto — konfirmasi via Telegram! 🔄"
    )

    tick = 0
    while True:
        try:
            check_tg_commands()
            cleanup_expired_pending()
            if tick % 3 == 0:
                fetch_prices()
            if not bot_paused:
                manage_positions()
                scan_candidates()
            check_position_report()
            check_daily_report()
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
