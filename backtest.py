"""
backtest.py — Бэктест стратегии Фибоначчи на исторических данных Bybit
Запуск: python backtest.py
"""

import time
import json
import logging
from datetime import datetime
from pybit.unified_trading import HTTP

log = logging.getLogger(__name__)

# ─── Настройки бэктеста ───────────────────
SYMBOLS      = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TIMEFRAME    = "15"
SWING_BARS   = 50
ENTRY_LEVELS = [0.382, 0.5, 0.618, 0.786]
TP_LEVELS    = [1.0, 1.272, 1.618]
STOP_BUFFER  = 0.003
RISK_PCT     = 2.0
LEVERAGE     = 10
VOLUME_MULT  = 1.3
INITIAL_DEP  = 1000.0   # стартовый депозит для симуляции
CANDLES      = 1000       # кол-во свечей для бэктеста


def fetch_history(session, symbol: str, limit: int = 500) -> list[dict]:
    resp = session.get_kline(
        category="linear", symbol=symbol,
        interval=TIMEFRAME, limit=limit
    )
    bars = resp["result"]["list"]
    bars.reverse()
    return [{
        "ts": int(b[0]), "open": float(b[1]), "high": float(b[2]),
        "low": float(b[3]), "close": float(b[4]), "volume": float(b[5])
    } for b in bars]


def find_swing(bars):
    swing_h = max(b["high"] for b in bars)
    swing_l = min(b["low"]  for b in bars)
    idx_h = max(range(len(bars)), key=lambda i: bars[i]["high"])
    idx_l = min(range(len(bars)), key=lambda i: bars[i]["low"])
    trend = "bull" if idx_l < idx_h else "bear"
    return swing_h, swing_l, trend


def calc_fib(swing_h, swing_l, trend):
    rng = swing_h - swing_l
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, 2.0]
    levels = {}
    for r in ratios:
        levels[r] = (swing_h - r * rng) if trend == "bull" else (swing_l + r * rng)
    return levels


def check_touch(price, level_price, tol=0.001):
    return abs(price - level_price) / level_price <= tol


def vol_ok(bars, lookback=20):
    avg = sum(b["volume"] for b in bars[-lookback:-1]) / (lookback - 1)
    return bars[-1]["volume"] >= avg * VOLUME_MULT


def candle_ok(bar, trend):
    mid = (bar["high"] + bar["low"]) / 2
    return bar["close"] > mid if trend == "bull" else bar["close"] < mid


def simulate(bars: list[dict]) -> dict:
    """Прогнать бэктест по барам. Возвращает статистику."""
    deposit   = INITIAL_DEP
    trades    = []
    wins = losses = 0

    for i in range(SWING_BARS + 1, len(bars) - 1):
        window  = bars[i - SWING_BARS: i]
        cur_bar = bars[i]
        price   = cur_bar["close"]

        sh, sl, trend = find_swing(window)
        fibs = calc_fib(sh, sl, trend)

        touched_lvl = None
        for fib_r in ENTRY_LEVELS:
            if check_touch(price, fibs[fib_r]):
                touched_lvl = fib_r
                break

        if touched_lvl is None:
            continue
        if not vol_ok(bars[:i+1]):
            continue
        if not candle_ok(cur_bar, trend):
            continue

        entry = price
        stop  = fibs[touched_lvl] * (1 - STOP_BUFFER) if trend == "bull" \
                else fibs[touched_lvl] * (1 + STOP_BUFFER)
        tp1   = fibs.get(TP_LEVELS[0], 0)

        if tp1 <= 0:
            continue

        stop_dist_pct = abs(entry - stop) / entry
        risk_usdt     = deposit * (RISK_PCT / 100)
        qty_usdt      = risk_usdt / stop_dist_pct

        # Симуляция: смотрим следующие 20 баров — достигнут ли TP или SL
        outcome = None
        for j in range(i + 1, min(i + 21, len(bars))):
            future = bars[j]
            if trend == "bull":
                if future["low"] <= stop:
                    outcome = "loss"; break
                if future["high"] >= tp1:
                    outcome = "win";  break
            else:
                if future["high"] >= stop:
                    outcome = "loss"; break
                if future["low"] <= tp1:
                    outcome = "win";  break

        if outcome is None:
            continue

        pnl_pct  = abs(tp1 - entry) / entry if outcome == "win" else -stop_dist_pct
        pnl_usdt = qty_usdt * pnl_pct - deposit * 0.001  # комиссия

        deposit += pnl_usdt
        trades.append({
            "i": i, "side": trend, "entry": round(entry, 2),
            "fib": touched_lvl, "outcome": outcome,
            "pnl_usdt": round(pnl_usdt, 2), "deposit": round(deposit, 2)
        })
        if outcome == "win":  wins += 1
        else:                 losses += 1

        if deposit <= 0:
            break

    total = wins + losses
    return {
        "trades":   total,
        "wins":     wins,
        "losses":   losses,
        "winrate":  round(wins / total * 100, 1) if total else 0,
        "final_dep": round(deposit, 2),
        "pnl_pct":  round((deposit - INITIAL_DEP) / INITIAL_DEP * 100, 1),
        "trade_log": trades
    }


def run_backtest(session):
    log.info("=== БЭКТЕСТ ЗАПУЩЕН ===")
    results = {}
    for sym in SYMBOLS:
        log.info(f"Загружаю данные {sym}...")
        bars = fetch_history(session, sym, limit=CANDLES)
        stats = simulate(bars)
        results[sym] = stats
        log.info(
            f"{sym} | Сделок: {stats['trades']} | "
            f"Winrate: {stats['winrate']}% | "
            f"PnL: {stats['pnl_pct']:+.1f}% | "
            f"Депо: ${stats['final_dep']}"
        )
        time.sleep(0.3)

    with open("backtest_results.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    log.info("Результаты сохранены в backtest_results.json")
    return results
