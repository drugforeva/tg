"""Слежка за подарками и людьми, просмотр подарка по ссылке."""

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database
from ..models import Filters
from ..parsing import parse_slug, parse_unique
from ..render import esc, upgrade_card
from ..watcher import fetch_unique
from .common import PREMIUM_HINT, App, can_manage, is_premium, premium_enabled, show, subscription_limit, tracked_limit

router = Router(name="watch")
PEOPLE_SUBSCRIPTION = "👤 Люди"


async def add_tracked(db: Database, chat_id: int, slug: str) -> str:
    await db.ensure_user(chat_id)
    tracked = await db.tracked_of(chat_id)
    if slug in tracked:
        return f"Уже слежу за {slug}."
    if len(tracked) >= await tracked_limit(db, chat_id):
        return "Достигнут лимит отслеживаемых подарков." + (f" {PREMIUM_HINT}" if premium_enabled() else "")
    await db.add_tracked(chat_id, slug)
    return f"👁 Слежу за {slug}: пришлю, если сменится владелец или цена, подарок выставят или снимут с продажи."


async def watches_view(db: Database, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    slugs = await db.tracked_of(chat_id)
    kb = InlineKeyboardBuilder()
    for slug in slugs:
        kb.row(
            InlineKeyboardButton(text=f"🎁 {slug}", url=f"https://t.me/nft/{slug}"),
            InlineKeyboardButton(text="✖️", callback_data=f"tw:del:{slug}"[:64]),
        )
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"))
    limit = await tracked_limit(db, chat_id)
    text = f"👁 <b>Слежка за подарками</b> ({len(slugs)}/{limit})\n\n"
    if not slugs:
        text += "Пока ни за чем не слежу. Пришлите <code>/watch t.me/nft/PlushPepe-123</code> или нажмите «👁 Следить» под уведомлением."
    else:
        text += "Пришлю, если у подарка сменится владелец или цена, его выставят или снимут с продажи."
    return text, kb.as_markup()


@router.message(Command("watch"))
async def cmd_watch(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    if not await can_manage(bot, message.chat, message.from_user.id):
        return
    slug = parse_slug(command.args or "")
    if not slug:
        await message.answer("Пришлите ссылку на подарок: <code>/watch https://t.me/nft/PlushPepe-123</code>")
        return
    await message.answer(await add_tracked(db, message.chat.id, slug))


@router.message(Command("watches"))
async def cmd_watches(message: Message, db: Database) -> None:
    await db.ensure_user(message.chat.id)
    text, kb = await watches_view(db, message.chat.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("tw:"))
async def cb_watch(call: CallbackQuery, db: Database, bot: Bot) -> None:
    action, _, slug = call.data[3:].partition(":")
    chat_id = call.message.chat.id
    if action == "list":
        await db.ensure_user(chat_id)
        await show(call, *await watches_view(db, chat_id))
        return
    if not await can_manage(bot, call.message.chat, call.from_user.id):
        await call.answer("Настройки меняют только админы чата", show_alert=True)
        return
    if action == "add":
        await call.answer(await add_tracked(db, chat_id, slug), show_alert=True)
    elif action == "del":
        await db.remove_tracked(chat_id, slug)
        if call.message.text and call.message.text.startswith("👁 Слежка"):
            await show(call, *await watches_view(db, chat_id))
        else:
            await call.answer(f"Больше не слежу за {slug}")


@router.callback_query(F.data.startswith("wo:"))
async def cb_watch_owner(call: CallbackQuery, db: Database, bot: Bot) -> None:
    """Следить за человеком: добавляет его в подписку «👤 Люди» (создаёт её при необходимости)."""
    if not await can_manage(bot, call.message.chat, call.from_user.id):
        await call.answer("Настройки меняют только админы чата", show_alert=True)
        return
    chat_id, username = call.message.chat.id, call.data[3:]
    await db.ensure_user(chat_id)
    subs = await db.subscriptions(chat_id)
    sub = next((s for s in subs if s.name == PEOPLE_SUBSCRIPTION), None)
    if sub is None:
        if len(subs) >= await subscription_limit(db, chat_id):
            await call.answer(f"Больше подписок нельзя. {PREMIUM_HINT if premium_enabled() else ''}", show_alert=True)
            return
        events = ["upgrade", "listing"] if await is_premium(db, chat_id) else ["upgrade"]
        await db.create_subscription(chat_id, PEOPLE_SUBSCRIPTION, Filters(events=events, owners=[username]))
    elif username.casefold() not in {o.casefold() for o in sub.filters.owners}:
        sub.filters.owners.append(username)
        await db.save_subscription(sub)
    await call.answer(
        f"👤 Слежу за @{username}: пришлю его улучшения и лоты. Список — в подписке «Люди».", show_alert=True
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(r"nft/[A-Za-z0-9]+-\d+", mode="search"))
async def on_gift_link(message: Message, db: Database, app: App) -> None:
    """Пользователь прислал ссылку на подарок — показываем карточку."""
    slug = parse_slug(message.text)
    if not slug:
        return
    resp = await fetch_unique(app.watcher.call, slug)
    if resp is None:
        await message.answer(f"Не нашёл подарок {esc(slug)}.")
        return
    u = parse_unique(resp, resp.gift.title)
    if app.market is not None:
        u.floor_stars = app.market.floor_of(u.gift_id)
    text = upgrade_card(u).replace("🆕 ", "🎁 ", 1)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Открыть", url=u.link)],
            [InlineKeyboardButton(text="👁 Следить за подарком", callback_data=f"tw:add:{u.slug}"[:64])],
        ]
    )
    await message.answer(
        text, reply_markup=kb, link_preview_options=LinkPreviewOptions(url=u.link, prefer_large_media=True)
    )
