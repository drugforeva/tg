"""Главное меню, онбординг, общие настройки чата и кнопки под уведомлениями."""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import config
from ..db import Database
from ..filters import fmt_one_in, fmt_permille
from ..models import Filters, Subscription
from ..render import esc
from .common import PREMIUM_HINT, can_manage, chat_of, flag, is_premium, premium_enabled, show, subscription_limit

router = Router(name="menu")

WINDOW_OPTIONS = {0: "мгновенно", 60: "раз в минуту", 300: "раз в 5 минут", 900: "раз в 15 минут", 3600: "раз в час"}
DAILY_OPTIONS = [None, 8, 10, 12, 15, 18, 21, 23]
PRESETS = {
    "rare": ("💎 Редкие модели", Filters(max_model_rarity=10)),
    "pretty": ("🔢 Красивые номера", Filters(pretty_num=True)),
    "sale": ("💰 Сразу на продаже", Filters(on_sale=True)),
    "snipe": ("🎯 Снайпер: дешевле флора", Filters(events=["listing"], min_discount=15)),
    "all": ("🌐 Все подряд", Filters()),
    "empty": ("⚙️ Настрою сам", Filters()),
}
PREMIUM_PRESETS = {"snipe"}

WELCOME = (
    "👋 Я слежу за подарками Telegram:\n"
    "• 🆕 присылаю подарки, которые <b>только что улучшили</b> до уникальных;\n"
    "• 🏷 ловлю новые лоты на маркете и те, что <b>дешевле флора</b>;\n"
    "• 👁 слежу за конкретными подарками и людьми.\n\n"
    "Настройки собраны в <b>подписки</b>: у каждой свои фильтры, их можно держать несколько. "
    "С чего начнём?"
)

HELP = (
    "<b>Команды</b>\n"
    "/menu — подписки и настройки\n"
    "/recent [N] — последнее по вашим подпискам\n"
    "/top — самые редкие улучшения за сутки\n"
    "/deals — самые выгодные лоты за сутки\n"
    "/collection [название] — статистика и график коллекции\n"
    "/watch ссылка — следить за подарком, /watches — список\n"
    "/connect @канал — присылать подписку в свой канал, /channels — список\n"
    "/daily — ежедневная сводка\n"
    "/stats — активность коллекций\n"
    "/premium — премиум\n"
    "/pause, /resume — пауза уведомлений\n\n"
    "Пришлите ссылку вида t.me/nft/PlushPepe-123 — покажу всё о подарке.\n"
    "В любом чате можно набрать <code>@имя_бота pepe</code> и поделиться находкой."
)


def sub_summary(sub: Subscription) -> str:
    f = sub.filters
    bits = []
    if "listing" in f.events:
        bits.append("🏷 продажи" + (" + улучшения" if "upgrade" in f.events else ""))
    if f.collections:
        bits.append(", ".join(f.collections[:3]) + ("…" if len(f.collections) > 3 else ""))
    for label, values in (("модели", f.models), ("фоны", f.backdrops), ("узоры", f.symbols), ("люди", f.owners)):
        if values:
            bits.append(f"{label}: {len(values)}")
    if f.max_model_rarity:
        bits.append(f"модель ≤ {fmt_permille(f.max_model_rarity)}")
    if f.min_one_in:
        bits.append(fmt_one_in(f.min_one_in))
    if f.pretty_num:
        bits.append("красивые номера")
    if f.numbers:
        bits.append(f"номера: {len(f.numbers)}")
    if f.max_num:
        bits.append(f"№ ≤ {f.max_num}")
    if f.min_discount:
        bits.append(f"−{f.min_discount}% от флора")
    if f.max_price:
        bits.append(f"до {f.max_price} ⭐")
    elif f.on_sale:
        bits.append("на продаже")
    if f.with_username:
        bits.append("с @username")
    if sub.target_id:
        bits.append("📢 в канал")
    return "; ".join(bits) or "все улучшения"


