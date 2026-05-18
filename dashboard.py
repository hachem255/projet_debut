"""
Dashboard module — analytics and reporting for admins and librarians.

Endpoints:
  GET /dashboard/overview          — KPI cards (librarian+)
  GET /dashboard/loans-over-time   — loans per day/week/month chart data
  GET /dashboard/top-books         — most borrowed books
  GET /dashboard/top-users         — most active users
  GET /dashboard/category-stats    — distribution by category
  GET /dashboard/activity          — recent activity feed (librarian+)
  GET /dashboard/overdue-summary   — overdue snapshot
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query

from auth import require_role, get_current_user

DB_PATH = os.getenv("LIBRARY_DB_PATH", "library_search.db")

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_count(cursor, sql: str, params=()) -> int:
    try:
        return cursor.execute(sql, params).fetchone()[0] or 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/overview")
async def overview(user=Depends(require_role("librarian"))):
    """
    Main KPI cards shown at the top of the dashboard.
    Returns counts, deltas vs last 30 days, and quick ratios.
    """
    conn   = get_db()
    cursor = conn.cursor()
    now    = datetime.now(timezone.utc)
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    total_books        = _safe_count(cursor, "SELECT COUNT(*) FROM books")
    available_books    = _safe_count(cursor, "SELECT COUNT(*) FROM books WHERE available_copies > 0")
    borrowed_books     = total_books - available_books
    digital_books      = _safe_count(cursor, "SELECT COUNT(*) FROM books WHERE is_digital = 1")

    total_users        = _safe_count(cursor, "SELECT COUNT(*) FROM users WHERE is_active = 1")
    new_users_month    = _safe_count(cursor, "SELECT COUNT(*) FROM users WHERE created_at >= ?", (month_ago,))

    active_loans       = _safe_count(cursor, "SELECT COUNT(*) FROM loans WHERE return_date IS NULL")
    overdue_loans      = _safe_count(cursor,
        "SELECT COUNT(*) FROM loans WHERE return_date IS NULL AND due_date < datetime('now')")
    loans_this_month   = _safe_count(cursor, "SELECT COUNT(*) FROM loans WHERE loan_date >= ?", (month_ago,))
    returns_this_month = _safe_count(cursor,
        "SELECT COUNT(*) FROM loans WHERE return_date >= ?", (month_ago,))

    total_categories   = _safe_count(cursor, "SELECT COUNT(*) FROM categories")

    conn.close()

    availability_rate = round((available_books / total_books * 100), 1) if total_books else 0
    overdue_rate      = round((overdue_loans  / active_loans  * 100), 1) if active_loans else 0

    return {
        "books": {
            "total":        total_books,
            "available":    available_books,
            "borrowed":     borrowed_books,
            "digital":      digital_books,
            "availability_rate_pct": availability_rate,
        },
        "users": {
            "total_active": total_users,
            "new_this_month": new_users_month,
        },
        "loans": {
            "active":           active_loans,
            "overdue":          overdue_loans,
            "overdue_rate_pct": overdue_rate,
            "issued_this_month":   loans_this_month,
            "returned_this_month": returns_this_month,
        },
        "catalog": {
            "total_categories": total_categories,
        },
        "generated_at": now.isoformat(),
    }


@router.get("/loans-over-time")
async def loans_over_time(
    period: Literal["day", "week", "month"] = Query("day"),
    days:   int = Query(30, ge=7, le=365),
    user=Depends(require_role("librarian")),
):
    """
    Time-series data for the loans chart.
    Returns [{date, issued, returned}] for the last N days,
    grouped by day / week / month.
    """
    conn  = get_db()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    if period == "day":
        fmt = "%Y-%m-%d"
    elif period == "week":
        fmt = "%Y-W%W"
    else:
        fmt = "%Y-%m"

    try:
        issued = conn.execute(f"""
            SELECT strftime('{fmt}', loan_date) AS period, COUNT(*) AS cnt
            FROM loans WHERE loan_date >= ?
            GROUP BY period ORDER BY period
        """, (since,)).fetchall()

        returned = conn.execute(f"""
            SELECT strftime('{fmt}', return_date) AS period, COUNT(*) AS cnt
            FROM loans WHERE return_date >= ?
            GROUP BY period ORDER BY period
        """, (since,)).fetchall()
    finally:
        conn.close()

    issued_map   = {r["period"]: r["cnt"] for r in issued}
    returned_map = {r["period"]: r["cnt"] for r in returned}
    all_periods  = sorted(set(issued_map) | set(returned_map))

    return {
        "period": period,
        "days":   days,
        "series": [
            {
                "date":     p,
                "issued":   issued_map.get(p, 0),
                "returned": returned_map.get(p, 0),
            }
            for p in all_periods
        ],
    }


@router.get("/top-books")
async def top_books(
    limit:   int = Query(10, ge=1, le=50),
    days:    int = Query(90, ge=1, le=365),
    user=Depends(require_role("librarian")),
):
    """Most borrowed books in the last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn  = get_db()

    rows = conn.execute("""
        SELECT b.id, b.title, b.author, c.name AS category,
               COUNT(l.id) AS borrow_count,
               b.available_copies, b.total_copies
        FROM loans l
        JOIN  books b ON b.id = l.book_id
        LEFT JOIN categories c ON c.id = b.category_id
        WHERE l.loan_date >= ?
        GROUP BY b.id
        ORDER BY borrow_count DESC
        LIMIT ?
    """, (since, limit)).fetchall()

    conn.close()
    return {"days": days, "books": [dict(r) for r in rows]}


