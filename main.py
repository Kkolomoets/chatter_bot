import asyncio
import logging
import random
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from os import getenv
from dotenv import load_dotenv

load_dotenv()

# ======================== НАСТРОЙКИ ========================
BOT_TOKEN = getenv("BOT_TOKEN")
ADMIN_IDS = [970941850]
SESSIONS_FILE = "sessions.json"

BASE_PROFILE_INTERVAL_MIN = 130
BASE_PROFILE_INTERVAL_MAX = 270
BASE_MESSAGE_INTERVAL_MIN = 80
BASE_MESSAGE_INTERVAL_MAX = 95

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ======================== СОСТОЯНИЯ ========================
class BearerState(StatesGroup):
    waiting_for_bearer = State()


# ======================== ХРАНИЛИЩЕ ========================
user_sessions: Dict[int, dict] = {}

DEFAULT_MONITORS = {"online": True, "offline": True, "messages": True}


# ======================== СОХРАНЕНИЕ / ЗАГРУЗКА ========================
def save_sessions():
    data = {}
    for uid, sess in user_sessions.items():
        data[str(uid)] = {
            "bearer": sess.get("bearer", ""),
            "chat_id": sess.get("chat_id", uid),
            "interval_multiplier": sess.get("interval_multiplier", 1.0),
            "running": sess.get("running", False),
            "monitors": sess.get("monitors", DEFAULT_MONITORS.copy()),
        }
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_sessions() -> dict:
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ======================== ХЕЛПЕРЫ ВРЕМЕНИ ========================
def _parse_api_dt(dt_str: str) -> datetime:
    """Парсит дату из API (всегда UTC) → aware datetime."""
    return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ======================== API ========================
BASE_URL = "https://mcs-1.chat-space.ai:8001"


