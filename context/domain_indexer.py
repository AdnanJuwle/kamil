"""
context/domain_indexer.py

Indexes domain knowledge books into ChromaDB with rich metadata.
Supports PDF, EPUB, MOBI, CHM formats.
Uses folder structure as domain tag automatically.

Collection: kamil_domain
Folder structure: C:\\kamil\\data\\domain_books\\{domain}\\{book}.{ext}

Metadata schema per chunk:
{
    "domain":        "prana" | "mlops" | "kamil_arch" | "cognitive" | "reliability" | "security"
    "source":        "book title"
    "filename":      "probabilistic_robotics.pdf"
    "knowledge_type": "theory" | "implementation" | "operational" | "failure_case"
    "page":          int
    "chunk_idx":     int
    "is_post_training": True/False (books after 2022 likely not in model weights)
    "year":          int
    "trust_level":   float (0.0-1.0)
}
"""

import os
import re
import sys
import time
sys.path.insert(0, r"C:\kamil")

from context.embedder import get_chroma_client, Embedder

DOMAIN_BOOKS_DIR = r"C:\kamil\data\domain_books"
COLLECTION_NAME  = "kamil_domain"
CHUNK_SIZE       = 800
CHUNK_OVERLAP    = 150
BATCH_SIZE       = 80

# Book metadata — year and trust level per filename stem
BOOK_META = {
    "probabilistic_robotics":           {"year": 2005, "trust": 0.95, "post_training": False},
    "small_unmanned_aircraft":          {"year": 2012, "trust": 0.93, "post_training": False},
    "modern_robotics":                  {"year": 2017, "trust": 0.93, "post_training": False},
    "kalman_bayesian_filters":          {"year": 2020, "trust": 0.92, "post_training": False},
    "realtime_embedded":                {"year": 2003, "trust": 0.88, "post_training": False},
    "designing_ml_systems":             {"year": 2022, "trust": 0.91, "post_training": True},
    "ml_engineering":                   {"year": 2020, "trust": 0.90, "post_training": False},
    "build_llm_from_scratch":           {"year": 2024, "trust": 0.89, "post_training": True},
    "designing_data_intensive":         {"year": 2017, "trust": 0.95, "post_training": False},
    "software_architecture_hard_parts": {"year": 2021, "trust": 0.91, "post_training": False},
    "thinking_fast_slow":               {"year": 2011, "trust": 0.90, "post_training": False},
    "godel_escher_bach":                {"year": 1979, "trust": 0.88, "post_training": False},
    "site_reliability_engineering":     {"year": 2016, "trust": 0.93, "post_training": False},
    "release_it":                       {"year": 2007, "trust": 0.90, "post_training": False},
    "web_app_hackers_handbook":         {"year": 2011, "trust": 0.87, "post_training": False},
}

# Domain → knowledge type hint
DOMAIN_KNOWLEDGE_TYPE = {
    "prana":       "implementation",
    "mlops":       "operational",
    "kamil_arch":  "theory",
    "cognitive":   "theory",
    "reliability": "operational",
    "security":    "operational",
}


# ── Text extraction ───────────────────────────────────────────────

def clean_text(text: str) -> str:
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    return text.strip()


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if len(chunk) > 100:  # skip tiny fragments
            chunks.append(chunk)
        start = end - CHUNK_OVERLAP
    return chunks


def extract_pdf(filepath: str) -> list[tuple[str, int]]:
    """Returns list of (text, page_number) tuples."""
    from pypdf import PdfReader
    reader = PdfReader(filepath)
    pages = []
    for i, page in enumerate(reader.pages):
        text = clean_text(page.extract_text() or "")
        if text:
            pages.append((text, i + 1))
    return pages


def extract_epub(filepath: str) -> list[tuple[str, int]]:
    """Extract text from EPUB using ebooklib."""
    try:
        import ebooklib
        from ebooklib import epub
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text_parts = []
                self.skip_tags = {"script", "style", "head"}
                self._skip = False
            def handle_starttag(self, tag, attrs):
                if tag in self.skip_tags:
                    self._skip = True
            def handle_endtag(self, tag):
                if tag in self.skip_tags:
                    self._skip = False
            def handle_data(self, data):
                if not self._skip and data.strip():
                    self.text_parts.append(data.strip())

        book = epub.read_epub(filepath)
        pages = []
        page_num = 0

        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                html = item.get_content().decode("utf-8", errors="ignore")
                parser = TextExtractor()
                parser.feed(html)
                text = clean_text(" ".join(parser.text_parts))
                if text:
                    page_num += 1
                    pages.append((text, page_num))

        return pages
    except ImportError:
        print("[domain_indexer] ebooklib not installed — install with: pip install ebooklib")
        return []
    except Exception as e:
        print(f"[domain_indexer] EPUB extraction failed: {e}")
        return []


