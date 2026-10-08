"""Response models shared by every router."""

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    detail: str
    request_id: str | None = Field(
        default=None,
        description="Names this request in the api's logs; quote it when reporting the error.",
    )
