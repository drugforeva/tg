"""Редактор подписки. Callback-данные: s:<id>:<действие>:<аргументы>."""

import base64
import json
import zlib

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import config
from ..db import Database
from ..filters import describe, fmt_one_in, fmt_permille, parse_numbers
from ..models import Filters, Subscription
from ..render import esc
from .common import PREMIUM_HINT, Edit, can_manage, flag, is_premium, nav, paginate, show
from .menu import show_menu

router = Router(name="subscription")

# Больше стольких вариантов списком не показываем — только ввод текстом.
MAX_PICKER_OPTIONS = 400
RARITY_OPTIONS = [None, 50, 20, 10, 5, 2]  # промилле: любая, 5%, 2%, 1%, 0.5%, 0.2%
ONE_IN_OPTIONS = [None, 1_000_000, 10_000_000, 50_000_000, 200_000_000]
DISCOUNT_OPTIONS = [None, 5, 10, 15, 20, 30, 50]
KINDS = {
    # kind: (эмодзи, именительный, родительный падеж, поле, поле-исключение, поле редкости)
    "model": ("🧩", "Модель", "модели", "models", "exclude_models", "max_model_rarity"),
    "backdrop": ("🎨", "Фон", "фона", "backdrops", "exclude_backdrops", "max_backdrop_rarity"),
    "symbol": ("🔣", "Узор", "узора", "symbols", "exclude_symbols", "max_symbol_rarity"),
}
NAMES_HINT = "Через запятую, регистр не важен, * — любой текст (например <code>*Pepe*</code>)."
TEXT_PROMPTS = {
    "models": f"🧩 Пришлите названия моделей. {NAMES_HINT}",
    "backdrops": f"🎨 Пришлите названия фонов. {NAMES_HINT}",
    "symbols": f"🔣 Пришлите названия узоров. {NAMES_HINT}",
    "exclude_models": f"🚫🧩 Какие модели НЕ присылать? {NAMES_HINT}",
    "exclude_backdrops": f"🚫🎨 Какие фоны НЕ присылать? {NAMES_HINT}",
    "exclude_symbols": f"🚫🔣 Какие узоры НЕ присылать? {NAMES_HINT}",
    "max_num": "🔢 Пришлите максимальный номер подарка (например 1000).",
    "max_price": "💰 Пришлите максимальную цену в звёздах (например 5000). Будут приходить только подарки на продаже не дороже.",
    "numbers": "🔢 Пришлите свои номера и диапазоны через запятую: <code>1, 69, 777, 1000-1100</code>.",
    "owners": "👤 Пришлите @username или id людей через запятую. Придут их улучшения и лоты (как владельца или отправителя).",
    "name": "✏️ Пришлите новое название подписки.",
}
NUMBER_FIELDS = {"max_num", "max_price"}
TOGGLES = {"pretty_num", "with_username", "on_sale"}
EVENT_BUTTONS = {"upgrade": "🆕 Улучшения", "listing": "🏷 Продажи"}


