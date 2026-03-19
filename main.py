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

# Bearer протухает через ~30 дней, но обновляем за 30 минут до истечения.
# Минимальный интервал между запросами на новый токен — 1 час (защита от петли).
BEARER_REFRESH_BEFORE_EXPIRY = timedelta(minutes=30)
BEARER_MIN_REFRESH_INTERVAL = timedelta(hours=1)

# Newsfeed: длина смены и порог — предупреждаем если до дедлайна < 8 ч (смена)
NEWSFEED_INTERVAL = timedelta(hours=12)
SHIFT_DURATION = timedelta(hours=8)
NEWSFEED_WARN_BEFORE = timedelta(minutes=0)  # уведомлять когда истекло
NEWSFEED_CHECK_INTERVAL = 30  # секунд между проверками newsfeed

IB_INTERVAL = timedelta(hours=6)  # icebreakers: порог устаревания
IB_CHECK_INTERVAL = 30 * 60  # секунд между проверками icebreakers (30 мин)

DEDUP_WINDOW = timedelta(minutes=8)  # антидубль: не повторять пару (girl_id, user_id)
SNOOZE_OPTIONS = [  # варианты игнора (секунды, метка)
    (15 * 60, "15м"),
    (30 * 60, "30м"),
    (60 * 60, "1ч"),
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ======================== СОСТОЯНИЯ ========================
class BearerState(StatesGroup):
    waiting_for_bearer = State()


class CredentialsState(StatesGroup):
    waiting_for_credentials = State()


# ======================== ХРАНИЛИЩЕ ========================
# Добавлены поля:
#   credentials: {"login": str, "password": str} | None  — для авто-обновления Bearer
#   bearer_expires_at: datetime | None                    — UTC время истечения токена
#   bearer_last_refreshed: datetime | None                — UTC время последнего обновления
#   newsfeed_reminded: set[str]                           — girl_id'ы, по которым уже послали напоминание
# Структура сессии (дополнительные поля):
#   dedup: dict[(girl_id, user_id), datetime]  — время последней отправки уведомления
#   snooze: dict[(girl_id, user_id), datetime] — время до которого пара игнорируется
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
            "credentials": sess.get("credentials"),  # {"login": ..., "password": ...}
            "bearer_expires_at": (
                sess["bearer_expires_at"].isoformat()
                if sess.get("bearer_expires_at")
                else None
            ),
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
    """Парсит дату из API чатов (формат без миллисекунд) → UTC aware."""
    return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _parse_newsfeed_dt(dt_str: str) -> datetime:
    """Парсит дату из newsfeed API (формат с миллисекундами) → UTC aware."""
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_timedelta(td: timedelta) -> str:
    """Форматирует timedelta в читаемый вид: '2ч 15мин'."""
    total = int(td.total_seconds())
    if total <= 0:
        return "прямо сейчас"
    h, rem = divmod(total, 3600)
    m = rem // 60
    if h and m:
        return f"{h}ч {m}мин"
    elif h:
        return f"{h}ч"
    else:
        return f"{m}мин"


# ======================== API ========================
BASE_URL = "https://mcs-1.chat-space.ai:8001"

_API_HEADERS_BASE = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


async def fetch_new_bearer(
    session: aiohttp.ClientSession, login: str, password: str
) -> Optional[dict]:
    """
    Получает новый Bearer по логину и паролю.
    Возвращает {"token": str, "expires_at": datetime} или None при ошибке.
    """
    url = f"{BASE_URL}/identity/auth/token"
    payload = {"username": login, "password": password}
    try:
        async with session.post(
            url,
            headers=_API_HEADERS_BASE,
            json=payload,
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.error(f"fetch_new_bearer error {resp.status}")
                return None
            data = await resp.json()
            token = data.get("accessToken")
            if not token:
                logger.error("fetch_new_bearer: no accessToken in response")
                return None

            # Пробуем вытащить expiration из JWT payload (средняя часть base64)
            expires_at = None
            try:
                import base64, json as _json

                payload_b64 = token.split(".")[1]
                # Добиваем до кратного 4 длины
                payload_b64 += "=" * (4 - len(payload_b64) % 4)
                jwt_payload = _json.loads(base64.b64decode(payload_b64))
                exp_ts = jwt_payload.get("expirationDate")  # Unix timestamp или ISO
                if isinstance(exp_ts, (int, float)):
                    expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
                elif isinstance(exp_ts, str):
                    # ISO формат
                    expires_at = datetime.fromisoformat(exp_ts.replace("Z", "+00:00"))
            except Exception as e:
                logger.warning(f"Could not parse JWT expiry: {e}")
                # Если не смогли — считаем что живёт 30 дней
                expires_at = _now_utc() + timedelta(days=30)

            return {"token": token, "expires_at": expires_at}
    except Exception as e:
        logger.error(f"fetch_new_bearer exception: {e}")
        return None


async def api_get(
    session: aiohttp.ClientSession, url: str, bearer: str
) -> Optional[dict]:
    headers = {**_API_HEADERS_BASE, "Authorization": f"Bearer {bearer}"}
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


async def api_post(
    session: aiohttp.ClientSession, url: str, bearer: str, payload: dict
) -> Optional[dict]:
    headers = {
        **_API_HEADERS_BASE,
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status == 400:
                return {"status": 400}
            if resp.status != 200:
                logger.error(f"API POST error {resp.status}: {url}")
                return None
            return {"status": 200}
    except Exception as e:
        logger.error(f"api_post error: {e}")
        return None


async def get_approved_newsfeed_item(
    session: aiohttp.ClientSession, bearer: str, profile_id: str
) -> Optional[dict]:
    """
    Возвращает первый APPROVED newsfeed для анкеты:
    {"id": ..., "type": ...} или None.
    """
    url = f"{BASE_URL}/operator/news-feed?profileId=pd-{profile_id}&status=APPROVED&idLast=0"
    data = await api_get(session, url, bearer)
    if not data or not isinstance(data, list) or len(data) == 0:
        return None
    try:
        item = data[0]
        nf_id = item["id"]
        nf_type = item["content"]["media"][0]["type"]
        return {"id": nf_id, "type": nf_type}
    except Exception as e:
        logger.error(f"get_approved_newsfeed_item parse error: {e}")
        return None


async def send_newsfeed_for_profile(
    session: aiohttp.ClientSession, bearer: str, profile_id: str
) -> str:
    """
    Обновляет newsfeed для одной анкеты.
    Возвращает: "ok" | "no_need" | "no_content" | "error"
    """
    item = await get_approved_newsfeed_item(session, bearer, profile_id)
    if item is None:
        return "no_content"
    url = f"{BASE_URL}/operator/news-feed"
    payload = {
        "profileId": f"pd-{profile_id}",
        "id": item["id"],
        "type": item["type"],
    }
    result = await api_post(session, url, bearer, payload)
    if result is None:
        return "error"
    if result.get("status") == 400:
        return "no_need"
    return "ok"


async def api_get_with_refresh(
    http: aiohttp.ClientSession,
    url: str,
    user_id: int,
    chat_id: int,
) -> Optional[dict]:
    """
    Выполняет GET-запрос. При 401 пробует обновить Bearer (если есть credentials).
    Обновляет user_sessions[user_id]["bearer"] на лету.
    """
    sess = user_sessions[user_id]
    bearer = sess["bearer"]
    headers = {**_API_HEADERS_BASE, "Authorization": f"Bearer {bearer}"}
    try:
        async with http.get(
            url, headers=headers, ssl=False, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 401:
                new_bearer = await _try_refresh_bearer(http, user_id, chat_id)
                if new_bearer:
                    # Повторяем запрос с новым токеном
                    new_headers = {
                        **_API_HEADERS_BASE,
                        "Authorization": f"Bearer {new_bearer}",
                    }
                    async with http.get(
                        url,
                        headers=new_headers,
                        ssl=False,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp2:
                        if resp2.status != 200:
                            return None
                        return await resp2.json()
                return None
            if resp.status != 200:
                logger.error(f"API error {resp.status}: {url}")
                return None
            return await resp.json()
    except Exception as e:
        logger.error(f"Request error: {e}")
        return None


async def _try_refresh_bearer(
    http: aiohttp.ClientSession, user_id: int, chat_id: int
) -> Optional[str]:
    """
    Пробует получить новый Bearer. Возвращает новый токен или None.
    Защита от частых запросов: не чаще BEARER_MIN_REFRESH_INTERVAL.
    """
    sess = user_sessions.get(user_id)
    if not sess:
        return None

    creds = sess.get("credentials")
    if not creds:
        await bot.send_message(
            chat_id,
            "⚠️ <b>Bearer токен устарел</b>, но логин/пароль не сохранены.\n"
            "Используй /setbearer для ввода нового токена вручную или "
            "/setcredentials для сохранения логина и пароля.",
            parse_mode="HTML",
        )
        return None

    last_refresh = sess.get("bearer_last_refreshed")
    if last_refresh and _now_utc() - last_refresh < BEARER_MIN_REFRESH_INTERVAL:
        logger.warning(f"Bearer refresh throttled for user {user_id}")
        return None

    result = await fetch_new_bearer(http, creds["login"], creds["password"])
    if not result:
        await bot.send_message(
            chat_id,
            "❌ Не удалось автоматически обновить Bearer.\n"
            "Проверь логин/пароль командой /setcredentials.",
            parse_mode="HTML",
        )
        return None

    sess["bearer"] = result["token"]
    sess["bearer_expires_at"] = result["expires_at"]
    sess["bearer_last_refreshed"] = _now_utc()
    save_sessions()

    await bot.send_message(
        chat_id,
        f"🔄 Bearer токен автоматически обновлён.\n"
        f"Действует до: {result['expires_at'].strftime('%d.%m.%Y %H:%M')} UTC",
        parse_mode="HTML",
    )
    return result["token"]


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


async def check_letters_available(
    session: aiohttp.ClientSession, bearer: str, list_of_id: list
) -> list:
    """
    Проверяет онлайн-пользователей у всех анкет.
    Возвращает тех у кого lettersLeft > 0 и messagesLeft > 1.
    """
    found = []
    for girl_id in list_of_id:
        data = await get_users_raw(session, bearer, girl_id, online=True)
        if not data:
            continue
        for user in data.get("dialogs", []):
            customer_id = user["customer"]["id"]
            profile_id = user["profileId"]
            url = f"{BASE_URL}/operator/chat/restriction?profileId={profile_id}&customerId={customer_id}"
            restriction = await api_get(session, url, bearer)
            if not restriction:
                continue
            letters_left = restriction.get("lettersLeft", 0)
            messages_left = restriction.get("messagesLeft", 0)
            if letters_left > 0 and messages_left > 1:
                found.append(
                    {
                        "user_name": user["customer"]["name"],
                        "user_id": customer_id,
                        "girl_id": girl_id,
                        "profile_id": profile_id,
                        "lettersLeft": letters_left,
                        "messagesLeft": messages_left,
                    }
                )
            await asyncio.sleep(0.3)
    return found


# ======================== СМЕНА ========================
WORK_SHIFT_ID = 4980


async def get_profile_emails(session: aiohttp.ClientSession, bearer: str) -> dict:
    """
    GET /balance/profile → возвращает словарь {girl_id: email}.
    girl_id здесь без префикса pd-.
    """
    url = f"{BASE_URL}/balance/profile"
    data = await api_get(session, url, bearer)
    if not data:
        return {}
    result = {}
    for item in data.get("balances", []):
        profile_id = item.get("profileId", "").replace("pd-", "")
        email = item.get("email", "")
        if profile_id and email:
            result[profile_id] = email
    return result


async def set_shift_for_profile(
    session: aiohttp.ClientSession,
    bearer: str,
    girl_id: str,
    name: str,
    email: str,
) -> bool:
    """
    PATCH /identity/profiles/pd-{girl_id}
    Сначала GET чтобы забрать текущие поля профиля (scope и др.),
    потом PATCH с полным объектом — только workShiftId меняем.
    """
    url = f"{BASE_URL}/identity/profiles/pd-{girl_id}"
    headers = {
        **_API_HEADERS_BASE,
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }

    # Шаг 1: получаем текущий профиль
    current = await api_get(session, url, bearer)
    if current is None:
        logger.error(f"set_shift: не удалось получить профиль {girl_id}")
        return False

    # Шаг 2: берём текущий объект и меняем только нужные поля
    payload = {**current, "name": name, "email": email, "workShiftId": WORK_SHIFT_ID}

    try:
        async with session.patch(
            url,
            headers=headers,
            json=payload,
            ssl=False,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status not in (200, 204):
                logger.error(f"set_shift error {resp.status} for {girl_id}")
                return False
            return True
    except Exception as e:
        logger.error(f"set_shift exception for {girl_id}: {e}")
        return False


# ======================== NEWSFEED ========================
async def get_newsfeed_statuses(session: aiohttp.ClientSession, bearer: str) -> list:
    """Возвращает сырой список статусов newsfeed."""
    url = f"{BASE_URL}/operator/news-feed/statuses"
    data = await api_get(session, url, bearer)
    return data if isinstance(data, list) else []


async def fetch_newsfeed_info(
    session: aiohttp.ClientSession, bearer: str, name_id: dict
) -> list:
    """
    Возвращает список анкет с информацией о newsfeed:
    [{"girl_id": str, "name": str, "time_left": timedelta, "deadline": datetime}]
    Только те, у кого time_left > 0 (т.е. ещё не просрочено).
    """
    raw = await get_newsfeed_statuses(session, bearer)
    result = []
    now = _now_utc()
    for item in raw:
        try:
            published = _parse_newsfeed_dt(item["publishedDate"])
            deadline = published + NEWSFEED_INTERVAL
            time_left = deadline - now
            girl_id = item["profileId"].replace("pd-", "")
            name = name_id.get(girl_id, girl_id)
            result.append(
                {
                    "girl_id": girl_id,
                    "name": name,
                    "time_left": time_left,
                    "deadline": deadline,
                }
            )
        except Exception:
            pass
    return result


def _newsfeed_report_lines(items: list) -> list[str]:
    """Формирует строки для отчёта о newsfeed."""
    lines = []
    now = _now_utc()
    for item in items:
        tl = item["time_left"]
        name = item["name"]
        if tl.total_seconds() <= 0:
            lines.append(f"🔴 <b>{name}</b> — просрочено!")
        elif tl <= SHIFT_DURATION:
            lines.append(f"⚠️ <b>{name}</b> — через {_format_timedelta(tl)}")
        else:
            lines.append(f"✅ {name} — через {_format_timedelta(tl)}")
    return lines


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
        f"\n{important}"
    )


def newsfeed_update_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Кнопка для обновления newsfeed на всех просроченных анкетах."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить NF на всех просроченных",
                    callback_data=f"nf_update_all_{user_id}",
                )
            ]
        ]
    )


def snooze_keyboard(girl_id: str, user_id: str) -> InlineKeyboardMarkup:
    """Маленькая строка кнопок игнора под обычным (не важным) уведомлением."""
    buttons = [
        InlineKeyboardButton(
            text=label,
            callback_data=f"snooze_{seconds}_{girl_id}_{user_id}",
        )
        for seconds, label in SNOOZE_OPTIONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _is_snoozed(sess: dict, girl_id: str, user_id: str) -> bool:
    """Проверяет активен ли игнор для пары (girl_id, user_id)."""
    key = (girl_id, user_id)
    snooze_until = sess.get("snooze", {}).get(key)
    return snooze_until is not None and _now_utc() < snooze_until


def _is_dedup(sess: dict, girl_id: str, user_id: str) -> bool:
    """Проверяет не было ли уведомления по этой паре в последние DEDUP_WINDOW минут."""
    key = (girl_id, user_id)
    last_sent = sess.get("dedup", {}).get(key)
    return last_sent is not None and _now_utc() - last_sent < DEDUP_WINDOW


def _mark_sent(sess: dict, girl_id: str, user_id: str):
    """Отмечает что уведомление по паре только что отправлено."""
    if "dedup" not in sess:
        sess["dedup"] = {}
    sess["dedup"][(girl_id, user_id)] = _now_utc()


def get_interval_seconds(multiplier: float) -> str:
    lo = int(BASE_PROFILE_INTERVAL_MIN * multiplier)
    hi = int(BASE_PROFILE_INTERVAL_MAX * multiplier)
    return f"{lo}–{hi}с"


def admin_keyboard(user_id: int) -> InlineKeyboardMarkup:
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
                    text="🔎 Одноразовые проверки",
                    callback_data=f"checks_panel_{user_id}",
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
                    text="🗓 Управление сменой", callback_data=f"shift_panel_{user_id}"
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


def checks_panel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Подпанель одноразовых проверок."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📨 Проверить уведомления",
                    callback_data=f"check_msg_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📴 Проверить оффлайны",
                    callback_data=f"check_offline_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🟢 Проверить онлайны",
                    callback_data=f"check_online_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✉️ Проверить письма",
                    callback_data=f"check_letters_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data=f"back_panel_{user_id}"
                )
            ],
        ]
    )


