"""
Symbol-level code indexer for Kamil
Symbol-level code indexer. Replaces chunk-based indexing with:
1. Per-symbol embeddings (class, function, method) with full source
2. Import graph for dependency-aware retrieval
3. Project architecture summary
4. File-level metadata

This gives Kamil Claude Code-level context awareness.
"""

import ast
import os
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


# ── Data structures ────────────────────────────────────────────────

@dataclass
class SymbolInfo:
    name: str
    kind: str          # "class" | "function" | "method" | "module"
    filepath: str
    lineno: int
    end_lineno: int
    source: str        # full source of this symbol
    docstring: str
    parent_class: str  # for methods
    signature: str     # def foo(x: int, y: str) -> bool
    decorators: list[str]
    file_hash: str     # to detect changes


@dataclass
class FileInfo:
    filepath: str
    module_name: str
    imports: list[str]          # what this file imports
    imported_by: list[str]      # what files import this file
    classes: list[str]
    functions: list[str]
    line_count: int
    summary: str                # one-line description from module docstring
    file_hash: str


@dataclass 
class ProjectArchitecture:
    project: str
    files: dict[str, FileInfo]  # filepath -> FileInfo
    import_graph: dict[str, list[str]]  # file -> files it imports
    reverse_graph: dict[str, list[str]] # file -> files that import it
    summary: str                # generated project overview


# ── AST extraction ────────────────────────────────────────────────

def _get_source_segment(source_lines: list[str], start: int, end: int) -> str:
    """Extract source lines (1-indexed)."""
    return "\n".join(source_lines[start-1:end])


def _get_signature(node: ast.FunctionDef, source_lines: list[str]) -> str:
    """Extract function signature line."""
    line = source_lines[node.lineno - 1].strip()
    # If signature spans multiple lines, grab until we hit the colon
    if not line.endswith(":"):
        for i in range(node.lineno, min(node.lineno + 5, len(source_lines))):
            line += " " + source_lines[i].strip()
            if line.rstrip().endswith(":"):
                break
    return line


def _get_decorators(node) -> list[str]:
    """Extract decorator names."""
    decorators = []
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Name):
            decorators.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            decorators.append(f"{dec.value.id}.{dec.attr}")
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                decorators.append(dec.func.id)
    return decorators


