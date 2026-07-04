"""Webhook bridge: Kiroshi advisories -> Cursor agent follow-up prompts.

Optional extra: ``pip install kiroshi[cursor]`` (pulls ``cursor-sdk``).
Run it with::

    python -m kiroshi.integrations.cursor_bridge   # or: kiroshi cursor-bridge

Endpoint ``POST /notify`` receives the exact JSON body Kiroshi's
:class:`~kiroshi.advisories.WebhookDispatcher` sends (``{advisory, origin}``),
formats the advisory into a compact prompt, and hands it to the Cursor SDK via
``Agent.resume(agent_id).send(prompt)``.

Deliberately fire-and-forget: it acks the webhook fast (so a slow SDK call
doesn't back up Kiroshi's dispatcher) and never blocks on the agent's response.
SDK errors are logged and dropped — a broken bridge must never break a run.

(Consolidated from the former ``kiroshi-cursor`` repo; that repo is retired.)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

logger = logging.getLogger(__name__)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9123


def _bearer_token(request: Request) -> Optional[str]:
    """Extract a Bearer token from the ``Authorization`` header, or None."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def format_prompt(advisory: dict[str, Any]) -> str:
    """Compose a Cursor-agent-facing prompt from one advisory.

    Deliberately compact — the agent's context is precious. Includes just
    enough for the model to know (a) what kind of thing happened, (b) where,
    (c) what to do about it, (d) how to look it up further.
    """
    sev = str(advisory.get("severity", "?")).upper()
    code = advisory.get("code", "?")
    disk = advisory.get("disk")
    scope = f"{code} on {disk}" if disk else code
    detail = (advisory.get("detail") or "").strip()
    action = (advisory.get("suggested_action") or "").strip()
    dash = advisory.get("dashboard_url") or ""
    fp = advisory.get("fingerprint") or code
    count = int(advisory.get("count") or 1)

    lines = [
        f"[Kiroshi advisory: {sev} {scope}]",
        "",
        detail or "(no detail supplied)",
    ]
    if action:
        lines += ["", f"Suggested action: {action}"]
    footer = f"Fingerprint: {fp}   Count: {count}"
    if dash:
        footer = f"Dashboard: {dash}   " + footer
    lines += ["", footer]
    return "\n".join(lines)


def _default_send(agent_id: str, prompt: str, *, api_key: str,
                  model: Optional[str] = None) -> None:
    """Resume the named Cursor agent and queue ``prompt`` as a follow-up.

    Imports the SDK lazily so tests can patch this whole function out without
    ever importing ``cursor_sdk``. Errors are re-raised; the caller runs this
    on a background task and swallows the exception.
    """
    from cursor_sdk import Agent, AgentOptions  # noqa: PLC0415

    opts_kwargs: dict[str, Any] = {"api_key": api_key}
    if model:
        opts_kwargs["model"] = model
    with Agent.resume(agent_id, AgentOptions(**opts_kwargs)) as agent:
        run = agent.send(prompt)
        run.wait()  # required — see Cursor SDK "Top Five Traps"


def create_app(
    api_key: Optional[str] = None,
    shared_secret: Optional[str] = None,
    model: Optional[str] = None,
    send_fn: Optional[Callable[..., None]] = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
      api_key: Cursor SDK key. Defaults to ``CURSOR_API_KEY`` env.
      shared_secret: Optional Bearer token required on incoming POSTs.
        Defaults to ``KIROSHI_CURSOR_TOKEN`` env.
      model: Optional model hint for ``Agent.resume`` (rarely needed).
        Defaults to ``KIROSHI_CURSOR_MODEL`` env.
      send_fn: Overridable "actually call the SDK" hook — tests inject a
        fake so no real API key or network is required.
    """
    api_key = api_key or os.environ.get("CURSOR_API_KEY", "")
    shared_secret = shared_secret if shared_secret is not None else \
        os.environ.get("KIROSHI_CURSOR_TOKEN")
    model = model or os.environ.get("KIROSHI_CURSOR_MODEL") or None
    send_fn = send_fn or _default_send

    app = FastAPI(title="kiroshi-cursor-bridge", version="0.1.0")
    app.state.api_key = api_key
    app.state.shared_secret = shared_secret
    app.state.model = model
    app.state.send_fn = send_fn
    app.state.last_dispatched: list[dict[str, Any]] = []

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "auth": bool(app.state.shared_secret),
            "api_key_configured": bool(app.state.api_key),
        }

    @app.post("/notify")
    async def notify(request: Request, bg: BackgroundTasks) -> dict[str, Any]:
        if app.state.shared_secret:
            presented = _bearer_token(request)
            if presented != app.state.shared_secret:
                raise HTTPException(status_code=401, detail="unauthorized")

        try:
            body = await request.json()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"bad JSON: {e}")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        advisory = body.get("advisory")
        origin = body.get("origin") or {}
        if not isinstance(advisory, dict):
            raise HTTPException(status_code=400,
                                detail="missing 'advisory' object in body")
        if not isinstance(origin, dict):
            raise HTTPException(status_code=400,
                                detail="'origin' must be a JSON object")

        agent_id = origin.get("agent_id")
        if not agent_id or not isinstance(agent_id, str):
            # No agent_id => nothing to do for this bridge (not an error —
            # Kiroshi may webhook other origin shapes: Slack, human on-call).
            return {"queued": False, "reason": "no agent_id in origin"}

        if not app.state.api_key:
            raise HTTPException(status_code=503,
                                detail="CURSOR_API_KEY not configured")

        prompt = format_prompt(advisory)
        dispatched = {
            "agent_id": agent_id,
            "prompt": prompt,
            "advisory_code": advisory.get("code"),
            "advisory_fingerprint": advisory.get("fingerprint"),
        }
        app.state.last_dispatched.append(dispatched)
        if len(app.state.last_dispatched) > 100:
            del app.state.last_dispatched[:-100]

        def _run() -> None:
            try:
                app.state.send_fn(
                    agent_id, prompt,
                    api_key=app.state.api_key,
                    model=app.state.model,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("cursor SDK send failed for agent %s: %r",
                               agent_id, e)

        bg.add_task(_run)
        return {"queued": True, "agent_id": agent_id,
                "code": advisory.get("code")}

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="kiroshi cursor-bridge",
        description="Webhook bridge: Kiroshi advisories -> Cursor Agent.resume().send()",
    )
    parser.add_argument("--host", default=os.environ.get("KIROSHI_CURSOR_HOST", DEFAULT_HOST),
                        help=f"Bind address (default: {DEFAULT_HOST}).")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("KIROSHI_CURSOR_PORT", DEFAULT_PORT)),
                        help=f"Bind port (default: {DEFAULT_PORT}).")
    args = parser.parse_args()

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