def shift_panel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Подпанель управления сменой."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📰 Newsfeed",
                    callback_data=f"check_newsfeed_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🧊 Icebreakers",
                    callback_data=f"check_ib_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏰ Выставить смену (15:00–23:00)",
                    callback_data=f"shift_set_{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data=f"back_panel_{user_id}"
                )
            ],
        ]
    )


def shift_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения выставления смены."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=f"shift_confirm_{user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"shift_panel_{user_id}"
                ),
            ]
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
        if "dedup" not in session_data:
            session_data["dedup"] = {}
        if "snooze" not in session_data:
            session_data["snooze"] = {}

        profiles_text = "\n".join(f"• {name} ({id_})" for id_, name in name_id.items())
        await bot.send_message(
            chat_id,
            f"✅ <b>Мониторинг запущен</b>\n\n📋 Профили ({len(list_of_id)}):\n{profiles_text}",
            parse_mode="HTML",
        )

        # --- Newsfeed: проверяем при старте ---
        await _check_and_notify_newsfeed(session, user_id, chat_id, startup=True)
        session_data["newsfeed_reminded"] = set()
        session_data["ib_notified"] = set()

        # --- Icebreakers: проверяем при старте ---
        await _check_and_notify_icebreakers(session, user_id, chat_id)

        # --- Bearer: проверяем при старте нужно ли скоро обновить ---
        await _check_bearer_expiry(session, user_id, chat_id)

        next_check = {
            id_: asyncio.get_event_loop().time() + random.randint(0, 20)
            for id_ in list_of_id
        }
        message_interval = random.randint(
            BASE_MESSAGE_INTERVAL_MIN, BASE_MESSAGE_INTERVAL_MAX
        )
        next_message_check = asyncio.get_event_loop().time() + message_interval

        # Newsfeed проверяем каждые 30 секунд
        next_newsfeed_check = asyncio.get_event_loop().time() + 30

        # Icebreakers проверяем каждые 30 минут
        next_ib_check = asyncio.get_event_loop().time() + IB_CHECK_INTERVAL

        # Bearer expiry проверяем каждые 10 минут
        next_bearer_check = asyncio.get_event_loop().time() + 600

        while session_data.get("running", False):
            now = asyncio.get_event_loop().time()
            multiplier = session_data.get("interval_multiplier", 1.0)
            mon = session_data.get("monitors", DEFAULT_MONITORS.copy())
            # Всегда берём актуальный bearer из сессии (мог обновиться)
            bearer = session_data["bearer"]

            for id_ in list_of_id:
                if now >= next_check[id_]:
                    if mon.get("online") or mon.get("offline"):
                        users = []

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
                            gid = user["girl_id"]
                            uid_str = user["user_id"]
                            is_important = user["status"] == "unanswered"

                            # Важные (unanswered) — всегда отправляем, без антидубля и игнора
                            if not is_important:
                                if _is_snoozed(session_data, gid, uid_str):
                                    continue
                                if _is_dedup(session_data, gid, uid_str):
                                    continue

                            _mark_sent(session_data, gid, uid_str)

                            text = format_user_alert(user, name_id)
                            if is_important:
                                await bot.send_message(chat_id, text, parse_mode="HTML")
                            else:
                                await bot.send_message(
                                    chat_id,
                                    text,
                                    parse_mode="HTML",
                                    reply_markup=snooze_keyboard(gid, uid_str),
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

            # Проверка newsfeed (когда истекло)
            if now >= next_newsfeed_check:
                await _newsfeed_remind_if_needed(session, user_id, chat_id)
                next_newsfeed_check = now + 30

            # Проверка icebreakers каждые 30 минут
            if now >= next_ib_check:
                await _check_and_notify_icebreakers(session, user_id, chat_id)
                next_ib_check = now + IB_CHECK_INTERVAL

            # Проверка Bearer на истечение
            if now >= next_bearer_check:
                await _check_bearer_expiry(session, user_id, chat_id)
                next_bearer_check = now + 600

            await asyncio.sleep(1)

    session_data["running"] = False
    save_sessions()
    await bot.send_message(chat_id, "⏹ Мониторинг остановлен.")


# ======================== ICEBREAKERS ========================
async def get_icebreakers(
    session: aiohttp.ClientSession, bearer: str, girl_id: str
) -> list:
    url = f"{BASE_URL}/scheduler/icebreakers/in-progress?profileId=pd-{girl_id}"
    data = await api_get(session, url, bearer)
    return data if isinstance(data, list) else []


async def check_icebreakers_outdated(
    session: aiohttp.ClientSession, bearer: str, list_of_id: list, name_id: dict
) -> list:
    """
    Возвращает список анкет у которых хотя бы один icebreaker
    не обновлялся более IB_INTERVAL (6 часов).
    Одна запись на анкету — имя + сколько времени прошло с последнего запуска.
    """
    outdated = []
    now = _now_utc()
    for girl_id in list_of_id:
        items = await get_icebreakers(session, bearer, girl_id)
        if not items:
            await asyncio.sleep(0.3)
            continue
        # Берём максимальное dateLastLaunched среди всех постов анкеты
        latest = None
        for item in items:
            try:
                dt = _parse_newsfeed_dt(item["dateLastLaunched"])
                if latest is None or dt > latest:
                    latest = dt
            except Exception:
                pass
        if latest is not None:
            idle = now - latest
            if idle > IB_INTERVAL:
                outdated.append(
                    {
                        "girl_id": girl_id,
                        "name": name_id.get(girl_id, girl_id),
                        "idle": idle,
                    }
                )
        await asyncio.sleep(0.3)
    return outdated


async def _check_and_notify_icebreakers(
    session: aiohttp.ClientSession, user_id: int, chat_id: int
):
    """Проверяет icebreakers и уведомляет об устаревших анкетах."""
    sess = user_sessions.get(user_id, {})
    list_of_id = sess.get("list_of_id", [])
    name_id = sess.get("name_id", {})
    bearer = sess.get("bearer", "")
    ib_notified: set = sess.get("ib_notified", set())

    outdated = await check_icebreakers_outdated(session, bearer, list_of_id, name_id)

    # Уведомляем только по тем анкетам, по которым ещё не слали в этом цикле
    new_outdated = [o for o in outdated if o["girl_id"] not in ib_notified]

    # Сбрасываем notified для тех, кто больше не устарел (обновили)
    current_outdated_ids = {o["girl_id"] for o in outdated}
    ib_notified &= current_outdated_ids
    sess["ib_notified"] = ib_notified

    if new_outdated:
        lines = ["🧊 <b>Icebreakers требуют обновления:</b>\n"]
        for o in new_outdated:
            lines.append(
                f"⚠️ <b>{o['name']}</b> — последний запуск {_format_timedelta(o['idle'])} назад"
            )
            ib_notified.add(o["girl_id"])
        sess["ib_notified"] = ib_notified
        await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


# ======================== NEWSFEED ЛОГИКА ========================
async def _check_and_notify_newsfeed(
    session: aiohttp.ClientSession,
    user_id: int,
    chat_id: int,
    startup: bool = False,
):
    """
    При startup=True — пишем полный отчёт: только анкеты, у которых
    дедлайн наступит в пределах текущей смены (< 8ч), и просроченные.
    Остальные — молча, они не актуальны для этой смены.
    """
    sess = user_sessions.get(user_id, {})
    name_id = sess.get("name_id", {})
    bearer = sess.get("bearer", "")

    items = await fetch_newsfeed_info(session, bearer, name_id)
    if not items:
        return

    urgent = [i for i in items if i["time_left"] <= SHIFT_DURATION]
    overdue = [i for i in urgent if i["time_left"].total_seconds() <= 0]
    soon = [i for i in urgent if i["time_left"].total_seconds() > 0]

    if not urgent:
        if startup:
            await bot.send_message(
                chat_id,
                "📰 <b>Newsfeed:</b> все анкеты в порядке, в этой смене обновлять не нужно.",
                parse_mode="HTML",
            )
        return

    lines = ["📰 <b>Newsfeed — требует внимания в эту смену:</b>\n"]
    for item in overdue:
        lines.append(f"🔴 <b>{item['name']}</b> — просрочено! Нужно обновить сейчас.")
    for item in soon:
        lines.append(
            f"⚠️ <b>{item['name']}</b> — через {_format_timedelta(item['time_left'])}"
            f" (до {item['deadline'].strftime('%H:%M')} UTC)"
        )

    await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


async def _newsfeed_remind_if_needed(
    session: aiohttp.ClientSession, user_id: int, chat_id: int
):
    """
    Проверяет newsfeed и отправляет напоминание за NEWSFEED_WARN_BEFORE (15 мин)
    до дедлайна. Каждой анкете — не более одного напоминания за цикл.
    """
    sess = user_sessions.get(user_id, {})
    name_id = sess.get("name_id", {})
    bearer = sess.get("bearer", "")
    reminded: set = sess.get("newsfeed_reminded", set())

    items = await fetch_newsfeed_info(session, bearer, name_id)
    new_reminders = []

    for item in items:
        gid = item["girl_id"]
        tl = item["time_left"]
        # Напоминаем если осталось меньше NEWSFEED_WARN_BEFORE и ещё не напоминали
        if timedelta(0) < tl <= NEWSFEED_WARN_BEFORE and gid not in reminded:
            new_reminders.append(item)
            reminded.add(gid)
        # Если уже просрочено и не напоминали — тоже предупреждаем
        elif tl.total_seconds() <= 0 and gid not in reminded:
            new_reminders.append(item)
            reminded.add(gid)

    sess["newsfeed_reminded"] = reminded

    if new_reminders:
        lines = ["⏰ <b>Напоминание Newsfeed:</b>\n"]
        has_overdue = False
        for item in new_reminders:
            tl = item["time_left"]
            if tl.total_seconds() <= 0:
                lines.append(f"🔴 <b>{item['name']}</b> — уже просрочено!")
                has_overdue = True
            else:
                lines.append(
                    f"⚠️ <b>{item['name']}</b> — до дедлайна {_format_timedelta(tl)}!"
                )
        # Если есть просроченные — предлагаем обновить одной кнопкой
        if has_overdue:
            lines.append(
                "\nНажми кнопку ниже, чтобы автоматически обновить NF на всех просроченных анкетах."
            )
            await bot.send_message(
                chat_id,
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=newsfeed_update_keyboard(user_id),
            )
        else:
            await bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


# ======================== BEARER EXPIRY ========================
async def _check_bearer_expiry(
    session: aiohttp.ClientSession, user_id: int, chat_id: int
):
    """
    Проверяет не истекает ли Bearer. Если до истечения < BEARER_REFRESH_BEFORE_EXPIRY
    и есть credentials — обновляет автоматически.
    Если credentials нет — предупреждает пользователя.
    """
    sess = user_sessions.get(user_id)
    if not sess:
        return

    expires_at = sess.get("bearer_expires_at")
    if not expires_at:
        return

    time_left = expires_at - _now_utc()
    if time_left > BEARER_REFRESH_BEFORE_EXPIRY:
        return  # Ещё рано

    creds = sess.get("credentials")
    if creds:
        logger.info(f"Auto-refreshing bearer for user {user_id}")
        await _try_refresh_bearer(session, user_id, chat_id)
    else:
        # Предупреждаем раз (проверяем что не слали в последние 2 часа)
        last_warn = sess.get("bearer_warn_sent_at")
        if not last_warn or _now_utc() - last_warn > timedelta(hours=2):
            await bot.send_message(
                chat_id,
                f"⚠️ <b>Bearer токен истекает через {_format_timedelta(time_left)}.</b>\n"
                f"Обнови его вручную /setbearer или сохрани логин/пароль /setcredentials "
                f"для автообновления.",
                parse_mode="HTML",
            )
            sess["bearer_warn_sent_at"] = _now_utc()


# ======================== КОМАНДЫ ========================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет!</b>\n\n"
        "Это бот-мониторинг чатов.\n"
        "Отправь свой <b>Bearer токен</b> командой /setbearer или через меню ниже. "
        "Если ты не знаешь что такое Bearer - введи команду /bearer",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("setbearer"))
async def cmd_setbearer(message: Message, state: FSMContext):
    await message.answer("🔑 Отправь Bearer токен следующим сообщением:")
    await state.set_state(BearerState.waiting_for_bearer)


@dp.message(Command("setcredentials"))
async def cmd_setcredentials(message: Message, state: FSMContext):
    await message.answer(
        "🔐 Отправь логин и пароль одним сообщением через пробел:\n"
        "<code>email@example.com пароль123</code>\n\n"
        "Сообщение будет удалено сразу после получения.",
        parse_mode="HTML",
    )
    await state.set_state(CredentialsState.waiting_for_credentials)


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
        "/setbearer - установить Bearer вручную\n"
        "/setcredentials - сохранить логин/пароль для авто-обновления Bearer\n"
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

    prev_monitors = (
        user_sessions[user_id].get("monitors", DEFAULT_MONITORS.copy())
        if user_id in user_sessions
        else DEFAULT_MONITORS.copy()
    )
    prev_creds = (
        user_sessions[user_id].get("credentials") if user_id in user_sessions else None
    )

    # Пробуем вытащить expires_at из JWT
    bearer_expires_at = None
    try:
        import base64, json as _json

        payload_b64 = bearer.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        jwt_payload = _json.loads(base64.b64decode(payload_b64))
        exp_ts = jwt_payload.get("expirationDate")
        if isinstance(exp_ts, (int, float)):
            bearer_expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        elif isinstance(exp_ts, str):
            bearer_expires_at = datetime.fromisoformat(exp_ts.replace("Z", "+00:00"))
    except Exception:
        pass

    user_sessions[user_id] = {
        "bearer": bearer,
        "bearer_expires_at": bearer_expires_at,
        "bearer_last_refreshed": None,
        "chat_id": message.chat.id,
        "running": True,
        "interval_multiplier": 1.0,
        "monitors": prev_monitors,
        "credentials": prev_creds,
        "task": None,
        "name_id": {},
        "list_of_id": [],
        "newsfeed_reminded": set(),
    }
    save_sessions()

    task = asyncio.create_task(monitoring_task(user_id, message.chat.id))
    user_sessions[user_id]["task"] = task

    await state.clear()

    expiry_text = ""
    if bearer_expires_at:
        expiry_text = (
            f"\n🗓 Токен действует до: {bearer_expires_at.strftime('%d.%m.%Y')} UTC"
        )

    await message.answer(
        f"✅ Bearer принят! Запускаю мониторинг...{expiry_text}\n"
        "Используй /panel для управления.",
        reply_markup=admin_keyboard(user_id),
    )


@dp.message(CredentialsState.waiting_for_credentials)
async def process_credentials(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()

    try:
        await message.delete()
    except Exception:
        pass

    parts = text.split(" ", 1)
    if len(parts) != 2:
        await message.answer(
            "❌ Неверный формат. Отправь логин и пароль через пробел:\n"
            "<code>email@example.com пароль123</code>",
            parse_mode="HTML",
        )
        return

    login, password = parts

    # Проверяем что credentials рабочие — получаем новый Bearer
    async with aiohttp.ClientSession() as http:
        result = await fetch_new_bearer(http, login, password)

    if not result:
        await message.answer(
            "❌ Не удалось войти с этими данными. Проверь логин и пароль."
        )
        await state.clear()
        return

    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "bearer": result["token"],
            "bearer_expires_at": result["expires_at"],
            "bearer_last_refreshed": _now_utc(),
            "chat_id": message.chat.id,
            "running": False,
            "interval_multiplier": 1.0,
            "monitors": DEFAULT_MONITORS.copy(),
            "credentials": {"login": login, "password": password},
            "task": None,
            "name_id": {},
            "list_of_id": [],
            "newsfeed_reminded": set(),
        }
    else:
        user_sessions[user_id]["credentials"] = {"login": login, "password": password}
        user_sessions[user_id]["bearer"] = result["token"]
        user_sessions[user_id]["bearer_expires_at"] = result["expires_at"]
        user_sessions[user_id]["bearer_last_refreshed"] = _now_utc()

    save_sessions()
    await state.clear()

    await message.answer(
        f"✅ Логин и пароль сохранены. Bearer обновлён автоматически.\n"
        f"🗓 Действует до: {result['expires_at'].strftime('%d.%m.%Y')} UTC\n\n"
        f"Теперь бот будет сам обновлять токен при истечении.",
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
@dp.callback_query(F.data.startswith("checks_panel_"))
async def cb_checks_panel(callback: CallbackQuery):
    uid = int(callback.data.split("_")[2])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    if uid not in user_sessions:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    await callback.message.edit_text(
        "🔎 <b>Одноразовые проверки</b>",
        parse_mode="HTML",
        reply_markup=checks_panel_keyboard(uid),
    )
    await callback.answer()


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
    await callback.answer("📨 Проверяю...")
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


@dp.callback_query(F.data.startswith("check_letters_"))
async def cb_check_letters(callback: CallbackQuery):
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
    await callback.answer("✉️ Проверяю письма...")
    async with aiohttp.ClientSession() as http_session:
        found = await check_letters_available(
            http_session, session["bearer"], list_of_id
        )
    if found:
        await callback.message.answer(
            f"✉️ <b>Доступны письма ({len(found)}):</b>", parse_mode="HTML"
        )
        for u in found:
            girl_name = name_id.get(u["girl_id"], u["girl_id"])
            await callback.message.answer(
                f"👤 <b>{u['user_name']}</b>\n"
                f"📋 Анкета: {girl_name}\n"
                f"✉️ Писем: {u['lettersLeft']}  💬 Сообщений: {u['messagesLeft']}",
                parse_mode="HTML",
            )
    else:
        await callback.message.answer("✅ Пользователей с доступными письмами нет.")


# ======================== CALLBACK: ПОДПАНЕЛЬ СМЕНЫ ========================
@dp.callback_query(F.data.startswith("shift_panel_"))
async def cb_shift_panel(callback: CallbackQuery):
    uid = int(callback.data.split("_")[2])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "🗓 <b>Управление сменой</b>",
        parse_mode="HTML",
        reply_markup=shift_panel_keyboard(uid),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("shift_set_"))
