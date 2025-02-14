import logging
import os
import asyncio
import re
import json
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Необходимо указать TELEGRAM_BOT_TOKEN в файле .env")

# Получаем токены для GigaChat и ИИ
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
AI_API_KEY = os.getenv("AI_API_KEY")

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_gigachat.chat_models import GigaChat

from localization import get_msg, LANG_MAP
from states import Registration, AdditionalInfo, EditingSchedule

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Файл для хранения расписаний
SCHEDULES_FILE = "schedules.json"

def load_schedules():
    if os.path.exists(SCHEDULES_FILE):
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_schedules(schedules):
    with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
        json.dump(schedules, f, ensure_ascii=False, indent=4)

user_schedules = load_schedules()

# Глобальные словари для хранения данных
registered_users = set()
user_profiles = {}  # Базовый профиль: хранится город; дополнительные данные (интересы, национальность) добавляются позже

# --- Функция для интеллектуального подбора событий через GigaChat ---
async def suggest_events_with_gigachat(interests, free_time, city):
    model = GigaChat(
        credentials=GIGACHAT_CREDENTIALS,
        scope="GIGACHAT_API_PERS",
        model="GigaChat",
        verify_ssl_certs=False,
    )
    free_time_str = ", ".join([f"{ft[0].strftime('%H:%M')}-{ft[1].strftime('%H:%M')}" for ft in free_time])
    system_msg = SystemMessage(
        content=f"Ты бот, который помогает иностранному студенту адаптироваться в России. "
                f"Студент интересуется: {interests if interests else 'нет указанных интересов'}. "
                f"Свободное время сегодня: {free_time_str}. "
                f"Предложи актуальные мероприятия в городе {city} с указанием места и времени."
    )
    events = get_events_api(city)
    human_msg = HumanMessage(content=str(events))
    res = model.invoke([system_msg, human_msg])
    return res.content

def compute_free_time(schedule_for_day):
    free = []
    try:
        day_date = datetime.strptime(schedule_for_day["date"], "%d.%m.%Y")
    except Exception:
        day_date = datetime.now()
    work_start = datetime.combine(day_date.date(), datetime.strptime("08:00", "%H:%M").time())
    work_end = datetime.combine(day_date.date(), datetime.strptime("20:00", "%H:%M").time())
    events = sorted(schedule_for_day.get("events", []), key=lambda e: e["start"])
    if not events:
        free.append((work_start, work_end))
        return free
    first_event_start = datetime.combine(day_date.date(), datetime.strptime(events[0]["start"], "%H:%M").time())
    if work_start < first_event_start:
        free.append((work_start, first_event_start))
    for i in range(len(events) - 1):
        current_end = datetime.combine(day_date.date(), datetime.strptime(events[i]["end"], "%H:%M").time())
        next_start = datetime.combine(day_date.date(), datetime.strptime(events[i+1]["start"], "%H:%M").time())
        if current_end < next_start:
            free.append((current_end, next_start))
    last_event_end = datetime.combine(day_date.date(), datetime.strptime(events[-1]["end"], "%H:%M").time())
    if last_event_end < work_end:
        free.append((last_event_end, work_end))
    return free

def get_events_api(city, days_ahead=30, max_events=30):
    import requests
    base_url = "https://kudago.com/public-api/v1.4/events/"
    now = int(time.time())
    params = {
        "location": city,
        "actual_since": now,
        "actual_until": now + days_ahead * 24 * 3600,
        "fields": "id,title,dates,place,description,price",
        "page_size": max_events,
        "lang": "ru",
        "expand": "place"
    }
    response = requests.get(base_url, params=params)
    if response.status_code == 200:
        return response.json().get("results", [])
    else:
        return []

# --- Регистрационный поток ---
@dp.message(Command("start"))
async def start_command(message: types.Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    if user_id in registered_users:
        await message.answer(get_msg("en", "already_registered"), parse_mode="HTML")
        return
    await message.answer(get_msg("en", "greeting"), parse_mode="HTML")
    lang_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Русский", callback_data="lang_ru"),
         InlineKeyboardButton(text="English", callback_data="lang_en")],
        [InlineKeyboardButton(text="Беларускі", callback_data="lang_be"),
         InlineKeyboardButton(text="Қазақша", callback_data="lang_kk")],
        [InlineKeyboardButton(text="中文", callback_data="lang_zh"),
         InlineKeyboardButton(text="한국어", callback_data="lang_ko")]
    ])
    await message.answer("Please choose your language:", reply_markup=lang_kb)
    await state.set_state(Registration.language)

