"""Интерфейс бота: подписки с фильтрами, выбор атрибутов, история и статистика.

Зависимости (db, watcher, delivery) приходят в хендлеры из workflow_data диспетчера.
Callback-данные подписки имеют вид s:<id>:<действие>:<аргументы>.
"""

import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import config
from .db import Database
from .delivery import Delivery
from .filters import describe, fmt_one_in, fmt_permille, matches
from .models import Filters, Subscription
from .render import esc, upgrade_list
from .watcher import Watcher

log = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 20
MAX_SUBSCRIPTIONS = 10
# Больше стольких вариантов списком не показываем — только ввод текстом.
MAX_PICKER_OPTIONS = 400
RARITY_OPTIONS = [None, 50, 20, 10, 5, 2]  # промилле: любая, 5%, 2%, 1%, 0.5%, 0.2%
ONE_IN_OPTIONS = [None, 1_000_000, 10_000_000, 50_000_000, 200_000_000]
WINDOW_OPTIONS = {0: "мгновенно", 60: "раз в минуту", 300: "раз в 5 минут", 900: "раз в 15 минут", 3600: "раз в час"}
KINDS = {
    # kind: (эмодзи, именительный, родительный падеж, поле со списком, поле с редкостью)
    "model": ("🧩", "Модель", "модели", "models", "max_model_rarity"),
    "backdrop": ("🎨", "Фон", "фона", "backdrops", "max_backdrop_rarity"),
    "symbol": ("🔣", "Узор", "узора", "symbols", "max_symbol_rarity"),
}
NAMES_HINT = "Через запятую, регистр не важен, * — любой текст (например <code>*Pepe*</code>)."
TEXT_PROMPTS = {
    "models": f"🧩 Пришлите названия моделей. {NAMES_HINT}",
    "backdrops": f"🎨 Пришлите названия фонов. {NAMES_HINT}",
    "symbols": f"🔣 Пришлите названия узоров. {NAMES_HINT}",
    "max_num": "🔢 Пришлите максимальный номер подарка (например 1000).",
    "max_price": "💰 Пришлите максимальную цену в звёздах (например 5000). Будут приходить только подарки на продаже не дороже этой цены.",
    "name": "✏️ Пришлите новое название подписки.",
}
TOGGLES = {"pretty_num", "with_username", "on_sale"}
PRESETS = {
    "rare": ("💎 Редкие модели", Filters(max_model_rarity=10)),
    "pretty": ("🔢 Красивые номера", Filters(pretty_num=True)),
    "sale": ("💰 Сразу на продаже", Filters(on_sale=True)),
    "all": ("🌐 Все подряд", Filters()),
    "empty": ("⚙️ Настрою сам", Filters()),
}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class Edit(StatesGroup):
    waiting = State()


# ---------- общие помощники ----------


def _flag(on: bool) -> str:
    return "✅" if on else "▫️"


async def can_manage(bot: Bot, chat, user_id: int) -> bool:
    """В группах настройки меняют только админы."""
    if chat.type == ChatType.PRIVATE:
        return True
    member = await bot.get_chat_member(chat.id, user_id)
    return member.status in ("creator", "administrator")


async def show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение с меню (для кнопок) или отправляет новое (для команд)."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


def _sub_summary(sub: Subscription) -> str:
    f = sub.filters
    bits = []
    if f.collections:
        bits.append(", ".join(f.collections[:3]) + ("…" if len(f.collections) > 3 else ""))
    for label, values in (("модели", f.models), ("фоны", f.backdrops), ("узоры", f.symbols)):
        if values:
            bits.append(f"{label}: {len(values)}")
    if f.max_model_rarity:
        bits.append(f"модель ≤ {fmt_permille(f.max_model_rarity)}")
    if f.min_one_in:
        bits.append(fmt_one_in(f.min_one_in))
    if f.pretty_num:
        bits.append("красивые номера")
    if f.max_num:
        bits.append(f"№ ≤ {f.max_num}")
    if f.max_price:
        bits.append(f"до {f.max_price} ⭐")
    elif f.on_sale:
        bits.append("на продаже")
    if f.with_username:
        bits.append("с @username")
    return "; ".join(bits) or "все подарки"


