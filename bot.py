import os
import re
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
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


required = ["TELEGRAM_BOT_TOKEN", "SMS_API_KEY"]
missing = [k for k in required if not os.getenv(k)]
if missing:
    raise ValueError(f"Не хватает переменных в .env: {', '.join(missing)}")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# —— FSM ——
class AuthStates(StatesGroup):
    waiting_for_contact = State()
    waiting_for_code = State()

# —— Вспомогательные функции ——
def normalize_phone(s: str) -> str | None:
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    if len(digits) == 10:
        return "+7" + digits
    return None

def generate_code(length: int = 4) -> str:
    code = "".join(secrets.choice("0123456789") for _ in range(length))
    print(f"CODE: {code}")
    return code

async def send_sms(phone: str, code: str) -> bool:
    """Отправляем код через ручку backend, используя залогиненную сессию"""
    cookies = await get_service_session()
    if not cookies:
        print("Нет сессии services, SMS не отправлено")
        return False

    url = f"{BACKEND_BASE_URL}/api/sms/sendVerificationMessage"
    payload = {"code": code, "phone_number": phone}

    try:
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
            response = await client.post(url, json=payload)
            print("SMS STATUS:", response.status_code)
            print("SMS BODY:", response.text)
            if response.status_code == 200:
                data = response.json()
                return data.get("error", 1) == 0
            return False
    except Exception as e:
        print(f"Ошибка отправки СМС: {e}")
        return False
    
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
            print("LOGIN BODY:", response.text)
            if response.status_code == 200:
                service_cookies = response.cookies
                return service_cookies
            return None
    except Exception as e:
        print(f"Ошибка при логине: {e}")
        return None


async def get_installer_by_phone(phone: str) -> dict | None:
    """Возвращаем данные установщика или None"""
    cookies = await get_service_session()
    if not cookies:
        print("Номер не был найден в базе установщиков")
        return None

    url = f"{BACKEND_BASE_URL}/api/installer/phone/{phone}"
    async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
        response = await client.get(url)
        print("STATUS:", response.status_code, "BODY:", response.text)
        if response.status_code == 200 and response.text:
            return response.json()
        if response.status_code == 204:
            return None
        return None

async def update_installer_tg(
    installer_id: int,
    full_name: str,
    phone: str,
    tg_id: int | None,
    max_id: int | None,
) -> bool:
    """Обновляем TG ID через query params, как требует backend"""
    cookies = await get_service_session()
    if not cookies:
        return False

    params = {
        "id": installer_id,
        "fullName": full_name,
        "phone": phone.replace("+", ""),
        "tgId": str(tg_id),
        "maxId": str(max_id),
    }

    async with httpx.AsyncClient(timeout=10.0, cookies=cookies) as client:
        response = await client.put(f"{BACKEND_BASE_URL}/api/installer", params=params)
        print("PUT STATUS:", response.status_code, "BODY:", response.text)
        return response.status_code == 200

# —— Telegram-хендлеры ——
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    kb = [[types.KeyboardButton(text="📱 Отправить номер", request_contact=True)]]
    tg_id = message.from_user.id
    # max_id = message.from_user.id
    print(f"🔧 Пользователь запустил бота. tgId = {tg_id} maxId = None")
    await message.answer(
        "Привет! Я бот дверной компании.\n\n Пожалуйста, отправьте ваш номер телефона.",
        reply_markup=types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)
    )
    await state.set_state(AuthStates.waiting_for_contact)

@dp.message(AuthStates.waiting_for_contact)
async def handle_contact(message: types.Message, state: FSMContext):
    try:
        if message.contact and message.contact.phone_number:
            phone = normalize_phone(message.contact.phone_number)
        elif message.text:
            phone = normalize_phone(message.text)
        else:
            await message.answer("Пожалуйста, отправьте номер кнопкой или введите вручную.")
            return

        if not phone:
            await message.answer("Неверный формат. Пример: +79001112233")
            return

        code = generate_code()
        await state.update_data(phone=phone, expected_code=code)
        sent = await send_sms(phone, code)

        kb = [[types.KeyboardButton(text="📱 Отправить номер", request_contact=True)]]

        if not sent:
           
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
    data = await state.get_data()
    phone = data.get("phone")
    expected_code = data.get("expected_code")

    if message.text.strip() != expected_code:
        await message.answer("Неверный код. Попробуйте ещё раз.")
        return

    installer = await get_installer_by_phone(phone)

    if installer is None:
        await message.answer(
            "Номер не найден в базе установщиков.\n"
            "Обратитесь к администратору."
        )
        await state.clear()
        return  

    success = await update_installer_tg(
        installer_id=installer["id"],
        full_name=installer.get("fullName", ""),
        phone=installer.get("phone", phone),
        tg_id=message.from_user.id,
        # max_id=message.from_user.id
    )

    if success:
        await message.answer(
            "Авторизация успешна!\n Теперь вы будете получать новые заказы.",
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        await message.answer(
            "Ошибка обновления данных. Обратитесь в поддержку."
        )

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
    print(f"Бот запущен. Режим: {ENV}, бэкенд: {BACKEND_BASE_URL}")
    await asyncio.gather(
        start_fastapi(),
        start_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