async def api_get(
    session: aiohttp.ClientSession, url: str, bearer: str
) -> Optional[dict]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {bearer}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }
    try:
        async with session.get(
            url, headers=headers, ssl=False, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                logger.error(f"API error {resp.status}: {url}")
                return None
            return await resp.json()
    except Exception as e:
        logger.error(f"Request error: {e}")
        return None


async def get_girl_ids(session: aiohttp.ClientSession, bearer: str):
    url = f"{BASE_URL}/identity/cabinets/assigned"
    data = await api_get(session, url, bearer)
    if not data:
        return [], {}
    list_of_id, name_id = [], {}
    for girl in data:
        parts = girl["name"].split(" ", 1)
        girl_id = parts[0]
        girl_name = parts[1] if len(parts) == 2 else girl_id
        list_of_id.append(girl_id)
        name_id[girl_id] = girl_name
    return list_of_id, name_id


async def get_users_raw(
    session: aiohttp.ClientSession,
    bearer: str,
    girl_account_id: str,
    online: bool = True,
):
    online_str = "true" if online else "false"
    url = f"{BASE_URL}/operator/chat?profileId=pd-{girl_account_id}&criteria=PD_ACTIVE&cursor=&online={online_str}"
    return await api_get(session, url, bearer)


async def get_limits(
    session: aiohttp.ClientSession, bearer: str, girl_id: str, customer_id: str
) -> int:
    url = f"{BASE_URL}/operator/chat/restriction?profileId={girl_id}&customerId={customer_id}"
    data = await api_get(session, url, bearer)
    return data.get("messagesLeft", 0) if data else 0


async def get_users(
    session: aiohttp.ClientSession, bearer: str, girl_account_id: str
) -> list:
    users_result = []

    # Онлайн — давно без ответа (>2 ч)
    data = await get_users_raw(session, bearer, girl_account_id, online=True)
    if data:
        for user in data.get("dialogs", []):
            try:
                created = _parse_api_dt(user["createdDate"])
                idle = _now_utc() - created
                if idle > timedelta(hours=2) and user["messagesLeft"] > 0:
                    users_result.append(
                        {
                            "user_name": user["customer"]["name"],
                            "user_id": user["customer"]["id"],
                            "girl_id": user["profileId"].replace("pd-", ""),
                            "messagesLeft": user["messagesLeft"],
                            "status": user["highlightType"],
                            "idle_hours": round(idle.total_seconds() / 3600, 1),
                        }
                    )
            except Exception:
                pass

    # Оффлайн — unanswered
    data = await get_users_raw(session, bearer, girl_account_id, online=False)
    if data:
        for user in data.get("dialogs", []):
            if user.get("highlightType") == "unanswered":
                users_result.append(
                    {
                        "user_name": user["customer"]["name"],
                        "user_id": user["customer"]["id"],
                        "girl_id": user["profileId"].replace("pd-", ""),
                        "messagesLeft": user["messagesLeft"],
                        "status": user["highlightType"],
                    }
                )

    return users_result


async def get_unanswered(session: aiohttp.ClientSession, bearer: str):
    url = (
        f"{BASE_URL}/operator/chat/unanswered?"
        "contentTypes=AUDIO,COMMENT,VIRTUAL_GIFT_BATCH,PHOTO_BATCH,MESSAGE,HTML,PHOTO,STICKER,"
        "VIDEO,REAL_PRESENT,TEXT_WITH_PHOTO_CONTENT,LIKE_USER,WINK,LIKE_PHOTO,LIKE_NEWSFEED_POST,"
        "REPLY_NEWSFEED_POST"
    )
    return await api_get(session, url, bearer)


async def check_unanswered(session: aiohttp.ClientSession, bearer: str) -> int:
    data = await get_unanswered(session, bearer)
    if not data:
        return 0
    messages = 0
    for user in data:
        if user:
            limits = await get_limits(
                session, bearer, user["profileId"], user["customer"]["id"]
            )
            if limits > 0:
                messages += 1
            await asyncio.sleep(random.uniform(0.5, 1.5))
    return messages


async def check_online_inactive(
    session: aiohttp.ClientSession, bearer: str, list_of_id: list
) -> list:
    """Онлайн-пользователи без ответа больше 1 часа."""
    found = []
    for girl_id in list_of_id:
        data = await get_users_raw(session, bearer, girl_id, online=True)
        if data:
            for user in data.get("dialogs", []):
                try:
                    created = _parse_api_dt(user["createdDate"])
                    idle = _now_utc() - created
                    if idle > timedelta(hours=1) and user["messagesLeft"] > 0:
                        found.append(
                            {
                                "user_name": user["customer"]["name"],
                                "user_id": user["customer"]["id"],
                                "girl_id": user["profileId"].replace("pd-", ""),
                                "messagesLeft": user["messagesLeft"],
                                "status": user.get("highlightType", ""),
                                "idle_hours": round(idle.total_seconds() / 3600, 1),
                            }
                        )
                except Exception:
                    pass
    return found


async def check_offline_unanswered(
    session: aiohttp.ClientSession, bearer: str, list_of_id: list
) -> list:
    found = []
    for girl_id in list_of_id:
        data = await get_users_raw(session, bearer, girl_id, online=False)
        if data:
            for user in data.get("dialogs", []):
                if user.get("highlightType") == "unanswered":
                    found.append(
                        {
                            "user_name": user["customer"]["name"],
                            "user_id": user["customer"]["id"],
                            "girl_id": user["profileId"].replace("pd-", ""),
                            "messagesLeft": user["messagesLeft"],
                            "status": user["highlightType"],
                        }
                    )
    return found


# ======================== ФОРМАТИРОВАНИЕ ========================
def format_user_alert(user: dict, name_id: dict) -> str:
    girl_name = name_id.get(user["girl_id"], user["girl_id"])
    important = " ⚠️ ВАЖНЫЙ!" if user["status"] == "unanswered" else ""
    idle = f"\n⏳ Без ответа: {user['idle_hours']} ч." if user.get("idle_hours") else ""
    return (
        f"👤 <b>{user['user_name']}</b>\n"
        f"📋 Анкета: {girl_name}\n"
        f"💬 Сообщений доступно: {user['messagesLeft']}"
        f"{idle}"
        f"{important}"
    )


# ======================== КЛАВИАТУРЫ ========================
def get_interval_seconds(multiplier: float) -> str:
    lo = int(BASE_PROFILE_INTERVAL_MIN * multiplier)
    hi = int(BASE_PROFILE_INTERVAL_MAX * multiplier)
    return f"{lo}–{hi}с"


def admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Основная панель управления."""
    session = user_sessions.get(user_id, {})
    running = session.get("running", False)
    multiplier = session.get("interval_multiplier", 1.0)
    interval_str = get_interval_seconds(multiplier)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏹ Стоп" if running else "▶️ Старт",
                    callback_data=f"toggle_{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🐢 Медленнее", callback_data=f"slower_{user_id}"
                ),
                InlineKeyboardButton(
                    text=f"⏱ x{multiplier:.1f} ({interval_str})", callback_data="noop"
                ),
                InlineKeyboardButton(
                    text="🐇 Быстрее", callback_data=f"faster_{user_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Проверить сообщения", callback_data=f"check_msg_{user_id}"
                ),
                InlineKeyboardButton(
                    text="📴 Проверить оффлайны",
                    callback_data=f"check_offline_{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Проверить онлайны", callback_data=f"check_online_{user_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎚 Управление мониторингом",
                    callback_data=f"monitors_{user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статус", callback_data=f"status_{user_id}"
                ),
            ],
        ]
    )


def monitors_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Панель переключателей мониторинга."""
    sess = user_sessions.get(user_id, {})
    mon = sess.get("monitors", DEFAULT_MONITORS.copy())

    def lbl(key: str, text: str) -> str:
        return f"{'✅' if mon.get(key) else '❌'} {text}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=lbl("messages", "Сообщения"),
                    callback_data=f"mon_toggle_messages_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=lbl("offline", "Оффлайны"),
                    callback_data=f"mon_toggle_offline_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=lbl("online", "Онлайны"),
                    callback_data=f"mon_toggle_online_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data=f"back_panel_{user_id}"
                )
            ],
        ]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔑 Добавить/сменить Bearer", callback_data="set_bearer"
                )
            ],
            [InlineKeyboardButton(text="🎛 Управление", callback_data="admin_panel")],
        ]
    )


