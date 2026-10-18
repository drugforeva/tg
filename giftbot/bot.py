import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import config
from .db import Database
from .filters import describe, fmt_permille, matches
from .models import Filters, Upgrade

log = logging.getLogger(__name__)
router = Router()
db: Database  # выставляется в setup()

PAGE_SIZE = 10
RARITY_OPTIONS = [None, 50, 20, 10, 5, 2]  # промилле: любая, 5%, 2%, 1%, 0.5%, 0.2%
TEXT_FIELDS = {
    "models": "🧩 Пришлите названия моделей через запятую (ищется по вхождению).",
    "backdrops": "🎨 Пришлите названия фонов через запятую.",
    "symbols": "🔣 Пришлите названия узоров через запятую.",
    "max_num": "🔢 Пришлите максимальный номер подарка (например 1000).",
}
RARITY_FIELDS = {"model": "модели", "backdrop": "фона", "symbol": "узора"}


class Edit(StatesGroup):
    waiting = State()


def setup(database: Database) -> Router:
    global db
    db = database
    return router


# ---------- оформление ----------


def _attr(label: str, name: str | None, rarity: int | None) -> str:
    if not name:
        return ""
    return f"{label}: <b>{html.escape(name)}</b> ({fmt_permille(rarity)})\n"


def format_upgrade(u: Upgrade) -> str:
    if u.owner_username:
        owner = f"@{html.escape(u.owner_username)}"
    elif u.owner_id and u.owner_name:
        owner = f'<a href="tg://user?id={u.owner_id}">{html.escape(u.owner_name)}</a>'
    elif u.owner_name:
        owner = html.escape(u.owner_name)
    elif u.owner_address:
        owner = f"<code>{html.escape(u.owner_address)}</code> (TON)"
    else:
        owner = "скрыт"
    text = (
        f"🆕 <b>{html.escape(u.collection)} #{u.num}</b>  ·  {u.issued}/{u.total}\n"
        + _attr("🧩 Модель", u.model, u.model_rarity)
        + _attr("🎨 Фон", u.backdrop, u.backdrop_rarity)
        + _attr("🔣 Узор", u.symbol, u.symbol_rarity)
        + f"👤 Владелец: {owner}\n"
    )
    if u.resale_stars:
        text += f"💰 На продаже: {u.resale_stars} ⭐\n"
    return text + f'\n<a href="{u.link}">{u.link}</a>'


def upgrade_kb(u: Upgrade) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть подарок", url=u.link)]]
    )


def _flag(on: bool) -> str:
    return "✅" if on else "▫️"


def filters_kb(f: Filters) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🎁 Коллекции ({len(f.collections) or 'все'})", callback_data="col:p:0")
    kb.button(text="🧩 Модель", callback_data="txt:models")
    kb.button(text="🎨 Фон", callback_data="txt:backdrops")
    kb.button(text="🔣 Узор", callback_data="txt:symbols")
    kb.button(text=f"⭐ Редкость модели: {fmt_permille(f.max_model_rarity) if f.max_model_rarity else 'любая'}", callback_data="rar:model")
    kb.button(text=f"⭐ Редкость фона: {fmt_permille(f.max_backdrop_rarity) if f.max_backdrop_rarity else 'любая'}", callback_data="rar:backdrop")
    kb.button(text=f"⭐ Редкость узора: {fmt_permille(f.max_symbol_rarity) if f.max_symbol_rarity else 'любая'}", callback_data="rar:symbol")
    kb.button(text="🔢 Макс. номер", callback_data="txt:max_num")
    kb.button(text=f"{_flag(f.pretty_num)} Красивые номера", callback_data="tog:pretty_num")
    kb.button(text=f"{_flag(f.with_username)} Владелец с @username", callback_data="tog:with_username")
    kb.button(text=f"{_flag(f.on_sale)} Только на продаже", callback_data="tog:on_sale")
    kb.button(text="♻️ Сбросить всё", callback_data="reset")
    kb.adjust(1, 3, 1, 1, 1, 2, 1, 1, 1)
    return kb.as_markup()


