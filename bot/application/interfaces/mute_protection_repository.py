from abc import ABC, abstractmethod
from datetime import datetime


class IMuteProtectionRepository(ABC):
    @abstractmethod
    async def save(self, user_id: int, chat_id: int, protected_until: datetime) -> None: ...

    @abstractmethod
    async def get(self, user_id: int, chat_id: int) -> datetime | None:
        """Возвращает время окончания защиты или None если защиты нет / истекла."""
        ...

    @abstractmethod
    async def delete(self, user_id: int, chat_id: int) -> None: ...