async def menu_view(db: Database, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    subs = await db.subscriptions(chat_id)
    active = await db.is_active(chat_id)
    window = await db.get_window(chat_id)
    daily = await db.get_daily(chat_id)
    tracked = await db.tracked_of(chat_id)
    lines = [
        "⚙️ <b>Меню</b>",
        "",
        f"Уведомления: {'🔔 включены' if active else '🔕 на паузе'} · ⏱ {WINDOW_OPTIONS.get(window, f'{window} с')}",
    ]
    if premium_enabled():
        lines.append("⭐ Премиум активен" if await is_premium(db, chat_id) else "Тариф: бесплатный · /premium")
    lines.append("")
    if subs:
        lines.append("<b>Подписки</b> — придёт всё, что подходит хотя бы под одну:")
        lines += [f"{flag(s.enabled)} <b>{esc(s.name)}</b> — {esc(sub_summary(s))}" for s in subs]
    else:
        lines.append("Подписок пока нет — создайте первую.")
    kb = InlineKeyboardBuilder()
    for s in subs:
        kb.button(text=f"{flag(s.enabled)} {s.name}", callback_data=f"s:{s.id}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="➕ Новая подписка", callback_data="new"))
    kb.row(
        InlineKeyboardButton(text=f"⏱ {WINDOW_OPTIONS.get(window, 'частота')}", callback_data="win"),
        InlineKeyboardButton(text="🔕 Пауза" if active else "🔔 Включить", callback_data="pause"),
    )
    kb.row(
        InlineKeyboardButton(
            text=f"📰 Сводка: {f'{daily}:00' if daily is not None else 'выкл'}", callback_data="daily"
        ),
        InlineKeyboardButton(text=f"👁 Слежка ({len(tracked)})", callback_data="tw:list"),
    )
    kb.row(
        InlineKeyboardButton(text="📜 Последние", callback_data="recent"),
        InlineKeyboardButton(text="🏆 Топ 24ч", callback_data="top"),
        InlineKeyboardButton(text="🔥 Скидки", callback_data="deals"),
    )
    kb.row(
        InlineKeyboardButton(text="📈 Коллекции", callback_data="col:list:0"),
        InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
        InlineKeyboardButton(text="📢 Каналы", callback_data="ch:list"),
    )
    if premium_enabled():
        kb.row(InlineKeyboardButton(text="⭐ Премиум", callback_data="premium"))
    return "\n".join(lines), kb.as_markup()


async def show_menu(target: Message | CallbackQuery, db: Database) -> None:
    await show(target, *await menu_view(db, chat_of(target).id))


def presets_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (title, _) in PRESETS.items():
        kb.button(text=title, callback_data=f"pre:{key}")
    kb.adjust(2)
    return kb.as_markup()


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database) -> None:
    await db.ensure_user(message.chat.id)
    if command.args == "premium" and premium_enabled():
        from .premium import send_invoice

        await send_invoice(message, db)
        return
    if await db.subscriptions(message.chat.id):
        await show_menu(message, db)
    else:
        await message.answer(WELCOME, reply_markup=presets_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("menu", "filters"))
async def cmd_menu(message: Message, db: Database) -> None:
    await db.ensure_user(message.chat.id)
    await show_menu(message, db)


@router.message(Command("pause", "resume"))
async def cmd_pause(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    if not await can_manage(bot, message.chat, message.from_user.id):
        return
    await db.ensure_user(message.chat.id)
    active = command.command == "resume"
    await db.set_active(message.chat.id, active)
    await message.answer("🔔 Уведомления включены." if active else "🔕 Уведомления на паузе. /resume — включить.")


@router.message(Command("daily"))
async def cmd_daily(message: Message, db: Database) -> None:
    await db.ensure_user(message.chat.id)
    text, kb = daily_view(await db.get_daily(message.chat.id))
    await message.answer(text, reply_markup=kb)


def daily_view(current: int | None) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    for hour in DAILY_OPTIONS:
        label = "Выключить" if hour is None else f"{hour}:00"
        kb.button(
            text=f"{'🔘' if hour == current else '⚪️'} {label}", callback_data=f"daily:{-1 if hour is None else hour}"
        )
    kb.adjust(4)
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"))
    text = (
        "📰 <b>Ежедневная сводка</b>\n\nРаз в день: сколько было улучшений и лотов, самые активные коллекции, "
        f"самые редкие улучшения и самые выгодные лоты. Время — {config.timezone}."
    )
    return text, kb.as_markup()


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await show_menu(call, db)


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(
    F.data.in_({"new", "win", "pause", "daily"}) | F.data.startswith(("pre:", "win:", "daily:", "mute:", "unmute:"))
)
async def cb_settings(call: CallbackQuery, db: Database, bot: Bot) -> None:
    chat_id = call.message.chat.id
    if not await can_manage(bot, call.message.chat, call.from_user.id):
        await call.answer("Настройки меняют только админы чата", show_alert=True)
        return
    await db.ensure_user(chat_id)
    action, _, arg = call.data.partition(":")

    if action == "new":
        await show(call, "Какую подписку создать?", presets_kb())
    elif action == "pre":
        if len(await db.subscriptions(chat_id)) >= await subscription_limit(db, chat_id):
            await call.answer(f"Больше подписок нельзя. {PREMIUM_HINT if premium_enabled() else ''}", show_alert=True)
            return
        if arg in PREMIUM_PRESETS and not await is_premium(db, chat_id):
            await call.answer(PREMIUM_HINT, show_alert=True)
            return
        title, preset = PRESETS[arg]
        name = "Основная" if arg == "empty" else title.split(" ", 1)[1]
        sub_id = await db.create_subscription(chat_id, name, Filters.from_dict(preset.to_dict()))
        from .subscription import show_subscription

        await show_subscription(call, await db.get_subscription(sub_id))
    elif action == "win":
        if arg:
            await db.set_window(chat_id, int(arg))
            await show_menu(call, db)
            return
        current = await db.get_window(chat_id)
        kb = InlineKeyboardBuilder()
        for seconds, label in WINDOW_OPTIONS.items():
            kb.button(text=f"{'🔘' if seconds == current else '⚪️'} {label}", callback_data=f"win:{seconds}")
        kb.button(text="⬅️ Назад", callback_data="menu")
        kb.adjust(1)
        await show(
            call,
            "⏱ <b>Как часто присылать?</b>\n\n«Мгновенно» — сразу по одному. Если подарков "
            "много, они всё равно соберутся в сводку. С интервалом — не чаще одного "
            "сообщения за период, остальное придёт сводкой.",
            kb.as_markup(),
        )
    elif action == "daily":
        if arg:
            hour = int(arg)
            await db.set_daily(chat_id, None if hour < 0 else hour)
            await show_menu(call, db)
            return
        await show(call, *daily_view(await db.get_daily(chat_id)))
    elif action == "pause":
        await db.set_active(chat_id, not await db.is_active(chat_id))
        await show_menu(call, db)
    elif action in ("mute", "unmute"):
        await toggle_mute(call, db, int(arg), mute=action == "mute")


async def toggle_mute(call: CallbackQuery, db: Database, gift_id: int, mute: bool) -> None:
    row = await db.collection(gift_id)
    if not row:
        await call.answer("Коллекция не найдена")
        return
    title = row["title"]
    for sub in await db.subscriptions(call.message.chat.id):
        excluded = sub.filters.exclude_collections
        if mute and title not in excluded:
            excluded.append(title)
        elif not mute and title in excluded:
            excluded.remove(title)
        await db.save_subscription(sub)
    rows = list(call.message.reply_markup.inline_keyboard[:-1]) if call.message.reply_markup else []
    toggle = (
        InlineKeyboardButton(text=f"↩️ Снова присылать {title}"[:60], callback_data=f"unmute:{gift_id}")
        if mute
        else InlineKeyboardButton(text=f"🔕 Не присылать {title}"[:60], callback_data=f"mute:{gift_id}")
    )
    try:
        await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[*rows, [toggle]]))
    except TelegramBadRequest:
        pass
    await call.answer(f"{title}: {'больше не присылаю' if mute else 'снова присылаю'}")