@router.get("/top-users")
async def top_users(
    limit: int = Query(10, ge=1, le=50),
    days:  int = Query(90, ge=1, le=365),
    user=Depends(require_role("librarian")),
):
    """Most active borrowers in the last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn  = get_db()

    rows = conn.execute("""
        SELECT u.id, u.username, u.full_name, u.role,
               COUNT(l.id)  AS total_loans,
               SUM(CASE WHEN l.return_date IS NULL THEN 1 ELSE 0 END) AS active_loans,
               SUM(CASE WHEN l.return_date IS NULL
                         AND l.due_date < datetime('now') THEN 1 ELSE 0 END) AS overdue_loans
        FROM loans l
        JOIN users u ON u.id = l.user_id
        WHERE l.loan_date >= ?
        GROUP BY u.id
        ORDER BY total_loans DESC
        LIMIT ?
    """, (since, limit)).fetchall()

    conn.close()
    return {"days": days, "users": [dict(r) for r in rows]}


@router.get("/category-stats")
async def category_stats(user=Depends(require_role("librarian"))):
    """Books and loans distribution by category — for pie/bar charts."""
    conn = get_db()

    rows = conn.execute("""
        SELECT c.id, c.name,
               COUNT(DISTINCT b.id)  AS total_books,
               COALESCE(SUM(b.total_copies), 0)     AS total_copies,
               COALESCE(SUM(b.available_copies), 0) AS available_copies,
               COUNT(l.id)           AS total_loans
        FROM categories c
        LEFT JOIN books b  ON b.category_id = c.id
        LEFT JOIN loans l  ON l.book_id = b.id
        GROUP BY c.id
        ORDER BY total_loans DESC
    """).fetchall()

    conn.close()
    data = [dict(r) for r in rows]

    total_books = sum(r["total_books"] for r in data) or 1
    for r in data:
        r["books_pct"] = round(r["total_books"] / total_books * 100, 1)

    return {"categories": data}


@router.get("/activity")
async def recent_activity(
    limit: int = Query(20, ge=1, le=100),
    user=Depends(require_role("librarian")),
):
    """
    Recent activity feed — new loans, returns, new books, new users.
    Each event has: type, description, timestamp, actor.
    """
    conn   = get_db()
    events = []

    # Recent loans
    for r in conn.execute("""
        SELECT l.loan_date AS ts, u.username, b.title
        FROM loans l
        JOIN users u ON u.id = l.user_id
        JOIN books b ON b.id = l.book_id
        ORDER BY l.loan_date DESC LIMIT ?
    """, (limit,)).fetchall():
        events.append({
            "type":      "loan",
            "timestamp": r["ts"],
            "message":   f"{r['username']} borrowed \"{r['title']}\"",
        })

    # Recent returns
    for r in conn.execute("""
        SELECT l.return_date AS ts, u.username, b.title
        FROM loans l
        JOIN users u ON u.id = l.user_id
        JOIN books b ON b.id = l.book_id
        WHERE l.return_date IS NOT NULL
        ORDER BY l.return_date DESC LIMIT ?
    """, (limit,)).fetchall():
        events.append({
            "type":      "return",
            "timestamp": r["ts"],
            "message":   f"{r['username']} returned \"{r['title']}\"",
        })

    # Recent book additions
    for r in conn.execute("""
        SELECT b.created_at AS ts, b.title, u.username AS added_by
        FROM books b
        LEFT JOIN users u ON u.id = b.added_by
        ORDER BY b.created_at DESC LIMIT ?
    """, (limit,)).fetchall():
        events.append({
            "type":      "new_book",
            "timestamp": r["ts"],
            "message":   f"Book added: \"{r['title']}\" by {r['added_by'] or 'system'}",
        })

    # Recent registrations
    for r in conn.execute("""
        SELECT created_at AS ts, username, role
        FROM users
        ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall():
        events.append({
            "type":      "new_user",
            "timestamp": r["ts"],
            "message":   f"New {r['role']} registered: {r['username']}",
        })

    conn.close()

    events.sort(key=lambda e: e["timestamp"] or "", reverse=True)
    return {"count": len(events[:limit]), "events": events[:limit]}


@router.get("/overdue-summary")
async def overdue_summary(user=Depends(require_role("librarian"))):
    """Overdue loans grouped by how many days late."""
    conn = get_db()
    rows = conn.execute("""
        SELECT
            l.id, l.due_date, l.loan_date,
            b.title, b.author,
            u.username, u.email,
            CAST(julianday('now') - julianday(l.due_date) AS INTEGER) AS days_overdue
        FROM loans l
        JOIN books b ON b.id = l.book_id
        JOIN users u ON u.id = l.user_id
        WHERE l.return_date IS NULL AND l.due_date < datetime('now')
        ORDER BY days_overdue DESC
    """).fetchall()
    conn.close()

    data = [dict(r) for r in rows]

    buckets = {"1-7 days": [], "8-30 days": [], "30+ days": []}
    for r in data:
        d = r["days_overdue"]
        if d <= 7:
            buckets["1-7 days"].append(r)
        elif d <= 30:
            buckets["8-30 days"].append(r)
        else:
            buckets["30+ days"].append(r)

    return {
        "total_overdue": len(data),
        "buckets": {k: {"count": len(v), "loans": v} for k, v in buckets.items()},
    }