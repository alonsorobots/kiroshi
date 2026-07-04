"""System-tray UI — a thin, optional launcher + live status indicator.

Mirroring at-field's philosophy ("the tray just opens the dashboard URLs and
manages the services; the engine stays headless"), this is deliberately small:
a status icon whose colour/tooltip reflects mesh health, and a menu that opens
the console pages (with the mesh token injected so the browser just works) and
performs local control actions (graceful-stop the Coordinator/Runners on this box,
open the logs folder).

Requires the optional ``tray`` extra (``pip install kiroshi[tray]``) which pulls
in ``pystray`` + ``pillow``. Importing this module without them raises
``ImportError``, which the CLI turns into a friendly install hint.
"""
from __future__ import annotations

import functools
import os
import sys
import threading
import webbrowser
from datetime import datetime
from typing import Optional

import pystray  # noqa: E402  (import error surfaced as a friendly CLI hint)
import requests
from PIL import Image, ImageDraw

from . import security
from .appstate import logs_dir
from .discovery import discover_coordinator

_AUTO = {"auto", "discover", "", None}


def _log(msg: str) -> None:
    """pythonw-safe logging.

    Under ``pythonw.exe`` there is no console and ``sys.stdout`` may be ``None``,
    so a bare ``print`` raises ``AttributeError`` — which, if it happens inside a
    tray callback, propagates into the Win32 message loop and kills the icon.
    We append to a log file instead, and only fall back to ``print`` when a real
    stdout exists.
    """
    line = f"[{datetime.now():%H:%M:%S}] [tray] {msg}"
    try:
        with open(logs_dir() / "tray.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001  (logging must never raise)
        pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass


class _guard:
    """Wrap a pystray callback so an exception can never escape into the native
    message loop (which on Windows terminates ``icon.run()`` and makes the tray
    icon vanish). Any error is logged and swallowed.

    Implemented as a callable *object* (no ``__code__`` attribute) with a
    descriptor ``__get__`` so it binds correctly as a method. pystray inspects
    ``action.__code__.co_argcount`` to decide arity; a plain ``functools.wraps``
    wrapper over ``(*args, **kwargs)`` reports ``co_argcount == 0`` which becomes
    ``-1`` for a bound method and trips pystray's ``ValueError``. Because this
    object has no ``__code__``, pystray accepts it verbatim and simply calls it
    with ``(icon, item)``.
    """

    def __init__(self, fn, instance=None):
        self._fn = fn
        self._instance = instance
        functools.update_wrapper(self, getattr(fn, "__func__", fn), updated=())

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return _guard(self._fn.__get__(obj, objtype), instance=obj)

    def __call__(self, *args, **kwargs):
        try:
            return self._fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            name = getattr(self._fn, "__name__", self._fn)
            _log(f"callback {name!r} failed: {e!r}")
            return None


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


class _TrayIcon(pystray.Icon):
    """pystray Icon with left-click → open dashboard (at-field behavior).

    pystray's default ``__call__`` opens the *menu* on left-click, identical to
    right-click. Users expect left-click to open the main view, so we override
    it to launch the dashboard in the browser. Right-click still shows the menu.
    """

    def __init__(self, tray: "Tray", *args, **kwargs):
        self._tray = tray
        super().__init__(*args, **kwargs)

    @_guard
    def __call__(self) -> None:
        self._tray._ensure_coordinator()
        webbrowser.open(self._tray._url("/"))


class Tray:
    def __init__(self, coordinator: Optional[str], token: Optional[str]):
        self._coordinator_arg = coordinator
        self.coordinator_url = "" if (coordinator or "").strip().lower() in _AUTO else coordinator.rstrip("/")
        self.token = token if token is not None else security.resolve_token()
        self._stop = threading.Event()
        self._status = "starting…"
        self._live = False
        self.icon: Optional[pystray.Icon] = None

    # -------------------------------------------------------------- helpers
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _base(self) -> str:
        # Prefer the resolved/discovered coordinator; fall back to the conventional
        # local default only as a last resort.
        return self.coordinator_url or "http://127.0.0.1:8787"

    def _url(self, path: str) -> str:
        base = self._base()
        sep = "&" if "?" in path else "?"
        if self.token:
            return f"{base}{path}{sep}token={self.token}"
        return f"{base}{path}"

    def _open(self, path: str):
        @_guard
        def _cb(icon, item):
            self._ensure_coordinator()
            webbrowser.open(self._url(path))
        return _cb

    def _ensure_coordinator(self) -> None:
        if not self.coordinator_url:
            url = discover_coordinator(timeout=4.0)
            if url:
                self.coordinator_url = url

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
            Item("Stop local coordinator (drain)", self._stop_coordinator),
            pystray.Menu.SEPARATOR,
            Item("Quit tray", self._quit),
        )

    @_guard
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

    @_guard
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
            _log(f"mesh token: {self.token}")

    @_guard
    def _stop_runners(self, icon, item):
        from .processreg import list_registered, request_stop
        n = 0
        for p in list_registered():
            if p.get("role") == "runner":
                if request_stop("runner", int(p.get("pid", 0))):
                    n += 1
        self._notify(f"asked {n} runner(s) to drain")

    @_guard
    def _stop_coordinator(self, icon, item):
        from .processreg import list_registered, request_stop
        n = 0
        for p in list_registered():
            if p.get("role") == "coordinator":
                if request_stop("coordinator", int(p.get("pid", 0))):
                    n += 1
        self._notify(f"asked {n} coordinator(s) to drain")

    def _notify(self, msg: str):
        try:
            if self.icon is not None:
                self.icon.notify(msg, "KIROSHI")
            else:
                _log(msg)
        except Exception:  # noqa: BLE001
            _log(msg)

    @_guard
    def _quit(self, icon, item):
        self._stop.set()
        self.icon.stop()

    # --------------------------------------------------------------- poller
    def _poll(self) -> None:
        while not self._stop.wait(2.0):
            self._ensure_coordinator()
            base = self._base()
            try:
                r = requests.get(f"{base}/status", timeout=4,
                                 headers=self._headers())
                if r.status_code == 200:
                    d = r.json()
                    self._live = True
                    pct = (100 * d.get("done", 0) / d["total"]) if d.get("total") else 0
                    # Fetch job summaries so the tooltip shows readable
                    # labels (the fix for "too many jobs") not just a raw count.
                    jobs = self._fetch_jobs(base)
                    if jobs:
                        active = [c for c in jobs
                                  if c.get("leased", 0) or c.get("pending", 0)]
                        top = active[:3]
                        names = [c.get("label") or c.get("job", "?") for c in top]
                        if len(active) > 3:
                            names.append(f"… +{len(active) - 3} more")
                        job_str = " | ".join(names) if names else "no active jobs"
                        self._status = (f"{pct:.0f}% · {d.get('rate_per_s', 0):.1f}/s · "
                                        f"{d.get('pending', 0)} queued\n{job_str}")
                    else:
                        self._status = (f"done {d.get('done', 0)}/{d.get('total', 0)} "
                                        f"({pct:.0f}%) · {d.get('rate_per_s', 0):.1f}/s "
                                        f"· {d.get('pending', 0)} queued")
                    # Per-disk in-flight when a topology is active (N6): a compact
                    # one-line-per-disk breakdown so the tray shows which spindles
                    # are busy vs their budget.
                    di = d.get("disk_inflight")
                    db = d.get("disk_budget")
                    if di and db:
                        parts = [f"{did}:{di.get(did,0)}/{db.get(did,'?')}"
                                 for did in db]
                        self._status += "\n" + " ".join(parts)
                else:
                    self._live = False
                    self._status = f"coordinator returned {r.status_code}"
            except requests.RequestException:
                self._live = False
                self._status = "coordinator unreachable"
            try:
                self.icon.icon = _make_icon(_GREEN if self._live else _GREY)
                self.icon.title = f"KIROSHI — {self._status}"
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def _fetch_jobs(self, base: str) -> list[dict]:
        """Fetch /groups for the tray tooltip (best-effort)."""
        try:
            r = requests.get(f"{base}/groups?limit=20", timeout=4,
                             headers=self._headers())
            if r.status_code == 200:
                return r.json().get("groups", [])
        except requests.RequestException:
            pass
        return []

    def run(self) -> int:
        # Self-register for autostart on login (idempotent, best-effort).
        # Mirrors at-field's tray: the Coordinator is a boot-start service; the
        # tray is a login-start UI lens. This one-time registration means
        # the tray icon shows up automatically every time you log in.
        from . import autostart

        try:
            outcome = autostart.ensure_registered()
        except Exception as e:  # noqa: BLE001  (autostart must never block the UI)
            outcome = None
            _log(f"autostart registration failed: {e!r}")

        self.icon = _TrayIcon(
            self, "kiroshi", _make_icon(_GREY), "KIROSHI", menu=self._menu(),
        )
        # Notify *after* the icon exists (notify() needs a live icon handle).
        if outcome == "registered":
            _log("registered for autostart on login (HKCU\\Run)")
            self._notify("Kiroshi tray will auto-start on login")
        elif outcome == "updated":
            _log("updated autostart entry (interpreter moved?)")

        threading.Thread(target=self._poll, name="kiroshi-tray-poll",
                         daemon=True).start()
        self.icon.run()
        return 0


def run_tray(coordinator: Optional[str] = None, token: Optional[str] = None) -> int:
    return Tray(coordinator, token).run()