def extract_mobi(filepath: str) -> list[tuple[str, int]]:
    """Extract MOBI by converting to text via mobi library."""
    try:
        import mobi
        tempdir, filepath_extracted = mobi.extract(filepath)
        # mobi extracts to HTML — parse it
        if os.path.isdir(tempdir):
            text_parts = []
            for root, _, files in os.walk(tempdir):
                for f in files:
                    if f.endswith((".html", ".htm", ".xhtml")):
                        with open(os.path.join(root, f), "r", encoding="utf-8", errors="ignore") as fp:
                            content = fp.read()
                        # Strip HTML tags
                        content = re.sub(r'<[^>]+>', ' ', content)
                        text_parts.append(clean_text(content))
            combined = " ".join(text_parts)
            # Split into pseudo-pages of ~2000 chars
            pages = []
            for i, start in enumerate(range(0, len(combined), 2000)):
                chunk = combined[start:start+2000].strip()
                if chunk:
                    pages.append((chunk, i + 1))
            return pages
    except ImportError:
        pass
    except Exception as e:
        print(f"[domain_indexer] MOBI extraction failed: {e}")

    # Fallback: try treating as HTML directly
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        content = re.sub(r'<[^>]+>', ' ', content)
        content = clean_text(content)
        pages = []
        for i, start in enumerate(range(0, len(content), 2000)):
            chunk = content[start:start+2000].strip()
            if chunk:
                pages.append((chunk, i + 1))
        return pages
    except Exception:
        return []


def extract_chm(filepath: str) -> list[tuple[str, int]]:
    """CHM extraction — read as binary and extract text patterns."""
    try:
        import pychm
        chm_file = pychm.chm.chmlib.CHMFile()
        # pychm is complex — fall back to simple text extraction
    except ImportError:
        pass

    # Simple fallback: extract readable text from CHM binary
    try:
        with open(filepath, "rb") as f:
            content = f.read()
        # Extract ASCII text segments
        text = content.decode("utf-8", errors="ignore")
        text = re.sub(r'<[^>]+>', ' ', text)  # strip HTML-like tags
        text = clean_text(text)

        if len(text) < 500:
            # Try latin-1
            text = content.decode("latin-1", errors="ignore")
            text = re.sub(r'<[^>]+>', ' ', text)
            text = clean_text(text)

        pages = []
        for i, start in enumerate(range(0, len(text), 2000)):
            chunk = text[start:start+2000].strip()
            if len(chunk) > 100:
                pages.append((chunk, i + 1))
        return pages
    except Exception as e:
        print(f"[domain_indexer] CHM extraction failed: {e}")
        return []


