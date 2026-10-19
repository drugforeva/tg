"""Админка аккаунтов-наблюдателей: вход по номеру телефона и коду прямо в боте.

Код из Telegram вводится кнопками-цифрами: если отправить его сообщением в любой
чат (в том числе боту), Telegram сразу аннулирует код. Нажатия кнопок сообщениями
не являются, поэтому код остаётся рабочим.
"""

import logging
import re
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from ..config import config
from ..db import Database
from ..render import esc
from .common import App, show

log = logging.getLogger(__name__)
router = Router(name="accounts")


class Login(StatesGroup):
    phone = State()
    code = State()
    password = State()


@dataclass
class LoginState:
    client: object
    phone: str
    phone_code_hash: str
    code: str = ""


def make_client(session: str = ""):
    # flood_sleep_threshold=0: FloodWait обрабатывает пул аккаунтов.
    return TelegramClient(
        StringSession(session),
        config.api_id,
        config.api_hash,
        flood_sleep_threshold=0,
        device_model="Gift Bot",
        system_version="1.0",
        app_version="1.0",
    )


def _factory(app: App):
    return app.client_factory or make_client


def is_admin(user_id: int) -> bool:
    return user_id in config.admins


def mask(phone: str) -> str:
    return phone[:4] + "•" * max(0, len(phone) - 6) + phone[-2:]


async def accounts_view(db: Database, app: App) -> tuple[str, InlineKeyboardMarkup]:
    rows = await db.accounts()
    live = {m.key for m in getattr(app.pool, "members", [])}
    kb = InlineKeyboardBuilder()
    lines = ["🛠 <b>Аккаунты-наблюдатели</b>", ""]
    if not rows and not live:
        lines.append("Пока ни одного. Без аккаунта бот не видит улучшений и маркета — подключите хотя бы один.")
    for r in rows:
        status = "✅" if f"db:{r['id']}" in live else "⛔️"
        lines.append(f"{status} {esc(r['name'] or '—')} · {mask(r['phone'])}")
        kb.row(
            InlineKeyboardButton(
                text=f"✖️ Отключить {r['name'] or mask(r['phone'])}"[:60], callback_data=f"acc:del:{r['id']}"
            )
        )
    from_env = [m for m in getattr(app.pool, "members", []) if not m.key.startswith("db:")]
    if from_env:
        lines.append(f"⚙️ Из настроек (SESSION_STRING): {len(from_env)}")
    lines += [
        "",
        "Лучше подключать отдельные аккаунты, не основной. Несколько аккаунтов — быстрее и надёжнее.",
    ]
    kb.row(InlineKeyboardButton(text="➕ Подключить аккаунт", callback_data="acc:add"))
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"))
    return "\n".join(lines), kb.as_markup()


def keypad(code: str) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for d in "123456789":
        kb.button(text=d, callback_data=f"acc:k:{d}")
    kb.button(text="⌫", callback_data="acc:k:back")
    kb.button(text="0", callback_data="acc:k:0")
    kb.button(text="✅ Готово", callback_data="acc:k:ok")
    kb.adjust(3)
    kb.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="acc:cancel"))
    shown = " ".join(code) if code else "—"
    text = (
        "🔢 Telegram прислал код на подключаемый аккаунт.\n"
        "<b>Введите его кнопками ниже</b> — не отправляйте сообщением, иначе Telegram его аннулирует.\n\n"
        f"Код: <code>{shown}</code>"
    )
    return text, kb.as_markup()


async def drop_login(app: App, user_id: int) -> None:
    login = app.logins.pop(user_id, None)
    if login:
        try:
            await login.client.disconnect()
        except Exception:
            pass


async def finish_login(message: Message, state: FSMContext, db: Database, app: App, login: LoginState) -> None:
    me = await login.client.get_me()
    name = " ".join(x for x in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if x) or None
    session = login.client.session.save()
    account_id = await db.add_account(login.phone, name, getattr(me, "id", None), session)
    app.logins.pop(message.chat.id, None)
    await state.clear()
    if app.pool is not None:
        app.pool.remove(f"db:{account_id}")
        app.pool.add(login.client, f"#{account_id} {name or ''}".strip(), key=f"db:{account_id}")
    text, kb = await accounts_view(db, app)
    await message.answer(f"✅ Аккаунт {esc(name or login.phone)} подключён и уже работает.\n\n{text}", reply_markup=kb)


@router.message(Command("accounts"), F.chat.type == ChatType.PRIVATE)
async def cmd_accounts(message: Message, db: Database, app: App) -> None:
    if not is_admin(message.from_user.id):
        return
    text, kb = await accounts_view(db, app)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("acc:"))