def subscription_kb(sub: Subscription, private: bool) -> InlineKeyboardMarkup:
    f, p = sub.filters, f"s:{sub.id}"

    def rar(limit: int | None) -> str:
        return f"≤{fmt_permille(limit)}" if limit else "любая"

    def count(values: list) -> str:
        return f" ({len(values)})" if values else ""

    kb = InlineKeyboardBuilder()
    kb.row(
        *(
            InlineKeyboardButton(text=f"{flag(e in f.events)} {label}", callback_data=f"{p}:ev:{e}")
            for e, label in EVENT_BUTTONS.items()
        )
    )
    kb.row(
        InlineKeyboardButton(text=f"🎁 Коллекции ({len(f.collections) or 'все'})", callback_data=f"{p}:c:i:0"),
        InlineKeyboardButton(text=f"🚫 Кроме ({len(f.exclude_collections)})", callback_data=f"{p}:c:x:0"),
    )
    kb.row(
        *(
            InlineKeyboardButton(
                text=f"{emoji} {title}{count(getattr(f, field) + getattr(f, xfield))}",
                callback_data=f"{p}:a:{kind}:0:i",
            )
            for kind, (emoji, title, _, field, xfield, _) in KINDS.items()
        )
    )
    kb.row(
        *(
            InlineKeyboardButton(text=f"⭐ {title.lower()} {rar(getattr(f, rfield))}", callback_data=f"{p}:r:{kind}")
            for kind, (_, title, _, _, _, rfield) in KINDS.items()
        )
    )
    kb.row(
        InlineKeyboardButton(
            text=f"💎 {fmt_one_in(f.min_one_in) if f.min_one_in else 'Сочетание: любое'}", callback_data=f"{p}:o"
        ),
        InlineKeyboardButton(
            text=f"📉 Скидка {'≥ ' + str(f.min_discount) + '%' if f.min_discount else 'любая'}", callback_data=f"{p}:d"
        ),
    )
    kb.row(
        InlineKeyboardButton(text=f"🔢 № ≤ {f.max_num or '∞'}", callback_data=f"{p}:i:max_num"),
        InlineKeyboardButton(text=f"{flag(f.pretty_num)} Красивые", callback_data=f"{p}:t:pretty_num"),
        InlineKeyboardButton(text=f"🔢 Свои{count(f.numbers)}", callback_data=f"{p}:i:numbers"),
    )
    kb.row(
        InlineKeyboardButton(text=f"{flag(f.on_sale)} На продаже", callback_data=f"{p}:t:on_sale"),
        InlineKeyboardButton(text=f"💰 ≤ {f.max_price or '∞'} ⭐", callback_data=f"{p}:i:max_price"),
    )
    kb.row(
        InlineKeyboardButton(text=f"{flag(f.with_username)} С @username", callback_data=f"{p}:t:with_username"),
        InlineKeyboardButton(text=f"👤 Люди{count(f.owners)}", callback_data=f"{p}:i:owners"),
    )
    extra = [InlineKeyboardButton(text="📢 В канал" if not sub.target_id else "📢 Канал ✅", callback_data=f"{p}:dst")]
    if config.webapp_url and private:
        extra.append(InlineKeyboardButton(text="🖥 Редактор", callback_data=f"{p}:web"))
    kb.row(*extra)
    kb.row(
        InlineKeyboardButton(text="⏸ Выключить" if sub.enabled else "▶️ Включить", callback_data=f"{p}:en"),
        InlineKeyboardButton(text="✏️ Имя", callback_data=f"{p}:i:name"),
        InlineKeyboardButton(text="♻️ Сброс", callback_data=f"{p}:reset"),
        InlineKeyboardButton(text="🗑", callback_data=f"{p}:del"),
    )
    kb.row(InlineKeyboardButton(text="⬅️ В меню", callback_data="menu"))
    return kb.as_markup()


async def show_subscription(target: Message | CallbackQuery, sub: Subscription, db: Database | None = None) -> None:
    status = "включена" if sub.enabled else "⏸ выключена"
    text = f"📌 <b>{esc(sub.name)}</b> ({status})\n\n{esc(describe(sub.filters))}"
    if sub.target_id and db is not None:
        channels = {r["channel_id"]: r["title"] for r in await db.channels_of(sub.chat_id)}
        text += f"\n📢 Присылать в канал: {esc(channels.get(sub.target_id, str(sub.target_id)))}"
    text += "\n\n<i>⭐ — максимальная редкость: чем меньше процент, тем реже.</i>"
    chat = target.message.chat if isinstance(target, CallbackQuery) else target.chat
    await show(target, text, subscription_kb(sub, chat.type == ChatType.PRIVATE))


def back(sub: Subscription) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=f"s:{sub.id}")


async def collections_screen(call: CallbackQuery, db: Database, sub: Subscription, mode: str, page: int) -> None:
    selected = sub.filters.collections if mode == "i" else sub.filters.exclude_collections
    rows, page, pages = paginate(await db.collections(), page)
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(
            text=f"{flag(r['title'] in selected)} {r['title']}",
            callback_data=f"s:{sub.id}:ct:{mode}:{r['gift_id']}:{page}",
        )
    kb.adjust(2)
    kb.row(*nav(f"s:{sub.id}:c:{mode}", page, pages))
    kb.row(InlineKeyboardButton(text="🧹 Очистить", callback_data=f"s:{sub.id}:cc:{mode}:{page}"), back(sub))
    text = "🎁 <b>Какие коллекции присылать?</b> Ничего не выбрано — все." if mode == "i" else ""
    if mode == "x":
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
    call: CallbackQuery, state: FSMContext, db: Database, sub: Subscription, kind: str, page: int, mode: str
) -> None:
    emoji, _, genitive, field, xfield, _ = KINDS[kind]
    included, excluded = getattr(sub.filters, field), getattr(sub.filters, xfield)
    options = await db.attribute_options(kind, await _gift_ids(db, sub.filters))
    if not options or len(options) > MAX_PICKER_OPTIONS:
        # Списком не показать — сразу просим ввести текстом.
        await text_input(
            call,
            state,
            sub,
            field if mode == "i" else xfield,
            extra="Чтобы выбирать из списка, оставьте в подписке одну-две коллекции.",
        )
        return
    items, page, pages = paginate(options, page)
    inc, exc = {s.casefold() for s in included}, {s.casefold() for s in excluded}
    kb = InlineKeyboardBuilder()
    for attr_id, name, rarity in items:
        mark = "✅" if name.casefold() in inc else "🚫" if name.casefold() in exc else "▫️"
        kb.button(
            text=f"{mark} {name} · {fmt_permille(rarity)}",
            callback_data=f"s:{sub.id}:at:{kind}:{attr_id}:{page}:{mode}",
        )
    kb.adjust(2)
    kb.row(*nav(f"s:{sub.id}:a:{kind}", page, pages, suffix=f":{mode}"))
    kb.row(
        InlineKeyboardButton(
            text=f"{'🔘' if mode == 'i' else '⚪️'} Включить", callback_data=f"s:{sub.id}:a:{kind}:{page}:i"
        ),
        InlineKeyboardButton(
            text=f"{'🔘' if mode == 'x' else '⚪️'} Исключить", callback_data=f"s:{sub.id}:a:{kind}:{page}:x"
        ),
    )
    target_field = field if mode == "i" else xfield
    kb.row(
        InlineKeyboardButton(text="⌨️ Ввести", callback_data=f"s:{sub.id}:i:{target_field}"),
        InlineKeyboardButton(text="🧹 Очистить", callback_data=f"s:{sub.id}:ic:{target_field}"),
        back(sub),
    )
    action = "выбор" if mode == "i" else "исключение"
    text = (
        f"{emoji} <b>{genitive.capitalize()}: {action}</b> (редкие сверху).\n"
        "✅ — присылать только такие, 🚫 — никогда не присылать. Ничего не выбрано — любые."
    )
    if included:
        text += f"\n\nТолько: {esc(', '.join(included))}"
    if excluded:
        text += f"\nКроме: {esc(', '.join(excluded))}"
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
    kb.add(back(sub))
    await show(call, text, kb.as_markup())


