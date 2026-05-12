from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InputType(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    AUDIO = "audio"
    WEB_SEARCH = "web_search"


class RequestStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class RequestPayload:
    request_id: str
    bale_user_id: str
    bale_chat_id: str
    input_type: InputType
    text: str | None = None
    caption: str | None = None
    media_bytes: bytes | None = None
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None
    user_mode: str = "chat"


@dataclass(slots=True)
class MiraMediaPart:
    media_type: str
    data: bytes
    file_name: str | None = None
    mime_type: str | None = None
    source_message_id: int | None = None


@dataclass(slots=True)
class MiraResponse:
    text_blocks: list[str] = field(default_factory=list)
    media_parts: list[MiraMediaPart] = field(default_factory=list)
    source_message_ids: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        chunks = [part.strip() for part in self.text_blocks if part and part.strip()]
        return "\n\n".join(chunks).strip()

    def as_json(self) -> dict[str, Any]:
        return {
            "text_blocks": self.text_blocks,
            "media_parts": [
                {
                    "media_type": part.media_type,
                    "file_name": part.file_name,
                    "mime_type": part.mime_type,
                    "size": len(part.data),
                    "source_message_id": part.source_message_id,
                }
                for part in self.media_parts
            ],
            "source_message_ids": self.source_message_ids,
        }
