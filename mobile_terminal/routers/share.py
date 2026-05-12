"""Web Share Target endpoints.

Lets the user share files from any Android app into MTO. The flow:

  1. POST /share (multipart) — Android Web Share Target hands files
     here. We stage them under /tmp/mto-share-staging/<id>/ and
     303-redirect to /?share_id=<id>.
  2. The PWA boots, sees ?share_id, fetches /api/share/list/<id>,
     prompts the user to confirm a destination pane.
  3. POST /api/share/commit (form: share_id, pane_id) moves files from
     staging into <pane.cwd>/.claude/uploads/ — same convention as
     /api/upload — and returns the resolved paths so the PWA can
     insert them into #logInput on the chosen pane.

Staging has a 30-min lazy TTL.
"""
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from mobile_terminal.helpers import run_subprocess

logger = logging.getLogger(__name__)

SHARE_STAGING = Path("/tmp/mto-share-staging")
SHARE_TTL_SECONDS = 30 * 60
MAX_FILE_BYTES = 25 * 1024 * 1024


def _purge_old_staging() -> None:
    if not SHARE_STAGING.is_dir():
        return
    now = time.time()
    for entry in SHARE_STAGING.iterdir():
        try:
            if not entry.is_dir():
                continue
            if now - entry.stat().st_mtime > SHARE_TTL_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except Exception:
            pass


def _safe_filename(name: Optional[str], default: str = "shared") -> str:
    base = os.path.basename(name or default)
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ").strip()
    return cleaned or default


