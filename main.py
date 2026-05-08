"""
main.py — Точка входа. Запускает бэктест, затем торгового бота.
Запуск: python main.py
"""

import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pybit.unified_trading import HTTP

from backtest import run_backtest, SYMBOLS, SWING_BARS, ENTRY_LEVELS, \
    TP_LEVELS, STOP_BUFFER, VOLUME_MULT, CANDLES, \
    find_swing, calc_fib, check_touch, vol_ok, candle_ok
from telegram_notify import (
    notify_start, notify_trade_open, notify_daily_stop, notify_backtest, send
)

# ─────────────────────────────────────────
# НАСТРОЙКИ — РЕДАКТИРУЙ ЗДЕСЬ
# ─────────────────────────────────────────
API_KEY    = "a7deGa5rP1fU6tQcaf"
API_SECRET = "IogtMB82qHTfdQs7Gs79LSlNzoZDGq0ts2iz"
TESTNET    = False # ← False только после успешного тестнета

LEVERAGE          = 10
RISK_PCT          = 2.0
LOOP_SLEEP        = 30     # секунд между проверками
MAX_CONSEC_LOSSES = 5
MAX_WORKERS       = 4      # параллельных потоков (снижено чтобы не бить rate limit)

LEVERAGE_SET_DELAY = 0.3   # секунд между запросами set_leverage (rate limit защита)

RUN_BACKTEST_FIRST   = True
MIN_BACKTEST_WINRATE = 0.0  # % — если ниже, бот не стартует
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

if TESTNET:
    session = HTTP(testnet=True, api_key=API_KEY, api_secret=API_SECRET)
else:
    session = HTTP(
        testnet=False,
        demo=True,
        api_key=API_KEY,
        api_secret=API_SECRET
    )


# ── Фильтрация символов под среду ────────

def get_available_symbols() -> list[str]:
    """Вернуть список символов из SYMBOLS, доступных на текущей бирже (testnet / mainnet)."""
    try:
        resp = session.get_instruments_info(category="linear")
        available = {
            item["symbol"]
            for item in resp["result"]["list"]
            if item.get("status", "").upper() == "TRADING"
        }
        log.info(f"Доступно символов на бирже: {len(available)}")
        filtered = [s for s in SYMBOLS if s in available]
        skipped  = [s for s in SYMBOLS if s not in available]
        if skipped:
            log.warning(f"Символы недоступны на {'testnet' if TESTNET else 'mainnet'}, "
                        f"будут пропущены: {', '.join(skipped)}")
        log.info(f"Будет торговаться: {len(filtered)} монет из {len(SYMBOLS)}")
        return filtered
    except Exception as e:
        log.error(f"Не удалось получить список инструментов: {e}. "
                  f"Используем весь список SYMBOLS.")
        return list(SYMBOLS)


# ── Вспомогательные функции ──────────────

def get_klines(symbol, limit=100):
    resp = session.get_kline(
        category="linear", symbol=symbol,
        interval="15", limit=limit
    )
    bars = resp["result"]["list"]
    bars.reverse()
    return [{
        "ts": int(b[0]), "open": float(b[1]), "high": float(b[2]),
        "low": float(b[3]), "close": float(b[4]), "volume": float(b[5])
    } for b in bars]


def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    for c in resp["result"]["list"][0]["coin"]:
        if c["coin"] == "USDT":
            val = c.get("availableToWithdraw") or c.get("walletBalance") or "0"
            return float(val) if val else 0.0
    return 0.0


def get_position(symbol):
    resp = session.get_positions(category="linear", symbol=symbol)
    for p in resp["result"]["list"]:
        if float(p["size"]) > 0:
            return p
    return None


def set_leverage(symbol):
    try:
        session.set_leverage(
            category="linear", symbol=symbol,
            buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE)
        )
    except Exception as e:
        # 110043 = плечо уже выставлено, не страшно
        if "110043" not in str(e):
            log.warning(f"{symbol} | Плечо: {e}")


def set_leverage_throttled(symbols: list[str]):
    """Выставить плечо последовательно с задержкой — избегаем rate limit."""
    log.info(f"Выставляю плечо x{LEVERAGE} для {len(symbols)} монет "
             f"(задержка {LEVERAGE_SET_DELAY}с между запросами)...")
    for sym in symbols:
        set_leverage(sym)
        time.sleep(LEVERAGE_SET_DELAY)
    log.info("Плечо выставлено ✅")


def calc_qty(balance, entry, stop):
    risk_usdt  = balance * (RISK_PCT / 100)
    stop_dist  = abs(entry - stop)
    stop_pct   = stop_dist / entry
    qty_usdt   = risk_usdt / stop_pct
    return round(qty_usdt / entry, 3)


