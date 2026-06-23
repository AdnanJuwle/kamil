import os
import json
import hashlib
from datetime import datetime
from sentence_transformers import SentenceTransformer
import chromadb
from core.config import DATA_DIR
from context.embedder import get_chroma_client

CHROMA_PATH = os.path.join(DATA_DIR, "chroma")
MEMORY_COLLECTION = "kamil_memories"
COMPRESSION_THRESHOLD = 8  # compress after every 8 messages


class LongTermMemory:
    def __init__(self, embedder=None):
        self.client = get_chroma_client()
        self._external_embedder = embedder
        self._own_model = None
        self.collection = self.client.get_or_create_collection(
            name=MEMORY_COLLECTION,
            metadata={"hnsw:space": "cosine"}
        )

    def _encode(self, texts: list) -> list:
        if self._external_embedder:
            return self._external_embedder.model.encode(texts).tolist()
        # share the singleton embedding model instead of loading a duplicate
        from context.embedder import get_embedding_model
        return get_embedding_model().encode(texts).tolist()

    def store(self, summary: str, topics: list[str] = [], project: str = ""):
        timestamp = datetime.now().isoformat()
        memory_id = hashlib.md5(f"{timestamp}{summary[:50]}".encode()).hexdigest()
        embedding = self._encode([summary])
        self.collection.upsert(
            ids=[memory_id],
            embeddings=embedding,
            documents=[summary],
            metadatas=[{
                "timestamp": timestamp,
                "topics": json.dumps(topics),
                "project": project
            }]
        )
        return memory_id

    def retrieve(self, query: str, n_results: int = 3, min_score: float = 0.4) -> list[dict]:
        # min_score gates relevance: without it, every prompt got 3 memories no
        # matter how irrelevant (off-topic queries matched at ~0.1). Calibrated:
        # relevant memories score >=0.5, irrelevant <=0.3, so 0.4 separates.
        count = self.collection.count()
        if count == 0:
            return []
        n_results = min(n_results, count)
        query_embedding = self._encode([query])
        results = self.collection.query(
            query_embeddings=query_embedding,
            n_results=n_results,
            include=["documents", "metadatas", "distances"]
        )
        output = []
        for i in range(len(results["ids"][0])):
            score = 1 - results["distances"][0][i]
            if score < min_score:
                continue
            output.append({
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "score": score,
                "timestamp": results["metadatas"][0][i].get("timestamp", "")
            })
        return sorted(output, key=lambda x: x["timestamp"], reverse=True)

    def count(self) -> int:
        return self.collection.count()