async def cb_accounts(call: CallbackQuery, state: FSMContext, db: Database, app: App) -> None:
    if not is_admin(call.from_user.id) or call.message.chat.type != ChatType.PRIVATE:
        await call.answer("Только для админа", show_alert=True)
        return
    parts = call.data.split(":")
    action = parts[1]
    user_id = call.from_user.id

    if action == "list":
        await show(call, *await accounts_view(db, app))
    elif action == "add":
        await drop_login(app, user_id)
        await state.set_state(Login.phone)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="acc:cancel")]])
        await show(
            call, "📱 Пришлите номер телефона аккаунта в международном формате, например <code>+79991234567</code>.", kb
        )
    elif action == "cancel":
        await drop_login(app, user_id)
        await state.clear()
        await show(call, *await accounts_view(db, app))
    elif action == "del":
        account_id = int(parts[2])
        member = app.pool.remove(f"db:{account_id}") if app.pool is not None else None
        if member:
            try:
                await member.client.log_out()  # завершаем сессию и на стороне Telegram
            except Exception:
                log.info("Не удалось завершить сессию аккаунта %s", account_id)
        await db.delete_account(account_id)
        await show(call, *await accounts_view(db, app))
    elif action == "k":
        login = app.logins.get(user_id)
        if login is None or await state.get_state() != Login.code.state:
            await call.answer("Вход устарел, начните заново", show_alert=True)
            return
        key = parts[2]
        if key == "back":
            login.code = login.code[:-1]
        elif key.isdigit() and len(login.code) < 8:
            login.code += key
        elif key == "ok":
            await submit_code(call, state, db, app, login)
            return
        await show(call, *keypad(login.code))


async def submit_code(call: CallbackQuery, state: FSMContext, db: Database, app: App, login: LoginState) -> None:
    if len(login.code) < 5:
        await call.answer("Код слишком короткий", show_alert=True)
        return
    try:
        await login.client.sign_in(phone=login.phone, code=login.code, phone_code_hash=login.phone_code_hash)
    except errors.SessionPasswordNeededError:
        await state.set_state(Login.password)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="acc:cancel")]])
        await show(
            call,
            "🔐 На аккаунте включён облачный пароль (2FA). Пришлите его сообщением — я сразу удалю его из чата.",
            kb,
        )
        return
    except errors.PhoneCodeInvalidError:
        login.code = ""
        await call.answer("Неверный код, введите ещё раз", show_alert=True)
        await show(call, *keypad(login.code))
        return
    except (errors.PhoneCodeExpiredError, errors.RPCError) as e:
        await drop_login(app, call.from_user.id)
        await state.clear()
        await call.answer(f"Не получилось: {type(e).__name__}. Начните заново.", show_alert=True)
        await show(call, *await accounts_view(db, app))
        return
    await call.answer()
    await finish_login(call.message, state, db, app, login)


@router.message(Login.phone, F.text, ~F.text.startswith("/"))
async def on_phone(message: Message, state: FSMContext, db: Database, app: App) -> None:
    if not is_admin(message.from_user.id):
        return
    phone = "+" + re.sub(r"\D", "", message.text)
    if len(phone) < 8:
        await message.answer("Это не похоже на номер. Пример: <code>+79991234567</code>")
        return
    client = _factory(app)()
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except errors.FloodWaitError as e:
        await message.answer(f"Telegram просит подождать {e.seconds} с перед новым кодом. Попробуйте позже.")
        await client.disconnect()
        return
    except (errors.RPCError, ConnectionError, OSError) as e:
        await message.answer(f"Не удалось отправить код: {esc(str(e))}. Проверьте номер и попробуйте ещё раз.")
        await client.disconnect()
        return
    app.logins[message.chat.id] = LoginState(client, phone, sent.phone_code_hash)
    await state.set_state(Login.code)
    text, kb = keypad("")
    await message.answer(text, reply_markup=kb)


@router.message(Login.password, F.text, ~F.text.startswith("/"))
async def on_password(message: Message, state: FSMContext, db: Database, app: App) -> None:
    if not is_admin(message.from_user.id):
        return
    password = message.text
    try:
        await message.delete()  # не оставляем пароль в истории чата
    except Exception:
        pass
    login = app.logins.get(message.chat.id)
    if login is None:
        await state.clear()
        await message.answer("Вход устарел, начните заново: /accounts")
        return
    try:
        await login.client.sign_in(password=password)
    except errors.PasswordHashInvalidError:
        await message.answer("Неверный пароль, пришлите ещё раз.")
        return
    except errors.RPCError as e:
        await drop_login(app, message.chat.id)
        await state.clear()
        await message.answer(f"Не получилось: {type(e).__name__}. Начните заново: /accounts")
        return
    await finish_login(message, state, db, app, login)
