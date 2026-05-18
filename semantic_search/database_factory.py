# database_factory.py
import sqlite3
import json


class SQLiteDB:
    """Unified wrapper exposed as `database` to SemanticSearch."""

    def __init__(self, db_path: str = "library_search.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # FIX: consistent dict-like row access
        self.conn.execute("PRAGMA journal_mode=WAL")   # FIX: safer concurrent writes
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                doc       TEXT    NOT NULL,
                embedding TEXT    NOT NULL DEFAULT '[]'
            )
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    def add_document(self, text: str, embedding: list) -> int:
        """Insert a document + its embedding. Returns the new row id."""
        cursor = self.conn.execute(
            "INSERT INTO documents (doc, embedding) VALUES (?, ?)",
            (text, json.dumps(embedding)),
        )
        self.conn.commit()
        return cursor.lastrowid  # FIX: return id so callers can reference it

    # ------------------------------------------------------------------
    def get_all_documents(self) -> list[tuple[str, list]]:
        """
        Return [(doc_text, embedding_list), ...] for every row that has a
        non-empty, valid embedding.  Rows whose embedding is '[]' or corrupt
        are silently skipped so build_faiss_index never sees a ragged array.
        """
        # FIX: filter empty/corrupt embeddings here so FAISS never gets them
        rows = self.conn.execute(
            "SELECT doc, embedding FROM documents WHERE embedding != '[]'"
        ).fetchall()

        result = []
        for row in rows:
            try:
                emb = json.loads(row["embedding"])
                if isinstance(emb, list) and len(emb) > 0:
                    result.append((row["doc"], emb))
            except (json.JSONDecodeError, TypeError):
                # Skip corrupt rows instead of crashing
                continue
        return result

    # ------------------------------------------------------------------
    def get_all_documents_with_id(self) -> list[tuple[int, str, list]]:
        """
        Return [(rowid, doc_text, embedding_list), ...] for rows with valid
        embeddings.  Used by SemanticSearch to build a stable FAISS↔rowid map.
        """
        rows = self.conn.execute(
            "SELECT rowid, doc, embedding FROM documents WHERE embedding != '[]'"
        ).fetchall()

        result = []
        for row in rows:
            try:
                emb = json.loads(row["embedding"])
                if isinstance(emb, list) and len(emb) > 0:
                    result.append((row["rowid"], row["doc"], emb))
            except (json.JSONDecodeError, TypeError):
                continue
        return result


class DatabaseFactory:
    @staticmethod
    def create_database(db_type: str = "sqlite", **kwargs) -> SQLiteDB:
        if db_type == "sqlite":
            return SQLiteDB(db_path=kwargs.get("db_path", "library_search.db"))
        raise ValueError(f"Unsupported db_type: {db_type}")