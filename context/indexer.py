import os
from pathlib import Path
from context.parser import parse_file
from context.embedder import Embedder
from context.watcher import WATCHED_EXTENSIONS, EXCLUDED_DIRS

EXCLUDED_FILES = {'package-lock.json', 'yarn.lock', 'poetry.lock', '.gitignore'}
PROJECTS_DIR = r"C:\Users\adnan\Documents\repos"
import json

INDEXED_CACHE_PATH = r"C:\kamil\data\indexed_projects.json"

class Indexer:
    def __init__(self):
        self.embedder = Embedder()
        self.current_project = None

    def _load_indexed_cache(self) -> set:
        try:
            with open(INDEXED_CACHE_PATH) as f:
                return set(json.load(f))
        except:
            return set()

    def _save_indexed_cache(self, project_name: str):
        cache = self._load_indexed_cache()
        cache.add(project_name)
        os.makedirs(os.path.dirname(INDEXED_CACHE_PATH), exist_ok=True)
        with open(INDEXED_CACHE_PATH, 'w') as f:
            json.dump(list(cache), f)

    def _remove_from_indexed_cache(self, project_name: str):
        cache = self._load_indexed_cache()
        cache.discard(project_name)
        with open(INDEXED_CACHE_PATH, 'w') as f:
            json.dump(list(cache), f)

    def is_indexed(self, project_name: str) -> bool:
        if project_name not in self._load_indexed_cache():
            return False
        try:
            collection = self.embedder.get_or_create_collection(project_name)
            count = collection.count()
            if count == 0:
                return False
            # Quick sanity check - try a test query
            collection.query(query_embeddings=[[0.1]*384], n_results=1)
            return True
        except Exception:
            # Collection is corrupted - remove from cache so it reindexes
            self._remove_from_indexed_cache(project_name)
            return False

    def list_projects(self) -> list[str]:
        projects = []
        base = Path(PROJECTS_DIR)
        for item in sorted(base.iterdir()):
            if item.is_dir() and item.name not in EXCLUDED_DIRS and not item.name.startswith('.'):
                projects.append(item.name)
        return projects

    def index_project(self, project_name: str, status_callback=None) -> int:
        project_path = Path(PROJECTS_DIR) / project_name
        if not project_path.exists():
            raise ValueError(f"Project not found: {project_name}")

        self.current_project = project_name
        total_chunks = 0
        files_indexed = 0

        all_files = []
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith('.')]
            for file in files:
                fpath = Path(root) / file
                if fpath.suffix.lower() in WATCHED_EXTENSIONS and fpath.name not in EXCLUDED_FILES:
                    all_files.append(str(fpath))
        for i, file_path in enumerate(all_files):
            chunks = parse_file(file_path)
            if chunks:
                self.embedder.index_chunks(project_name, chunks)
                total_chunks += len(chunks)
                files_indexed += 1
            if status_callback:
                status_callback(i + 1, len(all_files), file_path)
        self._save_indexed_cache(project_name)

        return total_chunks

    def reindex_file(self, project_name: str, file_path: str):
        if file_path.startswith("DELETED:"):
            actual_path = file_path[8:]
            self.embedder.delete_file(project_name, actual_path)
            return
        chunks = parse_file(file_path)
        if chunks:
            self.embedder.delete_file(project_name, file_path)
            self.embedder.index_chunks(project_name, chunks)

    def query(self, query_text: str, n_results: int = 8) -> list[dict]:
        if not self.current_project:
            return []
        return self.embedder.query(self.current_project, query_text, n_results)

    def start_watching(self):
        from context.watcher import FileWatcher
        def on_change(changed_files: set):
            if not self.current_project:
                return
            project_path = str(Path(PROJECTS_DIR) / self.current_project).lower()
            for file_path in changed_files:
                if file_path.lower().startswith(project_path):
                    self.reindex_file(self.current_project, file_path)
        self.watcher = FileWatcher(PROJECTS_DIR, on_change)
        self.watcher.start()

    def stop_watching(self):
        if hasattr(self, 'watcher'):
            self.watcher.stop()