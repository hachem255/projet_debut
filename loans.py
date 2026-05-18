"""
Loans module — borrow requests, admin confirmation, return flow, notifications.

Loan limits by role:
  student   → max 3 books, 14 days
  teacher   → max 10 books, 30 days
  librarian → max 15 books, 60 days
  admin     → unlimited

Borrow flow:
  1. User calls POST /loans/borrow                      — creates a pending borrow request
  2. Admin/librarian calls POST /loans/confirm-borrow/{id} — confirms and opens the loan
  3. Admin/librarian calls POST /loans/reject-borrow/{id}  — rejects the borrow request

Return flow:
  1. User calls POST /loans/request-return/{loan_id}    — creates a pending return request
  2. Admin/librarian calls POST /loans/confirm-return/{loan_id} — confirms and closes the loan
  3. Admin/librarian calls POST /loans/reject-return/{loan_id}  — rejects the request

Endpoints:
  POST  /loans/borrow                  — request to borrow      (all authenticated)
  POST  /loans/confirm-borrow/{id}     — confirm borrow request (librarian+)
  POST  /loans/reject-borrow/{id}      — reject borrow request  (librarian+)
  GET   /loans/pending-borrows         — pending borrow requests (librarian+)
  POST  /loans/request-return/{id}     — request to return      (all authenticated)
  POST  /loans/confirm-return/{id}     — confirm return         (librarian+)
  POST  /loans/reject-return/{id}      — reject return request  (librarian+)
  GET   /loans/pending-returns         — pending return requests (librarian+)
  GET   /loans/my                      — my loans               (all authenticated)
  GET   /loans/overdue                 — overdue loans          (librarian+)
  GET   /loans/all                     — all loans              (librarian+)
  GET   /loans/user/{user_id}          — loans by user          (librarian+)
  GET   /loans/notifications           — my due-soon alerts     (all authenticated)
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user, require_role

DB_PATH = os.getenv("LIBRARY_DB_PATH", "library_search.db")

# ---------------------------------------------------------------------------
# Loan policy per role
# ---------------------------------------------------------------------------
LOAN_POLICY = {
    "student":   {"max_books": 3,  "days": 14},
    "teacher":   {"max_books": 10, "days": 30},
    "librarian": {"max_books": 15, "days": 60},
    "admin":     {"max_books": 99, "days": 90},
}

DUE_SOON_DAYS = 3

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_loans_table():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS loans (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id      INTEGER REFERENCES books(id) ON DELETE SET NULL,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            loan_date    TEXT NOT NULL DEFAULT (datetime('now')),
            due_date     TEXT NOT NULL,
            return_date  TEXT,
            returned_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            notes        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_loans_user    ON loans(user_id);
        CREATE INDEX IF NOT EXISTS idx_loans_book    ON loans(book_id);
        CREATE INDEX IF NOT EXISTS idx_loans_active  ON loans(return_date) WHERE return_date IS NULL;

        CREATE TABLE IF NOT EXISTS return_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id      INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_at TEXT NOT NULL DEFAULT (datetime('now')),
            status       TEXT NOT NULL DEFAULT 'pending',
            reviewed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at  TEXT,
            notes        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_rr_loan   ON return_requests(loan_id);
        CREATE INDEX IF NOT EXISTS idx_rr_status ON return_requests(status);

        CREATE TABLE IF NOT EXISTS borrow_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_at TEXT NOT NULL DEFAULT (datetime('now')),
            status       TEXT NOT NULL DEFAULT 'pending',
            reviewed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reviewed_at  TEXT,
            notes        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_br_book   ON borrow_requests(book_id);
        CREATE INDEX IF NOT EXISTS idx_br_user   ON borrow_requests(user_id);
        CREATE INDEX IF NOT EXISTS idx_br_status ON borrow_requests(status);
    """)
    conn.commit()

    # Migration: nullable book_id
    col_info = conn.execute("PRAGMA table_info(loans)").fetchall()
    book_id_col = next((c for c in col_info if c["name"] == "book_id"), None)
    if book_id_col and book_id_col["notnull"] == 1:
        conn.executescript("""
            PRAGMA foreign_keys = OFF;
            ALTER TABLE loans RENAME TO loans_old;
            CREATE TABLE loans (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id      INTEGER REFERENCES books(id) ON DELETE SET NULL,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                loan_date    TEXT NOT NULL DEFAULT (datetime('now')),
                due_date     TEXT NOT NULL,
                return_date  TEXT,
                returned_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
                notes        TEXT
            );
            INSERT INTO loans SELECT * FROM loans_old;
            DROP TABLE loans_old;
            CREATE INDEX IF NOT EXISTS idx_loans_user   ON loans(user_id);
            CREATE INDEX IF NOT EXISTS idx_loans_book   ON loans(book_id);
            CREATE INDEX IF NOT EXISTS idx_loans_active ON loans(return_date) WHERE return_date IS NULL;
            PRAGMA foreign_keys = ON;
        """)
        conn.commit()
        print("INFO: loans table migrated — book_id is now nullable.")

    conn.close()


