"""System-tray UI — a thin, optional launcher + live status indicator.

Mirroring at-field's philosophy ("the tray just opens the dashboard URLs and
manages the services; the engine stays headless"), this is deliberately small:
a status icon whose colour/tooltip reflects mesh health, and a menu that opens
the console pages (with the mesh token injected so the browser just works) and
performs local control actions (graceful-stop the Fixer/Runners on this box,
open the logs folder).

Requires the optional ``tray`` extra (``pip install kiroshi[tray]``) which pulls
in ``pystray`` + ``pillow``. Importing this module without them raises
``ImportError``, which the CLI turns into a friendly install hint.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from typing import Optional

import pystray  # noqa: E402  (import error surfaced as a friendly CLI hint)
import requests
from PIL import Image, ImageDraw

from . import security
from .appstate import logs_dir
from .discovery import discover_fixer

_AUTO = {"auto", "discover", "", None}


def _make_icon(color: tuple[int, int, int]) -> Image.Image:
    """Draw a small 'Kiroshi optic' — a glowing ring on a dark tile."""
    size = 64
    img = Image.new("RGBA", (size, size), (7, 9, 13, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), outline=color, width=5)
    d.ellipse((24, 24, 40, 40), fill=color)
    return img


_GREEN = (26, 209, 200)
_GREY = (107, 122, 141)
_MAGENTA = (255, 46, 136)


class Tray:
    def __init__(self, fixer: Optional[str], token: Optional[str]):
        self._fixer_arg = fixer
        self.fixer_url = "" if (fixer or "").strip().lower() in _AUTO else fixer.rstrip("/")
        self.token = token if token is not None else security.resolve_token()
        self._stop = threading.Event()
        self._status = "starting…"
        self._live = False
        self.icon: Optional[pystray.Icon] = None

    # -------------------------------------------------------------- helpers
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _url(self, path: str) -> str:
        base = self.fixer_url or "http://127.0.0.1:8787"
        sep = "&" if "?" in path else "?"
        if self.token:
            return f"{base}{path}{sep}token={self.token}"
        return f"{base}{path}"

    def _open(self, path: str):
        def _cb(icon, item):
            self._ensure_fixer()
            webbrowser.open(self._url(path))
        return _cb

    def _ensure_fixer(self) -> None:
        if not self.fixer_url:
            url = discover_fixer(timeout=4.0)
            if url:
                self.fixer_url = url

    # ----------------------------------------------------------------- menu
    def _menu(self):
        Item = pystray.MenuItem
        return pystray.Menu(
            Item(lambda i: self._status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item("Open dashboard", self._open("/")),
            Item("Jobs", self._open("/ui/jobs")),
            Item("History", self._open("/ui/history")),
            pystray.Menu.SEPARATOR,
            Item("Open logs folder", self._open_logs),
            Item("Copy mesh token", self._copy_token, enabled=bool(self.token)),
            pystray.Menu.SEPARATOR,
            Item("Stop local runners (drain)", self._stop_runners),
            Item("Stop local fixer (drain)", self._stop_fixer),
            pystray.Menu.SEPARATOR,
            Item("Quit tray", self._quit),
        )

    def _open_logs(self, icon, item):
        d = str(logs_dir())
        try:
            if sys.platform == "win32":
                os.startfile(d)  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{d}"')
            else:
                os.system(f'xdg-open "{d}"')
        except OSError:
            pass

    def _copy_token(self, icon, item):
        if not self.token:
            return
        try:  # best-effort clipboard via tkinter (stdlib)
            import tkinter
            r = tkinter.Tk()
            r.withdraw()
            r.clipboard_clear()
            r.clipboard_append(self.token)
            r.update()
            r.destroy()
        except Exception:  # noqa: BLE001
            print(f"[tray] mesh token: {self.token}", flush=True)

    def _stop_runners(self, icon, item):
        from .processreg import list_registered, request_stop
        n = 0
        for p in list_registered():
            if p.get("role") == "runner":
                if request_stop("runner", int(p.get("pid", 0))):
                    n += 1
        self._notify(f"asked {n} runner(s) to drain")

    def _stop_fixer(self, icon, item):
        from .processreg import list_registered, request_stop
        n = 0
        for p in list_registered():
            if p.get("role") == "fixer":
                if request_stop("fixer", int(p.get("pid", 0))):
                    n += 1
        self._notify(f"asked {n} fixer(s) to drain")

    def _notify(self, msg: str):
        try:
            self.icon.notify(msg, "KIROSHI")
        except Exception:  # noqa: BLE001
            print(f"[tray] {msg}", flush=True)

    def _quit(self, icon, item):
        self._stop.set()
        self.icon.stop()

    # --------------------------------------------------------------- poller
    def _poll(self) -> None:
        while not self._stop.wait(2.0):
            self._ensure_fixer()
            try:
                r = requests.get(f"{self.fixer_url or 'http://127.0.0.1:8787'}/status",
                                 timeout=4, headers=self._headers())
                if r.status_code == 200:
                    d = r.json()
                    self._live = True
                    pct = (100 * d.get("done", 0) / d["total"]) if d.get("total") else 0
                    self._status = (f"done {d.get('done',0)}/{d.get('total',0)} "
                                    f"({pct:.0f}%) · {d.get('rate_per_s',0):.1f}/s "
                                    f"· {d.get('pending',0)} queued")
                else:
                    self._live = False
                    self._status = f"fixer returned {r.status_code}"
            except requests.RequestException:
                self._live = False
                self._status = "fixer unreachable"
            try:
                self.icon.icon = _make_icon(_GREEN if self._live else _GREY)
                self.icon.title = f"KIROSHI — {self._status}"
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> int:
        self.icon = pystray.Icon("kiroshi", _make_icon(_GREY), "KIROSHI",
                                 menu=self._menu())
        threading.Thread(target=self._poll, name="kiroshi-tray-poll",
                         daemon=True).start()
        self.icon.run()
        return 0


def run_tray(fixer: Optional[str] = None, token: Optional[str] = None) -> int:
    return Tray(fixer, token).run()
