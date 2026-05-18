"""
Catalog module v2 — full CRUD for books + dynamic category management.

New in v2:
  - Admin can create / edit / delete categories dynamically
  - Full-text search via SQLite FTS5 (partial, case-insensitive)
  - Improved validation and error handling
  - Search inside book descriptions

Endpoints:
  POST   /catalog/books              — add book         (librarian+)
  GET    /catalog/books              — list/search      (all authenticated)
  GET    /catalog/books/{id}         — book detail      (all authenticated)
  PUT    /catalog/books/{id}         — update book      (librarian+)
  DELETE /catalog/books/{id}         — delete book      (admin)
  GET    /catalog/categories         — list categories  (all authenticated)
  POST   /catalog/categories         — create category  (admin)
  PUT    /catalog/categories/{id}    — edit category    (admin)
  DELETE /catalog/categories/{id}    — delete category  (admin)
  GET    /catalog/stats              — catalog stats    (librarian+)
"""

import json
import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from auth import get_current_user, require_role

DB_PATH = os.getenv("LIBRARY_DB_PATH", "library_search.db")

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_catalog_tables():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS books (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            title            TEXT NOT NULL,
            author           TEXT NOT NULL,
            isbn             TEXT UNIQUE,
            category_id      INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            year             INTEGER,
            publisher        TEXT,
            description      TEXT,
            summary          TEXT,
            total_copies     INTEGER NOT NULL DEFAULT 1,
            available_copies INTEGER NOT NULL DEFAULT 1,
            language         TEXT NOT NULL DEFAULT 'fr',
            is_digital       INTEGER NOT NULL DEFAULT 0,
            file_path        TEXT,
            section          TEXT,
            shelf            TEXT,
            position         TEXT,
            added_by         INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_books_title    ON books(title);
        CREATE INDEX IF NOT EXISTS idx_books_author   ON books(author);
        CREATE INDEX IF NOT EXISTS idx_books_category ON books(category_id);

        -- FTS5 for books full-text search
        CREATE VIRTUAL TABLE IF NOT EXISTS books_fts
        USING fts5(
            title, author, description, publisher, isbn,
            content=books,
            content_rowid=id,
            tokenize="unicode61 remove_diacritics 2"
        );

        CREATE TRIGGER IF NOT EXISTS books_fts_insert
        AFTER INSERT ON books BEGIN
            INSERT INTO books_fts(rowid, title, author, description, publisher, isbn)
            VALUES (new.id, new.title, new.author,
                    COALESCE(new.description,''), COALESCE(new.publisher,''),
                    COALESCE(new.isbn,''));
        END;

        CREATE TRIGGER IF NOT EXISTS books_fts_update
        AFTER UPDATE ON books BEGIN
            INSERT INTO books_fts(books_fts, rowid, title, author, description, publisher, isbn)
            VALUES ('delete', old.id, old.title, old.author,
                    COALESCE(old.description,''), COALESCE(old.publisher,''),
                    COALESCE(old.isbn,''));
            INSERT INTO books_fts(rowid, title, author, description, publisher, isbn)
            VALUES (new.id, new.title, new.author,
                    COALESCE(new.description,''), COALESCE(new.publisher,''),
                    COALESCE(new.isbn,''));
        END;

        CREATE TRIGGER IF NOT EXISTS books_fts_delete
        AFTER DELETE ON books BEGIN
            INSERT INTO books_fts(books_fts, rowid, title, author, description, publisher, isbn)
            VALUES ('delete', old.id, old.title, old.author,
                    COALESCE(old.description,''), COALESCE(old.publisher,''),
                    COALESCE(old.isbn,''));
        END;

        INSERT OR IGNORE INTO categories (name, description) VALUES
            ('Informatique',       'Sciences informatiques et programmation'),
            ('Mathématiques',      'Mathématiques pures et appliquées'),
            ('Physique',           'Physique générale et appliquée'),
            ('Littérature',        'Romans, nouvelles et poésie'),
            ('Histoire',           'Histoire générale et régionale'),
            ('Sciences naturelles','Biologie, chimie, géologie'),
            ('Droit',              'Droit civil, pénal et administratif'),
            ('Économie',           'Économie et gestion'),
            ('Médecine',           'Médecine et sciences de la santé'),
            ('Autre',              'Autres domaines');
    """)
    conn.commit()
    conn.close()


init_catalog_tables()

# ---------------------------------------------------------------------------
# Migration: add new columns to existing databases that predate this version
# ---------------------------------------------------------------------------
def _migrate_books_table():
    conn = get_db()
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(books)").fetchall()}
    new_cols = {
        "summary":  "TEXT",
        "section":  "TEXT",
        "shelf":    "TEXT",
        "position": "TEXT",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE books ADD COLUMN {col} {col_type}")
            print(f"INFO: books.{col} column added (migration).")
    conn.commit()

    # Rebuild FTS index to include isbn column if it was added after initial creation.
    # We drop and recreate the virtual table + triggers so existing books are re-indexed.
    try:
        fts_cols = [row[1] for row in conn.execute("PRAGMA table_info(books_fts)").fetchall()]
        if "isbn" not in fts_cols:
            conn.executescript("""
                DROP TRIGGER IF EXISTS books_fts_insert;
                DROP TRIGGER IF EXISTS books_fts_update;
                DROP TRIGGER IF EXISTS books_fts_delete;
                DROP TABLE  IF EXISTS books_fts;

                CREATE VIRTUAL TABLE books_fts
                USING fts5(
                    title, author, description, publisher, isbn,
                    content=books,
                    content_rowid=id,
                    tokenize="unicode61 remove_diacritics 2"
                );

                INSERT INTO books_fts(rowid, title, author, description, publisher, isbn)
                SELECT id, title, author,
                       COALESCE(description,''), COALESCE(publisher,''),
                       COALESCE(isbn,'')
                FROM books;

                CREATE TRIGGER books_fts_insert
                AFTER INSERT ON books BEGIN
                    INSERT INTO books_fts(rowid, title, author, description, publisher, isbn)
                    VALUES (new.id, new.title, new.author,
                            COALESCE(new.description,''), COALESCE(new.publisher,''),
                            COALESCE(new.isbn,''));
                END;

                CREATE TRIGGER books_fts_update
                AFTER UPDATE ON books BEGIN
                    INSERT INTO books_fts(books_fts, rowid, title, author, description, publisher, isbn)
                    VALUES ('delete', old.id, old.title, old.author,
                            COALESCE(old.description,''), COALESCE(old.publisher,''),
                            COALESCE(old.isbn,''));
                    INSERT INTO books_fts(rowid, title, author, description, publisher, isbn)
                    VALUES (new.id, new.title, new.author,
                            COALESCE(new.description,''), COALESCE(new.publisher,''),
                            COALESCE(new.isbn,''));
                END;

                CREATE TRIGGER books_fts_delete
                AFTER DELETE ON books BEGIN
                    INSERT INTO books_fts(books_fts, rowid, title, author, description, publisher, isbn)
                    VALUES ('delete', old.id, old.title, old.author,
                            COALESCE(old.description,''), COALESCE(old.publisher,''),
                            COALESCE(old.isbn,''));
                END;
            """)
            conn.commit()
            print("INFO: books_fts rebuilt with isbn column.")
    except Exception as e:
        print(f"WARNING: FTS migration skipped: {e}")

    conn.close()

_migrate_books_table()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

def _validate_isbn(isbn: str) -> bool:
    """Validate ISBN-10 or ISBN-13. Returns True if valid."""
    # Strip hyphens and spaces
    raw = isbn.replace("-", "").replace(" ", "").upper()
    if len(raw) == 10:
        # ISBN-10: sum of (digit * position) mod 11 == 0, last char can be X=10
        total = 0
        for i, c in enumerate(raw):
            val = 10 if c == "X" else (int(c) if c.isdigit() else -999)
            total += val * (10 - i)
        return total % 11 == 0
    elif len(raw) == 13:
        # ISBN-13: alternating 1/3 weights, last digit is check digit
        if not raw.isdigit():
            return False
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(raw[:12]))
        check = (10 - (total % 10)) % 10
        return check == int(raw[12])
    return False


class BookCreate(BaseModel):
    title: str
    author: str
    isbn: Optional[str] = None
    category_id: Optional[int] = None
    year: Optional[int] = None
    publisher: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None
    total_copies: int = 1
    language: str = "fr"
    is_digital: bool = False
    file_path: Optional[str] = None
    # Location fields
    section: Optional[str] = None    # e.g. "A", "Sciences"
    shelf: Optional[str] = None      # e.g. "3", "B2"
    position: Optional[str] = None   # e.g. "12", "left-end"

    @field_validator("title", "author")
    @classmethod
    def not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()

    @field_validator("total_copies")
    @classmethod
    def positive_copies(cls, v):
        if v < 1:
            raise ValueError("Must have at least 1 copy")
        return v

    @field_validator("year")
    @classmethod
    def valid_year(cls, v):
        if v is not None and (v < 1000 or v > datetime.now().year + 1):
            raise ValueError("Invalid year")
        return v

    @field_validator("isbn")
    @classmethod
    def validate_isbn(cls, v):
        if v is not None and v.strip():
            if not _validate_isbn(v.strip()):
                raise ValueError(
                    "رقم ISBN غير صالح. يجب أن يكون ISBN-10 أو ISBN-13 صحيحاً. "
                    "Invalid ISBN: must be a valid ISBN-10 or ISBN-13."
                )
            return v.strip()
        return v


class BookUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    isbn: Optional[str] = None
    category_id: Optional[int] = None
    year: Optional[int] = None
    publisher: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None
    total_copies: Optional[int] = None
    language: Optional[str] = None
    is_digital: Optional[bool] = None
    file_path: Optional[str] = None
    section: Optional[str] = None
    shelf: Optional[str] = None
    position: Optional[str] = None

    @field_validator("isbn")
    @classmethod
    def validate_isbn(cls, v):
        if v is not None and v.strip():
            if not _validate_isbn(v.strip()):
                raise ValueError(
                    "رقم ISBN غير صالح. يجب أن يكون ISBN-10 أو ISBN-13 صحيحاً."
                )
            return v.strip()
        return v


class BookOut(BaseModel):
    id: int
    title: str
    author: str
    isbn: Optional[str]
    category_id: Optional[int]
    category_name: Optional[str]
    year: Optional[int]
    publisher: Optional[str]
    description: Optional[str]
    summary: Optional[str]
    total_copies: int
    available_copies: int
    language: str
    is_digital: bool
    file_path: Optional[str]
    section: Optional[str]
    shelf: Optional[str]
    position: Optional[str]
    created_at: str
    updated_at: str


class CategoryCreate(BaseModel):
    name: str
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def not_empty(cls, v):
        if not v or not v.strip():
            raise ValueError("Category name cannot be empty")
        return v.strip()


class CategoryOut(BaseModel):
    id: int
    name: str
    description: Optional[str]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_embedding_model = None

def _get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedding_model


def _sync_book_to_search(conn, book_id: int):
    row = conn.execute(
        "SELECT title, author, description FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    if not row:
        return
    parts = [row["title"], row["author"]]
    if row["description"]:
        parts.append(row["description"])
    text = " | ".join(parts)

    try:
        model     = _get_embedding_model()
        embedding = model.encode([text], convert_to_tensor=False)[0].tolist()
    except Exception:
        embedding = []

    embedding_json = json.dumps(embedding)

    try:
        existing = conn.execute(
            "SELECT id FROM documents WHERE doc LIKE ?", (f"%{row['title']}%",)
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE documents SET doc = ?, embedding = ? WHERE id = ?",
                (text, embedding_json, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO documents (doc, embedding) VALUES (?, ?)",
                (text, embedding_json),
            )
        conn.commit()
    except Exception:
        # documents table may not exist yet; silently skip
        pass


def _remove_book_from_search(conn, title: str):
    try:
        conn.execute("DELETE FROM documents WHERE doc LIKE ?", (f"%{title}%",))
        conn.commit()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/catalog", tags=["Catalog"])


# ── Categories — read ────────────────────────────────────────────────────────

@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(user = Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Categories — create (admin) ───────────────────────────────────────────────

@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(data: CategoryCreate, user = Depends(require_role("admin"))):
    """Create a new category — admin only."""
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO categories (name, description) VALUES (?, ?)",
            (data.name, data.description)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"Category '{data.name}' already exists")
    finally:
        conn.close()


# ── Categories — update (admin) ───────────────────────────────────────────────

@router.put("/categories/{cat_id}", response_model=CategoryOut)
async def update_category(cat_id: int, data: CategoryCreate, user = Depends(require_role("admin"))):
    conn = get_db()
    existing = conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="Category not found")
    try:
        conn.execute(
            "UPDATE categories SET name = ?, description = ? WHERE id = ?",
            (data.name, data.description, cat_id)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"Category name '{data.name}' already taken")
    finally:
        conn.close()


# ── Categories — delete (admin) ───────────────────────────────────────────────

@router.delete("/categories/{cat_id}", status_code=204)
async def delete_category(cat_id: int, user = Depends(require_role("admin"))):
    conn = get_db()
    existing = conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="Category not found")
    # Null-out books in this category (preserve books, just unlink)
    conn.execute("UPDATE books SET category_id = NULL WHERE category_id = ?", (cat_id,))
    conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()
    conn.close()


# ── Books — list / search ─────────────────────────────────────────────────────

@router.get("/books", response_model=dict)
async def list_books(
    q:              Optional[str] = Query(None),
    category_id:    Optional[int] = Query(None),
    language:       Optional[str] = Query(None),
    available_only: bool          = Query(False),
    page:           int           = Query(1, ge=1),
    page_size:      int           = Query(20, ge=1, le=100),
    user = Depends(get_current_user),
):
    conn = get_db()

    # Use FTS5 when a query is present for better partial/case-insensitive matching
    if q and q.strip():
        # Strip hyphens/spaces so "978-3-16-148410-0" matches stored "9783161484100"
        q_clean    = q.strip()
        q_isbn     = q_clean.replace("-", "").replace(" ", "")
        isbn_match = bool(q_isbn and q_isbn.isdigit())

        try:
            # Build FTS query — append * for prefix matching
            fts_query = " OR ".join(f'"{term}"*' for term in q_clean.split())
            fts_ids = [
                r[0] for r in conn.execute(
                    "SELECT rowid FROM books_fts WHERE books_fts MATCH ? ORDER BY rank LIMIT 500",
                    (fts_query,)
                ).fetchall()
            ]

            # Always also search isbn directly (FTS tokenizer may split digits oddly)
            if isbn_match:
                isbn_like = f"%{q_isbn}%"
                extra = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM books WHERE REPLACE(REPLACE(isbn,'-',''),' ','') LIKE ?",
                        (isbn_like,)
                    ).fetchall()
                ]
                fts_ids = list(dict.fromkeys(fts_ids + extra))  # deduplicate, preserve order

            if not fts_ids:
                # Fall back to LIKE for very short queries
                like = f"%{q_clean}%"
                fts_ids = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM books WHERE title LIKE ? OR author LIKE ? "
                        "OR description LIKE ? OR isbn LIKE ? "
                        "OR REPLACE(REPLACE(isbn,'-',''),' ','') LIKE ?",
                        (like, like, like, like, f"%{q_isbn}%" if isbn_match else like)
                    ).fetchall()
                ]
        except Exception:
            like = f"%{q_clean}%"
            fts_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM books WHERE title LIKE ? OR author LIKE ? "
                    "OR description LIKE ? OR isbn LIKE ? "
                    "OR REPLACE(REPLACE(isbn,'-',''),' ','') LIKE ?",
                    (like, like, like, like, f"%{q_isbn}%" if isbn_match else like)
                ).fetchall()
            ]

        if not fts_ids:
            conn.close()
            return {"total": 0, "page": page, "page_size": page_size, "results": []}

        id_placeholders = ",".join("?" * len(fts_ids))
        conditions = [f"b.id IN ({id_placeholders})"]
        params     = list(fts_ids)
    else:
        conditions = []
        params     = []

    if category_id:
        conditions.append("b.category_id = ?")
        params.append(category_id)
    if language:
        conditions.append("b.language = ?")
        params.append(language)
    if available_only:
        conditions.append("b.available_copies > 0")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    base_q = f"""
        SELECT b.*, c.name AS category_name
        FROM books b
        LEFT JOIN categories c ON b.category_id = c.id
        {where}
    """

    total  = conn.execute(f"SELECT COUNT(*) FROM ({base_q})", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows   = conn.execute(
        f"{base_q} ORDER BY b.created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset]
    ).fetchall()
    conn.close()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "results":   [dict(r) for r in rows],
    }


# ── Books — single ────────────────────────────────────────────────────────────

@router.get("/books/{book_id}", response_model=BookOut)
async def get_book(book_id: int, user = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("""
        SELECT b.*, c.name AS category_name
        FROM books b
        LEFT JOIN categories c ON b.category_id = c.id
        WHERE b.id = ?
    """, (book_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    return dict(row)


# ── Books — create ────────────────────────────────────────────────────────────

@router.post("/books", response_model=BookOut, status_code=201)
async def create_book(data: BookCreate, user = Depends(require_role("librarian"))):
    conn = get_db()
    try:
        cursor = conn.execute("""
            INSERT INTO books
                (title, author, isbn, category_id, year, publisher, description, summary,
                 total_copies, available_copies, language, is_digital, file_path,
                 section, shelf, position, added_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.title, data.author, data.isbn, data.category_id,
            data.year, data.publisher, data.description, data.summary,
            data.total_copies, data.total_copies,
            data.language, int(data.is_digital), data.file_path,
            data.section, data.shelf, data.position,
            user["id"]
        ))
        conn.commit()
        book_id = cursor.lastrowid
        _sync_book_to_search(conn, book_id)

        row = conn.execute("""
            SELECT b.*, c.name AS category_name
            FROM books b LEFT JOIN categories c ON b.category_id = c.id
            WHERE b.id = ?
        """, (book_id,)).fetchone()
        return dict(row)

    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail="رقم ISBN مكرر — هذا الكتاب موجود بالفعل / ISBN already exists in the catalog.")
    finally:
        conn.close()


