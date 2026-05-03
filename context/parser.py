import os
import hashlib
from pathlib import Path

try:
    import tree_sitter_python as tspython
    import tree_sitter_cpp as tscpp
    from tree_sitter import Language, Parser as TSParser
    TREE_SITTER_AVAILABLE = True
    PY_LANGUAGE = Language(tspython.language())
    CPP_LANGUAGE = Language(tscpp.language())
except Exception:
    TREE_SITTER_AVAILABLE = False

CHUNK_SIZE = 60        # lines per chunk for fallback chunker
CHUNK_OVERLAP = 10     # overlap between chunks to preserve context

def _chunk_id(file_path: str, start_line: int, label: str) -> str:
    raw = f"{file_path}:{start_line}:{label}"
    return hashlib.md5(raw.encode()).hexdigest()

def _fallback_chunk(file_path: str, lines: list[str]) -> list[dict]:
    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for i in range(0, len(lines), step):
        window = lines[i:i + CHUNK_SIZE]
        text = "".join(window).strip()
        if not text:
            continue
        chunks.append({
            "id": _chunk_id(file_path, i, "chunk"),
            "text": f"# {file_path} (lines {i+1}-{i+len(window)})\n{text}",
            "metadata": {
                "file_path": file_path,
                "start_line": i + 1,
                "end_line": i + len(window),
                "type": "chunk",
                "name": ""
            }
        })
    return chunks

def _extract_nodes(file_path: str, source: bytes, language, node_types: list[str]) -> list[dict]:
    parser = TSParser(language)
    tree = parser.parse(source)
    chunks = []

    def walk(node):
        if node.type in node_types:
            name_node = node.child_by_field_name("name")
            name = name_node.text.decode() if name_node else "unknown"
            text = source[node.start_byte:node.end_byte].decode(errors="replace").strip()
            if len(text) < 10:
                return
            chunks.append({
                "id": _chunk_id(file_path, node.start_point[0], name),
                "text": f"# {file_path} — {node.type} '{name}'\n{text}",
                "metadata": {
                    "file_path": file_path,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                    "type": node.type,
                    "name": name
                }
            })
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return chunks

def parse_file(file_path: str) -> list[dict]:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return []

    try:
        source = path.read_bytes()
        lines = source.decode(errors="replace").splitlines(keepends=True)
    except Exception:
        return []

    if not lines:
        return []

    ext = path.suffix.lower()

    if TREE_SITTER_AVAILABLE:
        if ext == ".py":
            chunks = _extract_nodes(file_path, source, PY_LANGUAGE, ["function_definition", "class_definition"])
            if chunks:
                return chunks
        elif ext in {".cpp", ".h", ".c", ".hpp"}:
            chunks = _extract_nodes(file_path, source, CPP_LANGUAGE, ["function_definition", "class_specifier"])
            if chunks:
                return chunks

    return _fallback_chunk(file_path, lines)