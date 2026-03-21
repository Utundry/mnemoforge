from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CodeChunk:
    content: str
    source_file: str
    symbol: str
    chunk_type: str
    language: str
    imports: list[str] = None  # module-level import names (Python only)

    def __post_init__(self):
        if self.imports is None:
            self.imports = []


SUPPORTED_CODE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".md": "markdown",
    ".txt": "text",
    ".rst": "rst",
}


def _safe_read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_imports(tree: ast.Module) -> list[str]:
    """Collect top-level imported module/symbol names."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                names.append(alias.asname or f"{module}.{alias.name}" if module else alias.name)
    return names


def _python_chunks(path: Path) -> list[CodeChunk]:
    text = _safe_read(path)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [
            CodeChunk(
                content=text[:4000],
                source_file=str(path),
                symbol=path.stem,
                chunk_type="file",
                language="python",
            )
        ]

    module_imports = _extract_imports(tree)
    lines = text.splitlines()
    chunks: list[CodeChunk] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = max(getattr(node, "lineno", 1) - 1, 0)
            end = getattr(node, "end_lineno", None) or min(start + 80, len(lines))
            snippet = "\n".join(lines[start:end]).strip()
            if not snippet:
                continue
            chunks.append(
                CodeChunk(
                    content=snippet[:4000],
                    source_file=str(path),
                    symbol=node.name,
                    chunk_type="class" if isinstance(node, ast.ClassDef) else "function",
                    language="python",
                    imports=module_imports,
                )
            )

    if not chunks:
        chunks.append(
            CodeChunk(
                content=text[:4000],
                source_file=str(path),
                symbol=path.stem,
                chunk_type="file",
                language="python",
                imports=module_imports,
            )
        )
    return chunks


def _markdown_chunks(path: Path) -> list[CodeChunk]:
    text = _safe_read(path)
    parts = re.split(r"^#{1,3}\s+", text, flags=re.MULTILINE)
    headings = re.findall(r"^#{1,3}\s+(.+)", text, flags=re.MULTILINE)
    chunks: list[CodeChunk] = []
    if headings and len(parts) > 1:
        for heading, body in zip(headings, parts[1:]):
            body = body.strip()
            if body:
                chunks.append(
                    CodeChunk(
                        content=body[:4000],
                        source_file=str(path),
                        symbol=heading.strip(),
                        chunk_type="section",
                        language="markdown",
                    )
                )
    if not chunks and text.strip():
        chunks.append(
            CodeChunk(
                content=text[:4000],
                source_file=str(path),
                symbol=path.stem,
                chunk_type="file",
                language="markdown",
            )
        )
    return chunks


# Matches top-level function/class/arrow-function declarations in JS/TS
_JS_DECL_RE = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function[\s*]+(\w+)"      # function foo
    r"|^(?:export\s+(?:default\s+)?)?class\s+(\w+)"                         # class Foo
    r"|^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?"       # const foo =
    r"(?:function\b|\([^)]*\)\s*=>)",
    re.MULTILINE,
)

# Matches RST section headings: title line followed by underline of = - ~ ^ ' " # * +
_RST_HEADING_RE = re.compile(
    r"^(.+)\n([=\-~^'\"#*+`]{3,})\n",
    re.MULTILINE,
)


def _js_chunks(path: Path, language: str) -> list[CodeChunk]:
    text = _safe_read(path)
    lines = text.splitlines()
    matches = list(_JS_DECL_RE.finditer(text))
    if not matches:
        return _generic_chunks(path, language)

    chunks: list[CodeChunk] = []
    for i, match in enumerate(matches):
        line_start = text[: match.start()].count("\n")
        if i + 1 < len(matches):
            line_end = min(text[: matches[i + 1].start()].count("\n"), line_start + 100)
        else:
            line_end = min(line_start + 100, len(lines))

        symbol = match.group(1) or match.group(2) or match.group(3) or path.stem
        snippet = "\n".join(lines[line_start:line_end]).strip()
        if not snippet:
            continue
        chunks.append(
            CodeChunk(
                content=snippet[:4000],
                source_file=str(path),
                symbol=symbol,
                chunk_type="class" if match.group(2) else "function",
                language=language,
            )
        )

    return chunks or _generic_chunks(path, language)


def _rst_chunks(path: Path) -> list[CodeChunk]:
    text = _safe_read(path)
    matches = list(_RST_HEADING_RE.finditer(text))
    chunks: list[CodeChunk] = []
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            chunks.append(
                CodeChunk(
                    content=body[:4000],
                    source_file=str(path),
                    symbol=heading,
                    chunk_type="section",
                    language="text",
                )
            )
    if not chunks and text.strip():
        chunks.append(
            CodeChunk(
                content=text[:4000],
                source_file=str(path),
                symbol=path.stem,
                chunk_type="file",
                language="text",
            )
        )
    return chunks


def _generic_chunks(path: Path, language: str) -> list[CodeChunk]:
    text = _safe_read(path)
    if not text.strip():
        return []
    return [
        CodeChunk(
            content=text[:4000],
            source_file=str(path),
            symbol=path.stem,
            chunk_type="file",
            language=language,
        )
    ]


def parse_code_file(path: Path) -> list[CodeChunk]:
    language = SUPPORTED_CODE_EXTENSIONS.get(path.suffix.lower())
    if language is None:
        return []
    if language == "python":
        return _python_chunks(path)
    if language == "markdown":
        return _markdown_chunks(path)
    if language in ("javascript", "typescript"):
        return _js_chunks(path, language)
    if language == "rst":
        return _rst_chunks(path)
    return _generic_chunks(path, language)


def scan_code_directory(root: Path, extensions: list[str] | None = None, recursive: bool = True) -> list[Path]:
    selected = {
        (ext if ext.startswith(".") else f".{ext}").lower()
        for ext in (extensions or list(SUPPORTED_CODE_EXTENSIONS))
    }
    pattern = "**/*" if recursive else "*"
    files: list[Path] = []
    for ext in selected:
        files.extend(root.glob(f"{pattern}{ext}"))
    return sorted({p for p in files if p.is_file()})
