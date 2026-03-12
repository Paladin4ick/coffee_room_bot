from abc import ABC, abstractmethod
from datetime import date


class IPerTargetLimitsRepository(ABC):
    @abstractmethod
    async def get_positive_given(
        self,
        actor_id: int,
        target_id: int,
        chat_id: int,
        day: date,
    ) -> int:
        """Количество положительных реакций от actor к target за день."""
        ...

    @abstractmethod
    async def increment_positive(
        self,
        actor_id: int,
        target_id: int,
        chat_id: int,
        day: date,
    ) -> None:
        """Инкрементировать счётчик положительных реакций от actor к target."""
        ...

    @abstractmethod
    async def decrement_positive(
        self,
        actor_id: int,
        target_id: int,
        chat_id: int,
        day: date,
    ) -> None:
        """Декрементировать счётчик (откат при снятии реакции)."""
        ...