async def cb_shift_set(callback: CallbackQuery):
    """Показывает список анкет и просит подтвердить выставление смены."""
    uid = int(callback.data.split("_")[2])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    sess = user_sessions.get(uid)
    if not sess:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return

    list_of_id = sess.get("list_of_id", [])
    name_id = sess.get("name_id", {})

    if not list_of_id:
        await callback.answer(
            "❌ Анкеты не загружены. Сначала запусти мониторинг.", show_alert=True
        )
        return

    await callback.answer("📋 Загружаю список анкет...")

    # Получаем email'ы и сохраняем в сессии
    async with aiohttp.ClientSession() as http:
        emails = await get_profile_emails(http, sess["bearer"])

    sess["profile_emails"] = (
        emails  # кэшируем, чтобы не запрашивать повторно при confirm
    )

    lines = ["⏰ <b>Выставить смену (15:00–23:00) для анкет:</b>\n"]
    missing = []
    for gid in list_of_id:
        name = name_id.get(gid, gid)
        email = emails.get(gid)
        if email:
            lines.append(f"✅ {name}")
        else:
            lines.append(f"⚠️ {name} — email не найден, будет пропущена")
            missing.append(name)

    if missing:
        lines.append(
            f"\n⚠️ {len(missing)} анкет будут пропущены из-за отсутствия email."
        )

    lines.append("\nПодтвердить?")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=shift_confirm_keyboard(uid),
    )


