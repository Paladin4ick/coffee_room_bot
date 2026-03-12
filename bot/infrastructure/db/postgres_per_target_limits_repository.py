from datetime import date

from asyncpg import Connection

from bot.application.interfaces.per_target_limits_repository import IPerTargetLimitsRepository


class PostgresPerTargetLimitsRepository(IPerTargetLimitsRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    async def get_positive_given(
        self,
        actor_id: int,
        target_id: int,
        chat_id: int,
        day: date,
    ) -> int:
        row = await self._conn.fetchrow(
            """
            SELECT given FROM daily_positive_limits
            WHERE actor_id = $1 AND target_id = $2 AND chat_id = $3 AND date = $4
            """,
            actor_id,
            target_id,
            chat_id,
            day,
        )
        return row["given"] if row else 0

    async def increment_positive(
        self,
        actor_id: int,
        target_id: int,
        chat_id: int,
        day: date,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO daily_positive_limits (actor_id, target_id, chat_id, date, given)
            VALUES ($1, $2, $3, $4, 1)
            ON CONFLICT (actor_id, target_id, chat_id, date) DO UPDATE
                SET given = daily_positive_limits.given + 1
            """,
            actor_id,
            target_id,
            chat_id,
            day,
        )

    async def decrement_positive(
        self,
        actor_id: int,
        target_id: int,
        chat_id: int,
        day: date,
    ) -> None:
        await self._conn.execute(
            """
            UPDATE daily_positive_limits
            SET given = GREATEST(0, given - 1)
            WHERE actor_id = $1 AND target_id = $2 AND chat_id = $3 AND date = $4
            """,
            actor_id,
            target_id,
            chat_id,
            day,
        )
