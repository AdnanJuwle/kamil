r"""
C:\kamil\context\religious_indexer.py
Indexes Quran and Sahih Bukhari into ChromaDB for semantic retrieval.
Run once to index, then queries are fast.
"""

import os
import sys
sys.path.insert(0, r'C:\kamil')

from pypdf import PdfReader
from context.embedder import get_chroma_client, Embedder
import re

RELIGIOUS_DIR = r"C:\kamil\data\religious"
QURAN_PDF = os.path.join(RELIGIOUS_DIR, "Holy-Quran-English.pdf")
BUKHARI_PDF = os.path.join(RELIGIOUS_DIR, "en_Sahih_Al-Bukhari.pdf")
COLLECTION_NAME = "kamil_religious"
CHUNK_SIZE = 600
CHUNK_OVERLAP = 100


def clean_text(text: str) -> str:
    """Clean extracted PDF text."""
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\x00-\x7F]+', ' ', text)  # remove non-ASCII artifacts
    return text.strip()


def chunk_text(text: str, source: str, page_num: int, chunk_size: int = CHUNK_SIZE) -> list[dict]:
    """Split text into overlapping chunks with metadata."""
    chunks = []
    start = 0
    chunk_idx = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append({
                "text": chunk.strip(),
                "source": source,
                "page": page_num,
                "chunk_idx": chunk_idx
            })
        start = end - CHUNK_OVERLAP
        chunk_idx += 1
    return chunks


def extract_quran_chunks(pdf_path: str) -> list[dict]:
    """Extract Quran text with chapter/verse metadata where detectable."""
    print(f"Reading Quran PDF...")
    reader = PdfReader(pdf_path)
    all_chunks = []
    current_chapter = "Unknown"

    for page_num, page in enumerate(reader.pages):
        text = clean_text(page.extract_text() or "")
        if not text:
            continue

        # Try to detect chapter name
        chapter_match = re.search(r'Chapter[_\s]+\d+[._\s]+([A-Z][A-Za-z\'\-]+)', text)
        if chapter_match:
            current_chapter = chapter_match.group(1)

        chunks = chunk_text(text, f"Quran - {current_chapter}", page_num + 1)
        all_chunks.extend(chunks)

        if (page_num + 1) % 100 == 0:
            print(f"  Quran: processed {page_num + 1}/{len(reader.pages)} pages")

    print(f"Quran: {len(all_chunks)} chunks from {len(reader.pages)} pages")
    return all_chunks


def extract_bukhari_chunks(pdf_path: str) -> list[dict]:
    """Extract Sahih Bukhari with book/hadith number metadata."""
    print(f"Reading Sahih Bukhari PDF...")
    reader = PdfReader(pdf_path)
    all_chunks = []
    current_book = "Unknown"

    for page_num, page in enumerate(reader.pages):
        text = clean_text(page.extract_text() or "")
        if not text:
            continue

        # Try to detect book
        book_match = re.search(r'BOOK\s+(\d+):\s+([A-Z][A-Za-z\s\'\(\),]+?)(?:\s{2,}|$)', text)
        if book_match:
            current_book = f"Book {book_match.group(1)}: {book_match.group(2).strip()}"

        chunks = chunk_text(text, f"Sahih Bukhari - {current_book}", page_num + 1)
        all_chunks.extend(chunks)

        if (page_num + 1) % 200 == 0:
            print(f"  Bukhari: processed {page_num + 1}/{len(reader.pages)} pages")

    print(f"Bukhari: {len(all_chunks)} chunks from {len(reader.pages)} pages")
    return all_chunks


def index_religious_texts():
    """Main indexing function."""
    print("Initializing ChromaDB and embedder...")
    client = get_chroma_client()
    embedder = Embedder()

    # Delete existing collection if present
    try:
        client.delete_collection(COLLECTION_NAME)
        print(f"Deleted existing '{COLLECTION_NAME}' collection")
    except Exception:
        pass

    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"}
    )
    print(f"Created collection: {COLLECTION_NAME}")

    # Extract chunks
    quran_chunks = extract_quran_chunks(QURAN_PDF)
    bukhari_chunks = extract_bukhari_chunks(BUKHARI_PDF)
    all_chunks = quran_chunks + bukhari_chunks

    print(f"\nTotal chunks to index: {len(all_chunks)}")
    print("Generating embeddings and indexing (this will take a few minutes)...")

    # Index in batches
    batch_size = 100
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]
        ids = [f"rel_{i + j}" for j in range(len(batch))]
        metadatas = [{
            "source": c["source"],
            "page": c["page"],
            "chunk_idx": c["chunk_idx"]
        } for c in batch]

        embeddings = embedder.model.encode(texts).tolist()

        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )

        if (i // batch_size + 1) % 10 == 0:
            print(f"  Indexed {i + len(batch)}/{len(all_chunks)} chunks")

    print(f"\nDone! Collection '{COLLECTION_NAME}' has {collection.count()} chunks")
    return collection.count()


def query_religious(query: str, n_results: int = 4) -> list[dict]:
    """Query the religious collection."""
    client = get_chroma_client()
    embedder = Embedder()

    try:
        collection = client.get_collection(COLLECTION_NAME)
    except Exception:
        return []

    query_embedding = embedder.model.encode([query]).tolist()
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=min(n_results, collection.count()),
        include=["documents", "metadatas", "distances"]
    )

    output = []
    for i in range(len(results["ids"][0])):
        output.append({
            "text": results["documents"][0][i],
            "source": results["metadatas"][0][i]["source"],
            "page": results["metadatas"][0][i]["page"],
            "score": 1 - results["distances"][0][i]
        })

    return sorted(output, key=lambda x: x["score"], reverse=True)


RELIGIOUS_KEYWORDS = [
    "quran", "hadith", "bukhari", "allah", "prophet", "muhammad", "islam",
    "salah", "prayer", "wudu", "zakat", "ramadan", "hajj", "halal", "haram",
    "sunnah", "verse", "surah", "ayah", "dua", "iman", "muslim", "masjid",
    "fiqh", "sharia", "jihad", "tawbah", "taqwa", "akhirah", "jannah",
    "jahannam", "angels", "jinn", "iblis", "shirk", "tawheed", "kafir",
    "nikah", "divorce", "inheritance", "fast", "fasting", "pilgrimage",
    "what does islam say", "what does quran say", "is it haram", "is it halal",
    "ruling on", "forbidden in islam", "permissible", "obligatory"
]


def is_religious_query(message: str) -> bool:
    msg = message.lower()
    # Must contain explicit Islamic reference, not just moral topics
    explicit_islamic = [
        "quran", "hadith", "allah", "prophet", "islam", "muslim",
        "salah", "prayer", "wudu", "zakat", "ramadan", "hajj",
        "sunnah", "surah", "ayah", "dua", "masjid", "fiqh",
        "what does islam say", "is it haram", "is it halal",
        "ruling on", "forbidden in islam", "islamic perspective"
    ]
    return any(kw in msg for kw in explicit_islamic)

if __name__ == "__main__":S
    # Run indexing
    count = index_religious_texts()
    print(f"\nIndexing complete: {count} chunks ready")

    # Test query
    print("\nTest query: 'what is the ruling on prayer'")
    results = query_religious("what is the ruling on prayer", n_results=2)
    for r in results:
        print(f"\nSource: {r['source']} (page {r['page']}, score {r['score']:.3f})")
        print(r['text'][:200])