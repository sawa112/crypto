"""
strategy2.py — Параллельная стратегия: вход в зоне Фибоначчи 0.618–0.786
────────────────────────────────────────────────────────────────────────────
Логика:
  • Тайм-фрейм: 1 минута
  • Анализ тренда: 15-минутный swing (импортируется из backtest)
  • Зона входа: цена между уровнями 0.618 и 0.786 Фибоначчи
  • Стоп: выше 0.786 (для шорта — ниже) + буфер
  • Тейк: уровень 0.5 Фибоначчи (середина диапазона)
  • Направление: и Long, и Short (как в основном боте)
  • Фильтры: объём и форма свечи — те же, что в стратегии 1
────────────────────────────────────────────────────────────────────────────
Запуск: импортируется в main.py и запускается параллельно с основным ботом.
"""

import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Импорт общих утилит из основного проекта ────────────────────────────────
from backtest import (
    find_swing, calc_fib, vol_ok, candle_ok,
    SWING_BARS, STOP_BUFFER, SYMBOLS
)

log = logging.getLogger("strategy2")

# ─── Настройки стратегии 2 ───────────────────────────────────────────────────
S2_TIMEFRAME    = "1"          # 1-минутный тайм-фрейм
S2_TREND_TF     = "15"         # тайм-фрейм для определения тренда (swing)
S2_TREND_BARS   = 50           # кол-во баров для swing на 15m
S2_LOOP_SLEEP   = 15           # секунд между итерациями сканирования
S2_MAX_WORKERS  = 4            # параллельных потоков

# Зона входа: цена должна быть МЕЖДУ этими уровнями (не просто касание)
ZONE_LOW_RATIO  = 0.618        # нижняя граница зоны (для bull: ближе к low)
ZONE_HIGH_RATIO = 0.786        # верхняя граница зоны (для bull: чуть выше)

STOP_RATIO      = 0.786        # стоп ставится за этим уровнем
TP_RATIO        = 0.5          # тейк-профит на уровне 0.5 (середина свинга)

ZONE_TOLERANCE  = 0.002        # дополнительный допуск ±0.2% для границ зоны

RISK_PCT        = 2.0          # риск на сделку, % от депозита
LEVERAGE        = 10           # плечо (должно совпадать с main.py)

# ─── Статистика стратегии 2 ──────────────────────────────────────────────────
class Strategy2Stats:
    """Потокобезопасная статистика для стратегии 2."""

    def __init__(self):
        self._lock   = threading.Lock()
        self.trades  = 0
        self.wins    = 0
        self.losses  = 0
        self.pnl     = 0.0
        self.log     = []   # список сделок

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
                "strategy":  "S2_zone_0618_0786",
                "trades":    total,
                "wins":      self.wins,
                "losses":    self.losses,
                "winrate":   wr,
                "total_pnl": round(self.pnl, 2),
            }


# Глобальный объект статистики — импортируется в main.py
s2_stats = Strategy2Stats()


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def _get_klines_tf(session, symbol: str, interval: str, limit: int) -> list[dict]:
    """Загрузить свечи с нужным тайм-фреймом."""
    resp = session.get_kline(
        category="linear", symbol=symbol,
        interval=interval, limit=limit
    )
    bars = resp["result"]["list"]
    bars.reverse()
    return [{
        "ts":     int(b[0]),
        "open":   float(b[1]),
        "high":   float(b[2]),
        "low":    float(b[3]),
        "close":  float(b[4]),
        "volume": float(b[5])
    } for b in bars]


def _in_zone(price: float, low_price: float, high_price: float,
             tol: float = ZONE_TOLERANCE) -> bool:
    """
    Цена находится в зоне [low_price, high_price] с допуском tol.
    Для bull: low_price = fib[0.786], high_price = fib[0.618]  (выше — 0.5)
    Для bear: low_price = fib[0.618], high_price = fib[0.786]
    """
    lo = low_price  * (1 - tol)
    hi = high_price * (1 + tol)
    return lo <= price <= hi


