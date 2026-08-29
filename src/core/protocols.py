from typing import Protocol


class MessageSender(Protocol):
    async def send(self, chat_id: int, text: str) -> None:
        """Deliver a plain-text message to a chat."""
