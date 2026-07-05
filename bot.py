import asyncio
import json
import os
import random
import re
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.enums import ChatType
from aiogram.utils.markdown import hbold

# ========== КОНФИГ ==========
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment")

ADMIN_IDS = [5454585281]

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
GIF_FILE = os.path.join(os.path.dirname(__file__), "gif_id.txt")

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = Bot(token=TOKEN)
dp = Dispatcher()


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
    else:
        d = {}
    d.setdefault("users", {})
    d.setdefault("bonus_cooldown", {})
    d.setdefault("tournaments", {})
    d.setdefault("next_tournament_id", 1)
    d.setdefault("profiles", {})
    d.setdefault("clans", {})
    d.setdefault("next_clan_id", 1)
    d.setdefault("roulette_history", {})
    d.setdefault("active_roulette_bets", {})
    d.setdefault("active_first_bet_time", {})
    d.setdefault("roulette_lose_chance", 0)
    return d


def get_profile(user_id: int) -> dict:
    uid = str(user_id)
    if uid not in data["profiles"]:
        data["profiles"][uid] = {
            "house": None,
            "clan_id": None,
            "duel_wins": 0,
            "duel_losses": 0,
            "duel_draws": 0,
            "lang": "ru",
        }
    return data["profiles"][uid]


def record_duel(winner_id: int, loser_id: int, draw: bool = False):
    if draw:
        get_profile(winner_id)["duel_draws"] += 1
        get_profile(loser_id)["duel_draws"] += 1
    else:
        get_profile(winner_id)["duel_wins"] += 1
        get_profile(loser_id)["duel_losses"] += 1
    save_data(data)


def get_user_rank(user_id: int) -> int:
    uid = str(user_id)
    sorted_users = sorted(data["users"].items(), key=lambda x: x[1], reverse=True)
    for i, (u, _) in enumerate(sorted_users, 1):
        if u == uid:
            return i
    return len(data["users"]) + 1


HOUSES = [
    ("🦁", "Гриффиндор"),
    ("🐍", "Слизерин"),
    ("🦅", "Когтевран"),
    ("🦡", "Пуффендуй"),
]


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


def fmt(n: int) -> str:
    """Форматирует число с пробелами: 1 000 / 10 000 / 100 000"""
    return f"{int(n):,}".replace(",", "\u00a0")


data = load_data()

waiting_for_gif = False

# Рулетка: состояние per-chat (восстанавливается из data.json после перезапуска)
def _restore_bets(raw: dict) -> dict:
    """JSON хранит list-of-lists, восстанавливаем в dict[int, list[tuple]]."""
    result = {}
    for k, entries in raw.items():
        result[int(k)] = [tuple(e) for e in entries]
    return result

roulette_bets: dict = _restore_bets(data.get("active_roulette_bets", {}))
first_bet_time: dict = {
    int(k): datetime.fromisoformat(v)
    for k, v in data.get("active_first_bet_time", {}).items()
}
roulette_spinning: dict = {}    # не сохраняем — после перезапуска считаем чистым
last_round_bets: dict = {}      # (chat_id, user_id) → [(amount, bet_type, bet_value, multiplier)]

# История из data.json, ключи → int chat_id
roulette_history: dict = {int(k): v for k, v in data.get("roulette_history", {}).items()}
roulette_lose_chance: int = data.get("roulette_lose_chance", 0)  # 0–100
waiting_for_roulette_chance: bool = False


def save_roulette_state():
    """Сохраняет активные ставки и время первой ставки в data.json."""
    data["active_roulette_bets"] = {
        str(cid): [list(b) for b in bets]
        for cid, bets in roulette_bets.items()
    }
    data["active_first_bet_time"] = {
        str(cid): t.isoformat()
        for cid, t in first_bet_time.items()
    }
    save_data(data)

# duel_challenges[chat_id][(challenger_id, target_id)] = {amount, expires}
duel_challenges: dict = {}

# Блокировки per-chat для защиты от race conditions
_chat_locks: dict = {}

def get_chat_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_user_balance(user_id: int) -> int:
    uid = str(user_id)
    if uid not in data["users"]:
        data["users"][uid] = 0
        save_data(data)
    return data["users"][uid]


def set_user_balance(user_id: int, amount: int):
    uid = str(user_id)
    data["users"][uid] = amount
    save_data(data)


def add_balance(user_id: int, amount: int):
    new_bal = max(0, get_user_balance(user_id) + amount)
    set_user_balance(user_id, new_bal)


def can_take_bonus(user_id: int) -> Tuple[bool, Optional[int]]:
    uid = str(user_id)
    if user_id in ADMIN_IDS:
        return True, None
    last = data["bonus_cooldown"].get(uid)
    if not last:
        return True, None
    last_time = datetime.fromisoformat(last)
    if datetime.now() >= last_time + timedelta(hours=24):
        return True, None
    remaining = int((last_time + timedelta(hours=24) - datetime.now()).total_seconds())
    return False, remaining


def set_bonus_taken(user_id: int):
    if user_id not in ADMIN_IDS:
        data["bonus_cooldown"][str(user_id)] = datetime.now().isoformat()
        save_data(data)


def mention(user_id: int, name: str) -> str:
    safe = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def parse_bet_type(token: str) -> Optional[Tuple[str, any, float]]:
    """Парсит один тип ставки без суммы. Возвращает (bet_type, bet_value, multiplier) или None."""
    t = token.lower().strip()
    if t in ["красное", "red", "к", "кр"]:
        return ("color", "red", 2.0)
    if t in ["черное", "чёрное", "black", "ч", "чр"]:
        return ("color", "black", 2.0)
    if t in ["odd", "нечет", "нечетное", "н", "нч", "одд"]:
        return ("parity", "odd", 2.0)
    if t in ["even", "чет", "четное", "чт", "евен"]:
        return ("parity", "even", 2.0)
    if t == "0":
        return ("number", 0, 35.0)
    if t.isdigit() and 0 <= int(t) <= 36:
        return ("number", int(t), 35.0)
    m = re.match(r"^(\d+)-(\d+)$", t)
    if m:
        low, high = int(m.group(1)), int(m.group(2))
        if 0 <= low <= high <= 36:
            count = high - low + 1
            return ("range", (low, high), 36.0 / count)
    return None


def parse_bet(text: str) -> Optional[Tuple[int, str, any, float]]:
    """Обратная совместимость: парсит одиночную ставку «сумма тип»."""
    parts = text.lower().strip().split()
    if len(parts) < 2:
        return None
    try:
        amount = int(parts[0])
    except Exception:
        return None
    result = parse_bet_type(" ".join(parts[1:]))
    if result is None:
        return None
    bet_type, bet_value, multiplier = result
    return (amount, bet_type, bet_value, multiplier)


def parse_multi_bet(text: str) -> Optional[Tuple[int, list]]:
    """Парсит мульти-ставку: «сумма тип1 тип2 тип3 ...»
    Возвращает (amount_per_bet, [(bet_type, bet_value, multiplier), ...]) или None.
    """
    parts = text.strip().split()
    if len(parts) < 2:
        return None
    try:
        amount = int(parts[0])
    except Exception:
        return None
    if amount <= 0:
        return None

    bets = []
    for token in parts[1:]:
        result = parse_bet_type(token)
        if result is None:
            return None  # неизвестный токен — не мульти-ставка
        bets.append(result)

    if not bets:
        return None
    return (amount, bets)


def calculate_win(number: int, bet_type: str, bet_value, multiplier: float) -> bool:
    reds = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
    if bet_type == "number":
        return number == bet_value
    elif bet_type == "color":
        if number == 0:
            return False
        if bet_value == "red":
            return number in reds
        else:
            return number not in reds and number != 0
    elif bet_type == "parity":
        if number == 0:
            return False
        if bet_value == "odd":
            return number % 2 == 1
        else:
            return number % 2 == 0
    elif bet_type == "range":
        low, high = bet_value
        return low <= number <= high
    return False


