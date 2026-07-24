from pydantic import BaseModel


class Category(BaseModel):
    name: str
    path: str
