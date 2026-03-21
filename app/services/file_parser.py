"""
Parse local files (Markdown, plain text, etc.) into chunks suitable for memory storage.
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ParsedChunk:
    content: str
    source_file: str
    heading: Optional[str] = None
    tags: list[str] = field(default_factory=list)


def _split_by_headings(text: str, min_chunk_len: int = 30) -> list[tuple[Optional[str], str]]:
    """Split Markdown text by H1/H2/H3 headings. Returns list of (heading, body)."""
    pattern = re.compile(r"^(#{1,3})\s+(.+)", re.MULTILINE)
    chunks: list[tuple[Optional[str], str]] = []
    last_heading: Optional[str] = None
    last_end = 0

    for m in pattern.finditer(text):
        body = text[last_end: m.start()].strip()
        if body and len(body) >= min_chunk_len:
            chunks.append((last_heading, body))
        last_heading = m.group(2).strip()
        last_end = m.end()

    remainder = text[last_end:].strip()
    if remainder and len(remainder) >= min_chunk_len:
        chunks.append((last_heading, remainder))

    return chunks


def _split_by_paragraphs(text: str, min_chunk_len: int = 30) -> list[str]:
    """Split plain text by double newlines (paragraphs)."""
    return [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) >= min_chunk_len]


def parse_markdown(path: Path) -> list[ParsedChunk]:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Extract YAML front-matter tags if present
    tags: list[str] = []
    fm_match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        tag_match = re.search(r"tags:\s*\[([^\]]+)\]", fm)
        if tag_match:
            tags = [t.strip().strip('"').strip("'") for t in tag_match.group(1).split(",")]
        text = text[fm_match.end():]

    chunks = _split_by_headings(text)
    if not chunks:
        # Fallback: no headings → paragraph split
        return [
            ParsedChunk(content=p, source_file=str(path), tags=tags)
            for p in _split_by_paragraphs(text)
        ]

    return [
        ParsedChunk(content=body, source_file=str(path), heading=heading, tags=tags)
        for heading, body in chunks
    ]


def parse_text(path: Path) -> list[ParsedChunk]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [
        ParsedChunk(content=p, source_file=str(path))
        for p in _split_by_paragraphs(text)
    ]


PARSERS = {
    ".md": parse_markdown,
    ".markdown": parse_markdown,
    ".txt": parse_text,
    ".rst": parse_text,
}


def parse_file(path: Path) -> list[ParsedChunk]:
    suffix = path.suffix.lower()
    parser = PARSERS.get(suffix)
    if parser is None:
        logger.warning("No parser for '%s', skipping", path)
        return []
    try:
        return parser(path)
    except Exception as e:
        logger.error("Failed to parse '%s': %s", path, e)
        return []


def scan_directory(
    root: Path,
    extensions: Optional[list[str]] = None,
    recursive: bool = True,
) -> list[Path]:
    """Return all files under root with the given extensions."""
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (extensions or list(PARSERS))}
    pattern = "**/*" if recursive else "*"
    files = []
    for ext in exts:
        files.extend(root.glob(f"{pattern}{ext}"))
    return sorted(set(files))