def extract_file(filepath: str) -> list[tuple[str, int]]:
    """Route to correct extractor based on extension."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        return extract_pdf(filepath)
    elif ext == ".epub":
        return extract_epub(filepath)
    elif ext == ".mobi":
        return extract_mobi(filepath)
    elif ext == ".chm":
        return extract_chm(filepath)
    else:
        print(f"[domain_indexer] Unsupported format: {ext}")
        return []


# ── Indexing ──────────────────────────────────────────────────────

def get_book_meta(filename: str) -> dict:
    stem = os.path.splitext(filename)[0].lower()
    return BOOK_META.get(stem, {
        "year": 2020,
        "trust": 0.85,
        "post_training": False
    })


def index_domain_books(force_reindex: bool = False):
    """
    Index all books in domain_books/ folder structure.
    Skips already-indexed books unless force_reindex=True.
    """
    print("Initializing ChromaDB and embedder...")
    client = get_chroma_client()
    embedder = Embedder()

    # Get or create collection
    try:
        collection = client.get_collection(COLLECTION_NAME)
        existing_count = collection.count()
        print(f"Existing collection: {existing_count} chunks")
        if force_reindex:
            client.delete_collection(COLLECTION_NAME)
            collection = client.create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"}
            )
            print("Force reindex — collection cleared")
        else:
            print("Appending to existing collection")
    except Exception:
        collection = client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
        print(f"Created new collection: {COLLECTION_NAME}")

    # Get already indexed filenames to skip
    indexed_files = set()
    if not force_reindex and collection.count() > 0:
        try:
            existing = collection.get(include=["metadatas"])
            for meta in existing["metadatas"]:
                indexed_files.add(meta.get("filename", ""))
        except Exception:
            pass

    total_indexed = 0
    supported_ext = {".pdf", ".epub", ".mobi", ".chm"}

    # Walk domain folders
    for domain in os.listdir(DOMAIN_BOOKS_DIR):
        domain_path = os.path.join(DOMAIN_BOOKS_DIR, domain)
        if not os.path.isdir(domain_path):
            continue

        knowledge_type = DOMAIN_KNOWLEDGE_TYPE.get(domain, "theory")
        print(f"\nDomain: {domain} ({knowledge_type})")

        for filename in os.listdir(domain_path):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in supported_ext:
                continue

            if filename in indexed_files:
                print(f"  Skipping (already indexed): {filename}")
                continue

            filepath = os.path.join(domain_path, filename)
            book_meta = get_book_meta(filename)

            print(f"  Processing: {filename} (year={book_meta['year']}, "
                  f"post_training={book_meta['post_training']})")

            # Extract text
            pages = extract_file(filepath)
            if not pages:
                print(f"  WARNING: No text extracted from {filename}")
                continue

            print(f"  Extracted {len(pages)} pages")

            # Build chunks with metadata
            all_chunks = []
            chunk_global_idx = 0
            for page_text, page_num in pages:
                text_chunks = chunk_text(page_text)
                for chunk in text_chunks:
                    all_chunks.append({
                        "text": chunk,
                        "metadata": {
                            "domain": domain,
                            "source": os.path.splitext(filename)[0].replace("_", " ").title(),
                            "filename": filename,
                            "knowledge_type": knowledge_type,
                            "page": page_num,
                            "chunk_idx": chunk_global_idx,
                            "year": book_meta["year"],
                            "trust_level": book_meta["trust"],
                            "is_post_training": book_meta["post_training"],
                        }
                    })
                    chunk_global_idx += 1

            if not all_chunks:
                print(f"  WARNING: No chunks from {filename}")
                continue

            print(f"  Indexing {len(all_chunks)} chunks...")

            # Index in batches
            ts = int(time.time())
            for i in range(0, len(all_chunks), BATCH_SIZE):
                batch = all_chunks[i:i + BATCH_SIZE]
                texts = [c["text"] for c in batch]
                ids = [f"dom_{domain}_{ts}_{total_indexed + i + j}"
                       for j in range(len(batch))]
                metadatas = [c["metadata"] for c in batch]
                embeddings = embedder.model.encode(texts).tolist()

                collection.upsert(
                    ids=ids,
                    embeddings=embeddings,
                    documents=texts,
                    metadatas=metadatas
                )

                if (i // BATCH_SIZE + 1) % 5 == 0:
                    print(f"    {i + len(batch)}/{len(all_chunks)} chunks")

            total_indexed += len(all_chunks)
            print(f"  Done: {filename} → {len(all_chunks)} chunks")

    print(f"\nTotal indexed this run: {total_indexed} chunks")
    print(f"Collection total: {collection.count()} chunks")
    return collection.count()


# ── Query ─────────────────────────────────────────────────────────

def query_domain(
    query: str,
    n_results: int = 4,
    domain_filter: str = None,
    prefer_post_training: bool = True,
    prefer_operational: bool = False,
) -> list[dict]:
    """
    Query the domain knowledge collection.

    Args:
        query: search query
        n_results: number of results
        domain_filter: restrict to specific domain (e.g. "prana")
        prefer_post_training: boost post-2022 books (likely not in model weights)
        prefer_operational: boost operational/implementation chunks over theory
    """
    client = get_chroma_client()
    embedder = Embedder()

    try:
        collection = client.get_collection(COLLECTION_NAME)
    except Exception:
        return []

    if collection.count() == 0:
        return []

    # Build where filter
    where = None
    if domain_filter:
        where = {"domain": {"$eq": domain_filter}}

    query_embedding = embedder.model.encode([query]).tolist()

    try:
        results = collection.query(
            query_embeddings=query_embedding,
            n_results=min(n_results * 2, collection.count()),  # oversample then rerank
            include=["documents", "metadatas", "distances"],
            where=where
        )
    except Exception as e:
        print(f"[domain_indexer] Query failed: {e}")
        return []

    output = []
    for i in range(len(results["ids"][0])):
        meta = results["metadatas"][0][i]
        base_score = 1 - results["distances"][0][i]

        # Boost post-training books (higher marginal value)
        if prefer_post_training and meta.get("is_post_training"):
            base_score *= 1.15

        # Boost operational knowledge if requested
        if prefer_operational and meta.get("knowledge_type") in ("operational", "implementation"):
            base_score *= 1.10

        # Trust level scaling
        trust = meta.get("trust_level", 0.85)
        final_score = base_score * trust

        output.append({
            "text": results["documents"][0][i],
            "source": meta.get("source", "Unknown"),
            "domain": meta.get("domain", ""),
            "filename": meta.get("filename", ""),
            "page": meta.get("page", 0),
            "knowledge_type": meta.get("knowledge_type", ""),
            "year": meta.get("year", 0),
            "is_post_training": meta.get("is_post_training", False),
            "score": round(min(final_score, 1.0), 3)
        })

    # Sort by final score and return top n
    output.sort(key=lambda x: x["score"], reverse=True)
    return output[:n_results]


def query_domain_for_code(query: str, n_results: int = 3) -> list[dict]:
    """Query with operational preference — for code/implementation questions."""
    return query_domain(
        query, n_results,
        prefer_post_training=True,
        prefer_operational=True
    )


def query_domain_for_prana(query: str, n_results: int = 3) -> list[dict]:
    """Query restricted to robotics/prana domain."""
    return query_domain(
        query, n_results,
        domain_filter="prana",
        prefer_operational=True
    )


# ── Domain detection ──────────────────────────────────────────────

DOMAIN_KEYWORDS = {
    "prana": [
        "rover", "robot", "sensor", "imu", "gps", "slam", "lidar",
        "kalman", "ekf", "navigation", "autonomous", "motor", "pid",
        "localization", "mapping", "waypoint", "trajectory", "kinematics",
        "ultrasonic", "encoder", "raspberry pi", "arduino", "embedded",
        "real-time", "interrupt", "serial", "watchdog", "actuator",
        "occupancy", "terrain", "path planning", "obstacle"
    ],
    "mlops": [
        "deployment", "pipeline", "training", "inference", "model serving",
        "monitoring", "drift", "feature store", "data pipeline", "mlops",
        "fine-tuning", "lora", "quantization", "gguf", "llm", "transformer",
        "attention", "batch size", "learning rate", "gradient"
    ],
    "kamil_arch": [
        "distributed", "database", "cache", "consistency", "replication",
        "architecture", "microservice", "api", "throughput", "latency",
        "scalability", "design pattern", "abstraction", "coupling",
        "data intensive", "stream processing", "message queue"
    ],
    "reliability": [
        "reliability", "fault tolerance", "graceful degradation", "circuit breaker",
        "timeout", "retry", "fallback", "watchdog", "postmortem", "incident",
        "sre", "uptime", "availability", "resilience", "bulkhead"
    ],
    "security": [
        "security", "vulnerability", "injection", "xss", "csrf", "authentication",
        "authorization", "exploit", "malware", "sandboxing", "prompt injection",
        "web application", "penetration", "attack", "defense"
    ],
    "cognitive": [
        "cognition", "reasoning", "heuristic", "bias", "decision making",
        "system 1", "system 2", "fast thinking", "slow thinking",
        "strange loop", "self-reference", "consciousness", "intelligence",
        "meta-cognition", "society of mind", "emergence"
    ]
}


def is_domain_query(message: str) -> bool:
    """Check if message likely benefits from domain knowledge retrieval."""
    msg = message.lower()
    for keywords in DOMAIN_KEYWORDS.values():
        if any(kw in msg for kw in keywords):
            return True
    return False


def detect_query_domain(message: str) -> str | None:
    """Detect which domain is most relevant for a query."""
    msg = message.lower()
    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        scores[domain] = sum(1 for kw in keywords if kw in msg)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv
    print("=" * 60)
    print("Kamil Domain Knowledge Indexer")
    print("=" * 60)
    print(f"Books directory: {DOMAIN_BOOKS_DIR}")
    print(f"Force reindex: {force}")
    print()

    # Check directory exists
    if not os.path.exists(DOMAIN_BOOKS_DIR):
        print(f"ERROR: {DOMAIN_BOOKS_DIR} does not exist")
        print("Create it and add your domain book folders")
        sys.exit(1)

    # Install ebooklib if needed
    try:
        import ebooklib
    except ImportError:
        print("Installing ebooklib for EPUB support...")
        os.system(f"{sys.executable} -m pip install ebooklib --quiet")

    count = index_domain_books(force_reindex=force)
    print(f"\nIndexing complete: {count} total chunks in collection")

    # Test query
    print("\nTest query: 'EKF sensor fusion for GPS IMU'")
    results = query_domain_for_prana("EKF sensor fusion for GPS IMU", n_results=2)
    for r in results:
        print(f"\nSource: {r['source']} ({r['domain']}, "
              f"page {r['page']}, score {r['score']:.3f})")
        print(r["text"][:200])