def _calc_stop_tp(fibs: dict, trend: str) -> tuple[float, float]:
    """
    Вычислить стоп и тейк для зоны 0.618–0.786.

    Bull:
      • вход — цена в диапазоне [fib[0.786], fib[0.618]]
      • стоп — ниже fib[0.786] (самый глубокий уровень зоны для bull)
      • тейк — fib[0.5] (выше зоны)

    Bear:
      • вход — цена в диапазоне [fib[0.618], fib[0.786]]
      • стоп — выше fib[0.786]
      • тейк — fib[0.5] (ниже зоны)
    """
    f_stop  = fibs[STOP_RATIO]   # 0.786
    f_tp    = fibs[TP_RATIO]     # 0.5

    if trend == "bull":
        stop = f_stop * (1 - STOP_BUFFER)   # чуть ниже 0.786
    else:
        stop = f_stop * (1 + STOP_BUFFER)   # чуть выше 0.786

    return stop, f_tp


def _zone_bounds(fibs: dict, trend: str) -> tuple[float, float]:
    """
    Вернуть (нижняя, верхняя) границы зоны входа в ценовых единицах.

    Bull: fib[0.786] < fib[0.618]  →  zone = [fib[0.786], fib[0.618]]
    Bear: fib[0.618] < fib[0.786]  →  zone = [fib[0.618], fib[0.786]]
    """
    a = fibs[ZONE_LOW_RATIO]    # 0.618
    b_val = fibs[ZONE_HIGH_RATIO]   # 0.786
    lo = min(a, b_val)
    hi = max(a, b_val)
    return lo, hi


# ─── Расчёт объёма позиции ───────────────────────────────────────────────────

def _calc_qty(balance: float, entry: float, stop: float) -> float:
    risk_usdt = balance * (RISK_PCT / 100)
    stop_dist = abs(entry - stop)
    if stop_dist == 0:
        return 0.0
    stop_pct  = stop_dist / entry
    qty_usdt  = risk_usdt / stop_pct
    return round(qty_usdt / entry, 3)


# ─── Проверка TP/SL направления ──────────────────────────────────────────────

def _trade_valid(entry: float, stop: float, tp: float, trend: str) -> bool:
    """Проверить, что стоп и тейк стоят по правильную сторону от входа."""
    if trend == "bull":
        return stop < entry < tp
    else:
        return tp < entry < stop


# ─── Сканирование одного символа ─────────────────────────────────────────────