init_loans_table()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class BorrowRequest(BaseModel):
    book_id: int
    notes: Optional[str] = None


class ReturnNotes(BaseModel):
    notes: Optional[str] = None


class LoanOut(BaseModel):
    id: int
    book_id: Optional[int]
    book_title: Optional[str]
    book_author: Optional[str]
    book_isbn: Optional[str] = None
    book_section: Optional[str] = None
    book_shelf: Optional[str] = None
    book_position: Optional[str] = None
    book_summary: Optional[str] = None
    book_category_id: Optional[int] = None
    user_id: int
    username: Optional[str]
    loan_date: str
    due_date: str
    return_date: Optional[str]
    is_overdue: bool
    days_remaining: Optional[int]
    notes: Optional[str]
    return_request_status: Optional[str] = None   # pending / confirmed / rejected / None


class BorrowRequestOut(BaseModel):
    id: int
    book_id: int
    book_title: Optional[str]
    book_author: Optional[str]
    user_id: int
    username: Optional[str]
    requested_at: str
    status: str
    notes: Optional[str] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enrich_loan(row: dict) -> dict:
    now = datetime.now(timezone.utc)
    if row.get("return_date"):
        row["is_overdue"]     = False
        row["days_remaining"] = None
    else:
        due   = datetime.fromisoformat(row["due_date"]).replace(tzinfo=timezone.utc)
        delta = (due - now).days
        row["is_overdue"]     = delta < 0
        row["days_remaining"] = delta
    return row


def _get_loan_detail(conn, loan_id: int) -> Optional[dict]:
    row = conn.execute("""
        SELECT l.*,
               b.title       AS book_title,
               b.author      AS book_author,
               b.section     AS book_section,
               b.shelf       AS book_shelf,
               b.position    AS book_position,
               b.summary     AS book_summary,
               b.isbn        AS book_isbn,
               b.category_id AS book_category_id,
               u.username,
               rr.status     AS return_request_status
        FROM loans l
        LEFT JOIN books b  ON b.id = l.book_id
        JOIN  users u      ON u.id = l.user_id
        LEFT JOIN return_requests rr
               ON rr.loan_id = l.id AND rr.status = 'pending'
        WHERE l.id = ?
    """, (loan_id,)).fetchone()
    return _enrich_loan(dict(row)) if row else None

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/loans", tags=["Loans"])


# ── Request borrow (user) ─────────────────────────────────────────────────────