def get_color_symbol(number: int) -> str:
    if number == 0:
        return "🟢"
    reds = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
    return "🔴" if number in reds else "⚫️"


def add_tournament_score(user_id: int, profit: int):
    """Начисляет очки участнику всех активных турниров."""
    for tour in data.get("tournaments", {}).values():
        if tour["status"] == "active" and user_id in tour["participants"]:
            scores = tour.setdefault("scores", {})
            scores[str(user_id)] = scores.get(str(user_id), 0) + profit
    save_data(data)


def bet_label_str(bt, bv) -> str:
    if bt == "color":
        return "RED" if bv == "red" else "BLACK"
    if bt == "parity":
        return "ODD" if bv == "odd" else "EVEN"
    if bt == "number":
        return str(bv)
    if bt == "range":
        return f"{bv[0]}-{bv[1]}"
    return str(bv)


async def process_roulette(chat_id: int):
    bets = roulette_bets.get(chat_id, [])
    if not bets:
        await bot.send_message(chat_id, "Нет ставок для розыгрыша.")
        return

    gif_id = None
    if os.path.exists(GIF_FILE):
        with open(GIF_FILE, "r") as f:
            gif_id = f.read().strip()

    if gif_id:
        gif_msg = await bot.send_animation(chat_id, gif_id)
        await asyncio.sleep(3)
        await bot.delete_message(chat_id, gif_msg.message_id)
        await asyncio.sleep(0.5)
    else:
        await bot.send_message(chat_id, "🎰 Вращаем рулетку...")
        await asyncio.sleep(2)

    result_num = random.randint(0, 36)
    color_symbol = get_color_symbol(result_num)

    # Записываем в историю (максимум 10) и сохраняем в файл
    if chat_id not in roulette_history:
        roulette_history[chat_id] = []
    roulette_history[chat_id].append((result_num, color_symbol))
    if len(roulette_history[chat_id]) > 9:
        roulette_history[chat_id].pop(0)
    data["roulette_history"][str(chat_id)] = roulette_history[chat_id]
    save_data(data)

    # Сохраняем ставки для кнопок Повторить/Удвоить
    per_user: dict = {}
    for user_id, amount, bet_type, bet_value, multiplier in bets:
        per_user.setdefault(user_id, []).append(
            (amount, bet_type, bet_value, multiplier)
        )
    for uid, entries in per_user.items():
        last_round_bets[(chat_id, uid)] = entries

    # Подсчёт результатов
    bet_results = []  # (user_id, amount, bet_type, bet_value, won, change)
    for user_id, amount, bet_type, bet_value, multiplier in bets:
        win = calculate_win(result_num, bet_type, bet_value, multiplier)
        # roulette_lose_chance: 0=всегда выигрыш, 50=норма, 100=всегда проигрыш
        if roulette_lose_chance < 50:
            # Форс-выигрыш на проигрышные ставки
            force_win_chance = (50 - roulette_lose_chance) * 2  # 0→100%, 25→50%, 49→2%
            if not win and random.randint(1, 100) <= force_win_chance:
                win = True
        elif roulette_lose_chance > 50:
            # Форс-проигрыш на выигрышные ставки
            force_lose_chance = (roulette_lose_chance - 50) * 2  # 100→100%, 75→50%, 51→2%
            if win and random.randint(1, 100) <= force_lose_chance:
                win = False
        if win:
            win_amount = int(amount * multiplier)
            add_balance(user_id, win_amount)
            profit = win_amount - amount
            bet_results.append((user_id, amount, bet_type, bet_value, True, profit))
            add_tournament_score(user_id, profit)
        else:
            bet_results.append((user_id, amount, bet_type, bet_value, False, -amount))

    # Кэш имён
    names: dict = {}
    for user_id, *_ in bet_results:
        if user_id not in names:
            try:
                u = await bot.get_chat(user_id)
                names[user_id] = u.first_name
            except Exception:
                names[user_id] = str(user_id)

    await send_roulette_result(
        chat_id, result_num, color_symbol, bet_results, names, per_user
    )

    # Сброс состояния этого чата
    roulette_bets.pop(chat_id, None)
    first_bet_time.pop(chat_id, None)
    roulette_spinning.pop(chat_id, None)
    save_roulette_state()


# ========== ОБРАБОТЧИКИ ==========
def main_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [
                types.KeyboardButton(text="👤 Профиль"),
                types.KeyboardButton(text="🔮 Хогвартс"),
            ],
            [
                types.KeyboardButton(text="📋 Команды"),
                types.KeyboardButton(text="🛒 Донат"),
            ],
            [types.KeyboardButton(text="🏆 Турниры")],
            [
                types.KeyboardButton(text="💬 Чаты"),
                types.KeyboardButton(text="🏰 Кланы"),
            ],
            [
                types.KeyboardButton(text="🎮 Игры"),
                types.KeyboardButton(text="🎁 Бонус"),
            ],
            [
                types.KeyboardButton(text="Политика"),
                types.KeyboardButton(text="Изменить язык"),
            ],
        ],
        resize_keyboard=True,
    )


@dp.message(Command("start"))
async def cmd_start(message: Message):
    me = await bot.get_me()
    inline_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Добавить бота в чат",
                    url=f"https://t.me/{me.username}?startgroup=start",
                )
            ]
        ]
    )
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "GRAM — развлекательный бот для вашего чата:\n"
        "• ⚔️ Создание своего клана\n"
        "• 🏆 Участие в турнирах\n"
        "• 🎮 Мини-игры\n"
        "• 🤺 Дуэли\n\n"
        "Запуская бота, вы соглашаетесь с условиями использования.",
        reply_markup=inline_kb,
    )
    await message.answer("Выберите раздел:", reply_markup=main_keyboard())


