"""Премиум за Telegram Stars. Включается, если задана PREMIUM_PRICE."""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from ..config import config
from ..db import Database
from .common import premium_enabled

log = logging.getLogger(__name__)
router = Router(name="premium")


def premium_text() -> str:
    return (
        f"⭐ <b>Премиум</b> — {config.premium_price} ⭐ за {config.premium_days} дн.\n\n"
        f"• до {config.max_subscriptions} подписок (бесплатно {config.free_subscriptions})\n"
        f"• лента продаж и снайпер «дешевле флора»\n"
        f"• слежка за {config.max_tracked} подарками (бесплатно {config.free_tracked})\n"
        f"• автопостинг подписок в свои каналы"
    )


def _until(ts: int) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(config.timezone)).strftime("%d.%m.%Y %H:%M")


async def send_invoice(message: Message, db: Database) -> None:
    until = await db.premium_until(message.chat.id)
    status = f"\n\nАктивен до {_until(until)}. Оплата продлит срок." if until > time.time() else ""
    await message.answer(premium_text() + status)
    await message.answer_invoice(
        title="Премиум",
        description=f"Премиум на {config.premium_days} дней для этого чата",
        payload=f"premium:{message.chat.id}:{config.premium_days}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Премиум на {config.premium_days} дн.", amount=config.premium_price)],
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message, db: Database) -> None:
    if not premium_enabled():
        await message.answer("Сейчас всё бесплатно — премиум не нужен 🙂")
        return
    await db.ensure_user(message.chat.id)
    await send_invoice(message, db)


@router.callback_query(F.data == "premium")
async def cb_premium(call: CallbackQuery, db: Database) -> None:
    await call.answer()
    if premium_enabled():
        await send_invoice(call.message, db)


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    ok = query.invoice_payload.startswith("premium:") and query.currency == "XTR" and premium_enabled()
    await query.answer(ok=ok, error_message=None if ok else "Оплата сейчас недоступна")


@router.message(F.successful_payment)
async def on_paid(message: Message, db: Database) -> None:
    payment = message.successful_payment
    _, chat_id, days = payment.invoice_payload.split(":")
    until = await db.add_payment(
        payment.telegram_payment_charge_id, int(chat_id), message.from_user.id, payment.total_amount, int(days)
    )
    await message.answer(f"⭐ Спасибо! Премиум активен до {_until(until)}.")


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    await message.answer(
        "По вопросам оплаты напишите администратору бота. Если премиум не активировался — "
        "пришлите ему номер платежа из чека, и мы вернём звёзды."
    )


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, db: Database, bot: Bot) -> None:
    if message.from_user.id not in config.admins:
        return
    charge_id = (command.args or "").strip()
    payment = await db.get_payment(charge_id) if charge_id else None
    if not payment:
        await message.answer("Использование: /refund <telegram_payment_charge_id>")
        return
    try:
        await bot.refund_star_payment(user_id=payment["user_id"], telegram_payment_charge_id=charge_id)
    except TelegramAPIError as e:
        await message.answer(f"Не удалось вернуть: {e}")
        return
    await db.refund_payment(charge_id)
    await message.answer("Звёзды возвращены, премиум за этот платёж снят.")
