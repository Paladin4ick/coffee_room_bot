"""Команды бота.

- Админские: /add, /sub, /set, /reset, /save, /restore, /op, /deop, /amute, /unmute
- Пользовательские: /mute, /selfmute, /tag, /transfer, /protect
- Справка: /help (интерактивная, привязана к вызвавшему, тексты из help.yaml)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberAdministrator,
    ChatMemberOwner,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dishka.integrations.aiogram import FromDishka, inject

from bot.application.score_service import ScoreService, SPECIAL_EMOJI
from bot.application.mute_service import MuteService
from bot.application.interfaces.user_repository import IUserRepository
from bot.application.interfaces.saved_permissions_repository import ISavedPermissionsRepository
from bot.application.interfaces.mute_protection_repository import IMuteProtectionRepository
from bot.domain.entities import MuteEntry
from bot.domain.tz import TZ_MSK
from bot.domain.bot_utils import is_admin, parse_duration, format_duration
from bot.infrastructure.config_loader import AppConfig
from bot.infrastructure.message_formatter import MessageFormatter, user_link
from bot.presentation.utils import NO_PREVIEW
from bot.presentation.handlers.help_renderer import HelpRenderer

logger = logging.getLogger(__name__)

# Поля прав администратора (Telegram API)
_ADMIN_PERM_FIELDS = (
    "can_manage_chat",
    "can_change_info",
    "can_delete_messages",
    "can_invite_users",
    "can_restrict_members",
    "can_pin_messages",
    "can_manage_video_chats",
    "can_promote_members",
    "can_post_messages",
    "can_edit_messages",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
    "can_manage_topics",
    "can_manage_direct_messages",
    "can_manage_tags",
)

# Права модератора, выдаваемые через /op
MODERATOR_PERMS = {
    "can_delete_messages": True,
    "can_restrict_members": True,
    "can_invite_users": True,
    "can_pin_messages": True,
    "can_manage_video_chats": True,
    "can_manage_topics": True,
}


# ── Вспомогательные функции ───────────────────────────────────────

def _extract_admin_permissions(member: ChatMemberAdministrator) -> dict:
    perms: dict = {}
    for field in _ADMIN_PERM_FIELDS:
        perms[field] = getattr(member, field, False) or False
    if member.custom_title:
        perms["custom_title"] = member.custom_title
    return perms


def _promote_kwargs(perms: dict) -> dict:
    return {k: v for k, v in perms.items() if k in _ADMIN_PERM_FIELDS}


def _parse_args_user_number(args: str | None) -> tuple[str, int] | None:
    if not args:
        return None
    parts = args.strip().split()
    if len(parts) != 2:
        return None
    username = parts[0].lstrip("@")
    try:
        n = int(parts[1])
    except ValueError:
        return None
    return (username, n)


async def _resolve_user_and_number(args, user_repo):
    parsed = _parse_args_user_number(args)
    if parsed is None:
        return None
    username, n = parsed
    user = await user_repo.get_by_username(username)
    return (user, n)


async def _resolve_mute_args(args, message, user_repo):
    """Разбирает аргументы /mute и /amute с поддержкой reply.

    Варианты:
      /mute @username 30    — явный username + время
      /mute 30              — только время, цель берётся из reply
    """
    from bot.domain.entities import User as DomainUser

    args_str = (args or "").strip()
    parts = args_str.split()

    if len(parts) == 2:
        username = parts[0].lstrip("@")
        seconds = parse_duration(parts[1])
        if seconds is not None:
            user = await user_repo.get_by_username(username)
            return (user, max(1, seconds // 60))
        return None

    if len(parts) == 1:
        seconds = parse_duration(parts[0])
        if seconds is None:
            return None
        reply = message.reply_to_message
        if reply is None or reply.from_user is None:
            return None
        tg_user = reply.from_user
        target = DomainUser(
            id=tg_user.id,
            username=tg_user.username,
            full_name=tg_user.full_name or str(tg_user.id),
        )
        return (target, max(1, seconds // 60))

    return None


async def _resolve_username(args: str | None, user_repo: IUserRepository):
    if not args:
        return None
    return await user_repo.get_by_username(args.strip().lstrip("@"))


def _admin_reply(formatter: MessageFormatter, target, new_value: int) -> str:
    display = user_link(target.username, target.full_name, target.id)
    return formatter._t["admin_score_set"].format(
        user=display,
        total=new_value,
        score_word=formatter._p.pluralize(abs(new_value)),
    )


# ── Снятие мута ───────────────────────────────────────────────────

async def _unmute_user(bot, mute_service: MuteService, entry: MuteEntry) -> None:
    try:
        await bot.restrict_chat_member(
            chat_id=entry.chat_id,
            user_id=entry.user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_invite_users=True,
                can_change_info=True,
                can_pin_messages=True,
                can_manage_topics=True,
            ),
        )
    except Exception:
        logger.exception("Failed to unrestrict user %d in chat %d", entry.user_id, entry.chat_id)

    if entry.was_admin and entry.admin_permissions:
        try:
            kw = _promote_kwargs(entry.admin_permissions)
            await bot.promote_chat_member(
                chat_id=entry.chat_id,
                user_id=entry.user_id,
                **kw,
            )
            if entry.admin_permissions.get("custom_title"):
                await bot.set_chat_administrator_custom_title(
                    chat_id=entry.chat_id,
                    user_id=entry.user_id,
                    custom_title=entry.admin_permissions["custom_title"],
                )
        except Exception:
            logger.exception(
                "Failed to restore admin rights for user %d in chat %d",
                entry.user_id, entry.chat_id,
            )

    await mute_service.delete_mute(entry.user_id, entry.chat_id)


# ── Роутер ────────────────────────────────────────────────────────

def create_admin_router(prefix: str) -> Router:  # prefix оставлен для совместимости
    router = Router(name="admin_commands")

    # ── /reset ────────────────────────────────────────────────────

    @router.message(Command("reset"))
    @inject
    async def cmd_reset(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        if not command.args:
            await message.reply(formatter._t["admin_usage_reset"])
            return
        target = await user_repo.get_by_username(command.args.strip().lstrip("@"))
        if target is None:
            await message.reply(formatter._t["error_user_not_found"])
            return
        new_value = await score_service.set_score(target.id, message.chat.id, 0, admin_id=message.from_user.id)
        await message.reply(_admin_reply(formatter, target, new_value), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /set ──────────────────────────────────────────────────────

    @router.message(Command("set"))
    @inject
    async def cmd_set(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        parsed = await _resolve_user_and_number(command.args, user_repo)
        if parsed is None or parsed[0] is None:
            await message.reply(formatter._t["admin_usage_set"])
            return
        target, amount = parsed
        if target is None:
            await message.reply(formatter._t["error_user_not_found"])
            return
        new_value = await score_service.set_score(target.id, message.chat.id, amount, admin_id=message.from_user.id)
        await message.reply(_admin_reply(formatter, target, new_value), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /add ──────────────────────────────────────────────────────

    @router.message(Command("add"))
    @inject
    async def cmd_add(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        parsed = await _resolve_user_and_number(command.args, user_repo)
        if parsed is None:
            await message.reply(formatter._t["admin_usage_add"])
            return
        target, amount = parsed
        if target is None:
            await message.reply(formatter._t["error_user_not_found"])
            return
        if amount <= 0:
            await message.reply(formatter._t["admin_negative_amount"])
            return
        new_value = await score_service.add_score(target.id, message.chat.id, amount, admin_id=message.from_user.id)
        await message.reply(_admin_reply(formatter, target, new_value), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /sub ──────────────────────────────────────────────────────

    @router.message(Command("sub"))
    @inject
    async def cmd_sub(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        parsed = await _resolve_user_and_number(command.args, user_repo)
        if parsed is None:
            await message.reply(formatter._t["admin_usage_sub"])
            return
        target, amount = parsed
        if target is None:
            await message.reply(formatter._t["error_user_not_found"])
            return
        if amount <= 0:
            await message.reply(formatter._t["admin_negative_amount"])
            return
        new_value = await score_service.add_score(target.id, message.chat.id, -amount, admin_id=message.from_user.id)
        await message.reply(_admin_reply(formatter, target, new_value), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /save ─────────────────────────────────────────────────────

    @router.message(Command("save"))
    @inject
    async def cmd_save(
        message: Message,
        command: CommandObject,
        user_repo: FromDishka[IUserRepository],
        saved_perms_repo: FromDishka[ISavedPermissionsRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        target = await _resolve_username(command.args, user_repo)
        if target is None:
            await message.reply(formatter._t["save_usage"])
            return
        display = user_link(target.username, target.full_name, target.id)
        try:
            member = await message.bot.get_chat_member(message.chat.id, target.id)
        except Exception:
            await message.reply(formatter._t["save_not_admin"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        if not isinstance(member, ChatMemberAdministrator):
            await message.reply(formatter._t["save_not_admin"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        perms = _extract_admin_permissions(member)
        existing = await saved_perms_repo.get(target.id, message.chat.id)
        await saved_perms_repo.save(target.id, message.chat.id, perms)
        key = "save_overwritten" if existing else "save_success"
        await message.reply(formatter._t[key].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /restore ──────────────────────────────────────────────────

    @router.message(Command("restore"))
    @inject
    async def cmd_restore(
        message: Message,
        command: CommandObject,
        user_repo: FromDishka[IUserRepository],
        saved_perms_repo: FromDishka[ISavedPermissionsRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        target = await _resolve_username(command.args, user_repo)
        if target is None:
            await message.reply(formatter._t["restore_usage"])
            return
        display = user_link(target.username, target.full_name, target.id)
        perms = await saved_perms_repo.get(target.id, message.chat.id)
        if perms is None:
            await message.reply(formatter._t["restore_not_found"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        try:
            await message.bot.promote_chat_member(chat_id=message.chat.id, user_id=target.id, **_promote_kwargs(perms))
            if perms.get("custom_title"):
                await message.bot.set_chat_administrator_custom_title(chat_id=message.chat.id, user_id=target.id, custom_title=perms["custom_title"])
        except Exception:
            logger.exception("Failed to restore permissions for user %d", target.id)
            await message.reply(formatter._t["restore_failed"])
            return
        await message.reply(formatter._t["restore_success"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /op ───────────────────────────────────────────────────────

    @router.message(Command("op"))
    @inject
    async def cmd_op(
        message: Message,
        command: CommandObject,
        user_repo: FromDishka[IUserRepository],
        saved_perms_repo: FromDishka[ISavedPermissionsRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        target = await _resolve_username(command.args, user_repo)
        if target is None:
            await message.reply(formatter._t["op_usage"])
            return
        display = user_link(target.username, target.full_name, target.id)
        try:
            member = await message.bot.get_chat_member(message.chat.id, target.id)
        except Exception:
            await message.reply(formatter._t["op_failed"])
            return
        if isinstance(member, (ChatMemberOwner, ChatMemberAdministrator)):
            await message.reply(formatter._t["op_already"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        try:
            await message.bot.promote_chat_member(chat_id=message.chat.id, user_id=target.id, can_invite_users=True)
        except Exception:
            logger.exception("Failed to op user %d", target.id)
            await message.reply(formatter._t["op_failed"])
            return
        await saved_perms_repo.save(target.id, message.chat.id, MODERATOR_PERMS)
        await message.reply(formatter._t["op_success"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /deop ─────────────────────────────────────────────────────

    @router.message(Command("deop"))
    @inject
    async def cmd_deop(
        message: Message,
        command: CommandObject,
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        target = await _resolve_username(command.args, user_repo)
        if target is None:
            await message.reply(formatter._t["op_usage"])
            return
        display = user_link(target.username, target.full_name, target.id)
        try:
            member = await message.bot.get_chat_member(message.chat.id, target.id)
        except Exception:
            await message.reply(formatter._t["op_failed"])
            return
        if isinstance(member, ChatMemberOwner):
            await message.reply(formatter._t["op_already"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        if not isinstance(member, ChatMemberAdministrator):
            await message.reply(formatter._t["deop_not_admin"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        try:
            demote_kw = {f: False for f in _ADMIN_PERM_FIELDS}
            await message.bot.promote_chat_member(chat_id=message.chat.id, user_id=target.id, **demote_kw)
        except Exception:
            logger.exception("Failed to deop user %d", target.id)
            await message.reply(formatter._t["op_failed"])
            return
        await message.reply(formatter._t["deop_success"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /mute ─────────────────────────────────────────────────────

    @router.message(Command("mute"))
    @inject
    async def cmd_mute(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        mute_service: FromDishka[MuteService],
        protection_repo: FromDishka[IMuteProtectionRepository],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        mute_cfg = config.mute
        p = formatter._p
        parsed = await _resolve_mute_args(command.args, message, user_repo)
        if parsed is None:
            await message.reply(formatter._t["mute_usage"].format(min=mute_cfg.min_minutes, max=mute_cfg.max_minutes))
            return
        target, minutes = parsed
        if target is None:
            await message.reply(formatter._t["error_user_not_found"])
            return
        if target.id == message.from_user.id:
            await message.reply(formatter._t["mute_self"])
            return
        if minutes < mute_cfg.min_minutes or minutes > mute_cfg.max_minutes:
            await message.reply(formatter._t["mute_invalid_minutes"].format(min=mute_cfg.min_minutes, max=mute_cfg.max_minutes))
            return
        protected_until = await protection_repo.get(target.id, message.chat.id)
        if protected_until is not None:
            target_link = user_link(target.username, target.full_name, target.id)
            until_str = protected_until.astimezone(TZ_MSK).strftime("%H:%M %d.%m")
            await message.reply(formatter._t["mute_target_protected"].format(target=target_link, until=until_str), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        cost = minutes * mute_cfg.cost_per_minute
        score = await score_service.get_score(message.from_user.id, message.chat.id)
        if score.value < cost:
            await message.reply(formatter._t["mute_not_enough"].format(cost=cost, score_word=p.pluralize(cost), balance=score.value, score_word_balance=p.pluralize(score.value)))
            return
        bot = message.bot
        chat_id = message.chat.id
        until = datetime.now(TZ_MSK) + timedelta(minutes=minutes)
        try:
            member = await bot.get_chat_member(chat_id, target.id)
        except Exception:
            await message.reply(formatter._t["mute_failed"])
            return
        was_admin = isinstance(member, ChatMemberAdministrator)
        admin_perms: dict | None = None
        if was_admin:
            admin_perms = _extract_admin_permissions(member)
            try:
                await bot.promote_chat_member(chat_id=chat_id, user_id=target.id, **{f: False for f in _ADMIN_PERM_FIELDS})
            except Exception:
                await message.reply(formatter._t["mute_failed"])
                return
        try:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=target.id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
        except Exception:
            if was_admin and admin_perms:
                try:
                    await bot.promote_chat_member(chat_id=chat_id, user_id=target.id, **_promote_kwargs(admin_perms))
                except Exception:
                    logger.exception("Failed to restore admin rights after mute failure")
            await message.reply(formatter._t["mute_failed"])
            return
        await mute_service.save_mute(MuteEntry(user_id=target.id, chat_id=chat_id, muted_by=message.from_user.id, until_at=until, was_admin=was_admin, admin_permissions=admin_perms))
        result = await score_service.spend_score(actor_id=message.from_user.id, target_id=target.id, chat_id=chat_id, cost=cost)
        if not result.success:
            await _unmute_user(bot, mute_service, MuteEntry(user_id=target.id, chat_id=chat_id, muted_by=message.from_user.id, until_at=until, was_admin=was_admin, admin_permissions=admin_perms))
            await message.reply(formatter._t["mute_not_enough"].format(cost=cost, score_word=p.pluralize(cost), balance=result.current_balance, score_word_balance=p.pluralize(result.current_balance)))
            return
        actor_link = user_link(message.from_user.username, message.from_user.full_name or "", message.from_user.id)
        target_link = user_link(target.username, target.full_name, target.id)
        await message.reply(
            formatter._t["mute_success"].format(actor=actor_link, target=target_link, minutes=minutes, cost=cost, score_word=p.pluralize(cost), balance=result.new_balance, score_word_balance=p.pluralize(result.new_balance)),
            parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
        )

    # ── /amute ────────────────────────────────────────────────────

    @router.message(Command("amute"))
    @inject
    async def cmd_amute(
        message: Message,
        command: CommandObject,
        mute_service: FromDishka[MuteService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        """Бесплатный мут для администраторов, обходит /protect."""
        if message.from_user is None or message.bot is None:
            return
        bot = message.bot
        chat_id = message.chat.id
        mute_cfg = config.mute
        is_config_admin = is_admin(message.from_user.username, config.admin.users)
        if not is_config_admin:
            try:
                caller_member = await bot.get_chat_member(chat_id, message.from_user.id)
                has_restrict = (
                    isinstance(caller_member, ChatMemberAdministrator) and caller_member.can_restrict_members
                ) or isinstance(caller_member, ChatMemberOwner)
            except Exception:
                has_restrict = False
            if not has_restrict:
                await message.reply(formatter._t["amute_not_allowed"])
                return
        parsed = await _resolve_mute_args(command.args, message, user_repo)
        if parsed is None:
            await message.reply(formatter._t["amute_usage"].format(min=mute_cfg.min_minutes, max=mute_cfg.max_minutes))
            return
        target, minutes = parsed
        if target is None:
            await message.reply(formatter._t["error_user_not_found"])
            return
        if target.id == message.from_user.id:
            await message.reply(formatter._t["mute_self"])
            return
        until = datetime.now(TZ_MSK) + timedelta(minutes=minutes)
        try:
            member = await bot.get_chat_member(chat_id, target.id)
        except Exception:
            await message.reply(formatter._t["mute_failed"])
            return
        was_admin = isinstance(member, ChatMemberAdministrator)
        admin_perms: dict | None = None
        if was_admin:
            admin_perms = _extract_admin_permissions(member)
            try:
                await bot.promote_chat_member(chat_id=chat_id, user_id=target.id, **{f: False for f in _ADMIN_PERM_FIELDS})
            except Exception:
                await message.reply(formatter._t["mute_failed"])
                return
        try:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=target.id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
        except Exception:
            if was_admin and admin_perms:
                try:
                    await bot.promote_chat_member(chat_id=chat_id, user_id=target.id, **_promote_kwargs(admin_perms))
                except Exception:
                    logger.exception("Failed to restore admin rights after amute failure")
            await message.reply(formatter._t["mute_failed"])
            return
        await mute_service.save_mute(MuteEntry(user_id=target.id, chat_id=chat_id, muted_by=message.from_user.id, until_at=until, was_admin=was_admin, admin_permissions=admin_perms))
        actor_link = user_link(message.from_user.username, message.from_user.full_name or "", message.from_user.id)
        target_link = user_link(target.username, target.full_name, target.id)
        await message.reply(
            formatter._t["amute_success"].format(actor=actor_link, target=target_link, minutes=minutes),
            parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
        )

    # ── /selfmute ─────────────────────────────────────────────────

    @router.message(Command("selfmute"))
    @inject
    async def cmd_selfmute(
        message: Message,
        command: CommandObject,
        mute_service: FromDishka[MuteService],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        mute_cfg = config.mute
        min_sec = mute_cfg.selfmute_min_minutes * 60
        max_sec = mute_cfg.selfmute_max_minutes * 60
        if not command.args:
            await message.reply(formatter._t["selfmute_usage"].format(min=mute_cfg.selfmute_min_minutes, max=mute_cfg.selfmute_max_minutes))
            return
        seconds = parse_duration(command.args)
        if seconds is None or seconds <= 0 or seconds < min_sec or seconds > max_sec:
            await message.reply(formatter._t["selfmute_invalid_minutes"].format(min=mute_cfg.selfmute_min_minutes, max=mute_cfg.selfmute_max_minutes))
            return
        bot = message.bot
        chat_id = message.chat.id
        user_id = message.from_user.id
        until = datetime.now(TZ_MSK) + timedelta(seconds=seconds)
        was_admin = False
        admin_perms: dict | None = None
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if isinstance(member, ChatMemberOwner):
                await message.reply(formatter._t["selfmute_failed"])
                return
            if isinstance(member, ChatMemberAdministrator):
                was_admin = True
                admin_perms = _extract_admin_permissions(member)
                await bot.promote_chat_member(chat_id=chat_id, user_id=user_id, **{f: False for f in _ADMIN_PERM_FIELDS})
        except TelegramBadRequest as e:
            logger.warning("selfmute pre-check failed for user %d: %s", user_id, e)
            await message.reply(formatter._t["selfmute_failed"])
            return
        try:
            await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
        except Exception:
            if was_admin and admin_perms:
                try:
                    await bot.promote_chat_member(chat_id=chat_id, user_id=user_id, **_promote_kwargs(admin_perms))
                except Exception:
                    logger.exception("Failed to restore admin rights after selfmute failure for user %d", user_id)
            await message.reply(formatter._t["selfmute_failed"])
            return
        await mute_service.save_mute(MuteEntry(user_id=user_id, chat_id=chat_id, muted_by=user_id, until_at=until, was_admin=was_admin, admin_permissions=admin_perms))
        user_link_str = user_link(message.from_user.username, message.from_user.full_name or "", user_id)
        await message.reply(
            formatter._t["selfmute_success"].format(user=user_link_str, duration=format_duration(seconds)),
            parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
        )

    # ── /unmute ───────────────────────────────────────────────────

    @router.message(Command("unmute"))
    @inject
    async def cmd_unmute(
        message: Message,
        command: CommandObject,
        mute_service: FromDishka[MuteService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        if not is_admin(message.from_user.username, config.admin.users):
            await message.reply(formatter._t["admin_not_allowed"])
            return
        target = await _resolve_username(command.args, user_repo)
        if target is None:
            reply = message.reply_to_message
            if reply is not None and reply.from_user is not None:
                from bot.domain.entities import User as DomainUser
                tg = reply.from_user
                target = DomainUser(id=tg.id, username=tg.username, full_name=tg.full_name or str(tg.id))
            else:
                await message.reply(formatter._t["unmute_usage"])
                return
        display = user_link(target.username, target.full_name, target.id)
        entry = await mute_service._repo.get(target.id, message.chat.id)
        if entry is None:
            await message.reply(formatter._t["unmute_not_muted"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        await _unmute_user(message.bot, mute_service, entry)
        await message.reply(formatter._t["unmute_success"].format(user=display), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /tag ──────────────────────────────────────────────────────

    @router.message(Command("tag"))
    @inject
    async def cmd_tag(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None or message.bot is None:
            return
        tc = config.tag
        p = formatter._p
        bot = message.bot
        chat_id = message.chat.id
        args = command.args
        if not args:
            await message.reply(formatter._t["tag_usage"].format(cost_self=tc.cost_self, sw_self=p.pluralize(tc.cost_self)))
            return
        parts = args.strip().split(maxsplit=1)
        if parts[0].startswith("@"):
            target = await user_repo.get_by_username(parts[0].lstrip("@"))
            if target is None:
                await message.reply(formatter._t["error_user_not_found"])
                return
            new_tag = parts[1].strip() if len(parts) > 1 else None
            if new_tag is None:
                await message.reply(formatter._t["tag_usage"].format(cost_self=tc.cost_self, sw_self=p.pluralize(tc.cost_self)))
                return
            is_self = (target.id == message.from_user.id)
        else:
            target = await user_repo.get_by_id(message.from_user.id)
            if target is None:
                await message.reply(formatter._t["error_user_not_found"])
                return
            new_tag = args.strip()
            is_self = True
        clearing = (new_tag == "--clear")
        is_free = is_admin(message.from_user.username, config.admin.users)
        if not clearing and len(new_tag) > tc.max_length:
            await message.reply(formatter._t["tag_too_long"].format(max=tc.max_length))
            return
        if is_self:
            cost = tc.cost_self
        else:
            try:
                member = await bot.get_chat_member(chat_id, target.id)
            except Exception:
                await message.reply(formatter._t["tag_failed"])
                return
            if isinstance(member, ChatMemberOwner):
                cost = tc.cost_owner
            elif isinstance(member, ChatMemberAdministrator):
                cost = tc.cost_admin
            else:
                cost = tc.cost_member
        if not is_free:
            score = await score_service.get_score(message.from_user.id, chat_id)
            if score.value < cost:
                await message.reply(formatter._t["tag_not_enough"].format(cost=cost, score_word=p.pluralize(cost), balance=score.value, score_word_balance=p.pluralize(score.value)))
                return
        try:
            await bot.set_chat_member_tag(chat_id=chat_id, user_id=target.id, tag=None if clearing else new_tag)
        except Exception:
            await message.reply(formatter._t["tag_failed"])
            return
        target_link = user_link(target.username, target.full_name, target.id)
        if is_free:
            text = formatter._t["tag_cleared_free"].format(target=target_link) if clearing else formatter._t["tag_success_free"].format(target=target_link, tag=new_tag)
            await message.reply(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
            return
        result = await score_service.spend_score(actor_id=message.from_user.id, target_id=target.id, chat_id=chat_id, cost=cost, emoji=SPECIAL_EMOJI["tag"])
        if not result.success:
            await message.reply(formatter._t["tag_not_enough"].format(cost=cost, score_word=p.pluralize(cost), balance=result.current_balance, score_word_balance=p.pluralize(result.current_balance)))
            return
        if clearing:
            text = formatter._t["tag_cleared"].format(target=target_link, cost=cost, score_word=p.pluralize(cost), balance=result.new_balance, score_word_balance=p.pluralize(result.new_balance))
        else:
            text = formatter._t["tag_success"].format(target=target_link, tag=new_tag, cost=cost, score_word=p.pluralize(cost), balance=result.new_balance, score_word_balance=p.pluralize(result.new_balance))
        await message.reply(text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)

    # ── /transfer ─────────────────────────────────────────────────

    @router.message(Command("transfer"))
    @inject
    async def cmd_transfer(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        user_repo: FromDishka[IUserRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None:
            return
        p = formatter._p
        chat_id = message.chat.id
        parsed = await _resolve_user_and_number(command.args, user_repo)
        if parsed is None:
            await message.reply(formatter._t["transfer_usage"])
            return
        target, amount = parsed
        if amount <= 0:
            await message.reply(formatter._t["transfer_invalid_amount"])
            return
        if target is None:
            await message.reply(formatter._t["error_user_not_found"])
            return
        if target.id == message.from_user.id:
            await message.reply(formatter._t["transfer_self"])
            return
        result = await score_service.transfer_score(sender_id=message.from_user.id, receiver_id=target.id, chat_id=chat_id, amount=amount)
        if not result.success:
            await message.reply(formatter._t["transfer_not_enough"].format(amount=amount, score_word=p.pluralize(amount), balance=result.current_balance, score_word_balance=p.pluralize(result.current_balance)))
            return
        sender_link = user_link(message.from_user.username, message.from_user.full_name or "", message.from_user.id)
        receiver_link = user_link(target.username, target.full_name, target.id)
        await message.reply(
            formatter._t["transfer_success"].format(sender=sender_link, receiver=receiver_link, amount=amount, score_word=p.pluralize(amount), sender_balance=result.sender_balance, score_word_sender=p.pluralize(result.sender_balance)),
            parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
        )

    # ── /protect ──────────────────────────────────────────────────

    @router.message(Command("protect"))
    @inject
    async def cmd_protect(
        message: Message,
        command: CommandObject,
        score_service: FromDishka[ScoreService],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        if message.from_user is None:
            return
        mute_cfg = config.mute
        p = formatter._p
        chat_id = message.chat.id
        user_id = message.from_user.id
        cost = mute_cfg.protection_cost
        hours = mute_cfg.protection_duration_hours
        score = await score_service.get_score(user_id, chat_id)
        if score.value < cost:
            await message.reply(formatter._t["protect_not_enough"].format(cost=cost, score_word=p.pluralize(cost), balance=score.value, score_word_balance=p.pluralize(score.value)))
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Да, потратить {cost} {p.pluralize(cost)}", callback_data=f"protect:confirm:{user_id}:{chat_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"protect:cancel:{user_id}"),
        ]])
        await message.reply(
            formatter._t["protect_confirm"].format(hours=hours, cost=cost, score_word=p.pluralize(cost), balance=score.value, score_word_balance=p.pluralize(score.value)),
            parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW, reply_markup=kb,
        )

    @router.callback_query(F.data.startswith("protect:"))
    @inject
    async def cb_protect(
        callback: CallbackQuery,
        score_service: FromDishka[ScoreService],
        protection_repo: FromDishka[IMuteProtectionRepository],
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
    ) -> None:
        async def safe_answer(text: str = "", alert: bool = False) -> None:
            try:
                await callback.answer(text, show_alert=alert)
            except Exception:
                pass

        parts = callback.data.split(":")
        action = parts[1]
        owner_id = int(parts[2])
        if callback.from_user.id != owner_id:
            await safe_answer("Это не твоя кнопка.", alert=True)
            return
        if action == "cancel":
            try:
                await callback.message.edit_text("❌ Защита отменена.")
            except Exception:
                pass
            await safe_answer()
            return
        chat_id = int(parts[3])
        mute_cfg = config.mute
        p = formatter._p
        cost = mute_cfg.protection_cost
        hours = mute_cfg.protection_duration_hours
        user_id = owner_id
        result = await score_service.spend_score(actor_id=user_id, target_id=user_id, chat_id=chat_id, cost=cost, emoji=SPECIAL_EMOJI["protect"])
        if not result.success:
            try:
                await callback.message.edit_text(formatter._t["protect_not_enough"].format(cost=cost, score_word=p.pluralize(cost), balance=result.current_balance, score_word_balance=p.pluralize(result.current_balance)))
            except Exception:
                pass
            await safe_answer()
            return
        existing = await protection_repo.get(user_id, chat_id)
        now = datetime.now(TZ_MSK)
        new_until = (existing if existing and existing > now else now) + timedelta(hours=hours)
        await protection_repo.save(user_id, chat_id, new_until)
        until_str = new_until.strftime("%H:%M %d.%m")
        user_link_str = user_link(callback.from_user.username, callback.from_user.full_name or "", user_id)
        if existing and existing > now:
            text = formatter._t["protect_extended"].format(until=until_str, cost=cost, score_word=p.pluralize(cost), balance=result.new_balance, score_word_balance=p.pluralize(result.new_balance))
        else:
            text = formatter._t["protect_success"].format(user=user_link_str, hours=hours, cost=cost, score_word=p.pluralize(cost), balance=result.new_balance, score_word_balance=p.pluralize(result.new_balance))
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
        await safe_answer()

    # ── /help ─────────────────────────────────────────────────────

    @router.message(Command("help"))
    @inject
    async def cmd_help(
        message: Message,
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
        renderer: FromDishka[HelpRenderer],
    ) -> None:
        if message.from_user is None:
            return
        uid = message.from_user.id
        await message.reply(
            renderer.main_text(config.score.icon),
            parse_mode=ParseMode.HTML,
            link_preview_options=NO_PREVIEW,
            reply_markup=renderer.main_kb(uid),
        )

    # ── Callback для /help ────────────────────────────────────────
    # Формат callback_data: "help:{section}:{caller_uid}"

    @router.callback_query(F.data.startswith("help:"))
    @inject
    async def cb_help(
        callback: CallbackQuery,
        formatter: FromDishka[MessageFormatter],
        config: FromDishka[AppConfig],
        renderer: FromDishka[HelpRenderer],
    ) -> None:
        async def safe_answer(text: str = "", show_alert: bool = False) -> None:
            try:
                await callback.answer(text, show_alert=show_alert)
            except Exception:
                pass

        parts = callback.data.split(":")
        if len(parts) != 3:
            await safe_answer()
            return

        _, section, caller_uid_str = parts
        try:
            caller_uid = int(caller_uid_str)
        except ValueError:
            await safe_answer()
            return

        if callback.from_user.id != caller_uid:
            await safe_answer("Это не твоя справка.", show_alert=True)
            return

        uid = caller_uid
        if section == "main":
            text = renderer.main_text(config.score.icon)
            kb = renderer.main_kb(uid)
        else:
            text = renderer.section_text(section, config, formatter)
            kb = renderer.back_kb(uid)

        if not text:
            await safe_answer()
            return

        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=NO_PREVIEW)
        except Exception:
            pass

        await safe_answer()

    return router
