"""
main.py — Точка входа. Запускает бэктест, затем обе стратегии параллельно.

  • Стратегия 1 (S1): Фибоначчи 0.382 / 0.5 / 0.618 / 0.786, тайм-фрейм 15m
  • Стратегия 2 (S2): Зона 0.618–0.786, тейк 0.5, тайм-фрейм 1m (тренд по 15m)

Запуск: python main.py
"""

import time
import json
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pybit.unified_trading import HTTP

from backtest import run_backtest, SYMBOLS, SWING_BARS, ENTRY_LEVELS, \
    TP_LEVELS, STOP_BUFFER, VOLUME_MULT, CANDLES, \
    find_swing, calc_fib, check_touch, vol_ok, candle_ok

from strategy2 import run_strategy2, backtest_s2, s2_stats

from telegram_notify import (
    notify_start, notify_trade_open, notify_daily_stop, notify_backtest, send
)

# ─────────────────────────────────────────
# НАСТРОЙКИ — РЕДАКТИРУЙ ЗДЕСЬ
# ─────────────────────────────────────────
API_KEY    = "a7deGa5rP1fU6tQcaf"
API_SECRET = "IogtMB82qHTfdQs7Gs79LSlNzoZDGq0ts2iz"
TESTNET    = False  # ← False только после успешного тестнета

LEVERAGE          = 10
RISK_PCT          = 2.0
LOOP_SLEEP        = 30      # секунд между проверками (S1)
MAX_CONSEC_LOSSES = 5
MAX_WORKERS       = 4       # параллельных потоков S1
LEVERAGE_SET_DELAY = 0.3    # секунд между set_leverage

RUN_BACKTEST_FIRST   = True
MIN_BACKTEST_WINRATE = 0.0  # если ниже, бот не стартует
RUN_STRATEGY2        = True # запускать стратегию 2 параллельно
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


# ═══════════════════════════════════════════════════════════════
#  СТАТИСТИКА СТРАТЕГИИ 1
# ═══════════════════════════════════════════════════════════════

class Strategy1Stats:
    """Потокобезопасная статистика для стратегии 1."""

    def __init__(self):
        self._lock   = threading.Lock()
        self.trades  = 0
        self.wins    = 0
        self.losses  = 0
        self.pnl     = 0.0
        self.log     = []

    def record(self, symbol: str, side: str, entry: float,
               stop: float, tp: float, outcome: str, pnl: float):
        with self._lock:
            self.trades += 1
            self.pnl    += pnl
            if outcome == "win":
                self.wins += 1
            else:
                self.losses += 1
            self.log.append({
                "ts":      datetime.utcnow().isoformat(),
                "symbol":  symbol,
                "side":    side,
                "entry":   round(entry, 4),
                "stop":    round(stop,  4),
                "tp":      round(tp,    4),
                "outcome": outcome,
                "pnl":     round(pnl,  2),
            })

    def summary(self) -> dict:
        with self._lock:
            total = self.trades
            wr    = round(self.wins / total * 100, 1) if total else 0.0
            return {
                "strategy":  "S1_fib_0382_0786",
                "trades":    total,
                "wins":      self.wins,
                "losses":    self.losses,
                "winrate":   wr,
                "total_pnl": round(self.pnl, 2),
            }


s1_stats = Strategy1Stats()


def print_combined_stats():
    """Вывести сводную статистику обеих стратегий в лог."""
    s1 = s1_stats.summary()
    s2 = s2_stats.summary()
    total_trades = s1["trades"] + s2["trades"]
    total_pnl    = s1["total_pnl"] + s2["total_pnl"]

    log.info("=" * 55)
    log.info("  📊 СВОДНАЯ СТАТИСТИКА")
    log.info(f"  S1 | Сделок: {s1['trades']} | WR: {s1['winrate']}% | PnL: ${s1['total_pnl']:+.2f}")
    log.info(f"  S2 | Сделок: {s2['trades']} | WR: {s2['winrate']}% | PnL: ${s2['total_pnl']:+.2f}")
    log.info(f"  Итого | Сделок: {total_trades} | PnL: ${total_pnl:+.2f}")
    log.info("=" * 55)

    # Отправить в Telegram раз в час
    try:
        send(
            f"📊 *Статистика бота*\n"
            f"S1: {s1['trades']} сделок | WR {s1['winrate']}% | PnL ${s1['total_pnl']:+.2f}\n"
            f"S2: {s2['trades']} сделок | WR {s2['winrate']}% | PnL ${s2['total_pnl']:+.2f}\n"
            f"Итого PnL: ${total_pnl:+.2f}"
        )
    except Exception:
        pass


