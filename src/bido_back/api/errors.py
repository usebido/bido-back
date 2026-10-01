"""Response models shared by every router."""

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    detail: str
