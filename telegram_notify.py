"""
telegram_notify.py — Уведомления в Telegram
"""

import requests
import logging

log = logging.getLogger(__name__)

# ─── Настройки ───────────────────────────
TG_TOKEN   = "8678125519:AAEnBwODFtYZpEMhKfd0vTHrgO4ouo5Mggc"   # от @BotFather
TG_CHAT_ID = "1630387325"     # ваш chat_id (узнать у @userinfobot)


def send(text: str):
    """Отправить сообщение в Telegram."""
    if TG_TOKEN == "8678125519:AAEnBwODFtYZpEMhKfd0vTHrgO4ouo5Mggc":
        log.debug(f"[TG отключён] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        log.warning(f"Telegram ошибка: {e}")


def notify_trade_open(symbol, side, entry, stop, tp, qty):
    emoji = "🟢" if side == "long" else "🔴"
    send(
        f"{emoji} <b>ВХОД {symbol}</b>\n"
        f"Сторона: {side.upper()}\n"
        f"Вход: <b>${entry:,.2f}</b>\n"
        f"Стоп: ${stop:,.2f}\n"
        f"TP1: ${tp:,.2f}\n"
        f"Объём: {qty}"
    )


def notify_trade_close(symbol, outcome, pnl_pct, deposit):
    emoji = "✅" if outcome == "win" else "❌"
    send(
        f"{emoji} <b>ЗАКРЫТИЕ {symbol}</b>\n"
        f"Результат: {'ПРИБЫЛЬ' if outcome == 'win' else 'УБЫТОК'}\n"
        f"PnL: <b>{pnl_pct:+.2f}%</b>\n"
        f"Депозит: <b>${deposit:,.2f}</b>"
    )


def notify_daily_stop(symbol):
    send(f"⛔ <b>{symbol}</b> — дневная остановка ({5} убытков подряд)")


def notify_start(symbols, balance):
    send(
        f"🤖 <b>Fibonacci Bot запущен</b>\n"
        f"Монеты: {', '.join(symbols)}\n"
        f"Баланс: <b>${balance:,.2f}</b>\n"
        f"Плечо: x10 | Риск: 2%/сделка"
    )


def notify_backtest(results: dict):
    lines = ["📊 <b>Результаты бэктеста</b>\n"]
    for sym, s in results.items():
        lines.append(
            f"<b>{sym}</b>: {s['trades']} сделок | "
            f"WR {s['winrate']}% | PnL {s['pnl_pct']:+.1f}%"
        )
    send("\n".join(lines))
