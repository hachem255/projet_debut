"""
Smart Library API — main.py v6.1
Modules: auth · catalog · loans · recommendations · dashboard · search · pdfs

New in v6.1:
  - Public self-registration with email OTP (/auth/request-verification-code,
    /auth/verify-signup) — configure via GMAIL_EMAIL + GMAIL_APP_PASSWORD
  - GET /signup serves signup.html for the frontend registration page
  - .env file auto-loaded on startup (no extra dependency needed)

Windows-compatible: use `python main.py` (Waitress) or `uvicorn main:app --reload`
Production:         see server.py for Waitress / Gunicorn launchers
"""

import os
import sys
import time
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env file before anything else so env vars are available immediately
# ---------------------------------------------------------------------------
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("smart_library")

# ---------------------------------------------------------------------------
# Lazy imports (optional heavy deps)
# ---------------------------------------------------------------------------
try:
    from semantic_search import SemanticSearch, DatabaseFactory, FAISS_INDEX_PATH as _FAISS_INDEX_PATH
    SEARCH_AVAILABLE = True
except ImportError as e:
    log.warning(f"Semantic search unavailable: {e}")
    SEARCH_AVAILABLE = False
    _FAISS_INDEX_PATH = "library.index"

from auth            import router as auth_router,            get_current_user, require_role
from catalog         import router as catalog_router
from loans           import router as loans_router
from recommendations import router as recommendations_router
from dashboard       import router as dashboard_router
from pdfs            import router as pdfs_router

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Smart Library API",
    description=(
        "Intelligent library — search · catalog · loans · PDFs · "
        "recommendations · dashboard · self-registration"
    ),
    version="6.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Rate limiting (simple in-memory, per IP)
# ---------------------------------------------------------------------------
_rate_store: dict = defaultdict(list)
RATE_LIMIT  = int(os.getenv("RATE_LIMIT_REQUESTS", "200"))
RATE_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - RATE_WINDOW
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]
    if len(_rate_store[ip]) >= RATE_LIMIT:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {RATE_LIMIT} req/{RATE_WINDOW}s"},
        )
    _rate_store[ip].append(now)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Request timing
# ---------------------------------------------------------------------------
@app.middleware("http")
async def timing_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = (time.time() - start) * 1000
    response.headers["X-Process-Time"] = f"{ms:.1f}ms"
    return response


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(auth_router)
app.include_router(catalog_router)
app.include_router(loans_router)
app.include_router(recommendations_router)
app.include_router(dashboard_router)
app.include_router(pdfs_router)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

upload_dir = Path(os.getenv("PDF_UPLOAD_DIR", "uploads/pdfs"))
upload_dir.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Search engine initialisation
# ---------------------------------------------------------------------------
DB_PATH          = os.getenv("LIBRARY_DB_PATH", "library_search.db")
FAISS_INDEX_PATH = os.getenv("FAISS_INDEX_PATH", _FAISS_INDEX_PATH)

search_engine = None

if SEARCH_AVAILABLE:
    log.info("Connecting to database and loading FAISS index …")
    try:
        import faiss as _faiss
        db            = DatabaseFactory.create_database(db_type="sqlite", db_path=DB_PATH)
        search_engine = SemanticSearch(database=db)

        if os.path.exists(FAISS_INDEX_PATH):
            search_engine.faiss_index = _faiss.read_index(FAISS_INDEX_PATH)
            search_engine._faiss_rowids = [
                row[0] for row in db.get_all_documents_with_id()
            ]
            log.info(
                f"FAISS index loaded — {search_engine.faiss_index.ntotal} vectors, "
                f"{len(search_engine._faiss_rowids)} rowids."
            )
        else:
            try:
                search_engine.build_faiss_index()
                log.info("FAISS index built from existing DB documents.")
            except ValueError:
                log.info("DB empty — FAISS index will build on first document.")
    except Exception as e:
        log.error(f"Search engine init failed: {e}")
        search_engine = None