def scan_symbol_s2(symbol: str, session, state, balance: float,
                   notify_fn=None, open_trade_fn=None):
    """
    Проверить один символ по стратегии 2.

    Параметры:
        session       — HTTP-сессия Bybit (из main.py)
        state         — объект State из main.py (общий, чтобы не дублировать позиции)
        balance       — текущий баланс USDT
        notify_fn     — функция уведомления (notify_trade_open из telegram_notify)
        open_trade_fn — функция открытия сделки (open_trade из main.py)
    """
    try:
        # Пропускаем, если символ уже в стопе (консекутивные убытки) — общий state
        if symbol in state.stopped:
            return

        # Не открываем новую позицию, если уже есть открытая
        from main import get_position  # импорт здесь, чтобы избежать циркулярности
        if get_position(symbol):
            return

        # ── 1. Определяем тренд по 15-минутному тайм-фрейму ─────────────────
        bars_15m = _get_klines_tf(session, symbol, S2_TREND_TF, S2_TREND_BARS + 5)
        if len(bars_15m) < S2_TREND_BARS:
            return

        sh, sl, trend = find_swing(bars_15m[-S2_TREND_BARS:])
        fibs = calc_fib(sh, sl, trend)

        # ── 2. Берём текущую цену с 1-минутного тайм-фрейма ─────────────────
        bars_1m = _get_klines_tf(session, symbol, S2_TIMEFRAME, 30)
        if len(bars_1m) < 5:
            return

        cur_bar = bars_1m[-1]
        price   = cur_bar["close"]

        # ── 3. Проверяем, что цена в зоне 0.618–0.786 ───────────────────────
        zone_lo, zone_hi = _zone_bounds(fibs, trend)
        if not _in_zone(price, zone_lo, zone_hi):
            return

        # ── 4. Фильтры объёма (по 1m) и формы свечи ─────────────────────────
        if not vol_ok(bars_1m, lookback=20):
            return
        if not candle_ok(cur_bar, trend):
            return

        # ── 5. Рассчитываем стоп и тейк ─────────────────────────────────────
        entry = price
        stop, tp = _calc_stop_tp(fibs, trend)

        # Санити-чек: стоп и тейк должны стоять правильно
        if not _trade_valid(entry, stop, tp, trend):
            log.debug(f"{symbol} | S2: некорректный trade_valid, пропуск "
                      f"(entry={entry:.4f} stop={stop:.4f} tp={tp:.4f} trend={trend})")
            return

        side = "long" if trend == "bull" else "short"
        qty  = _calc_qty(balance, entry, stop)
        if qty <= 0:
            return

        log.info(
            f"[S2] {symbol} | {side.upper()} | entry={entry:.4f} "
            f"stop={stop:.4f} tp={tp:.4f} | zone=[{zone_lo:.4f}–{zone_hi:.4f}]"
        )

        # ── 6. Открываем сделку ──────────────────────────────────────────────
        if open_trade_fn:
            open_trade_fn(symbol, side, entry, stop, [tp], balance)
        else:
            # Fallback: прямой вызов сессии (если open_trade_fn не передана)
            bybit_side = "Buy" if side == "long" else "Sell"
            try:
                session.place_order(
                    category="linear", symbol=symbol,
                    side=bybit_side, orderType="Market", qty=str(qty),
                    stopLoss=str(round(stop, 4)),
                    takeProfit=str(round(tp, 4)),
                    slTriggerBy="LastPrice", tpTriggerBy="LastPrice",
                    timeInForce="GTC", reduceOnly=False,
                )
            except Exception as e:
                log.error(f"[S2] {symbol} | Ошибка ордера: {e}")
                return

        # Уведомление в Telegram
        if notify_fn:
            try:
                notify_fn(symbol, side, entry, round(stop, 4), round(tp, 4), qty)
            except Exception:
                pass

    except Exception as e:
        log.error(f"[S2] {symbol} | Ошибка сканирования: {e}")


# ─── Основной цикл стратегии 2 ───────────────────────────────────────────────

def run_strategy2(session, state, get_balance_fn,
                  notify_fn=None, open_trade_fn=None,
                  symbols: list[str] | None = None,
                  stop_event: threading.Event | None = None):
    """
    Запускается в отдельном потоке из main.py.

    Параметры:
        session        — HTTP-сессия Bybit
        state          — общий объект State из main.py
        get_balance_fn — функция get_balance() из main.py
        notify_fn      — notify_trade_open из telegram_notify (или None)
        open_trade_fn  — open_trade из main.py (или None)
        symbols        — список символов (None → берёт из state.symbols)
        stop_event     — threading.Event для graceful shutdown
    """
    syms = symbols or state.symbols
    log.info(
        f"[S2] Стратегия 2 запущена | "
        f"Зона: 0.618–0.786 | TP: 0.5 | TF: {S2_TIMEFRAME}m (тренд: {S2_TREND_TF}m) | "
        f"Монет: {len(syms)}"
    )

    while True:
        if stop_event and stop_event.is_set():
            log.info("[S2] Получен сигнал остановки, завершаю работу.")
            break

        try:
            state.reset_daily()
            balance = get_balance_fn()
            active  = [s for s in syms if s not in state.stopped]

            with ThreadPoolExecutor(max_workers=S2_MAX_WORKERS) as pool:
                futures = {
                    pool.submit(
                        scan_symbol_s2, sym, session, state,
                        balance, notify_fn, open_trade_fn
                    ): sym
                    for sym in active
                }
                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        log.error(f"[S2] {sym} | Поток упал: {e}")

        except Exception as e:
            log.error(f"[S2] Ошибка цикла: {e}")

        # Ждём, проверяя stop_event каждую секунду
        for _ in range(S2_LOOP_SLEEP):
            if stop_event and stop_event.is_set():
                break
            time.sleep(1)


