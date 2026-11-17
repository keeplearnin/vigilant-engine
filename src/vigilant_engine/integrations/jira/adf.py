"""Atlassian Document Format (ADF) builders for Jira Cloud REST API v3 descriptions.

ADF rejects empty text nodes, so every builder drops empty strings.
"""

from __future__ import annotations

from typing import Any

Node = dict[str, Any]


def text(value: str, *, bold: bool = False, italic: bool = False) -> Node | None:
    if not value:
        return None
    node: Node = {"type": "text", "text": value}
    marks = ([{"type": "strong"}] if bold else []) + ([{"type": "em"}] if italic else [])
    if marks:
        node["marks"] = marks
    return node


def paragraph(*parts: Node | str | None) -> Node | None:
    content = [text(p) if isinstance(p, str) else p for p in parts]
    content = [c for c in content if c]
    return {"type": "paragraph", "content": content} if content else None


def heading(value: str, level: int = 3) -> Node | None:
    t = text(value)
    return {"type": "heading", "attrs": {"level": level}, "content": [t]} if t else None


def bullet_list(items: list[Node | str]) -> Node | None:
    entries = []
    for item in items:
        para = paragraph(item) if isinstance(item, str) else item
        if para:
            entries.append({"type": "listItem", "content": [para]})
    return {"type": "bulletList", "content": entries} if entries else None


def rule() -> Node:
    return {"type": "rule"}


def doc(*blocks: Node | None) -> Node:
    return {"type": "doc", "version": 1, "content": [b for b in blocks if b]}
