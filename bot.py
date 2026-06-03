import os, time, threading, requests, psycopg2, psycopg2.extras, pytz, logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ── CONFIG ────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
QT_ACCOUNT   = os.environ.get("QUESTRADE_ACCOUNT", "29904191")
PORT         = int(os.environ.get("PORT", 8080))
BOT_MODE     = os.environ.get("BOT_MODE", "paper")

WATCHLIST = ["AAPL","TSLA","NVDA","MSFT","AMZN","META","AMD","SOFI","PLTR","SPY"]

# ── DATABASE ──────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id           SERIAL PRIMARY KEY,
                symbol       VARCHAR(20),
                action       VARCHAR(10),
                shares       FLOAT,
                entry_price  FLOAT,
                exit_price   FLOAT,
                stop_loss    FLOAT,
                pnl          FLOAT DEFAULT 0,
                status       VARCHAR(20) DEFAULT 'OPEN',
                mode         VARCHAR(10) DEFAULT 'paper',
                created_at   TIMESTAMP DEFAULT NOW(),
                closed_at    TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bot_logs (
                id         SERIAL PRIMARY KEY,
                message    TEXT,
                log_type   VARCHAR(20) DEFAULT 'info',
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id           SERIAL PRIMARY KEY,
                cash         FLOAT,
                market_value FLOAT,
                total_equity FLOAT,
                mode         VARCHAR(10),
                created_at   TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        db_log("✅ Database ready", "success")
        logger.info("✅ DB initialized")
    except Exception as e:
        logger.error(f"❌ DB init error: {e}")

def db_log(message, log_type="info"):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO bot_logs (message, log_type) VALUES (%s, %s)", (message, log_type))
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"[{log_type.upper()}] {message}")
    except Exception as e:
        logger.error(f"Log DB error: {e}")

# ── QUESTRADE AUTH ────────────────────────────────────────
qt = {"access_token": None, "api_server": None, "expiry": 0}

def qt_login():
    # Always read fresh from environment — fixes token update issue
    token = os.environ.get("QUESTRADE_REFRESH_TOKEN", "")
    if not token:
        db_log("⚠️ No QUESTRADE_REFRESH_TOKEN set", "warn")
        return False
    try:
        res = requests.post(
            "https://login.questrade.com/oauth2/token",
            params={"grant_type": "refresh_token", "refresh_token": token},
            timeout=15
        )
        data = res.json()
        if "access_token" not in data:
            raise Exception(str(data))
        qt["access_token"] = data["access_token"]
        qt["api_server"]   = data["api_server"]
        qt["expiry"]       = time.time() + data.get("expires_in", 1800) - 120
        db_log("✅ Questrade connected", "success")
        return True
    except Exception as e:
        db_log(f"❌ Questrade login failed: {e}", "error")
        return False

def qt_headers():
    if time.time() >= qt["expiry"]:
        qt_login()
    return {"Authorization": f"Bearer {qt['access_token']}"}

def qt_get(path):
    r = requests.get(qt["api_server"] + "v1/" + path, headers=qt_headers(), timeout=10)
    return r.json()

def qt_post(path, payload):
    r = requests.post(qt["api_server"] + "v1/" + path, headers=qt_headers(), json=payload, timeout=10)
    return r.json()

# ── QUESTRADE ACTIONS ─────────────────────────────────────
def get_qt_balance():
    try:
        data = qt_get(f"accounts/{QT_ACCOUNT}/balances")
        balances = data.get("combinedBalances", [])
        cad = next((b for b in balances if b.get("currency") == "CAD"), {})
        return {
            "cash": round(cad.get("cash", 0), 2),
            "market_value": round(cad.get("marketValue", 0), 2),
            "total_equity": round(cad.get("totalEquity", 0), 2),
        }
    except Exception as e:
        logger.error(f"Balance error: {e}")
        return {"cash": 0, "market_value": 0, "total_equity": 0}

def get_qt_positions():
    try:
        return qt_get(f"accounts/{QT_ACCOUNT}/positions").get("positions", [])
    except:
        return []

def get_qt_orders():
    try:
        return qt_get(f"accounts/{QT_ACCOUNT}/orders?stateFilter=Open").get("orders", [])
    except:
        return []

def place_qt_order(symbol, action, quantity):
    try:
        search = qt_get(f"symbols/search?prefix={symbol}")
        syms = search.get("symbols", [])
        if not syms:
            raise Exception(f"Symbol {symbol} not found")
        symbol_id = syms[0]["symbolId"]
        payload = {
            "accountNumber": QT_ACCOUNT,
            "symbolId": symbol_id,
            "quantity": quantity,
            "action": action,
            "orderType": "Market",
            "timeInForce": "Day",
            "primaryRoute": "AUTO",
            "secondaryRoute": "AUTO",
        }
        result = qt_post(f"accounts/{QT_ACCOUNT}/orders", payload)
        return result
    except Exception as e:
        db_log(f"❌ Order error: {e}", "error")
        return {"error": str(e)}