@dp.message(Command("adm"))
async def cmd_adm(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.reply("Команда доступна только в личных сообщениях с ботом.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Загрузить гифку для рулетки", callback_data="upload_gif"
                )
            ],
            [InlineKeyboardButton(text="💰 Выдать GRAM", callback_data="give_gram")],
            [InlineKeyboardButton(text="💸 Забрать GRAM", callback_data="take_gram")],
            [
                InlineKeyboardButton(
                    text="🏆 Создать турнир", callback_data="create_tournament"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔚 Завершить турнир", callback_data="end_tournament"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎲 Изменить шанс проигрыша рулетки", callback_data="roulette_chance"
                )
            ],
        ]
    )
    await message.answer(
        f"🔧 Админ-панель\n\nТекущий шанс проигрыша: <b>{roulette_lose_chance}%</b>",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.callback_query(
    lambda c: c.data
    in ["upload_gif", "give_gram", "take_gram", "create_tournament", "end_tournament", "roulette_chance"]
)
async def admin_callbacks(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    if callback.data == "upload_gif":
        global waiting_for_gif
        waiting_for_gif = True
        await callback.message.answer("Отправьте гифку (одну, она сохранится навсегда)")
        await callback.answer()
    elif callback.data == "give_gram":
        await callback.message.answer(
            "Введите: `/givegram @username 1000` или `/givegram 123456789 1000`",
            parse_mode="Markdown",
        )
        await callback.answer()
    elif callback.data == "take_gram":
        await callback.message.answer(
            "Введите: `/takegram @username 500`", parse_mode="Markdown"
        )
        await callback.answer()
    elif callback.data == "create_tournament":
        await callback.message.answer(
            "🏆 Создать турнир (рулеточный чемпионат):\n"
            "<code>/турнир [взнос] [макс_мест] [название]</code>\n\n"
            "Пример: <code>/турнир 500 10 Гран-при</code>\n"
            "Бесплатный: <code>/турнир 0 20 Летний кубок</code>\n\n"
            "После набора: <code>/старт [id]</code>\n"
            "Завершить: <code>/завершить [id]</code>\n"
            "Удалить: <code>/удалить [id]</code>",
            parse_mode="HTML",
        )
        await callback.answer()
    elif callback.data == "end_tournament":
        tours = data.get("tournaments", {})
        all_active = {
            tid: t for tid, t in tours.items() if t["status"] in ("open", "active")
        }
        if not all_active:
            await callback.message.answer("Нет активных турниров.")
        else:
            lines = ["<b>Управление турнирами:</b>\n"]
            for tid, t in all_active.items():
                status = "🟢 Набор" if t["status"] == "open" else "🔴 Идёт"
                lines.append(
                    f"• #{tid} — {t['name']} {status} | {len(t['participants'])}/{t['max_slots']} мест\n"
                    f"  <code>/старт {tid}</code> | <code>/завершить {tid}</code> | <code>/удалить {tid}</code>"
                )
            await callback.message.answer("\n\n".join(lines), parse_mode="HTML")
        await callback.answer()
    elif callback.data == "roulette_chance":
        global waiting_for_roulette_chance
        waiting_for_roulette_chance = True
        await callback.message.answer(
            f"Текущее значение: <b>{roulette_lose_chance}</b>\n\n"
            "Введите число от 0 до 100:\n"
            "• 0 — всегда выигрыш\n"
            "• 50 — стандартная игра\n"
            "• 100 — всегда проигрыш",
            parse_mode="HTML",
        )
        await callback.answer()


@dp.callback_query(
    lambda c: c.data and (c.data.startswith("rpt:") or c.data.startswith("dbl:"))
)
async def repeat_double_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    action = parts[0]
    chat_id = int(parts[1])
    user_id = callback.from_user.id

    saved = last_round_bets.get((chat_id, user_id))
    if not saved:
        await callback.answer("Вы не играли в прошлом раунде", show_alert=True)
        return

    if roulette_spinning.get(chat_id):
        await callback.answer("Раунд уже идёт — подожди.", show_alert=True)
        return

    multiplier_factor = 2 if action == "dbl" else 1
    new_bets = [
        (amount * multiplier_factor, bt, bv, mult) for amount, bt, bv, mult in saved
    ]
    total_cost = sum(a for a, *_ in new_bets)
    balance = get_user_balance(user_id)

    if total_cost > balance:
        await callback.answer("Недостаточно GRAM на балансе", show_alert=True)
        return

    add_balance(user_id, -total_cost)

    if chat_id not in roulette_bets:
        roulette_bets[chat_id] = []
    if chat_id not in first_bet_time:
        first_bet_time[chat_id] = datetime.now()

    for amount, bet_type, bet_value, mult in new_bets:
        roulette_bets[chat_id].append((user_id, amount, bet_type, bet_value, mult))
    save_roulette_state()

    name = mention(user_id, callback.from_user.first_name)
    lines = "\n".join(
        f"Ставка принята: {name} {fmt(a)} GRAM на {bet_label_str(bt, bv)}"
        for a, bt, bv, _ in new_bets
    )
    await callback.message.reply(lines, parse_mode="HTML")
    await callback.answer()


@dp.message(lambda msg: msg.text and msg.text.startswith("/givegram"))
async def give_gram(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("Формат: /givegram @username 1000")
        return
    try:
        amount = int(parts[-1])
        target = parts[-2]
        if target.startswith("@"):
            try:
                chat = await bot.get_chat(target)
                user_id = chat.id
            except Exception:
                await message.reply("Пользователь не найден")
                return
        else:
            user_id = int(target)
        add_balance(user_id, amount)
        await message.reply(f"✅ Выдано {fmt(amount)} GRAM пользователю {target}")
    except Exception:
        await message.reply("Ошибка в формате")


@dp.message(lambda msg: msg.text and msg.text.startswith("/takegram"))
async def take_gram(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.reply("Формат: /takegram @username 500")
        return
    try:
        amount = int(parts[-1])
        target = parts[-2]
        if target.startswith("@"):
            chat = await bot.get_chat(target)
            user_id = chat.id
        else:
            user_id = int(target)
        current = get_user_balance(user_id)
        new_bal = max(0, current - amount)
        set_user_balance(user_id, new_bal)
        await message.reply(
            f"✅ Забрано {fmt(amount)} GRAM у {target}. Новый баланс: {fmt(new_bal)}"
        )
    except Exception:
        await message.reply("Ошибка")


@dp.message(lambda msg: msg.text and re.match(r"^п(\s|$)", msg.text.lower()))
async def transfer_gram(message: Message):
    sender_id = message.from_user.id
    sender_name = message.from_user.first_name
    parts = message.text.split()

    # Формат 1: ответ на сообщение → п 1000
    # Формат 2: по айди              → п 123456789 1000
    comment = ""
    if message.reply_to_message:
        if len(parts) < 2:
            await message.reply(
                "❗ Формат: ответь на сообщение и напиши <code>п 1000</code>",
                parse_mode="HTML",
            )
            return
        try:
            amount = int(parts[1])
        except ValueError:
            await message.reply(
                "❗ Укажи сумму числом: <code>п 1000</code>", parse_mode="HTML"
            )
            return
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name
        comment = " ".join(parts[2:])
    else:
        if len(parts) < 3:
            await message.reply(
                "❗ Форматы передачи:\n"
                "• Ответь на сообщение: <code>п 1000</code>\n"
                "• По айди: <code>п 123456789 1000</code>",
                parse_mode="HTML",
            )
            return
        try:
            target_id = int(parts[1])
            amount = int(parts[2])
        except ValueError:
            await message.reply(
                "❗ Укажи айди и сумму числами: <code>п 123456789 1000</code>",
                parse_mode="HTML",
            )
            return
        try:
            target_chat = await bot.get_chat(target_id)
            target_name = target_chat.first_name
        except Exception:
            await message.reply("❌ Пользователь с таким айди не найден.")
            return
        comment = " ".join(parts[3:])

    if target_id == sender_id:
        await message.reply("🤡 Нельзя передать GRAM самому себе.")
        return
    if amount <= 0:
        await message.reply("❗ Сумма должна быть больше 0.")
        return

    sender_balance = get_user_balance(sender_id)
    if amount > sender_balance:
        await message.reply("Недостаточно GRAM на балансе")
        return

    add_balance(sender_id, -amount)
    add_balance(target_id, amount)

    text = (
        f"{mention(sender_id, sender_name)} перевел {fmt(amount)} GRAM"
        f" {mention(target_id, target_name)}"
    )
    if comment:
        text += f"\n<blockquote>💬 {comment}</blockquote>"

    await message.reply(text, parse_mode="HTML")


@dp.message(lambda msg: msg.text and msg.text.lower() in ["б", "b", "в"])
async def check_balance_short(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name
    balance = get_user_balance(user_id)
    await message.answer(
        f"{mention(user_id, name)}\n💰 Баланс: <b>{fmt(balance)}</b> GRAM",
        parse_mode="HTML",
    )


@dp.message(
    lambda msg: msg.text
    and re.match(r"^дуэль\s+", msg.text.lower())
    and msg.chat.type != ChatType.PRIVATE
)
async def duel_challenge(message: Message):
    challenger_id = message.from_user.id
    challenger_name = message.from_user.first_name
    chat_id = message.chat.id

    parts = message.text.split()
    if len(parts) < 3:
        await message.reply(
            "❗ Формат: <code>дуэль @username 500</code>", parse_mode="HTML"
        )
        return

    raw_target = parts[1]
    try:
        amount = int(parts[2])
    except ValueError:
        await message.reply(
            "❗ Укажи сумму числом: <code>дуэль @username 500</code>", parse_mode="HTML"
        )
        return

    if amount <= 0:
        await message.reply("❗ Сумма должна быть больше 0.")
        return

    challenger_balance = get_user_balance(challenger_id)
    if amount > challenger_balance:
        await message.reply("Недостаточно GRAM на балансе")
        return

    # Определяем цель
    target_id = None
    target_name = None

    # Если ответ на сообщение — берём из reply
    if message.reply_to_message and raw_target.lower() in ["@", "."]:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name
    elif raw_target.startswith("@"):
        try:
            chat = await bot.get_chat(raw_target)
            target_id = chat.id
            target_name = chat.first_name
        except Exception:
            await message.reply("❌ Пользователь не найден.")
            return
    else:
        await message.reply(
            "❗ Формат: <code>дуэль @username 500</code>", parse_mode="HTML"
        )
        return

    if target_id == challenger_id:
        await message.reply("🤡 Нельзя вызвать самого себя.")
        return

    target_balance = get_user_balance(target_id)
    if amount > target_balance:
        await message.reply(
            f"❌ У {mention(target_id, target_name)} недостаточно GRAM (баланс: {fmt(target_balance)}).",
            parse_mode="HTML",
        )
        return

    # Сохраняем вызов
    if chat_id not in duel_challenges:
        duel_challenges[chat_id] = {}
    duel_challenges[chat_id][(challenger_id, target_id)] = {
        "amount": amount,
        "expires": datetime.now() + timedelta(seconds=60),
    }

    await message.reply(
        f"⚔️ {mention(challenger_id, challenger_name)} вызывает {mention(target_id, target_name)} на дуэль!\n"
        f"💰 Ставка: <b>{fmt(amount)} GRAM</b>\n\n"
        f"{mention(target_id, target_name)}, напиши <b>принять</b> в течение 60 секунд.",
        parse_mode="HTML",
    )


@dp.message(
    lambda msg: msg.text
    and msg.text.lower() == "принять"
    and msg.chat.type != ChatType.PRIVATE
)
async def duel_accept(message: Message):
    target_id = message.from_user.id
    target_name = message.from_user.first_name
    chat_id = message.chat.id

    if chat_id not in duel_challenges:
        return

    # Ищем вызов для этого пользователя
    challenge_key = None
    challenge_data = None
    for (c_id, t_id), cdata in list(duel_challenges[chat_id].items()):
        if t_id == target_id:
            if datetime.now() > cdata["expires"]:
                del duel_challenges[chat_id][(c_id, t_id)]
                await message.reply("⏰ Время дуэли истекло.")
                return
            challenge_key = (c_id, t_id)
            challenge_data = cdata
            break

    if not challenge_key:
        return

    challenger_id = challenge_key[0]
    amount = challenge_data["amount"]

    # Финальная проверка балансов
    if get_user_balance(challenger_id) < amount:
        del duel_challenges[chat_id][challenge_key]
        try:
            challenger = await bot.get_chat(challenger_id)
            c_name = challenger.first_name
        except Exception:
            c_name = str(challenger_id)
        await message.reply(
            f"❌ У {mention(challenger_id, c_name)} больше нет достаточно GRAM для дуэли.",
            parse_mode="HTML",
        )
        return
    if get_user_balance(target_id) < amount:
        del duel_challenges[chat_id][challenge_key]
        await message.reply("Недостаточно GRAM на балансе")
        return

    del duel_challenges[chat_id][challenge_key]

    # Списываем ставки
    add_balance(challenger_id, -amount)
    add_balance(target_id, -amount)

    try:
        challenger = await bot.get_chat(challenger_id)
        c_name = challenger.first_name
    except Exception:
        c_name = str(challenger_id)

    await message.reply("⚔️ Дуэль начинается! Бросаем кубики...")
    await asyncio.sleep(2)

    c_roll = random.randint(1, 6)
    t_roll = random.randint(1, 6)
    pot = amount * 2

    result = (
        f"🎲 {mention(challenger_id, c_name)}: <b>{c_roll}</b>\n"
        f"🎲 {mention(target_id, target_name)}: <b>{t_roll}</b>\n\n"
    )

    if c_roll > t_roll:
        add_balance(challenger_id, pot)
        record_duel(challenger_id, target_id)
        result += f"🏆 Победитель: {mention(challenger_id, c_name)} +{fmt(pot)} GRAM!"
    elif t_roll > c_roll:
        add_balance(target_id, pot)
        record_duel(target_id, challenger_id)
        result += f"🏆 Победитель: {mention(target_id, target_name)} +{fmt(pot)} GRAM!"
    else:
        # Ничья — возвращаем ставки
        add_balance(challenger_id, amount)
        add_balance(target_id, amount)
        record_duel(challenger_id, target_id, draw=True)
        result += "🤝 Ничья! Ставки возвращены."

    await message.reply(result, parse_mode="HTML")


@dp.message(Command("top"))
@dp.message(lambda msg: msg.text and msg.text.lower() == "топ")
async def cmd_top(message: Message):
    users = data.get("users", {})
    if not users:
        await message.reply("📊 Пока никто не заработал GRAM.")
        return

    sorted_users = sorted(users.items(), key=lambda x: x[1], reverse=True)[:10]

    lines = ["🏆 <b>Топ-10 богатейших игроков</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, balance) in enumerate(sorted_users):
        try:
            user = await bot.get_chat(int(uid))
            name = user.first_name
        except Exception:
            name = f"Игрок {uid}"
        icon = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{icon} {mention(int(uid), name)} — <b>{fmt(balance)}</b> GRAM")

    await message.reply("\n".join(lines), parse_mode="HTML")


@dp.message(
    lambda msg: msg.text
    and msg.text.lower() == "лог"
    and msg.chat.type != ChatType.PRIVATE
)
async def cmd_roulette_log(message: Message):
    chat_id = message.chat.id
    history = roulette_history.get(chat_id, [])
    if not history:
        await message.reply("📜 История ещё пуста — сыграйте хотя бы один раунд.")
        return
    lines = [f"{num}{sym}" for num, sym in reversed(history[-9:])]
    await message.reply("\n".join(lines))


@dp.message(
    lambda msg: msg.text
    and msg.text.lower() == "ставки"
    and msg.chat.type != ChatType.PRIVATE
)
async def cmd_my_bets(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    bets = [b for b in roulette_bets.get(chat_id, []) if b[0] == user_id]
    if not bets:
        name = mention(user_id, message.from_user.first_name)
        await message.answer(f"Не найдено ни одной активной ставки {name}", parse_mode="HTML")
        return

    name = mention(user_id, message.from_user.first_name)
    lines = [f"Ставка: {name} {fmt(b[1])} GRAM на {bet_label_str(b[2], b[3])}" for b in bets]
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(
    lambda msg: msg.text
    and msg.text.lower() == "отмена"
    and msg.chat.type != ChatType.PRIVATE
)
async def cmd_cancel_bets(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if roulette_spinning.get(chat_id):
        await message.reply("Раунд уже идёт — отмена невозможна.")
        return

    all_bets = roulette_bets.get(chat_id, [])
    player_bets = [b for b in all_bets if b[0] == user_id]
    if not player_bets:
        name = mention(user_id, message.from_user.first_name)
        await message.answer(f"Не найдено ни одной активной ставки {name}", parse_mode="HTML")
        return

    refund = sum(b[1] for b in player_bets)
    roulette_bets[chat_id] = [b for b in all_bets if b[0] != user_id]
    if not roulette_bets[chat_id]:
        roulette_bets.pop(chat_id, None)
        first_bet_time.pop(chat_id, None)
    add_balance(user_id, refund)
    save_roulette_state()
    name = mention(user_id, message.from_user.first_name)
    await message.answer(f"Ставки отменены {name}", parse_mode="HTML")


def looks_like_bet(text: str) -> bool:
    parts = text.strip().split()
    if len(parts) < 2:
        return False
    try:
        int(parts[0])
        return True
    except ValueError:
        return False


@dp.message(
    lambda msg: msg.chat.type != ChatType.PRIVATE
    and msg.text
    and looks_like_bet(msg.text)
)
async def handle_roulette_bet(message: Message):
    """Ставки принимаются в любой момент — рулетку запускает «го»."""
    chat_id = message.chat.id

    multi = parse_multi_bet(message.text)
    if not multi:
        return

    amount, bets = multi
    user_id = message.from_user.id
    total_cost = amount * len(bets)

    async with get_chat_lock(chat_id):
        # Если идёт розыгрыш — ставки уже не принимаем
        if roulette_spinning.get(chat_id):
            return

        balance = get_user_balance(user_id)
        if total_cost > balance:
            await message.reply("Недостаточно GRAM на балансе")
            return

        add_balance(user_id, -total_cost)

        if chat_id not in roulette_bets:
            roulette_bets[chat_id] = []
        for bet_type, bet_value, multiplier in bets:
            roulette_bets[chat_id].append(
                (user_id, amount, bet_type, bet_value, multiplier)
            )

        if chat_id not in first_bet_time:
            first_bet_time[chat_id] = datetime.now()
        save_roulette_state()

    name = mention(message.from_user.id, message.from_user.first_name)
    bet_lines = "\n".join(
        f"Ставка принята: {name} {fmt(amount)} GRAM на {bet_label_str(bt, bv)}"
        for bt, bv, _ in bets
    )
    await message.reply(bet_lines, parse_mode="HTML")


def sec_word(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "секунд"
    r = n % 10
    if r == 1:
        return "секунда"
    if 2 <= r <= 4:
        return "секунды"
    return "секунд"


@dp.message(
    lambda msg: msg.text
    and msg.text.lower() in ["го", "go"]
    and msg.chat.type != ChatType.PRIVATE
)
async def trigger_roulette(message: Message):
    chat_id = message.chat.id

    should_spin = False
    async with get_chat_lock(chat_id):
        if roulette_spinning.get(chat_id):
            return

        bets = roulette_bets.get(chat_id, [])
        if not bets:
            await message.answer("Невозможно начать игру без ставок.")
            return

        elapsed = (datetime.now() - first_bet_time[chat_id]).total_seconds()
        wait_needed = 13 - elapsed
        if wait_needed > 0:
            await message.answer(
                f"Ошибка. Закончить раунд можно через {int(wait_needed) + 1} секунд"
            )
            return

        roulette_spinning[chat_id] = True
        should_spin = True

    if should_spin:
        await process_roulette(chat_id)


@dp.message(lambda msg: msg.text and waiting_for_roulette_chance and msg.from_user.id in ADMIN_IDS)
async def set_roulette_chance(message: Message):
    global waiting_for_roulette_chance, roulette_lose_chance
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.reply("Введите целое число от 0 до 100.")
        return
    if not 0 <= val <= 100:
        await message.reply("Число должно быть от 0 до 100.")
        return
    roulette_lose_chance = val
    data["roulette_lose_chance"] = val
    save_data(data)
    waiting_for_roulette_chance = False
    await message.reply(f"✅ Шанс проигрыша установлен: <b>{val}%</b>", parse_mode="HTML")


@dp.message(lambda msg: msg.animation and waiting_for_gif)
async def save_gif(message: Message):
    global waiting_for_gif
    if message.from_user.id not in ADMIN_IDS:
        return
    file_id = message.animation.file_id
    with open(GIF_FILE, "w") as f:
        f.write(file_id)
    waiting_for_gif = False
    await message.reply("✅ Гифка сохранена навсегда! Будет использоваться в рулетке.")


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("📱 Меню бота:", reply_markup=main_keyboard())


# ========== ТУРНИРЫ ==========
# Механика: участники крутят рулетку → прибыль от выигрышей = очки
# Кто наберёт больше очков к моменту завершения — забирает призовой фонд.
# Статусы: open (набор) → active (идёт, очки считаются) → finished


@dp.message(lambda msg: msg.text and msg.text.startswith("/турнир"))
async def create_tournament(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    # Формат: /турнир [взнос] [макс_мест] [название]
    parts = message.text.split(maxsplit=3)
    if len(parts) < 4:
        await message.reply(
            "❗ Формат: <code>/турнир [взнос] [макс_мест] [название]</code>\n"
            "Пример: <code>/турнир 500 10 Гран-при</code>\n"
            "Взнос 0 = бесплатно | Макс. мест = кол-во участников",
            parse_mode="HTML",
        )
        return
    try:
        entry_fee = int(parts[1])
        max_slots = int(parts[2])
    except ValueError:
        await message.reply("❗ Взнос и кол-во мест должны быть числами.")
        return
    if entry_fee < 0 or max_slots < 2:
        await message.reply("❗ Взнос ≥ 0, мест минимум 2.")
        return

    name = parts[3].strip()
    tid = str(data["next_tournament_id"])
    data["next_tournament_id"] += 1
    data["tournaments"][tid] = {
        "name": name,
        "entry_fee": entry_fee,
        "prize_pool": 0,
        "max_slots": max_slots,
        "participants": [],
        "scores": {},
        "status": "open",
        "winner_id": None,
        "created_by": message.from_user.id,
    }
    save_data(data)

    fee_text = (
        f"Взнос: <b>{fmt(entry_fee)} GRAM</b>" if entry_fee > 0 else "Бесплатный вход"
    )
    await message.reply(
        f"🏆 Турнир <b>#{tid} — {name}</b> создан!\n"
        f"{fee_text} | Мест: <b>{max_slots}</b>\n\n"
        f"Набор участников: <code>уч {tid}</code>\n"
        f"Старт (после набора): <code>/старт {tid}</code>\n"
        f"Удалить: <code>/удалить {tid}</code>",
        parse_mode="HTML",
    )


@dp.message(lambda msg: msg.text and msg.text.startswith("/старт"))
async def start_tournament(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❗ Формат: <code>/старт [id]</code>", parse_mode="HTML")
        return
    tid = parts[1]
    tour = data["tournaments"].get(tid)
    if not tour:
        await message.reply(f"❌ Турнир #{tid} не найден.")
        return
    if tour["status"] != "open":
        await message.reply(
            f"❌ Турнир #{tid} уже {'идёт' if tour['status'] == 'active' else 'завершён'}."
        )
        return
    if len(tour["participants"]) < 2:
        await message.reply("❌ Нужно минимум 2 участника для старта.")
        return

    tour["status"] = "active"
    save_data(data)

    p_count = len(tour["participants"])
    await message.reply(
        f"🚀 Турнир <b>{tour['name']}</b> стартовал!\n"
        f"👥 Участников: {p_count} | 💰 Призовой фонд: {fmt(tour['prize_pool'])} GRAM\n\n"
        f"🎰 Участники крутят рулетку — прибыль от выигрышей идёт в очки!\n"
        f"Завершить: <code>/завершить {tid}</code>",
        parse_mode="HTML",
    )


@dp.message(lambda msg: msg.text and msg.text.startswith("/завершить"))
async def end_tournament(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "❗ Формат: <code>/завершить [id]</code>", parse_mode="HTML"
        )
        return
    tid = parts[1]
    tour = data["tournaments"].get(tid)
    if not tour:
        await message.reply(f"❌ Турнир #{tid} не найден.")
        return
    if tour["status"] == "finished":
        await message.reply(f"❌ Турнир #{tid} уже завершён.")
        return
    if tour["status"] == "open":
        await message.reply(
            f"❌ Турнир #{tid} ещё не стартовал. Сначала: <code>/старт {tid}</code>",
            parse_mode="HTML",
        )
        return

    scores = tour.get("scores", {})
    if not scores:
        await message.reply("❌ Никто не набрал очков. Завершение невозможно.")
        return

    # Находим победителя — максимум очков
    winner_uid = max(scores, key=lambda k: scores[k])
    winner_id = int(winner_uid)
    winner_score = scores[winner_uid]
    prize = tour["prize_pool"]
    add_balance(winner_id, prize)

    tour["status"] = "finished"
    tour["winner_id"] = winner_id
    save_data(data)

    try:
        winner = await bot.get_chat(winner_id)
        w_name = winner.first_name
    except Exception:
        w_name = str(winner_id)

    # Топ-3 участников по очкам
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4.", "5."]
    board_lines = []
    for i, (uid, sc) in enumerate(sorted_scores):
        try:
            u = await bot.get_chat(int(uid))
            uname = u.first_name
        except Exception:
            uname = uid
        board_lines.append(f"{medals[i]} {mention(int(uid), uname)} — {fmt(sc)} очков")

    await message.reply(
        f"🏁 Турнир <b>{tour['name']}</b> завершён!\n\n"
        f"👥 Участников: {len(tour['participants'])}\n"
        f"💰 Призовой фонд: <b>{fmt(prize)} GRAM</b>\n\n"
        f"<b>Таблица лидеров:</b>\n" + "\n".join(board_lines) + f"\n\n"
        f"🥇 Победитель: {mention(winner_id, w_name)} ({fmt(winner_score)} очков) → +{fmt(prize)} GRAM!",
        parse_mode="HTML",
    )


@dp.message(lambda msg: msg.text and msg.text.startswith("/удалить"))
async def delete_tournament(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❗ Формат: <code>/удалить [id]</code>", parse_mode="HTML")
        return
    tid = parts[1]
    tour = data["tournaments"].get(tid)
    if not tour:
        await message.reply(f"❌ Турнир #{tid} не найден.")
        return
    if tour["status"] == "finished":
        del data["tournaments"][tid]
        save_data(data)
        await message.reply(f"🗑 Турнир #{tid} удалён из истории.")
        return

    # Возвращаем взносы, если турнир ещё не завершён
    refunded = []
    for uid in tour["participants"]:
        if tour["entry_fee"] > 0:
            add_balance(uid, tour["entry_fee"])
            refunded.append(uid)

    del data["tournaments"][tid]
    save_data(data)

    refund_text = (
        f"\n💸 Взносы возвращены {len(refunded)} участникам." if refunded else ""
    )
    await message.reply(
        f"🗑 Турнир <b>#{tid} — {tour['name']}</b> удалён.{refund_text}",
        parse_mode="HTML",
    )


@dp.message(lambda msg: msg.text and msg.text.lower() == "турниры")
async def list_tournaments(message: Message):
    tours = data.get("tournaments", {})
    active = {tid: t for tid, t in tours.items() if t["status"] in ("open", "active")}
    if not active:
        await message.reply("🏆 Сейчас нет активных турниров.")
        return

    lines = ["🏆 <b>Активные турниры</b>\n"]
    for tid, t in active.items():
        fee_text = f"{t['entry_fee']} GRAM" if t["entry_fee"] > 0 else "бесплатно"
        slots_left = t["max_slots"] - len(t["participants"])
        status_icon = "🟢 Набор" if t["status"] == "open" else "🔴 Идёт"
        lines.append(
            f"<b>#{tid} — {t['name']}</b> {status_icon}\n"
            f"   Взнос: {fee_text} | Мест осталось: {slots_left} | Приз: {fmt(t['prize_pool'])} GRAM\n"
            + (
                f"   Вступить: <code>уч {tid}</code>"
                if t["status"] == "open"
                else "   🎰 Крутите рулетку!"
            )
        )
    await message.reply("\n\n".join(lines), parse_mode="HTML")


@dp.message(
    lambda msg: msg.text and re.match(r"^уч(\s+\d+)?$", msg.text.lower().strip())
)
async def join_tournament(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name
    parts = message.text.strip().split()

    if len(parts) < 2:
        tours = {
            tid: t
            for tid, t in data.get("tournaments", {}).items()
            if t["status"] == "open"
        }
        if len(tours) == 1:
            tid = list(tours.keys())[0]
        elif not tours:
            await message.reply("🏆 Нет турниров, принимающих участников.")
            return
        else:
            lines = ["Укажи номер турнира: <code>уч [id]</code>\n"]
            for tid, t in tours.items():
                fee_text = (
                    f"{t['entry_fee']} GRAM" if t["entry_fee"] > 0 else "бесплатно"
                )
                lines.append(
                    f"• #{tid} — {t['name']} ({fee_text}, {len(t['participants'])}/{t['max_slots']} мест)"
                )
            await message.reply("\n".join(lines), parse_mode="HTML")
            return
    else:
        tid = parts[1]

    tour = data["tournaments"].get(tid)
    if not tour:
        await message.reply(f"❌ Турнир #{tid} не найден.")
        return
    if tour["status"] != "open":
        await message.reply(f"❌ Набор в турнир #{tid} закрыт.")
        return
    if user_id in tour["participants"]:
        await message.reply(
            f"✅ Ты уже записан в <b>{tour['name']}</b>!", parse_mode="HTML"
        )
        return
    if len(tour["participants"]) >= tour["max_slots"]:
        await message.reply(
            f"❌ В турнире <b>{tour['name']}</b> все места заняты!", parse_mode="HTML"
        )
        return

    fee = tour["entry_fee"]
    if fee > 0:
        if get_user_balance(user_id) < fee:
            await message.reply("Недостаточно GRAM на балансе")
            return
        add_balance(user_id, -fee)
        tour["prize_pool"] += fee

    tour["participants"].append(user_id)
    tour["scores"][str(user_id)] = 0
    save_data(data)

    slots_left = tour["max_slots"] - len(tour["participants"])
    fee_text = f" (списано {fmt(fee)} GRAM)" if fee > 0 else ""
    await message.reply(
        f"✅ {mention(user_id, name)} записан в турнир <b>{tour['name']}</b>!{fee_text}\n"
        f"👥 {len(tour['participants'])}/{tour['max_slots']} мест | 💰 Призовой фонд: {fmt(tour['prize_pool'])} GRAM\n"
        + (
            f"⏳ Осталось мест: {slots_left}"
            if slots_left > 0
            else "✅ Все места заняты!"
        ),
        parse_mode="HTML",
    )


@dp.message(lambda msg: msg.text and msg.text.lower() == "очки")
async def my_tournament_score(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name
    active_tours = {
        tid: t
        for tid, t in data.get("tournaments", {}).items()
        if t["status"] == "active" and user_id in t["participants"]
    }
    if not active_tours:
        await message.reply("Ты не участвуешь ни в одном активном турнире.")
        return
    lines = [f"🎯 <b>Твои очки в турнирах</b> ({mention(user_id, name)})\n"]
    for tid, t in active_tours.items():
        score = t.get("scores", {}).get(str(user_id), 0)
        sorted_scores = sorted(t.get("scores", {}).values(), reverse=True)
        rank = sorted_scores.index(score) + 1 if score in sorted_scores else "?"
        lines.append(f"🏆 <b>{t['name']}</b>: {fmt(score)} очков (#{rank} место)")
    await message.reply("\n".join(lines), parse_mode="HTML")


# ========== МЕНЮ: ПРОФИЛЬ ==========


@dp.message(lambda msg: msg.text == "👤 Профиль")
async def profile_button(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name
    prof = get_profile(user_id)
    balance = get_user_balance(user_id)
    rank = get_user_rank(user_id)
    wins = prof["duel_wins"]
    losses = prof["duel_losses"]
    draws = prof["duel_draws"]
    total_duels = wins + losses + draws

    house_line = ""
    if prof["house"]:
        emoji, hname = next(
            ((e, n) for e, n in HOUSES if n == prof["house"]), ("🏠", prof["house"])
        )
        house_line = f"\n{emoji} Факультет: <b>{hname}</b>"

    clan_line = ""
    if prof["clan_id"]:
        clan = data["clans"].get(str(prof["clan_id"]))
        if clan:
            clan_line = f"\n🏰 Клан: <b>{clan['name']}</b>"

    duel_line = (
        f"⚔️ Дуэли: <b>{total_duels}</b> (🏆{wins} / ❌{losses} / 🤝{draws})"
        if total_duels
        else "⚔️ Дуэли: нет"
    )

    await message.reply(
        f"👤 <b>{name}</b>\n"
        f"🆔 ID: <code>{user_id}</code>{house_line}{clan_line}\n\n"
        f"💰 Баланс: <b>{fmt(balance)} GRAM</b>\n"
        f"🏅 Место в топе: <b>#{rank}</b>\n"
        f"{duel_line}",
        parse_mode="HTML",
    )


# ========== МЕНЮ: ХОГВАРТС ==========


@dp.message(lambda msg: msg.text == "🔮 Хогвартс")
async def hogwarts_button(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name
    prof = get_profile(user_id)

    if prof["house"]:
        emoji, hname = next(
            ((e, n) for e, n in HOUSES if n == prof["house"]), ("🏠", prof["house"])
        )
        await message.reply(
            f"🔮 {mention(user_id, name)}, ты уже принадлежишь к факультету "
            f"<b>{emoji} {hname}</b>!",
            parse_mode="HTML",
        )
        return

    emoji, hname = random.choice(HOUSES)
    prof["house"] = hname
    save_data(data)

    await message.reply(
        f"🔮 Шляпа-распределитель думает...\n\n"
        f"...{emoji} <b>{hname.upper()}!</b>\n\n"
        f"{mention(user_id, name)}, добро пожаловать на факультет <b>{hname}</b>!",
        parse_mode="HTML",
    )


# ========== МЕНЮ: КОМАНДЫ ==========


@dp.message(lambda msg: msg.text == "📋 Команды")
async def commands_button(message: Message):
    await message.reply(
        "📋 <b>Все команды бота</b>\n\n"
        "<b>💰 Баланс и переводы</b>\n"
        "• <code>б</code> — посмотреть баланс\n"
        "• <code>п [сумма]</code> (ответом) — перевести GRAM\n"
        "• <code>п [id] [сумма]</code> — перевод по ID\n\n"
        "<b>🎮 Игры</b>\n"
        "• <code>[ставка] [тип]</code> — поставить на рулетку\n"
        "  Типы: <code>к</code> красное, <code>ч</code> чёрное, <code>н</code> нечётное, <code>чт</code> чётное, число 0–36\n"
        "• <code>го</code> — запустить рулетку (≥15 сек после первой ставки)\n"
        "• <code>дуэль @игрок [сумма]</code> — вызвать на дуэль\n"
        "• <code>принять</code> — принять дуэль\n\n"
        "<b>🏆 Турниры</b>\n"
        "• <code>турниры</code> — список активных турниров\n"
        "• <code>уч [id]</code> — вступить в турнир\n\n"
        "<b>🏰 Кланы</b>\n"
        "• <code>клан</code> — информация о вашем клане\n"
        "• <code>создать клан [название]</code> — создать клан (1000 GRAM)\n"
        "• <code>вступить [название]</code> — вступить в клан\n"
        "• <code>выйти</code> — выйти из клана\n"
        "• <code>вклад [сумма]</code> — пополнить казну клана\n\n"
        "<b>📊 Прочее</b>\n"
        "• <code>топ</code> — таблица лидеров\n"
        "• 🎁 Бонус — 2500 GRAM раз в 24 часа",
        parse_mode="HTML",
    )


# ========== МЕНЮ: ДОНАТ ==========


@dp.message(lambda msg: msg.text == "🛒 Донат")
async def donate_button(message: Message):
    await message.reply(
        "🛒 <b>Как получить GRAM</b>\n\n"
        "🎁 <b>Ежедневный бонус</b> — 2 500 GRAM раз в 24 часа\n"
        "⚔️ <b>Дуэли</b> — ставь и побеждай соперников\n"
        "🎰 <b>Рулетка</b> — выигрыш до 35x от ставки\n"
        "🏆 <b>Турниры</b> — побеждай и забирай призовой фонд\n"
        "🏰 <b>Клановая казна</b> — участвуй в жизни клана\n\n"
        "💡 GRAM — игровая валюта бота.\n\n"
        "📩 По всем вопросам пишите @вдминистратора",
        parse_mode="HTML",
    )


# ========== МЕНЮ: ЧАТЫ ==========


@dp.message(lambda msg: msg.text == "💬 Чаты")
async def chats_button(message: Message):
    total_players = len(data["users"])
    total_gram = sum(data["users"].values())
    total_clans = len(data["clans"])
    open_tours = sum(1 for t in data["tournaments"].values() if t["status"] == "open")
    await message.reply(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Игроков: <b>{total_players}</b>\n"
        f"💰 GRAM в обращении: <b>{fmt(total_gram)}</b>\n"
        f"🏰 Кланов: <b>{total_clans}</b>\n"
        f"🏆 Активных турниров: <b>{open_tours}</b>",
        parse_mode="HTML",
    )


# ========== МЕНЮ: ИГРЫ ==========


@dp.message(lambda msg: msg.text == "🎮 Игры")
async def games_button(message: Message):
    await message.reply(
        "🎮 <b>Доступные игры</b>\n\n"
        "🎰 <b>Рулетка</b>\n"
        "Ставь GRAM на цвет, чётность или число.\n"
        "Примеры: <code>100 к</code> <code>500 ч</code> <code>1000 7</code>\n"
        "Затем напиши <code>го</code> чтобы крутить.\n"
        "Выигрыш: x2 (цвет/чётность) или x35 (число)\n\n"
        "⚔️ <b>Дуэль</b>\n"
        "Вызови другого игрока на бросок кубика.\n"
        "Кто выбросил больше — забирает банк.\n"
        "Команда: <code>дуэль @username 500</code>",
        parse_mode="HTML",
    )


# ========== МЕНЮ: ПОЛИТИКА ==========


@dp.message(lambda msg: msg.text == "Политика")
async def policy_button(message: Message):
    await message.reply(
        "📜 <b>Правила и политика</b>\n\n"
        "1. GRAM — игровая валюта без реальной стоимости.\n"
        "2. Запрещено использовать ботов и скрипты для накрутки.\n"
        "3. Запрещено оскорблять других участников.\n"
        "4. Администрация вправе изменить баланс любого пользователя.\n"
        "5. Использование бота означает согласие с этими правилами.\n\n"
        "По вопросам обращайтесь к администратору.",
        parse_mode="HTML",
    )


# ========== МЕНЮ: ЯЗЫК ==========


@dp.message(lambda msg: msg.text == "Изменить язык")
async def language_button(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇷🇺 Русский ✅", callback_data="lang_ru"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
            ]
        ]
    )
    await message.reply("🌐 Выберите язык / Choose language:", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data in ["lang_ru", "lang_en"])
async def language_callback(callback: CallbackQuery):
    lang = callback.data.split("_")[1]
    prof = get_profile(callback.from_user.id)
    prof["lang"] = lang
    save_data(data)
    if lang == "ru":
        await callback.message.edit_text(
            "🇷🇺 Язык установлен: <b>Русский</b>", parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "🇬🇧 Language set: <b>English</b>", parse_mode="HTML"
        )
    await callback.answer()


# ========== КЛАНЫ ==========


@dp.message(lambda msg: msg.text and msg.text.lower() == "клан")
async def clan_info(message: Message):
    user_id = message.from_user.id
    prof = get_profile(user_id)
    clan_id = str(prof["clan_id"]) if prof["clan_id"] else None
    if not clan_id or clan_id not in data["clans"]:
        await message.reply(
            "🏰 Ты не состоишь в клане.\n\n"
            "• <code>создать клан [название]</code> — создать клан (1000 GRAM)\n"
            "• <code>вступить [название]</code> — вступить в существующий клан",
            parse_mode="HTML",
        )
        return
    clan = data["clans"][clan_id]
    owner_name = "неизвестен"
    try:
        owner = await bot.get_chat(clan["owner_id"])
        owner_name = owner.first_name
    except Exception:
        owner_name = str(clan["owner_id"])
    await message.reply(
        f"🏰 <b>{clan['name']}</b>\n\n"
        f"👑 Лидер: {mention(clan['owner_id'], owner_name)}\n"
        f"👥 Участников: <b>{len(clan['members'])}</b>\n"
        f"💰 Казна: <b>{fmt(clan['pool'])} GRAM</b>\n\n"
        f"• <code>вклад [сумма]</code> — пополнить казну\n"
        f"• <code>выйти</code> — покинуть клан",
        parse_mode="HTML",
    )


@dp.message(
    lambda msg: msg.text and re.match(r"^создать клан\s+.+", msg.text.lower().strip())
)
async def create_clan(message: Message):
    user_id = message.from_user.id
    name = message.from_user.first_name
    prof = get_profile(user_id)

    if prof["clan_id"] and str(prof["clan_id"]) in data["clans"]:
        await message.reply(
            "❌ Ты уже состоишь в клане. Сначала выйди: <code>выйти</code>",
            parse_mode="HTML",
        )
        return

    cost = 1000
    if get_user_balance(user_id) < cost:
        await message.reply("Недостаточно GRAM на балансе")
        return

    parts = message.text.strip().split(maxsplit=2)
    clan_name = parts[2].strip() if len(parts) >= 3 else parts[-1].strip()

    # Проверяем уникальность названия
    for c in data["clans"].values():
        if c["name"].lower() == clan_name.lower():
            await message.reply("❌ Клан с таким названием уже существует.")
            return

    clan_id = str(data["next_clan_id"])
    data["next_clan_id"] += 1
    add_balance(user_id, -cost)
    data["clans"][clan_id] = {
        "name": clan_name,
        "owner_id": user_id,
        "members": [user_id],
        "pool": 0,
    }
    prof["clan_id"] = clan_id
    save_data(data)

    await message.reply(
        f"🏰 Клан <b>{clan_name}</b> создан!\n"
        f"💸 Списано {cost} GRAM. Баланс: {fmt(get_user_balance(user_id))} GRAM\n\n"
        f"Другие игроки могут вступить: <code>вступить {clan_name}</code>",
        parse_mode="HTML",
    )


@dp.message(
    lambda msg: msg.text and re.match(r"^вступить\s+.+", msg.text.lower().strip())
)
async def join_clan(message: Message):
    user_id = message.from_user.id
    prof = get_profile(user_id)

    if prof["clan_id"] and str(prof["clan_id"]) in data["clans"]:
        await message.reply(
            "❌ Ты уже в клане. Сначала выйди: <code>выйти</code>", parse_mode="HTML"
        )
        return

    parts = message.text.strip().split(maxsplit=1)
    clan_name = parts[1].strip() if len(parts) >= 2 else ""

    target_clan_id = None
    for cid, c in data["clans"].items():
        if c["name"].lower() == clan_name.lower():
            target_clan_id = cid
            break

    if not target_clan_id:
        await message.reply(f"❌ Клан «{clan_name}» не найден.")
        return

    clan = data["clans"][target_clan_id]
    clan["members"].append(user_id)
    prof["clan_id"] = target_clan_id
    save_data(data)

    await message.reply(
        f"✅ Ты вступил в клан <b>{clan['name']}</b>!\n"
        f"👥 Участников: {len(clan['members'])}",
        parse_mode="HTML",
    )


@dp.message(lambda msg: msg.text and msg.text.lower().strip() == "выйти")
async def leave_clan(message: Message):
    user_id = message.from_user.id
    prof = get_profile(user_id)
    clan_id = str(prof["clan_id"]) if prof["clan_id"] else None

    if not clan_id or clan_id not in data["clans"]:
        await message.reply("❌ Ты не состоишь в клане.")
        return

    clan = data["clans"][clan_id]
    if clan["owner_id"] == user_id:
        await message.reply(
            "👑 Ты лидер клана. Сначала передай лидерство или расформируй клан."
        )
        return

    clan["members"].remove(user_id)
    prof["clan_id"] = None
    save_data(data)
    await message.reply(
        f"✅ Ты вышел из клана <b>{clan['name']}</b>.", parse_mode="HTML"
    )


@dp.message(
    lambda msg: msg.text and re.match(r"^вклад\s+\d+$", msg.text.lower().strip())
)
async def clan_deposit(message: Message):
    user_id = message.from_user.id
    prof = get_profile(user_id)
    clan_id = str(prof["clan_id"]) if prof["clan_id"] else None

    if not clan_id or clan_id not in data["clans"]:
        await message.reply("❌ Ты не состоишь в клане.")
        return

    parts = message.text.strip().split()
    amount = int(parts[1])
    if amount <= 0:
        await message.reply("❌ Сумма должна быть больше 0.")
        return
    if get_user_balance(user_id) < amount:
        await message.reply("Недостаточно GRAM на балансе")
        return

    add_balance(user_id, -amount)
    clan = data["clans"][clan_id]
    clan["pool"] += amount
    save_data(data)

    await message.reply(
        f"✅ Ты внёс <b>{fmt(amount)} GRAM</b> в казну клана <b>{clan['name']}</b>.\n"
        f"💰 Казна: {fmt(clan['pool'])} GRAM | Твой баланс: {fmt(get_user_balance(user_id))} GRAM",
        parse_mode="HTML",
    )


# ========== МЕНЮ: ТУРНИРЫ ==========


@dp.message(lambda msg: msg.text == "🏆 Турниры")
async def tournaments_button(message: Message):
    await list_tournaments(message)


@dp.message(lambda msg: msg.text == "🎁 Бонус")
async def bonus_button(message: Message):
    user_id = message.from_user.id
    can, remaining = can_take_bonus(user_id)
    if can:
        add_balance(user_id, 2500)
        set_bonus_taken(user_id)
        await message.reply(
            f"🎁 Вам начислено: 2 500 GRAM\n💰 Новый баланс: {fmt(get_user_balance(user_id))}"
        )
    else:
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        await message.reply(
            f"⏳ Осталось подождать {hours:02d}:{minutes:02d} до следующего бонуса"
        )


# ========== ЗАПУСК =========
async def send_chunks(chat_id: int, text: str, parse_mode: str = "HTML", reply_markup=None):
    """Разбивает длинный текст на сообщения ≤4096 символов, режет по строкам."""
    MAX = 4096
    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        candidate = current + "\n" + line if current else line
        if len(candidate) > MAX:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        await bot.send_message(
            chat_id,
            chunk,
            parse_mode=parse_mode,
            reply_markup=reply_markup if is_last else None,
        )


async def send_roulette_result(
    chat_id: int,
    result_num: int,
    color_symbol: str,
    bet_results: list,
    names: dict,
    per_user: dict,
):
    lines = [f"Рулетка: {result_num}{color_symbol}"]
    winners = []
    for user_id, amount, bet_type, bet_value, won, change in bet_results:
        link = mention(user_id, names[user_id])
        label = bet_label_str(bet_type, bet_value)
        lines.append(f"{link} {fmt(amount)} GRAM на {label}")
        if won:
            win_amount = change + amount
            winners.append(f"{link} ставка {fmt(amount)} GRAM выиграл {fmt(win_amount)} на {label}")

    if winners:
        lines.append("")
        lines.extend(winners)

    buttons = [[
        InlineKeyboardButton(text="Повторить", callback_data=f"rpt:{chat_id}"),
        InlineKeyboardButton(text="Удвоить",   callback_data=f"dbl:{chat_id}"),
    ]] if per_user else []
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await send_chunks(chat_id, "\n".join(lines), reply_markup=keyboard)


async def main():
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
