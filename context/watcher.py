import threading
import time
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

WATCHED_EXTENSIONS = {'.py', '.cpp', '.h', '.js', '.ts', '.md', '.txt', '.json', '.yaml', '.yml'}

EXCLUDED_DIRS = {'__pycache__', '.git', 'venv', 'node_modules', '.env', 'build', 'dist', '.idea', '.vscode', 'env', 'Lib', 'site-packages'}

class ChangeHandler(FileSystemEventHandler):
    def __init__(self, changed_files: set, lock: threading.Lock):
        self.changed_files = changed_files
        self.lock = lock

    def _handle(self, path: str):
        p = Path(path)
        if p.suffix not in WATCHED_EXTENSIONS:
            return
        for part in p.parts:
            if part in EXCLUDED_DIRS:
                return
        with self.lock:
            self.changed_files.add(str(p))

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            with self.lock:
                self.changed_files.add(f"DELETED:{event.src_path}")


class FileWatcher:
    def __init__(self, watch_dir: str, on_change_callback):
        self.watch_dir = watch_dir
        self.on_change_callback = on_change_callback
        self.changed_files = set()
        self.lock = threading.Lock()
        self.observer = Observer()
        self._running = False

    def start(self):
        handler = ChangeHandler(self.changed_files, self.lock)
        self.observer.schedule(handler, self.watch_dir, recursive=True)
        self.observer.start()
        self._running = True
        self._debounce_thread = threading.Thread(target=self._debounce_loop, daemon=True)
        self._debounce_thread.start()

    def _debounce_loop(self):
        while self._running:
            time.sleep(2)
            with self.lock:
                if not self.changed_files:
                    continue
                batch = set(self.changed_files)
                self.changed_files.clear()
            self.on_change_callback(batch)

    def stop(self):
        self._running = False
        self.observer.stop()
        self.observer.join()