# ── LIVE PRICES (FINNHUB) ─────────────────────────────────
def get_prices():
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")
    if not finnhub_key:
        db_log("⚠️ No FINNHUB_API_KEY set", "warn")
        return {}
    result = {}
    for sym in WATCHLIST[:8]:
        try:
            url = f"https://finnhub.io/api/v1/quote?symbol={sym}&token={finnhub_key}"
            res = requests.get(url, timeout=8)
            data = res.json()
            price = data.get("c", 0)
            prev  = data.get("pc", price)
            if price and price > 0:
                change = round(((price - prev) / prev * 100) if prev else 0, 2)
                result[sym] = {"price": price, "change": change}
                logger.info(f"📈 {sym}: ${price} ({change}%)")
        except Exception as e:
            logger.error(f"Price error for {sym}: {e}")
            continue
    logger.info(f"Prices fetched: {len(result)} symbols")
    return result

# ── MARKET HOURS ──────────────────────────────────────────
def market_status():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False, "Weekend — market closed"
    total = now.hour * 60 + now.minute
    if total < 570:  return False, "Pre-market (opens 9:30 AM ET)"
    if total < 600:  return False, "Avoiding open volatility (first 30 min)"
    if total >= 930: return False, "Avoiding close volatility (last 30 min)"
    if total >= 960: return False, "Market closed for today"
    return True, f"Market open ({now.strftime('%I:%M %p')} ET)"

# ── POSITION SIZING ───────────────────────────────────────
def position_size(balance, entry, stop):
    risk_dollar    = balance * 0.02
    risk_per_share = entry - stop
    if risk_per_share <= 0: return 1
    shares     = int(risk_dollar / risk_per_share)
    max_shares = int((balance * 0.15) / entry)
    return max(1, min(shares, max_shares))

# ── TRADE LOGIC ───────────────────────────────────────────
def find_signal(prices, positions):
    open_syms = {p["symbol"] for p in positions}
    movers = sorted(
        [(sym, d) for sym, d in prices.items() if d["price"] > 0],
        key=lambda x: abs(x[1]["change"]), reverse=True
    )
    for sym, d in movers:
        if sym in open_syms: continue
        if d["change"] < 0.5: break
        price = d["price"]
        return {"symbol": sym, "price": price, "stop": round(price * 0.982, 2), "change": d["change"]}
    return None

# ── BOT STATE ─────────────────────────────────────────────
bot_running = False
bot_thread  = None

def save_trade(symbol, action, shares, entry, stop, mode):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO trades (symbol,action,shares,entry_price,stop_loss,mode) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (symbol, action, shares, entry, stop, mode)
        )
        trade_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return trade_id
    except Exception as e:
        logger.error(f"Save trade error: {e}")
        return None

def check_stops(prices):
    # Fixed: only takes prices argument
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM trades WHERE status='OPEN'")
        for t in cur.fetchall():
            sym = t["symbol"]
            if sym not in prices: continue
            current = prices[sym]["price"]
            if current <= t["stop_loss"]:
                pnl = round((current - t["entry_price"]) * t["shares"], 2)
                cur.execute(
                    "UPDATE trades SET exit_price=%s,pnl=%s,status='CLOSED',closed_at=NOW() WHERE id=%s",
                    (current, pnl, t["id"])
                )
                conn.commit()
                db_log(f"🛑 STOP LOSS: {sym} @ ${current:.2f} | P&L: ${pnl}", "error")
        cur.close(); conn.close()
    except Exception as e:
        logger.error(f"Stop check error: {e}")

def bot_cycle():
    db_log("🔍 Scanning market...", "info")
    open_ok, reason = market_status()
    if not open_ok:
        db_log(f"🕐 {reason}", "warn")
        return

    prices = get_prices()
    if not prices:
        db_log("⚠️ No price data — check FINNHUB_API_KEY", "warn")
        return

    balance_data = get_qt_balance()
    positions    = get_qt_positions()

    # Save balance snapshot
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO account_snapshots (cash,market_value,total_equity,mode) VALUES (%s,%s,%s,%s)",
            (balance_data["cash"], balance_data["market_value"], balance_data["total_equity"], BOT_MODE)
        )
        conn.commit(); cur.close(); conn.close()
    except: pass

    check_stops(prices)  # Fixed: only pass prices

    signal = find_signal(prices, positions)
    if not signal:
        db_log("⏸ No signal — holding", "info")
        return

    sym    = signal["symbol"]
    price  = signal["price"]
    stop   = signal["stop"]
    change = signal["change"]
    balance = balance_data.get("cash", 500)
    shares  = position_size(balance, price, stop)

    if shares * price > balance * 0.95:
        db_log(f"⚠️ Insufficient funds for {sym}", "warn")
        return

    db_log(f"📡 Signal: BUY {shares}x {sym} @ ${price:.2f} | +{change:.1f}% | Stop: ${stop}", "info")

    if BOT_MODE == "live":
        result = place_qt_order(sym, "Buy", shares)
        if "error" not in result:
            save_trade(sym, "BUY", shares, price, stop, "live")
            db_log(f"✅ LIVE ORDER: BUY {shares}x {sym} @ ${price:.2f}", "trade")
        else:
            db_log(f"❌ Order failed: {result['error']}", "error")
    else:
        save_trade(sym, "BUY", shares, price, stop, "paper")
        db_log(f"📝 PAPER: BUY {shares}x {sym} @ ${price:.2f}", "trade")

