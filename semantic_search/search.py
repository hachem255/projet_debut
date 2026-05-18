# search.py
import json
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from semantic_search.database_factory import DatabaseFactory

FAISS_INDEX_PATH = "library.index"


class SemanticSearch:

    def __init__(self, database, model_name: str = "all-MiniLM-L6-v2"):
        self.model = SentenceTransformer(model_name)
        self.db = database          # SQLiteDB wrapper
        self.faiss_index = None
        # FIX: maintain a stable list of rowids that correspond 1-to-1 with
        # FAISS vector positions, so pos 0 always maps to _faiss_rowids[0].
        self._faiss_rowids: list[int] = []

    # ------------------------------------------------------------------
    def build_faiss_index(self) -> None:
        """
        Build (or rebuild) the FAISS index from all documents that already
        have a stored embedding.  Documents with empty embeddings (added via
        catalog sync without re-encoding) are skipped — they will be included
        the next time their embedding is generated via add_document().
        """
        # FIX: use get_all_documents_with_id so we keep rowid→FAISS-pos alignment
        docs = self.db.get_all_documents_with_id()
        if not docs:
            raise ValueError(
                "No valid embeddings found in the database. "
                "Add documents via /add/ first."
            )

        rowids     = [d[0] for d in docs]
        embeddings = np.array([d[2] for d in docs], dtype=np.float32)

        # Sanity-check: all embeddings must have the same dimension
        if embeddings.ndim != 2:
            raise ValueError("Embeddings array is not 2-D — possible corrupt rows.")

        self.faiss_index    = faiss.IndexFlatL2(embeddings.shape[1])
        self.faiss_index.add(embeddings)           # type: ignore[arg-type]
        self._faiss_rowids  = rowids

        faiss.write_index(self.faiss_index, FAISS_INDEX_PATH)

    # ------------------------------------------------------------------
    def add_document(self, text: str) -> None:
        """
        Encode `text`, persist it (doc + embedding) in the DB, then append
        the vector to the live FAISS index and flush to disk.
        """
        embedding = self.model.encode([text], convert_to_tensor=False)[0]
        embedding = np.array(embedding, dtype=np.float32)

        # FIX: capture the new row's id so _faiss_rowids stays aligned
        new_rowid = self.db.add_document(text, embedding.tolist())

        if self.faiss_index is not None:
            self.faiss_index.add(embedding[np.newaxis, :])  # type: ignore[arg-type]
            self._faiss_rowids.append(new_rowid)
            faiss.write_index(self.faiss_index, FAISS_INDEX_PATH)
        # If the index hasn't been built yet the vector is stored in the DB
        # and will be included next time build_faiss_index() is called.

    # ------------------------------------------------------------------
    def retrieve(self, query: str, top_k: int = 5) -> list[str]:
        """
        Return up to top_k document texts most relevant to `query`.
        Uses FAISS when available; falls back to cosine similarity otherwise.
        Both paths skip documents that have no real embedding.
        """
        query_embedding = self.model.encode(query, convert_to_tensor=False)
        if not isinstance(query_embedding, np.ndarray):
            query_embedding = np.array(query_embedding)
        query_embedding = query_embedding.astype(np.float32).reshape(1, -1)

        # FIX: always use get_all_documents (already filters empty embeddings)
        docs = self.db.get_all_documents()
        if not docs:
            return []

        # ── FAISS path ─────────────────────────────────────────────────
        if self.faiss_index is not None and self._faiss_rowids:
            k = min(top_k, len(self._faiss_rowids))
            distances, positions = self.faiss_index.search(query_embedding, k)

            # Build rowid→doc_text lookup from the filtered docs
            # (get_all_documents_with_id keeps the same rowid ordering)
            id_to_doc = {
                row[0]: row[1]
                for row in self.db.get_all_documents_with_id()
            }

            results = []
            for pos in positions[0]:
                if pos < 0 or pos >= len(self._faiss_rowids):
                    continue
                rowid   = self._faiss_rowids[pos]
                doc_text = id_to_doc.get(rowid)
                if doc_text:
                    results.append(doc_text)
            return results

        # ── Cosine fallback (no FAISS index loaded) ────────────────────
        # FIX: docs already filtered — no empty-embedding rows
        doc_embeddings = np.array([d[1] for d in docs], dtype=np.float32)
        similarities   = cosine_similarity(query_embedding, doc_embeddings)[0]
        sorted_indices = np.argsort(similarities)[::-1][:top_k]
        return [
            docs[i][0]
            for i in sorted_indices
            if similarities[i] > 0.3
        ]