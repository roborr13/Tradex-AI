import os, time, threading, requests, psycopg2, psycopg2.extras, pytz, logging
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DATABASE_URL      = os.environ.get("DATABASE_URL", "")
QT_REFRESH_TOKEN  = os.environ.get("QUESTRADE_REFRESH_TOKEN", "")
QT_ACCOUNT        = os.environ.get("QUESTRADE_ACCOUNT", "29904191")
POLYGON_KEY       = os.environ.get("POLYGON_API_KEY", "")
PORT              = int(os.environ.get("PORT", 8080))
BOT_MODE          = os.environ.get("BOT_MODE", "paper")

WATCHLIST = ["AAPL","TSLA","NVDA","MSFT","AMZN","META","AMD","SOFI","PLTR","SPY"]

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY, symbol VARCHAR(20), action VARCHAR(10),
                shares FLOAT, entry_price FLOAT, exit_price FLOAT, stop_loss FLOAT,
                pnl FLOAT DEFAULT 0, status VARCHAR(20) DEFAULT 'OPEN',
                mode VARCHAR(10) DEFAULT 'paper', created_at TIMESTAMP DEFAULT NOW(), closed_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bot_logs (
                id SERIAL PRIMARY KEY, message TEXT, log_type VARCHAR(20) DEFAULT 'info', created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id SERIAL PRIMARY KEY, cash FLOAT, market_value FLOAT, total_equity FLOAT, mode VARCHAR(10), created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit(); cur.close(); conn.close()
        db_log("✅ Database ready", "success")
    except Exception as e:
        logger.error(f"❌ DB init error: {e}")

def db_log(message, log_type="info"):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO bot_logs (message, log_type) VALUES (%s, %s)", (message, log_type))
        conn.commit(); cur.close(); conn.close()
        logger.info(f"[{log_type.upper()}] {message}")
    except Exception as e:
        logger.error(f"Log error: {e}")

qt = {"access_token": None, "api_server": None, "expiry": 0}

def qt_login():
    try:
        res = requests.post("https://login.questrade.com/oauth2/token",
            params={"grant_type": "refresh_token", "refresh_token": QT_REFRESH_TOKEN}, timeout=15)
        data = res.json()
        if "access_token" not in data:
