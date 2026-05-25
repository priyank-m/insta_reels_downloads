from pydantic import BaseModel


class CryptoRequest(BaseModel):
    value: str


class CryptoResponseData(BaseModel):
    value: str

