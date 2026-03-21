import os
import re
import asyncio
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession

from dotenv import load_dotenv
import httpx
import secrets
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

load_dotenv()

# —— Настройки ——
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SMS_API_KEY = os.getenv("SMS_API_KEY")
ENV = os.getenv("ENV", "dev")
BACKEND_BASE_URL = os.getenv("BACKEND_PROD_URL") if ENV == "prod" else os.getenv("BACKEND_DEV_URL")

SERVICES_LOGIN = os.getenv("SERVICES_LOGIN")
SERVICES_PASSWORD = os.getenv("SERVICES_PASSWORD")
service_cookies: httpx.Cookies | None = None

# —— Блокировки: tg_id -> datetime до которого заблокирован ——
blocked_users: dict[int, datetime] = {}

MAX_CODE_ATTEMPTS = 3
BLOCK_MINUTES = 15

# —— Хранилище авторизованных пользователей ——
STORAGE_FILE = "authorized_users.json"

required = ["TELEGRAM_BOT_TOKEN", "SMS_API_KEY"]
missing = [k for k in required if not os.getenv(k)]
if missing:
    raise ValueError(f"Не хватает переменных в .env: {', '.join(missing)}")

session = AiohttpSession(proxy="http://127.0.0.1:10809")
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())


# —— FSM ——
class AuthStates(StatesGroup):
    waiting_for_contact = State()
    waiting_for_code = State()


# —— JSON хранилище ——
def load_authorized() -> set[int]:
    """Загружает авторизованных пользователей из файла."""
    if not os.path.exists(STORAGE_FILE):
        return set()
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("tg_ids", []))
    except Exception as e:
        print(f"[STORAGE] Ошибка чтения: {e}")
        return set()

def save_authorized(tg_ids: set[int]):
    """Сохраняет авторизованных пользователей в файл."""
    try:
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"tg_ids": list(tg_ids)}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[STORAGE] Ошибка записи: {e}")

def is_authorized_local(tg_id: int) -> bool:
    return tg_id in load_authorized()

def authorize_local(tg_id: int):
    tg_ids = load_authorized()
    tg_ids.add(tg_id)
    save_authorized(tg_ids)
    print(f"[STORAGE] Сохранён tg_id={tg_id}")


# —— Вспомогательные функции ——
def normalize_phone(s: str) -> str | None:
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    return None

def generate_code(length: int = 4) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))

def is_blocked(tg_id: int) -> datetime | None:
    until = blocked_users.get(tg_id)
    if until and datetime.now() < until:
        return until
    if until:
        del blocked_users[tg_id]
    return None

def block_user(tg_id: int):
    blocked_users[tg_id] = datetime.now() + timedelta(minutes=BLOCK_MINUTES)
    print(f"[BLOCK] tg_id={tg_id} заблокирован до {blocked_users[tg_id]}")

async def get_service_session() -> httpx.Cookies | None:
    global service_cookies
    if service_cookies:
        return service_cookies

    url_login = f"{BACKEND_BASE_URL}/api/login"
    data = {"username": SERVICES_LOGIN, "password": SERVICES_PASSWORD}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url_login, json=data)
            print("LOGIN STATUS:", response.status_code)
            if response.status_code == 200:
                service_cookies = response.cookies
                return service_cookies
            return None
    except Exception as e:
        print(f"Ошибка при логине: {e}")
        return None

async def get_installer_by_phone(phone: str) -> dict | None | bool:
    """Возвращает dict если найден, None если 204, False если ошибка."""
    global service_cookies
    cookies = await get_service_session()
    if not cookies:
        return False

    url = f"{BACKEND_BASE_URL}/api/installer/phone/{phone}"
    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            response = await client.get(url)
            print("STATUS:", response.status_code, "BODY:", response.text)
            if response.status_code in (401, 403):
                service_cookies = None
                return False
            if response.status_code == 200 and response.text:
                return response.json()
            if response.status_code == 204:
                return None
            return False
    except Exception as e:
        print(f"Ошибка запроса installer: {e}")
        return False

async def send_verification_sms(phone: str, code: str) -> bool:
    if ENV == "dev":
        print(f"[DEV] Код для {phone}: {code}")
        return True

    cookies = await get_service_session()
    if not cookies:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            response = await client.post(
                f"{BACKEND_BASE_URL}/api/sms/sendVerificationMessage",
                json={"phone_number": phone, "code": code}  
            )
            print(f"[SMS] status={response.status_code} body={response.text}")
            return response.status_code == 200
    except Exception as e:
        print(f"[SMS] Ошибка: {e}")
        return False

