"""Desktop entry point -- the dashboard as an application window.

Same server as `run.py --serve`, minus the terminal. It picks a free port,
starts aiohttp on a background thread, and opens a native window pointed at it.
Closing the window stops everything.

Two things this has to get right that the CLI does not:

* **Where the data lives.** The ranks cache is ~2 GB and the database grows
  past 1 GB, so neither can live inside the bundle -- a frozen exe unpacks to a
  temp directory that is wiped between runs, which would mean re-downloading
  2 GB every launch and losing every domain found. The path is resolved at
  startup and written to config.json next to the executable.

* **No signal handlers.** `web.run_app()` installs them, and that throws
  outright off the main thread. The runner is driven manually instead.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

APP_NAME = "ShopScan"
CONFIG = "config.json"
# The installer waits on this. Without it, upgrading while the app is open
# fails on a locked executable and leaves a half-applied install.
MUTEX = "ShopScanRunning"


def claim_mutex() -> None:
    """Publish a named mutex so the installer can see we are running.

    Deliberately not used to enforce a single instance -- the app picks a free
    port, so two copies coexist happily. This only exists so Inno Setup's
    AppMutex check can ask the user to close the app before an upgrade.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX)
    except Exception:
        pass                          # never block startup over this


def base_dir() -> Path:
    """Folder the app was launched from -- the exe's folder when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_data_dir() -> Path:
    """Find the data directory, preferring one that already has the cache.

    Order matters: an existing 2 GB ranks cache and a populated database are
    worth far more than a tidy default location, so anything already holding
    them wins over creating somewhere new.
    """
    env = os.environ.get("SHOPSCAN_DATA")
    if env:
        return Path(env)

    cfg = base_dir() / CONFIG
    if cfg.exists():
        try:
            saved = json.loads(cfg.read_text()).get("data_dir")
            if saved and Path(saved).exists():
                return Path(saved)
        except (OSError, ValueError):
            pass                       # unreadable config is not fatal

    here = base_dir()
    for cand in (here / "data",
                 here.parent / "data",
                 here.parent / "shopify-finder" / "data"):
        if cand.is_dir():
            return cand

    fallback = Path(os.environ.get("LOCALAPPDATA", here)) / "ShopScan"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def save_config(data_dir: Path) -> None:
    try:
        (base_dir() / CONFIG).write_text(
            json.dumps({"data_dir": str(data_dir)}, indent=2))
    except OSError:
        pass                           # read-only install directory is fine


def free_port() -> int:
    """Let the OS pick, so two copies never fight over 8787."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int, db: str, out: str, ready: threading.Event) -> None:
    from aiohttp import web
    from finder.server import build_app

    async def go():
        runner = web.AppRunner(build_app(db, out), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        ready.set()
        await asyncio.Event().wait()      # serve until the process exits

    asyncio.new_event_loop().run_until_complete(go())


def wait_for(port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def main() -> int:
    claim_mutex()
    data_dir = resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    save_config(data_dir)

    out_dir = base_dir() / "output"
    out_dir.mkdir(exist_ok=True)
    db = str(data_dir / "finder.db")

    port = free_port()
    ready = threading.Event()
    threading.Thread(target=serve, args=(port, db, str(out_dir), ready),
                     daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    if not wait_for(port):
        print(f"server did not start on {port}", file=sys.stderr)
        return 1

    try:
        import webview
        from finder import __version__
        webview.create_window(f"{APP_NAME} {__version__}", url,
                              width=1500, height=950, min_size=(1100, 700))
        webview.start()                # blocks until the window is closed
    except Exception as e:
        # No WebView2 runtime, or a headless environment. The dashboard is a
        # normal web page, so a browser tab is a complete fallback rather than
        # a degraded one.
        print(f"[window] {type(e).__name__}: {e} -- opening a browser instead")
        import webbrowser
        webbrowser.open(url)
        print(f"{APP_NAME} running at {url}\nClose this window to stop.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
