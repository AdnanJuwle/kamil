import chromadb
import os
from sentence_transformers import SentenceTransformer
from core.config import DATA_DIR

os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
CHROMA_PATH = os.path.join(DATA_DIR, "chroma")

# Singleton client — only one instance allowed per path
_chroma_client = None

def get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _chroma_client

class Embedder:
    def __init__(self):
        print("Loading embedding model...")
        self.model = SentenceTransformer(EMBEDDING_MODEL)
        self.client = get_chroma_client()
        print("Embedding model ready.")

    def get_or_create_collection(self, project_name: str):
        safe_name = project_name.replace(" ", "_").replace("-", "_").lower()
        return self.client.get_or_create_collection(
            name=safe_name,
            metadata={"hnsw:space": "cosine"}
        )

    def index_chunks(self, project_name: str, chunks: list[dict]):
        # chunks: list of {"id": str, "text": str, "metadata": dict}
        if not chunks:
            return
        collection = self.get_or_create_collection(project_name)
        texts = [c["text"] for c in chunks]
        ids = [c["id"] for c in chunks]
        metadatas = [c["metadata"] for c in chunks]
        embeddings = self.model.encode(texts, show_progress_bar=False).tolist()
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )

    def query(self, project_name: str, query_text: str, n_results: int = 8) -> list[dict]:
        collection = self.get_or_create_collection(project_name)
        count = collection.count()
        if count == 0:
            return []
        n_results = min(n_results, count)
        query_embedding = self.model.encode([query_text]).tolist()
        results = collection.query(
            query_embeddings=query_embedding,
            n_results=n_results,
            include=["documents", "metadatas", "distances"]
        )
        output = []
        for i in range(len(results["ids"][0])):
            output.append({
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "score": 1 - results["distances"][0][i]
            })
        return output

    def delete_file(self, project_name: str, file_path: str):
        collection = self.get_or_create_collection(project_name)
        results = collection.get(where={"file_path": file_path})
        if results["ids"]:
            collection.delete(ids=results["ids"])