@dp.callback_query(lambda callback: callback.data in LANG_MAP, StateFilter(Registration.language))
async def language_chosen(callback: types.CallbackQuery, state: FSMContext) -> None:
    lang_code = LANG_MAP[callback.data]
    await state.update_data(language=lang_code)
    logger.info(f"User {callback.from_user.id} выбрал язык: {lang_code}")
    await callback.answer()
    await callback.message.answer(get_msg(lang_code, "enter_login"), parse_mode="HTML")
    await state.set_state(Registration.account_login)

@dp.message(StateFilter(Registration.account_login))
async def process_login(message: types.Message, state: FSMContext) -> None:
    login = message.text.strip()
    data = await state.get_data()
    lang = data.get("language", "en")
    if not re.fullmatch(r'^[A-Za-z0-9_]+$', login):
        await message.answer(get_msg(lang, "invalid_login"), parse_mode="HTML")
        return
    await state.update_data(login=login)
    logger.info(f"User {message.from_user.id} ввёл логин: {login}")
    await message.answer(get_msg(lang, "enter_password"), parse_mode="HTML")
    await state.set_state(Registration.account_password)

@dp.message(StateFilter(Registration.account_password))
async def process_password(message: types.Message, state: FSMContext) -> None:
    password = message.text.strip()
    await state.update_data(password=password)
    data = await state.get_data()
    lang = data.get("language", "en")
    logger.info(f"User {message.from_user.id} ввёл пароль.")
    await message.answer(get_msg(lang, "enter_city"), parse_mode="HTML")
    await state.set_state(Registration.city)

@dp.message(StateFilter(Registration.city))
async def process_city(message: types.Message, state: FSMContext) -> None:
    city = message.text.strip()
    await state.update_data(city=city)
    data = await state.get_data()
    lang = data.get("language", "en")
    logger.info(f"User {message.from_user.id} ввёл город: {city}")
    user_profiles[str(message.from_user.id)] = {"city": city}
    uni_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ЦУ", callback_data="uni_cu"),
         InlineKeyboardButton(text="Бауманка", callback_data="uni_bauman")],
        [InlineKeyboardButton(text="ВШЭ", callback_data="uni_hse")]
    ])
    await message.answer(get_msg(lang, "choose_university"), reply_markup=uni_kb, parse_mode="HTML")
    await state.set_state(Registration.university)

@dp.callback_query(lambda callback: callback.data in ["uni_cu", "uni_bauman", "uni_hse"], StateFilter(Registration.university))
async def process_university(callback: types.CallbackQuery, state: FSMContext) -> None:
    global registered_users
    uni_map = {
        "uni_cu": "ЦУ",
        "uni_bauman": "Бауманка",
        "uni_hse": "ВШЭ"
    }
    university = uni_map.get(callback.data, "")
    await state.update_data(university=university)
    data = await state.get_data()
    lang = data.get("language", "en")
    logger.info(f"User {callback.from_user.id} выбрал вуз: {university}")
    await callback.answer()
    await callback.message.answer(get_msg(lang, "university_auth"), parse_mode="HTML")
    await asyncio.sleep(3)
    user_id = str(callback.from_user.id)
    if user_id not in user_schedules:
        user_schedules[user_id] = {
            "ПН": {"date": "03.02.2025", "events": [
                {"start": "09:00", "end": "10:30", "desc": "Лекция по математике"},
                {"start": "10:45", "end": "12:15", "desc": "Семинар по физике"},
                {"start": "13:00", "end": "14:30", "desc": "Практическое занятие по программированию"}
            ]},
            "ВТ": {"date": "04.02.2025", "events": [
                {"start": "09:00", "end": "10:30", "desc": "Лекция по информатике"},
                {"start": "10:45", "end": "12:15", "desc": "Практикум по алгоритмам"},
                {"start": "13:00", "end": "14:30", "desc": "Лабораторная по сетям"}
            ]},
            "СР": {"date": "05.02.2025", "events": [
                {"start": "09:00", "end": "10:30", "desc": "Лекция по истории"},
                {"start": "10:45", "end": "12:15", "desc": "Семинар по обществознанию"},
                {"start": "13:00", "end": "14:30", "desc": "Практическое занятие по праву"}
            ]},
            "ЧТ": {"date": "06.02.2025", "events": [
                {"start": "09:00", "end": "10:30", "desc": "Лекция по литературе"},
                {"start": "10:45", "end": "12:15", "desc": "Практикум по русскому языку"},
                {"start": "13:00", "end": "14:30", "desc": "Кафедральная практика"}
            ]},
            "ПТ": {"date": "07.02.2025", "events": [
                {"start": "09:00", "end": "10:30", "desc": "Лабораторная по химии"},
                {"start": "10:45", "end": "12:15", "desc": "Семинар по биологии"},
                {"start": "13:00", "end": "14:30", "desc": "Практическое занятие по экологии"}
            ]},
            "СБ": {"date": "08.02.2025", "events": [
                {"start": "10:00", "end": "12:00", "desc": "Практическая работа в лаборатории"},
                {"start": "13:00", "end": "14:30", "desc": "Семинар по спорту"},
                {"start": "15:00", "end": "16:30", "desc": "Внеучебная деятельность"}
            ]},
            "ВС": {"date": "09.02.2025", "events": []}
        }
        save_schedules(user_schedules)
    else:
        user_schedules.update(load_schedules())
    if user_id not in user_profiles or "interests" not in user_profiles.get(user_id, {}):
        final_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=get_msg(lang, "event_search"), callback_data="search_events")],
            [InlineKeyboardButton(text=get_msg(lang, "update_info"), callback_data="update_info")],
            [InlineKeyboardButton(text=get_msg(lang, "edit_schedule"), callback_data="edit_schedule")],
            [InlineKeyboardButton(text=get_msg(lang, "view_schedule"), callback_data="view_schedule")]
        ])
    else:
        final_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=get_msg(lang, "view_schedule"), callback_data="view_schedule")],
            [InlineKeyboardButton(text=get_msg(lang, "edit_schedule"), callback_data="edit_schedule")],
            [InlineKeyboardButton(text=get_msg(lang, "event_search"), callback_data="search_events")]
        ])
    await callback.message.answer(get_msg(lang, "registration_finished"), reply_markup=final_kb, parse_mode="HTML")
    registered_users.add(int(user_id))
    await state.clear()