# ─── Бэктест стратегии 2 ─────────────────────────────────────────────────────

def backtest_s2(session, symbols: list[str] | None = None,
                candles: int = 1000, initial_dep: float = 1000.0) -> dict:
    """
    Бэктест стратегии 2 на исторических данных.
    Загружает 15m свечи для тренда и 1m для входа.
    Возвращает сводную статистику по всем символам.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    syms = symbols or SYMBOLS
    results = {}

    def _bt_one(sym):
        # 15m для тренда
        bars_15m = _get_klines_tf(session, sym, S2_TREND_TF, candles)
        # 1m для поиска входов (берём то же кол-во для синхронизации)
        bars_1m  = _get_klines_tf(session, sym, S2_TIMEFRAME, min(candles, 1000))

        deposit = initial_dep
        wins = losses = 0
        trades = []

        for i in range(S2_TREND_BARS + 1, len(bars_1m) - 1):
            # Находим ближайший 15m бар по времени
            ts_1m = bars_1m[i]["ts"]
            idx_15 = None
            for k in range(len(bars_15m) - 1, -1, -1):
                if bars_15m[k]["ts"] <= ts_1m:
                    idx_15 = k
                    break
            if idx_15 is None or idx_15 < S2_TREND_BARS:
                continue

            window_15 = bars_15m[idx_15 - S2_TREND_BARS: idx_15]
            sh, sl, trend = find_swing(window_15)
            fibs = calc_fib(sh, sl, trend)

            cur_bar = bars_1m[i]
            price   = cur_bar["close"]

            zone_lo, zone_hi = _zone_bounds(fibs, trend)
            if not _in_zone(price, zone_lo, zone_hi):
                continue

            if not vol_ok(bars_1m[:i+1], lookback=20):
                continue
            if not candle_ok(cur_bar, trend):
                continue

            entry = price
            stop, tp = _calc_stop_tp(fibs, trend)
            if not _trade_valid(entry, stop, tp, trend):
                continue

            stop_dist_pct = abs(entry - stop) / entry
            risk_usdt     = deposit * (RISK_PCT / 100)
            qty_usdt      = risk_usdt / stop_dist_pct

            # Смотрим следующие 30 минутных баров
            outcome = None
            for j in range(i + 1, min(i + 31, len(bars_1m))):
                fut = bars_1m[j]
                if trend == "bull":
                    if fut["low"]  <= stop: outcome = "loss"; break
                    if fut["high"] >= tp:   outcome = "win";  break
                else:
                    if fut["high"] >= stop: outcome = "loss"; break
                    if fut["low"]  <= tp:   outcome = "win";  break

            if outcome is None:
                continue

            pnl_pct  = abs(tp - entry) / entry if outcome == "win" else -stop_dist_pct
            pnl_usdt = qty_usdt * pnl_pct - deposit * 0.001  # комиссия

            deposit += pnl_usdt
            trades.append({
                "i": i, "side": trend, "entry": round(entry, 4),
                "outcome": outcome,
                "pnl_usdt": round(pnl_usdt, 2),
                "deposit":  round(deposit, 2)
            })
            if outcome == "win": wins += 1
            else:                losses += 1

            if deposit <= 0:
                break

        total = wins + losses
        return {
            "trades":    total,
            "wins":      wins,
            "losses":    losses,
            "winrate":   round(wins / total * 100, 1) if total else 0,
            "final_dep": round(deposit, 2),
            "pnl_pct":   round((deposit - initial_dep) / initial_dep * 100, 1),
            "trade_log": trades
        }

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_bt_one, sym): sym for sym in syms}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                stats = fut.result()
                results[sym] = stats
                log.info(
                    f"[S2-BT] {sym} | Сделок: {stats['trades']} | "
                    f"Winrate: {stats['winrate']}% | "
                    f"PnL: {stats['pnl_pct']:+.1f}% | "
                    f"Депо: ${stats['final_dep']}"
                )
            except Exception as e:
                log.warning(f"[S2-BT] {sym} | Ошибка: {e}")

    return results
