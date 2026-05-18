"""
Recommendations module — personalized book suggestions.

Strategies (applied in order, results merged by score):
  1. Collaborative filtering  — users with similar loan history liked X
  2. Content-based filtering  — books similar to what you borrowed (FAISS)
  3. Category affinity        — your most borrowed categories
  4. Trending                 — most borrowed books in the last 30 days

Endpoints:
  GET /recommendations/me              — personalized for current user
  GET /recommendations/similar/{book_id} — books similar to a given book
  GET /recommendations/trending        — trending across all users
  GET /recommendations/by-category/{category_id} — top in a category
"""

import os
import sqlite3
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user

DB_PATH = os.getenv("LIBRARY_DB_PATH", "library_search.db")
FAISS_INDEX_PATH = "library.index"

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _book_detail(conn, book_id: int) -> Optional[dict]:
    row = conn.execute("""
        SELECT b.*, c.name AS category_name
        FROM books b
        LEFT JOIN categories c ON b.category_id = c.id
        WHERE b.id = ?
    """, (book_id,)).fetchone()
    return dict(row) if row else None


def _user_borrowed_ids(conn, user_id: int) -> set:
    """All book IDs ever borrowed by this user."""
    rows = conn.execute(
        "SELECT DISTINCT book_id FROM loans WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["book_id"] for r in rows}


def _user_category_affinity(conn, user_id: int) -> dict:
    """
    Returns {category_id: borrow_count} for the user,
    sorted descending. Used to weight recommendations.
    """
    rows = conn.execute("""
        SELECT b.category_id, COUNT(*) AS cnt
        FROM loans l
        JOIN books b ON b.id = l.book_id
        WHERE l.user_id = ? AND b.category_id IS NOT NULL
        GROUP BY b.category_id
        ORDER BY cnt DESC
    """, (user_id,)).fetchall()
    return {r["category_id"]: r["cnt"] for r in rows}


# ---------------------------------------------------------------------------
# Strategy 1 — Collaborative filtering (user-based)
# ---------------------------------------------------------------------------

def _collaborative(conn, user_id: int, exclude_ids: set, limit: int) -> list[tuple[int, float]]:
    """
    Find users who borrowed at least 2 books in common with current user,
    then recommend books they borrowed that the current user hasn't.
    Returns [(book_id, score)] where score = overlap count.
    """
    my_ids = _user_borrowed_ids(conn, user_id)
    if not my_ids:
        return []

    # Find similar users
    placeholders = ",".join("?" * len(my_ids))
    rows = conn.execute(f"""
        SELECT user_id, COUNT(*) AS overlap
        FROM loans
        WHERE book_id IN ({placeholders})
          AND user_id != ?
        GROUP BY user_id
        HAVING overlap >= 2
        ORDER BY overlap DESC
        LIMIT 20
    """, list(my_ids) + [user_id]).fetchall()

    similar_users = [r["user_id"] for r in rows]
    if not similar_users:
        return []

    # Books borrowed by similar users that current user hasn't read
    ph2 = ",".join("?" * len(similar_users))
    ex  = ",".join("?" * len(exclude_ids)) if exclude_ids else "0"
    candidate_rows = conn.execute(f"""
        SELECT book_id, COUNT(*) AS score
        FROM loans
        WHERE user_id IN ({ph2})
          AND book_id NOT IN ({ex})
        GROUP BY book_id
        ORDER BY score DESC
        LIMIT ?
    """, similar_users + list(exclude_ids) + [limit]).fetchall()

    return [(r["book_id"], float(r["score"])) for r in candidate_rows]


# ---------------------------------------------------------------------------
# Strategy 2 — Content-based (FAISS vector similarity)
# ---------------------------------------------------------------------------

def _content_based(conn, user_id: int, exclude_ids: set, limit: int) -> list[tuple[int, float]]:
    """
    Build an average embedding from the user's borrowed books,
    then find nearest neighbours in the FAISS index.
    """
    try:
        import faiss as _faiss
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return []

    if not os.path.exists(FAISS_INDEX_PATH):
        return []

    # Get descriptions of borrowed books
    my_ids = _user_borrowed_ids(conn, user_id)
    if not my_ids:
        return []

    ph = ",".join("?" * len(my_ids))
    rows = conn.execute(
        f"SELECT id, title, description FROM books WHERE id IN ({ph})",
        list(my_ids)
    ).fetchall()
    if not rows:
        return []

    texts = []
    for r in rows:
        parts = [r["title"]]
        if r["description"]:
            parts.append(r["description"])
        texts.append(" ".join(parts))

    model = SentenceTransformer("all-MiniLM-L6-v2")
    embeddings = model.encode(texts, convert_to_tensor=False)
    avg_vec = np.mean(embeddings, axis=0).astype("float32").reshape(1, -1)

    index = _faiss.read_index(FAISS_INDEX_PATH)
    k = min(limit + len(exclude_ids) + 10, index.ntotal)
    distances, indices = index.search(avg_vec, k)

    # FIX: build the map keyed by FAISS position (0-based over non-empty-embedding
    # rows only), matching exactly how build_faiss_index orders vectors.
    doc_rows = conn.execute(
        "SELECT rowid, doc FROM documents WHERE embedding != '[]' ORDER BY rowid"
    ).fetchall()
    pos_to_doc = {i: r["doc"] for i, r in enumerate(doc_rows)}

    results = []
    for pos, dist in zip(indices[0], distances[0]):
        if pos < 0 or pos >= len(pos_to_doc):
            continue
        doc_text = pos_to_doc[pos]
        # Match document text to book title
        book_row = conn.execute(
            "SELECT id FROM books WHERE ? LIKE '%' || title || '%'", (doc_text,)
        ).fetchone()
        if not book_row:
            continue
        bid = book_row["id"]
        if bid in exclude_ids or bid in my_ids:
            continue
        score = 1.0 / (1.0 + float(dist))
        results.append((bid, score))
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# Strategy 3 — Category affinity
# ---------------------------------------------------------------------------

def _category_affinity(conn, user_id: int, exclude_ids: set, limit: int) -> list[tuple[int, float]]:
    """
    Recommend popular books in the user's favourite categories.
    Score = category_affinity_count * borrow_popularity.
    """
    affinity = _user_category_affinity(conn, user_id)
    if not affinity:
        return []

    my_ids = _user_borrowed_ids(conn, user_id)
    all_exclude = exclude_ids | my_ids
    ex = ",".join("?" * len(all_exclude)) if all_exclude else "0"

    results = []
    for cat_id, aff_score in list(affinity.items())[:3]:  # top 3 categories
        rows = conn.execute(f"""
            SELECT b.id,
                   (SELECT COUNT(*) FROM loans l2 WHERE l2.book_id = b.id) AS popularity
            FROM books b
            WHERE b.category_id = ?
              AND b.available_copies > 0
              AND b.id NOT IN ({ex})
            ORDER BY popularity DESC
            LIMIT ?
        """, [cat_id] + list(all_exclude) + [limit]).fetchall()

        for r in rows:
            score = aff_score * (1 + r["popularity"])
            results.append((r["id"], float(score)))

    return results


# ---------------------------------------------------------------------------
# Strategy 4 — Trending (last 30 days)
# ---------------------------------------------------------------------------

def _trending(conn, exclude_ids: set, limit: int) -> list[tuple[int, float]]:
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    ex = ",".join("?" * len(exclude_ids)) if exclude_ids else "0"

    rows = conn.execute(f"""
        SELECT book_id, COUNT(*) AS borrows
        FROM loans
        WHERE loan_date >= ?
          AND book_id NOT IN ({ex})
        GROUP BY book_id
        ORDER BY borrows DESC
        LIMIT ?
    """, [since] + list(exclude_ids) + [limit]).fetchall()

    return [(r["book_id"], float(r["borrows"])) for r in rows]


# ---------------------------------------------------------------------------
# Score merger
# ---------------------------------------------------------------------------

def _merge_scores(*score_lists, weights: list[float]) -> list[int]:
    """
    Merge multiple [(book_id, score)] lists with weights.
    Normalise each list to [0,1] then combine.
    Returns book IDs sorted by combined score, deduplicated.
    """
    combined: dict[int, float] = defaultdict(float)

    for scores, w in zip(score_lists, weights):
        if not scores:
            continue
        max_s = max(s for _, s in scores) or 1.0
        for bid, s in scores:
            combined[bid] += w * (s / max_s)

    return [bid for bid, _ in sorted(combined.items(), key=lambda x: -x[1])]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/recommendations", tags=["Recommendations"])


@router.get("/me")
async def recommend_for_me(
    limit: int = Query(10, ge=1, le=50),
    user=Depends(get_current_user),
):
    """
    Personalised recommendations for the current user.
    Combines collaborative, content-based and category-affinity strategies.
    """
    conn = get_db()
    my_ids = _user_borrowed_ids(conn, user["id"])

    # Check if user has any history
    has_history = len(my_ids) > 0

    collab   = _collaborative(conn, user["id"], my_ids, limit)
    content  = _content_based(conn, user["id"], my_ids, limit) if has_history else []
    category = _category_affinity(conn, user["id"], my_ids, limit)
    trending = _trending(conn, my_ids, limit)

    # Weights: collaborative > content > category > trending
    merged_ids = _merge_scores(
        collab, content, category, trending,
        weights=[0.4, 0.3, 0.2, 0.1]
    )[:limit]

    books = [b for bid in merged_ids if (b := _book_detail(conn, bid))]
    conn.close()

    strategy_used = []
    if collab:   strategy_used.append("collaborative")
    if content:  strategy_used.append("content-based")
    if category: strategy_used.append("category-affinity")
    if trending: strategy_used.append("trending")

    if not books:
        # Cold start — return trending for new users
        conn = get_db()
        trend_ids = _trending(conn, set(), limit)
        books = [b for bid, _ in trend_ids if (b := _book_detail(conn, bid))]
        conn.close()
        strategy_used = ["trending (cold start)"]

    return {
        "user": user["username"],
        "strategies_used": strategy_used,
        "has_history": has_history,
        "count": len(books),
        "recommendations": books,
    }


@router.get("/similar/{book_id}")
async def similar_books(
    book_id: int,
    limit: int = Query(8, ge=1, le=30),
    user=Depends(get_current_user),
):
    """Books similar to a given book (content-based via FAISS + category)."""
    conn = get_db()
    target = _book_detail(conn, book_id)
    if not target:
        conn.close()
        raise HTTPException(status_code=404, detail="Book not found")

    results = []
    seen = {book_id}

    # FAISS similarity on this book's text
    if os.path.exists(FAISS_INDEX_PATH):
        try:
            import faiss as _faiss
            from sentence_transformers import SentenceTransformer

            text = target["title"]
            if target.get("description"):
                text += " " + target["description"]

            model = SentenceTransformer("all-MiniLM-L6-v2")
            vec   = model.encode([text], convert_to_tensor=False).astype("float32")
            index = _faiss.read_index(FAISS_INDEX_PATH)
            k     = min(limit + 5, index.ntotal)
            distances, indices = index.search(vec, k)

            doc_rows = conn.execute(
                "SELECT rowid, doc FROM documents WHERE embedding != '[]' ORDER BY rowid"
            ).fetchall()
            pos_to_doc = {i: r["doc"] for i, r in enumerate(doc_rows)}

            for pos, dist in zip(indices[0], distances[0]):
                if pos < 0:
                    continue
                doc_text = pos_to_doc.get(pos, "")
                row = conn.execute(
                    "SELECT id FROM books WHERE ? LIKE '%' || title || '%'", (doc_text,)
                ).fetchone()
                if row and row["id"] not in seen:
                    seen.add(row["id"])
                    b = _book_detail(conn, row["id"])
                    if b:
                        b["similarity_score"] = round(1.0 / (1.0 + float(dist)), 3)
                        results.append(b)
                if len(results) >= limit:
                    break
        except Exception as e:
            print(f"FAISS similarity error: {e}")

    # Fill remaining slots with books from the same category
    if len(results) < limit and target.get("category_id"):
        fill = limit - len(results)
        ex   = ",".join("?" * len(seen))
        rows = conn.execute(f"""
            SELECT b.id FROM books b
            WHERE b.category_id = ? AND b.id NOT IN ({ex})
            ORDER BY (SELECT COUNT(*) FROM loans l WHERE l.book_id = b.id) DESC
            LIMIT ?
        """, [target["category_id"]] + list(seen) + [fill]).fetchall()
        for r in rows:
            b = _book_detail(conn, r["id"])
            if b:
                b["similarity_score"] = None
                results.append(b)

    conn.close()
    return {
        "reference_book": target,
        "count": len(results),
        "similar_books": results,
    }


@router.get("/trending")
async def trending_books(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
    user=Depends(get_current_user),
):
    """Most borrowed books over the last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn  = get_db()

    rows = conn.execute("""
        SELECT b.id, COUNT(l.id) AS borrow_count
        FROM loans l
        JOIN books b ON b.id = l.book_id
        WHERE l.loan_date >= ?
        GROUP BY b.id
        ORDER BY borrow_count DESC
        LIMIT ?
    """, (since, limit)).fetchall()

    books = []
    for r in rows:
        b = _book_detail(conn, r["id"])
        if b:
            b["borrow_count"] = r["borrow_count"]
            books.append(b)

    # If no loans yet, return newest books
    if not books:
        rows = conn.execute("""
            SELECT * FROM books ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        books = [dict(r) for r in rows]

    conn.close()
    return {
        "period_days": days,
        "count": len(books),
        "trending": books,
    }


@router.get("/by-category/{category_id}")
async def recommend_by_category(
    category_id: int,
    limit: int = Query(10, ge=1, le=50),
    user=Depends(get_current_user),
):
    """Top books in a specific category, ranked by popularity."""
    conn = get_db()
    cat = conn.execute(
        "SELECT * FROM categories WHERE id = ?", (category_id,)
    ).fetchone()
    if not cat:
        conn.close()
        raise HTTPException(status_code=404, detail="Category not found")

    rows = conn.execute("""
        SELECT b.id,
               COUNT(l.id) AS borrow_count
        FROM books b
        LEFT JOIN loans l ON l.book_id = b.id
        WHERE b.category_id = ?
        GROUP BY b.id
        ORDER BY borrow_count DESC, b.created_at DESC
        LIMIT ?
    """, (category_id, limit)).fetchall()

    books = []
    for r in rows:
        b = _book_detail(conn, r["id"])
        if b:
            b["borrow_count"] = r["borrow_count"]
            books.append(b)

    conn.close()
    return {
        "category": dict(cat),
        "count": len(books),
        "books": books,
    }