# ── Books — update ────────────────────────────────────────────────────────────

@router.put("/books/{book_id}", response_model=BookOut)
async def update_book(book_id: int, data: BookUpdate, user = Depends(require_role("librarian"))):
    conn = get_db()

    existing = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="Book not found")

    # Only update fields that were actually sent
    raw = data.model_dump(exclude_unset=True)
    fields = {}
    for k, v in raw.items():
        if k == "is_digital":
            fields[k] = int(v) if v is not None else 0
        elif v is not None:
            fields[k] = v

    if not fields:
        conn.close()
        raise HTTPException(status_code=400, detail="No fields to update")

    if "total_copies" in fields:
        diff = fields["total_copies"] - existing["total_copies"]
        fields["available_copies"] = max(0, min(existing["available_copies"] + diff, fields["total_copies"]))

    fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values     = list(fields.values()) + [book_id]

    try:
        conn.execute(f"UPDATE books SET {set_clause} WHERE id = ?", values)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        raise HTTPException(status_code=409, detail=str(e))

    _sync_book_to_search(conn, book_id)

    row = conn.execute("""
        SELECT b.*, c.name AS category_name
        FROM books b LEFT JOIN categories c ON b.category_id = c.id
        WHERE b.id = ?
    """, (book_id,)).fetchone()
    conn.close()
    return dict(row)