@router.post("/borrow", response_model=BorrowRequestOut, status_code=201)
async def borrow_book(data: BorrowRequest, user=Depends(get_current_user)):
    """
    User requests to borrow a book.
    Creates a pending borrow request that admin/librarian must confirm.
    """
    role   = user["role"]
    policy = LOAN_POLICY.get(role, LOAN_POLICY["student"])
    conn   = get_db()

    book = conn.execute("SELECT * FROM books WHERE id = ?", (data.book_id,)).fetchone()
    if not book:
        conn.close()
        raise HTTPException(status_code=404, detail="Book not found")
    if book["available_copies"] < 1:
        conn.close()
        raise HTTPException(status_code=409, detail="No available copies for this book")

    active_count = conn.execute(
        "SELECT COUNT(*) FROM loans WHERE user_id = ? AND return_date IS NULL",
        (user["id"],)
    ).fetchone()[0]
    if active_count >= policy["max_books"]:
        conn.close()
        raise HTTPException(
            status_code=409,
            detail=(
                f"Loan limit reached: your role '{role}' allows "
                f"max {policy['max_books']} active loan(s). "
                f"You currently have {active_count}."
            )
        )

    already_loan = conn.execute(
        "SELECT id FROM loans WHERE user_id = ? AND book_id = ? AND return_date IS NULL",
        (user["id"], data.book_id)
    ).fetchone()
    if already_loan:
        conn.close()
        raise HTTPException(status_code=409, detail="You already have an active loan for this book")

    existing_request = conn.execute(
        "SELECT id FROM borrow_requests WHERE user_id = ? AND book_id = ? AND status = 'pending'",
        (user["id"], data.book_id)
    ).fetchone()
    if existing_request:
        conn.close()
        raise HTTPException(status_code=409, detail="You already have a pending borrow request for this book")

    cursor = conn.execute(
        "INSERT INTO borrow_requests (book_id, user_id, notes) VALUES (?, ?, ?)",
        (data.book_id, user["id"], data.notes)
    )
    conn.commit()
    req_id = cursor.lastrowid

    row = conn.execute("""
        SELECT br.*, b.title AS book_title, b.author AS book_author, u.username
        FROM borrow_requests br
        JOIN books b ON b.id = br.book_id
        JOIN users u ON u.id = br.user_id
        WHERE br.id = ?
    """, (req_id,)).fetchone()
    conn.close()
    return dict(row)


# ── Confirm borrow (librarian+) ───────────────────────────────────────────────