# --- Обработка кнопки "Поиск события" ---
@dp.callback_query(lambda c: c.data == "search_events")
async def search_events_handler(callback: types.CallbackQuery, state: FSMContext) -> None:
    user_id = str(callback.from_user.id)
    profile = user_profiles.get(user_id, {})
    city = profile.get("city", "Екатеринбург")
    interests = profile.get("interests", "")
    weekday_map = {0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ", 4: "ПТ", 5: "СБ", 6: "ВС"}
    today_abbr = weekday_map[datetime.now().weekday()]
    if user_id in user_schedules and today_abbr in user_schedules[user_id]:
        schedule_for_day = user_schedules[user_id][today_abbr]
        free_time = compute_free_time(schedule_for_day)
    else:
        today_date = datetime.now().date()
        work_start = datetime.combine(today_date, datetime.strptime("08:00", "%H:%M").time())
        work_end = datetime.combine(today_date, datetime.strptime("20:00", "%H:%M").time())
        free_time = [(work_start, work_end)]
    suggested = await suggest_events_with_gigachat(interests, free_time, city)
    if not suggested:
        result_text = "Подходящих событий не найдено."
    else:
        result_text = f"Рекомендуемые события:\n{suggested}"
    await callback.answer()
    await callback.message.answer(result_text, parse_mode="HTML")

# --- Обработка кнопки "Мое расписание" ---
@dp.callback_query(lambda c: c.data == "view_schedule")
async def view_schedule_handler(callback: types.CallbackQuery, state: FSMContext) -> None:
    days = [("ПН", "ПН"), ("ВТ", "ВТ"), ("СР", "СР"), ("ЧТ", "ЧТ"), ("ПТ", "ПТ"), ("СБ", "СБ"), ("ВС", "ВС")]
    day_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=abbr, callback_data=f"day_{abbr}")] for abbr, _ in days
    ])
    await callback.answer()
    await callback.message.answer("Выберите день недели:", reply_markup=day_kb, parse_mode="HTML")

@dp.callback_query(lambda c: c.data.startswith("day_"))
async def day_schedule_handler(callback: types.CallbackQuery, state: FSMContext) -> None:
    day = callback.data[4:]
    user_id = str(callback.from_user.id)
    schedule = user_schedules.get(user_id, {})
    if day not in schedule:
        await callback.answer("Расписание не найдено.", show_alert=True)
        return
    date = schedule[day]["date"]
    events = schedule[day]["events"]
    events_str = "\n".join([f"{e['start']}-{e['end']}: {e['desc']}" for e in events]) if events else "Нет событий."
    output = f"<b>{day} {date}</b>\n{events_str}"
    await callback.answer()
    await callback.message.answer(output, parse_mode="HTML")

# --- Обработка кнопки "Дополнить информацию о себе" ---
@dp.callback_query(lambda c: c.data == "update_info")
async def update_info_handler(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    lang = "ru"
    update_activity_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=str(i)) for i in range(1, 6)]
    ])
    await state.set_state(AdditionalInfo.activity)
    await callback.message.answer(get_msg(lang, "update_enter_activity"), reply_markup=update_activity_kb, parse_mode="HTML")