@dp.callback_query(F.data.startswith("shift_confirm_"))
async def cb_shift_confirm(callback: CallbackQuery):
    """Выставляет смену для всех анкет."""
    uid = int(callback.data.split("_")[2])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    sess = user_sessions.get(uid)
    if not sess:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return

    list_of_id = sess.get("list_of_id", [])
    name_id = sess.get("name_id", {})
    emails = sess.get("profile_emails", {})
    bearer = sess["bearer"]

    await callback.answer("⏳ Выставляю смену...")
    await callback.message.edit_text(
        "⏳ Выставляю смену для всех анкет...", parse_mode="HTML"
    )

    ok, failed, skipped = [], [], []

    async with aiohttp.ClientSession() as http:
        for gid in list_of_id:
            name = name_id.get(gid, gid)
            email = emails.get(gid)
            if not email:
                skipped.append(name)
                continue
            success = await set_shift_for_profile(http, bearer, gid, name, email)
            if success:
                ok.append(name)
            else:
                failed.append(name)
            await asyncio.sleep(0.3)  # небольшая пауза между запросами

    lines = ["🗓 <b>Результат выставления смены:</b>\n"]
    for name in ok:
        lines.append(f"✅ {name}")
    for name in failed:
        lines.append(f"❌ {name} — ошибка запроса")
    for name in skipped:
        lines.append(f"⚠️ {name} — нет email, пропущена")

    lines.append(
        f"\n<b>Итого:</b> {len(ok)} успешно, {len(failed)} ошибок, {len(skipped)} пропущено"
    )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="◀️ Назад", callback_data=f"shift_panel_{uid}"
                    )
                ]
            ]
        ),
    )