@router.post("/confirm-borrow/{request_id}", response_model=LoanOut)
async def confirm_borrow(
    request_id: int,
    body: ReturnNotes = ReturnNotes(),
    user=Depends(require_role("librarian")),
):
    """
    Admin or librarian confirms the borrow request and opens the loan.
    """
    conn = get_db()
    req = conn.execute("SELECT * FROM borrow_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        raise HTTPException(status_code=404, detail="Borrow request not found")
    if req["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=409, detail=f"Request is already '{req['status']}'")

    book = conn.execute("SELECT * FROM books WHERE id = ?", (req["book_id"],)).fetchone()
    if not book:
        conn.close()
        raise HTTPException(status_code=404, detail="Book no longer exists")
    if book["available_copies"] < 1:
        conn.close()
        raise HTTPException(status_code=409, detail="No available copies for this book anymore")

    borrower_role = conn.execute(
        "SELECT role FROM users WHERE id = ?", (req["user_id"],)
    ).fetchone()
    role_name = borrower_role["role"] if borrower_role else "student"
    policy    = LOAN_POLICY.get(role_name, LOAN_POLICY["student"])

    now      = datetime.now(timezone.utc)
    due_date = now + timedelta(days=policy["days"])

    cursor = conn.execute("""
        INSERT INTO loans (book_id, user_id, loan_date, due_date, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (
        req["book_id"], req["user_id"],
        now.strftime("%Y-%m-%d %H:%M:%S"),
        due_date.strftime("%Y-%m-%d %H:%M:%S"),
        body.notes,
    ))
    loan_id = cursor.lastrowid

    conn.execute(
        "UPDATE books SET available_copies = available_copies - 1 WHERE id = ?",
        (req["book_id"],)
    )
    conn.execute("""
        UPDATE borrow_requests
        SET status = 'confirmed', reviewed_by = ?, reviewed_at = datetime('now')
        WHERE id = ?
    """, (user["id"], request_id))
    conn.commit()

    result = _get_loan_detail(conn, loan_id)
    conn.close()
    return result


# ── Reject borrow (librarian+) ────────────────────────────────────────────────

@router.post("/reject-borrow/{request_id}")
async def reject_borrow(
    request_id: int,
    body: ReturnNotes = ReturnNotes(),
    user=Depends(require_role("librarian")),
):
    """
    Admin or librarian rejects the borrow request.
    """
    conn = get_db()
    req = conn.execute("SELECT * FROM borrow_requests WHERE id = ?", (request_id,)).fetchone()
    if not req:
        conn.close()
        raise HTTPException(status_code=404, detail="Borrow request not found")
    if req["status"] != "pending":
        conn.close()
        raise HTTPException(status_code=409, detail=f"Request is already '{req['status']}'")

    conn.execute("""
        UPDATE borrow_requests
        SET status = 'rejected', reviewed_by = ?, reviewed_at = datetime('now'),
            notes = COALESCE(?, notes)
        WHERE id = ?
    """, (user["id"], body.notes, request_id))
    conn.commit()
    conn.close()
    return {"message": "Borrow request rejected."}


# ── Pending borrow requests (librarian+) ──────────────────────────────────────

@router.get("/pending-borrows")
async def pending_borrows(user=Depends(require_role("librarian"))):
    """List all pending borrow requests awaiting confirmation."""
    conn = get_db()
    rows = conn.execute("""
        SELECT
            br.id          AS request_id,
            br.requested_at,
            br.notes       AS request_notes,
            b.id           AS book_id,
            b.title        AS book_title,
            b.author       AS book_author,
            b.isbn         AS book_isbn,
            b.available_copies,
            u.id           AS user_id,
            u.username,
            u.full_name,
            u.email,
            u.role         AS user_role
        FROM borrow_requests br
        JOIN books b ON b.id = br.book_id
        JOIN users u ON u.id = br.user_id
        WHERE br.status = 'pending'
        ORDER BY br.requested_at ASC
    """).fetchall()
    conn.close()
    return {"count": len(rows), "requests": [dict(r) for r in rows]}


# ── Request return (user) ─────────────────────────────────────────────────────

@router.post("/request-return/{loan_id}")
async def request_return(
    loan_id: int,
    body: ReturnNotes = ReturnNotes(),
    user=Depends(get_current_user),
):
    """
    User asks to return a book.
    Creates a pending return request that admin/librarian must confirm.
    """
    conn = get_db()
    loan = conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan["return_date"] is not None:
        conn.close()
        raise HTTPException(status_code=409, detail="This loan has already been returned")
    if loan["user_id"] != user["id"]:
        from auth import ROLE_HIERARCHY
        if ROLE_HIERARCHY.get(user["role"], 0) < ROLE_HIERARCHY["librarian"]:
            conn.close()
            raise HTTPException(status_code=403, detail="You can only request return for your own loans")

    # Check no pending request already exists
    existing = conn.execute(
        "SELECT id FROM return_requests WHERE loan_id = ? AND status = 'pending'",
        (loan_id,)
    ).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="A return request is already pending for this loan")

    conn.execute(
        "INSERT INTO return_requests (loan_id, user_id, notes) VALUES (?, ?, ?)",
        (loan_id, user["id"], body.notes)
    )
    conn.commit()
    conn.close()
    return {"message": "Return request submitted. Waiting for admin/librarian confirmation."}


# ── Confirm return (librarian+) ───────────────────────────────────────────────

@router.post("/confirm-return/{loan_id}", response_model=LoanOut)
async def confirm_return(
    loan_id: int,
    body: ReturnNotes = ReturnNotes(),
    user=Depends(require_role("librarian")),
):
    """
    Admin or librarian confirms the return request and closes the loan.
    """
    conn = get_db()

    loan = conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="Loan not found")
    if loan["return_date"] is not None:
        conn.close()
        raise HTTPException(status_code=409, detail="This loan has already been returned")

    pending = conn.execute(
        "SELECT id FROM return_requests WHERE loan_id = ? AND status = 'pending'",
        (loan_id,)
    ).fetchone()
    if not pending:
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="No pending return request found for this loan"
        )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Close the loan
    conn.execute("""
        UPDATE loans
        SET return_date = ?, returned_by = ?, notes = COALESCE(?, notes)
        WHERE id = ?
    """, (now, user["id"], body.notes, loan_id))

    # Restore book copy
    conn.execute(
        "UPDATE books SET available_copies = available_copies + 1 WHERE id = ?",
        (loan["book_id"],)
    )

    # Mark request as confirmed
    conn.execute("""
        UPDATE return_requests
        SET status = 'confirmed', reviewed_by = ?, reviewed_at = ?
        WHERE loan_id = ? AND status = 'pending'
    """, (user["id"], now, loan_id))

    conn.commit()
    result = _get_loan_detail(conn, loan_id)
    conn.close()
    return result


# ── Reject return (librarian+) ────────────────────────────────────────────────

@router.post("/reject-return/{loan_id}")
async def reject_return(
    loan_id: int,
    body: ReturnNotes = ReturnNotes(),
    user=Depends(require_role("librarian")),
):
    """
    Admin or librarian rejects the return request (book not physically received).
    """
    conn = get_db()

    pending = conn.execute(
        "SELECT id FROM return_requests WHERE loan_id = ? AND status = 'pending'",
        (loan_id,)
    ).fetchone()
    if not pending:
        conn.close()
        raise HTTPException(status_code=404, detail="No pending return request for this loan")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        UPDATE return_requests
        SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, notes = COALESCE(?, notes)
        WHERE loan_id = ? AND status = 'pending'
    """, (user["id"], now, body.notes, loan_id))
    conn.commit()
    conn.close()
    return {"message": "Return request rejected. The loan remains active."}


# ── Pending return requests (librarian+) ──────────────────────────────────────

@router.get("/pending-returns")
async def pending_returns(user=Depends(require_role("librarian"))):
    """
    List all pending return requests awaiting admin/librarian confirmation.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            rr.id          AS request_id,
            rr.requested_at,
            rr.notes       AS request_notes,
            l.id           AS loan_id,
            l.loan_date,
            l.due_date,
            b.title        AS book_title,
            b.author       AS book_author,
            b.isbn         AS book_isbn,
            b.section      AS book_section,
            b.shelf        AS book_shelf,
            u.id           AS user_id,
            u.username,
            u.full_name,
            u.email,
            CAST(julianday('now') - julianday(l.due_date) AS INTEGER) AS days_overdue
        FROM return_requests rr
        JOIN loans l ON l.id = rr.loan_id
        LEFT JOIN books b ON b.id = l.book_id
        JOIN users u ON u.id = l.user_id
        WHERE rr.status = 'pending'
        ORDER BY rr.requested_at ASC
    """).fetchall()
    conn.close()
    return {"count": len(rows), "requests": [dict(r) for r in rows]}


# ── My borrow requests ───────────────────────────────────────────────────────

@router.get("/my-borrow-requests")
async def my_borrow_requests(user=Depends(get_current_user)):
    """Return all borrow requests submitted by the current user."""
    conn = get_db()
    rows = conn.execute("""
        SELECT br.*, b.title AS book_title, b.author AS book_author,
               b.available_copies
        FROM borrow_requests br
        JOIN books b ON b.id = br.book_id
        WHERE br.user_id = ?
        ORDER BY br.requested_at DESC
    """, (user["id"],)).fetchall()
    conn.close()
    return {"count": len(rows), "requests": [dict(r) for r in rows]}


# ── My loans ─────────────────────────────────────────────────────────────────

@router.get("/my", response_model=list[LoanOut])
async def my_loans(
    active_only: bool = Query(False),
    user=Depends(get_current_user),
):
    conn = get_db()
    where  = "WHERE l.user_id = ?"
    params = [user["id"]]
    if active_only:
        where += " AND l.return_date IS NULL"

    rows = conn.execute(f"""
        SELECT l.*, b.title AS book_title, b.author AS book_author,
               b.section AS book_section, b.shelf AS book_shelf,
               b.position AS book_position, b.summary AS book_summary,
               b.isbn AS book_isbn, u.username,
               rr.status AS return_request_status
        FROM loans l
        LEFT JOIN books b ON b.id = l.book_id
        JOIN  users u     ON u.id = l.user_id
        LEFT JOIN return_requests rr
               ON rr.loan_id = l.id AND rr.status = 'pending'
        {where}
        ORDER BY l.loan_date DESC
    """, params).fetchall()
    conn.close()
    return [_enrich_loan(dict(r)) for r in rows]


# ── Due-soon notifications ────────────────────────────────────────────────────

@router.get("/notifications")
async def my_notifications(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("""
        SELECT l.*, b.title AS book_title, b.author AS book_author,
               b.section AS book_section, b.shelf AS book_shelf,
               b.position AS book_position, b.summary AS book_summary,
               b.isbn AS book_isbn,
               rr.status AS return_request_status
        FROM loans l
        LEFT JOIN books b ON b.id = l.book_id
        LEFT JOIN return_requests rr
               ON rr.loan_id = l.id AND rr.status = 'pending'
        WHERE l.user_id = ? AND l.return_date IS NULL
        ORDER BY l.due_date ASC
    """, (user["id"],)).fetchall()
    conn.close()

    alerts = []
    for r in rows:
        enriched = _enrich_loan(dict(r))
        days = enriched["days_remaining"]
        if enriched["is_overdue"]:
            enriched["alert_type"]    = "overdue"
            enriched["alert_message"] = f"Overdue by {abs(days)} day(s)! Please return immediately."
            alerts.append(enriched)
        elif days is not None and days <= DUE_SOON_DAYS:
            enriched["alert_type"]    = "due_soon"
            enriched["alert_message"] = (
                "Due today!" if days == 0 else
                "Due tomorrow!" if days == 1 else
                f"Due in {days} day(s)."
            )
            alerts.append(enriched)

    return {"count": len(alerts), "alerts": alerts}


# ── All loans (librarian+) ────────────────────────────────────────────────────

@router.get("/all")
async def all_loans(
    active_only: bool = Query(False),
    page:        int  = Query(1,  ge=1),
    page_size:   int  = Query(30, ge=1, le=100),
    user=Depends(require_role("librarian")),
):
    conn  = get_db()
    where = "WHERE l.return_date IS NULL" if active_only else ""
    total = conn.execute(f"SELECT COUNT(*) FROM loans l {where}").fetchone()[0]
    offset = (page - 1) * page_size

    rows = conn.execute(f"""
        SELECT l.*, b.title AS book_title, b.author AS book_author,
               b.section AS book_section, b.shelf AS book_shelf,
               b.position AS book_position, b.summary AS book_summary,
               b.isbn AS book_isbn, u.username,
               rr.status AS return_request_status
        FROM loans l
        LEFT JOIN books b ON b.id = l.book_id
        JOIN  users u     ON u.id = l.user_id
        LEFT JOIN return_requests rr
               ON rr.loan_id = l.id AND rr.status = 'pending'
        {where}
        ORDER BY l.loan_date DESC
        LIMIT ? OFFSET ?
    """, [page_size, offset]).fetchall()
    conn.close()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "results":   [_enrich_loan(dict(r)) for r in rows],
    }


# ── Overdue loans (librarian+) ────────────────────────────────────────────────

@router.get("/overdue")
async def overdue_loans(user=Depends(require_role("librarian"))):
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    rows = conn.execute("""
        SELECT l.*, b.title AS book_title, b.author AS book_author,
               b.section AS book_section, b.shelf AS book_shelf,
               b.position AS book_position, b.summary AS book_summary,
               b.isbn AS book_isbn, u.username, u.email,
               rr.status AS return_request_status
        FROM loans l
        LEFT JOIN books b ON b.id = l.book_id
        JOIN  users u     ON u.id = l.user_id
        LEFT JOIN return_requests rr
               ON rr.loan_id = l.id AND rr.status = 'pending'
        WHERE l.return_date IS NULL AND l.due_date < ?
        ORDER BY l.due_date ASC
    """, (now,)).fetchall()
    conn.close()
    return {"count": len(rows), "loans": [_enrich_loan(dict(r)) for r in rows]}


# ── Loans by user (librarian+) ────────────────────────────────────────────────

@router.get("/user/{target_user_id}", response_model=list[LoanOut])
async def user_loans(
    target_user_id: int,
    active_only: bool = Query(False),
    user=Depends(require_role("librarian")),
):
    conn   = get_db()
    where  = "WHERE l.user_id = ?"
    params = [target_user_id]
    if active_only:
        where += " AND l.return_date IS NULL"

    rows = conn.execute(f"""
        SELECT l.*, b.title AS book_title, b.author AS book_author,
               b.section AS book_section, b.shelf AS book_shelf,
               b.position AS book_position, b.summary AS book_summary,
               b.isbn AS book_isbn, u.username,
               rr.status AS return_request_status
        FROM loans l
        LEFT JOIN books b ON b.id = l.book_id
        JOIN  users u     ON u.id = l.user_id
        LEFT JOIN return_requests rr
               ON rr.loan_id = l.id AND rr.status = 'pending'
        {where}
        ORDER BY l.loan_date DESC
    """, params).fetchall()
    conn.close()
    return [_enrich_loan(dict(r)) for r in rows]