# ---------- главное меню ----------


async def menu_view(db: Database, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    subs = await db.subscriptions(chat_id)
    active = await db.is_active(chat_id)
    window = await db.get_window(chat_id)
    lines = [
        "⚙️ <b>Меню</b>",
        "",
        f"Уведомления: {'🔔 включены' if active else '🔕 на паузе'} · ⏱ {WINDOW_OPTIONS.get(window, f'{window} с')}",
        "",
    ]
    if subs:
        lines.append("<b>Подписки</b> — придёт всё, что подходит хотя бы под одну:")
        lines += [f"{_flag(s.enabled)} <b>{esc(s.name)}</b> — {esc(_sub_summary(s))}" for s in subs]
    else:
        lines.append("Подписок пока нет — создайте первую.")
    kb = InlineKeyboardBuilder()
    for s in subs:
        kb.button(text=f"{_flag(s.enabled)} {s.name}", callback_data=f"s:{s.id}")
    kb.adjust(2)
    if len(subs) < MAX_SUBSCRIPTIONS:
        kb.row(InlineKeyboardButton(text="➕ Новая подписка", callback_data="new"))
    kb.row(
        InlineKeyboardButton(text=f"⏱ {WINDOW_OPTIONS.get(window, 'частота')}", callback_data="win"),
        InlineKeyboardButton(text="🔕 Пауза" if active else "🔔 Включить", callback_data="pause"),
    )
    kb.row(
        InlineKeyboardButton(text="📜 Последние", callback_data="recent"),
        InlineKeyboardButton(text="🏆 Топ 24ч", callback_data="top"),
        InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
    )
    return "\n".join(lines), kb.as_markup()


async def show_menu(target: Message | CallbackQuery, db: Database) -> None:
    chat = target.message.chat if isinstance(target, CallbackQuery) else target.chat
    await show(target, *await menu_view(db, chat.id))


def presets_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (title, _) in PRESETS.items():
        kb.button(text=title, callback_data=f"pre:{key}")
    kb.adjust(2)
    return kb.as_markup()


WELCOME = (
    "👋 Я присылаю подарки Telegram, которые <b>только что улучшили</b> до уникальных: "
    "модель, фон, узор, редкость, номер, владелец и цена, если подарок сразу выставили на продажу.\n\n"
    "Настройки собраны в <b>подписки</b>: у каждой свои фильтры, их можно держать несколько. "
    "С чего начнём?"
)


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database) -> None:
    await db.ensure_user(message.chat.id)
    if await db.subscriptions(message.chat.id):
        await show_menu(message, db)
    else:
        await message.answer(WELCOME, reply_markup=presets_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "/menu — подписки и настройки\n"
        "/recent [N] — последние улучшения по вашим подпискам\n"
        "/top — самые редкие улучшения за сутки\n"
        "/stats — активность коллекций\n"
        "/pause, /resume — пауза уведомлений\n\n"
        "Под каждым уведомлением есть кнопка, чтобы больше не получать эту коллекцию."
    )


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


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext, db: Database) -> None:
    await state.clear()
    await show_menu(call, db)


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data.in_({"new", "win", "pause"}) | F.data.startswith(("pre:", "win:", "mute:", "unmute:")))
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
        if len(await db.subscriptions(chat_id)) >= MAX_SUBSCRIPTIONS:
            await call.answer("Слишком много подписок", show_alert=True)
            return
        title, preset = PRESETS[arg]
        name = "Основная" if arg == "empty" else title.split(" ", 1)[1]
        sub_id = await db.create_subscription(chat_id, name, Filters.from_dict(preset.to_dict()))
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
    elif action == "pause":
        await db.set_active(chat_id, not await db.is_active(chat_id))
        await show_menu(call, db)
    elif action in ("mute", "unmute"):
        await toggle_mute(call, db, int(arg), mute=action == "mute")