def choice_kb(sub: Subscription, prefix: str, options: list, current, label) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for value in options:
        kb.button(text=f"{'🔘' if value == current else '⚪️'} {label(value)}", callback_data=f"{prefix}:{value or 0}")
    kb.adjust(2)
    kb.row(back(sub))
    return kb.as_markup()


def webapp_url(sub: Subscription, collections: list[str]) -> str:
    """Данные для Mini App передаются во фрагменте ссылки (на сервер не уходят), сжатыми."""
    payload = {"id": sub.id, "name": sub.name, "filters": sub.filters.to_dict(), "collections": collections}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    packed = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")
    if len(packed) > 6000:  # слишком длинно для кнопки — без списка коллекций
        return webapp_url(sub, [])
    return f"{config.webapp_url}#{packed}"


async def target_screen(call: CallbackQuery, db: Database, sub: Subscription) -> None:
    channels = await db.channels_of(sub.chat_id)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{'🔘' if not sub.target_id else '⚪️'} Сюда, в этот чат", callback_data=f"s:{sub.id}:dst:0")
    for ch in channels:
        mark = "🔘" if sub.target_id == ch["channel_id"] else "⚪️"
        kb.button(text=f"{mark} 📢 {ch['title']}"[:60], callback_data=f"s:{sub.id}:dst:{ch['channel_id']}")
    kb.adjust(1)
    kb.row(back(sub))
    text = "📢 <b>Куда присылать эту подписку?</b>"
    if not channels:
        text += (
            "\n\nКаналов пока нет. Добавьте бота админом в свой канал (с правом публиковать) "
            "и пришлите сюда <code>/connect @имя_канала</code>."
        )
    await show(call, text, kb.as_markup())


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

    async def save_and_show() -> None:
        await db.save_subscription(sub)
        await show_subscription(call, sub, db)

    if action == "":
        await show_subscription(call, sub, db)
    elif action == "ev":
        event = args[0]
        if event == "listing" and event not in f.events and not await is_premium(db, sub.chat_id):
            await call.answer(PREMIUM_HINT, show_alert=True)
            return
        if event in f.events and len(f.events) > 1:
            f.events.remove(event)
        elif event not in f.events and event in EVENT_BUTTONS:
            f.events.append(event)
        await save_and_show()
    elif action == "c":
        await collections_screen(call, db, sub, args[0], int(args[1]))
    elif action in ("ct", "cc"):
        mode = args[0]
        selected = f.collections if mode == "i" else f.exclude_collections
        if action == "cc":
            selected.clear()
        else:
            row = await db.collection(int(args[1]))
            title = row["title"] if row else None
            if title in selected:
                selected.remove(title)
            elif title:
                selected.append(title)
        await db.save_subscription(sub)
        await collections_screen(call, db, sub, mode, int(args[-1]))
    elif action == "a":
        await attributes_screen(call, state, db, sub, args[0], int(args[1]), args[2] if len(args) > 2 else "i")
    elif action == "at":
        kind, attr_id, page, mode = args[0], int(args[1]), int(args[2]), args[3] if len(args) > 3 else "i"
        name = await db.attribute_name(kind, attr_id)
        _, _, _, field, xfield, _ = KINDS[kind]
        selected, other = (
            (getattr(f, field), getattr(f, xfield)) if mode == "i" else (getattr(f, xfield), getattr(f, field))
        )
        if name:
            existing = [s for s in selected if s.casefold() == name.casefold()]
            for s in existing:
                selected.remove(s)
            if not existing:
                selected.append(name)
                other[:] = [s for s in other if s.casefold() != name.casefold()]
            await db.save_subscription(sub)
        await attributes_screen(call, state, db, sub, kind, page, mode)
    elif action == "r":
        kind = args[0]
        _, _, genitive, _, _, rfield = KINDS[kind]
        if len(args) > 1:
            setattr(f, rfield, int(args[1]) or None)
            await save_and_show()
            return
        label = lambda v: "Любая" if v is None else f"≤ {fmt_permille(v)}"  # noqa: E731
        await show(
            call,
            f"⭐ <b>Максимальная редкость {genitive}</b>\nЧем меньше процент, тем реже встречается.",
            choice_kb(sub, f"s:{sub.id}:r:{kind}", RARITY_OPTIONS, getattr(f, rfield), label),
        )
    elif action == "o":
        if args:
            f.min_one_in = int(args[0]) or None
            await save_and_show()
            return
        label = lambda v: "Любое" if v is None else f"реже {fmt_one_in(v)}"  # noqa: E731
        await show(
            call,
            "💎 <b>Редкость сочетания</b>\nВероятность выпадения именно такой модели, фона и узора "
            "вместе (их редкости перемножаются).",
            choice_kb(sub, f"s:{sub.id}:o", ONE_IN_OPTIONS, f.min_one_in, label),
        )
    elif action == "d":
        if args:
            f.min_discount = int(args[0]) or None
            await save_and_show()
            return
        label = lambda v: "Любая" if v is None else f"дешевле на {v}%+"  # noqa: E731
        await show(
            call,
            "📉 <b>Скидка от флора</b>\nПрисылать только лоты, цена которых ниже минимальной цены коллекции "
            "на маркете хотя бы на столько процентов. Работает для продаж и для улучшенных подарков, "
            "которые сразу выставили.",
            choice_kb(sub, f"s:{sub.id}:d", DISCOUNT_OPTIONS, f.min_discount, label),
        )
    elif action == "t" and args[0] in TOGGLES:
        setattr(f, args[0], not getattr(f, args[0]))
        await save_and_show()
    elif action == "i" and args[0] in TEXT_PROMPTS:
        await text_input(call, state, sub, args[0])
    elif action == "ic" and args[0] in TEXT_PROMPTS:
        field = args[0]
        setattr(f, field, [] if isinstance(getattr(f, field), list) else None)
        await save_and_show()
    elif action == "dst":
        if not args:
            await target_screen(call, db, sub)
            return
        target = int(args[0])
        if target and not await is_premium(db, sub.chat_id):
            await call.answer(PREMIUM_HINT, show_alert=True)
            return
        if target and target not in {r["channel_id"] for r in await db.channels_of(sub.chat_id)}:
            await call.answer("Канал не найден", show_alert=True)
            return
        sub.target_id = target or None
        await save_and_show()
    elif action == "web" and config.webapp_url:
        titles = [r["title"] for r in await db.collections()]
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🖥 Открыть редактор", web_app=WebAppInfo(url=webapp_url(sub, titles)))]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await call.message.answer(
            f"🖥 Нажмите кнопку внизу, чтобы открыть редактор подписки «{esc(sub.name)}».", reply_markup=keyboard
        )
        await call.answer()
    elif action == "en":
        sub.enabled = not sub.enabled
        await save_and_show()
    elif action == "reset":
        sub.filters = Filters()
        await save_and_show()
    elif action == "del":
        kb = InlineKeyboardBuilder()
        kb.button(text="🗑 Да, удалить", callback_data=f"s:{sub.id}:delok")
        kb.add(back(sub))
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
    if field in NUMBER_FIELDS:
        digits = value.replace(" ", "")
        if not digits.isdigit():
            await message.answer("Нужно целое число, попробуйте ещё раз.")
            return
        setattr(sub.filters, field, int(digits) or None)
    elif field == "name":
        sub.name = value[:40]
    elif field == "numbers":
        numbers = parse_numbers(value)
        if not numbers:
            await message.answer("Не нашёл номеров. Пример: <code>1, 69, 777, 1000-1100</code>")
            return
        sub.filters.numbers = numbers
    elif field == "owners":
        sub.filters.owners = [x.strip().lstrip("@") for x in value.replace("\n", ",").split(",") if x.strip()]
    else:
        setattr(sub.filters, field, [x.strip() for x in value.split(",") if x.strip()])
    await db.save_subscription(sub)
    await state.clear()
    await show_subscription(message, sub, db)