def bot_loop():
    global bot_running
    last_qt_login = 0
    while bot_running:
        try:
            # Re-login every 25 min — reads fresh token from env each time
            if time.time() - last_qt_login > 25 * 60:
                qt_login()
                last_qt_login = time.time()
            bot_cycle()
        except Exception as e:
            db_log(f"❌ Cycle error: {e}", "error")
        time.sleep(60)

# ── API ROUTES ────────────────────────────────────────────
@app.route("/")
def root():
    return jsonify({"status": "TradeX AI running ✅", "mode": BOT_MODE})

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/api/stats")
def api_stats():
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 50")
        trades = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM bot_logs ORDER BY created_at DESC LIMIT 40")
        logs = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM account_snapshots ORDER BY created_at DESC LIMIT 1")
        snap = cur.fetchone()
        cur.close(); conn.close()
        closed    = [t for t in trades if t["status"] == "CLOSED"]
        wins      = [t for t in closed if (t["pnl"] or 0) > 0]
        total_pnl = round(sum(t["pnl"] or 0 for t in closed), 2)
        return jsonify({
            "trades": trades, "logs": logs,
            "snapshot": dict(snap) if snap else None,
            "bot_running": bot_running, "bot_mode": BOT_MODE,
            "stats": {
                "total": len(closed), "wins": len(wins),
                "losses": len(closed)-len(wins),
                "total_pnl": total_pnl,
                "win_rate": round(len(wins)/max(len(closed),1)*100)
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/balance")
def api_balance():
    return jsonify(get_qt_balance())

@app.route("/api/positions")
def api_positions():
    return jsonify({"positions": get_qt_positions()})

@app.route("/api/orders")
def api_orders():
    return jsonify({"orders": get_qt_orders()})

@app.route("/api/prices")
def api_prices():
    return jsonify({"prices": get_prices()})

@app.route("/api/market")
def api_market():
    open_ok, reason = market_status()
    return jsonify({"open": open_ok, "reason": reason})

@app.route("/api/bot/start", methods=["POST"])
def api_start():
    global bot_running, bot_thread
    if bot_running:
        return jsonify({"status": "already running"})
    bot_running = True
    bot_thread  = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    db_log(f"🚀 Bot started — {BOT_MODE.upper()} mode", "success")
    return jsonify({"status": "started", "mode": BOT_MODE})

@app.route("/api/bot/stop", methods=["POST"])
def api_stop():
    global bot_running
    bot_running = False
    db_log("🛑 Bot stopped by user", "warn")
    return jsonify({"status": "stopped"})

@app.route("/api/bot/status")
def api_bot_status():
    open_ok, reason = market_status()
    return jsonify({"running": bot_running, "mode": BOT_MODE,
                    "market_open": open_ok, "market_reason": reason})

@app.route("/api/trade/close", methods=["POST"])
def api_close_trade():
    body       = request.json or {}
    trade_id   = body.get("trade_id")
    exit_price = body.get("exit_price")
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM trades WHERE id=%s", (trade_id,))
        t = cur.fetchone()
        if not t: return jsonify({"error": "Not found"}), 404
        pnl = round((exit_price - t["entry_price"]) * t["shares"], 2)
        cur.execute(
            "UPDATE trades SET exit_price=%s,pnl=%s,status='CLOSED',closed_at=NOW() WHERE id=%s",
            (exit_price, pnl, trade_id)
        )
        conn.commit(); cur.close(); conn.close()
        db_log(f"📤 Closed #{trade_id} {t['symbol']} @ ${exit_price} | P&L: ${pnl}", "trade")
        return jsonify({"pnl": pnl})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── STARTUP ───────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("🚀 TradeX AI starting...")
    try:
        init_db()
    except Exception as e:
        logger.error(f"DB startup error: {e}")
    try:
        qt_login()
    except Exception as e:
        logger.error(f"QT startup error: {e}")
    bot_running = True
    bot_thread  = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    logger.info(f"🌐 API server on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