@dp.callback_query(lambda c: c.data in [str(i) for i in range(1, 6)], StateFilter(AdditionalInfo.activity))
async def update_info_activity_cb(callback: types.CallbackQuery, state: FSMContext) -> None:
    chosen_activity = callback.data
    await state.update_data(additional_activity=chosen_activity)
    lang = "ru"
    logger.info(f"User {callback.from_user.id} (update info) выбрал активность: {chosen_activity}")
    await callback.message.delete()
    update_sociability_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=str(i)) for i in range(1, 6)]
    ])
    await state.set_state(AdditionalInfo.sociability)
    await bot.send_message(callback.message.chat.id, get_msg(lang, "update_enter_sociability"), reply_markup=update_sociability_kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(lambda c: c.data in [str(i) for i in range(1, 6)], StateFilter(AdditionalInfo.sociability))
async def update_info_sociability_cb(callback: types.CallbackQuery, state: FSMContext) -> None:
    chosen_sociability = callback.data
    await state.update_data(additional_sociability=chosen_sociability)
    lang = "ru"
    logger.info(f"User {callback.from_user.id} (update info) выбрал общительность: {chosen_sociability}")
    await callback.message.delete()
    await state.set_state(AdditionalInfo.interests)
    await bot.send_message(callback.message.chat.id, get_msg(lang, "update_enter_hobbies"), parse_mode="HTML")
    await callback.answer()

@dp.message(StateFilter(AdditionalInfo.interests))
async def update_info_interests(message: types.Message, state: FSMContext) -> None:
    interests = message.text.strip()
    await state.update_data(additional_interests=interests)
    lang = "ru"
    # Переходим к запросу предпочтительной национальности
    await state.set_state(AdditionalInfo.nationality)
    await message.answer(get_msg(lang, "update_enter_nationality"), parse_mode="HTML")

@dp.message(StateFilter(AdditionalInfo.nationality))
async def update_info_nationality(message: types.Message, state: FSMContext) -> None:
    nationality = message.text.strip()
    lang = "ru"
    profile = user_profiles.get(str(message.from_user.id), {})
    profile["nationality"] = nationality
    user_profiles[str(message.from_user.id)] = profile
    await message.answer(get_msg(lang, "info_updated"), parse_mode="HTML")
    final_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_msg(lang, "view_schedule"), callback_data="view_schedule")],
        [InlineKeyboardButton(text=get_msg(lang, "edit_schedule"), callback_data="edit_schedule")],
        [InlineKeyboardButton(text=get_msg(lang, "event_search"), callback_data="search_events")]
    ])
    await message.answer(get_msg(lang, "registration_finished"), reply_markup=final_kb, parse_mode="HTML")
    await state.clear()

# --- Обработка кнопки "Внести правки в расписание" ---
@dp.callback_query(lambda c: c.data == "edit_schedule")
async def edit_schedule_handler(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EditingSchedule.new_event)
    await callback.message.answer(get_msg("ru", "edit_schedule_prompt"), parse_mode="HTML")

@dp.message(StateFilter(EditingSchedule.new_event))
async def process_new_event(message: types.Message, state: FSMContext) -> None:
    text = message.text.strip()
    pattern = r"^(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+(.+)$"
    match = re.match(pattern, text)
    if not match:
        await message.answer("Неверный формат. Попробуйте снова. Пример: ПН 19:40 - 21:30 просмотр фильма", parse_mode="HTML")
        return
    day, start, end, event_desc = match.groups()
    new_event = {"start": start, "end": end, "desc": event_desc}
    user_id = str(message.from_user.id)
    schedules = load_schedules()
    user_schedule = schedules.get(user_id, {})
    if day not in user_schedule:
        user_schedule[day] = {"date": get_date_for_day(day), "events": []}
    user_schedule[day]["events"].append(new_event)
    # Сортируем события по времени начала
    user_schedule[day]["events"].sort(key=lambda e: datetime.strptime(e["start"], "%H:%M"))
    schedules[user_id] = user_schedule
    save_schedules(schedules)
    global user_schedules
    user_schedules = schedules
    await message.answer("Событие добавлено.", parse_mode="HTML")
    final_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=get_msg("ru", "view_schedule"), callback_data="view_schedule")],
        [InlineKeyboardButton(text=get_msg("ru", "edit_schedule"), callback_data="edit_schedule")],
        [InlineKeyboardButton(text=get_msg("ru", "event_search"), callback_data="search_events")]
    ])
    await message.answer(get_msg("ru", "registration_finished"), reply_markup=final_kb, parse_mode="HTML")
    await state.clear()

def get_date_for_day(day_abbr: str) -> str:
    mapping = {
        "ПН": "03.02.2025",
        "ВТ": "04.02.2025",
        "СР": "05.02.2025",
        "ЧТ": "06.02.2025",
        "ПТ": "07.02.2025",
        "СБ": "08.02.2025",
        "ВС": "09.02.2025"
    }
    return mapping.get(day_abbr, "")

if __name__ == '__main__':
    async def main():
        await dp.start_polling(bot)
    asyncio.run(main())