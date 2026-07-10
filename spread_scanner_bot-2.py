import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiohttp import web
import time
import os
import hmac
import hashlib
import urllib.parse

# ================= НАСТРОЙКИ =================
# ВАЖНО: токен ТОЛЬКО из переменной окружения. Никогда не хардкодь его в файле,
# иначе при пуше на GitHub он утечёт даже из приватного репозитория. На Render:
# Settings -> Environment -> добавь BOT_TOKEN.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

# Опционально: read-only ключ MEXC для проверки реальных контрактов монет
# (эндпоинт /api/v3/capital/config/getall — ПОДПИСЫВАЕМЫЙ, без ключа недоступен).
# Если не заданы — бот просто не проверяет контракты и работает как раньше.
MEXC_API_KEY = os.environ.get("MEXC_API_KEY")
MEXC_API_SECRET = os.environ.get("MEXC_API_SECRET")

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

    # Спред должен непрерывно держаться выше порога (/sp) хотя бы это число
    # секунд, прежде чем бот отправит алерт — отсекает случайные разовые скачки
    # цены на долю секунды, которые физически не успеть исполнить руками.
    # 0 = выключено (алерт сразу же, как раньше).
    "spread_stable_sec": 0,

    # Мин. сумма в $, которую реально можно "прокрутить" (min объёма на бид/аск
    # с обеих сторон по глубине стакана) — отсекает пары, где спред красивый на
    # бумаге, но исполнить его целиком нельзя из-за тонкого стакана.
    # 0 = выключено (фильтр не применяется, сумма просто показывается в алерте).
    "min_turnover_usd": 0,

    # Фильтр по факту возможности перевода монеты. ВАЖНО: реально проверяется
    # ТОЛЬКО сторона HTX (публичный эндпоинт, без API-ключа). Статус MEXC без
    # приватного API-ключа недоступен в принципе — эта сторона в алерте всегда
    # помечается как "не проверяется", её нужно смотреть на бирже вручную.
    "require_transferable": True,

    "chat_id": None,
    "channel_id": None,
}

blacklist = set()
# symbol -> {"time": ts первого алерта, "last_msg": ts последнего алерта, "spread": % на момент последнего алерта}
alert_memory = {}

# symbol -> ts, когда спред НЕПРЕРЫВНО начал держаться выше порога (для /ss).
# Сбрасывается, как только спред падает ниже порога хоть на одном проходе.
spread_track = {}

# Кэш статуса ввода/вывода с HTX: {"ts": fetched_at, "data": {"BTC": {"deposit": bool, "withdraw": bool}, ...}}
# Обновляется редко (см. HTX_TRANSFER_TTL) — этот статус почти не меняется в течение дня,
# незачем дёргать эндпоинт каждый проход сканера.
htx_transfer_cache = {"ts": 0.0, "data": {}}
HTX_TRANSFER_TTL = 600  # 10 минут

mexc_contracts_cache = {"ts": 0.0, "data": {}}
MEXC_CONTRACTS_TTL = 6 * 3600  # 6 часов — сети/контракты почти никогда не меняются