async def update_installer_tg(
    installer_id: int,
    full_name: str,
    phone: str,
    tg_id: int,
) -> bool:
    """Сохраняем tgId установщика."""
    global service_cookies
    cookies = await get_service_session()
    if not cookies:
        return False

    params = {
        "id": installer_id,
        "fullName": full_name,
        "phone": re.sub(r"\D", "", phone),
        "tgId": str(tg_id),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            response = await client.put(f"{BACKEND_BASE_URL}/api/installer", params=params)
            print("PUT STATUS:", response.status_code, "BODY:", response.text)
            if response.status_code in (401, 403):
                service_cookies = None
            return response.status_code == 200
    except Exception as e:
        print(f"[PUT] Ошибка: {e}")
        return False


# —— Telegram-хендлеры ——
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id
    print(f"Пользователь запустил бота. tg_id={tg_id}")

    blocked_until = is_blocked(tg_id)
    if blocked_until:
        remaining = int((blocked_until - datetime.now()).total_seconds() / 60) + 1
        await message.answer(
            f"Вы заблокированы на {remaining} мин. из-за превышения попыток ввода кода.\n"
            "Попробуйте позже."
        )
        return

    if is_authorized_local(tg_id):
        await message.answer(
            "Вы уже авторизованы.\nЗаказы будут приходить автоматически."
        )
        return

    await state.clear()
    kb = [[types.KeyboardButton(text="📱 Отправить номер", request_contact=True)]]
    await message.answer(
        "Привет! Я бот дверной компании.\n\nПожалуйста, отправьте ваш номер телефона.",
        reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    )
    await state.set_state(AuthStates.waiting_for_contact)

@dp.message(AuthStates.waiting_for_contact)
async def handle_contact(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id

    # Проверяем блокировку
    blocked_until = is_blocked(tg_id)
    if blocked_until:
        remaining = int((blocked_until - datetime.now()).total_seconds() / 60) + 1
        await message.answer(f"Вы заблокированы. Попробуйте через {remaining} мин.")
        return

    try:
        if message.contact and message.contact.phone_number:
            phone = normalize_phone(message.contact.phone_number)
        elif message.text:
            phone = normalize_phone(message.text)
        else:
            await message.answer("Пожалуйста, отправьте номер кнопкой или введите вручную.")
            return

        if not phone:
            kb = [[types.KeyboardButton(text="📱 Отправить номер", request_contact=True)]]
            await message.answer(
                "Неверный формат номера. Пример: +79001112233",
                reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
            )
            return

       
        installer = await get_installer_by_phone(phone)

        if installer is False:
            await message.answer("Ошибка соединения с сервером. Попробуйте позже.")
            return

        if installer is None:
            kb = [[types.KeyboardButton(text="📱 Отправить номер", request_contact=True)]]
            await message.answer(
                f"Номер {phone} не найден в базе установщиков.\n"
                "Проверьте номер или обратитесь к администратору.",
                reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
            )
            return

        code = generate_code()
        await state.update_data(
            phone=phone,
            expected_code=code,
            code_attempts=0,
            installer_id=installer["id"],
            installer_full_name=installer.get("fullName", ""),
            installer_phone=installer.get("phone", phone),
        )

        sent = await send_verification_sms(phone, code)
        if not sent:
            kb = [[types.KeyboardButton(text="📱 Отправить номер", request_contact=True)]]
            await message.answer(
                "Не удалось отправить SMS. Попробуйте ещё раз.",
                reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
            )
            return

        await message.answer(
            "Код отправлен в SMS. Введите его:",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(AuthStates.waiting_for_code)

    except Exception as e:
        print(f"Ошибка в хендлере contact: {e}")
        await state.clear()
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")

@dp.message(AuthStates.waiting_for_code)
async def handle_code(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id
    data = await state.get_data()
    expected_code = data.get("expected_code")
    phone = data.get("phone")
    installer_id = data.get("installer_id")
    installer_full_name = data.get("installer_full_name")
    installer_phone = data.get("installer_phone")
    code_attempts = data.get("code_attempts", 0)

    if message.text.strip() != expected_code:
        code_attempts += 1
        await state.update_data(code_attempts=code_attempts)
        remaining_attempts = MAX_CODE_ATTEMPTS - code_attempts

        if code_attempts >= MAX_CODE_ATTEMPTS:
            block_user(tg_id)
            await state.clear()
            await message.answer(
                f"Вы ввели неверный код {MAX_CODE_ATTEMPTS} раза.\n"
                f"Доступ заблокирован на {BLOCK_MINUTES} минут."
            )
            return

        await message.answer(
            f"Неверный код. Осталось попыток: {remaining_attempts}."
        )
        return

    success = await update_installer_tg(
        installer_id=installer_id,
        full_name=installer_full_name,
        phone=installer_phone,
        tg_id=tg_id,
    )

    if success:
        authorize_local(tg_id)
        await message.answer(
            "Авторизация успешна!\nТеперь вы будете получать новые заказы.",
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        await message.answer("Ошибка обновления данных. Обратитесь в поддержку.")

    await state.clear()


# —— FastAPI: /send-message ——
fastapi_app = FastAPI(title="FastDoor Bot API")
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class SendMessageRequest(BaseModel):
    TgId: str | None
    MaxId: str | None
    message: str

@fastapi_app.post("/send-message")
async def send_message_api(request: SendMessageRequest):
    try:
        await bot.send_message(chat_id=request.TgId, text=request.message[:4096])
        return {"status": "sent", "TgId": request.TgId, "MaxId": request.MaxId}
    except Exception as e:
        print(f"Не удалось отправить сообщение {request.TgId}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def start_bot():
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"Ошибка polling: {e}")
    finally:
        await bot.session.close()

async def start_fastapi():
    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=3000)
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    global service_cookies
    service_cookies = None
    authorized = load_authorized()
    print(f"Бот запущен. Режим: {ENV}, бэкенд: {BACKEND_BASE_URL}")
    print(f"Загружено авторизованных пользователей: {len(authorized)}")
    await asyncio.gather(
        start_fastapi(),
        start_bot()
    )

if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())