def resume_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Возобновить", callback_data=f"resume_{user_id}"
                ),
                InlineKeyboardButton(text="❌ Не нужно", callback_data="noop"),
            ]
        ]
    )


# ======================== ФОНОВАЯ ЗАДАЧА ========================
async def monitoring_task(user_id: int, chat_id: int):
    session_data = user_sessions[user_id]
    bearer = session_data["bearer"]

    async with aiohttp.ClientSession() as session:
        list_of_id, name_id = await get_girl_ids(session, bearer)
        if not list_of_id:
            await bot.send_message(
                chat_id, "❌ Не удалось получить список анкет. Проверьте Bearer токен."
            )
            session_data["running"] = False
            save_sessions()
            return

        session_data["name_id"] = name_id
        session_data["list_of_id"] = list_of_id

        profiles_text = "\n".join(f"• {name} ({id_})" for id_, name in name_id.items())
        await bot.send_message(
            chat_id,
            f"✅ <b>Мониторинг запущен</b>\n\n📋 Профили ({len(list_of_id)}):\n{profiles_text}",
            parse_mode="HTML",
        )

        next_check = {
            id_: asyncio.get_event_loop().time() + random.randint(0, 20)
            for id_ in list_of_id
        }
        message_interval = random.randint(
            BASE_MESSAGE_INTERVAL_MIN, BASE_MESSAGE_INTERVAL_MAX
        )
        next_message_check = asyncio.get_event_loop().time() + message_interval

        while session_data.get("running", False):
            now = asyncio.get_event_loop().time()
            multiplier = session_data.get("interval_multiplier", 1.0)
            mon = session_data.get("monitors", DEFAULT_MONITORS.copy())

            for id_ in list_of_id:
                if now >= next_check[id_]:
                    if mon.get("online") or mon.get("offline"):
                        users = []

                        # Онлайн
                        if mon.get("online"):
                            data = await get_users_raw(
                                session, bearer, id_, online=True
                            )
                            if data:
                                for u in data.get("dialogs", []):
                                    try:
                                        created = _parse_api_dt(u["createdDate"])
                                        idle = _now_utc() - created
                                        if (
                                            idle > timedelta(hours=2)
                                            and u["messagesLeft"] > 0
                                        ):
                                            users.append(
                                                {
                                                    "user_name": u["customer"]["name"],
                                                    "user_id": u["customer"]["id"],
                                                    "girl_id": u["profileId"].replace(
                                                        "pd-", ""
                                                    ),
                                                    "messagesLeft": u["messagesLeft"],
                                                    "status": u["highlightType"],
                                                    "idle_hours": round(
                                                        idle.total_seconds() / 3600, 1
                                                    ),
                                                }
                                            )
                                    except Exception:
                                        pass

                        # Оффлайн
                        if mon.get("offline"):
                            data = await get_users_raw(
                                session, bearer, id_, online=False
                            )
                            if data:
                                for u in data.get("dialogs", []):
                                    if u.get("highlightType") == "unanswered":
                                        users.append(
                                            {
                                                "user_name": u["customer"]["name"],
                                                "user_id": u["customer"]["id"],
                                                "girl_id": u["profileId"].replace(
                                                    "pd-", ""
                                                ),
                                                "messagesLeft": u["messagesLeft"],
                                                "status": u["highlightType"],
                                            }
                                        )

                        for user in users:
                            await bot.send_message(
                                chat_id,
                                format_user_alert(user, name_id),
                                parse_mode="HTML",
                            )

                    interval = (
                        random.randint(
                            BASE_PROFILE_INTERVAL_MIN, BASE_PROFILE_INTERVAL_MAX
                        )
                        * multiplier
                    )
                    next_check[id_] = now + interval

            if now >= next_message_check:
                if mon.get("messages"):
                    messages = await check_unanswered(session, bearer)
                    if messages:
                        await bot.send_message(
                            chat_id,
                            f"📨 <b>Непрочитанных уведомлений: {messages}</b>",
                            parse_mode="HTML",
                        )
                next_message_check += message_interval * multiplier

            await asyncio.sleep(1)

    session_data["running"] = False
    save_sessions()
    await bot.send_message(chat_id, "⏹ Мониторинг остановлен.")


