from abc import ABC, abstractmethod

from bot.domain.entities import UserStats


class IUserStatsRepository(ABC):
    @abstractmethod
    async def get(self, user_id: int, chat_id: int) -> UserStats: ...

    @abstractmethod
    async def add_score_given(self, user_id: int, chat_id: int, delta: int) -> None:
        """Увеличить счётчик подаренных кирчиков (delta > 0)."""
        ...

    @abstractmethod
    async def add_score_taken(self, user_id: int, chat_id: int, delta: int) -> None:
        """Увеличить счётчик отнятых кирчиков (delta — абсолютное значение > 0)."""
        ...

    @abstractmethod
    async def add_win(self, user_id: int, chat_id: int, game: str) -> None:
        """Добавить победу в игре. game: 'blackjack' | 'slots' | 'dice' | 'giveaway'."""
        ...
