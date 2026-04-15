"""Token and device-code response models."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel


class TokenSet(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime  # UTC
    token_type: str = "Bearer"

    @property
    def is_expired(self) -> bool:
        # 30-second buffer so we refresh before actual expiry.
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(seconds=30)


class DeviceCodeResponse(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str = ""
    expires_in: int
    interval: int = 5
