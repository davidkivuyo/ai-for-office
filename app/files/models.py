from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DocumentTable:
    """Normalized table representation. Preserves source location for citations."""

    name: str | None = None  # sheet name, or section title
    headers: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    source: str | None = None  # e.g., "Sheet: July Rows: 14-22" or "Page: 7"
    # raw cell values as strings; formula handling via openpyxl if needed
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def col_count(self) -> int:
        return len(self.headers) if self.headers else (len(self.rows[0]) if self.rows else 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "headers": self.headers,
            "rows": self.rows,
            "source": self.source,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentTable:
        return cls(
            name=data.get("name"),
            headers=data.get("headers", []),
            rows=data.get("rows", []),
            source=data.get("source"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class DocumentSection:
    """Section for DOCX — heading + content with source citation."""

    title: str
    content: str
    source: str | None = None  # e.g., "Section: Financial Results"


@dataclass
class DocumentContent:
    """Normalized document representation per AGENTS §6.

    Application code must not depend directly on one parser's output, so every
    extractor normalizes into this structure.
    """

    filename: str
    file_type: str  # txt, md, csv, xlsx, docx, pdf
    text: str = ""
    tables: list[DocumentTable] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)  # for xlsx
    pages: int | None = None  # for pdf
    sections: list[DocumentSection] = field(default_factory=list)  # for docx
    metadata: dict[str, Any] = field(default_factory=dict)
    # Derived sizing
    token_estimate: int = 0
    size_category: str = "small"  # small | medium | large per §8

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "file_type": self.file_type,
            "text": self.text,
            "tables": [t.to_dict() for t in self.tables],
            "sheets": self.sheets,
            "pages": self.pages,
            "sections": [{"title": s.title, "content": s.content, "source": s.source} for s in self.sections],
            "metadata": self.metadata,
            "token_estimate": self.token_estimate,
            "size_category": self.size_category,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentContent:
        tables = [DocumentTable.from_dict(t) for t in data.get("tables", [])]
        sections = [
            DocumentSection(title=s.get("title", ""), content=s.get("content", ""), source=s.get("source"))
            for s in data.get("sections", [])
        ]
        return cls(
            filename=data.get("filename", ""),
            file_type=data.get("file_type", ""),
            text=data.get("text", ""),
            tables=tables,
            sheets=data.get("sheets", []),
            pages=data.get("pages"),
            sections=sections,
            metadata=data.get("metadata", {}),
            token_estimate=data.get("token_estimate", 0),
            size_category=data.get("size_category", "small"),
        )