async def toggle_mute(call: CallbackQuery, db: Database, gift_id: int, mute: bool) -> None:
    titles = {r["gift_id"]: r["title"] for r in await db.collections(enabled_only=False)}
    title = titles.get(gift_id)
    if not title:
        await call.answer("Коллекция не найдена")
        return
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


# ---------- редактор подписки ----------


def subscription_kb(sub: Subscription) -> InlineKeyboardMarkup:
    f, p = sub.filters, f"s:{sub.id}"

    def rar(limit: int | None) -> str:
        return f"≤{fmt_permille(limit)}" if limit else "любая"

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=f"🎁 Коллекции ({len(f.collections) or 'все'})", callback_data=f"{p}:c:i:0"),
        InlineKeyboardButton(text=f"🚫 Кроме ({len(f.exclude_collections)})", callback_data=f"{p}:c:x:0"),
    )
    kb.row(
        *(
            InlineKeyboardButton(
                text=f"{emoji} {title}" + (f" ({len(getattr(f, field))})" if getattr(f, field) else ""),
                callback_data=f"{p}:a:{kind}:0",
            )
            for kind, (emoji, title, _, field, _) in KINDS.items()
        )
    )
    kb.row(
        *(
            InlineKeyboardButton(text=f"⭐ {title.lower()} {rar(getattr(f, rfield))}", callback_data=f"{p}:r:{kind}")
            for kind, (_, title, _, _, rfield) in KINDS.items()
        )
    )
    kb.row(
        InlineKeyboardButton(
            text=f"💎 Сочетание: {fmt_one_in(f.min_one_in) if f.min_one_in else 'любое'}", callback_data=f"{p}:o"
        )
    )
    kb.row(
        InlineKeyboardButton(text=f"🔢 № ≤ {f.max_num or '∞'}", callback_data=f"{p}:i:max_num"),
        InlineKeyboardButton(text=f"{_flag(f.pretty_num)} Красивые №", callback_data=f"{p}:t:pretty_num"),
    )
    kb.row(
        InlineKeyboardButton(text=f"{_flag(f.on_sale)} На продаже", callback_data=f"{p}:t:on_sale"),
        InlineKeyboardButton(text=f"💰 ≤ {f.max_price or '∞'} ⭐", callback_data=f"{p}:i:max_price"),
    )
    kb.row(
        InlineKeyboardButton(
            text=f"{_flag(f.with_username)} Владелец с @username", callback_data=f"{p}:t:with_username"
        )
    )
    kb.row(
        InlineKeyboardButton(text="⏸ Выключить" if sub.enabled else "▶️ Включить", callback_data=f"{p}:en"),
        InlineKeyboardButton(text="✏️ Имя", callback_data=f"{p}:i:name"),
        InlineKeyboardButton(text="♻️ Сброс", callback_data=f"{p}:reset"),
        InlineKeyboardButton(text="🗑", callback_data=f"{p}:del"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"))
    return kb.as_markup()


async def show_subscription(target: Message | CallbackQuery, sub: Subscription) -> None:
    status = "включена" if sub.enabled else "⏸ выключена"
    text = (
        f"📌 <b>{esc(sub.name)}</b> ({status})\n\n{esc(describe(sub.filters))}\n\n"
        "<i>⭐ — максимальная редкость: чем меньше процент, тем реже.</i>"
    )
    await show(target, text, subscription_kb(sub))


def _back(sub: Subscription) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=f"s:{sub.id}")


def _nav(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page + 1}"))
    return nav


async def collections_screen(call: CallbackQuery, db: Database, sub: Subscription, mode: str, page: int) -> None:
    selected = sub.filters.collections if mode == "i" else sub.filters.exclude_collections
    rows = await db.collections()
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    kb = InlineKeyboardBuilder()
    for r in rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        kb.button(
            text=f"{_flag(r['title'] in selected)} {r['title']}",
            callback_data=f"s:{sub.id}:ct:{mode}:{r['gift_id']}:{page}",
        )
    kb.adjust(2)
    kb.row(*_nav(f"s:{sub.id}:c:{mode}", page, pages))
    kb.row(InlineKeyboardButton(text="🧹 Очистить", callback_data=f"s:{sub.id}:cc:{mode}:{page}"), _back(sub))
    if mode == "i":
        text = "🎁 <b>Какие коллекции присылать?</b> Ничего не выбрано — все."
    else:
        text = "🚫 <b>Какие коллекции не присылать?</b>"
    if selected:
        text += f"\n\nВыбрано: {esc(', '.join(selected))}"
    if not rows:
        text += "\n\nСписок коллекций ещё загружается, загляните через минуту."
    await show(call, text, kb.as_markup())


async def _gift_ids(db: Database, f: Filters) -> list[int] | None:
    if not f.collections:
        return None
    return [r["gift_id"] for r in await db.collections() if r["title"] in f.collections]


async def attributes_screen(
    call: CallbackQuery, state: FSMContext, db: Database, sub: Subscription, kind: str, page: int
) -> None:
    emoji, _, genitive, field, _ = KINDS[kind]
    selected = getattr(sub.filters, field)
    options = await db.attribute_options(kind, await _gift_ids(db, sub.filters))
    if not options or len(options) > MAX_PICKER_OPTIONS:
        # Списком не показать — сразу просим ввести текстом.
        await text_input(
            call, state, sub, field, extra="Чтобы выбирать из списка, оставьте в подписке одну-две коллекции."
        )
        return
    pages = max(1, (len(options) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chosen = {s.casefold() for s in selected}
    kb = InlineKeyboardBuilder()
    for attr_id, name, rarity in options[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        kb.button(
            text=f"{_flag(name.casefold() in chosen)} {name} · {fmt_permille(rarity)}",
            callback_data=f"s:{sub.id}:at:{kind}:{attr_id}:{page}",
        )
    kb.adjust(2)
    kb.row(*_nav(f"s:{sub.id}:a:{kind}", page, pages))
    kb.row(
        InlineKeyboardButton(text="⌨️ Ввести", callback_data=f"s:{sub.id}:i:{field}"),
        InlineKeyboardButton(text="🧹 Очистить", callback_data=f"s:{sub.id}:ic:{field}"),
        _back(sub),
    )
    text = f"{emoji} <b>Выбор {genitive}</b> (редкие сверху). Ничего не выбрано — любые."
    if selected:
        text += f"\n\nВыбрано: {esc(', '.join(selected))}"
    await show(call, text, kb.as_markup())


async def text_input(call: CallbackQuery, state: FSMContext, sub: Subscription, field: str, extra: str = "") -> None:
    await state.set_state(Edit.waiting)
    await state.update_data(sub_id=sub.id, field=field)
    current = getattr(sub.filters, field, None) if field != "name" else sub.name
    text = TEXT_PROMPTS[field]
    if call.message.chat.type != ChatType.PRIVATE:
        text += "\n\nВ группе ответьте на это сообщение (Reply), иначе бот его не увидит."
    if extra:
        text += f"\n\n{extra}"
    if current:
        shown = ", ".join(current) if isinstance(current, list) else str(current)
        text += f"\n\nСейчас: <code>{esc(shown)}</code>"
    kb = InlineKeyboardBuilder()
    if field != "name":
        kb.button(text="🧹 Очистить", callback_data=f"s:{sub.id}:ic:{field}")
    kb.add(_back(sub))
    await show(call, text, kb.as_markup())


def _choice_kb(sub: Subscription, prefix: str, options: list, current, label) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for value in options:
        kb.button(text=f"{'🔘' if value == current else '⚪️'} {label(value)}", callback_data=f"{prefix}:{value or 0}")
    kb.adjust(2)
    kb.row(_back(sub))
    return kb.as_markup()


@router.callback_query(F.data.startswith("s:"))
async def cb_subscription(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot) -> None:
    parts = call.data.split(":")
    sub = await db.get_subscription(int(parts[1]))
    if sub is None or sub.chat_id != call.message.chat.id:
        await call.answer("Подписка не найдена", show_alert=True)
        return
    if not await can_manage(bot, call.message.chat, call.from_user.id):
        await call.answer("Настройки меняют только админы чата", show_alert=True)
        return
    action, args = (parts[2] if len(parts) > 2 else ""), parts[3:]
    f = sub.filters
    await state.clear()

    if action == "":
        await show_subscription(call, sub)
    elif action == "c":
        await collections_screen(call, db, sub, args[0], int(args[1]))
    elif action in ("ct", "cc"):
        mode = args[0]
        selected = f.collections if mode == "i" else f.exclude_collections
        if action == "cc":
            selected.clear()
        else:
            titles = {r["gift_id"]: r["title"] for r in await db.collections()}
            title = titles.get(int(args[1]))
            if title in selected:
                selected.remove(title)
            elif title:
                selected.append(title)
        await db.save_subscription(sub)
        await collections_screen(call, db, sub, mode, int(args[-1]))
    elif action == "a":
        await attributes_screen(call, state, db, sub, args[0], int(args[1]))
    elif action == "at":
        kind, attr_id, page = args[0], int(args[1]), int(args[2])
        name = await db.attribute_name(kind, attr_id)
        selected = getattr(f, KINDS[kind][3])
        if name:
            existing = [s for s in selected if s.casefold() == name.casefold()]
            if existing:
                for s in existing:
                    selected.remove(s)
            else:
                selected.append(name)
            await db.save_subscription(sub)
        await attributes_screen(call, state, db, sub, kind, page)
    elif action == "r":
        kind = args[0]
        _, _, genitive, _, rfield = KINDS[kind]
        if len(args) > 1:
            setattr(f, rfield, int(args[1]) or None)
            await db.save_subscription(sub)
            await show_subscription(call, sub)
            return
        label = lambda v: "Любая" if v is None else f"≤ {fmt_permille(v)}"  # noqa: E731
        await show(
            call,
            f"⭐ <b>Максимальная редкость {genitive}</b>\nЧем меньше процент, тем реже встречается.",
            _choice_kb(sub, f"s:{sub.id}:r:{kind}", RARITY_OPTIONS, getattr(f, rfield), label),
        )
    elif action == "o":
        if args:
            f.min_one_in = int(args[0]) or None
            await db.save_subscription(sub)
            await show_subscription(call, sub)
            return
        label = lambda v: "Любое" if v is None else f"реже {fmt_one_in(v)}"  # noqa: E731
        await show(
            call,
            "💎 <b>Редкость сочетания</b>\nВероятность выпадения именно такой модели, фона и узора "
            "вместе (их редкости перемножаются).",
            _choice_kb(sub, f"s:{sub.id}:o", ONE_IN_OPTIONS, f.min_one_in, label),
        )
    elif action == "t" and args[0] in TOGGLES:
        setattr(f, args[0], not getattr(f, args[0]))
        await db.save_subscription(sub)
        await show_subscription(call, sub)
    elif action == "i" and args[0] in TEXT_PROMPTS:
        await text_input(call, state, sub, args[0])
    elif action == "ic":
        field = args[0]
        setattr(f, field, [] if isinstance(getattr(f, field), list) else None)
        await db.save_subscription(sub)
        await show_subscription(call, sub)
    elif action == "en":
        sub.enabled = not sub.enabled
        await db.save_subscription(sub)
        await show_subscription(call, sub)
    elif action == "reset":
        sub.filters = Filters()
        await db.save_subscription(sub)
        await show_subscription(call, sub)
    elif action == "del":
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Да, удалить", callback_data=f"s:{sub.id}:delok")
        kb.add(_back(sub))
        await show(call, f"Удалить подписку «{esc(sub.name)}»?", kb.as_markup())
    elif action == "delok":
        await db.delete_subscription(sub.id)
        await show_menu(call, db)
    else:
        await call.answer()


@router.message(Edit.waiting, F.text, ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    sub = await db.get_subscription(data["sub_id"])
    if sub is None or sub.chat_id != message.chat.id:
        await state.clear()
        return
    field, value = data["field"], message.text.strip()
    if field in ("max_num", "max_price"):
        digits = value.replace(" ", "")
        if not digits.isdigit():
            await message.answer("Нужно целое число, попробуйте ещё раз.")
            return
        setattr(sub.filters, field, int(digits) or None)
    elif field == "name":
        sub.name = value[:40]
    else:
        setattr(sub.filters, field, [x.strip() for x in value.split(",") if x.strip()])
    await db.save_subscription(sub)
    await state.clear()
    await show_subscription(message, sub)


# ---------- история и статистика ----------


async def recent_text(db: Database, chat_id: int, limit: int) -> str:
    subs = [s for s in await db.subscriptions(chat_id) if s.enabled]
    found = [u for u in await db.recent_upgrades(3000) if not subs or any(matches(s.filters, u) for s in subs)][:limit]
    if not found:
        return "Пока ничего не нашлось по вашим подпискам."
    return upgrade_list(f"📜 <b>Последние улучшения по вашим подпискам</b> ({len(found)})", found, limit)


async def top_text(db: Database) -> str:
    found = await db.rarest_upgrades(int(time.time()) - 86400, 15)
    if not found:
        return "За сутки пока нет данных."
    return upgrade_list("🏆 <b>Самые редкие сочетания за 24 часа</b>", found, 15)


def _ago(ts: float | None) -> str:
    if not ts:
        return "—"
    seconds = int(time.time() - ts)
    if seconds < 60:
        return f"{seconds} с назад"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч назад"


async def stats_text(db: Database, watcher: Watcher, delivery: Delivery, is_admin: bool) -> str:
    s = await db.stats()
    top = await db.top_collections(int(time.time()) - 86400, 10)
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"✨ Улучшений: {s['upgrades_1h']} за час, {s['upgrades_24h']} за сутки",
        f"🎁 Коллекций отслеживается: {s['collections']}",
    ]
    if top:
        lines += ["", "<b>Самые активные за сутки:</b>"]
        lines += [f"{i}. {esc(title)} — {count}" for i, (title, count) in enumerate(top, 1)]
    if is_admin:
        m = watcher.metrics
        uptime = int(time.time() - m.started_at)
        hot = sum(1 for st in watcher.states.values() if st.interval <= config.poll_min * 2)
        lines += [
            "",
            "<b>Служебное</b>",
            f"👥 Чатов: {s['users']} (активных {s['active']}), подписок: {s['subscriptions']}",
            f"⏱ Аптайм: {uptime // 3600} ч {uptime % 3600 // 60} мин",
            f"📡 Запросов: {m.requests}, скорость {watcher.limiter.rate:.2f}/с, FloodWait: {m.flood_waits}",
            f"🔥 Активных коллекций: {hot}",
            f"🆕 Найдено за запуск: {m.found}, последнее {_ago(m.last_found_at)}",
            f"✉️ Отправлено: {delivery.sent}, в очереди чатов: {len(delivery.pending)}",
        ]
        if m.fatal:
            lines.append(f"⛔️ Наблюдатель остановлен: {m.fatal}")
        if m.last_error:
            lines.append(f"⚠️ Последняя ошибка: {esc(m.last_error[:200])}")
    return "\n".join(lines)


def _back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]])


@router.message(Command("recent"))
async def cmd_recent(message: Message, command: CommandObject, db: Database) -> None:
    try:
        limit = max(1, min(int(command.args or 15), 30))
    except ValueError:
        limit = 15
    await message.answer(await recent_text(db, message.chat.id, limit), link_preview_options=NO_PREVIEW)


@router.message(Command("top"))
async def cmd_top(message: Message, db: Database) -> None:
    await message.answer(await top_text(db), link_preview_options=NO_PREVIEW)


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, watcher: Watcher, delivery: Delivery) -> None:
    is_admin = message.from_user.id in config.admins
    await message.answer(await stats_text(db, watcher, delivery, is_admin))


@router.callback_query(F.data.in_({"recent", "top", "stats"}))
async def cb_info(call: CallbackQuery, db: Database, watcher: Watcher, delivery: Delivery) -> None:
    if call.data == "recent":
        text = await recent_text(db, call.message.chat.id, 15)
    elif call.data == "top":
        text = await top_text(db)
    else:
        text = await stats_text(db, watcher, delivery, call.from_user.id in config.admins)
    await show(call, text, _back_to_menu())
