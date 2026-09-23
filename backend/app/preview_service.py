"""Immutable repository snapshots and isolated, disposable UI preview builds.

Repository code is never executed on the API host. Plain HTML is copied; Vite
is compiled in a restricted Docker container with networking disabled. A
separate install container receives a sanitized dependency manifest only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import time
from typing import Any


MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024
MAX_FILES = 20_000
BUILD_TIMEOUT = 180
INSTALL_TIMEOUT = 240
STALE_BUILD_SECONDS = 2 * (BUILD_TIMEOUT + INSTALL_TIMEOUT + 160) + 120
VARIANTS = {"before", "after"}
BLOCKED_NAMES = {"node_modules", "dist", "build", "coverage", "__pycache__", "venv", "id_rsa", "id_ed25519"}
ASSET_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".wasm", ".txt", ".mp4", ".webm", ".mp3", ".ogg", ".wav"}


class PreviewError(Exception):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


def preview_root() -> Path:
    root = Path(os.environ.get("PREVIEW_STORAGE_PATH", str(Path(__file__).resolve().parents[1] / "preview_data"))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_root(workspace_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(workspace_id)):
        raise PreviewError("This job does not have a valid preview workspace.")
    path = preview_root() / str(workspace_id)
    if path.is_symlink() or not path.resolve().is_relative_to(preview_root()):
        raise PreviewError("This preview workspace is unavailable.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)
    finally:
        # Windows can deny replacing a file while another request reads it.
        # Never leave a partial metadata file after a failed atomic write.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _state(workspace_id: str, status: str, message: str, **extra: Any) -> None:
    _write_json(workspace_root(workspace_id) / "status.json", {"status": status, "message": message, "updated_at": int(time.time()), **extra})


def get_preview_status(workspace_id: str, job_id: int | None = None) -> dict[str, Any]:
    state = _read_json(workspace_root(workspace_id) / "status.json")
    if state.get("status") not in {"idle", "building", "ready", "unsupported", "failed"}:
        return {"status": "idle", "message": "A visual preview can be built when this change is ready."}
    # A process restart must not strand the UI in an eternal building state.
    if state["status"] == "building" and time.time() - state.get("updated_at", 0) > STALE_BUILD_SECONDS:
        return {"status": "failed", "message": "The preview build was interrupted. Try building it again."}
    return {key: state[key] for key in ("status", "message", "project_root", "kind") if key in state}


def _allowed_name(name: str) -> bool:
    return not name.startswith(".") and name.lower() not in BLOCKED_NAMES and not name.lower().endswith((".pem", ".key", ".p12", ".pfx"))


def _remove_private_tree(path: Path) -> None:
    root = preview_root()
    resolved = path.resolve()
    if path.is_symlink() or resolved == root or not resolved.is_relative_to(root):
        raise PreviewError("Preview cleanup could not be completed safely.")
    if path.exists():
        shutil.rmtree(path)


def _copy_tree(source: Path, destination: Path, *, assets_only: bool = False) -> None:
    source = source.resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=True)
    count = total = 0
    for current, directories, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in directories if _allowed_name(name) and not (current_path / name).is_symlink()]
        for name in filenames:
            path = current_path / name
            if not _allowed_name(name) or path.is_symlink():
                continue
            if assets_only and path.suffix.lower() not in ASSET_SUFFIXES:
                continue
            information = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(information.st_mode) or not path.resolve().is_relative_to(source):
                continue
            count += 1
            total += information.st_size
            if count > MAX_FILES or total > MAX_SNAPSHOT_BYTES:
                raise PreviewError("This repository is too large for an isolated visual preview.", "unsupported")
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target, follow_symlinks=False)


def create_snapshot(workspace_path: str, workspace_id: str, variant: str) -> bool:
    """Capture once, before/after editing. Failures never interrupt code generation."""
    temporary: Path | None = None
    try:
        if variant not in VARIANTS:
            raise PreviewError("Unknown preview version.")
        root = workspace_root(workspace_id)
        source = Path(workspace_path).resolve(strict=True)
        if not source.is_dir() or root.is_relative_to(source):
            raise PreviewError("The preview must be stored outside the repository.")
        target = root / "snapshots" / variant
        if target.exists():
            return not target.is_symlink()  # Original snapshots are immutable.
        temporary = root / f"snapshot-{variant}-{secrets.token_hex(8)}"
        _copy_tree(source, temporary)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.rename(target)
        return True
    except (OSError, PreviewError) as error:
        try:
            _state(workspace_id, getattr(error, "status", "failed"), str(error) if isinstance(error, PreviewError) else "The repository snapshot could not be prepared.")
        except (OSError, PreviewError):
            pass
        return False
    finally:
        if temporary is not None and temporary.exists():
            _remove_private_tree(temporary)


def _project(snapshot: Path) -> tuple[str, Path]:
    candidates = [snapshot, *(snapshot / name for name in ("frontend", "client", "web", "app", "site", "public"))]
    candidates += sorted(path.parent for path in snapshot.glob("*/*/package.json"))
    candidates = list(dict.fromkeys(path for path in candidates if path.is_dir() and not path.is_symlink()))
    another_framework = False
    for candidate in candidates:
        package = _read_json(candidate / "package.json")
        dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})} if isinstance(package.get("dependencies", {}), dict) and isinstance(package.get("devDependencies", {}), dict) else {}
        if "vite" in dependencies:
            if package.get("workspaces"):
                raise PreviewError("This workspace needs a custom preview build. Automatic previews support standalone Vite apps and static HTML.", "unsupported")
            return "vite", candidate
        another_framework = another_framework or bool(set(dependencies) & {"react-scripts", "next", "gatsby", "nuxt", "@angular/core", "@sveltejs/kit"})
    if another_framework:
        raise PreviewError("This framework needs a custom isolated preview builder. Automatic previews currently support Vite apps and static HTML.", "unsupported")
    for candidate in candidates:
        index = candidate / "index.html"
        if index.is_file() and not index.is_symlink():
            html = index.read_text(encoding="utf-8", errors="replace")
            if "%PUBLIC_URL%" in html or re.search(r"(?:src|href)\s*=\s*['\"][^'\"]*\.(?:tsx?|jsx)(?:[?'\"])", html, re.I):
                continue
            return "static", candidate
    raise PreviewError("No supported web app was found. Visual previews support static index.html sites and React/Vite projects.", "unsupported")


def _dependency_manifest(project: Path) -> dict[str, Any]:
    package = _read_json(project / "package.json")
    dependencies: dict[str, str] = {}
    for group in ("dependencies", "devDependencies", "optionalDependencies"):
        entries = package.get(group, {})
        if not isinstance(entries, dict):
            raise PreviewError("The project dependency manifest is not supported.", "unsupported")
        for name, version in entries.items():
            if not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name) or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9*~^<>=|.+ -]{1,120}", version):
                raise PreviewError("This app uses local or non-registry dependencies. A custom isolated preview builder is required.", "unsupported")
            dependencies[name] = version
    return {"name": "repoagent-isolated-preview", "private": True, "version": "1.0.0", "dependencies": dependencies}


def _docker(arguments: list[str], timeout: int = 30, *, check: bool = True) -> None:
    options: dict[str, Any] = {"check": check, "timeout": timeout, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.run(["docker", *arguments], **options)


def _constraints(name: str, user: str) -> list[str]:
    return ["run", "--rm", "--name", name, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=768m", "--memory-swap=768m", "--cpus=1", "--user", user, "--tmpfs", "/tmp:rw,nosuid,size=768m,mode=1777", "--env", "NODE_OPTIONS=--max-old-space-size=512"]


def _build_vite(snapshot: Path, project: Path, output: Path, scratch: Path) -> None:
    try:
        _docker(["info", "--format", "{{.ServerVersion}}"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise PreviewError("Start Docker to build this React/Vite preview, then try again. Repository code is never run directly on the server.", "unsupported") from None
    manifest = scratch / "package.json"
    manifest.write_text(json.dumps(_dependency_manifest(project)), encoding="utf-8")
    token = secrets.token_hex(10)
    volume = f"repoagent-preview-deps-{token}"
    install_name, build_name = f"repoagent-preview-install-{token}", f"repoagent-preview-build-{token}"
    image = os.environ.get("PREVIEW_BUILDER_IMAGE", "node:22-bookworm-slim")
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o777)
    try:
        _docker(["volume", "create", volume])
        # Only a generated package manifest enters the networked installer.
        # No repository, credentials, npm config, lockfile, or scripts are mounted.
        install = _constraints(install_name, "0:0") + ["--network=bridge", "--mount", f"type=volume,source={volume},target=/dependencies", "--mount", f"type=bind,source={manifest.resolve()},target=/manifest/package.json,readonly", "--workdir", "/dependencies", image, "sh", "-eu", "-c", "umask 000; cp /manifest/package.json ./package.json; npm install --ignore-scripts --no-audit --no-fund --package-lock=false --cache=/tmp/npm-cache --registry=https://registry.npmjs.org"]
        _docker(install, timeout=INSTALL_TIMEOUT)
        relative_project = project.relative_to(snapshot).as_posix()
        # Project config and dependency code execute only in this offline,
        # non-root container. The sole host write mount is disposable output.
        build = _constraints(build_name, "1000:1000") + ["--network=none", "--mount", f"type=volume,source={volume},target=/dependencies", "--mount", f"type=bind,source={snapshot.resolve()},target=/source,readonly", "--mount", f"type=bind,source={output.resolve()},target=/output", "--env", f"PREVIEW_PROJECT_ROOT={relative_project}", image, "sh", "-eu", "-c", 'mkdir -p /tmp/project; cp -R /source/. /tmp/project/; cd "/tmp/project/$PREVIEW_PROJECT_ROOT"; ln -s /dependencies/node_modules node_modules; node /dependencies/node_modules/vite/bin/vite.js build --base=/ --outDir=/output --emptyOutDir']
        _docker(build, timeout=BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PreviewError("The isolated preview build exceeded its time limit. Smaller standalone Vite apps are supported.") from None
    except (OSError, subprocess.SubprocessError):
        raise PreviewError("This app could not be built for preview. Check that its Vite build works without private dependencies, environment secrets, or a backend service.") from None
    finally:
        # Docker CLI timeout does not stop its container. Always remove both.
        for name in (install_name, build_name):
            try:
                _docker(["rm", "--force", name], check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            _docker(["volume", "rm", "--force", volume], check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def build_previews(workspace_id: str, job_id: int | None = None) -> None:
    root = workspace_root(workspace_id)
    lock = root / "build.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime > STALE_BUILD_SECONDS:
        lock.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        return
    scratch = root / f"build-{secrets.token_hex(10)}"
    try:
        snapshots = {variant: root / "snapshots" / variant for variant in VARIANTS}
        if not all(path.is_dir() and not path.is_symlink() for path in snapshots.values()):
            _state(workspace_id, "unsupported", "Original and updated snapshots are unavailable for this older job. Generate a new change to capture both versions.")
            return
        _state(workspace_id, "building", "Building isolated previews of the original and updated repository.")
        scratch.mkdir()
        projects = {variant: _project(snapshot) for variant, snapshot in snapshots.items()}
        for variant, snapshot in snapshots.items():
            kind, project = projects[variant]
            prepared = scratch / variant
            if kind == "vite":
                variant_scratch = scratch / f"{variant}-build"
                variant_scratch.mkdir()
                raw_output = variant_scratch / "output"
                _build_vite(snapshot, project, raw_output, variant_scratch)
                _copy_tree(raw_output, prepared, assets_only=True)
            else:
                _copy_tree(project, prepared, assets_only=True)
            if not (prepared / "index.html").is_file():
                raise PreviewError("The app did not produce an index.html page for the visual preview.", "unsupported")
        artifacts = root / "artifacts"
        if artifacts.exists():
            _remove_private_tree(artifacts)
        artifacts.mkdir()
        for variant in VARIANTS:
            (scratch / variant).rename(artifacts / variant)
        kind, project = projects["after"]
        _state(workspace_id, "ready", "Original and updated previews are ready. Network requests and backend services are disabled inside previews.", project_root=project.relative_to(snapshots["after"]).as_posix(), kind=kind)
    except PreviewError as error:
        _state(workspace_id, error.status, str(error))
    except Exception:
        _state(workspace_id, "failed", "The visual preview could not be prepared. Your generated code change is still available.")
    finally:
        if scratch.exists():
            _remove_private_tree(scratch)
        lock.unlink(missing_ok=True)


def create_capability(workspace_id: str, job_id: int, variant: str, session_id: str, session_expires_at: int) -> tuple[str, int]:
    if variant not in VARIANTS:
        raise PreviewError("Unknown preview version.")
    secret = os.environ.get("APP_SECRET") or os.environ.get("SESSION_SECRET", "")
    if len(secret) < 32:
        raise PreviewError("Preview links require a configured application secret.")
    bucket = int(time.time()) // 1800
    token = hmac.new(secret.encode(), f"preview:{workspace_id}:{job_id}:{variant}:{session_id}:{bucket}".encode(), hashlib.sha256).hexdigest()[:32]
    expires_at = min((bucket + 2) * 1800, int(session_expires_at))
    folder = preview_root() / "capabilities"
    folder.mkdir(exist_ok=True)
    path = folder / f"{token}.json"
    claims = {"workspace_id": workspace_id, "job_id": job_id, "variant": variant, "session_id": session_id, "expires_at": expires_at}
    # A token identifies one immutable set of claims. Repeated metadata reads
    # must not rewrite the file that simultaneous iframe requests are reading.
    if path.exists():
        if _read_json(path) == claims:
            return token, expires_at
        raise PreviewError("This preview link could not be prepared. Refresh the preview and try again.")
    try:
        _write_json(path, claims)
    except OSError:
        # Another request may have published the exact same claims while our
        # replace was denied. Only that verified record is safe to reuse.
        if _read_json(path) != claims:
            raise PreviewError("This preview link could not be prepared. Refresh the preview and try again.") from None
    return token, expires_at


def read_capability(token: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        return None
    result = _read_json(preview_root() / "capabilities" / f"{token}.json")
    if not isinstance(result.get("expires_at"), int) or result["expires_at"] <= time.time() or result.get("variant") not in VARIANTS:
        return None
    return result


def resolve_asset(workspace_id: str, variant: str, asset_path: str, *, spa: bool = False) -> Path | None:
    if variant not in VARIANTS or "\\" in asset_path or "\x00" in asset_path:
        return None
    relative = PurePosixPath(asset_path.lstrip("/"))
    if any(part in {".", ".."} or not _allowed_name(part) for part in relative.parts):
        return None
    root = workspace_root(workspace_id) / "artifacts" / variant
    if root.is_symlink() or not root.is_dir():
        return None
    target = root.joinpath(*relative.parts)
    if target.is_dir():
        target /= "index.html"
    if not target.exists() and spa and not relative.suffix:
        target = root / "index.html"
    if target.suffix.lower() not in ASSET_SUFFIXES or not target.is_file():
        return None
    if not target.resolve().is_relative_to(root.resolve()):
        return None
    check = target
    while check != root:
        if check.is_symlink():
            return None
        check = check.parent
    return target
