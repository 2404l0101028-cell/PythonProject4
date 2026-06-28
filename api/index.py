import asyncio
import hashlib
import hmac
import json
import os
import random
import time
from fastapi import FastAPI, Request
from itertools import combinations

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# =====================================================================
# НАСТРОЙКИ
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

WORK_COOLDOWN = 60
WORK_ENERGY_COST = 15
TRAIN_ENERGY_COST = 50

# =====================================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# =====================================================================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# =====================================================================
# ПУЛ СОЕДИНЕНИЙ С БД (создаётся один раз на экземпляр функции)
# =====================================================================
_db_pool: asyncpg.Pool | None = None


async def get_db() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await _init_db(_db_pool)
    return _db_pool


async def _init_db(pool: asyncpg.Pool):
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id               BIGINT PRIMARY KEY,
                balance               INTEGER DEFAULT 0,
                level                 INTEGER DEFAULT 1,
                exp                   INTEGER DEFAULT 0,
                job                   TEXT    DEFAULT 'Безработный',
                last_work_time        BIGINT  DEFAULT 0,
                agility               INTEGER DEFAULT 1,
                endurance             INTEGER DEFAULT 1,
                charisma              INTEGER DEFAULT 1,
                intellect             INTEGER DEFAULT 1,
                luck                  INTEGER DEFAULT 1,
                communication_level   INTEGER DEFAULT 1,
                driving_level         INTEGER DEFAULT 0,
                charisma_level        INTEGER DEFAULT 0,
                organization_level    INTEGER DEFAULT 0,
                management_level      INTEGER DEFAULT 0,
                job_rank              INTEGER DEFAULT 1,
                has_scooter           INTEGER DEFAULT 0,
                has_shaker            INTEGER DEFAULT 0,
                has_laptop            INTEGER DEFAULT 0,
                has_professor_badge   INTEGER DEFAULT 0,
                has_logistics_license INTEGER DEFAULT 0,
                has_import_license    INTEGER DEFAULT 0,
                has_dean_seal         INTEGER DEFAULT 0,
                has_business_plan     INTEGER DEFAULT 0,
                has_franchise_contract INTEGER DEFAULT 0,
                hp                    INTEGER DEFAULT 100,
                energy                INTEGER DEFAULT 100
            )
        """)


async def register_user(user_id: int):
    pool = await get_db()
    luck = random.randint(1, 10)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, luck) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            user_id, luck,
        )


async def get_user(user_id: int) -> dict | None:
    pool = await get_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
    return dict(row) if row else None


async def update_user(user_id: int, **kwargs):
    if not kwargs:
        return
    pool = await get_db()
    fields = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(kwargs))
    values = list(kwargs.values())
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE users SET {fields} WHERE user_id = $1",
            user_id, *values,
        )


async def get_user_safe(user_id: int) -> dict:
    await register_user(user_id)
    return await get_user(user_id)


# =====================================================================
# СОСТОЯНИЕ ИГР В ПАМЯТИ
# ВНИМАНИЕ: при перезапуске serverless-функции состояние сбрасывается!
# Для продакшена вынесите bunker_games/poker_games в Redis/Supabase.
# =====================================================================
bunker_games: dict[int, "BunkerGame"] = {}
poker_games: dict[int, "PokerGame"] = {}

# =====================================================================
# ФОРМУЛА ОПЫТА
# =====================================================================
def xp_needed(level: int) -> int:
    return int(120 * (level ** 1.65))

def scale_xp(base_xp: int, level: int) -> int:
    return int(base_xp * (1 + level * 0.08))

def scale_coins(base_coins: int, level: int) -> int:
    return int(base_coins * (1 + level * 0.05))

# =====================================================================
# ВЕТКИ ПРОФЕССИЙ (без изменений из оригинала)
# =====================================================================
JOBS = {
    "intel_1": {
        "name": "Списывальщик на Сом-сынаве", "branch": "intel", "grade": 1,
        "min_level": 1, "min_reward": 8, "max_reward": 14, "min_exp": 30, "max_exp": 50,
        "description": "Ловко списываешь на экзаменах. Немного монет, зато море опыта.",
        "evolves_to": "intel_2", "req_level": 5,
    },
    "intel_2": {
        "name": "Активист Студпарламента, умоляющий не отчислять", "branch": "intel", "grade": 2,
        "min_level": 5, "min_reward": 18, "max_reward": 30, "min_exp": 80, "max_exp": 120,
        "description": "Ходишь по деканатам с умоляющим взглядом. Опыт копится быстро.",
        "evolves_to": "intel_3", "req_level": 10, "req_skill": ("communication_level", 2),
    },
    "intel_3": {
        "name": "Старшекурсник, пишущий дипломные за еду в Джал-Маркете", "branch": "intel", "grade": 3,
        "min_level": 10, "min_reward": 40, "max_reward": 65, "min_exp": 180, "max_exp": 260,
        "description": "Пишешь чужие дипломы за шаурму. Знания растут.",
        "evolves_to": "intel_4", "req_level": 20,
        "req_skill": ("communication_level", 4), "req_item": ("has_laptop", "💻 Ноутбук"),
    },
    "intel_4": {
        "name": "Младший ассистент, который бегает за кофе для кафедры", "branch": "intel", "grade": 4,
        "min_level": 20, "min_reward": 90, "max_reward": 140, "min_exp": 380, "max_exp": 520,
        "description": "Носишь кофе профессорам и тихо всё записываешь.",
        "evolves_to": "intel_5", "req_level": 35,
        "req_skill": ("communication_level", 5), "req_item": ("has_laptop", "💻 Ноутбук"),
    },
    "intel_5": {
        "name": "Строгий препод, который принципиально не ставит «А» автоматом", "branch": "intel", "grade": 5,
        "min_level": 35, "min_reward": 200, "max_reward": 310, "min_exp": 800, "max_exp": 1100,
        "description": "Истязаешь студентов зачётными книжками. Много опыта.",
        "evolves_to": "intel_6", "req_level": 50,
        "req_skill": ("management_level", 3), "req_item": ("has_professor_badge", "🎓 Профессорский значок"),
    },
    "intel_6": {
        "name": "Завкафедрой, потерявший все ведомости", "branch": "intel", "grade": 6,
        "min_level": 50, "min_reward": 400, "max_reward": 600, "min_exp": 1600, "max_exp": 2200,
        "description": "Управляешь кафедрой в перманентном хаосе. Огромный XP.",
        "evolves_to": "intel_7", "req_level": 70,
        "req_skill": ("management_level", 6), "req_item": ("has_professor_badge", "🎓 Профессорский значок"),
    },
    "intel_7": {
        "name": "Декан самого элитного факультета", "branch": "intel", "grade": 7,
        "min_level": 70, "min_reward": 700, "max_reward": 1000, "min_exp": 3000, "max_exp": 4000,
        "description": "Элита элит. Студенты дрожат при виде тебя.",
        "evolves_to": "intel_8", "req_level": 80,
        "req_skill": ("management_level", 8), "req_item": ("has_dean_seal", "🔏 Декановская печать"),
    },
    "intel_8": {
        "name": "Официальный представитель Минобра в КТУ", "branch": "intel", "grade": 8,
        "min_level": 80, "min_reward": 1200, "max_reward": 1700, "min_exp": 5500, "max_exp": 7500,
        "description": "Следишь за всеми и ни за что не отвечаешь.",
        "evolves_to": "intel_9", "req_level": 100,
        "req_skill": ("management_level", 10), "req_item": ("has_dean_seal", "🔏 Декановская печать"),
    },
    "intel_9": {
        "name": "Всемогущий Ректор университета «Манас»", "branch": "intel", "grade": 9,
        "min_level": 100, "min_reward": 2000, "max_reward": 3000, "min_exp": 8000, "max_exp": 12000,
        "description": "Ты — Ректор. Интеллект растёт с каждым уровнем +10.",
        "evolves_to": None, "special": "intellect_x10",
    },
    "balance_1": {
        "name": "Бегун за пирожками в Джал-Маркет на перемене", "branch": "balance", "grade": 1,
        "min_level": 1, "min_reward": 18, "max_reward": 28, "min_exp": 18, "max_exp": 28,
        "description": "Носишься за едой. Всё ровненько: и монеты, и опыт.",
        "evolves_to": "balance_2", "req_level": 5,
    },
    "balance_2": {
        "name": "Таксист на попутке (Джал — Турусбекова)", "branch": "balance", "grade": 2,
        "min_level": 5, "min_reward": 45, "max_reward": 65, "min_exp": 45, "max_exp": 65,
        "description": "Подбираешь студентов по дороге. Баланс идеален.",
        "evolves_to": "balance_3", "req_level": 10, "req_skill": ("driving_level", 2),
    },
    "balance_3": {
        "name": "Пеший курьер, доставляющий прямо в аудитории", "branch": "balance", "grade": 3,
        "min_level": 10, "min_reward": 100, "max_reward": 150, "min_exp": 100, "max_exp": 150,
        "description": "Врываешься в пары с заказами. Преподы в шоке.",
        "evolves_to": "balance_4", "req_level": 20,
        "req_skill": ("driving_level", 3), "req_item": ("has_scooter", "🛵 Скутер"),
    },
    "balance_4": {
        "name": "Владелец точки ксерокопии у главного корпуса", "branch": "balance", "grade": 4,
        "min_level": 20, "min_reward": 220, "max_reward": 320, "min_exp": 220, "max_exp": 320,
        "description": "Ксеришь зачётки и методички. Народу тьма.",
        "evolves_to": "balance_5", "req_level": 35,
        "req_skill": ("organization_level", 4), "req_item": ("has_scooter", "🛵 Скутер"),
    },
    "balance_5": {
        "name": "Организатор студенческих туров на Иссык-Куль", "branch": "balance", "grade": 5,
        "min_level": 35, "min_reward": 480, "max_reward": 680, "min_exp": 480, "max_exp": 680,
        "description": "Вывозишь студентов отдыхать. Все довольны.",
        "evolves_to": "balance_6", "req_level": 50,
        "req_skill": ("organization_level", 6), "req_item": ("has_logistics_license", "📋 Лицензия логиста"),
    },
    "balance_6": {
        "name": "Глава крупной бишкекской службы доставки", "branch": "balance", "grade": 6,
        "min_level": 50, "min_reward": 900, "max_reward": 1300, "min_exp": 900, "max_exp": 1300,
        "description": "Управляешь сотнями курьеров. Серьёзный уровень.",
        "evolves_to": "balance_7", "req_level": 70,
        "req_skill": ("organization_level", 8), "req_item": ("has_logistics_license", "📋 Лицензия логиста"),
    },
    "balance_7": {
        "name": "Владелец логистической сети по всему СНГ", "branch": "balance", "grade": 7,
        "min_level": 70, "min_reward": 1600, "max_reward": 2200, "min_exp": 1600, "max_exp": 2200,
        "description": "Бизнес вышел за пределы Кыргызстана.",
        "evolves_to": "balance_8", "req_level": 80,
        "req_skill": ("management_level", 7), "req_item": ("has_business_plan", "📊 Бизнес-план"),
    },
    "balance_8": {
        "name": "Главный инвестор и спонсор новых корпусов КТУ", "branch": "balance", "grade": 8,
        "min_level": 80, "min_reward": 2800, "max_reward": 3800, "min_exp": 2800, "max_exp": 3800,
        "description": "Строишь новые корпусы и вешаешь на них своё имя.",
        "evolves_to": "balance_9", "req_level": 100,
        "req_skill": ("management_level", 10), "req_item": ("has_business_plan", "📊 Бизнес-план"),
    },
    "balance_9": {
        "name": "Магнат каршеринга и всей инфраструктуры Бишкека", "branch": "balance", "grade": 9,
        "min_level": 100, "min_reward": 5000, "max_reward": 7000, "min_exp": 5000, "max_exp": 7000,
        "description": "Весь город работает на тебя.", "evolves_to": None,
    },
    "money_1": {
        "name": "Помощник на раздаче Чорбо в столовой", "branch": "money", "grade": 1,
        "min_level": 1, "min_reward": 30, "max_reward": 48, "min_exp": 8, "max_exp": 14,
        "description": "Разливаешь суп. Монет много, опыта мало.",
        "evolves_to": "money_2", "req_level": 5,
    },
    "money_2": {
        "name": "Тайный дилер Турецкого Чая и Симитов в коридорах", "branch": "money", "grade": 2,
        "min_level": 5, "min_reward": 70, "max_reward": 100, "min_exp": 20, "max_exp": 35,
        "description": "Продаёшь симиты из-под полы. Охрана в курсе.",
        "evolves_to": "money_3", "req_level": 10, "req_skill": ("charisma_level", 2),
    },
    "money_3": {
        "name": "Бариста в кофейне напротив ворот Манаса", "branch": "money", "grade": 3,
        "min_level": 10, "min_reward": 160, "max_reward": 230, "min_exp": 45, "max_exp": 70,
        "description": "Льёшь латте студентам. Чаевые огонь.",
        "evolves_to": "money_4", "req_level": 20,
        "req_skill": ("charisma_level", 3), "req_item": ("has_shaker", "🍹 Проф. шейкер"),
    },
    "money_4": {
        "name": "Управляющий университетским буфетом", "branch": "money", "grade": 4,
        "min_level": 20, "min_reward": 340, "max_reward": 480, "min_exp": 90, "max_exp": 130,
        "description": "Контролируешь все продажи в буфете. Касса звенит.",
        "evolves_to": "money_5", "req_level": 35,
        "req_skill": ("charisma_level", 5), "req_item": ("has_shaker", "🍹 Проф. шейкер"),
    },
    "money_5": {
        "name": "Поставщик турецких продуктов для всех кафе Джала", "branch": "money", "grade": 5,
        "min_level": 35, "min_reward": 700, "max_reward": 1000, "min_exp": 175, "max_exp": 260,
        "description": "Возишь товар оптом. Маржа ощутимая.",
        "evolves_to": "money_6", "req_level": 50,
        "req_skill": ("management_level", 3), "req_item": ("has_import_license", "🛃 Импортная лицензия"),
    },
    "money_6": {
        "name": "Владелец сети донерных вокруг всех вузов Бишкека", "branch": "money", "grade": 6,
        "min_level": 50, "min_reward": 1400, "max_reward": 2000, "min_exp": 350, "max_exp": 500,
        "description": "Донер везде. Ты — везде. Деньги — везде.",
        "evolves_to": "money_7", "req_level": 70,
        "req_skill": ("management_level", 6), "req_item": ("has_import_license", "🛃 Импортная лицензия"),
    },
    "money_7": {
        "name": "Главный арендатор и владелец франшиз столовых КТУ", "branch": "money", "grade": 7,
        "min_level": 70, "min_reward": 2500, "max_reward": 3500, "min_exp": 600, "max_exp": 850,
        "description": "Все едальни КТУ платят тебе ренту.",
        "evolves_to": "money_8", "req_level": 80,
        "req_skill": ("management_level", 8), "req_item": ("has_franchise_contract", "📜 Франшизный контракт"),
    },
    "money_8": {
        "name": "Ресторатор-миллионер, открывший элитный ресторан в центре", "branch": "money", "grade": 8,
        "min_level": 80, "min_reward": 4500, "max_reward": 6000, "min_exp": 1000, "max_exp": 1400,
        "description": "Твой ресторан — самый дорогой в Бишкеке.",
        "evolves_to": "money_9", "req_level": 100,
        "req_skill": ("management_level", 10), "req_item": ("has_franchise_contract", "📜 Франшизный контракт"),
    },
    "money_9": {
        "name": "Теневой спонсор университета и король общепита Кыргызстана", "branch": "money", "grade": 9,
        "min_level": 100, "min_reward": 9000, "max_reward": 15000, "min_exp": 1500, "max_exp": 2200,
        "description": "Заработок в 500+ раз выше первого уровня. Ты — легенда.",
        "evolves_to": None, "special": "money_king",
    },
}

STARTER_JOB_KEYS = ["intel_1", "balance_1", "money_1"]
JOB_NAME_TO_KEY = {data["name"]: key for key, data in JOBS.items()}

WORK_EVENTS = {
    "intel": {
        "positive": [
            "🔥 Министерский грант! Твои знания оценили по-настоящему!",
            "🔥 Студент сдал на «А»! Родители принесли торт и конверт!",
            "🔥 Научная статья принята! Гонорар прилетел мгновенно!",
        ],
        "negative": [
            "⚠️ Списал не ту шпаргалку. Препод поставил пересдачу.",
            "⚠️ Студенты пожаловались! Штраф от деканата.",
            "⚠️ Потерял зачётку — пришлось восстанавливать за свой счёт.",
        ],
    },
    "balance": {
        "positive": [
            "🔥 Двойной заказ! Клиент доволен и щедро отсыпал сверху!",
            "🔥 VIP-тур! Богатые студенты оплатили трёхдневный тур!",
            "🔥 Реклама сработала! Поток клиентов удвоился!",
        ],
        "negative": [
            "⚠️ Прокол колеса в Джале. Ремонт съел часть выручки.",
            "⚠️ Опоздал на сдачу заказа — штраф от клиента.",
            "⚠️ Навигатор завёл не туда. Бензин сгорел впустую.",
        ],
    },
    "money": {
        "positive": [
            "🔥 Банкет у ректора! Чаевые бешеные!",
            "🔥 Блогер снял сторис о твоей точке — наплыв клиентов!",
            "🔥 Крупная партия симитов разошлась мгновенно!",
        ],
        "negative": [
            "⚠️ Санинспектор нагрянул. Штраф и нервы.",
            "⚠️ Просрочка поставки — пришлось выбросить партию.",
            "⚠️ Кофемашина сломалась. Ремонт за свой счёт.",
        ],
    },
}

# =====================================================================
# CALLBACK DATA
# =====================================================================
class JobCallback(CallbackData, prefix="job"):
    job_key: str

class ShopCallback(CallbackData, prefix="shop"):
    item_key: str

class TrainCallback(CallbackData, prefix="train"):
    stat: str

class UpgradeJobCallback(CallbackData, prefix="upjob"):
    job_key: str

class BunkerModeCallback(CallbackData, prefix="bunker_mode"):
    mode: str

class BunkerJoinCallback(CallbackData, prefix="bunker_join"):
    chat_id: int

class BunkerVoteCallback(CallbackData, prefix="bunker_vote"):
    target_id: int
    chat_id: int

# =====================================================================
# МАГАЗИН
# =====================================================================
SHOP_ITEMS = {
    "scooter": {"name": "🛵 Скутер", "price": 1200, "flag": "has_scooter",
                "description": "Нужен для ветки Баланс (10+ лвл). Даёт +1 к Вождению.",
                "skill_bonus": ("driving_level", 1)},
    "shaker": {"name": "🍹 Проф. шейкер", "price": 800, "flag": "has_shaker",
               "description": "Нужен для ветки Деньги (10+ лвл). Даёт +1 к Харизме.",
               "skill_bonus": ("charisma_level", 1)},
    "laptop": {"name": "💻 Ноутбук", "price": 3500, "flag": "has_laptop",
               "description": "Нужен для ветки Интеллект (10+ лвл). Даёт +1 к Коммуникации.",
               "skill_bonus": ("communication_level", 1)},
    "professor_badge": {"name": "🎓 Профессорский значок", "price": 15000, "flag": "has_professor_badge",
                        "description": "Нужен для ветки Интеллект (50+ лвл). Даёт +1 к Менеджменту.",
                        "skill_bonus": ("management_level", 1)},
    "logistics_license": {"name": "📋 Лицензия логиста", "price": 18000, "flag": "has_logistics_license",
                          "description": "Нужна для ветки Баланс (50+ лвл). Даёт +1 к Организованности.",
                          "skill_bonus": ("organization_level", 1)},
    "import_license": {"name": "🛃 Импортная лицензия", "price": 20000, "flag": "has_import_license",
                       "description": "Нужна для ветки Деньги (50+ лвл). Даёт +1 к Менеджменту.",
                       "skill_bonus": ("management_level", 1)},
    "dean_seal": {"name": "🔏 Декановская печать", "price": 80000, "flag": "has_dean_seal",
                  "description": "Нужна для ветки Интеллект (70+ лвл). Даёт +2 к Менеджменту.",
                  "skill_bonus": ("management_level", 2)},
    "business_plan": {"name": "📊 Бизнес-план", "price": 90000, "flag": "has_business_plan",
                      "description": "Нужен для ветки Баланс (80+ лвл). Даёт +2 к Менеджменту.",
                      "skill_bonus": ("management_level", 2)},
    "franchise_contract": {"name": "📜 Франшизный контракт", "price": 100000, "flag": "has_franchise_contract",
                           "description": "Нужен для ветки Деньги (80+ лвл). Даёт +2 к Менеджменту.",
                           "skill_bonus": ("management_level", 2)},
}

CONSUMABLES = {
    "water": {"name": "💧 Вода", "price": 20, "hp": 0, "energy": 10,
              "description": "Небольшой глоток сил."},
    "plaster": {"name": "🩹 Пластырь", "price": 25, "hp": 15, "energy": 0,
                "description": "Заклеивает мелкие порезы."},
    "bar": {"name": "🍫 Батончик", "price": 60, "hp": 0, "energy": 25,
            "description": "Быстрый перекус, даёт энергии."},
    "energy_drink": {"name": "⚡ Энергетик", "price": 120, "hp": 0, "energy": 50,
                     "description": "Хороший буст энергии."},
    "bandage": {"name": "🩻 Бинт", "price": 100, "hp": 40, "energy": 0,
                "description": "Перевязывает средние раны."},
    "lunch": {"name": "🍱 Сытный обед", "price": 200, "hp": 40, "energy": 40,
              "description": "Восстанавливает и силы, и здоровье."},
    "first_aid_kit": {"name": "🚑 Автомобильная аптечка", "price": 350, "hp": 80, "energy": 0,
                      "description": "Серьёзное лечение."},
    "caffeine": {"name": "💊 Кофеин в таблетках", "price": 400, "hp": 0, "energy": 100,
                 "description": "Мощный энергетический заряд."},
    "ration": {"name": "🪖 Армейский сухпаёк", "price": 700, "hp": 100, "energy": 100,
               "description": "Полноценное восстановление в полевых условиях."},
    "elixir": {"name": "✨ Эликсир бодрости", "price": 1500, "hp": 9999, "energy": 9999,
               "description": "Полностью восстанавливает HP и Энергию."},
}

SKILL_CONFIG = {
    "communication_level": {"label": "🗣 Коммуникация", "cost_base": 400,
                             "description": "Нужна для ветки Интеллект.", "unlock_item": None},
    "driving_level": {"label": "🚗 Вождение", "cost_base": 600,
                      "description": "Нужно для ветки Баланс. Требует 🛵 Скутер.",
                      "unlock_item": "has_scooter"},
    "charisma_level": {"label": "✨ Харизма", "cost_base": 500,
                       "description": "Нужна для ветки Деньги. Требует 🍹 Шейкер.",
                       "unlock_item": "has_shaker"},
    "organization_level": {"label": "📋 Организованность", "cost_base": 700,
                            "description": "Нужна для ветки Баланс (средние грейды). Требует 🛵 Скутер.",
                            "unlock_item": "has_scooter"},
    "management_level": {"label": "🏢 Менеджмент", "cost_base": 900,
                         "description": "Нужен для всех веток на высоких уровнях.",
                         "unlock_item": None},
}

# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================
def get_max_hp(user: dict) -> int:
    return 100 + user["endurance"] * 10

def get_max_energy(user: dict) -> int:
    return 100 + user["intellect"] * 10

def get_job_key(user: dict) -> str | None:
    return JOB_NAME_TO_KEY.get(user["job"])

def get_job(user: dict) -> dict | None:
    key = get_job_key(user)
    return JOBS.get(key) if key else None

def get_branch(user: dict) -> str | None:
    j = get_job(user)
    return j["branch"] if j else None

def get_stat_gains(branch: str, new_level: int) -> dict:
    if branch == "intel":
        intel_bonus = 10 if new_level >= 100 else (3 if new_level >= 70 else 2)
        return {"agility": 1, "endurance": 1, "charisma": 1, "intellect": intel_bonus}
    elif branch == "balance":
        return {"agility": 2, "endurance": 2, "charisma": 2, "intellect": 2}
    elif branch == "money":
        return {"agility": 1, "endurance": 1, "charisma": 3, "intellect": 1}
    return {"agility": 1, "endurance": 1, "charisma": 1, "intellect": 1}

async def auto_level_up(user: dict) -> tuple[dict, list[str]]:
    messages = []
    branch = get_branch(user) or ""
    while user["exp"] >= xp_needed(user["level"]):
        cost = xp_needed(user["level"])
        new_level = user["level"] + 1
        new_exp = user["exp"] - cost
        gains = get_stat_gains(branch, new_level)
        new_agi = user["agility"] + gains["agility"]
        new_end = user["endurance"] + gains["endurance"]
        new_cha = user["charisma"] + gains["charisma"]
        new_int = user["intellect"] + gains["intellect"]
        await update_user(
            user["user_id"],
            level=new_level, exp=new_exp,
            agility=new_agi, endurance=new_end,
            charisma=new_cha, intellect=new_int,
        )
        user = {**user, "level": new_level, "exp": new_exp,
                "agility": new_agi, "endurance": new_end,
                "charisma": new_cha, "intellect": new_int}
        gains_str = (
            f"  🏃 Ловкость:     +{gains['agility']} → {new_agi}\n"
            f"  💪 Выносливость: +{gains['endurance']} → {new_end} "
            f"(макс. HP: {get_max_hp(user)})\n"
            f"  ✨ Харизма:      +{gains['charisma']} → {new_cha}\n"
            f"  🧠 Интеллект:    +{gains['intellect']} → {new_int} "
            f"(макс. ⚡: {get_max_energy(user)})"
        )
        msg = f"\n\n🎉 <b>НОВЫЙ УРОВЕНЬ — {new_level}!</b>\n📊 <b>Прирост:</b>\n{gains_str}"
        job = get_job(user)
        if job:
            next_key = job.get("evolves_to")
            if next_key:
                next_job = JOBS[next_key]
                if new_level >= next_job["min_level"]:
                    msg += (
                        f"\n\n🌟 <b>Новый грейд доступен!</b>\n"
                        f"Открой <b>💼 Профессии</b> для повышения до «{next_job['name']}»"
                    )
        messages.append(msg)
    return user, messages

def check_upgrade_conditions(user: dict) -> tuple[bool, str]:
    job_key = get_job_key(user)
    if not job_key or job_key not in JOBS:
        return False, "❌ Сначала выбери профессию."
    job = JOBS[job_key]
    next_key = job.get("evolves_to")
    if not next_key:
        return False, "✅ Ты уже на максимальном грейде!"
    next_job = JOBS[next_key]
    if user["level"] < next_job["min_level"]:
        return False, f"❌ Нужен <b>{next_job['min_level']}</b> уровень (у тебя {user['level']})."
    if "req_skill" in next_job:
        skill_key, skill_min = next_job["req_skill"]
        cfg = SKILL_CONFIG.get(skill_key, {})
        label = cfg.get("label", skill_key)
        val = user.get(skill_key, 0)
        if val < skill_min:
            return False, f"❌ Нужен навык {label} уровня <b>{skill_min}</b> (у тебя {val})."
    if "req_item" in next_job:
        item_flag, item_label = next_job["req_item"]
        if not user.get(item_flag):
            return False, f"❌ Нужен предмет: {item_label} (купи в Магазине)."
    return True, "✅ Все условия выполнены!"

def build_jobs_text(user: dict) -> str:
    if user["job"] == "Безработный":
        lines = ["💼 <b>Выбор профессии</b>\n", "Выбери ветку — сменить нельзя!\n"]
        for key in STARTER_JOB_KEYS:
            d = JOBS[key]
            lines.append(
                f"🔹 <b>{d['name']}</b>\n"
                f"   💰 {d['min_reward']}–{d['max_reward']} мон. | "
                f"✨ {d['min_exp']}–{d['max_exp']} XP\n"
                f"   <i>{d['description']}</i>"
            )
        return "\n".join(lines)
    job_key = get_job_key(user)
    if not job_key:
        return build_jobs_text({**user, "job": "Безработный"})
    job = JOBS[job_key]
    rank = user.get("job_rank", 1)
    bonus = (rank - 1) * 10
    next_key = job.get("evolves_to")
    can, why = check_upgrade_conditions(user)
    lines = [
        f"💼 <b>{user['job']}</b>  |  Грейд: {job['grade']}/9  |  Ранг: {rank}\n",
        f"<i>{job['description']}</i>\n",
        f"💰 Заработок: {job['min_reward']}–{job['max_reward']} мон."
        + (f" (+{bonus}% ранг)" if bonus > 0 else "") + "\n",
        f"✨ Опыт за смену: {job['min_exp']}–{job['max_exp']}\n",
        f"⭐ Уровень: <b>{user['level']}</b> | XP: <b>{user['exp']}</b> / {xp_needed(user['level'])}\n",
    ]
    if next_key:
        next_job = JOBS[next_key]
        lines.append("━━━ 🔓 Следующий грейд ━━━")
        lines.append(f"<b>{next_job['name']}</b>")
        lines.append("Требования:")
        lines.append(f"  • Уровень: <b>{next_job['min_level']}</b>  "
                     f"({'✅' if user['level'] >= next_job['min_level'] else '❌'})")
        if "req_skill" in next_job:
            sk, sv = next_job["req_skill"]
            lbl = SKILL_CONFIG.get(sk, {}).get("label", sk)
            lines.append(f"  • {lbl}: <b>{sv}</b>  "
                         f"({'✅' if user.get(sk, 0) >= sv else '❌'}, у тебя {user.get(sk, 0)})")
        if "req_item" in next_job:
            iflag, ilabel = next_job["req_item"]
            lines.append(f"  • {ilabel}: {'✅' if user.get(iflag) else '❌'}")
        if not can:
            lines.append(f"\n{why}")
    else:
        lines.append("\n🏆 <b>Максимальный грейд достигнут!</b>")
    return "\n".join(lines)

def get_jobs_keyboard(user: dict) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    if user["job"] == "Безработный":
        for key in STARTER_JOB_KEYS:
            builder.button(text=JOBS[key]["name"][:50], callback_data=JobCallback(job_key=key).pack())
        builder.adjust(1)
        return builder.as_markup()
    job_key = get_job_key(user)
    if not job_key:
        for key in STARTER_JOB_KEYS:
            builder.button(text=JOBS[key]["name"][:50], callback_data=JobCallback(job_key=key).pack())
        builder.adjust(1)
        return builder.as_markup()
    job = JOBS[job_key]
    next_key = job.get("evolves_to")
    if next_key:
        can, _ = check_upgrade_conditions(user)
        if can:
            builder.button(text="🚀 Повысить грейд", callback_data=UpgradeJobCallback(job_key=next_key).pack())
            builder.adjust(1)
            return builder.as_markup()
    return None

# =====================================================================
# ЛОГИКА: РАБОТА
# =====================================================================
async def do_work(user: dict) -> tuple[bool, str]:
    if user["job"] == "Безработный":
        return False, "❌ Сначала выбери профессию через <b>💼 Профессии</b>!"
    now = int(time.time())
    elapsed = now - user["last_work_time"]
    if elapsed < WORK_COOLDOWN:
        return False, f"⏳ Подожди ещё <b>{WORK_COOLDOWN - elapsed}</b> сек."
    if user["energy"] < WORK_ENERGY_COST:
        return False, (
            f"😴 Недостаточно энергии!\n"
            f"Нужно: <b>{WORK_ENERGY_COST} ⚡</b>, есть: <b>{user['energy']} ⚡</b> / {get_max_energy(user)}.\n"
            f"Купи расходники в 🛒 Магазине."
        )
    job = get_job(user)
    lvl = user["level"]
    if job is None:
        earned_coins = scale_coins(10, lvl)
        earned_exp = scale_xp(5, lvl)
        branch = None
    else:
        earned_coins = scale_coins(random.randint(job["min_reward"], job["max_reward"]), lvl)
        earned_exp = scale_xp(random.randint(job["min_exp"], job["max_exp"]), lvl)
        branch = job["branch"]
    rank = user.get("job_rank", 1)
    if rank > 1:
        mult = 1 + (rank - 1) * 0.10
        earned_coins = int(earned_coins * mult)
        earned_exp = int(earned_exp * mult)
    event_line = ""
    event_prefix = ""
    if random.random() < 0.20:
        luck = user.get("luck", 1)
        pos_chance = min(luck * 4, 60) / 100
        branch_events = WORK_EVENTS.get(branch, {}) if branch else {}
        if random.random() < pos_chance:
            earned_coins *= 2
            event_prefix = "🎲 <b>Случайное событие!</b>\n"
            event_line = (random.choice(branch_events["positive"]) if branch_events.get("positive")
                          else "🔥 Удача! Двойная выплата!") + " <b>(х2 монеты)</b>"
        else:
            earned_coins = max(1, earned_coins // 2)
            event_prefix = "🎲 <b>Случайное событие!</b>\n"
            event_line = (random.choice(branch_events["negative"]) if branch_events.get("negative")
                          else "⚠️ Неудачный день. Доход урезан.") + " <b>(х0.5 монеты)</b>"
    new_balance = user["balance"] + earned_coins
    new_exp = user["exp"] + earned_exp
    new_energy = max(0, user["energy"] - WORK_ENERGY_COST)
    await update_user(user["user_id"], balance=new_balance, exp=new_exp,
                      last_work_time=now, energy=new_energy)
    user = {**user, "balance": new_balance, "exp": new_exp, "energy": new_energy}
    user, level_msgs = await auto_level_up(user)
    level_block = "".join(level_msgs)
    event_block = f"\n\n{event_prefix}{event_line}" if event_line else ""
    max_energy = get_max_energy(user)
    return True, (
        f"🛠 Ты поработал как <b>{user['job']}</b>!"
        f"{event_block}\n\n"
        f"💰 Получено: <b>+{earned_coins}</b> монет\n"
        f"✨ Опыт: <b>+{earned_exp}</b>  (всего: {user['exp']} / {xp_needed(user['level'])})\n"
        f"⚡ Энергия: <b>{new_energy}</b> / {max_energy}  (-{WORK_ENERGY_COST})\n"
        f"📊 Итого монет: <b>{user['balance']}</b>"
        f"{level_block}"
    )

# =====================================================================
# ЛОГИКА: НАВЫКИ
# =====================================================================
def build_skills_text(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🧠 <b>Навыки</b>\n"]
    builder = InlineKeyboardBuilder()
    for key, cfg in SKILL_CONFIG.items():
        val = user.get(key, 0)
        cost = max(cfg["cost_base"], val * cfg["cost_base"])
        unlock = cfg.get("unlock_item")
        locked = unlock and not user.get(unlock)
        lines.append(f"{cfg['label']}: <b>{val} ур.</b>\n  <i>{cfg['description']}</i>")
        if locked:
            lines.append("  🔒 Требуется предмет (Магазин)\n")
        else:
            lines.append(f"  Следующий уровень: <b>{cost} монет</b>\n")
            builder.button(text=f"⬆️ {cfg['label']} ({cost} мон.)",
                           callback_data=f"upgrade_skill:{key}")
    lines.append(f"\n💰 Баланс: <b>{user['balance']}</b> монет")
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()

async def do_upgrade_skill(user: dict, skill_key: str) -> tuple[bool, str]:
    cfg = SKILL_CONFIG.get(skill_key)
    if not cfg:
        return False, "❌ Неизвестный навык."
    unlock = cfg.get("unlock_item")
    if unlock and not user.get(unlock):
        return False, "❌ Сначала купи нужный предмет в Магазине."
    val = user.get(skill_key, 0)
    cost = max(cfg["cost_base"], val * cfg["cost_base"])
    if user["balance"] < cost:
        return False, f"❌ Нужно <b>{cost}</b> монет, есть <b>{user['balance']}</b>."
    await update_user(user["user_id"], **{skill_key: val + 1, "balance": user["balance"] - cost})
    return True, (f"✅ <b>{cfg['label']}</b> прокачана до уровня <b>{val + 1}</b>!\n"
                  f"💰 Потрачено: {cost} монет.")

# =====================================================================
# ЛОГИКА: ТРЕНИРОВКИ
# =====================================================================
TRAIN_CONFIG = {
    "intellect": {"name": "📖 Почитать книгу", "stat_label": "🧠 Интеллект", "active": True},
    "endurance": {"name": "🏃 Пробежка", "stat_label": "💪 Выносливость", "active": True},
    "agility": {"name": "🤸 Акробатика", "stat_label": "🏃 Ловкость", "active": False},
    "charisma": {"name": "🎤 Публичное выступление", "stat_label": "✨ Харизма", "active": False},
}

def build_training_text(user: dict) -> str:
    max_energy = get_max_energy(user)
    return (
        f"🏋️ <b>Тренировки</b>\n\n"
        f"⚡ Энергия: <b>{user['energy']}</b> / {max_energy}\n\n"
        f"Тренировки тратят <b>{TRAIN_ENERGY_COST} ⚡</b> и прокачивают характеристики.\n"
        f"<i>⚡ Энергия восстанавливается только расходниками из Магазина!</i>"
    )

def get_training_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📖 Почитать книгу  (-{TRAIN_ENERGY_COST} ⚡) → +1 🧠 Интеллект",
                   callback_data=TrainCallback(stat="intellect").pack())
    builder.button(text=f"🏃 Пробежка  (-{TRAIN_ENERGY_COST} ⚡) → +1 💪 Выносливость",
                   callback_data=TrainCallback(stat="endurance").pack())
    builder.button(text="🤸 Акробатика  (-50 ⚡) → +1 🏃 Ловкость  [Скоро]",
                   callback_data=TrainCallback(stat="agility").pack())
    builder.button(text="🎤 Выступление  (-50 ⚡) → +1 ✨ Харизма  [Скоро]",
                   callback_data=TrainCallback(stat="charisma").pack())
    builder.adjust(1)
    return builder.as_markup()

async def do_train(user: dict, stat: str) -> tuple[bool, str]:
    cfg = TRAIN_CONFIG.get(stat)
    if not cfg:
        return False, "❌ Неизвестная тренировка."
    if not cfg["active"]:
        return False, f"🚧 <b>{cfg['name']}</b> пока в разработке. Скоро появится!"
    if user["energy"] < TRAIN_ENERGY_COST:
        return False, (f"😴 Недостаточно энергии!\n"
                       f"Нужно: <b>{TRAIN_ENERGY_COST} ⚡</b>, есть: <b>{user['energy']} ⚡</b>.\n"
                       f"Купи расходник в 🛒 Магазине.")
    new_energy = max(0, user["energy"] - TRAIN_ENERGY_COST)
    new_stat = user[stat] + 1
    await update_user(user["user_id"], energy=new_energy, **{stat: new_stat})
    updated = {**user, stat: new_stat, "energy": new_energy}
    return True, (f"✅ <b>{cfg['name']}</b> завершена!\n\n"
                  f"{cfg['stat_label']}: <b>+1</b> → {new_stat}\n"
                  f"⚡ Энергия: <b>{new_energy}</b> / {get_max_energy(updated)}  (-{TRAIN_ENERGY_COST})")

# =====================================================================
# ЛОГИКА: МАГАЗИН
# =====================================================================
def build_shop_text(user: dict) -> str:
    lines = ["🛒 <b>Магазин</b>\n", "━━━ 🎒 Снаряжение (постоянные) ━━━"]
    for item in SHOP_ITEMS.values():
        owned = "✅ куплено" if user.get(item["flag"]) else f"{item['price']} монет"
        lines.append(f"{item['name']} — <b>{owned}</b>\n  <i>{item['description']}</i>")
    lines.append("\n━━━ 🧃 Расходники ━━━")
    for item in CONSUMABLES.values():
        effects = []
        if item["hp"] > 0: effects.append(f"+{min(item['hp'], 9999)} HP")
        if item["energy"] > 0: effects.append(f"+{min(item['energy'], 9999)} ⚡")
        if item["hp"] >= 9999 and item["energy"] >= 9999:
            effects = ["полное восстановление"]
        lines.append(f"{item['name']} — <b>{item['price']} монет</b>  ({', '.join(effects)})\n"
                     f"  <i>{item['description']}</i>")
    lines.append(f"\n💰 Твой баланс: <b>{user['balance']}</b> монет")
    return "\n".join(lines)

def get_shop_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, item in SHOP_ITEMS.items():
        builder.button(text=f"{item['name']} — {item['price']} монет",
                       callback_data=ShopCallback(item_key=key).pack())
    for key in CONSUMABLES:
        item = CONSUMABLES[key]
        builder.button(text=f"{item['name']} — {item['price']} монет",
                       callback_data=f"consume:{key}")
    builder.adjust(1)
    return builder.as_markup()

async def do_buy_item(user: dict, item_key: str) -> tuple[bool, str]:
    item = SHOP_ITEMS.get(item_key)
    if not item:
        return False, "❌ Такого товара нет."
    if user.get(item["flag"]):
        return False, f"У тебя уже есть {item['name']}!"
    if user["balance"] < item["price"]:
        return False, (f"❌ Недостаточно монет.\n"
                       f"Нужно: <b>{item['price']}</b>, есть: <b>{user['balance']}</b>.")
    new_balance = user["balance"] - item["price"]
    extra = {item["flag"]: 1}
    if "skill_bonus" in item:
        skill_key, skill_bonus = item["skill_bonus"]
        extra[skill_key] = user.get(skill_key, 0) + skill_bonus
    await update_user(user["user_id"], balance=new_balance, **extra)
    bonus_line = ""
    if "skill_bonus" in item:
        sk, _ = item["skill_bonus"]
        cfg = SKILL_CONFIG.get(sk)
        if cfg:
            bonus_line = f"\n🎁 Бонус: {cfg['label']} +1!"
    return True, (f"✅ Ты купил {item['name']}!\n"
                  f"💰 Потрачено: <b>{item['price']}</b> монет.{bonus_line}\n"
                  f"<i>{item['description']}</i>")

async def do_use_consumable(user: dict, item_key: str) -> tuple[bool, str]:
    item = CONSUMABLES.get(item_key)
    if not item:
        return False, "❌ Такого предмета нет."
    if user["balance"] < item["price"]:
        return False, (f"❌ Недостаточно монет.\n"
                       f"Нужно: <b>{item['price']}</b>, есть: <b>{user['balance']}</b>.")
    max_hp = get_max_hp(user)
    max_energy = get_max_energy(user)
    old_hp = user["hp"]
    old_energy = user["energy"]
    new_hp = min(max_hp, old_hp + item["hp"])
    new_energy = min(max_energy, old_energy + item["energy"])
    new_balance = user["balance"] - item["price"]
    await update_user(user["user_id"], hp=new_hp, energy=new_energy, balance=new_balance)
    gained_hp = new_hp - old_hp
    gained_energy = new_energy - old_energy
    lines = [f"✅ Использован {item['name']}!"]
    if gained_hp > 0: lines.append(f"❤️ HP:     +{gained_hp} → {new_hp} / {max_hp}")
    if gained_energy > 0: lines.append(f"⚡ Энергия: +{gained_energy} → {new_energy} / {max_energy}")
    if gained_hp == 0 and gained_energy == 0:
        lines.append("ℹ️ Ресурсы уже на максимуме — предмет потрачен впустую!")
    lines.append(f"💰 Потрачено: {item['price']} монет")
    return True, "\n".join(lines)

# =====================================================================
# ПРОФИЛЬ
# =====================================================================
def build_profile_text(user: dict, mention: str) -> str:
    lvl = user["level"]
    needed_xp = xp_needed(lvl)
    max_hp = get_max_hp(user)
    max_energy = get_max_energy(user)
    rank = user.get("job_rank", 1)
    job = get_job(user)
    grade = job["grade"] if job else 0
    inv_parts = [item["name"] for key, item in SHOP_ITEMS.items() if user.get(item["flag"])]
    inventory_str = ", ".join(inv_parts) if inv_parts else "пусто"
    skills_lines = [f"{cfg['label']}: <b>{user.get(sk, 0)} ур.</b>"
                    for sk, cfg in SKILL_CONFIG.items() if user.get(sk, 0) > 0]
    if not skills_lines:
        skills_lines = ["(навыки ещё не прокачаны)"]
    return (
        f"👤 <b>Профиль</b> {mention}\n\n"
        f"💼 Профессия: <b>{user['job']}</b>"
        + (f"  [Грейд {grade}/9, Ранг {rank}]" if grade > 0 else "") + "\n"
        f"⭐ Уровень: <b>{lvl}</b>\n"
        f"✨ Опыт: <b>{user['exp']}</b> / {needed_xp}\n"
        f"💰 Баланс: <b>{user['balance']}</b> монет\n\n"
        f"━━━ 💗 Ресурсы ━━━\n"
        f"❤️ Здоровье:  <b>{user['hp']}</b> / {max_hp}\n"
        f"⚡ Энергия:   <b>{user['energy']}</b> / {max_energy}\n\n"
        f"━━━ 📊 Характеристики ━━━\n"
        f"🏃 Ловкость:      <b>{user['agility']}</b>\n"
        f"💪 Выносливость:  <b>{user['endurance']}</b>\n"
        f"✨ Харизма:       <b>{user['charisma']}</b>\n"
        f"🧠 Интеллект:     <b>{user['intellect']}</b>\n"
        f"🍀 Удача:         <b>{user['luck']}</b>\n\n"
        f"━━━ 🗣 Навыки ━━━\n"
        + "\n".join(skills_lines) + "\n\n"
        f"━━━ 🎒 Инвентарь ━━━\n"
        f"{inventory_str}"
    )

# =====================================================================
# ГЛАВНОЕ МЕНЮ
# =====================================================================
def get_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💼 Профессии")],
            [KeyboardButton(text="🛠 Работа"), KeyboardButton(text="🏋️ Тренировки")],
            [KeyboardButton(text="🧠 Навыки"), KeyboardButton(text="🛒 Магазин")],
        ],
        resize_keyboard=True,
        persistent=True,
    )

# =====================================================================
# БИРЖА
# =====================================================================
STOCK_MIN_BET = 100
STOCK_OUTCOMES = ["ап"] * 18 + ["давн"] * 18 + ["нейтрал"] * 2
STOCK_LABELS = {"ап": "📈 АП (рынок вырос)", "давн": "📉 ДАВН (рынок упал)", "нейтрал": "➡️ НЕЙТРАЛ (флэт)"}
STOCK_WIN_PHRASES = [
    "🎯 Вы поймали тренд как настоящий трейдер КТУ!",
    "🤑 Уоррен Баффет из Джала гордился бы вами!",
    "📊 Анализ — огонь! Депозит пополнен!",
    "🚀 Вы в плюсе! Биржа покорилась!",
]
STOCK_LOSE_PHRASES = [
    "📉 Рынок — это не Джал-Маркет, тут цены не фиксированные...",
    "💀 Стоп-лосс не спас. Ставка сгорела дотла.",
    "😭 Инвестиции — это риск. Особенно твои.",
    "🔥 Ставка улетела вместе с надеждами.",
]

async def do_stock_bet(user: dict, outcome_input: str, bet: int) -> tuple[bool, str]:
    outcome_input = outcome_input.strip().lower()
    if outcome_input not in ("ап", "давн", "нейтрал"):
        return False, "❌ Используй: <b>акция ап [сумма]</b>, <b>акция давн [сумма]</b> или <b>акция нейтрал [сумма]</b>"
    if bet < STOCK_MIN_BET:
        return False, f"❌ Минимальная ставка: <b>{STOCK_MIN_BET}</b> монет."
    if user["balance"] < bet:
        return False, f"❌ Недостаточно монет.\nСтавка: <b>{bet}</b> | Баланс: <b>{user['balance']}</b>"
    market_result = random.choice(STOCK_OUTCOMES)
    market_label = STOCK_LABELS[market_result]
    won = (outcome_input == market_result)
    if won:
        new_balance = user["balance"] + bet
        await update_user(user["user_id"], balance=new_balance)
        return True, (f"📊 <b>Рынок пришёл в движение!</b>\nГрафик пошёл: <b>{market_label}</b>\n\n"
                      f"🎉 <b>Вы угадали тренд!</b>\n{random.choice(STOCK_WIN_PHRASES)}\n\n"
                      f"💰 Выигрыш: <b>+{bet}</b> монет\n📈 Баланс: <b>{new_balance}</b> монет")
    else:
        new_balance = user["balance"] - bet
        await update_user(user["user_id"], balance=new_balance)
        player_label = STOCK_LABELS[outcome_input]
        return True, (f"📊 <b>Рынок пришёл в движение!</b>\nТы ставил на: <b>{player_label}</b>\n"
                      f"График пошёл: <b>{market_label}</b>\n\n"
                      f"📉 <b>Мимо!</b>\n{random.choice(STOCK_LOSE_PHRASES)}\n\n"
                      f"💸 Потеря: <b>-{bet}</b> монет\n📉 Баланс: <b>{new_balance}</b> монет")

# =====================================================================
# БУНКЕР (полный перенос из оригинала)
# =====================================================================
BUNKER_CLASSIC = {
    "disasters": [
        "☢️ <b>Ядерная война</b>\nМировые сверхдержавы нажали кнопки. Горизонт пылает. "
        "Радиационный фон смертелен. У вас есть считанные минуты, чтобы скрыться в бункере.",
        "🧟 <b>Зомби-вирус «Штамм-Х»</b>\nНеизвестный патоген превращает людей в агрессивных мертвецов. "
        "Правительства рухнули. Единственный шанс на выживание — изоляция в бункере.",
        "🌊 <b>Всемирный потоп</b>\nПолюса стремительно тают, уровень океана поднялся на 50 метров. "
        "Большинство городов под водой. Бункер — последнее сухое убежище.",
        "☄️ <b>Падение астероида</b>\nАстероид диаметром 2 км вошёл в атмосферу. "
        "Пылевое облако накроет Землю на десятилетия. Бункер — единственный шанс.",
    ],
    "bunker_desc": (
        "🏗 <b>Бункер:</b> Классическое подземное убежище глубиной 50 м. "
        "Гермодвери, система фильтрации воздуха, запас консервов на 5 лет, "
        "дизельный генератор, медотсек и библиотека."
    ),
    "professions": ["👨‍⚕️ Врач общей практики", "🔧 Инженер-механик", "🔬 Учёный-биохимик",
                    "🪖 Военный (спецназ)", "🌾 Агроном", "👩‍🍳 Шеф-повар",
                    "🧑‍💻 IT-специалист", "🧑‍🏫 Педагог", "🧰 Сантехник", "📻 Радиоинженер"],
    "baggage": ["🔫 Охотничье ружьё с патронами", "💊 Большая аптечка",
                "🌱 Семена овощей (годовой запас)", "📖 Энциклопедия выживания",
                "🔋 Солнечная панель + аккумулятор", "🔪 Многофункциональный нож",
                "📡 Портативная рация", "🧲 Набор инструментов",
                "🌡️ Счётчик Гейгера", "🥫 Запас консервов на 6 месяцев"],
    "hobbies": ["🏹 Охота и рыбалка", "📚 Чтение и самообразование", "🍳 Кулинария",
                "🎸 Игра на гитаре", "🧘 Йога и медитация", "🌿 Огородничество",
                "🛠 Слесарное дело", "🎲 Настольные игры"],
    "facts": ["💰 Тайный миллионер — владеет оффшорным счётом",
              "🦠 Имеет природный иммунитет к вирусу", "🎖 Бывший агент ЦРУ",
              "🩸 Универсальный донор крови (I группа)",
              "🧬 Несёт ключевые гены для восстановления генофонда",
              "⚡ Умеет собирать генератор из подручных материалов",
              "🗺 Знает расположение всех бункеров страны",
              "🔐 Бывший взломщик сейфов"],
}

BUNKER_MANAS = {
    "disaster": (
        "📋 <b>КАТАСТРОФА: Тотальная проверка ректората!</b>\n\n"
        "Ректорат объявил внезапную тотальную проверку посещаемости "
        "и Сом-сынав (экзамен) по ВСЕМ предметам за ВСЕ 4 года. "
        "Деканы с ведомостями идут по коридорам. "
        "Всех, кто не пройдёт — отчислят без права восстановления!\n"
        "Единственный выход — успеть спрятаться в Бункере!"
    ),
    "bunkers": [
        ("🍵 <b>Бункер: Секретный подвал корпуса Джал</b>\nЗдесь хранится бесконечный запас "
         "турецкого чая и симитов. Wi-Fi не ловит (деканат не найдёт), зато есть розетки и "
         "старый диван. Вместимость ограничена."),
        ("🎧 <b>Бункер: Заброшенная аудитория синхронного перевода</b>\nСюда вообще не поступает "
         "сигнал деканата — глушилка для связи работает ещё с 2009 года. "
         "Есть наушники, сломанный проектор и вечно закрытая доска."),
        ("🏛 <b>Бункер: Кабинет ректора</b>\nРектор срочно улетел в Анкару на конференцию. "
         "Кожаные кресла, кофемашина, холодильник с едой. "
         "Секретарша подкуплена симитами и молчит."),
    ],
    "professions": ["📚 Вечный студент подготовительного курса (Хазырлык)",
                    "🗣 Отличник-коммуникатор с Факультета Филологии (ФФ)",
                    "🍲 Повар на раздаче из столовой корпуса Джал",
                    "🚌 Студент, у которого пары на Турусбекова, а он в Джале",
                    "📋 Активист-Староста своей группы", "🎮 Геймер с кафедры ИТ",
                    "💅 Красотка с ФГиЭ", "⚽ Спортсмен из студенческой сборной КТУ",
                    "🎵 Участник студенческого ансамбля «Жаштык»",
                    "🏆 Победитель университетской олимпиады по математике"],
    "baggage": ["🥐 Коробка свежих симитов (ещё тёплых)",
                "💾 Флешка с ответами на Сом-сынав 2025 года",
                "🪪 Студенческий билет с подписью самого ректора",
                "🫕 Огромный казан для плова (на 40 персон)",
                "📝 Шпаргалка, написанная микрошрифтом 2pt на туалетной бумаге",
                "📱 Телефон с 100% зарядкой и безлимитом",
                "🔑 Ключи от всех аудиторий корпуса Джал",
                "🎁 Коробка турецких конфет для задабривания охраны",
                "💻 Ноутбук с пиратским Office и VPN",
                "🧃 Упаковка турецкого чая (50 пакетиков)"],
    "characters": ["😴 Спит на первой парте даже во время Сом-сынава",
                   "🤝 Умеет договариваться с охранниками через шоколадку",
                   "🚌 Постоянно опаздывает на маршрутку №100",
                   "🌙 Прогуливает пары ради чиллаута у фонтана",
                   "📸 Фотографирует всё для студенческих пабликов",
                   "🔕 Никогда не отвечает на звонки деканата",
                   "☕ Не может жить без кофе из автомата в коридоре",
                   "📊 Делает красивые таблицы в Excel для каждой мелочи"],
    "hobbies": ["🎮 Играть в корейские ММО прямо на парах",
                "🎤 Петь на фестивалях и вечерах Манаса",
                "😤 Жаловаться на еду в столовой (но всё равно есть там каждый день)",
                "💬 Писать мемы в студенческие паблики ВКонтакте",
                "🎶 Слушать турецкие сериалы в оригинале",
                "🛒 Ходить за едой в Джал-Маркет во время лекций",
                "📺 Смотреть аниме в читальном зале библиотеки",
                "🃏 Играть в карты в общаге вместо подготовки к сессии"],
    "facts": ["👨‍👩‍👦 Двоюродный племянник замдекана по учебной части",
              "🏃 Ни разу в жизни не был на физкультуре (и гордится этим)",
              "☕ Умеет варить идеальный турецкий кофе в турке",
              "📶 Знает секретный пароль от Wi-Fi ректората",
              "🔑 Имеет дубликат ключа от деканата (откуда — не говорит)",
              "📱 Его номер есть в телефоне у самого ректора",
              "🎓 Написал дипломную работу за одну ночь и получил «А»",
              "🃏 Профессиональный переписчик чужих конспектов за деньги"],
}

BUNKER_REGISTRATION_SECONDS = 120


class BunkerGame:
    def __init__(self, chat_id: int, creator_id: int, creator_name: str, mode: str):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.creator_name = creator_name
        self.mode = mode
        self.players: dict[int, dict] = {}
        self.phase = "registration"
        self.disaster_text = ""
        self.bunker_text = ""
        self.survivors_limit = 0
        self.round_votes: dict[int, int] = {}
        self.eliminated: list[int] = []
        self.reg_deadline = int(time.time()) + BUNKER_REGISTRATION_SECONDS

    def _generate_card(self) -> dict:
        if self.mode == "classic":
            p = BUNKER_CLASSIC
            return {"profession": random.choice(p["professions"]),
                    "baggage": random.choice(p["baggage"]),
                    "hobby": random.choice(p["hobbies"]),
                    "fact": random.choice(p["facts"]),
                    "revealed": set()}
        else:
            p = BUNKER_MANAS
            return {"profession": random.choice(p["professions"]),
                    "baggage": random.choice(p["baggage"]),
                    "character": random.choice(p["characters"]),
                    "hobby": random.choice(p["hobbies"]),
                    "fact": random.choice(p["facts"]),
                    "revealed": set()}

    def start_game(self):
        n = len(self.players)
        self.survivors_limit = max(1, n // 2)
        if self.mode == "classic":
            self.disaster_text = random.choice(BUNKER_CLASSIC["disasters"])
            self.bunker_text = BUNKER_CLASSIC["bunker_desc"]
        else:
            self.disaster_text = BUNKER_MANAS["disaster"]
            self.bunker_text = random.choice(BUNKER_MANAS["bunkers"])
        for uid in self.players:
            self.players[uid]["card"] = self._generate_card()
        self.phase = "active"

    def card_text(self, user_id: int) -> str:
        card = self.players[user_id]["card"]
        if self.mode == "classic":
            return (f"🃏 <b>Твоя карта персонажа</b>\n\n"
                    f"💼 Профессия: <b>{card['profession']}</b>\n"
                    f"🎒 Багаж: <b>{card['baggage']}</b>\n"
                    f"🎮 Хобби: <b>{card['hobby']}</b>\n"
                    f"🔍 Секретный факт: <b>{card['fact']}</b>\n\n"
                    f"<i>Открывай командой: <code>открыть профессия</code> / "
                    f"<code>открыть багаж</code> / <code>открыть хобби</code> / <code>открыть факт</code></i>")
        else:
            return (f"🃏 <b>Твоя карта студента</b>\n\n"
                    f"🎓 Факультет/роль: <b>{card['profession']}</b>\n"
                    f"🎒 Багаж: <b>{card['baggage']}</b>\n"
                    f"😏 Черта характера: <b>{card['character']}</b>\n"
                    f"🎮 Хобби: <b>{card['hobby']}</b>\n"
                    f"🔍 Секрет: <b>{card['fact']}</b>\n\n"
                    f"<i>Открывай командой: <code>открыть факультет</code> / <code>открыть багаж</code> / "
                    f"<code>открыть характер</code> / <code>открыть хобби</code> / <code>открыть секрет</code></i>")

    def reveal(self, user_id: int, attr_raw: str) -> tuple[bool, str]:
        if user_id not in self.players:
            return False, "Ты не участвуешь в этой игре."
        if self.phase != "active":
            return False, "Сейчас не фаза обсуждения."
        card = self.players[user_id]["card"]
        name = self.players[user_id]["name"]
        mapping_classic = {"профессия": "profession", "профессию": "profession",
                           "багаж": "baggage", "хобби": "hobby", "факт": "fact", "секрет": "fact"}
        mapping_manas = {"факультет": "profession", "роль": "profession",
                         "багаж": "baggage", "характер": "character", "черта": "character",
                         "хобби": "hobby", "секрет": "fact", "факт": "fact"}
        mapping = mapping_classic if self.mode == "classic" else mapping_manas
        key = mapping.get(attr_raw.lower())
        if not key or key not in card:
            valid = " / ".join(f"<code>{k}</code>" for k in mapping)
            return False, f"Неизвестная характеристика. Доступны: {valid}"
        if key in card["revealed"]:
            label_map = {"profession": "Профессия/Факультет", "baggage": "Багаж",
                         "character": "Характер", "hobby": "Хобби", "fact": "Секрет"}
            return False, f"Ты уже открывал <b>{label_map.get(key, key)}</b>!"
        card["revealed"].add(key)
        label_map = {"profession": "💼 Профессия/Факультет", "baggage": "🎒 Багаж",
                     "character": "😏 Черта характера", "hobby": "🎮 Хобби", "fact": "🔍 Секретный факт"}
        label = label_map.get(key, key)
        return True, f"📢 <b>{name}</b> открывает {label}:\n<b>{card[key]}</b>"

    def vote(self, voter_id: int, target_id: int) -> tuple[bool, str]:
        if voter_id not in self.players:
            return False, "Ты не в игре."
        if target_id not in self.players:
            return False, "Такого игрока нет в игре."
        if voter_id == target_id:
            return False, "Нельзя голосовать против себя!"
        if self.phase != "voting":
            return False, "Сейчас не фаза голосования."
        self.round_votes[voter_id] = target_id
        return True, f"✅ Твой голос принят против <b>{self.players[target_id]['name']}</b>."

    def count_votes(self) -> tuple[int | None, str]:
        if not self.round_votes:
            return None, "Никто не проголосовал — голосование не засчитано."
        tally: dict[int, int] = {}
        for target in self.round_votes.values():
            tally[target] = tally.get(target, 0) + 1
        max_votes = max(tally.values())
        candidates = [uid for uid, v in tally.items() if v == max_votes]
        loser_id = random.choice(candidates)
        loser_name = self.players[loser_id]["name"]
        self.eliminated.append(loser_id)
        del self.players[loser_id]
        self.round_votes.clear()
        lines = ["📊 <b>Итоги голосования:</b>"]
        for uid, cnt in sorted(tally.items(), key=lambda x: -x[1]):
            pname = self.players.get(uid, {}).get("name", f"#{uid}")
            lines.append(f"  • {pname}: {cnt} голос(а)")
        if self.mode == "manas":
            lines.append(f"\n😱 <b>{loser_name}</b> поймали деканы и отправили на отчисление!")
        else:
            lines.append(f"\n☠️ <b>{loser_name}</b> выдворен из бункера!")
        if len(self.players) <= self.survivors_limit:
            self.phase = "finished"
        return loser_id, "\n".join(lines)

    def final_text(self) -> str:
        survivors = list(self.players.values())
        names_str = "\n".join(f"  🏅 {p['name']}" for p in survivors)
        if self.mode == "manas":
            story = ("Пока деканы бушевали по коридорам, наши герои тихо сидели в бункере, "
                     "попивали турецкий чай и делали вид, что очень заняты. Победа!")
            title = "🎓 Спаслись от отчисления!"
        else:
            story = ("Прошли месяцы. Выжившие наладили быт и начали строить новое общество. "
                     "Когда опасность миновала, они вышли наружу — основателями нового мира.")
            title = "🏆 Выжившие в бункере!"
        return (f"🎉 <b>ИГРА ОКОНЧЕНА!</b>\n\n🔒 <b>{title}</b>\n{names_str}\n\n📖 <i>{story}</i>")

    def vote_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for uid, data in self.players.items():
            builder.button(text=f"👎 {data['name']}",
                           callback_data=BunkerVoteCallback(target_id=uid, chat_id=self.chat_id).pack())
        builder.adjust(1)
        return builder.as_markup()

    def join_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="🚪 Участвовать",
                       callback_data=BunkerJoinCallback(chat_id=self.chat_id).pack())
        builder.adjust(1)
        return builder.as_markup()


def _bunker_active(chat_id: int) -> BunkerGame | None:
    g = bunker_games.get(chat_id)
    return g if g and g.phase != "finished" else None

# =====================================================================
# ПОКЕР (полный перенос из оригинала)
# =====================================================================
POKER_SMALL_BLIND = 10
POKER_BIG_BLIND = 20
POKER_MAX_PLAYERS = 6
POKER_MIN_PLAYERS = 2
POKER_REG_SECONDS = 120
POKER_TURN_SECONDS = 60

RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
SUITS = ['♥️', '♦️', '♣️', '♠️']
RANK_VALUE = {r: i for i, r in enumerate(RANKS, 2)}
HAND_NAMES = ['Старшая карта', 'Пара', 'Две пары', 'Тройка', 'Стрит',
              'Флеш', 'Фулл-хаус', 'Каре', 'Стрит-флеш', 'Роял-флеш']


def _new_deck() -> list[str]:
    deck = [f"{r}{s}" for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def _card_rank(card: str) -> int:
    for r in sorted(RANK_VALUE, key=lambda x: -len(x)):
        if card.startswith(r):
            return RANK_VALUE[r]
    return 0


def _card_suit(card: str) -> str:
    for s in SUITS:
        if card.endswith(s):
            return s
    return ''


def _hand_rank_5(cards: list[str]) -> tuple:
    from collections import Counter
    ranks = sorted([_card_rank(c) for c in cards], reverse=True)
    suits = [_card_suit(c) for c in cards]
    is_flush = len(set(suits)) == 1
    is_straight = (ranks == list(range(ranks[0], ranks[0] - 5, -1))) or \
                  (sorted(ranks) == [2, 3, 4, 5, 14])
    if is_straight and sorted(ranks) == [2, 3, 4, 5, 14]:
        ranks = [5, 4, 3, 2, 1]
    cnt = Counter(ranks)
    groups = sorted(cnt.values(), reverse=True)
    vals = sorted(cnt.keys(), key=lambda x: (cnt[x], x), reverse=True)
    if is_flush and is_straight:
        return (9, ranks) if ranks[0] == 14 else (8, ranks)
    if groups[0] == 4: return (7, vals)
    if groups[:2] == [3, 2]: return (6, vals)
    if is_flush: return (5, ranks)
    if is_straight: return (4, ranks)
    if groups[0] == 3: return (3, vals)
    if groups[:2] == [2, 2]: return (2, vals)
    if groups[0] == 2: return (1, vals)
    return (0, ranks)


def _best_hand(cards: list[str]) -> tuple:
    best = None
    best_combo = []
    for combo in combinations(cards, 5):
        rank = _hand_rank_5(list(combo))
        if best is None or rank > best:
            best = rank
            best_combo = list(combo)
    return best, best_combo


class PokerGame:
    def __init__(self, chat_id: int, creator_id: int, creator_name: str):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.creator_name = creator_name
        self.players: dict[int, dict] = {}
        self.order: list[int] = []
        self.phase = "registration"
        self.deck: list[str] = []
        self.community: list[str] = []
        self.pot: int = 0
        self.current_bet: int = 0
        self.dealer_idx: int = 0
        self.current_idx: int = 0
        self.round_done: set[int] = set()
        self.reg_deadline = int(time.time()) + POKER_REG_SECONDS
        self.turn_task = None

    def active_players(self) -> list[int]:
        return [uid for uid in self.order if not self.players[uid]["folded"]]

    def players_who_can_act(self) -> list[int]:
        return [uid for uid in self.active_players() if not self.players[uid]["allin"]]

    def table_text(self) -> str:
        community_str = " ".join(self.community) if self.community else "—"
        lines = [f"🃏 <b>Карты на столе:</b> {community_str}",
                 f"💰 <b>Банк (Pot):</b> {self.pot} монет",
                 f"💵 <b>Текущая ставка:</b> {self.current_bet}", "",
                 "<b>👤 Статус игроков:</b>"]
        dealer_id = self.order[self.dealer_idx] if self.order else None
        for uid in self.order:
            p = self.players[uid]
            if p["folded"]:
                status = "❌ Фолд"
            elif p["allin"]:
                status = f"💥 Ва-банк (ставка {p['total_bet']})"
            else:
                status = f"Ставка {p['total_bet']}, баланс {p['balance']}"
            dealer_mark = " 🎴Дилер" if uid == dealer_id else ""
            lines.append(f"  • {p['name']}{dealer_mark} — {status}")
        return "\n".join(lines)

    def start_game(self):
        uids = list(self.players.keys())
        random.shuffle(uids)
        self.order = uids
        self.deck = _new_deck()
        for uid in self.order:
            self.players[uid]["hole"] = [self.deck.pop(), self.deck.pop()]
            self.players[uid]["bet"] = 0
            self.players[uid]["total_bet"] = 0
            self.players[uid]["folded"] = False
            self.players[uid]["allin"] = False
        n = len(self.order)
        self.dealer_idx = 0
        sb_idx = 1 % n
        bb_idx = 2 % n
        self._post_blind(self.order[sb_idx], POKER_SMALL_BLIND)
        self._post_blind(self.order[bb_idx], POKER_BIG_BLIND)
        self.current_bet = POKER_BIG_BLIND
        self.current_idx = (bb_idx + 1) % n
        if n == 2:
            self.current_idx = sb_idx
        self.round_done = set()
        self.phase = "preflop"

    def _post_blind(self, uid: int, amount: int):
        p = self.players[uid]
        actual = min(amount, p["balance"])
        p["balance"] -= actual
        p["bet"] = actual
        p["total_bet"] += actual
        self.pot += actual
        if p["balance"] == 0:
            p["allin"] = True

    def action_fold(self, uid: int) -> str:
        self.players[uid]["folded"] = True
        self.round_done.add(uid)
        return f"❌ {self.players[uid]['name']} сбросил карты (Фолд)."

    def action_check(self, uid: int) -> tuple[bool, str]:
        p = self.players[uid]
        if p["bet"] < self.current_bet:
            return False, (f"❌ Нельзя чек — текущая ставка {self.current_bet}, "
                           f"твоя ставка {p['bet']}. Нужно Колл или Рейз.")
        self.round_done.add(uid)
        return True, f"✅ {p['name']} — Чек."

    def action_call(self, uid: int) -> str:
        p = self.players[uid]
        need = self.current_bet - p["bet"]
        actual = min(need, p["balance"])
        p["balance"] -= actual
        p["bet"] += actual
        p["total_bet"] += actual
        self.pot += actual
        if p["balance"] == 0:
            p["allin"] = True
        self.round_done.add(uid)
        return f"✅ {p['name']} — Колл ({actual} монет). Баланс: {p['balance']}"

    def action_raise(self, uid: int, amount: int) -> tuple[bool, str]:
        p = self.players[uid]
        total_raise = self.current_bet + amount
        need = total_raise - p["bet"]
        if need <= 0:
            return False, "❌ Некорректная сумма рейза."
        if need > p["balance"]:
            return False, f"❌ Недостаточно монет. Нужно {need}, есть {p['balance']}."
        p["balance"] -= need
        p["bet"] += need
        p["total_bet"] += need
        self.pot += need
        self.current_bet = p["bet"]
        if p["balance"] == 0:
            p["allin"] = True
        self.round_done = {uid}
        return True, f"📈 {p['name']} — Рейз до {self.current_bet}! Баланс: {p['balance']}"

    def action_allin(self, uid: int) -> str:
        p = self.players[uid]
        amount = p["balance"]
        if p["bet"] + amount > self.current_bet:
            self.current_bet = p["bet"] + amount
            self.round_done = {uid}
        p["total_bet"] += amount
        p["bet"] += amount
        p["balance"] = 0
        p["allin"] = True
        self.pot += amount
        self.round_done.add(uid)
        return f"💥 {p['name']} — ВА-БАНК! (+{amount} монет в банк)"

    def is_round_over(self) -> bool:
        can_act = self.players_who_can_act()
        if not can_act:
            return True
        for uid in can_act:
            if uid not in self.round_done:
                return False
            if self.players[uid]["bet"] < self.current_bet:
                return False
        return True

    def advance_phase(self) -> str:
        transitions = {"preflop": "flop", "flop": "turn", "turn": "river", "river": "showdown"}
        self.phase = transitions.get(self.phase, "showdown")
        for uid in self.order:
            self.players[uid]["bet"] = 0
        self.current_bet = 0
        self.round_done = set()
        if self.phase == "flop":
            self.community = [self.deck.pop(), self.deck.pop(), self.deck.pop()]
        elif self.phase in ("turn", "river"):
            self.community.append(self.deck.pop())
        n = len(self.order)
        idx = (self.dealer_idx + 1) % n
        for _ in range(n):
            uid = self.order[idx]
            if not self.players[uid]["folded"] and not self.players[uid]["allin"]:
                self.current_idx = idx
                break
            idx = (idx + 1) % n
        else:
            self.current_idx = idx
        return self.phase

    def showdown(self) -> list:
        alive = self.active_players()
        results = []
        for uid in alive:
            all_cards = self.players[uid]["hole"] + self.community
            rank, best5 = _best_hand(all_cards)
            hand_name = HAND_NAMES[rank[0]]
            cards_str = " ".join(self.players[uid]["hole"])
            results.append((uid, hand_name, cards_str, rank, best5))
        results.sort(key=lambda x: x[3], reverse=True)
        return results

    def distribute_pot(self) -> dict[int, int]:
        alive = self.active_players()
        winnings: dict[int, int] = {uid: 0 for uid in self.order}
        if len(alive) == 1:
            winnings[alive[0]] = self.pot
            return winnings
        results = self.showdown()
        best_rank = results[0][3]
        winners = [r for r in results if r[3] == best_rank]
        share = self.pot // len(winners)
        for w in winners:
            winnings[w[0]] = share
        winnings[winners[0][0]] += self.pot - share * len(winners)
        return winnings

# =====================================================================
# HELP TEXT
# =====================================================================
HELP_TEXT = (
    "📋 <b>Все команды игры «МанасWorker»</b>\n\n"
    "━━━ 👤 Персонаж ━━━\n"
    "<b>Профиль</b> — посмотреть профиль, уровень, баланс, характеристики\n\n"
    "━━━ 💼 Работа ━━━\n"
    "<b>Профессии</b> — меню профессий, выбор и повышение грейда\n"
    "<b>Работа</b> — отработать смену и получить монеты и XP\n\n"
    "━━━ 💪 Развитие ━━━\n"
    "<b>Тренировки</b> — прокачать Интеллект или Выносливость\n"
    "<b>Навыки</b> — улучшить навыки\n\n"
    "━━━ 🛒 Магазин ━━━\n"
    "<b>Магазин</b> — купить снаряжение или расходники\n\n"
    "━━━ 📈 Биржа ━━━\n"
    "<b>акция ап [сумма]</b> / <b>акция давн [сумма]</b> / <b>акция нейтрал [сумма]</b>\n\n"
    "━━━ 🏗 Бункер ━━━\n"
    "<b>бункер создать</b> — создать игру\n"
    "<b>+</b> — войти в игру\n"
    "<b>бункер старт</b> — начать игру (создатель)\n"
    "<b>открыть [характеристика]</b> — раскрыть черту персонажа\n"
    "<b>бункер голосование</b> — запустить голосование\n"
    "<b>кик @username</b> — проголосовать против игрока\n"
    "<b>бункер итог</b> — подвести итоги (создатель)\n"
    "<b>бункер статус</b> — состояние игры\n"
    "<b>бункер отмена</b> — отменить игру\n\n"
    "━━━ 🃏 Покер ━━━\n"
    "<b>покер создать</b> / <b>+</b> / <b>покер старт</b>\n"
    "<b>чек</b> / <b>колл</b> / <b>рейз [сумма]</b> / <b>фолд</b> / <b>ва-банк</b>\n"
    "<b>покер стол</b> / <b>покер отмена</b>\n\n"
    "━━━ 💸 Переводы ━━━\n"
    "<b>перевод @username 500</b> или ответом на сообщение <b>перевод 500</b>\n\n"
    "<b>Команда</b> — это меню"
)

# =====================================================================
# FASTAPI WEBHOOK HANDLER
# =====================================================================

app = FastAPI()

@app.post("/api")
async def telegram_webhook(request: Request):
    try:
        # Инициализируем пул БД при первом запросе
        await get_db()

        json_data = await request.json()
        update = Update.model_validate(json_data, context={"bot": bot})
        await dp.feed_update(bot, update)

    except Exception as e:
        print(f"Error processing update: {e}")

    return {"status": "ok"}
# =====================================================================
# ХЕНДЛЕРЫ — БУНКЕР
# =====================================================================

@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower().startswith("бункер создать"))
)
async def bunker_create(message: Message):
    chat_id = message.chat.id
    creator_id = message.from_user.id
    if _bunker_active(chat_id):
        await message.answer("⚠️ В этом чате уже идёт игра «Бункер»!")
        return
    text_lower = message.text.strip().lower()
    if "манас" in text_lower:
        mode = "manas"
    elif "обычный" in text_lower or "классик" in text_lower:
        mode = "classic"
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="🏫 Режим «Манас» (КТУ-студенты)",
                       callback_data=BunkerModeCallback(mode="manas").pack())
        builder.button(text="☢️ Режим «Обычный» (классический апокалипсис)",
                       callback_data=BunkerModeCallback(mode="classic").pack())
        builder.adjust(1)
        await message.answer("🏗 <b>Создание игры «Бункер»</b>\n\nВыберите режим:",
                             reply_markup=builder.as_markup())
        return
    await _start_bunker_registration(message, creator_id, mode)


@dp.callback_query(BunkerModeCallback.filter())
async def bunker_mode_chosen(callback: CallbackQuery, callback_data: BunkerModeCallback):
    chat_id = callback.message.chat.id
    if _bunker_active(chat_id):
        await callback.answer("Игра уже идёт!", show_alert=True)
        return
    await callback.answer()
    await _start_bunker_registration(callback.message, callback.from_user.id, callback_data.mode)


async def _start_bunker_registration(message: Message, creator_id: int, mode: str):
    chat_id = message.chat.id
    creator_name = message.from_user.full_name if hasattr(message, "from_user") and message.from_user else "Организатор"
    game = BunkerGame(chat_id, creator_id, creator_name, mode)
    game.players[creator_id] = {"name": creator_name, "card": {}}
    bunker_games[chat_id] = game
    mode_label = "🏫 Режим «Манас»" if mode == "manas" else "☢️ Режим «Обычный»"
    await message.answer(
        f"🏗 <b>Открыта регистрация в «Бункер»!</b>\n{mode_label}\n\n"
        f"👤 Создал: <b>{creator_name}</b>\n"
        f"⏳ Регистрация открыта <b>{BUNKER_REGISTRATION_SECONDS // 60} минуты</b>.\n\n"
        f"Напиши <b>+</b> или нажми кнопку, чтобы войти!\n"
        f"Когда все готовы — создатель пишет <code>бункер старт</code>.",
        reply_markup=game.join_keyboard()
    )


@dp.callback_query(BunkerJoinCallback.filter())
async def bunker_join_button(callback: CallbackQuery, callback_data: BunkerJoinCallback):
    chat_id = callback_data.chat_id
    game = bunker_games.get(chat_id)
    if not game or game.phase != "registration":
        await callback.answer("Регистрация уже закрыта.", show_alert=True)
        return
    uid = callback.from_user.id
    name = callback.from_user.full_name
    if uid in game.players:
        await callback.answer("Ты уже в списке!", show_alert=True)
        return
    game.players[uid] = {"name": name, "card": {}}
    await callback.answer(f"✅ Ты в игре, {name}!", show_alert=True)
    await callback.message.answer(f"✅ <b>{name}</b> вошёл в бункер! Всего: <b>{len(game.players)}</b>")


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower() in ("бункер старт", "бункер start"))
)
async def bunker_start(message: Message):
    chat_id = message.chat.id
    game = _bunker_active(chat_id)
    if not game:
        await message.answer("❌ Нет активной игры. Начни с <code>бункер создать</code>.")
        return
    if game.phase != "registration":
        await message.answer("⚠️ Игра уже началась!")
        return
    if message.from_user.id != game.creator_id:
        await message.answer("❌ Только создатель игры может её запустить.")
        return
    if len(game.players) < 2:
        await message.answer("❌ Нужно хотя бы 2 игрока!")
        return
    game.start_game()
    n = len(game.players)
    limit = game.survivors_limit
    await message.answer(
        f"🚨 <b>ИГРА «БУНКЕР» НАЧАЛАСЬ!</b>\n\n{game.disaster_text}\n\n{game.bunker_text}\n\n"
        f"👥 Игроков: <b>{n}</b> | В бункер попадут: <b>{limit}</b>\nОстальные останутся снаружи..."
    )
    failed = []
    for uid, data in game.players.items():
        try:
            await bot.send_message(uid, game.card_text(uid))
        except Exception:
            failed.append(data["name"])
    players_list = "\n".join(f"  • {d['name']}" for d in game.players.values())
    msg = (f"📋 <b>Список игроков:</b>\n{players_list}\n\n"
           f"📬 Карты персонажей отправлены в личные сообщения!\n\n"
           f"<b>Фаза обсуждения:</b> Открывайте характеристики командой "
           f"<code>открыть [характеристика]</code>\n\n"
           f"Когда наговорились — создатель пишет <code>бункер голосование</code>.")
    if failed:
        msg += f"\n\n⚠️ Не удалось доставить карту: {', '.join(failed)}"
    await message.answer(msg)


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower().startswith("открыть "))
)
async def bunker_reveal(message: Message):
    chat_id = message.chat.id
    game = _bunker_active(chat_id)
    if not game or game.phase != "active":
        return
    attr = message.text.strip()[len("открыть "):].strip()
    ok, text = game.reveal(message.from_user.id, attr)
    if ok or "не участвуешь" in text or "Неизвестная" in text or "уже открывал" in text:
        await message.answer(text)


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower() in ("бункер голосование", "бункер голосовать"))
)
async def bunker_vote_start(message: Message):
    chat_id = message.chat.id
    game = _bunker_active(chat_id)
    if not game:
        await message.answer("❌ Нет активной игры.")
        return
    if game.phase not in ("active", "voting"):
        await message.answer("⚠️ Голосование сейчас недоступно.")
        return
    if message.from_user.id != game.creator_id:
        await message.answer("❌ Только создатель игры запускает голосование.")
        return
    if len(game.players) <= 1:
        await message.answer("Остался один игрок — игра завершена.")
        return
    game.phase = "voting"
    game.round_votes.clear()
    await message.answer(
        f"🗳 <b>Голосование!</b>\nКого выгоняем из бункера?\n"
        f"Нажмите на кнопку или напишите <code>кик @username</code>.",
        reply_markup=game.vote_keyboard()
    )


@dp.callback_query(BunkerVoteCallback.filter())
async def bunker_vote_button(callback: CallbackQuery, callback_data: BunkerVoteCallback):
    chat_id = callback_data.chat_id
    target_id = callback_data.target_id
    game = bunker_games.get(chat_id)
    if not game or game.phase != "voting":
        await callback.answer("Голосование недоступно.", show_alert=True)
        return
    ok, text = game.vote(callback.from_user.id, target_id)
    await callback.answer(text[:200], show_alert=not ok)
    if ok:
        active_voters = set(game.players.keys())
        voted = set(game.round_votes.keys())
        if active_voters == voted:
            await _finish_vote_round(callback.message, game)


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower().startswith("кик "))
)
async def bunker_kick_text(message: Message):
    chat_id = message.chat.id
    game = _bunker_active(chat_id)
    if not game or game.phase != "voting":
        return
    target_id = None
    target_name = None
    if message.entities:
        for ent in message.entities:
            if ent.type == "mention":
                username = message.text[ent.offset + 1: ent.offset + ent.length]
                for uid, data in game.players.items():
                    if data["name"].lstrip("@").lower() == username.lower():
                        target_id = uid
                        target_name = data["name"]
                        break
            elif ent.type == "text_mention" and ent.user:
                target_id = ent.user.id
                target_name = ent.user.full_name
                break
    if target_id is None:
        raw = message.text.strip()[len("кик "):].strip().lower().lstrip("@")
        for uid, data in game.players.items():
            if data["name"].lower() == raw:
                target_id = uid
                target_name = data["name"]
                break
    if target_id is None:
        await message.answer("❌ Игрок не найден.")
        return
    ok, text = game.vote(message.from_user.id, target_id)
    await message.answer(text)
    if ok:
        active_voters = set(game.players.keys())
        voted = set(game.round_votes.keys())
        if active_voters == voted:
            await _finish_vote_round(message, game)


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower() in ("бункер итог", "бункер результат"))
)
async def bunker_vote_end(message: Message):
    chat_id = message.chat.id
    game = _bunker_active(chat_id)
    if not game or game.phase != "voting":
        await message.answer("❌ Сейчас нет активного голосования.")
        return
    if message.from_user.id != game.creator_id:
        await message.answer("❌ Только создатель подводит итоги.")
        return
    await _finish_vote_round(message, game)


async def _finish_vote_round(message: Message, game: BunkerGame):
    _, result_text = game.count_votes()
    await message.answer(result_text)
    if game.phase == "finished":
        await message.answer(game.final_text())
        if game.chat_id in bunker_games:
            del bunker_games[game.chat_id]
    else:
        remaining = len(game.players)
        need = game.survivors_limit
        await message.answer(
            f"👥 Осталось: <b>{remaining}</b> (выбыть ещё <b>{remaining - need}</b>)\n\n"
            f"Продолжайте обсуждение! <code>бункер голосование</code>"
        )


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower() in ("бункер статус", "бункер status"))
)
async def bunker_status(message: Message):
    chat_id = message.chat.id
    game = _bunker_active(chat_id)
    if not game:
        await message.answer("Активной игры «Бункер» нет.")
        return
    phase_labels = {"registration": "📝 Регистрация", "active": "💬 Обсуждение",
                    "voting": "🗳 Голосование", "finished": "✅ Завершена"}
    players_list = "\n".join(f"  • {d['name']}" for d in game.players.values())
    mode_label = "🏫 Манас" if game.mode == "manas" else "☢️ Обычный"
    await message.answer(
        f"🏗 <b>Игра «Бункер»</b>\nРежим: {mode_label}\n"
        f"Фаза: {phase_labels.get(game.phase, game.phase)}\n"
        f"Игроков: <b>{len(game.players)}</b> (бункер вмещает {game.survivors_limit})\n\n"
        f"<b>Участники:</b>\n{players_list}"
    )


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower() in ("бункер отмена", "бункер стоп"))
)
async def bunker_cancel(message: Message):
    chat_id = message.chat.id
    game = _bunker_active(chat_id)
    if not game:
        await message.answer("Нет активной игры для отмены.")
        return
    if message.from_user.id != game.creator_id:
        await message.answer("❌ Только создатель может отменить игру.")
        return
    del bunker_games[chat_id]
    await message.answer("🚫 Игра «Бункер» отменена.")

# =====================================================================
# ХЕНДЛЕРЫ — ПОКЕР
# =====================================================================

@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower() == "покер создать")
)
async def poker_create(message: Message):
    chat_id = message.chat.id
    if chat_id in poker_games and poker_games[chat_id].phase != "showdown":
        await message.answer("⚠️ В этом чате уже идёт покер!")
        return
    uid = message.from_user.id
    name = message.from_user.full_name
    game = PokerGame(chat_id, uid, name)
    user_data = await get_user_safe(uid)
    game.players[uid] = {"name": name, "hole": [], "balance": user_data["balance"],
                         "bet": 0, "total_bet": 0, "folded": False, "allin": False}
    poker_games[chat_id] = game
    await message.answer(
        f"🃏 <b>Техасский Покер!</b>\n\n👤 Создал: <b>{name}</b>\n"
        f"⏳ Регистрация открыта <b>2 минуты</b>. Пиши <b>+</b> чтобы войти!\n"
        f"Мест: 1/{POKER_MAX_PLAYERS}\n\nКогда все готовы — <code>покер старт</code>\n"
        f"Блайнды: малый {POKER_SMALL_BLIND} / большой {POKER_BIG_BLIND} монет"
    )


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text == "+"
)
async def group_join_handler(message: Message):
    chat_id = message.chat.id
    uid = message.from_user.id
    name = message.from_user.full_name

    # Покер
    game = poker_games.get(chat_id)
    if game and game.phase == "registration":
        if uid in game.players:
            await message.answer(f"🟢 {name}, ты уже за столом!")
            return
        if len(game.players) >= POKER_MAX_PLAYERS:
            await message.answer("❌ Стол заполнен (максимум 6 игроков).")
            return
        user_data = await get_user_safe(uid)
        if user_data["balance"] < POKER_BIG_BLIND:
            await message.answer(f"❌ {name}, недостаточно монет. Нужно минимум {POKER_BIG_BLIND}.")
            return
        game.players[uid] = {"name": name, "hole": [], "balance": user_data["balance"],
                             "bet": 0, "total_bet": 0, "folded": False, "allin": False}
        await message.answer(f"✅ <b>{name}</b> сел за стол! Игроков: {len(game.players)}/{POKER_MAX_PLAYERS}\n"
                             f"Баланс в игре: {user_data['balance']} монет")
        return

    # Бункер
    b_game = _bunker_active(chat_id)
    if b_game and b_game.phase == "registration":
        if uid in b_game.players:
            await message.answer(f"🟢 {name}, ты уже в бункере!")
            return
        b_game.players[uid] = {"name": name, "card": {}}
        await message.answer(f"✅ <b>{name}</b> вошёл в бункер! Всего: <b>{len(b_game.players)}</b>")


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower() in ("покер старт", "покер start"))
)
async def poker_start_cmd(message: Message):
    chat_id = message.chat.id
    game = poker_games.get(chat_id)
    if not game or game.phase != "registration":
        await message.answer("❌ Нет активной регистрации. Начни с <code>покер создать</code>.")
        return
    if message.from_user.id != game.creator_id:
        await message.answer("❌ Только создатель может запустить игру.")
        return
    if len(game.players) < POKER_MIN_PLAYERS:
        await message.answer(f"❌ Нужно минимум {POKER_MIN_PLAYERS} игрока!")
        return
    await _poker_begin(message, game)


async def _poker_begin(message: Message, game: PokerGame):
    chat_id = game.chat_id
    game.start_game()
    # Обновляем балансы из БД (сохраняем блайнды)
    for uid, p in game.players.items():
        await update_user(uid, balance=p["balance"])

    failed = []
    for uid, p in game.players.items():
        try:
            await bot.send_message(uid, f"🃏 <b>Твои карманные карты:</b>\n\n"
                                        f"<b>{p['hole'][0]}  {p['hole'][1]}</b>\n\nУдачи! 🤞")
        except Exception:
            failed.append(p["name"])

    players_list = "\n".join(f"  • {p['name']} (баланс: {p['balance']})" for p in game.players.values())
    dealer_name = game.players[game.order[game.dealer_idx]]["name"]
    n = len(game.order)
    sb_name = game.players[game.order[1 % n]]["name"]
    bb_name = game.players[game.order[2 % n]]["name"]

    msg = (f"🃏 <b>ПОКЕР НАЧАЛСЯ!</b>\n\n<b>Игроки:</b>\n{players_list}\n\n"
           f"🎴 Дилер: <b>{dealer_name}</b>\n"
           f"💰 Малый блайнд ({POKER_SMALL_BLIND}): <b>{sb_name}</b>\n"
           f"💰 Большой блайнд ({POKER_BIG_BLIND}): <b>{bb_name}</b>\n\n"
           f"📬 Карманные карты отправлены в личные сообщения!")
    if failed:
        msg += f"\n\n⚠️ Не удалось доставить карты: {', '.join(failed)}"
    await message.answer(msg)
    await message.answer(game.table_text())
    await _poker_next_turn(message, game)


async def _poker_next_turn(message: Message, game: PokerGame):
    chat_id = game.chat_id
    alive = game.active_players()
    if len(alive) == 1:
        await _poker_end_single(message, game, alive[0])
        return
    if game.is_round_over():
        if game.phase == "river":
            await _poker_showdown(message, game)
            return
        new_phase = game.advance_phase()
        phase_labels = {"flop": "🌅 ФЛОП", "turn": "🌄 ТЕРН", "river": "🌃 РИВЕР", "showdown": "🏆 ВСКРЫТИЕ"}
        await message.answer(f"\n{phase_labels.get(new_phase, new_phase)}\n\n" + game.table_text())
        if not game.players_who_can_act():
            while game.phase != "showdown" and not game.players_who_can_act():
                np = game.advance_phase()
                if np == "showdown":
                    break
                await message.answer(f"{phase_labels.get(np, np)}\n\n" + game.table_text())
            await _poker_showdown(message, game)
            return

    current_uid = None
    n = len(game.order)
    for i in range(n):
        idx = game.current_idx % n
        uid = game.order[idx]
        p = game.players[uid]
        if not p["folded"] and not p["allin"]:
            current_uid = uid
            game.current_idx = idx
            break
        game.current_idx = (game.current_idx + 1) % n

    if current_uid is None:
        if game.phase == "river":
            await _poker_showdown(message, game)
        else:
            game.advance_phase()
            await _poker_next_turn(message, game)
        return

    p = game.players[current_uid]
    need = game.current_bet - p["bet"]
    actions = []
    if need == 0:
        actions.append("<code>чек</code>")
    else:
        actions.append(f"<code>колл</code> ({need} монет)")
    actions.append("<code>рейз [сумма]</code>")
    actions.append("<code>ва-банк</code>")
    actions.append("<code>фолд</code>")

    mention = f"<a href='tg://user?id={current_uid}'>{p['name']}</a>"
    await message.answer(
        f"⏳ Ход игрока: {mention}\n"
        f"💰 Банк: {game.pot} | Ставка: {game.current_bet} | "
        f"Твоя ставка: {p['bet']} | Баланс: {p['balance']}\n\n"
        f"Действия: {' / '.join(actions)}\n"
        f"<i>У тебя {POKER_TURN_SECONDS} секунд</i>"
    )

    # Таймер автофолда — НА VERCEL НЕ РАБОТАЕТ НАДЁЖНО!
    # Используется как best-effort: если функция жива — сработает
    if game.turn_task:
        try:
            game.turn_task.cancel()
        except Exception:
            pass

    async def auto_fold_task():
        await asyncio.sleep(POKER_TURN_SECONDS)
        g = poker_games.get(chat_id)
        if not g or g.phase == "showdown":
            return
        if game.order[g.current_idx % len(g.order)] == current_uid and \
                not g.players[current_uid]["folded"]:
            text = g.action_fold(current_uid)
            g.current_idx = (g.current_idx + 1) % len(g.order)
            await message.answer(f"⏰ Время вышло! {text}")
            await _poker_next_turn(message, g)

    try:
        game.turn_task = asyncio.create_task(auto_fold_task())
    except RuntimeError:
        pass  # Нет event loop — на Vercel игнорируем


async def _poker_end_single(message: Message, game: PokerGame, winner_id: int):
    winner = game.players[winner_id]
    new_bal = winner["balance"] + game.pot
    await update_user(winner_id, balance=new_bal)
    await message.answer(f"🏆 Все остальные сбросили карты!\n\n"
                         f"🥇 Победитель: <b>{winner['name']}</b>\n"
                         f"💰 Выигрыш: <b>{game.pot}</b> монет\n"
                         f"💳 Новый баланс: <b>{new_bal}</b> монет")
    if game.chat_id in poker_games:
        del poker_games[game.chat_id]


async def _poker_showdown(message: Message, game: PokerGame):
    results = game.showdown()
    winnings = game.distribute_pot()
    lines = ["🏆 <b>ВСКРЫТИЕ!</b>\n"]
    for uid, hand_name, cards_str, rank, best5 in results:
        lines.append(f"👤 <b>{game.players[uid]['name']}</b>\n"
                     f"   🃏 Карты: {cards_str}\n"
                     f"   🥇 Комбинация: <b>{hand_name}</b>")
    lines.append(f"\n🃏 <b>Общие карты:</b> {' '.join(game.community)}\n")
    winners_text = []
    for uid, prize in winnings.items():
        if prize > 0:
            new_bal = game.players[uid]["balance"] + prize
            await update_user(uid, balance=new_bal)
            winners_text.append(f"🥇 <b>{game.players[uid]['name']}</b> выигрывает "
                                 f"<b>{prize}</b> монет! (баланс: {new_bal})")
    lines.append("\n".join(winners_text))
    await message.answer("\n".join(lines))
    if game.chat_id in poker_games:
        del poker_games[game.chat_id]


def _get_poker_game_and_player(chat_id: int, uid: int):
    game = poker_games.get(chat_id)
    if not game or game.phase in ("registration", "showdown"):
        return None, None
    if uid not in game.players:
        return None, None
    if game.order[game.current_idx % len(game.order)] != uid:
        return None, None
    p = game.players[uid]
    if p["folded"] or p["allin"]:
        return None, None
    return game, p


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower() == "чек"))
async def poker_check(message: Message):
    game, p = _get_poker_game_and_player(message.chat.id, message.from_user.id)
    if not game:
        return
    ok, text = game.action_check(message.from_user.id)
    await message.answer(text)
    if ok:
        if game.turn_task:
            try:
                game.turn_task.cancel()
            except Exception:
                pass
        game.current_idx = (game.current_idx + 1) % len(game.order)
        await _poker_next_turn(message, game)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower() == "колл"))
async def poker_call(message: Message):
    game, p = _get_poker_game_and_player(message.chat.id, message.from_user.id)
    if not game:
        return
    if game.turn_task:
        try:
            game.turn_task.cancel()
        except Exception:
            pass
    text = game.action_call(message.from_user.id)
    await message.answer(text)
    game.current_idx = (game.current_idx + 1) % len(game.order)
    await _poker_next_turn(message, game)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower().startswith("рейз ")))
async def poker_raise(message: Message):
    game, p = _get_poker_game_and_player(message.chat.id, message.from_user.id)
    if not game:
        return
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("❌ Формат: <code>рейз 100</code>")
        return
    amount = int(parts[1])
    ok, text = game.action_raise(message.from_user.id, amount)
    await message.answer(text)
    if ok:
        if game.turn_task:
            try:
                game.turn_task.cancel()
            except Exception:
                pass
        game.current_idx = (game.current_idx + 1) % len(game.order)
        await _poker_next_turn(message, game)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower() == "фолд"))
async def poker_fold(message: Message):
    game, p = _get_poker_game_and_player(message.chat.id, message.from_user.id)
    if not game:
        return
    if game.turn_task:
        try:
            game.turn_task.cancel()
        except Exception:
            pass
    text = game.action_fold(message.from_user.id)
    await message.answer(text)
    game.current_idx = (game.current_idx + 1) % len(game.order)
    await _poker_next_turn(message, game)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower() == "ва-банк"))
async def poker_allin(message: Message):
    game, p = _get_poker_game_and_player(message.chat.id, message.from_user.id)
    if not game:
        return
    if game.turn_task:
        try:
            game.turn_task.cancel()
        except Exception:
            pass
    text = game.action_allin(message.from_user.id)
    await message.answer(text)
    game.current_idx = (game.current_idx + 1) % len(game.order)
    await _poker_next_turn(message, game)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower() in ("покер статус", "покер стол")))
async def poker_status(message: Message):
    game = poker_games.get(message.chat.id)
    if not game:
        await message.answer("Активной покер-игры нет.")
        return
    await message.answer(game.table_text())


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower() in ("покер отмена", "покер стоп")))
async def poker_cancel(message: Message):
    chat_id = message.chat.id
    game = poker_games.get(chat_id)
    if not game:
        await message.answer("Нет активной покер-игры.")
        return
    if message.from_user.id != game.creator_id:
        await message.answer("❌ Только создатель может отменить игру.")
        return
    if game.turn_task:
        try:
            game.turn_task.cancel()
        except Exception:
            pass
    for uid, p in game.players.items():
        if p["total_bet"] > 0:
            refund = p["balance"] + p["total_bet"]
            await update_user(uid, balance=refund)
    del poker_games[chat_id]
    await message.answer("🚫 Покер отменён. Ставки возвращены игрокам.")

# =====================================================================
# ХЕНДЛЕРЫ — ПЕРЕВОДЫ
# =====================================================================

@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.text.func(lambda t: t and t.strip().lower().startswith("перевод"))
)
async def transfer_money_group(message: Message):
    sender_id = message.from_user.id
    sender_name = message.from_user.full_name
    sender = await get_user_safe(sender_id)
    mention_sender = f"<a href='tg://user?id={sender_id}'>{sender_name}</a>"
    parts = message.text.strip().split()
    target_id = None
    target_name = None
    amount = None

    if message.reply_to_message and message.reply_to_message.from_user:
        ru = message.reply_to_message.from_user
        if ru.id == sender_id:
            await message.answer("❌ Нельзя переводить самому себе.")
            return
        if ru.is_bot:
            await message.answer("❌ Нельзя переводить боту.")
            return
        target_id = ru.id
        target_name = ru.full_name
        if len(parts) == 2 and parts[1].isdigit():
            amount = int(parts[1])
    elif message.entities:
        for ent in message.entities:
            if ent.type == "mention":
                uname = message.text[ent.offset:ent.offset + ent.length]
                try:
                    chat_member = await bot.get_chat_member(message.chat.id, uname)
                    ru = chat_member.user
                    if ru.id == sender_id:
                        await message.answer("❌ Нельзя переводить самому себе.")
                        return
                    if ru.is_bot:
                        await message.answer("❌ Нельзя переводить боту.")
                        return
                    target_id = ru.id
                    target_name = ru.full_name
                except Exception:
                    await message.answer(f"❌ Не удалось найти {uname}.\n"
                                         "Попробуй ответить на его сообщение командой <code>перевод [сумма]</code>.")
                    return
            elif ent.type == "text_mention" and ent.user:
                ru = ent.user
                if ru.id == sender_id:
                    await message.answer("❌ Нельзя переводить самому себе.")
                    return
                target_id = ru.id
                target_name = ru.full_name
        for part in reversed(parts):
            if part.isdigit():
                amount = int(part)
                break

    if target_id is None:
        await message.answer("❌ Форматы:\n• <code>перевод @username 500</code>\n"
                             "• Ответь на сообщение: <code>перевод 500</code>")
        return
    if amount is None:
        await message.answer("❌ Не указана сумма.")
        return
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше нуля.")
        return
    if amount > sender["balance"]:
        await message.answer(f"❌ Недостаточно монет.\nХочешь: <b>{amount}</b> | Баланс: <b>{sender['balance']}</b>")
        return

    await register_user(target_id)
    receiver = await get_user(target_id)
    if not receiver:
        await message.answer("❌ Получатель не найден. Пусть напишет /start боту.")
        return

    new_sender_balance = sender["balance"] - amount
    new_receiver_balance = receiver["balance"] + amount
    await update_user(sender_id, balance=new_sender_balance)
    await update_user(target_id, balance=new_receiver_balance)
    mention_receiver = f"<a href='tg://user?id={target_id}'>{target_name}</a>"
    await message.answer(
        f"💸 <b>Перевод выполнен!</b>\n\n"
        f"👤 От: {mention_sender}\n👤 Кому: {mention_receiver}\n"
        f"💰 Сумма: <b>{amount}</b> монет\n\n"
        f"📊 Баланс {sender_name}: <b>{new_sender_balance}</b>\n"
        f"📊 Баланс {target_name}: <b>{new_receiver_balance}</b>"
    )


@dp.message(F.chat.type == "private",
            F.text.func(lambda t: t and t.strip().lower().startswith("перевод")))
async def transfer_money_private(message: Message):
    await message.answer("ℹ️ Переводы работают только в групповых чатах.\n\n"
                         "Форматы:\n• <code>перевод @username 500</code>\n"
                         "• Ответь на сообщение: <code>перевод 500</code>")

# =====================================================================
# ХЕНДЛЕРЫ — ЛИЧНЫЕ СООБЩЕНИЯ
# =====================================================================

@dp.message(Command("start"), F.chat.type == "private")
async def cmd_start_private(message: Message):
    await get_user_safe(message.from_user.id)
    await message.answer(
        f"👋 Привет, <b>{message.from_user.full_name}</b>!\n\nДобро пожаловать в «МанасWorker»!",
        reply_markup=get_main_menu()
    )


@dp.message(F.text == "👤 Профиль", F.chat.type == "private")
async def btn_profile_private(message: Message):
    user = await get_user_safe(message.from_user.id)
    await message.answer(build_profile_text(user, message.from_user.mention_html()))


@dp.message(F.text == "💼 Профессии", F.chat.type == "private")
async def btn_jobs_private(message: Message):
    user = await get_user_safe(message.from_user.id)
    kb = get_jobs_keyboard(user)
    text = build_jobs_text(user)
    if kb:
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text)


@dp.message(F.text == "🛠 Работа", F.chat.type == "private")
async def btn_work_private(message: Message):
    user = await get_user_safe(message.from_user.id)
    _, text = await do_work(user)
    await message.answer(text)


@dp.message(F.text == "🏋️ Тренировки", F.chat.type == "private")
async def btn_training_private(message: Message):
    user = await get_user_safe(message.from_user.id)
    await message.answer(build_training_text(user), reply_markup=get_training_keyboard())


@dp.message(F.text == "🧠 Навыки", F.chat.type == "private")
async def btn_skills_private(message: Message):
    user = await get_user_safe(message.from_user.id)
    text, kb = build_skills_text(user)
    await message.answer(text, reply_markup=kb)


@dp.message(F.text == "🛒 Магазин", F.chat.type == "private")
async def btn_shop_private(message: Message):
    user = await get_user_safe(message.from_user.id)
    await message.answer(build_shop_text(user), reply_markup=get_shop_keyboard())


def _is(text: str, *variants: str) -> bool:
    if not text:
        return False
    return text.strip().lower() in {v.lower() for v in variants}


@dp.message(Command("start"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_start_group(message: Message):
    await get_user_safe(message.from_user.id)
    await message.answer(
        f"👋 {message.from_user.mention_html()}, добро пожаловать!\n"
        f"Пиши <b>Профиль</b>, <b>Работа</b>, <b>Профессии</b>, "
        f"<b>Тренировки</b>, <b>Навыки</b>, <b>Магазин</b>.",
        reply_markup=get_main_menu()
    )


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and _is(t, "профиль", "profile")))
async def txt_profile_group(message: Message):
    user = await get_user_safe(message.from_user.id)
    await message.answer(build_profile_text(user, message.from_user.mention_html()))


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and _is(t, "профессии", "профессия", "jobs", "job")))
async def txt_jobs_group(message: Message):
    user = await get_user_safe(message.from_user.id)
    kb = get_jobs_keyboard(user)
    text = f"{message.from_user.mention_html()}\n{build_jobs_text(user)}"
    if kb:
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and _is(t, "работа", "работать", "work")))
async def txt_work_group(message: Message):
    user = await get_user_safe(message.from_user.id)
    _, text = await do_work(user)
    await message.answer(f"{message.from_user.mention_html()}\n{text}")


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and _is(t, "тренировки", "тренировка", "train")))
async def txt_train_group(message: Message):
    user = await get_user_safe(message.from_user.id)
    await message.answer(f"{message.from_user.mention_html()}\n{build_training_text(user)}",
                         reply_markup=get_training_keyboard())


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and _is(t, "навыки", "навык", "skills")))
async def txt_skills_group(message: Message):
    user = await get_user_safe(message.from_user.id)
    text, kb = build_skills_text(user)
    await message.answer(f"{message.from_user.mention_html()}\n{text}", reply_markup=kb)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and _is(t, "магазин", "shop")))
async def txt_shop_group(message: Message):
    user = await get_user_safe(message.from_user.id)
    await message.answer(f"{message.from_user.mention_html()}\n{build_shop_text(user)}",
                         reply_markup=get_shop_keyboard())


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and _is(t, "команда", "команды", "помощь", "help")))
async def txt_help_group(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(F.chat.type.in_({"group", "supergroup"}),
            F.text.func(lambda t: t and t.strip().lower().startswith("акция ")))
async def txt_stock_group(message: Message):
    user = await get_user_safe(message.from_user.id)
    mention = message.from_user.mention_html()
    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer(f"{mention}\n❌ Формат: <b>акция ап 300</b> / "
                             f"<b>акция давн 500</b> / <b>акция нейтрал 150</b>")
        return
    outcome_input = parts[1].lower()
    try:
        bet = int(parts[2])
    except ValueError:
        await message.answer(f"{mention}\n❌ Сумма ставки должна быть числом.")
        return
    _, text = await do_stock_bet(user, outcome_input, bet)
    await message.answer(f"{mention}\n{text}")


@dp.message(F.chat.type == "private",
            F.text.func(lambda t: t and t.strip().lower().startswith("акция ")))
async def txt_stock_private(message: Message):
    user = await get_user_safe(message.from_user.id)
    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer("❌ Формат: <b>акция ап 300</b> / <b>акция давн 500</b> / <b>акция нейтрал 150</b>")
        return
    outcome_input = parts[1].lower()
    try:
        bet = int(parts[2])
    except ValueError:
        await message.answer("❌ Сумма ставки должна быть числом.")
        return
    _, text = await do_stock_bet(user, outcome_input, bet)
    await message.answer(text)


@dp.message(F.chat.type == "private",
            F.text.func(lambda t: _is(t, "команда", "команды", "помощь", "help")))
async def txt_help_private(message: Message):
    await message.answer(HELP_TEXT)

# =====================================================================
# CALLBACKS (RPG-часть)
# =====================================================================

@dp.callback_query(JobCallback.filter())
async def callback_choose_job(callback: CallbackQuery, callback_data: JobCallback):
    user_id = callback.from_user.id
    user = await get_user_safe(user_id)
    job_key = callback_data.job_key
    job = JOBS.get(job_key)
    if not job:
        await callback.answer("❌ Профессия не найдена.", show_alert=True)
        return
    if user["job"] != "Безработный":
        await callback.answer("⛔ Ты уже выбрал профессию!", show_alert=True)
        return
    if job_key not in STARTER_JOB_KEYS:
        await callback.answer("❌ Нельзя начать с этой профессии.", show_alert=True)
        return
    await update_user(user_id, job=job["name"])
    await callback.answer(f"✅ Выбрана: {job['name']}!", show_alert=True)
    await callback.message.edit_text(
        f"✅ {callback.from_user.mention_html()} начинает карьеру!\n\n"
        f"💼 <b>{job['name']}</b>\n<i>{job['description']}</i>\n\n"
        f"📌 Это твой путь. Сменить его нельзя."
    )


@dp.callback_query(UpgradeJobCallback.filter())
async def callback_upgrade_job(callback: CallbackQuery, callback_data: UpgradeJobCallback):
    user = await get_user_safe(callback.from_user.id)
    next_key = callback_data.job_key
    next_job = JOBS.get(next_key)
    if not next_job:
        await callback.answer("❌ Профессия не найдена.", show_alert=True)
        return
    can, why = check_upgrade_conditions(user)
    if not can:
        await callback.answer(why[:200], show_alert=True)
        return
    current_job_key = get_job_key(user)
    if not current_job_key:
        await callback.answer("❌ Ошибка профессии.", show_alert=True)
        return
    current_job = JOBS[current_job_key]
    if current_job.get("evolves_to") != next_key:
        await callback.answer("❌ Этот грейд не следует из твоей профессии.", show_alert=True)
        return
    await update_user(callback.from_user.id, job=next_job["name"], job_rank=1)
    await callback.answer(f"🌟 Грейд повышен! Теперь ты: {next_job['name']}", show_alert=True)
    updated = await get_user(callback.from_user.id)
    await callback.message.edit_text(build_jobs_text(updated), reply_markup=get_jobs_keyboard(updated))


@dp.callback_query(F.data.startswith("upgrade_skill:"))
async def callback_upgrade_skill(callback: CallbackQuery):
    skill_key = callback.data.split(":")[1]
    user = await get_user_safe(callback.from_user.id)
    success, text = await do_upgrade_skill(user, skill_key)
    await callback.answer(text[:200], show_alert=not success)
    if success:
        updated = await get_user(callback.from_user.id)
        new_text, new_kb = build_skills_text(updated)
        await callback.message.edit_text(new_text, reply_markup=new_kb)


@dp.callback_query(ShopCallback.filter())
async def callback_shop_buy(callback: CallbackQuery, callback_data: ShopCallback):
    user = await get_user_safe(callback.from_user.id)
    success, text = await do_buy_item(user, callback_data.item_key)
    await callback.answer(text[:200], show_alert=True)
    if success:
        updated = await get_user(callback.from_user.id)
        await callback.message.edit_text(build_shop_text(updated), reply_markup=get_shop_keyboard())


@dp.callback_query(F.data.startswith("consume:"))
async def callback_use_consumable(callback: CallbackQuery):
    item_key = callback.data.split(":")[1]
    user = await get_user_safe(callback.from_user.id)
    success, text = await do_use_consumable(user, item_key)
    await callback.answer(text[:200], show_alert=True)
    if success:
        updated = await get_user(callback.from_user.id)
        await callback.message.edit_text(build_shop_text(updated), reply_markup=get_shop_keyboard())


@dp.callback_query(TrainCallback.filter())
async def callback_train(callback: CallbackQuery, callback_data: TrainCallback):
    user = await get_user_safe(callback.from_user.id)
    success, text = await do_train(user, callback_data.stat)
    await callback.answer(text[:200], show_alert=True)
    if success:
        updated = await get_user(callback.from_user.id)
        await callback.message.edit_text(build_training_text(updated), reply_markup=get_training_keyboard())

