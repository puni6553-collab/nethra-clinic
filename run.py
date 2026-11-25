"""NetraCare backend — start the API from the project root.

    python run.py           # start (no reload — works under Application Control)
    python run.py --reload  # auto-restart on code changes, where permitted

The FastAPI app lives in backend\\app\\, so a bare `uvicorn app.main:app` only
resolves when your current directory is backend\\. This script puts that folder
on sys.path for you first, so you can start the server from anywhere. With
--reload, uvicorn's reloader inherits that path too, so restarts keep working.

Why --reload is opt-in: the reloader spawns the app in a child process, and
this machine's Application Control policy blocks the venv's extension DLLs
there ("DLL load failed while importing ...: An Application Control policy
has blocked this file"). The same imports load fine in a normal process, so
plain start always works.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"

if not (BACKEND / "app" / "main.py").exists():
    sys.exit(f"Could not find backend\\app\\main.py next to this script.\n"
             f"Expected it at: {BACKEND / 'app' / 'main.py'}")

# Makes `import app...` resolve no matter which folder you launched from.
sys.path.insert(0, str(BACKEND))

import uvicorn  # noqa: E402  (imported after sys.path is set)

if __name__ == "__main__":
    print("\n  NetraCare API")
    print("    http://127.0.0.1:8000        (the API)")
    print("    http://127.0.0.1:8000/docs   (click around by hand)")
    print("\n  Ctrl+C to stop.\n")
    # Watch only the backend code. Without this, --reload scans the whole
    # project root, including venv\ and node_modules\, which is slow and can
    # trigger restarts while npm is writing files.
    reload = "--reload" in sys.argv[1:]
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=reload,
        reload_dirs=[str(BACKEND / "app")] if reload else None,
    )