async def show_filters(target: Message | CallbackQuery, user_id: int) -> None:
    f = await db.get_filters(user_id)
    active = await db.is_active(user_id)
    text = (
        "⚙️ <b>Фильтры</b>\n\n"
        + describe(f)
        + f"\n\nУведомления: {'🔔 включены' if active else '🔕 на паузе'}"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=filters_kb(f))
        await target.answer()
    else:
        await target.answer(text, reply_markup=filters_kb(f))


# ---------- команды ----------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await db.ensure_user(message.from_user.id)
    await message.answer(
        "👋 Я присылаю подарки Telegram, которые <b>только что улучшили</b> до уникальных.\n\n"
        "Настройте фильтры, чтобы получать только интересное: коллекции, модели, фоны, "
        "узоры, редкость, номер, владельца.\n\n"
        "/filters — фильтры\n"
        "/recent — последние улучшения по вашим фильтрам\n"
        "/pause, /resume — пауза уведомлений"
    )
    await show_filters(message, message.from_user.id)


@router.message(Command("filters"))
async def cmd_filters(message: Message) -> None:
    await db.ensure_user(message.from_user.id)
    await show_filters(message, message.from_user.id)


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    await db.ensure_user(message.from_user.id)
    await db.set_active(message.from_user.id, False)
    await message.answer("🔕 Уведомления на паузе. /resume — включить.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    await db.ensure_user(message.from_user.id)
    await db.set_active(message.from_user.id, True)
    await message.answer("🔔 Уведомления включены.")


@router.message(Command("recent"))
async def cmd_recent(message: Message, command: CommandObject) -> None:
    await db.ensure_user(message.from_user.id)
    try:
        limit = max(1, min(int(command.args or 10), 30))
    except ValueError:
        limit = 10
    f = await db.get_filters(message.from_user.id)
    found = [u for u in await db.recent_upgrades(2000) if matches(f, u)][:limit]
    if not found:
        await message.answer("Пока ничего не нашлось по вашим фильтрам.")
        return
    lines = []
    for u in found:
        extra = ", ".join(x for x in (u.model, u.backdrop, u.symbol) if x)
        lines.append(f'• <a href="{u.link}">{html.escape(u.collection)} #{u.num}</a> — {html.escape(extra)}')
    await message.answer(
        f"📜 Последние улучшения ({len(found)}):\n\n" + "\n".join(lines),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.message(Command("stats"), F.from_user.id.in_(config.admins))
async def cmd_stats(message: Message) -> None:
    s = await db.stats()
    await message.answer(
        f"👥 Пользователей: {s['users']} (активных {s['active']})\n"
        f"🎁 Коллекций: {s['collections']}\n"
        f"✨ Улучшений: {s['upgrades']} (за сутки {s['upgrades_24h']})"
    )


# ---------- фильтры: переключатели и редкость ----------


@router.callback_query(F.data.startswith("tog:"))
async def cb_toggle(call: CallbackQuery) -> None:
    field = call.data.split(":", 1)[1]
    f = await db.get_filters(call.from_user.id)
    setattr(f, field, not getattr(f, field))
    await db.set_filters(call.from_user.id, f)
    await show_filters(call, call.from_user.id)


@router.callback_query(F.data == "reset")
async def cb_reset(call: CallbackQuery) -> None:
    await db.set_filters(call.from_user.id, Filters())
    await show_filters(call, call.from_user.id)


@router.callback_query(F.data == "back")
async def cb_back(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_filters(call, call.from_user.id)


@router.callback_query(F.data.regexp(r"^rar:\w+$"))
async def cb_rarity_menu(call: CallbackQuery) -> None:
    kind = call.data.split(":")[1]
    kb = InlineKeyboardBuilder()
    for value in RARITY_OPTIONS:
        label = "Любая" if value is None else f"≤ {fmt_permille(value)}"
        kb.button(text=label, callback_data=f"rar:{kind}:{value or 0}")
    kb.button(text="⬅️ Назад", callback_data="back")
    kb.adjust(3, 3, 1)
    await call.message.edit_text(
        f"⭐ Максимальная редкость {RARITY_FIELDS[kind]} (чем меньше %, тем реже):",
        reply_markup=kb.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.regexp(r"^rar:\w+:\d+$"))
async def cb_rarity_set(call: CallbackQuery) -> None:
    _, kind, value = call.data.split(":")
    f = await db.get_filters(call.from_user.id)
    setattr(f, f"max_{kind}_rarity", int(value) or None)
    await db.set_filters(call.from_user.id, f)
    await show_filters(call, call.from_user.id)


# ---------- фильтры: коллекции ----------


async def collections_kb(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    f = await db.get_filters(user_id)
    rows = await db.collections()
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = InlineKeyboardBuilder()
    for r in rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        kb.button(
            text=f"{_flag(r['title'] in f.collections)} {r['title']}",
            callback_data=f"col:t:{r['gift_id']}:{page}",
        )
    kb.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"col:p:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"col:p:{page + 1}"))
    kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text="🌐 Все коллекции", callback_data=f"col:all:{page}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back"),
    )
    selected = ", ".join(f.collections) if f.collections else "все"
    text = f"🎁 Выберите коллекции (сейчас: {html.escape(selected)})"
    if not rows:
        text += "\n\nСписок коллекций ещё загружается, загляните через минуту."
    return text, kb.as_markup()


@router.callback_query(F.data.startswith("col:"))
async def cb_collections(call: CallbackQuery) -> None:
    parts = call.data.split(":")
    action, page = parts[1], int(parts[-1])
    if action in ("t", "all"):
        f = await db.get_filters(call.from_user.id)
        if action == "all":
            f.collections = []
        else:
            titles = {r["gift_id"]: r["title"] for r in await db.collections()}
            title = titles.get(int(parts[2]))
            if title in f.collections:
                f.collections.remove(title)
            elif title:
                f.collections.append(title)
        await db.set_filters(call.from_user.id, f)
    text, kb = await collections_kb(call.from_user.id, page)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# ---------- фильтры: текстовый ввод ----------


@router.callback_query(F.data.startswith("txt:"))
async def cb_text(call: CallbackQuery, state: FSMContext) -> None:
    field = call.data.split(":", 1)[1]
    await state.set_state(Edit.waiting)
    await state.update_data(field=field)
    kb = InlineKeyboardBuilder()
    kb.button(text="🧹 Очистить", callback_data=f"clr:{field}")
    kb.button(text="⬅️ Назад", callback_data="back")
    await call.message.edit_text(TEXT_FIELDS[field], reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("clr:"))
async def cb_clear(call: CallbackQuery, state: FSMContext) -> None:
    field = call.data.split(":", 1)[1]
    f = await db.get_filters(call.from_user.id)
    setattr(f, field, None if field == "max_num" else [])
    await db.set_filters(call.from_user.id, f)
    await state.clear()
    await show_filters(call, call.from_user.id)


@router.message(Edit.waiting, F.text)
async def on_text(message: Message, state: FSMContext) -> None:
    field = (await state.get_data())["field"]
    f = await db.get_filters(message.from_user.id)
    if field == "max_num":
        if not message.text.strip().isdigit():
            await message.answer("Нужно целое число, попробуйте ещё раз.")
            return
        f.max_num = int(message.text.strip()) or None
    else:
        setattr(f, field, [x.strip() for x in message.text.split(",") if x.strip()])
    await db.set_filters(message.from_user.id, f)
    await state.clear()
    await show_filters(message, message.from_user.id)


# ---------- рассылка ----------


class Notifier:
    def __init__(self, bot: Bot, database: Database):
        self.bot = bot
        self.db = database

    async def __call__(self, u: Upgrade) -> None:
        text = format_upgrade(u)
        preview = LinkPreviewOptions(url=u.link, prefer_large_media=True)
        for user_id, f in await self.db.active_subscribers():
            if not matches(f, u):
                continue
            await self._send(user_id, text, preview, upgrade_kb(u))

    async def _send(self, user_id, text, preview, kb) -> None:
        for _ in range(3):
            try:
                await self.bot.send_message(user_id, text, link_preview_options=preview, reply_markup=kb)
                await asyncio.sleep(0.04)  # ~25 сообщений/с — в пределах лимитов Bot API
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                await self.db.set_active(user_id, False)
                return
            except Exception:
                log.exception("Не удалось отправить %s", user_id)
                return
