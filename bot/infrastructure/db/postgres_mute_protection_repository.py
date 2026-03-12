from datetime import datetime

import asyncpg

from bot.application.interfaces.mute_protection_repository import IMuteProtectionRepository


class PostgresMuteProtectionRepository(IMuteProtectionRepository):
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def save(self, user_id: int, chat_id: int, protected_until: datetime) -> None:
        await self._conn.execute(
            """
            INSERT INTO mute_protection (user_id, chat_id, protected_until)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, chat_id) DO UPDATE
                SET protected_until = EXCLUDED.protected_until,
                    created_at = now()
            """,
            user_id,
            chat_id,
            protected_until,
        )

    async def get(self, user_id: int, chat_id: int) -> datetime | None:
        row = await self._conn.fetchrow(
            """
            SELECT protected_until FROM mute_protection
            WHERE user_id = $1 AND chat_id = $2 AND protected_until > now()
            """,
            user_id,
            chat_id,
        )
        return row["protected_until"] if row else None

    async def delete(self, user_id: int, chat_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM mute_protection WHERE user_id = $1 AND chat_id = $2",
            user_id,
            chat_id,
        )