# ======================== КОМАНДЫ ========================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Это бот-мониторинг чатов.\n"
        "Отправь свой <b>Bearer токен</b> командой /setbearer или через меню ниже."
        "Если ты не знаешь что такое Bearer - введи команду /bearer",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("setbearer"))
async def cmd_setbearer(message: Message, state: FSMContext):
    await message.answer("🔑 Отправь Bearer токен следующим сообщением:")
    await state.set_state(BearerState.waiting_for_bearer)


@dp.message(Command("panel"))
async def cmd_panel(message: Message):
    user_id = message.from_user.id
    if user_id not in user_sessions:
        await message.answer("❌ Сначала установи Bearer токен командой /setbearer")
        return
    await message.answer(
        "🎛 <b>Панель управления</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(user_id),
    )


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    user_id = message.from_user.id
    if user_id in user_sessions and user_sessions[user_id].get("running"):
        user_sessions[user_id]["running"] = False
        save_sessions()
        await message.answer("⏹ Остановка мониторинга...")
    else:
        await message.answer("ℹ️ Мониторинг не запущен.")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/start - для запуска мониторинга\n"
        "/stop - для остановки мониторинга\n"
        "/panel - чтоб открыть панель управления\n"
        "/setbearer - установить Bearer, чтобы его узнать введи /bearer\n"
        "/bearer - гайд по нахождению Bearer токена"
    )


