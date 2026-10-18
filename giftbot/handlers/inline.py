"""Inline-режим: @бот pepe gold — поиск по недавним улучшениям, чтобы поделиться в любом чате.

Включается у @BotFather: /setinline.
"""

import time

from aiogram import Router
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
)

from ..db import Database
from ..filters import fmt_one_in
from ..render import upgrade_card

router = Router(name="inline")


@router.inline_query()
async def on_inline(query: InlineQuery, db: Database) -> None:
    words = query.query.split()[:5]
    if words:
        found = await db.search_upgrades(words, 20)
    else:
        found = await db.rarest_upgrades(int(time.time()) - 86400, 20)
    results = []
    for u in found:
        attrs = " · ".join(x for x in (u.model, u.backdrop, u.symbol) if x)
        description = attrs + (f" · {fmt_one_in(u.one_in)}" if u.one_in else "")
        results.append(
            InlineQueryResultArticle(
                id=u.slug[:64],
                title=f"{u.collection} #{u.num}",
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=upgrade_card(u).replace("🆕 ", "🎁 ", 1),
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(url=u.link, prefer_large_media=True),
                ),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть", url=u.link)]]
                ),
            )
        )
    await query.answer(results, cache_time=30, is_personal=False)
