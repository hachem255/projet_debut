"""
Authentication module — JWT + 4 roles + email-verification signup.

Roles (in order of privilege):
  1. student      — search, view catalog, manage own loans
  2. teacher      — student rights + request purchases
  3. librarian    — teacher rights + manage catalog & loans
  4. admin        — full access (users, config, stats)

New endpoints:
  POST /auth/request-verification-code  — send 6-digit OTP to email (public)
  POST /auth/verify-signup              — confirm OTP and create account (public)
  GET  /auth/debug-email                — diagnose Gmail config (admin only)

Requires:  GMAIL_EMAIL + GMAIL_APP_PASSWORD  env vars (or .env file).
"""

import json
import os
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration  (read at module load — set before importing this module)
# ---------------------------------------------------------------------------
SECRET_KEY                  = os.getenv("JWT_SECRET_KEY", "change-this-in-production-use-openssl-rand-hex-32")
ALGORITHM                   = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS   = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
VERIFICATION_TTL_MINUTES    = int(os.getenv("VERIFICATION_TTL_MINUTES", "10"))

# NOTE: GMAIL_EMAIL and GMAIL_APP_PASSWORD are intentionally read at *call time*
# inside _send_verification_email() so that .env changes don't require a restart
# and so the module can be imported before the env is fully loaded.