@dp.message(Command("bearer"))
async def cmd_bearer(message: Message):
    await message.answer(
        "Bearer - это своего рода пароль для сайтов\n"
        "Чтобы его узнать - зайди на сайт, нажми F12\n"
        'Откроется консоль разработчика - там нужно нажать "Network"(Сеть)\n'
        'Чуть ниже, в фильтрах, нужно выбрать "Fetch/XHR"\n'
        "После этого открывай любой запрос, который отобразится, если их нет - обнови страницу\n"
        'Во вкладке "Headers" пролистай чуть ниже. Там будет написано Authorization. '
        'Скинь мне то, что справа от Authorization - там будет "Bearer и много символов". '
        'Слово Bearer и все пробелы удали, мне скинь лишь символы начинающиеся на "eyJ"'
    )


@dp.message(BearerState.waiting_for_bearer)
async def process_bearer(message: Message, state: FSMContext):
    user_id = message.from_user.id
    bearer = message.text.strip()

    try:
        await message.delete()
    except Exception:
        pass

    if user_id in user_sessions:
        old_task = user_sessions[user_id].get("task")
        if old_task and not old_task.done():
            user_sessions[user_id]["running"] = False
            await asyncio.sleep(2)

    # Сохраняем monitors если сессия уже была
    prev_monitors = (
        user_sessions[user_id].get("monitors", DEFAULT_MONITORS.copy())
        if user_id in user_sessions
        else DEFAULT_MONITORS.copy()
    )

    user_sessions[user_id] = {
        "bearer": bearer,
        "chat_id": message.chat.id,
        "running": True,
        "interval_multiplier": 1.0,
        "monitors": prev_monitors,
        "task": None,
        "name_id": {},
        "list_of_id": [],
    }
    save_sessions()

    task = asyncio.create_task(monitoring_task(user_id, message.chat.id))
    user_sessions[user_id]["task"] = task

    await state.clear()
    await message.answer(
        "✅ Bearer принят! Запускаю мониторинг...\nИспользуй /panel для управления.",
        reply_markup=admin_keyboard(user_id),
    )


