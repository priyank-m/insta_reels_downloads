from typing import Any, Optional

from pydantic import BaseModel


class ApiResponse(BaseModel):
    code: int
    data: Optional[Any] = None
    message: Optional[str] = None

