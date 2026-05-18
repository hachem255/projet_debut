"""
PDFs module v2 — upload, extract, serve, search, and edit PDF documents.

New in v2:
  - PUT /pdfs/{id}  — edit title, author, language, category, replace file
  - language field added to pdf_documents table
  - Migration runs on startup to add language to existing DBs

Endpoints:
  POST   /pdfs/upload         — upload PDF            (librarian+)
  GET    /pdfs/               — list all PDFs         (all authenticated)
  GET    /pdfs/{id}           — PDF metadata          (all authenticated)
  PUT    /pdfs/{id}           — edit PDF metadata     (librarian+)
  GET    /pdfs/{id}/view      — serve PDF in-browser  (all authenticated)
  GET    /pdfs/{id}/download  — force download        (all authenticated)
  DELETE /pdfs/{id}           — delete PDF            (admin)
  GET    /pdfs/search         — full-text search      (all authenticated)
"""

import os
import re
import json
import sqlite3
import hashlib
import aiofiles
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.responses import FileResponse
from auth import get_current_user, require_role


DB_PATH        = os.getenv("LIBRARY_DB_PATH", "library_search.db")
UPLOAD_DIR     = Path(os.getenv("PDF_UPLOAD_DIR", "uploads/pdfs"))
MAX_FILE_MB    = int(os.getenv("PDF_MAX_FILE_MB", "50"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_pdf_tables():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pdf_documents (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            title          TEXT NOT NULL,
            author         TEXT,
            language       TEXT NOT NULL DEFAULT 'fr',
            filename       TEXT NOT NULL,
            file_path      TEXT NOT NULL UNIQUE,
            file_size      INTEGER NOT NULL DEFAULT 0,
            page_count     INTEGER NOT NULL DEFAULT 0,
            extracted_text TEXT NOT NULL DEFAULT '',
            sha256         TEXT,
            category_id    INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            book_id        INTEGER REFERENCES books(id)      ON DELETE SET NULL,
            uploaded_by    INTEGER REFERENCES users(id)      ON DELETE SET NULL,
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_pdf_title  ON pdf_documents(title);
        CREATE INDEX IF NOT EXISTS idx_pdf_author ON pdf_documents(author);
        CREATE VIRTUAL TABLE IF NOT EXISTS pdf_fts
        USING fts5(
            title, author, extracted_text,
            content=pdf_documents,
            content_rowid=id
        );
        CREATE TRIGGER IF NOT EXISTS pdf_fts_insert
        AFTER INSERT ON pdf_documents BEGIN
            INSERT INTO pdf_fts(rowid, title, author, extracted_text)
            VALUES (new.id, new.title, COALESCE(new.author,\'\'), new.extracted_text);
        END;
        CREATE TRIGGER IF NOT EXISTS pdf_fts_update
        AFTER UPDATE ON pdf_documents BEGIN
            INSERT INTO pdf_fts(pdf_fts, rowid, title, author, extracted_text)
            VALUES ('delete', old.id, old.title, COALESCE(old.author,\'\'), old.extracted_text);
            INSERT INTO pdf_fts(rowid, title, author, extracted_text)
            VALUES (new.id, new.title, COALESCE(new.author,\'\'), new.extracted_text);
        END;
        CREATE TRIGGER IF NOT EXISTS pdf_fts_delete
        AFTER DELETE ON pdf_documents BEGIN
            INSERT INTO pdf_fts(pdf_fts, rowid, title, author, extracted_text)
            VALUES ('delete', old.id, old.title, COALESCE(old.author,\'\'), old.extracted_text);
        END;
    """)
    conn.commit()
    conn.close()


def _migrate_pdf_table():
    conn = get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pdf_documents)").fetchall()}
    if "language" not in cols:
        conn.execute("ALTER TABLE pdf_documents ADD COLUMN language TEXT NOT NULL DEFAULT 'fr'")
        conn.commit()
        print("INFO: pdf_documents.language column added (migration).")
    conn.close()


init_pdf_tables()
_migrate_pdf_table()


def _extract_text_pdfplumber(path):
    try:
        import pdfplumber
        pages_text = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t: pages_text.append(t.strip())
            return "\n\n".join(pages_text), len(pdf.pages)
    except Exception:
        return _extract_text_pypdf(path)


def _extract_text_pypdf(path):
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        pages_text = []
        for page in reader.pages:
            t = page.extract_text()
            if t: pages_text.append(t.strip())
        return "\n\n".join(pages_text), len(reader.pages)
    except Exception as e:
        return f"[Extraction failed: {e}]", 0


def _extract_pdf_metadata(path):
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        meta = reader.metadata or {}
        return {
            "title":  (meta.get("/Title")  or "").strip() or None,
            "author": (meta.get("/Author") or "").strip() or None,
        }
    except Exception:
        return {}


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdf_safe(row):
    d = dict(row)
    d.pop("extracted_text", None)
    return d


router = APIRouter(prefix="/pdfs", tags=["PDFs"])


@router.post("/upload", status_code=201)
async def upload_pdf(
    file:        UploadFile = File(...),
    title:       Optional[str] = Form(None),
    author:      Optional[str] = Form(None),
    language:    Optional[str] = Form("fr"),
    category_id: Optional[int] = Form(None),
    book_id:     Optional[int] = Form(None),
    user = Depends(require_role("librarian")),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    safe_name  = re.sub(r"[^\w.\-]", "_", file.filename)
    timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest_name  = f"{timestamp}_{safe_name}"
    dest_path  = UPLOAD_DIR / dest_name
    total_bytes = 0

    try:
        async with aiofiles.open(dest_path, "wb") as out:
            while chunk := await file.read(65536):
                total_bytes += len(chunk)
                if total_bytes > MAX_FILE_BYTES:
                    await out.close()
                    dest_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail=f"File exceeds {MAX_FILE_MB} MB")
                await out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    extracted_text, page_count = _extract_text_pdfplumber(str(dest_path))
    pdf_meta = _extract_pdf_metadata(str(dest_path))
    sha256   = _file_sha256(str(dest_path))
    final_title  = title  or pdf_meta.get("title")  or safe_name.replace(".pdf", "")
    final_author = author or pdf_meta.get("author")
    final_lang   = language or "fr"

    conn = get_db()
    try:
        dup = conn.execute("SELECT id, title FROM pdf_documents WHERE sha256 = ?", (sha256,)).fetchone()
        if dup:
            dest_path.unlink(missing_ok=True)
            conn.close()
            raise HTTPException(status_code=409, detail=f"Duplicate — already uploaded as '{dup['title']}' (id={dup['id']})")

        cursor = conn.execute("""
            INSERT INTO pdf_documents
                (title, author, language, filename, file_path, file_size, page_count,
                 extracted_text, sha256, category_id, book_id, uploaded_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (final_title, final_author, final_lang, dest_name, str(dest_path),
              total_bytes, page_count, extracted_text, sha256, category_id, book_id, user["id"]))
        conn.commit()
        doc_id = cursor.lastrowid

        search_text = final_title
        if final_author: search_text += f" | {final_author}"
        if extracted_text and len(extracted_text) > 20: search_text += f" | {extracted_text[:500]}"
        conn.execute("INSERT OR IGNORE INTO documents (doc, embedding) VALUES (?,?)", (search_text, "[]"))
        conn.commit()

        row = conn.execute("""
            SELECT p.*, c.name AS category_name FROM pdf_documents p
            LEFT JOIN categories c ON c.id = p.category_id WHERE p.id = ?
        """, (doc_id,)).fetchone()
        conn.close()
        return _pdf_safe(row)
    except HTTPException:
        raise
    except Exception as e:
        dest_path.unlink(missing_ok=True)
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{doc_id}", status_code=200)
async def edit_pdf(
    doc_id:      int,
    title:       Optional[str]        = Form(None),
    author:      Optional[str]        = Form(None),
    language:    Optional[str]        = Form(None),
    category_id: Optional[str]        = Form(None),
    file:        Optional[UploadFile] = File(None),
    user = Depends(require_role("librarian")),
):
    conn = get_db()
    existing = conn.execute("SELECT * FROM pdf_documents WHERE id = ?", (doc_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="PDF not found")

    updates = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if title is not None and title.strip():
        updates["title"] = title.strip()
    if author is not None:
        updates["author"] = author.strip() or None
    if language is not None and language.strip():
        updates["language"] = language.strip()
    if category_id is not None:
        updates["category_id"] = int(category_id) if category_id.strip() else None

    if file and file.filename:
        if not file.filename.lower().endswith(".pdf"):
            conn.close()
            raise HTTPException(status_code=400, detail="Only PDF files accepted")
        safe_name  = re.sub(r"[^\w.\-]", "_", file.filename)
        timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest_name  = f"{timestamp}_{safe_name}"
        dest_path  = UPLOAD_DIR / dest_name
        total_bytes = 0
        try:
            async with aiofiles.open(dest_path, "wb") as out:
                while chunk := await file.read(65536):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_FILE_BYTES:
                        await out.close()
                        dest_path.unlink(missing_ok=True)
                        conn.close()
                        raise HTTPException(status_code=413, detail=f"File too large")
                    await out.write(chunk)
        except HTTPException:
            conn.close()
            raise
        except Exception as e:
            dest_path.unlink(missing_ok=True)
            conn.close()
            raise HTTPException(status_code=500, detail=str(e))
        extracted_text, page_count = _extract_text_pdfplumber(str(dest_path))
        sha256 = _file_sha256(str(dest_path))
        updates.update({
            "filename": dest_name, "file_path": str(dest_path),
            "file_size": total_bytes, "page_count": page_count,
            "extracted_text": extracted_text, "sha256": sha256,
        })
        Path(existing["file_path"]).unlink(missing_ok=True)

    if not updates:
        conn.close()
        raise HTTPException(status_code=400, detail="Nothing to update")

    updates["updated_at"] = now
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE pdf_documents SET {set_clause} WHERE id = ?", list(updates.values()) + [doc_id])
    conn.commit()

    row = conn.execute("""
        SELECT p.*, c.name AS category_name FROM pdf_documents p
        LEFT JOIN categories c ON c.id = p.category_id WHERE p.id = ?
    """, (doc_id,)).fetchone()
    conn.close()
    return _pdf_safe(row)


@router.get("/search")
async def search_pdfs(
    q:     str = Query(..., min_length=2),
    limit: int = Query(20, ge=1, le=100),
    user = Depends(get_current_user),
):
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT p.id, p.title, p.author, p.language, p.filename, p.page_count,
                   p.file_size, p.created_at, p.category_id,
                   snippet(pdf_fts, 2, '<mark>', '</mark>', '…', 32) AS snippet
            FROM pdf_fts JOIN pdf_documents p ON pdf_fts.rowid = p.id
            WHERE pdf_fts MATCH ? ORDER BY rank LIMIT ?
        """, (q, limit)).fetchall()
    except Exception:
        like = f"%{q}%"
        rows = conn.execute("""
            SELECT id, title, author, language, filename, page_count, file_size,
                   created_at, category_id, SUBSTR(extracted_text, 1, 200) AS snippet
            FROM pdf_documents
            WHERE title LIKE ? OR author LIKE ? OR extracted_text LIKE ?
            LIMIT ?
        """, (like, like, like, limit)).fetchall()
    finally:
        conn.close()
    return {"query": q, "count": len(rows), "results": [dict(r) for r in rows]}


@router.get("/")
async def list_pdfs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user = Depends(get_current_user),
):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM pdf_documents").fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute("""
        SELECT p.id, p.title, p.author, p.language, p.filename, p.file_size,
               p.page_count, p.created_at, p.category_id, c.name AS category_name
        FROM pdf_documents p
        LEFT JOIN categories c ON c.id = p.category_id
        ORDER BY p.created_at DESC LIMIT ? OFFSET ?
    """, (page_size, offset)).fetchall()
    conn.close()
    return {"total": total, "page": page, "page_size": page_size, "results": [dict(r) for r in rows]}


@router.get("/{doc_id}")
async def get_pdf_meta(doc_id: int, user = Depends(get_current_user)):
    conn = get_db()
    row = conn.execute("""
        SELECT p.*, c.name AS category_name FROM pdf_documents p
        LEFT JOIN categories c ON c.id = p.category_id WHERE p.id = ?
    """, (doc_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="PDF not found")
    return _pdf_safe(row)


@router.get("/{doc_id}/view")
async def view_pdf(doc_id: int, request: Request):
    conn = get_db()
    row  = conn.execute("SELECT file_path, title FROM pdf_documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    if not row: raise HTTPException(status_code=404, detail="PDF not found")
    path = Path(row["file_path"])
    if not path.exists(): raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(str(path), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{row["title"]}.pdf"'})


@router.get("/{doc_id}/download")
async def download_pdf(doc_id: int, request: Request):
    conn = get_db()
    row  = conn.execute("SELECT file_path, title FROM pdf_documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    if not row: raise HTTPException(status_code=404, detail="PDF not found")
    path = Path(row["file_path"])
    if not path.exists(): raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(str(path), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{row["title"]}.pdf"'})


@router.delete("/{doc_id}", status_code=204)
async def delete_pdf(doc_id: int, user = Depends(require_role("admin"))):
    conn = get_db()
    row  = conn.execute("SELECT file_path FROM pdf_documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="PDF not found")
    conn.execute("DELETE FROM pdf_documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    Path(row["file_path"]).unlink(missing_ok=True)