# ── Books — delete ────────────────────────────────────────────────────────────

@router.delete("/books/{book_id}", status_code=204)
async def delete_book(
    book_id: int,
    force: bool = Query(False, description="Close active loans and delete"),
    user = Depends(require_role("admin")),
):
    conn = get_db()
    row = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Book not found")

    active_loans = conn.execute(
        "SELECT COUNT(*) FROM loans WHERE book_id = ? AND return_date IS NULL", (book_id,)
    ).fetchone()[0]

    if active_loans > 0 and not force:
        conn.close()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {active_loans} active loan(s) exist for this book",
        )

    try:
        if active_loans > 0 and force:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                UPDATE loans
                SET return_date = ?, notes = 'إرجاع تلقائي عند حذف الكتاب'
                WHERE book_id = ? AND return_date IS NULL
            """, (now, book_id))
        conn.execute(
            "UPDATE loans SET book_id = NULL WHERE book_id = ? AND return_date IS NOT NULL",
            (book_id,),
        )
        _remove_book_from_search(conn, row["title"])
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        conn.close()
        raise HTTPException(status_code=409, detail=f"Cannot delete book: {exc}")
    finally:
        conn.close()


# ── Stats ──────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def catalog_stats(user = Depends(require_role("librarian"))):
    conn = get_db()
    total_books      = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    total_copies     = conn.execute("SELECT COALESCE(SUM(total_copies), 0) FROM books").fetchone()[0]
    available_copies = conn.execute("SELECT COALESCE(SUM(available_copies), 0) FROM books").fetchone()[0]
    digital_books    = conn.execute("SELECT COUNT(*) FROM books WHERE is_digital = 1").fetchone()[0]
    by_category      = conn.execute("""
        SELECT c.name, COUNT(b.id) AS total
        FROM categories c
        LEFT JOIN books b ON b.category_id = c.id
        GROUP BY c.id ORDER BY total DESC
    """).fetchall()
    conn.close()
    return {
        "total_books":      total_books,
        "total_copies":     total_copies,
        "available_copies": available_copies,
        "borrowed_copies":  total_copies - available_copies,
        "digital_books":    digital_books,
        "by_category":      [dict(r) for r in by_category],
    }