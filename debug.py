from main import session, get_klines
from backtest import find_swing, calc_fib, vol_ok, candle_ok, SWING_BARS, ENTRY_LEVELS, SYMBOLS
from backtest import check_touch

symbol = "BTCUSDT"
bars = get_klines(symbol, limit=100)

price = bars[-1]["close"]
sh, sl, trend = find_swing(bars[-SWING_BARS:])
fibs = calc_fib(sh, sl, trend)

print(f"\n{'='*40}")
print(f"Символ:      {symbol}")
print(f"Цена:        {price}")
print(f"Swing High:  {sh}")
print(f"Swing Low:   {sl}")
print(f"Тренд:       {trend}")
print(f"Фибо уровни: {fibs}")
print(f"Объём OK:    {vol_ok(bars)}")
print(f"Свеча OK:    {candle_ok(bars[-1], trend)}")
print(f"{'='*40}")

# Проверяем касание каждого уровня
print("\nПроверка касания уровней:")
for fib_r in ENTRY_LEVELS:
    if fib_r in fibs:
        touched = check_touch(price, fibs[fib_r])
        print(f"  Уровень {fib_r}: цена={price:.2f} | уровень={fibs[fib_r]:.2f} | касание={touched}")

# Прогоняем все символы
print(f"\n{'='*40}")
print("Сканируем все символы...\n")

passed = []
for sym in SYMBOLS:
    try:
        b = get_klines(sym, limit=100)
        if len(b) < SWING_BARS:
            print(f"{sym:15} | ❌ Мало баров ({len(b)})")
            continue
        p = b[-1]["close"]
        h, l, t = find_swing(b[-SWING_BARS:])
        if not t:
            print(f"{sym:15} | ❌ Тренд не определён")
            continue
        f = calc_fib(h, l, t)
        touched = any(check_touch(p, f[r]) for r in ENTRY_LEVELS if r in f)
        v_ok = vol_ok(b)
        c_ok = candle_ok(b[-1], t)
        status = "✅ СИГНАЛ" if (touched and v_ok and c_ok) else "—"
        print(f"{sym:15} | тренд={t:4} | объём={'✅' if v_ok else '❌'} | свеча={'✅' if c_ok else '❌'} | касание={'✅' if touched else '❌'} | {status}")
        if touched and v_ok and c_ok:
            passed.append(sym)
    except Exception as e:
        print(f"{sym:15} | 💥 Ошибка: {e}")

print(f"\nИтого сигналов: {len(passed)} из {len(SYMBOLS)}")
if passed:
    print(f"Символы с сигналом: {passed}")