def save_combined_stats():
    """Сохранить объединённую статистику обеих стратегий в JSON."""
    data = {
        "saved_at":  datetime.utcnow().isoformat(),
        "strategy1": s1_stats.summary(),
        "strategy2": s2_stats.summary(),
        "trade_log_s1": s1_stats.log[-50:],   # последние 50 сделок
        "trade_log_s2": s2_stats.log[-50:],
    }
    with open("live_stats.json", "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  ФИЛЬТРАЦИЯ СИМВОЛОВ
# ═══════════════════════════════════════════════════════════════

def get_available_symbols() -> list[str]:
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
        log.error(f"Не удалось получить список инструментов: {e}. Используем весь SYMBOLS.")
        return list(SYMBOLS)


# ═══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════

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
        if "110043" not in str(e):
            log.warning(f"{symbol} | Плечо: {e}")


def set_leverage_throttled(symbols: list[str]):
    log.info(f"Выставляю плечо x{LEVERAGE} для {len(symbols)} монет "
             f"(задержка {LEVERAGE_SET_DELAY}с)...")
    for sym in symbols:
        set_leverage(sym)
        time.sleep(LEVERAGE_SET_DELAY)
    log.info("Плечо выставлено ✅")


def calc_qty(balance, entry, stop):
    risk_usdt = balance * (RISK_PCT / 100)
    stop_dist = abs(entry - stop)
    stop_pct  = stop_dist / entry
    qty_usdt  = risk_usdt / stop_pct
    return round(qty_usdt / entry, 3)


def open_trade(symbol, side, entry, stop, tp_prices, balance):
    """Открыть сделку. Используется обеими стратегиями."""
    qty = calc_qty(balance, entry, stop)
    if qty <= 0:
        return
    bybit_side = "Buy" if side == "long" else "Sell"
    sl = round(stop, 4)
    tp = round(tp_prices[0], 4)
    log.info(f"{symbol} | {bybit_side} qty={qty} entry≈{entry:.4f} SL={sl} TP={tp}")
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


# ═══════════════════════════════════════════════════════════════
#  СОСТОЯНИЕ БОТА (ОБЩЕЕ ДЛЯ ОБЕИХ СТРАТЕГИЙ)
# ═══════════════════════════════════════════════════════════════

class State:
    """
    Общий объект состояния: используется обеими стратегиями.
    Потокобезопасен через threading.Lock там, где нужно.
    """
    def __init__(self, symbols: list[str]):
        self.symbols:  list[str]       = symbols
        self.consec:   dict[str, int]  = {s: 0 for s in symbols}
        self.stopped:  set[str]        = set()
        self.last_day: dict[str, str]  = {}
        self._lock = threading.Lock()

    def reset_daily(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with self._lock:
            for sym in self.symbols:
                if self.last_day.get(sym) != today:
                    self.stopped.discard(sym)
                    self.consec[sym] = 0
                    self.last_day[sym] = today

    def add_loss(self, symbol: str):
        with self._lock:
            self.consec[symbol] = self.consec.get(symbol, 0) + 1
            if self.consec[symbol] >= MAX_CONSEC_LOSSES:
                self.stopped.add(symbol)
                log.warning(f"{symbol} | {MAX_CONSEC_LOSSES} подряд убытков — стоп до завтра")
                notify_daily_stop(symbol)

    def add_win(self, symbol: str):
        with self._lock:
            self.consec[symbol] = 0


# ═══════════════════════════════════════════════════════════════
#  ОСНОВНОЙ ЦИКЛ — СТРАТЕГИЯ 1
# ═══════════════════════════════════════════════════════════════

def scan_symbol(symbol, state, balance):
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


def _run_s1_loop(symbols: list[str], state: State,
                 stop_event: threading.Event):
    """Основной цикл стратегии 1 — выполняется в отдельном потоке."""
    log.info(f"[S1] Стратегия 1 запущена | ENTRY: {ENTRY_LEVELS} | TF: 15m | Монет: {len(symbols)}")

    # Счётчик для периодической печати статистики
    stat_counter = 0

    while not stop_event.is_set():
        try:
            state.reset_daily()
            balance = get_balance()
            active  = [s for s in symbols if s not in state.stopped]

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
                        log.error(f"{sym} | [S1] Поток упал: {e}")

            # Каждые 60 итераций (~30 мин) — печатаем статистику
            stat_counter += 1
            if stat_counter % 60 == 0:
                print_combined_stats()
                save_combined_stats()

        except Exception as e:
            log.error(f"[S1] Ошибка цикла: {e}")
            send(f"🚨 [S1] Ошибка: {e}")

        for _ in range(LOOP_SLEEP):
            if stop_event.is_set():
                break
            time.sleep(1)

    log.info("[S1] Стратегия 1 остановлена.")


# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК ОБЕИХ СТРАТЕГИЙ
# ═══════════════════════════════════════════════════════════════

def run_bot(symbols: list[str]):
    state = State(symbols)

    # Плечо выставляем один раз для всех монет
    set_leverage_throttled(symbols)

    balance = get_balance()
    notify_start(symbols, balance)
    log.info(
        f"Баланс: ${balance:.2f} | Монет: {len(symbols)} | "
        f"S1-интервал: {LOOP_SLEEP}с | Стратегия 2: {'ВКЛ' if RUN_STRATEGY2 else 'ВЫКЛ'}"
    )

    stop_event = threading.Event()

    # ── Поток стратегии 1 ────────────────────────────────────────────────────
    t1 = threading.Thread(
        target=_run_s1_loop,
        args=(symbols, state, stop_event),
        name="Strategy1",
        daemon=True
    )
    t1.start()
    log.info("[S1] Поток запущен.")

    # ── Поток стратегии 2 ────────────────────────────────────────────────────
    t2 = None
    if RUN_STRATEGY2:
        t2 = threading.Thread(
            target=run_strategy2,
            kwargs={
                "session":       session,
                "state":         state,          # ← ОБЩИЙ state
                "get_balance_fn": get_balance,
                "notify_fn":     notify_trade_open,
                "open_trade_fn": open_trade,     # ← та же функция открытия
                "symbols":       symbols,
                "stop_event":    stop_event,
            },
            name="Strategy2",
            daemon=True
        )
        t2.start()
        log.info("[S2] Поток запущен.")

    # ── Главный поток: ждём Ctrl-C ───────────────────────────────────────────
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Получен Ctrl-C, останавливаю стратегии...")
        stop_event.set()

    t1.join(timeout=10)
    if t2:
        t2.join(timeout=10)

    print_combined_stats()
    save_combined_stats()

    send("🛑 Бот остановлен вручную")
    log.info("Бот остановлен. Финальная статистика сохранена в live_stats.json")


# ═══════════════════════════════════════════════════════════════
#  ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("=" * 55)
    log.info("  FIBONACCI BOT — BYBIT FUTURES")
    log.info(f"  Тестнет: {TESTNET} | Монет: {len(SYMBOLS)}")
    log.info(f"  Стратегия 2: {'ВКЛ' if RUN_STRATEGY2 else 'ВЫКЛ'}")
    log.info("=" * 55)

    active_symbols = get_available_symbols()

    if not active_symbols:
        log.error("Нет доступных символов — бот не запущен.")
        send("🚨 Нет доступных символов — бот не запущен.")

    elif RUN_BACKTEST_FIRST:
        log.info("Запускаю бэктест S1...")
        results_s1 = run_backtest(session)
        notify_backtest(results_s1)

        log.info("Запускаю бэктест S2...")
        results_s2 = backtest_s2(session, symbols=active_symbols)

        # Сохраняем оба бэктеста
        with open("backtest_results.json", "w") as f:
            json.dump({"s1": results_s1, "s2": results_s2}, f,
                      indent=2, ensure_ascii=False)
        log.info("Результаты бэктестов сохранены в backtest_results.json")

        # Проверка порога
        s1_ok = all(r["winrate"] >= MIN_BACKTEST_WINRATE for r in results_s1.values())
        s2_ok = all(r["winrate"] >= MIN_BACKTEST_WINRATE for r in results_s2.values())

        if not (s1_ok and s2_ok):
            failed = []
            if not s1_ok: failed.append("S1")
            if not s2_ok: failed.append("S2")
            log.warning(
                f"Бэктест {'/'.join(failed)} не прошёл порог "
                f"{MIN_BACKTEST_WINRATE}%. Бот не запущен."
            )
            send(f"⚠️ Бэктест {'/'.join(failed)} ниже порога — бот не запущен.")
        else:
            log.info("Оба бэктеста пройдены ✅ Запускаю бота...")
            run_bot(active_symbols)

    else:
        run_bot(active_symbols)