PUBLIC_SIGNUP_ROLES = {"student", "teacher"}
ROLE_HIERARCHY      = {"student": 1, "teacher": 2, "librarian": 3, "admin": 4}

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# .env loader (fallback — main.py loads it first, but guard here too)
# ---------------------------------------------------------------------------
def _load_env_if_needed() -> None:
    """
    If GMAIL_EMAIL is still empty, try reading .env from the project root.
    Safe to call multiple times.
    """
    if os.getenv("GMAIL_EMAIL"):
        return  # already set
    env_candidates = [
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in env_candidates:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        break  # stop after first found


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("LIBRARY_DB_PATH", "library_search.db")


def get_users_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            username        TEXT UNIQUE NOT NULL,
            email           TEXT UNIQUE NOT NULL,
            full_name       TEXT,
            hashed_password TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'student',
            is_active       INTEGER NOT NULL DEFAULT 1,
            email_verified  INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Migration: add email_verified to older databases
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "email_verified" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1"
        )
        print("INFO: users.email_verified column added (migration).")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_codes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT NOT NULL,
            code         TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            expires_at   TEXT NOT NULL,
            used_at      TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def _seed_admin_if_empty(conn):
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        conn.execute("""
            INSERT INTO users
                (username, email, full_name, hashed_password, role, email_verified)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (
            "admin", "admin@library.local", "System Administrator",
            hash_password("admin1234"), "admin",
        ))
        conn.commit()
        print("INFO: Default admin created — username: admin / password: admin1234")
        print("INFO: Change this password immediately in production!")


_conn_seed = get_users_db()
_seed_admin_if_empty(_conn_seed)
_conn_seed.close()


# ---------------------------------------------------------------------------
# Email helper — reads credentials at call time
# ---------------------------------------------------------------------------

def _send_verification_email(target_email: str, code: str) -> None:
    """Send a 6-digit OTP via Gmail SMTP SSL with fallback options.

    Credentials are read from env at *call time* so that a freshly loaded
    .env file is always used even if the module was imported first.
    """
    _load_env_if_needed()

    gmail_user = os.getenv("GMAIL_EMAIL", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

    if not gmail_user or not gmail_pass:
        raise HTTPException(
            status_code=503,
            detail=(
                "Email service not configured. "
                "Add GMAIL_EMAIL and GMAIL_APP_PASSWORD to your .env file."
            ),
        )

    msg = EmailMessage()
    msg["Subject"] = "Smart Library — رمز التحقق"
    msg["From"]    = gmail_user
    msg["To"]      = target_email
    msg.set_content(
        f"رمز التحقق الخاص بك هو:\n\n  {code}\n\n"
        f"صالح لمدة {VERIFICATION_TTL_MINUTES} دقائق.\n"
        "إذا لم تطلب هذا الرمز يمكنك تجاهل هذه الرسالة."
    )

    # Try primary method: SMTP_SSL on port 465
    smtp_configs = [
        ("smtp.gmail.com", 465, True, "SMTP_SSL (port 465)"),
        ("smtp.gmail.com", 587, False, "SMTP_STARTTLS (port 587)"),
    ]

    last_error = None
    for smtp_host, smtp_port, use_ssl, method_name in smtp_configs:
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as smtp:
                    smtp.login(gmail_user, gmail_pass)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
                    smtp.starttls()
                    smtp.login(gmail_user, gmail_pass)
                    smtp.send_message(msg)
            return  # Success!
        except smtplib.SMTPAuthenticationError as exc:
            last_error = (
                f"Gmail authentication failed ({method_name}). "
                "Make sure you are using a Gmail App Password "
                "(not your regular password). "
                "Generate one at: https://myaccount.google.com/apppasswords"
            )
            continue
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            last_error = str(exc)
            continue

    # If we get here, both methods failed
    raise HTTPException(
        status_code=503,
        detail=(
            f"Failed to send verification email: {last_error}. "
            "Check your Gmail credentials and ensure:\n"
            "1. GMAIL_EMAIL is a valid Gmail address\n"
            "2. GMAIL_APP_PASSWORD is a 16-char App Password (no spaces)\n"
            "3. Your network allows outbound SMTP (ports 465 or 587)\n"
            "Generate app password at: https://myaccount.google.com/apppasswords"
        ),
    )


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    username:  str
    email:     str
    full_name: Optional[str] = None
    password:  str
    role:      str = "student"


class UserOut(BaseModel):
    id:         int
    username:   str
    email:      str
    full_name:  Optional[str]
    role:       str
    is_active:  bool
    created_at: str


class TokenResponse(BaseModel):
    access_token:  str
    refresh_token: str
    token_type:    str = "bearer"
    role:          str
    username:      str


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password:     str


class UserUpdate(BaseModel):
    email:     Optional[str] = None
    full_name: Optional[str] = None
    username:  Optional[str] = None
    password:  Optional[str] = None
    is_active: Optional[bool] = None


class RequestVerificationCodeBody(BaseModel):
    username:  str
    email:     str
    password:  str
    full_name: Optional[str] = None
    role:      str = "student"


class VerifySignupBody(BaseModel):
    email: str
    code:  str


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def _create_token(data: dict, expires_delta: timedelta) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + expires_delta
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(username: str, role: str) -> str:
    return _create_token(
        {"sub": username, "role": role, "type": "access"},
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(username: str, role: str) -> str:
    return _create_token(
        {"sub": username, "role": role, "type": "refresh"},
        timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Expected access token")
    username = payload.get("sub")
    role     = payload.get("role")
    if not username or not role:
        raise HTTPException(status_code=401, detail="Malformed token")

    conn = get_users_db()
    row  = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
    ).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return dict(row)


def require_role(minimum_role: str):
    def _check(user: dict = Depends(get_current_user)):
        if ROLE_HIERARCHY.get(user["role"], 0) < ROLE_HIERARCHY.get(minimum_role, 99):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role '{minimum_role}' or higher. Your role: '{user['role']}'",
            )
        return user
    return _check


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/debug-email")
async def debug_email_config(admin=Depends(require_role("admin"))):
    """
    Shows what Gmail credentials the server currently sees.
    Use this to confirm your .env was loaded correctly.
    Admin only — never exposes the full password.
    """
    _load_env_if_needed()
    gmail_user = os.getenv("GMAIL_EMAIL", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

    masked_pass = ""
    if gmail_pass:
        masked_pass = gmail_pass[:4] + "*" * (len(gmail_pass) - 4)

    # Try to test the connection
    connection_status = "Unknown"
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=5) as smtp:
            connection_status = "Port 465 reachable ✓"
    except Exception as e:
        connection_status = f"Port 465 failed: {type(e).__name__}: {str(e)[:50]}"

    return {
        "GMAIL_EMAIL":        gmail_user or "(not set)",
        "GMAIL_APP_PASSWORD": masked_pass or "(not set)",
        "password_length":    len(gmail_pass),
        "smtp_connection":    connection_status,
        "tip": (
            "App Password must be exactly 16 characters (no spaces). "
            "Generate at: https://myaccount.google.com/apppasswords"
        ),
    }


@router.post("/test-email")
async def test_email_send(admin=Depends(require_role("admin"))):
    """
    Attempt to send a test email to verify Gmail configuration.
    Admin only.
    """
    _load_env_if_needed()
    gmail_user = os.getenv("GMAIL_EMAIL", "").strip()
    
    try:
        _send_verification_email(gmail_user, "123456")
        return {"status": "success", "message": f"Test email sent to {gmail_user}"}
    except HTTPException as e:
        return {"status": "error", "message": e.detail}


# ── Public self-registration — step 1: request OTP ──────────────────────────

@router.post("/request-verification-code", status_code=200)
async def request_verification_code(body: RequestVerificationCodeBody):
    """
    Public — no authentication required.
    Sends a 7-digit OTP to the provided email address.
    Only 'student' and 'teacher' roles are allowed for self-registration.
    """
    role = (body.role or "student").strip().lower()
    if role not in PUBLIC_SIGNUP_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Public signup only allows: {sorted(PUBLIC_SIGNUP_ROLES)}",
        )
    if not body.username.strip() or not body.email.strip():
        raise HTTPException(status_code=400, detail="Username and email are required")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    email = body.email.strip().lower()
    conn  = get_users_db()
    try:
        if conn.execute(
            "SELECT 1 FROM users WHERE username = ? OR email = ?",
            (body.username.strip(), email),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Username or email already exists")

        code = f"{secrets.randbelow(10_000_000):07d}"
        payload_data = {
            "username":  body.username.strip(),
            "email":     email,
            "password":  body.password,
            "full_name": (body.full_name or "").strip() or None,
            "role":      role,
        }
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=VERIFICATION_TTL_MINUTES)
        ).isoformat()

        conn.execute("DELETE FROM verification_codes WHERE email = ?", (email,))
        conn.execute(
            "INSERT INTO verification_codes (email, code, payload_json, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (email, code, json.dumps(payload_data), expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    _send_verification_email(email, code)  # raises HTTPException on failure
    return {"message": "Verification code sent to your email"}


# ── Public self-registration — step 2: verify OTP ───────────────────────────

@router.post("/verify-signup", status_code=201)
async def verify_signup(body: VerifySignupBody):
    """Confirm the OTP and create the user account."""
    email = body.email.strip().lower()
    code  = "".join(body.code.split())  # remove all whitespace/newlines
    if not email or not code:
        raise HTTPException(status_code=400, detail="Email and code are required")
    if not code.isdigit() or len(code) != 7:
        raise HTTPException(status_code=400, detail="Verification code must be exactly 7 digits")

    conn = get_users_db()
    try:
        row = conn.execute(
            "SELECT * FROM verification_codes "
            "WHERE email = ? AND code = ? AND used_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (email, code),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid verification code")

        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=500, detail="Malformed expiry in code record")

        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Verification code has expired")

        payload_data = json.loads(row["payload_json"])
        if conn.execute(
            "SELECT 1 FROM users WHERE username = ? OR email = ?",
            (payload_data["username"], email),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Username or email already exists")

        conn.execute(
            "INSERT INTO users "
            "(username, email, full_name, hashed_password, role, is_active, email_verified) "
            "VALUES (?, ?, ?, ?, ?, 1, 1)",
            (
                payload_data["username"],
                email,
                payload_data.get("full_name"),
                hash_password(payload_data["password"]),
                payload_data.get("role", "student"),
            ),
        )
        conn.execute(
            "UPDATE verification_codes SET used_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    return {"message": "Account created successfully. You can now log in."}


# ── Standard login ───────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    conn = get_users_db()
    row  = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (form.username,)
    ).fetchone()
    conn.close()

    if row is None or not verify_password(form.password, row["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(
        access_token=create_access_token(row["username"], row["role"]),
        refresh_token=create_refresh_token(row["username"], row["role"]),
        role=row["role"],
        username=row["username"],
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Expected refresh token")

    conn = get_users_db()
    row  = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1", (payload["sub"],)
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=401, detail="User not found")

    return TokenResponse(
        access_token=create_access_token(payload["sub"], payload["role"]),
        refresh_token=create_refresh_token(payload["sub"], payload["role"]),
        role=payload["role"],
        username=payload["sub"],
    )


@router.get("/me", response_model=UserOut)
async def get_me(user: dict = Depends(get_current_user)):
    return user


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: dict = Depends(get_current_user),
):
    if not verify_password(body.current_password, user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    conn = get_users_db()
    conn.execute(
        "UPDATE users SET hashed_password = ? WHERE username = ?",
        (hash_password(body.new_password), user["username"]),
    )
    conn.commit()
    conn.close()
    return {"message": "Password changed successfully"}


@router.get("/users", response_model=list[UserOut])
async def list_users(admin=Depends(require_role("admin"))):
    conn = get_users_db()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/register", response_model=UserOut, status_code=201)
async def register(data: UserCreate, admin=Depends(require_role("admin"))):
    if data.role not in ROLE_HIERARCHY:
        raise HTTPException(status_code=400, detail=f"Invalid role. Choose from: {list(ROLE_HIERARCHY)}")
    conn = get_users_db()
    try:
        conn.execute(
            "INSERT INTO users (username, email, full_name, hashed_password, role, email_verified) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (data.username, data.email, data.full_name, hash_password(data.password), data.role),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (data.username,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Username or email already exists")
    finally:
        conn.close()


@router.put("/users/{username}", response_model=UserOut)
async def update_user(
    username: str,
    data: UserUpdate,
    admin=Depends(require_role("admin")),
):
    """Update any user's profile fields (admin only)."""
    conn = get_users_db()
    row  = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")

    fields: dict = {}

    if data.email is not None:
        data.email = data.email.strip().lower()
        if not data.email:
            conn.close()
            raise HTTPException(status_code=400, detail="Email cannot be empty")
        fields["email"] = data.email

    if data.full_name is not None:
        fields["full_name"] = data.full_name.strip() or None

    if data.username is not None:
        new_uname = data.username.strip()
        if not new_uname:
            conn.close()
            raise HTTPException(status_code=400, detail="Username cannot be empty")
        fields["username"] = new_uname

    if data.password is not None:
        if len(data.password) < 6:
            conn.close()
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        fields["hashed_password"] = hash_password(data.password)

    if data.is_active is not None:
        fields["is_active"] = int(data.is_active)

    if not fields:
        conn.close()
        raise HTTPException(status_code=400, detail="Nothing to update")

    set_clause = ", ".join(f"{k} = ?" for k in fields)
    try:
        result = conn.execute(
            f"UPDATE users SET {set_clause} WHERE username = ?",
            list(fields.values()) + [username],
        )
        conn.commit()
        if result.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="User not found")
        # Fetch using new username if it changed
        lookup = fields.get("username", username)
        updated = conn.execute("SELECT * FROM users WHERE username = ?", (lookup,)).fetchone()
        conn.close()
        return dict(updated)
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="Username or email already taken")


@router.patch("/users/{username}/activate")
async def activate_user(username: str, admin=Depends(require_role("admin"))):
    """Re-activate a previously deactivated user."""
    conn   = get_users_db()
    result = conn.execute("UPDATE users SET is_active = 1 WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": f"{username} activated"}


@router.patch("/users/{username}/role")
async def update_role(
    username: str,
    new_role: str = Query(...),
    admin=Depends(require_role("admin")),
):
    if new_role not in ROLE_HIERARCHY:
        raise HTTPException(status_code=400, detail=f"Invalid role: {new_role}")
    conn = get_users_db()
    result = conn.execute("UPDATE users SET role = ? WHERE username = ?", (new_role, username))
    conn.commit()
    if result.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    conn.close()
    return {"message": f"{username} is now '{new_role}'"}


@router.patch("/users/{username}/deactivate")
async def deactivate_user(username: str, admin=Depends(require_role("admin"))):
    if username == admin["username"]:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    conn = get_users_db()
    result = conn.execute("UPDATE users SET is_active = 0 WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": f"{username} deactivated"}