def extract_symbols(filepath: str) -> tuple[list[SymbolInfo], FileInfo]:
    """
    Parse a Python file and extract all symbols with full source.
    Returns (symbols, file_info).
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except Exception:
        return [], None

    file_hash = hashlib.md5(source.encode()).hexdigest()
    source_lines = source.splitlines()

    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return [], None

    module_name = Path(filepath).stem
    symbols: list[SymbolInfo] = []
    classes: list[str] = []
    functions: list[str] = []
    imports: list[str] = []

    # Module docstring
    module_doc = ast.get_docstring(tree) or ""
    module_summary = module_doc.split("\n")[0][:120] if module_doc else ""

    # Extract imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    # Extract top-level classes and functions
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
            class_doc = ast.get_docstring(node) or ""
            class_source = _get_source_segment(source_lines, node.lineno, node.end_lineno)

            # Class-level symbol
            symbols.append(SymbolInfo(
                name=node.name,
                kind="class",
                filepath=filepath,
                lineno=node.lineno,
                end_lineno=node.end_lineno,
                source=class_source,
                docstring=class_doc,
                parent_class="",
                signature=f"class {node.name}",
                decorators=_get_decorators(node),
                file_hash=file_hash
            ))

            # Methods within class
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    method_doc = ast.get_docstring(item) or ""
                    method_source = _get_source_segment(source_lines, item.lineno, item.end_lineno)
                    sig = _get_signature(item, source_lines)

                    symbols.append(SymbolInfo(
                        name=item.name,
                        kind="method",
                        filepath=filepath,
                        lineno=item.lineno,
                        end_lineno=item.end_lineno,
                        source=method_source,
                        docstring=method_doc,
                        parent_class=node.name,
                        signature=sig,
                        decorators=_get_decorators(item),
                        file_hash=file_hash
                    ))

        elif isinstance(node, ast.FunctionDef):
            functions.append(node.name)
            func_doc = ast.get_docstring(node) or ""
            func_source = _get_source_segment(source_lines, node.lineno, node.end_lineno)
            sig = _get_signature(node, source_lines)

            symbols.append(SymbolInfo(
                name=node.name,
                kind="function",
                filepath=filepath,
                lineno=node.lineno,
                end_lineno=node.end_lineno,
                source=func_source,
                docstring=func_doc,
                parent_class="",
                signature=sig,
                decorators=_get_decorators(node),
                file_hash=file_hash
            ))

    file_info = FileInfo(
        filepath=filepath,
        module_name=module_name,
        imports=list(set(imports)),
        imported_by=[],  # filled in later
        classes=classes,
        functions=functions,
        line_count=len(source_lines),
        summary=module_summary,
        file_hash=file_hash
    )

    return symbols, file_info


# ── Project indexer ───────────────────────────────────────────────

class SymbolIndex:
    """
    Symbol-level index for a project.
    Stored in ChromaDB collection: {project}_symbols
    Also maintains import graph on disk.
    """

    SUPPORTED_EXTENSIONS = {".py"}
    SKIP_DIRS = {"__pycache__", ".git", "venv", "venv313", "node_modules",
                 ".venv", "dist", "build", ".pytest_cache"}
    SKIP_FILES = {"__init__.py", "setup.py", "conftest.py"}

    def __init__(self, project_root: str, project_name: str):
        self.project_root = project_root
        self.project_name = project_name
        self.collection_name = f"{project_name}_symbols"
        self.arch_path = os.path.join(
            r"C:\kamil\data\projects",
            f"{project_name}_architecture.json"
        )
        self._arch: Optional[ProjectArchitecture] = None

        # Lazy imports to avoid circular dependency
        self._embedder = None
        self._collection = None

    def _get_embedder(self):
        if self._embedder is None:
            from context.embedder import Embedder
            self._embedder = Embedder()
        return self._embedder

    def _get_collection(self, create=False):
        if self._collection is None:
            from context.embedder import get_chroma_client
            client = get_chroma_client()
            if create:
                try:
                    client.delete_collection(self.collection_name)
                except Exception:
                    pass
                self._collection = client.create_collection(
                    name=self.collection_name,
                    metadata={"hnsw:space": "cosine"}
                )
            else:
                try:
                    self._collection = client.get_collection(self.collection_name)
                except Exception:
                    return None
        return self._collection

    def _walk_project(self) -> list[str]:
        """Get all Python files in project."""
        py_files = []
        for root, dirs, files in os.walk(self.project_root):
            # Skip unwanted dirs in-place
            dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
            for f in files:
                if f in self.SKIP_FILES:
                    continue
                if any(f.endswith(ext) for ext in self.SUPPORTED_EXTENSIONS):
                    py_files.append(os.path.join(root, f))
        return py_files

    def _build_import_graph(self, file_infos: dict[str, FileInfo]) -> dict[str, list[str]]:
        """
        Build import graph: filepath -> list of filepaths it imports.
        Resolves relative imports where possible.
        """
        graph = {fp: [] for fp in file_infos}
        module_to_file = {fi.module_name: fp for fp, fi in file_infos.items()}

        for filepath, fi in file_infos.items():
            for imp in fi.imports:
                # Try to resolve to a file in the project
                imp_base = imp.split(".")[-1]
                if imp_base in module_to_file:
                    target = module_to_file[imp_base]
                    if target != filepath:
                        graph[filepath].append(target)

        return graph

    def _build_reverse_graph(self, graph: dict[str, list[str]]) -> dict[str, list[str]]:
        reverse = {fp: [] for fp in graph}
        for fp, deps in graph.items():
            for dep in deps:
                if dep in reverse:
                    reverse[dep].append(fp)
        return reverse

    def _generate_project_summary(self, file_infos: dict[str, FileInfo]) -> str:
        """Generate a human-readable project architecture summary."""
        lines = [f"# {self.project_name} — Architecture Summary\n"]
        lines.append(f"Total files: {len(file_infos)}")
        total_lines = sum(fi.line_count for fi in file_infos.values())
        lines.append(f"Total lines: {total_lines:,}\n")

        lines.append("## Files")
        for fp, fi in sorted(file_infos.items(), key=lambda x: x[1].module_name):
            rel = os.path.relpath(fp, self.project_root)
            class_str = f" | classes: {', '.join(fi.classes)}" if fi.classes else ""
            func_str = f" | functions: {', '.join(fi.functions[:5])}" if fi.functions else ""
            summary_str = f" — {fi.summary}" if fi.summary else ""
            lines.append(f"- `{rel}` ({fi.line_count} lines){summary_str}{class_str}{func_str}")

        return "\n".join(lines)

    def index(self, force: bool = False) -> int:
        """
        Index all Python files in the project at symbol level.
        Returns total symbols indexed.
        """
        print(f"[SymbolIndex] Indexing {self.project_name} at {self.project_root}")
        py_files = self._walk_project()
        print(f"[SymbolIndex] Found {len(py_files)} Python files")

        all_symbols: list[SymbolInfo] = []
        file_infos: dict[str, FileInfo] = {}

        for filepath in py_files:
            symbols, file_info = extract_symbols(filepath)
            if file_info:
                all_symbols.extend(symbols)
                file_infos[filepath] = file_info

        print(f"[SymbolIndex] Extracted {len(all_symbols)} symbols")

        # Build import graph
        import_graph = self._build_import_graph(file_infos)
        reverse_graph = self._build_reverse_graph(import_graph)

        # Update imported_by
        for filepath, deps in reverse_graph.items():
            if filepath in file_infos:
                file_infos[filepath].imported_by = [
                    os.path.basename(d) for d in deps
                ]

        # Generate architecture summary
        arch_summary = self._generate_project_summary(file_infos)

        # Build architecture object
        self._arch = ProjectArchitecture(
            project=self.project_name,
            files=file_infos,
            import_graph=import_graph,
            reverse_graph=reverse_graph,
            summary=arch_summary
        )

        # Save architecture to disk
        os.makedirs(os.path.dirname(self.arch_path), exist_ok=True)
        arch_dict = {
            "project": self._arch.project,
            "summary": self._arch.summary,
            "import_graph": {
                os.path.basename(k): [os.path.basename(v) for v in vs]
                for k, vs in import_graph.items()
            },
            "files": {
                os.path.relpath(fp, self.project_root): {
                    "module": fi.module_name,
                    "classes": fi.classes,
                    "functions": fi.functions,
                    "imports": fi.imports,
                    "imported_by": fi.imported_by,
                    "lines": fi.line_count,
                    "summary": fi.summary
                }
                for fp, fi in file_infos.items()
            }
        }
        with open(self.arch_path, "w", encoding="utf-8") as f:
            json.dump(arch_dict, f, indent=2)
        print(f"[SymbolIndex] Architecture saved to {self.arch_path}")

        # Index symbols into ChromaDB
        collection = self._get_collection(create=True)
        embedder = self._get_embedder()

        batch_size = 50
        indexed = 0
        for i in range(0, len(all_symbols), batch_size):
            batch = all_symbols[i:i + batch_size]

            # Build rich text for embedding
            texts = []
            for sym in batch:
                # Include signature, docstring, and source for rich embedding
                text_parts = [
                    f"File: {os.path.basename(sym.filepath)}",
                    f"{sym.kind.upper()}: {sym.parent_class + '.' if sym.parent_class else ''}{sym.name}",
                ]
                if sym.docstring:
                    text_parts.append(f"Description: {sym.docstring[:200]}")
                text_parts.append(f"Signature: {sym.signature}")
                text_parts.append(sym.source[:1500])  # full source up to 1500 chars
                texts.append("\n".join(text_parts))

            ids = [f"sym_{i + j}" for j in range(len(batch))]
            metadatas = [{
                "name": sym.name,
                "kind": sym.kind,
                "filepath": sym.filepath,
                "lineno": sym.lineno,
                "end_lineno": sym.end_lineno,
                "parent_class": sym.parent_class,
                "signature": sym.signature,
                "module": os.path.basename(sym.filepath).replace(".py", ""),
                "docstring": sym.docstring[:200],
            } for sym in batch]

            # Store full source separately (ChromaDB document)
            documents = [sym.source for sym in batch]

            embeddings = embedder.model.encode(texts).tolist()

            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas
            )
            indexed += len(batch)

        print(f"[SymbolIndex] Indexed {indexed} symbols into '{self.collection_name}'")
        return indexed

    def query_symbols(self, query: str, n_results: int = 8) -> list[dict]:
        """
        Query symbols by semantic similarity.
        Returns list of symbol dicts with full source.
        """
        collection = self._get_collection()
        if not collection:
            return []

        embedder = self._get_embedder()
        query_embedding = embedder.model.encode([query]).tolist()

        try:
            results = collection.query(
                query_embeddings=query_embedding,
                n_results=min(n_results, collection.count()),
                include=["documents", "metadatas", "distances"]
            )
        except Exception:
            return []

        output = []
        for i in range(len(results["ids"][0])):
            meta = results["metadatas"][0][i]
            output.append({
                "name": meta["name"],
                "kind": meta["kind"],
                "parent_class": meta["parent_class"],
                "filepath": meta["filepath"],
                "module": meta["module"],
                "lineno": meta["lineno"],
                "signature": meta["signature"],
                "docstring": meta["docstring"],
                "source": results["documents"][0][i],
                "score": round(1 - results["distances"][0][i], 3)
            })

        return sorted(output, key=lambda x: x["score"], reverse=True)

    def get_file_symbols(self, filepath: str) -> list[dict]:
        """Get all symbols from a specific file."""
        collection = self._get_collection()
        if not collection:
            return []

        try:
            results = collection.get(
                where={"filepath": filepath},
                include=["documents", "metadatas"]
            )
            output = []
            for i in range(len(results["ids"])):
                meta = results["metadatas"][i]
                output.append({
                    "name": meta["name"],
                    "kind": meta["kind"],
                    "parent_class": meta["parent_class"],
                    "lineno": meta["lineno"],
                    "signature": meta["signature"],
                    "source": results["documents"][i]
                })
            return sorted(output, key=lambda x: x["lineno"])
        except Exception:
            return []

    def get_dependencies(self, filepath: str) -> list[str]:
        """Get files that this file imports (direct dependencies)."""
        if not self._arch:
            self._load_arch()
        if not self._arch:
            return []
        return self._arch.import_graph.get(filepath, [])

    def get_dependents(self, filepath: str) -> list[str]:
        """Get files that import this file."""
        if not self._arch:
            self._load_arch()
        if not self._arch:
            return []
        return self._arch.reverse_graph.get(filepath, [])

    def get_architecture_summary(self) -> str:
        """Get the project architecture summary string."""
        if not self._arch:
            self._load_arch()
        if self._arch:
            return self._arch.summary
        # Try loading from disk
        if os.path.exists(self.arch_path):
            with open(self.arch_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("summary", "")
        return ""

    def _load_arch(self):
        """Load architecture from disk."""
        if os.path.exists(self.arch_path):
            try:
                with open(self.arch_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Reconstruct partial arch for graph queries
                self._arch = ProjectArchitecture(
                    project=data["project"],
                    files={},
                    import_graph={},
                    reverse_graph={},
                    summary=data.get("summary", "")
                )
            except Exception:
                pass

    def build_context_for_query(self, query: str, max_tokens: int = 8000) -> str:
        import re
        parts = []

        # Architecture summary always included
        arch = self.get_architecture_summary()
        if arch:
            parts.append(arch)

        # Detect explicit file reference: "in ekf.py", "ekf.py"
        file_match = re.search(r'(\w+)\.py', query, re.IGNORECASE)
        # Detect explicit class reference: "EKF class", "class EKF"
        class_match = re.search(r'\b([A-Z][a-zA-Z]+)\s+class|class\s+([A-Z][a-zA-Z]+)', query)

        exact_sections = []

        if file_match:
            module_name = file_match.group(1).lower()
            symbols = self.get_module_symbols(module_name)
            if symbols:
                lines = [f"\n### {module_name}.py — ALL SYMBOLS"]
                for sym in symbols:
                    qual = f"{sym['parent_class']}.{sym['name']}" if sym['parent_class'] else sym['name']
                    lines.append(f"\n#### {sym['kind'].upper()}: {qual} (line {sym['lineno']})")
                    if sym['docstring']:
                        lines.append(f'"""{sym["docstring"]}"""')
                    lines.append("```python")
                    lines.append(sym['source'][:2000])
                    lines.append("```")
                exact_sections.append("\n".join(lines))

        if class_match:
            class_name = class_match.group(1) or class_match.group(2)
            methods = self.get_class_methods(class_name)
            if methods:
                lines = [f"\n### {class_name} — ALL METHODS"]
                for m in methods:
                    lines.append(f"\n#### {m['kind'].upper()}: {class_name}.{m['name']} (line {m['lineno']})")
                    if m['docstring']:
                        lines.append(f'"""{m["docstring"]}"""')
                    lines.append("```python")
                    lines.append(m['source'][:1500])
                    lines.append("```")
                exact_sections.append("\n".join(lines))

        if exact_sections:
            parts.append("\n## Exact Matches\n" + "\n".join(exact_sections))

        # Semantic search for additional context
        symbols = self.query_symbols(query, n_results=6)
        # Filter out already shown symbols
        shown = set()
        if file_match:
            shown.add(file_match.group(1).lower())

        remaining = [s for s in symbols if s['module'] not in shown]
        if remaining:
            by_file: dict[str, list] = {}
            for sym in remaining:
                by_file.setdefault(sym['filepath'], []).append(sym)

            sem_sections = []
            for filepath, syms in by_file.items():
                filename = os.path.basename(filepath)
                section_lines = [f"\n### {filename} (semantic match)"]
                for sym in sorted(syms, key=lambda x: x['lineno']):
                    qual = f"{sym['parent_class']}.{sym['name']}" if sym['parent_class'] else sym['name']
                    section_lines.append(f"\n#### {sym['kind']}: {qual} (score {sym['score']})")
                    section_lines.append("```python")
                    section_lines.append(sym['source'][:1000])
                    section_lines.append("```")
                sem_sections.append("\n".join(section_lines))

            if sem_sections:
                parts.append("\n## Related Code\n" + "\n".join(sem_sections))

        return "\n\n".join(parts)
    def is_indexed(self) -> bool:
        """Check if this project has been indexed."""
        collection = self._get_collection()
        if not collection:
            return False
        try:
            return collection.count() > 0
        except Exception:
            return False

    def get_class_methods(self, class_name: str) -> list[dict]:
        """Get all methods of a specific class by exact name match."""
        collection = self._get_collection()
        if not collection:
            return []
        try:
            results = collection.get(
                where={"parent_class": class_name},
                include=["documents", "metadatas"]
            )
            output = []
            for i in range(len(results["ids"])):
                meta = results["metadatas"][i]
                output.append({
                    "name": meta["name"],
                    "kind": meta["kind"],
                    "lineno": meta["lineno"],
                    "signature": meta["signature"],
                    "docstring": meta["docstring"],
                    "source": results["documents"][i]
                })
            return sorted(output, key=lambda x: x["lineno"])
        except Exception as e:
            print(f"get_class_methods error: {e}")
            return []

    def get_module_symbols(self, module_name: str) -> list[dict]:
        """Get all symbols from a specific module/file by exact name."""
        collection = self._get_collection()
        if not collection:
            return []
        try:
            results = collection.get(
                where={"module": module_name},
                include=["documents", "metadatas"]
            )
            output = []
            for i in range(len(results["ids"])):
                meta = results["metadatas"][i]
                output.append({
                    "name": meta["name"],
                    "kind": meta["kind"],
                    "parent_class": meta["parent_class"],
                    "lineno": meta["lineno"],
                    "signature": meta["signature"],
                    "docstring": meta["docstring"],
                    "source": results["documents"][i]
                })
            return sorted(output, key=lambda x: x["lineno"])
        except Exception as e:
            print(f"get_module_symbols error: {e}")
            return []


# ── Registry ──────────────────────────────────────────────────────

_symbol_indexes: dict[str, SymbolIndex] = {}


def get_symbol_index(project_name: str, project_root: str) -> SymbolIndex:
    """Get or create a SymbolIndex for a project."""
    if project_name not in _symbol_indexes:
        _symbol_indexes[project_name] = SymbolIndex(project_root, project_name)
    return _symbol_indexes[project_name]