def register(app: FastAPI, deps) -> None:
    SHARE_STAGING.mkdir(parents=True, exist_ok=True)

    async def _resolve_pane_cwd(pane_id: str) -> Optional[Path]:
        """Look up a pane's current working directory via tmux.

        Falls back to the session's configured repo path if the tmux
        query fails, so a stale or invalid pane_id doesn't dead-end
        the share flow."""
        session = app.state.current_session
        try:
            parts = pane_id.split(":")
            if len(parts) == 2 and session:
                target = f"{session}:{parts[0]}.{parts[1]}"
                result = await run_subprocess(
                    ["tmux", "display-message", "-t", target, "-p", "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=2,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return Path(result.stdout.strip())
        except Exception as e:
            logger.debug(f"[share] tmux cwd lookup failed for {pane_id}: {e}")
        # Fallback: configured repo path for this session
        try:
            for repo in app.state.config.repos:
                if repo.session == session:
                    return Path(repo.path).expanduser().resolve()
        except Exception:
            pass
        return deps.get_current_repo_path()

    @app.post("/share")
    async def share_receive(
        files: List[UploadFile] = File(default_factory=list),
        title: Optional[str] = Form(None),
        text: Optional[str] = Form(None),
        url: Optional[str] = Form(None),
    ):
        """Web Share Target landing endpoint.

        Stage incoming files under a fresh share_id and redirect the
        user to the PWA so they can pick a destination pane. Title /
        text / url payloads (used when sharing a URL or note) are kept
        as a sidecar JSON; the frontend can choose to insert the text
        as a prompt prefill in addition to the files.
        """
        _purge_old_staging()
        share_id = uuid.uuid4().hex[:12]
        staging_dir = SHARE_STAGING / share_id
        staging_dir.mkdir(parents=True, exist_ok=True)

        saved: List[dict] = []
        for f in files or []:
            if not f or not f.filename:
                continue
            name = _safe_filename(f.filename)
            # Avoid overwrite within the same share
            dest = staging_dir / name
            n = 1
            while dest.exists():
                stem = dest.stem
                suf = dest.suffix
                dest = staging_dir / f"{stem}-{n}{suf}"
                n += 1
            content = await f.read()
            if len(content) > MAX_FILE_BYTES:
                logger.warning(f"[share] dropping oversized file {name} ({len(content)} bytes)")
                continue
            with open(dest, "wb") as out:
                out.write(content)
            saved.append({"name": dest.name, "size": len(content), "content_type": f.content_type or ""})

        meta = {
            "created_at": time.time(),
            "files": saved,
            "title": title or "",
            "text": text or "",
            "url": url or "",
        }
        (staging_dir / "_meta.json").write_text(json.dumps(meta))

        logger.info(f"[share] received share_id={share_id} files={len(saved)} text={bool(text)} url={bool(url)}")
        # Redirect via 303 so the browser switches to GET; PWA's start
        # handler reads ?share_id and opens the confirmation modal.
        # Prefix with config.base_path so the redirect lands inside
        # MTO's mount when fronted by a reverse proxy / Tailscale Funnel
        # — without this, /?share_id=... resolves outside the proxy
        # mount and the user sees a 502.
        base = ""
        try:
            base = (app.state.config.base_path or "").rstrip("/")
        except Exception:
            pass
        return RedirectResponse(url=f"{base}/?share_id={share_id}", status_code=303)

    @app.get("/api/share/list/{share_id}")
    async def share_list(share_id: str):
        staging_dir = SHARE_STAGING / share_id
        if not staging_dir.is_dir():
            raise HTTPException(status_code=404, detail="share_id not found")
        meta_path = staging_dir / "_meta.json"
        meta = {}
        try:
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        except Exception:
            meta = {}
        files = []
        for p in sorted(staging_dir.iterdir()):
            if p.name.startswith("_"):
                continue
            files.append({
                "name": p.name,
                "size": p.stat().st_size,
                "is_image": p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"},
            })
        return {
            "share_id": share_id,
            "files": files,
            "title": meta.get("title", ""),
            "text": meta.get("text", ""),
            "url": meta.get("url", ""),
        }

    @app.get("/api/share/preview/{share_id}/{filename}")
    async def share_preview(share_id: str, filename: str):
        """Serve a staged file so the PWA can render thumbnails."""
        staging_dir = SHARE_STAGING / share_id
        if not staging_dir.is_dir():
            raise HTTPException(status_code=404)
        safe = _safe_filename(filename)
        path = staging_dir / safe
        if not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path)

    @app.post("/api/share/commit")
    async def share_commit(
        share_id: str = Form(...),
        pane_id: str = Form(...),
    ):
        """Move staged files into the chosen pane's .claude/uploads/.

        Uses the same destination convention as /api/upload so paths
        resolve identically and existing log-view file rendering works
        without changes."""
        staging_dir = SHARE_STAGING / share_id
        if not staging_dir.is_dir():
            raise HTTPException(status_code=404, detail="share_id not found")

        cwd = await _resolve_pane_cwd(pane_id)
        if not cwd:
            raise HTTPException(status_code=400, detail=f"cannot resolve cwd for pane {pane_id}")

        uploads_dir = cwd / ".claude" / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

        moved: List[str] = []
        ts = int(time.time() * 1000)
        for src in sorted(staging_dir.iterdir()):
            if src.name.startswith("_"):
                continue
            # Mirror /api/upload's "upload-<ms>.<ext>" naming so the
            # log view's file detection has a consistent prefix to
            # latch onto. Each file gets a slightly bumped timestamp
            # so two simultaneous shares don't collide.
            ext = src.suffix.lstrip(".") or "bin"
            ts += 1
            dest_name = f"upload-{ts}.{ext}"
            dest = uploads_dir / dest_name
            try:
                shutil.move(str(src), str(dest))
                moved.append(str(dest.resolve()))
            except Exception as e:
                logger.warning(f"[share] move failed {src} -> {dest}: {e}")

        # Cleanup staging dir + sidecar
        shutil.rmtree(staging_dir, ignore_errors=True)
        logger.info(f"[share] committed share_id={share_id} pane_id={pane_id} count={len(moved)}")
        return {"paths": moved, "pane_id": pane_id}

    @app.delete("/api/share/{share_id}")
    async def share_cancel(share_id: str):
        staging_dir = SHARE_STAGING / share_id
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir, ignore_errors=True)
        return {"ok": True}