def open_trade(symbol, side, entry, stop, tp_prices, balance):
    qty = calc_qty(balance, entry, stop)
    if qty <= 0:
        return
    bybit_side = "Buy" if side == "long" else "Sell"
    sl = round(stop, 2)
    tp = round(tp_prices[0], 2)
    log.info(f"{symbol} | {bybit_side} qty={qty} entry≈{entry:.2f} SL={sl} TP1={tp}")
    notify_trade_open(symbol, side, entry, sl, tp, qty)
    try:
        session.place_order(
            category="linear", symbol=symbol,
            side=bybit_side, orderType="Market", qty=str(qty),
            stopLoss=str(sl), takeProfit=str(tp),
            slTriggerBy="LastPrice", tpTriggerBy="LastPrice",
            timeInForce="GTC", reduceOnly=False,
        )
    except Exception as e:
        log.error(f"{symbol} | Ошибка ордера: {e}")
        send(f"🚨 {symbol} | Ошибка ордера: {e}")


# ── Состояние бота ───────────────────────

class State:
    def __init__(self, symbols: list[str]):
        self.symbols:  list[str]       = symbols
        self.consec:   dict[str, int]  = {s: 0 for s in symbols}
        self.stopped:  set[str]        = set()
        self.last_day: dict[str, str]  = {}

    def reset_daily(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        for sym in self.symbols:
            if self.last_day.get(sym) != today:
                self.stopped.discard(sym)
                self.consec[sym] = 0
                self.last_day[sym] = today


# ── Сканирование одного символа ──────────

def scan_symbol(symbol, state, balance):
    """Проверить один символ. Вызывается из пула потоков."""
    try:
        if symbol in state.stopped:
            return

        if get_position(symbol):
            log.info(f"{symbol} | Позиция открыта, пропуск")
            return

        bars = get_klines(symbol, limit=SWING_BARS + 10)
        if len(bars) < SWING_BARS:
            return

        price = bars[-1]["close"]
        sh, sl, trend = find_swing(bars[-SWING_BARS:])
        fibs = calc_fib(sh, sl, trend)

        touched = None
        for fib_r in ENTRY_LEVELS:
            if check_touch(price, fibs[fib_r]):
                touched = fib_r
                break

        if touched is None:
            return
        if not vol_ok(bars):
            return
        if not candle_ok(bars[-1], trend):
            return

        entry = price
        stop  = fibs[touched] * (1 - STOP_BUFFER) if trend == "bull" \
                else fibs[touched] * (1 + STOP_BUFFER)
        tps   = [fibs[r] for r in TP_LEVELS if r in fibs and fibs[r] > 0]
        side  = "long" if trend == "bull" else "short"

        if tps:
            open_trade(symbol, side, entry, stop, tps, balance)

    except Exception as e:
        log.error(f"{symbol} | Ошибка сканирования: {e}")


# ── Основной цикл ────────────────────────

def run_bot(symbols: list[str]):
    state = State(symbols)

    # Выставляем плечо последовательно с задержкой (без rate limit)
    set_leverage_throttled(symbols)

    balance = get_balance()
    notify_start(symbols, balance)
    log.info(f"Баланс: ${balance:.2f} | Монеты: {len(symbols)} | Интервал: {LOOP_SLEEP}с")

    while True:
        try:
            state.reset_daily()
            balance = get_balance()

            active = [s for s in symbols if s not in state.stopped]

            # Параллельное сканирование доступных монет
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                futures = {
                    pool.submit(scan_symbol, sym, state, balance): sym
                    for sym in active
                }
                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        log.error(f"{sym} | Поток упал: {e}")

        except KeyboardInterrupt:
            send("🛑 Бот остановлен вручную")
            log.info("Бот остановлен.")
            break
        except Exception as e:
            log.error(f"Ошибка цикла: {e}")
            send(f"🚨 Ошибка: {e}")

        time.sleep(LOOP_SLEEP)


# ── Точка входа ──────────────────────────

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  FIBONACCI BOT — BYBIT FUTURES")
    log.info(f"  Тестнет: {TESTNET} | Монеты: {len(SYMBOLS)}")
    log.info("=" * 50)

    # Фильтруем символы под текущую среду (testnet / mainnet)
    active_symbols = get_available_symbols()

    if not active_symbols:
        log.error("Нет доступных символов — бот не запущен.")
        send("🚨 Нет доступных символов — бот не запущен.")
    elif RUN_BACKTEST_FIRST:
        log.info("Запускаю бэктест перед стартом бота...")
        results = run_backtest(session)
        notify_backtest(results)

        ok = all(r["winrate"] >= MIN_BACKTEST_WINRATE for r in results.values())
        if not ok:
            log.warning(
                f"Бэктест не прошёл порог {MIN_BACKTEST_WINRATE}% winrate. "
                f"Бот не запущен."
            )
            send(f"⚠️ Бэктест ниже порога {MIN_BACKTEST_WINRATE}% — бот не запущен.")
        else:
            log.info("Бэктест пройден ✅ Запускаю бота...")
            run_bot(active_symbols)
    else:
        run_bot(active_symbols)
