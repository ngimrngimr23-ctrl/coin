import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiohttp import web
import time
import os

# ================= НАСТРОЙКИ =================
# ВАЖНО: токен ТОЛЬКО из переменной окружения. Никогда не хардкодь его в файле,
# иначе при пуше на GitHub он утечёт даже из приватного репозитория. На Render:
# Settings -> Environment -> добавь BOT_TOKEN.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

settings = {
    # ПЕРВИЧНЫЙ критерий: мин. % спреда между MEXC и HTX (в ЛЮБУЮ из двух сторон),
    # чтобы сработал алерт. Спред считается как отношение (цена продажи - цена
    # покупки) / цена покупки * 100, где покупка идёт по ask, продажа — по bid
    # (реальные исполнимые цены топа стакана, а не last price).
    "spread_percent": 1.0,

    # Мин. объём торгов за 24ч в $ — ОБЯЗАН выполняться на ОБЕИХ биржах разом,
    # иначе пара считается неликвидной (спред может быть просто "фантомным" —
    # широкий стакан без реальной глубины) и пропускается.
    "min_volume": 100000,

    "check_interval": 20,    # Как часто проверять (сек) — bulk-эндпоинты дешёвые
    "cooldown_min": 10,      # Мин. пауза между повторными алертами по одной паре

    "chat_id": None,
    "channel_id": None,
}

blacklist = set()
# symbol -> {"time": ts первого алерта, "last_msg": ts последнего алерта, "spread": % на момент последнего алерта}
alert_memory = {}

debug_stats = {
    "ts": 0.0,
    "mexc_ok": False,
    "huobi_ok": False,
    "common_pairs": 0,
    "passed_volume_floor": 0,
    "passed_spread_filter": 0,
    "passed_cooldown": 0,
    "alerts_sent": 0,
    "last_error": None,
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def fmt_money(x):
    """Компактный формат суммы в $: без десятичных для крупных чисел, 2 знака для мелких."""
    try:
        x = float(x)
    except Exception:
        return str(x)
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.2f}"


def fmt_price(x):
    """Цена без обрезания значимых знаков — у мелких монет 6-8 знаков после запятой важны."""
    try:
        x = float(x)
    except Exception:
        return str(x)
    if x >= 1:
        return f"{x:,.4f}".rstrip('0').rstrip('.')
    return f"{x:.8f}".rstrip('0').rstrip('.')


# ================= TELEGRAM UI =================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    settings["chat_id"] = message.chat.id
    await message.answer(
        "🔀 <b>Спред-сканер MEXC ⇄ HTX (Huobi) запущен</b>\n"
        "Ищет расхождение цены по ВСЕМ общим USDT-парам на обеих биржах. "
        "Спред считается по реальным исполнимым ценам топа стакана (bid/ask), "
        "в обе стороны — берётся направление с большим спредом.\n\n"

        "⚙️ <b>Команды</b>\n"
        f"/sp 1.5 — мин. % спреда, чтобы сработал алерт (ПЕРВИЧНЫЙ критерий)\n"
        f"   └ сейчас: <b>{settings['spread_percent']}%</b>\n"
        f"/v 100000 — мин. объём торгов за 24ч в $, обязателен на ОБЕИХ биржах сразу (фильтр ликвидности/фантомных спредов)\n"
        f"   └ сейчас: <b>{settings['min_volume']:,}$</b>\n"
        f"/cd 10 — пауза между повторными алертами по одной и той же паре, в минутах\n"
        f"   └ сейчас: <b>{settings['cooldown_min']} мин</b>\n"
        f"/b BTC — добавить монету в чёрный список (без алертов)\n"
        f"   └ в ЧС сейчас: <b>{len(blacklist)} шт.</b>\n"
        f"/channel @имя_канала — куда дублировать сигналы (пусто = выкл)\n"
        f"   └ сейчас: <b>{settings['channel_id'] or 'Не задан'}</b>\n"
        f"/s — текущий статус настроек\n"
        f"/debug — воронка последнего прохода сканера (диагностика, если алертов нет)\n\n"

        "⚠️ <b>Важно понимать</b>\n"
        "Это спред между ценами В МОМЕНТ ЗАПРОСА, без учёта комиссий за сделки "
        "(обычно ~0.1-0.2% на каждой бирже) и БЕЗ учёта перевода монеты между "
        "биржами — реальный арбитраж требует уже иметь баланс на обеих биржах, "
        "либо спред должен держаться дольше времени перевода. Перед сделкой всегда "
        "проверяй, что ввод/вывод именно этой монеты не приостановлен ни на одной "
        "из бирж — иначе спред технически недостижим.",
        parse_mode="HTML")