# ======================== CALLBACK: НАВИГАЦИЯ ========================
@dp.callback_query(F.data == "set_bearer")
async def cb_set_bearer(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🔑 Отправь Bearer токен следующим сообщением:")
    await state.set_state(BearerState.waiting_for_bearer)
    await callback.answer()


@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in user_sessions:
        await callback.answer("❌ Сначала установи Bearer токен!", show_alert=True)
        return
    await callback.message.edit_text(
        "🎛 <b>Панель управления</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(user_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("back_panel_"))
async def cb_back_panel(callback: CallbackQuery):
    uid = int(callback.data.split("_")[2])
    await callback.message.edit_text(
        "🎛 <b>Панель управления</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard(uid),
    )
    await callback.answer()


# ======================== CALLBACK: УПРАВЛЕНИЕ ЗАПУСКОМ ========================
@dp.callback_query(F.data.startswith("toggle_"))
async def cb_toggle(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return

    session = user_sessions.get(user_id)
    if not session:
        await callback.answer("❌ Сессия не найдена", show_alert=True)
        return

    if session.get("running"):
        session["running"] = False
        save_sessions()
        await callback.answer("⏹ Останавливаю...")
    else:
        session["running"] = True
        save_sessions()
        task = asyncio.create_task(monitoring_task(user_id, session["chat_id"]))
        session["task"] = task
        await callback.answer("▶️ Запускаю...")

    await callback.message.edit_reply_markup(reply_markup=admin_keyboard(user_id))


@dp.callback_query(F.data.startswith("resume_"))
async def cb_resume(callback: CallbackQuery):
    uid = int(callback.data.split("_")[1])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    sess = user_sessions.get(uid)
    if not sess:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    sess["running"] = True
    save_sessions()
    task = asyncio.create_task(monitoring_task(uid, sess["chat_id"]))
    sess["task"] = task
    await callback.message.edit_text("▶️ Мониторинг возобновлён.")
    await callback.answer()


# ======================== CALLBACK: СКОРОСТЬ ========================
@dp.callback_query(F.data.startswith("faster_"))
async def cb_faster(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    session = user_sessions.get(user_id, {})
    m = round(max(0.3, session.get("interval_multiplier", 1.0) - 0.2), 1)
    session["interval_multiplier"] = m
    save_sessions()
    await callback.message.edit_reply_markup(reply_markup=admin_keyboard(user_id))
    await callback.answer(f"⚡ Интервал x{m:.1f} ({get_interval_seconds(m)})")


@dp.callback_query(F.data.startswith("slower_"))
async def cb_slower(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    session = user_sessions.get(user_id, {})
    m = round(min(5.0, session.get("interval_multiplier", 1.0) + 0.2), 1)
    session["interval_multiplier"] = m
    save_sessions()
    await callback.message.edit_reply_markup(reply_markup=admin_keyboard(user_id))
    await callback.answer(f"🐢 Интервал x{m:.1f} ({get_interval_seconds(m)})")


# ======================== CALLBACK: ОДНОРАЗОВЫЕ ПРОВЕРКИ ========================
@dp.callback_query(F.data.startswith("check_msg_"))
async def cb_check_msg(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    session = user_sessions.get(user_id)
    if not session:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    await callback.answer("🔍 Проверяю...")
    async with aiohttp.ClientSession() as http_session:
        messages = await check_unanswered(http_session, session["bearer"])
    if messages:
        await callback.message.answer(
            f"📨 <b>Непрочитанных уведомлений: {messages}</b>", parse_mode="HTML"
        )
    else:
        await callback.message.answer("✅ Непрочитанных уведомлений нет.")


@dp.callback_query(F.data.startswith("check_offline_"))
async def cb_check_offline(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    session = user_sessions.get(user_id)
    if not session:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    list_of_id = session.get("list_of_id", [])
    name_id = session.get("name_id", {})
    if not list_of_id:
        await callback.answer(
            "❌ Анкеты не загружены. Дождись запуска мониторинга.", show_alert=True
        )
        return
    await callback.answer("📴 Проверяю оффлайны...")
    async with aiohttp.ClientSession() as http_session:
        found = await check_offline_unanswered(
            http_session, session["bearer"], list_of_id
        )
    if found:
        for u in found:
            await callback.message.answer(
                format_user_alert(u, name_id), parse_mode="HTML"
            )
    else:
        await callback.message.answer("✅ Оффлайн-уведомлений нет.")


@dp.callback_query(F.data.startswith("check_online_"))
async def cb_check_online(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    session = user_sessions.get(user_id)
    if not session:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    list_of_id = session.get("list_of_id", [])
    name_id = session.get("name_id", {})
    if not list_of_id:
        await callback.answer(
            "❌ Анкеты не загружены. Дождись запуска мониторинга.", show_alert=True
        )
        return
    await callback.answer("🟢 Проверяю онлайны...")
    async with aiohttp.ClientSession() as http_session:
        found = await check_online_inactive(http_session, session["bearer"], list_of_id)
    if found:
        await callback.message.answer(
            f"🟢 <b>Онлайн без ответа ({len(found)}):</b>", parse_mode="HTML"
        )
        for u in found:
            await callback.message.answer(
                format_user_alert(u, name_id), parse_mode="HTML"
            )
    else:
        await callback.message.answer("✅ Онлайн-пользователей без ответа нет.")


# ======================== CALLBACK: СТАТУС ========================
@dp.callback_query(F.data.startswith("status_"))
async def cb_status(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    session = user_sessions.get(user_id, {})
    running = session.get("running", False)
    multiplier = session.get("interval_multiplier", 1.0)
    profiles = len(session.get("list_of_id", []))
    interval_str = get_interval_seconds(multiplier)
    mon = session.get("monitors", DEFAULT_MONITORS.copy())

    def tick(k):
        return "✅" if mon.get(k) else "❌"

    await callback.message.answer(
        f"📊 <b>Статус мониторинга</b>\n\n"
        f"{'🟢 Работает' if running else '🔴 Остановлен'}\n"
        f"⏱ Множитель: x{multiplier:.1f} ({interval_str} между проверками анкеты)\n"
        f"📋 Анкет в работе: {profiles}\n\n"
        f"<b>Активные виды мониторинга:</b>\n"
        f"{tick('messages')} Сообщения\n"
        f"{tick('offline')} Оффлайны\n"
        f"{tick('online')} Онлайны",
        parse_mode="HTML",
    )
    await callback.answer()


# ======================== CALLBACK: ПАНЕЛЬ МОНИТОРИНГА ========================
@dp.callback_query(F.data.startswith("monitors_"))
async def cb_monitors_panel(callback: CallbackQuery):
    uid = int(callback.data.split("_")[1])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "🎚 <b>Управление мониторингом</b>\n\nВключи или выключи нужные виды проверок:",
        parse_mode="HTML",
        reply_markup=monitors_keyboard(uid),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mon_toggle_"))
async def cb_mon_toggle(callback: CallbackQuery):
    # формат: mon_toggle_{key}_{user_id}
    parts = callback.data.split("_")
    key = parts[2]
    uid = int(parts[3])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    sess = user_sessions.get(uid)
    if not sess:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    mon = sess.setdefault("monitors", DEFAULT_MONITORS.copy())
    mon[key] = not mon.get(key, True)
    save_sessions()
    labels = {"messages": "Сообщения", "offline": "Оффлайны", "online": "Онлайны"}
    state_str = "включён ✅" if mon[key] else "выключен ❌"
    await callback.answer(f"{labels.get(key, key)} {state_str}")
    await callback.message.edit_reply_markup(reply_markup=monitors_keyboard(uid))


# ======================== CALLBACK: ПРОЧЕЕ ========================
@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


# ======================== ADMIN ========================
@dp.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Нет доступа.")
        return
    if not user_sessions:
        await message.answer("Нет активных пользователей.")
        return
    text = "👥 <b>Активные пользователи:</b>\n\n"
    for uid, sess in user_sessions.items():
        status = "🟢" if sess.get("running") else "🔴"
        text += f"{status} ID: {uid}, профилей: {len(sess.get('list_of_id', []))}\n"
    await message.answer(text, parse_mode="HTML")


# ======================== ЗАПУСК ========================
async def main():
    logger.info("Bot started")

    saved = load_sessions()
    for uid_str, data in saved.items():
        uid = int(uid_str)
        bearer = data.get("bearer", "")
        chat_id = data.get("chat_id", uid)
        multiplier = data.get("interval_multiplier", 1.0)
        was_running = data.get("running", False)
        monitors = data.get("monitors", DEFAULT_MONITORS.copy())

        if not bearer:
            continue

        user_sessions[uid] = {
            "bearer": bearer,
            "chat_id": chat_id,
            "running": False,  # не стартуем автоматически
            "interval_multiplier": multiplier,
            "monitors": monitors,
            "task": None,
            "name_id": {},
            "list_of_id": [],
        }

        # Уведомляем только тех, у кого мониторинг был включён
        if was_running:
            try:
                await bot.send_message(
                    chat_id,
                    "🔄 <b>Бот перезапущен.</b>\nХотите возобновить мониторинг?",
                    parse_mode="HTML",
                    reply_markup=resume_keyboard(uid),
                )
            except Exception as e:
                logger.warning(
                    f"Не удалось отправить сообщение пользователю {uid}: {e}"
                )

        logger.info(f"Restored session for user {uid} (was_running={was_running})")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
