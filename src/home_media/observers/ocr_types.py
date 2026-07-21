"""OCR token geometry — portable, independent of Vision / Netflix anchors."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OcrToken(BaseModel):
    """One OCR token with normalized top-left box in [0, 1]."""

    text: str
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def normalized_text(self) -> str:
        return " ".join(self.text.split()).strip().lower()


class OcrDocument(BaseModel):
    tokens: list[OcrToken] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None