@dp.message(Command("channel"))
async def set_channel(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args:
        settings["channel_id"] = command.args
        await message.answer(f"✅ Канал установлен: <b>{command.args}</b>\n<i>Сделай бота админом канала!</i>", parse_mode="HTML")
    else:
        settings["channel_id"] = None
        await message.answer("✅ Дублирование в канал <b>ОТКЛЮЧЕНО</b>", parse_mode="HTML")


@dp.message(Command("sp"))
async def set_spread(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    try:
        val = abs(float(command.args.replace(',', '.')))
        settings["spread_percent"] = val
        await message.answer(f"✅ Мин. % спреда для алерта: <b>{val}%</b>", parse_mode="HTML")
    except Exception:
        await message.answer("❌ Ошибка. Пример: /sp 1.5")


@dp.message(Command("v"))
async def set_volume(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.isdigit():
        settings["min_volume"] = int(command.args)
        await message.answer(f"✅ Мин. объём 24ч (на ОБЕИХ биржах): <b>{settings['min_volume']:,}$</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка. Пример: /v 100000")


@dp.message(Command("cd"))
async def set_cooldown(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.isdigit():
        settings["cooldown_min"] = int(command.args)
        await message.answer(f"✅ Пауза между повторными алертами: <b>{settings['cooldown_min']} мин</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка. Пример: /cd 10")


@dp.message(Command("b"))
async def add_blacklist(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args:
        coin = command.args.upper()
        pair = coin if coin.endswith("USDT") else f"{coin}USDT"
        if pair in blacklist:
            blacklist.discard(pair)
            await message.answer(f"✅ <b>{pair}</b> убран из ЧС", parse_mode="HTML")
        else:
            blacklist.add(pair)
            await message.answer(f"🚫 <b>{pair}</b> в ЧС", parse_mode="HTML")


@dp.message(Command("s"))
async def status_cmd(message: types.Message):
    await message.answer(
        "📊 <b>Статус</b>\n"
        f"🔀 Мин. % спреда: <b>{settings['spread_percent']}%</b>\n"
        f"💰 Мин. объём 24ч (обе биржи): <b>{settings['min_volume']:,}$</b>\n"
        f"⏱ Пауза между повторными алертами: <b>{settings['cooldown_min']} мин</b>\n"
        f"🚫 В чёрном списке: <b>{len(blacklist)} шт.</b>\n"
        f"📢 Канал: {settings['channel_id'] or 'Не задан'}\n"
        f"🔁 Интервал проверки: {settings['check_interval']} сек\n"
        f"🛑 В памяти алертов: {len(alert_memory)}\n"
        f"🔗 Общих пар на прошлом проходе: {debug_stats['common_pairs']}"
        , parse_mode="HTML")


@dp.message(Command("debug"))
async def debug_cmd(message: types.Message):
    ts = debug_stats["ts"]
    ago = int(time.time() - ts) if ts else None
    ago_str = f"{ago} сек назад" if ago is not None else "ещё не было прохода"

    mexc_status = "✅ ОК" if debug_stats["mexc_ok"] else "❌ Ошибка/пусто"
    huobi_status = "✅ ОК" if debug_stats["huobi_ok"] else "❌ Ошибка/пусто"

    lines = [
        "🔍 <b>Воронка последнего прохода сканера</b>",
        f"⏱ Прошёл: {ago_str}",
        f"📡 MEXC (bid/ask + объём): {mexc_status}",
        f"📡 HTX (bid/ask + объём): {huobi_status}",
        f"1️⃣ Общих USDT-пар на обеих биржах: {debug_stats['common_pairs']}",
        f"2️⃣ Прошли мин. объём 24ч на обеих биржах (/v): {debug_stats['passed_volume_floor']}",
        f"3️⃣ Прошли порог спреда (/sp): {debug_stats['passed_spread_filter']}",
        f"4️⃣ Прошли анти-спам (кулдаун /cd): {debug_stats['passed_cooldown']}",
        f"📨 Алертов отправлено за этот проход: {debug_stats['alerts_sent']}",
    ]
    if debug_stats["last_error"]:
        lines.append(f"⚠️ Последняя ошибка: {debug_stats['last_error']}")

    lines.append("")
    lines.append(
        "💡 Если шаг 1 близок к нулю — вероятно, упал запрос к одной из бирж "
        "(смотри статус выше). Если шаг 2→3 сильно обнуляется — попробуй снизить "
        "/sp, спреды >1-2% на ликвидных парах случаются нечасто."
    )

    await message.answer("\n".join(lines), parse_mode="HTML")


# ================= API =================

async def fetch_mexc_data():
    """
    Возвращает dict symbol -> {"bid": float, "ask": float, "vol": float ($ за 24ч)}.
    Два bulk-запроса без параметров (все пары сразу): bookTicker даёт bid/ask,
    ticker/24hr даёт объём в quote-валюте (USDT).
    """
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.mexc.com/api/v3/ticker/bookTicker", timeout=15) as resp:
                if resp.status != 200:
                    debug_stats["last_error"] = f"MEXC bookTicker HTTP {resp.status}"
                    return {}
                book_data = await resp.json()
            async with session.get("https://api.mexc.com/api/v3/ticker/24hr", timeout=15) as resp:
                if resp.status != 200:
                    debug_stats["last_error"] = f"MEXC 24hr HTTP {resp.status}"
                    return {}
                vol_data = await resp.json()
    except Exception as e:
        debug_stats["last_error"] = f"MEXC запрос: {e}"
        return {}

    vol_by_symbol = {}
    for item in vol_data:
        try:
            sym = item["symbol"]
            if sym.endswith("USDT"):
                vol_by_symbol[sym] = float(item["quoteVolume"])
        except Exception:
            continue

    for item in book_data:
        try:
            sym = item["symbol"]
            if not sym.endswith("USDT"):
                continue
            bid = float(item["bidPrice"])
            ask = float(item["askPrice"])
            if bid <= 0 or ask <= 0:
                continue
            result[sym] = {"bid": bid, "ask": ask, "vol": vol_by_symbol.get(sym, 0.0)}
        except Exception:
            continue

    return result


def _first_num(v):
    """Huobi иногда отдаёт bid/ask как число, иногда как [цена, объём] — берём цену в обоих случаях."""
    if isinstance(v, (list, tuple)):
        return float(v[0]) if v else None
    return float(v)


async def fetch_huobi_data():
    """
    Возвращает dict symbol (в формате MEXC, напр. "BTCUSDT") -> {"bid", "ask", "vol"}.
    Один bulk-запрос без параметров — все пары сразу, поле "vol" — суточный оборот
    в quote-валюте (для *usdt пар — в USDT), совпадает по смыслу с quoteVolume MEXC.
    """
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.huobi.pro/market/tickers", timeout=15) as resp:
                if resp.status != 200:
                    debug_stats["last_error"] = f"HTX tickers HTTP {resp.status}"
                    return {}
                payload = await resp.json()
    except Exception as e:
        debug_stats["last_error"] = f"HTX запрос: {e}"
        return {}

    for item in payload.get("data", []):
        try:
            raw_sym = item.get("symbol", "")
            if not raw_sym.endswith("usdt"):
                continue
            sym = raw_sym.upper()  # "btcusdt" -> "BTCUSDT", тот же формат, что у MEXC
            bid = _first_num(item.get("bid"))
            ask = _first_num(item.get("ask"))
            if not bid or not ask or bid <= 0 or ask <= 0:
                continue
            vol = float(item.get("vol", 0.0) or 0.0)  # оборот в USDT за сутки
            result[sym] = {"bid": bid, "ask": ask, "vol": vol}
        except Exception:
            continue

    return result


# ================= ОСНОВНОЙ ЦИКЛ =================

async def scanner_task():
    while True:
        try:
            mexc_data, huobi_data = await asyncio.gather(fetch_mexc_data(), fetch_huobi_data())
            debug_stats["ts"] = time.time()
            debug_stats["mexc_ok"] = bool(mexc_data)
            debug_stats["huobi_ok"] = bool(huobi_data)
            debug_stats["passed_volume_floor"] = 0
            debug_stats["passed_spread_filter"] = 0
            debug_stats["passed_cooldown"] = 0
            debug_stats["alerts_sent"] = 0

            if not mexc_data or not huobi_data:
                await asyncio.sleep(settings["check_interval"])
                continue

            common = set(mexc_data.keys()) & set(huobi_data.keys())
            common -= blacklist
            debug_stats["common_pairs"] = len(common)

            now = time.time()
            for pair in common:
                m = mexc_data[pair]
                h = huobi_data[pair]

                if m["vol"] < settings["min_volume"] or h["vol"] < settings["min_volume"]:
                    continue
                debug_stats["passed_volume_floor"] += 1

                # Направление 1: купить на MEXC по ask, продать на HTX по bid
                spread_mexc_to_htx = (h["bid"] - m["ask"]) / m["ask"] * 100
                # Направление 2: купить на HTX по ask, продать на MEXC по bid
                spread_htx_to_mexc = (m["bid"] - h["ask"]) / h["ask"] * 100

                if spread_mexc_to_htx >= spread_htx_to_mexc:
                    best_spread = spread_mexc_to_htx
                    buy_ex, sell_ex = "MEXC", "HTX"
                    buy_price, sell_price = m["ask"], h["bid"]
                else:
                    best_spread = spread_htx_to_mexc
                    buy_ex, sell_ex = "HTX", "MEXC"
                    buy_price, sell_price = h["ask"], m["bid"]

                if best_spread < settings["spread_percent"]:
                    continue
                debug_stats["passed_spread_filter"] += 1

                # Анти-спам: простой кулдаун по времени (спред колеблется вокруг
                # порога чаще, чем растёт монотонно, поэтому x2-правило пампа тут
                # не подходит — просто не спамим чаще N минут по одной паре).
                prev = alert_memory.get(pair)
                if prev and (now - prev["last_msg"]) < settings["cooldown_min"] * 60:
                    continue
                debug_stats["passed_cooldown"] += 1

                alert_memory[pair] = {
                    "time": prev["time"] if prev else now,
                    "last_msg": now,
                    "spread": best_spread,
                }
                debug_stats["alerts_sent"] += 1

                base_coin = pair.replace("USDT", "")
                lines = [
                    f"🔀 <b>СПРЕД: <code>{base_coin}</code></b>",
                    "",
                    f"💹 <b>{best_spread:+.2f}%</b> · Купить на <b>{buy_ex}</b> ({fmt_price(buy_price)}) "
                    f"→ Продать на <b>{sell_ex}</b> ({fmt_price(sell_price)})",
                    "",
                    f"📥 MEXC: bid {fmt_price(m['bid'])} / ask {fmt_price(m['ask'])}",
                    f"📤 HTX: bid {fmt_price(h['bid'])} / ask {fmt_price(h['ask'])}",
                    "",
                    f"💰 Объём 24ч: MEXC {fmt_money(m['vol'])}$ · HTX {fmt_money(h['vol'])}$",
                ]
                alert_text = "\n".join(lines)

                if settings["chat_id"]:
                    try:
                        await bot.send_message(settings["chat_id"], alert_text, parse_mode="HTML")
                    except Exception as e:
                        print(f"Ошибка отправки админу: {e}", flush=True)

                if settings["channel_id"]:
                    try:
                        await bot.send_message(settings["channel_id"], alert_text, parse_mode="HTML")
                    except Exception as e:
                        print(f"Не удалось отправить в канал {settings['channel_id']}: {e}", flush=True)

        except Exception as e:
            print(f"Ошибка сканера: {e}", flush=True)
            debug_stats["last_error"] = str(e)

        await asyncio.sleep(settings["check_interval"])


# ================= WEB & RUN =================

async def handle_ping(request):
    return web.Response(text="OK", status=200)


async def main():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 10000)))
    await site.start()

    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(scanner_task())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
