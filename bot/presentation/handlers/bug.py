"""/bug — отправляет баг-репорт в личку получателям из конфига и удаляет исходное сообщение."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka, inject

from bot.infrastructure.config_loader import AppConfig
from bot.presentation.utils import reply_and_delete

logger = logging.getLogger(__name__)
router = Router(name="bug")


@router.message(Command("bug"))
@inject
async def cmd_bug(
    message: Message,
    command: CommandObject,
    config: FromDishka[AppConfig],
) -> None:
    """Принимает текст бага, рассылает в личку получателям из config.bug.recipients,
    удаляет исходное сообщение команды."""
    if message.from_user is None or message.bot is None:
        return

    bug_text = (command.args or "").strip()
    if not bug_text:
        await reply_and_delete(
            message,
            "❗ Укажи описание бага: <code>/bug &lt;текст&gt;</code>",
            parse_mode="HTML",
        )
        return

    sender = message.from_user
    sender_name = sender.full_name or str(sender.id)
    username_part = f" (@{sender.username})" if sender.username else ""
    chat_title = message.chat.title or str(message.chat.id)

    report = (
        f"🐛 <b>Баг-репорт</b>\n"
        f"От: <a href=\"tg://user?id={sender.id}\">{sender_name}</a>{username_part}\n"
        f"Чат: {chat_title}\n\n"
        f"{bug_text}"
    )

    sent_count = 0
    failed_count = 0
    for recipient_id in config.bug.recipients:
        try:
            await message.bot.send_message(recipient_id, report, parse_mode="HTML")
            sent_count += 1
        except TelegramBadRequest as e:
            logger.warning("bug: не удалось отправить репорт пользователю %d: %s", recipient_id, e)
            failed_count += 1
        except Exception:
            logger.exception("bug: ошибка при отправке репорта пользователю %d", recipient_id)
            failed_count += 1

    if not config.bug.recipients:
        logger.warning("bug: список получателей пуст — репорт некуда отправить")

    # Краткое подтверждение отправителю (быстро удаляется)
    if sent_count > 0:
        await reply_and_delete(message, "✅ Баг принят, спасибо!", delay=10)
    elif config.bug.recipients:
        await reply_and_delete(message, "⚠️ Не удалось доставить репорт.", delay=10)

    # Удаляем саму команду немедленно
    try:
        await message.delete()
    except TelegramBadRequest:
        pass