@dp.callback_query(F.data.startswith("check_ib_"))
async def cb_check_ib(callback: CallbackQuery):
    uid = int(callback.data.split("_")[2])
    if callback.from_user.id != uid:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    sess = user_sessions.get(uid)
    if not sess:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    if not sess.get("list_of_id"):
        await callback.answer(
            "❌ Анкеты не загружены. Дождись запуска мониторинга.", show_alert=True
        )
        return
    await callback.answer("🧊 Проверяю Icebreakers...")
    async with aiohttp.ClientSession() as http:
        outdated = await check_icebreakers_outdated(
            http, sess["bearer"], sess["list_of_id"], sess["name_id"]
        )
    if outdated:
        lines = ["🧊 <b>Icebreakers требуют обновления:</b>\n"]
        for o in outdated:
            lines.append(
                f"⚠️ <b>{o['name']}</b> — последний запуск {_format_timedelta(o['idle'])} назад"
            )
        await callback.message.answer("\n".join(lines), parse_mode="HTML")
    else:
        await callback.message.answer("✅ Все Icebreakers актуальны.")


@dp.callback_query(F.data.startswith("check_newsfeed_"))
async def cb_check_newsfeed(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    session = user_sessions.get(user_id)
    if not session:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return
    name_id = session.get("name_id", {})
    if not name_id:
        await callback.answer(
            "❌ Анкеты не загружены. Дождись запуска мониторинга.", show_alert=True
        )
        return
    await callback.answer("📰 Проверяю Newsfeed...")
    async with aiohttp.ClientSession() as http:
        items = await fetch_newsfeed_info(http, session["bearer"], name_id)

    if not items:
        await callback.message.answer("📰 Данные Newsfeed недоступны.")
        return

    lines = ["📰 <b>Статус Newsfeed:</b>\n"]
    lines += _newsfeed_report_lines(items)
    await callback.message.answer("\n".join(lines), parse_mode="HTML")


# ======================== CALLBACK: ОБНОВЛЕНИЕ NEWSFEED ========================
@dp.callback_query(F.data.startswith("nf_update_all_"))
async def cb_nf_update_all(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[3])
    if callback.from_user.id != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    sess = user_sessions.get(user_id)
    if not sess:
        await callback.answer("❌ Нет сессии", show_alert=True)
        return

    name_id = sess.get("name_id", {})
    bearer = sess.get("bearer", "")

    if not name_id:
        await callback.answer("❌ Анкеты не загружены", show_alert=True)
        return

    await callback.answer("🔄 Обновляю Newsfeed...")

    # Убираем кнопку с сообщения
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    async with aiohttp.ClientSession() as http:
        items = await fetch_newsfeed_info(http, bearer, name_id)

    # Берём только просроченные
    overdue = [i for i in items if i["time_left"].total_seconds() <= 0]

    if not overdue:
        await callback.message.answer(
            "✅ Просроченных Newsfeed нет — всё уже актуально!"
        )
        return

    results_ok = []
    results_no_need = []
    results_no_content = []
    results_error = []

    async with aiohttp.ClientSession() as http:
        for item in overdue:
            gid = item["girl_id"]
            name = item["name"]
            status = await send_newsfeed_for_profile(http, bearer, gid)
            if status == "ok":
                results_ok.append(name)
            elif status == "no_need":
                results_no_need.append(name)
            elif status == "no_content":
                results_no_content.append(name)
            else:
                results_error.append(name)
            await asyncio.sleep(0.5)

    lines = ["📰 <b>Результат обновления Newsfeed:</b>\n"]
    for name in results_ok:
        lines.append(f"✅ <b>{name}</b> — обновлён успешно")
    for name in results_no_need:
        lines.append(f"ℹ️ <b>{name}</b> — обновление не требуется")
    for name in results_no_content:
        lines.append(f"⚠️ <b>{name}</b> — нет одобренного контента")
    for name in results_error:
        lines.append(f"❌ <b>{name}</b> — ошибка запроса")

    # Сбрасываем напоминалки для успешно обновлённых, чтобы не спамить
    reminded: set = sess.get("newsfeed_reminded", set())
    for item in overdue:
        if item["name"] in results_ok:
            reminded.discard(item["girl_id"])
    sess["newsfeed_reminded"] = reminded

    await callback.message.answer("\n".join(lines), parse_mode="HTML")


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

    expires_at = session.get("bearer_expires_at")
    bearer_line = ""
    if expires_at:
        tl = expires_at - _now_utc()
        bearer_line = f"\n🔑 Bearer истекает через: {_format_timedelta(tl)}"

    creds_line = (
        "✅ Авто-обновление настроено"
        if session.get("credentials")
        else "⚠️ Авто-обновление не настроено (/setcredentials)"
    )

    await callback.message.answer(
        f"📊 <b>Статус мониторинга</b>\n\n"
        f"{'🟢 Работает' if running else '🔴 Остановлен'}\n"
        f"⏱ Множитель: x{multiplier:.1f} ({interval_str} между проверками анкеты)\n"
        f"📋 Анкет в работе: {profiles}\n"
        f"{bearer_line}\n"
        f"{creds_line}\n\n"
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


@dp.callback_query(F.data.startswith("snooze_"))
async def cb_snooze(callback: CallbackQuery):
    # формат: snooze_{seconds}_{girl_id}_{user_id}
    parts = callback.data.split("_", 3)
    # parts: ["snooze", seconds, girl_id, user_id]
    seconds = int(parts[1])
    girl_id = parts[2]
    user_id_str = parts[3]

    # Ищем сессию по chat_id
    sess = None
    for uid, s in user_sessions.items():
        if s.get("chat_id") == callback.message.chat.id:
            sess = s
            break

    if not sess:
        await callback.answer("❌ Сессия не найдена", show_alert=True)
        return

    if "snooze" not in sess:
        sess["snooze"] = {}

    until = _now_utc() + timedelta(seconds=seconds)
    sess["snooze"][(girl_id, user_id_str)] = until

    # Убираем кнопки с сообщения
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    label = next((lbl for s, lbl in SNOOZE_OPTIONS if s == seconds), f"{seconds//60}м")
    await callback.answer(f"⏸ Игнор на {label}")


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
        creds = "🔐" if sess.get("credentials") else "🔑"
        text += (
            f"{status}{creds} ID: {uid}, профилей: {len(sess.get('list_of_id', []))}\n"
        )
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
        credentials = data.get("credentials")
        bearer_expires_at_raw = data.get("bearer_expires_at")
        bearer_expires_at = None
        if bearer_expires_at_raw:
            try:
                bearer_expires_at = datetime.fromisoformat(bearer_expires_at_raw)
            except Exception:
                pass

        if not bearer:
            continue

        user_sessions[uid] = {
            "bearer": bearer,
            "bearer_expires_at": bearer_expires_at,
            "bearer_last_refreshed": None,
            "chat_id": chat_id,
            "running": False,
            "interval_multiplier": multiplier,
            "monitors": monitors,
            "credentials": credentials,
            "task": None,
            "name_id": {},
            "list_of_id": [],
            "newsfeed_reminded": set(),
        }

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