debug_stats = {
    "ts": 0.0,
    "mexc_ok": False,
    "huobi_ok": False,
    "mexc_contracts_ok": False,
    "htx_transfer_ok": False,
    "common_pairs": 0,
    "passed_volume_floor": 0,
    "passed_spread_filter": 0,
    "passed_stability": 0,
    "passed_turnover_filter": 0,
    "passed_transfer_check": 0,
    "blocked_by_transfer": 0,
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
    stable_display = "Выкл" if settings["spread_stable_sec"] == 0 else f"{settings['spread_stable_sec']} сек"
    turnover_display = "Выкл" if settings["min_turnover_usd"] == 0 else f"{settings['min_turnover_usd']:,.0f}$"
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
        f"/mt 500 — мин. сумма в $, которую реально можно прокрутить по глубине стакана (0 = не фильтровать, просто показывать)\n"
        f"   └ сейчас: <b>{turnover_display}</b>\n"
        f"/cd 10 — пауза между повторными алертами по одной и той же паре, в минутах\n"
        f"   └ сейчас: <b>{settings['cooldown_min']} мин</b>\n"
        f"/ss 30 — спред должен непрерывно держаться выше порога минимум N секунд перед алертом (0 = выключить)\n"
        f"   └ сейчас: <b>{stable_display}</b>\n"
        f"/tr — вкл/выкл фильтр по доступности вывода/ввода (см. ⚠️ ниже про ограничение)\n"
        f"   └ сейчас: <b>{'Вкл' if settings['require_transferable'] else 'Выкл'}</b>\n"
        f"/b BTC — добавить монету в чёрный список (без алертов)\n"
        f"   └ в ЧС сейчас: <b>{len(blacklist)} шт.</b>\n"
        f"/channel @имя_канала — куда дублировать сигналы (пусто = выкл)\n"
        f"   └ сейчас: <b>{settings['channel_id'] or 'Не задан'}</b>\n"
        f"/s — текущий статус настроек\n"
        f"/debug — воронка последнего прохода сканера (диагностика, если алертов нет)\n\n"

        "⚠️ <b>Важно понимать</b>\n"
        "Это спред между ценами В МОМЕНТ ЗАПРОСА, без учёта комиссий за сделки "
        "(обычно ~0.1-0.2% на каждой бирже) и БЕЗ учёта времени перевода монеты "
        "между биржами.\n\n"
        "🚚 <b>Фильтр перевода (/tr)</b>: бот проверяет статус ввода/вывода "
        "монеты ТОЛЬКО на HTX (публичные данные, без ключа). Статус MEXC "
        "недоступен без приватного API-ключа — эта сторона в каждом алерте "
        "помечена как «не проверяется», проверяй её на бирже вручную перед "
        "сделкой. Если фильтр выключен — алерты идут вообще без проверки "
        "переводимости ни по одной из бирж.",
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


@dp.message(Command("ss"))
async def set_spread_stable(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.lstrip('-').isdigit():
        val = max(0, int(command.args))
        settings["spread_stable_sec"] = val
        spread_track.clear()  # старые отметки времени были для другого порога — сбрасываем
        if val == 0:
            await message.answer("✅ Фильтр стабильности спреда <b>ВЫКЛЮЧЕН</b> (алерт сразу, как только спред превысит порог)", parse_mode="HTML")
        else:
            await message.answer(f"✅ Спред должен непрерывно держаться выше порога минимум <b>{val} сек</b> перед алертом", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка. Пример: /ss 30 (0 = выключить)")


@dp.message(Command("mt"))
async def set_min_turnover(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.replace('.', '', 1).isdigit():
        val = max(0.0, float(command.args))
        settings["min_turnover_usd"] = val
        if val == 0:
            await message.answer("✅ Фильтр мин. оборота <b>ВЫКЛЮЧЕН</b> (сумма для прокрутки просто показывается в алерте, не фильтрует)", parse_mode="HTML")
        else:
            await message.answer(f"✅ Мин. сумма для прокрутки (по глубине стакана): <b>{val:,.0f}$</b> — пары с меньшей глубиной не алертятся", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка. Пример: /mt 500 (0 = выключить)")


@dp.message(Command("cd"))
async def set_cooldown(message: types.Message, command: CommandObject):
    settings["chat_id"] = message.chat.id
    if command.args and command.args.isdigit():
        settings["cooldown_min"] = int(command.args)
        await message.answer(f"✅ Пауза между повторными алертами: <b>{settings['cooldown_min']} мин</b>", parse_mode="HTML")
    else:
        await message.answer("❌ Ошибка. Пример: /cd 10")


@dp.message(Command("tr"))
async def toggle_transfer_filter(message: types.Message):
    settings["chat_id"] = message.chat.id
    settings["require_transferable"] = not settings["require_transferable"]
    state = "ВКЛЮЧЕН" if settings["require_transferable"] else "ВЫКЛЮЧЕН"
    await message.answer(
        f"✅ Фильтр по доступности перевода: <b>{state}</b>\n"
        f"<i>Напоминание: реально проверяется только сторона HTX (публичный статус). "
        f"MEXC не проверяется — нет API-ключа, эта сторона в алерте всегда помечена как непроверенная.</i>",
        parse_mode="HTML")


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
    stable_display = "Выкл" if settings["spread_stable_sec"] == 0 else f"{settings['spread_stable_sec']} сек"
    turnover_display = "Выкл" if settings["min_turnover_usd"] == 0 else f"{settings['min_turnover_usd']:,.0f}$"
    await message.answer(
        "📊 <b>Статус</b>\n"
        f"🔀 Мин. % спреда: <b>{settings['spread_percent']}%</b>\n"
        f"💰 Мин. объём 24ч (обе биржи): <b>{settings['min_volume']:,}$</b>\n"
        f"📦 Мин. сумма для прокрутки: <b>{turnover_display}</b>\n"
        f"⏱ Пауза между повторными алертами: <b>{settings['cooldown_min']} мин</b>\n"
        f"⏳ Стабильность спреда перед алертом: <b>{stable_display}</b>\n"
        f"🚚 Фильтр перевода (только HTX-плечо): <b>{'Вкл' if settings['require_transferable'] else 'Выкл'}</b>\n"
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
    htx_transfer_status = "✅ ОК" if debug_stats["htx_transfer_ok"] else "❌ Ошибка/пусто"
    if not MEXC_API_KEY or not MEXC_API_SECRET:
        contracts_status = "⚪ Выключено (нет MEXC_API_KEY/SECRET)"
    else:
        contracts_status = "✅ ОК" if debug_stats["mexc_contracts_ok"] else "❌ Ошибка"

    lines = [
        "🔍 <b>Воронка последнего прохода сканера</b>",
        f"⏱ Прошёл: {ago_str}",
        f"📡 MEXC (bid/ask + объём): {mexc_status}",
        f"📡 HTX (bid/ask + объём): {huobi_status}",
        f"📡 HTX (статус ввода/вывода): {htx_transfer_status}",
        f"📡 MEXC (контракты монет): {contracts_status}",
        f"1️⃣ Общих USDT-пар на обеих биржах: {debug_stats['common_pairs']}",
        f"2️⃣ Прошли мин. объём 24ч на обеих биржах (/v): {debug_stats['passed_volume_floor']}",
        f"3️⃣ Прошли порог спреда (/sp): {debug_stats['passed_spread_filter']}",
        f"4️⃣ Прошли фильтр стабильности (/ss): {debug_stats['passed_stability']}",
        f"5️⃣ Прошли фильтр перевода (/tr): {debug_stats['passed_transfer_check']} (заблокировано: {debug_stats['blocked_by_transfer']})",
        f"6️⃣ Прошли анти-спам (кулдаун /cd): {debug_stats['passed_cooldown']}",
        f"7️⃣ Прошли фильтр мин. оборота (/mt): {debug_stats['passed_turnover_filter']}",
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
            result[sym] = {
                "bid": bid, "ask": ask, "vol": vol_by_symbol.get(sym, 0.0),
                "bid_qty": float(item.get("bidQty", 0) or 0),
                "ask_qty": float(item.get("askQty", 0) or 0),
            }
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


def _mexc_signed_query(extra_params=None):
    """Строит подписанную query-строку для приватных (SIGNED) эндпоинтов MEXC.
    Возвращает (query_string, api_key) или (None, None), если ключи не заданы."""
    if not MEXC_API_KEY or not MEXC_API_SECRET:
        return None, None
    params = dict(extra_params or {})
    params["timestamp"] = int(time.time() * 1000)
    params.setdefault("recvWindow", 10000)
    query = urllib.parse.urlencode(params)
    signature = hmac.new(MEXC_API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}", MEXC_API_KEY


async def get_mexc_contracts():
    """
    Реальные контракты/сети монет с MEXC через ПОДПИСЫВАЕМЫЙ эндпоинт
    /api/v3/capital/config/getall (Binance-style HMAC-SHA256, требует
    MEXC_API_KEY + MEXC_API_SECRET в переменных окружения; ключ — только Read).
    Если переменные не заданы — просто ничего не возвращает, остальной бот
    работает как и раньше, без проверки контрактов.
    Кэшируется надолго — список сетей/контрактов почти никогда не меняется.
    """
    now = time.time()
    if mexc_contracts_cache["data"] and (now - mexc_contracts_cache["ts"]) < MEXC_CONTRACTS_TTL:
        return mexc_contracts_cache["data"]

    query, api_key = _mexc_signed_query()
    if not api_key:
        return {}

    url = f"https://api.mexc.com/api/v3/capital/config/getall?{query}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"X-MEXC-APIKEY": api_key}, timeout=20) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    debug_stats["last_error"] = f"MEXC contracts HTTP {resp.status}: {body[:150]}"
                    return mexc_contracts_cache["data"]
                data = await resp.json()
    except Exception as e:
        debug_stats["last_error"] = f"MEXC contracts запрос: {e}"
        return mexc_contracts_cache["data"]

    result = {}
    for item in (data if isinstance(data, list) else []):
        try:
            coin = str(item.get("coin", "")).upper()
            networks = []
            for net in item.get("networkList", []):
                addr = net.get("contract") or net.get("contractAddress")
                if addr:
                    networks.append({
                        "network": net.get("network") or net.get("netWork") or "?",
                        "contract": addr,
                    })
            if networks:
                result[coin] = networks
        except Exception:
            continue

    if result:
        mexc_contracts_cache["ts"] = now
        mexc_contracts_cache["data"] = result
        debug_stats["mexc_contracts_ok"] = True
    else:
        debug_stats["mexc_contracts_ok"] = False

    return mexc_contracts_cache["data"]
    """Возвращает (сумма_комиссии, тип) для одной сети HTX, или (None, None), если
    определить не удалось. fixed — фиксированная сумма; circulated/ratio — берём
    минимальную границу комиссии (minTransactFeeWithdraw)."""
    fee_type = chain.get("withdrawFeeType")
    try:
        if fee_type == "fixed":
            return float(chain.get("transactFeeWithdraw", 0) or 0), "фикс"
        if fee_type in ("circulated", "ratio"):
            return float(chain.get("minTransactFeeWithdraw", 0) or 0), "мин"
    except Exception:
        pass
    return None, None


async def get_htx_transfer_status():
    """
    Статус ввода/вывода и комиссия за вывод по каждой монете на HTX. ПУБЛИЧНЫЙ
    эндпоинт, ключ не нужен. Агрегируем по всем сетям (chains) монеты: если хотя
    бы одна сеть открыта — считаем ввод/вывод доступным, а комиссию берём по
    САМОЙ ДЕШЁВОЙ из открытых для вывода сетей (для арбитража не важно через
    какую именно сеть, важно с какой минимальной комиссией). Кэшируется на
    HTX_TRANSFER_TTL — эти данные почти не меняются в течение дня.
    """
    now = time.time()
    if htx_transfer_cache["data"] and (now - htx_transfer_cache["ts"]) < HTX_TRANSFER_TTL:
        return htx_transfer_cache["data"]

    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.huobi.pro/v2/reference/currencies", timeout=20) as resp:
                if resp.status != 200:
                    debug_stats["last_error"] = f"HTX currencies HTTP {resp.status}"
                    return htx_transfer_cache["data"]  # отдаём старый кэш, если был, лучше чем ничего
                payload = await resp.json()
    except Exception as e:
        debug_stats["last_error"] = f"HTX currencies запрос: {e}"
        return htx_transfer_cache["data"]

    for item in payload.get("data", []):
        try:
            coin = item.get("currency", "").upper()
            chains = item.get("chains", [])
            deposit_ok = any(ch.get("depositStatus") == "allowed" for ch in chains)
            withdraw_ok = any(ch.get("withdrawStatus") == "allowed" for ch in chains)

            best_fee, best_fee_type, best_chain = None, None, None
            for ch in chains:
                if ch.get("withdrawStatus") != "allowed":
                    continue
                fee, ftype = _extract_withdraw_fee(ch)
                if fee is not None and (best_fee is None or fee < best_fee):
                    best_fee, best_fee_type = fee, ftype
                    best_chain = ch.get("displayName") or ch.get("chain")

            result[coin] = {
                "deposit": deposit_ok, "withdraw": withdraw_ok,
                "fee": best_fee, "fee_type": best_fee_type, "fee_chain": best_chain,
            }
        except Exception:
            continue

    if result:
        htx_transfer_cache["ts"] = now
        htx_transfer_cache["data"] = result
        debug_stats["htx_transfer_ok"] = True
    else:
        debug_stats["htx_transfer_ok"] = False

    return htx_transfer_cache["data"]


async def get_htx_depth(symbol_lower):
    """
    Топ стакана HTX (объём на лучшей цене bid/ask) — только по паре, которая уже
    прошла остальные фильтры, поэтому лишним запросом не нагружаем весь цикл.
    Возвращает (bid_qty, ask_qty) в штуках монеты, либо (None, None) при ошибке.
    """
    url = f"https://api.huobi.pro/market/depth?symbol={symbol_lower}&type=step0"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=8) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
        tick = data.get("tick") or {}
        bids = tick.get("bids") or []
        asks = tick.get("asks") or []
        bid_qty = float(bids[0][1]) if bids else None
        ask_qty = float(asks[0][1]) if asks else None
        return bid_qty, ask_qty
    except Exception:
        return None, None


# ================= ОСНОВНОЙ ЦИКЛ =================

async def scanner_task():
    while True:
        try:
            mexc_data, huobi_data = await asyncio.gather(fetch_mexc_data(), fetch_huobi_data())
            htx_transfer = await get_htx_transfer_status()
            mexc_contracts = await get_mexc_contracts()  # {} если ключей нет — это ок
            debug_stats["ts"] = time.time()
            debug_stats["mexc_ok"] = bool(mexc_data)
            debug_stats["huobi_ok"] = bool(huobi_data)
            debug_stats["passed_volume_floor"] = 0
            debug_stats["passed_spread_filter"] = 0
            debug_stats["passed_stability"] = 0
            debug_stats["passed_turnover_filter"] = 0
            debug_stats["passed_transfer_check"] = 0
            debug_stats["blocked_by_transfer"] = 0
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
                    spread_track.pop(pair, None)  # спред упал ниже порога — сбрасываем отсчёт
                    continue
                debug_stats["passed_spread_filter"] += 1

                # ============ ФИЛЬТР СТАБИЛЬНОСТИ СПРЕДА (/ss) ============
                # Если включён (>0): спред должен непрерывно (без единого прохода
                # ниже порога) держаться хотя бы N секунд, прежде чем алерт уйдёт.
                # Если выключен (0): пропускаем сразу же, как раньше.
                if settings["spread_stable_sec"] > 0:
                    first_seen = spread_track.get(pair)
                    if first_seen is None:
                        spread_track[pair] = now
                        continue  # первый раз видим спред выше порога — ждём подтверждения
                    if (now - first_seen) < settings["spread_stable_sec"]:
                        continue  # ещё не набрали нужную длительность
                debug_stats["passed_stability"] += 1

                # Проверяем ТОЛЬКО HTX-плечо (единственное, что видно без ключа):
                # если покупаем на HTX — нужен открытый ВЫВОД с HTX; если продаём
                # на HTX — нужен открытый ВВОД на HTX. MEXC-плечо не проверяется —
                # ключа нет, помечаем как "не проверено" прямо в сообщении.
                base_coin = pair.replace("USDT", "")
                htx_leg = "withdraw" if buy_ex == "HTX" else "deposit"
                htx_coin_status = htx_transfer.get(base_coin)
                htx_known = htx_coin_status is not None
                htx_ok = htx_coin_status.get(htx_leg, False) if htx_known else True  # неизвестно = не блокируем

                if settings["require_transferable"] and htx_known and not htx_ok:
                    debug_stats["blocked_by_transfer"] += 1
                    continue
                debug_stats["passed_transfer_check"] += 1

                leg_name_ru = "вывод" if htx_leg == "withdraw" else "ввод"
                if not htx_known:
                    htx_transfer_label = f"❔ статус {leg_name_ru}а неизвестен"
                elif htx_ok:
                    htx_transfer_label = f"✅ {leg_name_ru.upper()} ОТКРЫТ"
                else:
                    htx_transfer_label = f"❌ {leg_name_ru.upper()} ЗАКРЫТ"

                # Анти-спам: простой кулдаун по времени (спред колеблется вокруг
                # порога чаще, чем растёт монотонно, поэтому x2-правило пампа тут
                # не подходит — просто не спамим чаще N минут по одной паре).
                prev = alert_memory.get(pair)
                if prev and (now - prev["last_msg"]) < settings["cooldown_min"] * 60:
                    continue
                debug_stats["passed_cooldown"] += 1

                # Глубина стакана HTX запрашивается ТОЛЬКО здесь — по паре, которая
                # уже прошла все остальные фильтры (таких мало за проход), поэтому
                # лишний точечный запрос не бьёт по общему бюджету запросов.
                htx_bid_qty, htx_ask_qty = await get_htx_depth(pair.lower())

                if buy_ex == "MEXC":
                    buy_qty, sell_qty = m.get("ask_qty"), htx_bid_qty
                else:
                    buy_qty, sell_qty = htx_ask_qty, m.get("bid_qty")

                def _fmt_qty(q):
                    return f"{q:,.4f}".rstrip('0').rstrip('.') if q is not None else "н/д"

                tradable_usd = None
                if buy_qty is not None and sell_qty is not None:
                    tradable = min(buy_qty, sell_qty)
                    tradable_usd = tradable * buy_price

                # ============ ФИЛЬТР МИН. ОБОРОТА (/mt) ============
                # Если включён (>0): отсекаем пары, где на прокрутку в моменте
                # доступно меньше указанной суммы в $ — иначе спред красивый на
                # бумаге, но исполнить его целиком не получится (стакан тонкий).
                # Если объём с одной из сторон не удалось получить (None) —
                # НЕ блокируем алерт этим фильтром (недостаток данных ≠ отказ),
                # но явно помечаем это в самом сообщении.
                if settings["min_turnover_usd"] > 0 and tradable_usd is not None and tradable_usd < settings["min_turnover_usd"]:
                    continue
                debug_stats["passed_turnover_filter"] += 1

                depth_line = f"📦 Доступно: купить {_fmt_qty(buy_qty)} {base_coin} / продать {_fmt_qty(sell_qty)} {base_coin}"
                if tradable_usd is not None:
                    depth_line += f" → прокрутить ~{_fmt_qty(min(buy_qty, sell_qty))} (~{fmt_money(tradable_usd)}$)"
                else:
                    depth_line += " → сумму для прокрутки посчитать не удалось (нет данных по одной из сторон)"

                alert_memory[pair] = {
                    "time": prev["time"] if prev else now,
                    "last_msg": now,
                    "spread": best_spread,
                }
                debug_stats["alerts_sent"] += 1

                # Комиссия за вывод релевантна, только если реально ВЫВОДИМ с HTX
                # (т.е. купили на HTX и переводим монету на MEXC для продажи).
                fee_line = None
                if htx_leg == "withdraw" and htx_known:
                    fee_amt = htx_coin_status.get("fee")
                    if fee_amt is not None:
                        fee_type = htx_coin_status.get("fee_type") or ""
                        fee_chain = htx_coin_status.get("fee_chain") or "?"
                        fee_usd = fee_amt * buy_price
                        fee_line = (
                            f"💸 Комиссия вывода с HTX ({fee_chain}, {fee_type}): "
                            f"{fee_amt:g} {base_coin} (~{fmt_money(fee_usd)}$)"
                        )

                lines = [
                    f"🔀 <b>СПРЕД: <code>{base_coin}</code></b>",
                    "",
                    f"💹 <b>{best_spread:+.2f}%</b> · Купить на <b>{buy_ex}</b> ({fmt_price(buy_price)}) "
                    f"→ Продать на <b>{sell_ex}</b> ({fmt_price(sell_price)})",
                    "",
                    f"📥 MEXC: bid {fmt_price(m['bid'])} / ask {fmt_price(m['ask'])}",
                    f"📤 HTX: bid {fmt_price(h['bid'])} / ask {fmt_price(h['ask'])}",
                    "",
                    depth_line,
                    "",
                    f"💰 Объём 24ч: MEXC {fmt_money(m['vol'])}$ · HTX {fmt_money(h['vol'])}$",
                    "",
                    f"🚚 Перевод HTX ({leg_name_ru}): {htx_transfer_label}",
                    f"⚠️ Перевод MEXC: не проверяется (нет API-ключа)",
                ]
                if fee_line:
                    lines.append(fee_line)

                # Контракт MEXC — реальные данные, если заданы MEXC_API_KEY/SECRET;
                # HTX публичного источника контрактов не имеет, поэтому сравнить
                # напрямую не можем — но показываем адрес MEXC, чтобы можно было
                # быстро сверить его на странице пополнения на HTX вручную.
                coin_networks = mexc_contracts.get(base_coin)
                if coin_networks:
                    top_nets = coin_networks[:3]
                    nets_str = "; ".join(f"{n['network']}: {n['contract']}" for n in top_nets)
                    lines.append(f"🔗 MEXC контракт: {nets_str}")
                    lines.append("<i>Сверь этот адрес на странице пополнения HTX вручную — авто-сверки с HTX нет (нет публичного источника контрактов).</i>")
                elif MEXC_API_KEY:
                    lines.append("🔗 Контракт MEXC: не найден в ответе API для этой монеты")
                else:
                    lines.append("🔗 Проверка контракта: выключена (не задан MEXC_API_KEY/SECRET)")
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