# ---------------------------------------------------------------------------
# Pydantic
# ---------------------------------------------------------------------------
class Document(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def serve_homepage():
    """Serve the main dashboard (index.html)."""
    for candidate in [
        Path(__file__).parent / "templates" / "index.html",
        Path(__file__).parent / "index.html",
    ]:
        if candidate.exists():
            return FileResponse(str(candidate))
    return {"message": "Smart Library API v6.1 — see /docs"}


@app.get("/signup", include_in_schema=False)
@app.get("/signup.html", include_in_schema=False)
async def serve_signup():
    """Serve the self-registration page (signup.html)."""
    for candidate in [
        Path(__file__).parent / "templates" / "signup.html",
        Path(__file__).parent / "signup.html",
    ]:
        if candidate.exists():
            return FileResponse(str(candidate))
    raise HTTPException(
        status_code=404,
        detail="signup.html not found. Place it next to main.py or in templates/.",
    )


# ---------------------------------------------------------------------------
# API info
# ---------------------------------------------------------------------------

@app.get("/api")
def api_info():
    return {
        "name": "Smart Library API",
        "version": "6.1.0",
        "endpoints": {
            "auth":            "/auth",
            "signup_page":     "/signup",
            "catalog":         "/catalog/books",
            "loans":           "/loans",
            "pdfs":            "/pdfs",
            "recommendations": "/recommendations/me",
            "dashboard":       "/dashboard/overview",
            "search":          "/search/",
            "docs":            "/docs",
        },
    }


@app.get("/stats")
async def get_stats(user=Depends(get_current_user)):
    """Global quick stats — all authenticated users."""
    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        def count(sql, p=()):
            try:
                return cursor.execute(sql, p).fetchone()[0] or 0
            except Exception:
                return 0

        result = {
            "total_books":       count("SELECT COUNT(*) FROM books"),
            "available_books":   count("SELECT COUNT(*) FROM books WHERE available_copies > 0"),
            "active_loans":      count("SELECT COUNT(*) FROM loans WHERE return_date IS NULL"),
            "overdue_loans":     count(
                "SELECT COUNT(*) FROM loans "
                "WHERE return_date IS NULL AND due_date < datetime('now')"
            ),
            "total_users":       count("SELECT COUNT(*) FROM users WHERE is_active = 1"),
            "indexed_documents": count("SELECT COUNT(*) FROM documents"),
            "total_pdfs":        (
                count("SELECT COUNT(*) FROM pdf_documents")
                if _table_exists(cursor, "pdf_documents") else 0
            ),
        }
        conn.close()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _table_exists(cursor, table_name: str) -> bool:
    return cursor.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()[0] > 0


@app.post("/add/")
async def add_document(doc: Document, user=Depends(require_role("librarian"))):
    if not search_engine:
        raise HTTPException(status_code=503, detail="Search engine not available")
    if not doc.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    try:
        search_engine.add_document(doc.text)
        return {"status": "success", "message": "Document indexed"}
    except Exception as e:
        log.error(f"add_document failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/")
async def search_documents(
    q: str,
    k: int = 5,
    page: int = 1,
    user=Depends(get_current_user),
):
    """Semantic search with pagination."""
    if not search_engine:
        raise HTTPException(status_code=503, detail="Search engine not available")
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    page_size = k
    offset    = (page - 1) * page_size
    fetch_k   = offset + page_size

    try:
        all_results = search_engine.retrieve(query=q, top_k=fetch_k)
        paginated   = all_results[offset:offset + page_size]
        return {
            "query":     q,
            "total":     len(all_results),
            "page":      page,
            "page_size": page_size,
            "results":   paginated,
        }
    except Exception as e:
        log.error(f"search failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    db_ok    = search_engine is not None and search_engine.db is not None
    faiss_ok = search_engine is not None and search_engine.faiss_index is not None
    vectors  = search_engine.faiss_index.ntotal if faiss_ok else 0
    return {
        "status":   "running",
        "version":  "6.1.0",
        "database": "connected" if db_ok    else "disconnected",
        "faiss":    f"{vectors} vectors" if faiss_ok else "not loaded",
        "modules":  [
            "auth", "catalog", "loans", "pdfs",
            "recommendations", "dashboard", "search",
        ],
        "email_verification": bool(
            os.getenv("GMAIL_EMAIL") and os.getenv("GMAIL_APP_PASSWORD")
        ),
    }


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8000"))

    print("\n" + "=" * 60)
    print("  Smart Library API v6.1")
    print(f"  http://{HOST}:{PORT}")
    print(f"  Docs:   http://{HOST}:{PORT}/docs")
    print(f"  Signup: http://{HOST}:{PORT}/signup")
    print("=" * 60 + "\n")

    try:
        from waitress import serve
        log.info(f"Starting with Waitress on {HOST}:{PORT}")
        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        import uvicorn
        log.info(f"Waitress not found — starting with Uvicorn on {HOST}:{PORT}")
        uvicorn.run("main:app", host=HOST, port=PORT, reload=True)