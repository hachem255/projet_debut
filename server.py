"""
server.py — Production launchers for Smart Library API.

Usage:
  Windows:  python server.py
  Linux:    python server.py --gunicorn
  Docker:   python server.py --gunicorn --workers 4
"""

import argparse
import os
import sys
import multiprocessing

HOST    = os.getenv("HOST", "0.0.0.0")
PORT    = int(os.getenv("PORT", "8000"))
WORKERS = int(os.getenv("WORKERS", (multiprocessing.cpu_count() * 2) + 1))


def run_waitress():
    """Windows-compatible server using asgiref WSGI bridge."""
    try:
        from waitress import serve
        from asgiref.wsgi import WsgiToAsgi  # WRONG direction
    except ImportError:
        pass

    # Waitress is WSGI. FastAPI is ASGI. Use a2wsgi to bridge.
    try:
        from a2wsgi import ASGIMiddleware
        from waitress import serve
        from main import app
        wsgi_app = ASGIMiddleware(app)
        print(f"[Waitress+a2wsgi] Listening on http://{HOST}:{PORT}")
        serve(wsgi_app, host=HOST, port=PORT, threads=WORKERS)
    except ImportError:
        # Fallback: use uvicorn if a2wsgi not installed
        import uvicorn
        print("[Uvicorn] a2wsgi not found, using Uvicorn instead")
        uvicorn.run("main:app", host=HOST, port=PORT)


def run_gunicorn(workers: int = WORKERS):
    """Linux/Mac production server."""
    import subprocess
    cmd = [
        "gunicorn", "main:app",
        "--worker-class", "uvicorn.workers.UvicornWorker",
        "--workers",      str(workers),
        "--bind",         f"{HOST}:{PORT}",
        "--timeout",      "120",
        "--keepalive",    "5",
        "--max-requests", "1000",
        "--max-requests-jitter", "100",
        "--access-logfile", "logs/access.log",
        "--error-logfile",  "logs/error.log",
        "--log-level",      os.getenv("LOG_LEVEL", "info"),
    ]
    import pathlib
    pathlib.Path("logs").mkdir(exist_ok=True)
    print(f"[Gunicorn] {workers} workers on {HOST}:{PORT}")
    subprocess.run(cmd)


def run_uvicorn_dev():
    """Development server with hot-reload."""
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True, log_level="info")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smart Library API server")
    parser.add_argument("--gunicorn",  action="store_true", help="Use Gunicorn (Linux/Mac)")
    parser.add_argument("--dev",       action="store_true", help="Uvicorn dev mode with reload")
    parser.add_argument("--workers",   type=int, default=WORKERS)
    args = parser.parse_args()

    if args.dev:
        run_uvicorn_dev()
    elif args.gunicorn:
        run_gunicorn(args.workers)
    else:
        # Default: Waitress (works on Windows AND Linux)
        run_waitress()
