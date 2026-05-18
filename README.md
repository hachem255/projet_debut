# Smart Library Platform v6.0

Full-stack university library management system — FastAPI + SQLite + FAISS + SQLite FTS5.

---

## What's New in v6.0

| Feature | Details |
|---|---|
| PDF Upload | Drag-and-drop upload, automatic text extraction (pdfplumber + pypdf fallback) |
| Full-Text Search | SQLite FTS5 for books AND PDF content — partial matches, case-insensitive |
| PDF Content Search | Search inside extracted PDF text with highlighted snippets |
| Category Management | Admin can create / edit / delete categories dynamically |
| Inline PDF Viewer | Browser renders PDFs inline; secure download with auth |
| Improved Catalog | Edit books, better validation, FTS5-backed search |
| Windows Support | Waitress production server replaces Gunicorn (no Unix signals) |
| Better Error Handling | Structured logging, graceful fallbacks, form validation |

---

## Folder Structure

```
smart_library/
├── main.py              ← FastAPI app, middleware, startup
├── server.py            ← Waitress / Gunicorn / Uvicorn launchers
├── auth.py              ← JWT auth, 4 roles, user management
├── catalog.py           ← Books CRUD + FTS5 + dynamic categories
├── pdfs.py              ← PDF upload, extraction, FTS5, view/download
├── loans.py             ← Borrow / return / notifications
├── recommendations.py   ← Collaborative + content-based recommendations
├── dashboard.py         ← Analytics & reporting
├── search.py            ← Semantic search (FAISS + sentence-transformers)
├── database_factory.py  ← SQLite wrapper
├── __init__.py
├── index.html           ← Full-stack frontend (RTL Arabic UI)
├── requirements.txt
├── uploads/
│   └── pdfs/            ← Uploaded PDF files (auto-created)
├── static/              ← CSS / JS / images (auto-created)
└── logs/                ← Access + error logs (auto-created)
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the development server

```bash
# Windows or Linux (Uvicorn with hot-reload)
python main.py

# OR explicit dev mode
python server.py --dev
```

### 3. Run in production

```bash
# Windows (Waitress — recommended)
python server.py

# Linux / Mac (Gunicorn)
python server.py --gunicorn --workers 4
```

### 4. Open the app

```
http://localhost:8000         → Frontend UI
http://localhost:8000/docs    → Swagger API docs
```

Default admin credentials: **admin / admin1234** (change immediately!)

---

## Environment Variables

```env
# Server
HOST=0.0.0.0
PORT=8000
WORKERS=5

# Database
LIBRARY_DB_PATH=library_search.db
FAISS_INDEX_PATH=library.index

# Auth
JWT_SECRET_KEY=change-this-to-a-long-random-string
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=7

# PDF uploads
PDF_UPLOAD_DIR=uploads/pdfs
PDF_MAX_FILE_MB=50

# CORS
ALLOWED_ORIGINS=https://yourdomain.com,https://app.yourdomain.com

# Rate limiting
RATE_LIMIT_REQUESTS=200
RATE_LIMIT_WINDOW_SEC=60

# Logging
LOG_LEVEL=info
```

Create a `.env` file and load it with `python-dotenv` if needed.

---

## API Endpoints Summary

### Authentication `/auth`
| Method | Path | Access |
|---|---|---|
| POST | `/auth/login` | Public |
| POST | `/auth/register` | Admin |
| POST | `/auth/refresh` | Authenticated |
| GET  | `/auth/me` | Authenticated |
| GET  | `/auth/users` | Admin |
| PATCH | `/auth/users/{username}/role` | Admin |
| PATCH | `/auth/users/{username}/deactivate` | Admin |

### Catalog `/catalog`
| Method | Path | Access |
|---|---|---|
| GET  | `/catalog/books` | All |
| POST | `/catalog/books` | Librarian+ |
| PUT  | `/catalog/books/{id}` | Librarian+ |
| DELETE | `/catalog/books/{id}` | Admin |
| GET  | `/catalog/categories` | All |
| POST | `/catalog/categories` | Admin |
| PUT  | `/catalog/categories/{id}` | Admin |
| DELETE | `/catalog/categories/{id}` | Admin |

### PDFs `/pdfs`
| Method | Path | Access |
|---|---|---|
| POST | `/pdfs/upload` | Librarian+ |
| GET  | `/pdfs/` | All |
| GET  | `/pdfs/search?q=...` | All |
| GET  | `/pdfs/{id}` | All |
| GET  | `/pdfs/{id}/view` | All |
| GET  | `/pdfs/{id}/download` | All |
| DELETE | `/pdfs/{id}` | Admin |

### Search `/search`
| Method | Path | Notes |
|---|---|---|
| GET | `/search/?q=...&k=5` | Semantic FAISS search |

---

## Role Hierarchy

```
student   → search, view catalog, manage own loans
teacher   → student + can request purchases
librarian → teacher + manage catalog, loans, upload PDFs
admin     → full access + user management, categories
```

---

## PDF Text Extraction

Files are processed immediately on upload:
1. **pdfplumber** (primary) — best for text-heavy PDFs with complex layouts
2. **pypdf** (fallback) — used if pdfplumber fails
3. Extracted text is stored in SQLite and indexed in the FTS5 virtual table
4. Duplicate detection via SHA-256 hash

For scanned (image-only) PDFs, install OCR support:
```bash
pip install pytesseract pdf2image Pillow
# Also install Tesseract binary: https://tesseract-ocr.github.io/tessdoc/Installation.html
```

---

## Production Checklist

- [ ] Change `JWT_SECRET_KEY` to a random 64-char hex string
- [ ] Change default admin password
- [ ] Set `ALLOWED_ORIGINS` to your domain(s)
- [ ] Put behind a reverse proxy (Nginx / Caddy) for HTTPS
- [ ] Set `PDF_UPLOAD_DIR` to a path with sufficient disk space
- [ ] Schedule regular SQLite backups (`VACUUM` + copy)
- [ ] Monitor `logs/access.log` and `logs/error.log`
