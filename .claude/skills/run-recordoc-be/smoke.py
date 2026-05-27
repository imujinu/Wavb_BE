"""
Smoke-tests the Recordoc FastAPI server.

Usage:
    cd server/
    uv run python ../.claude/skills/run-recordoc-be/smoke.py [port]

Launches the server, runs checks, shuts it down.
Exits 0 on success, 1 on any failure.
"""

import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASE = f"http://127.0.0.1:{PORT}"
SERVER_DIR = Path(__file__).parent.parent.parent.parent / "server"


def wait_for_server(timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"{BASE}/health", timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("Server did not start in time")


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f": {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def main() -> None:
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=SERVER_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        print(f"Starting server on port {PORT} ...")
        wait_for_server()
        print("Server ready.\n")

        # 1. /health
        print("==> GET /health")
        r = httpx.get(f"{BASE}/health")
        check("/health status", r.status_code == 200, str(r.status_code))
        check("/health body", r.json().get("status") == "ok", r.text)

        # 2. OpenAPI spec
        print("==> GET /openapi.json")
        r = httpx.get(f"{BASE}/openapi.json")
        check("openapi.json reachable", r.status_code == 200)
        check("openapi.json title", "Recordoc Backend" in r.text)

        # 3. /audio/summarize — bad extension → 400
        print("==> POST /audio/summarize (bad extension)")
        dummy = b"not audio"
        r = httpx.post(
            f"{BASE}/audio/summarize",
            files={"file": ("test.txt", dummy, "text/plain")},
        )
        check("/audio/summarize rejects .txt", r.status_code == 400, str(r.status_code))

        # 4. /audio/transcripts — bad extension → 400
        print("==> POST /audio/transcripts (bad extension)")
        r = httpx.post(
            f"{BASE}/audio/transcripts",
            files={"file": ("test.txt", dummy, "text/plain")},
            data={"domain_type": "meeting"},
        )
        check("/audio/transcripts rejects .txt", r.status_code == 400, str(r.status_code))

        print("\nAll smoke checks passed.")

    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
