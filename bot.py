import asyncio
import os
import logging
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from google import genai
from google.genai import types as genai_types
from aiohttp import web

# Завантаження змінних середовища з файлу .env (для локального запуску)
load_dotenv()

# Передаємо НАЗВИ змінних середовища з панелі Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise ValueError("ПОМИЛКА: Змінна BOT_TOKEN відсутня в Environment Variables на Render!")

if not GEMINI_API_KEY:
    raise ValueError("ПОМИЛКА: Змінна GEMINI_API_KEY відсутня в Environment Variables на Render!")

# Ініціалізація клієнтів
client = genai.Client(api_key=GEMINI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

logging.basicConfig(level=logging.INFO)

# --- ВЕБ-СЕРВЕР ДЛЯ KEEP-ALIVE НА RENDER ---
async def handle_ping(request):
    return web.Response(text="Bot is running and healthy!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Keep-alive web server started on port {port}")
# -----------------------------------------------------

# Опис станів машини станів (FSM)
class MenuForm(StatesGroup):
    waiting_for_products = State()
    waiting_for_preferences = State()

# Системний промпт для Gemini
SYSTEM_PROMPT = """
Ти — професійний шеф-кухар та нутриціолог.
Твоє завдання — скласти тижневе меню для сім'ї з 3 осіб (2 дорослих + 1 дитина) та список покупок.

ВРАХОВУЙ НАСТУПНІ ПРАВИЛА:
1. Максимально використати "Наявні продукти вдома", щоб зменшити витрати.
2. Харчування має бути різноманітним:
   - Сніданок це обов'язково облет або яєчнеця 
   - Обід має бути ситним
   - Можна готувати одне блюдо на декілька днів головне його чередувати
   - Чергуй джерела білка (птиця, риба, м'ясо, яйця, сир).
   - Головні страви не повинні повторюватися два дні поспіль.
3. Склади список продуктів до купівлі, яких НЕМАЄ вдома.
4. Вкажи ціни на ринку/в супермаркетах Києва (в грн). Краще для кожного продукту додай вартість +5%

Оформи відповідь чітко та структуровано (використовуй емодзі та стандартний текст без складних тегів):
- Меню по днях (Пн-Нд: Сніданок, Обід, Вечеря).
- Меню має містити грами, мілітрів  т.п.
- Список покупок за категоріями з вагою та орієнтовною ціною.
- Загальний бюджет на тиждень. 
"""

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Вітаю! Я допоможу скласти різноманітне меню на тиждень для сім'ї з 3 осіб.\n\n"
        "Напишіть, які <b>продукти є вдома</b> (у холодильнику чи коморі):",
        parse_mode="HTML"
    )
    await state.set_state(MenuForm.waiting_for_products)

@router.message(MenuForm.waiting_for_products)
async def process_products(message: types.Message, state: FSMContext):
    await state.update_data(products=message.text)
    
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Без додаткових побажань")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await message.answer(
        "Зрозумів! Тепер вкажіть <b>додаткові побажання або обмеження</b> "
        "(наприклад: без алергенів, дитина не їсть цибулю, готувати обіди на 2 дні тощо):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(MenuForm.waiting_for_preferences)

@router.message(MenuForm.waiting_for_preferences)
async def process_preferences(message: types.Message, state: FSMContext):
    preferences = message.text
    if preferences == "Без додаткових побажань":
        preferences = "Немає"

    user_data = await state.get_data()
    products = user_data.get("products")

    await message.answer(
        "⏳ Генерую меню та розраховую список покупок... Зачекайте декілька секунд.",
        reply_markup=ReplyKeyboardRemove()
    )

    user_prompt = (
        f"Наявні продукти вдома:\n{products}\n\n"
        f"Особливі побажання та обмеження:\n{preferences}"
    )

    # Список моделей для почергової спроби
    candidate_models = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-1.5-flash"]
    response = None

    for model_name in candidate_models:
        for attempt in range(2):  # 2 спроби для кожної моделі
            try:
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.7,
                    )
                )
                if response and response.text:
                    break
            except Exception as e:
                logging.warning(f"Спроба {attempt + 1} для моделі {model_name} не вдалася: {e}")
                await asyncio.sleep(2)  # Пауза 2 секунди перед наступною спробою
        
        if response and response.text:
            break

    if response and response.text:
        result_text = response.text

        if len(result_text) > 4000:
            for x in range(0, len(result_text), 4000):
                await message.answer(result_text[x:x+4000])
        else:
            await message.answer(result_text)

        await message.answer("Щоб скласти нове меню, введіть команду /start")
    else:
        await message.answer("⚠️ Сервери Google зараз перевантажені. Будь ласка, спробуйте ще раз через 1-2 хвилини (/start).")

    await state.clear()

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())