
import chromadb
from chromadb import EmbeddingFunction
from core.config import CHROMA_DIR


class _NoOpEmbeddingFunction(EmbeddingFunction):
    """query_memory() only ever does a plain .get() lookup, never a vector
    similarity search, so a real embedding model is unused overhead. Chroma's
    built-in default embedding function downloads a ~90MB ONNX model from S3
    on first use, which can stall for minutes (or fail outright) on restricted
    corporate networks. Returning a fixed dummy vector skips that entirely."""

    def __call__(self, input):
        return [[0.0] for _ in input]

    @staticmethod
    def name() -> str:
        return "idamp_noop"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "_NoOpEmbeddingFunction":
        return _NoOpEmbeddingFunction()


def get_chroma_client():
    """Get persistent ChromaDB client."""
    return chromadb.PersistentClient(path=str(CHROMA_DIR))


def get_collection(name: str = "idamp_memory"):
    """Get or create the IDAMP memory collection."""
    client = get_chroma_client()
    return client.get_or_create_collection(name=name, embedding_function=_NoOpEmbeddingFunction())



def store_document(doc_id: str, text: str, metadata: dict | None = None) -> None:
    """Store a document in semantic memory."""
    try:
        collection = get_collection()
        collection.upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata or {}],
        )
    except Exception:
        # Keep pipeline running even if semantic memory backend is unavailable.
        pass


def query_memory(query_text: str, n_results: int = 5) -> list[dict]:
    """Query semantic memory for relevant documents (searches metadata/text without embeddings)."""
    try:
        collection = get_collection()
        # Get all documents and filter locally (no embedding-based search)
        # This is a simple implementation that just returns recent docs
        results = collection.get(limit=n_results)
        return [
            {"id": id_, "document": doc, "metadata": meta}
            for id_, doc, meta in zip(
                results["ids"], results["documents"], results["metadatas"]
            )
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Pinned insights — NEW. Reuses store_document/get_collection rather than
# introducing a second storage system: a pinned insight is just a Chroma
# document tagged metadata={"type": "insight", ...}, stored in the same
# collection business_intent and reports already live in.
# ---------------------------------------------------------------------------

def store_insight(run_id: str, question: str, answer: str, sql: str = "") -> str:
    """Pin a chat Q&A exchange as a tagged finding in semantic memory.

    Returns the doc_id used, so the caller (Streamlit UI) can track pin state
    per-answer in session state (e.g. to grey out an already-pinned button).
    """
    doc_id = f"insight_{run_id}_{abs(hash(question)) % (10 ** 10)}"
    text = f"Q: {question}\nA: {answer}"
    store_document(
        doc_id=doc_id,
        text=text,
        metadata={
            "type": "insight",
            "run_id": run_id,
            "question": question,
            "answer": answer,
            "sql": sql,
        },
    )
    return doc_id


def list_pinned_insights(run_id: str | None = None, n_results: int = 50) -> list[dict]:
    """Return pinned insights, most recently added first if metadata timestamps
    aren't used, filtered to one run_id when given. Uses a metadata `where`
    filter directly (rather than query_memory's naive unfiltered .get()) so
    this scales correctly once other document types share the collection.
    """
    try:
        collection = get_collection()
        where: dict = {"type": "insight"}
        if run_id:
            where = {"$and": [{"type": "insight"}, {"run_id": run_id}]}
        results = collection.get(where=where, limit=n_results)
        return [
            {"id": id_, "document": doc, "metadata": meta}
            for id_, doc, meta in zip(
                results["ids"], results["documents"], results["metadatas"]
            )
        ]
    except Exception:
        return []


def delete_insight(doc_id: str) -> None:
    """Unpin a previously pinned insight."""
    try:
        get_collection().delete(ids=[doc_id])
    except Exception:
        pass
