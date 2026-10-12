import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from app.ai import mcp
from app.config import settings

log = logging.getLogger(__name__)

# All vault WRITES prefer the obsidian-mcp sidecar: it runs as the vault owner
# (uid 1000), so files it creates match what Obsidian Sync expects — the app
# container runs as root, and root-owned files break sync. Direct filesystem I/O
# remains as the fallback so capture never fails when the sidecar is down, and is
# still used where the MCP can't help: verbatim reads (read_note strips/reshapes
# frontmatter), mtime scans, and the atomic inbox-staging rename (which preserves
# ownership anyway).


async def _mcp_write(rel_path: str, content: str, mode: str) -> bool:
    """Write a note through obsidian-mcp. Returns False if the sidecar is
    unavailable or the call fails, so callers can fall back to a direct write."""
    if not mcp.has_tool("write_note"):
        return False
    try:
        await mcp.call_tool("write_note", {"path": rel_path, "content": content, "mode": mode})
        return True
    except Exception:
        log.exception("MCP write_note failed for %s — falling back to direct write", rel_path)
        return False


async def append_note(rel_path: str, content: str) -> None:
    """Append to a vault note, preferring obsidian-mcp, direct write as fallback."""
    if await _mcp_write(rel_path, content, "append"):
        return
    _file_append(Path(settings.vault_path) / rel_path, content)


async def write_note(rel_path: str, content: str) -> None:
    """Create/replace a vault note, preferring obsidian-mcp, direct write as fallback."""
    if await _mcp_write(rel_path, content, "overwrite"):
        return
    target = Path(settings.vault_path) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _match_vault_owner(target, target.parent)


def _format_entry(content: str, label: str | None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    heading = f"## {ts}" + (f" · {label}" if label else "")
    return f"\n{heading}\n{content}\n"


def _match_vault_owner(*paths: Path) -> None:
    """Chown files/dirs created by a direct write to the vault root's owner so
    Obsidian Sync (uid 1000) can manage them. Best-effort — a non-root process
    can't chown, and MCP-routed writes never need this."""
    try:
        st = Path(settings.vault_path).stat()
    except OSError:
        return
    for p in paths:
        try:
            if p.exists() and (p.stat().st_uid != st.st_uid or p.stat().st_gid != st.st_gid):
                os.chown(p, st.st_uid, st.st_gid)
        except OSError:
            pass


def _file_append(target: Path, entry: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("a", encoding="utf-8") as f:
            f.write(entry)
        log.debug("Appended to %s (direct write)", target.name)
        _match_vault_owner(target, target.parent)
    except OSError as exc:
        log.error("Failed to write to %s: %s", target, exc)


async def append_to_vault(content: str, label: str | None = None) -> None:
    await append_note(settings.inbox_filename, _format_entry(content, label))


async def record_capture(text: str, *, source: str) -> None:
    """Append a memo verbatim to the dated raw-capture log, before the agent runs.

    Durability must not depend on the model choosing to call a tool: between
    2026-07-26 and 2026-08-02 two captures produced zero tool calls and were lost
    entirely (nothing in the vault, nothing in Todoist, status "done"). This log is
    the floor — write_to_vault still files the memo into the inbox as normal, and
    the capture log lives under archive/ so is_machine_path keeps it out of inbox
    processing and the daily summary. Best-effort: never fail the capture path.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rel_path = f"{settings.capture_log_path}/{date_str}.md"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    line = f"- `{ts}` · **{source}** — {' '.join(text.split())}\n"
    target = Path(settings.vault_path) / rel_path
    try:
        if not target.exists() or target.stat().st_size == 0:
            line = f"# Raw Captures — {date_str}\n\n" + line
        await append_note(rel_path, line)
    except Exception:
        log.exception("Failed to record raw capture (continuing)")


async def read_from_vault(path: str) -> str:
    target = Path(settings.vault_path) / path.lstrip("/")
    if not target.exists():
        return f"Note not found: {path}"
    return target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Inbox processing lifecycle
#
# The processor never works on the live inbox. `begin_inbox_processing` atomically
# renames it to a staged working file (inbox.processing.md); memos arriving mid-run
# land in a fresh inbox.md and are handled next cycle. `archive_inbox` snapshots the
# staged file verbatim into the dated archive before removing it, so the destructive
# step can never lose data — and it only ever consumes the staged file, never the
# live inbox. Leftover staged/staging files from a crashed run are folded back in
# on the next `begin_inbox_processing`, never dropped.
# ---------------------------------------------------------------------------


def _inbox_path() -> Path:
    return Path(settings.vault_path) / settings.inbox_filename


def _processing_path() -> Path:
    p = _inbox_path()
    return p.with_name(p.stem + ".processing" + p.suffix)


def _merge_into(src: Path, dest: Path) -> None:
    """Append src's content to dest, then remove src. On failure src is kept."""
    try:
        content = src.read_text(encoding="utf-8")
        if content.strip():
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("a", encoding="utf-8") as f:
                f.write(content if content.startswith("\n") else "\n" + content)
            _match_vault_owner(dest)
        src.unlink()
    except OSError as exc:
        log.error("Failed to merge %s into %s — keeping source: %s", src, dest, exc)


async def begin_inbox_processing() -> str:
    """Stage the live inbox into the working file and return its content."""
    inbox = _inbox_path()
    proc = _processing_path()
    staging = inbox.with_name(inbox.name + ".staging")

    # Crash leftover from an interrupted staging on a previous run
    if staging.exists():
        _merge_into(staging, proc)

    if inbox.exists():
        inbox.replace(staging)  # atomic: new memos now land in a fresh inbox.md
        _merge_into(staging, proc)

    return proc.read_text(encoding="utf-8") if proc.exists() else ""


async def read_inbox_working() -> str:
    """Content of the staged inbox the current processing run is consuming."""
    proc = _processing_path()
    if not proc.exists():
        return "Inbox is empty."
    return proc.read_text(encoding="utf-8")


async def archive_inbox(processed_log: str) -> str:
    """Append the processed log + a verbatim snapshot of the staged inbox to a
    dated archive file, then remove the staged file.

    The snapshot is independent of the model-generated ``processed_log`` — even if
    that summary is incomplete or wrong, the original captured memos are preserved.
    The archive write is strict: any failure raises and the staged file is kept.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_path = f"{settings.inbox_archive_path}/{date_str}.md"
    proc = _processing_path()

    block = processed_log
    if proc.exists():
        raw = proc.read_text(encoding="utf-8")
        if raw.strip():
            block += (
                "\n\n## Raw captured entries (verbatim, preserved before clearing)\n"
                "```\n" + raw.strip() + "\n```\n"
            )

    # Append so multiple runs on the same day accumulate in one file. Strict:
    # if the MCP write fails, the direct fallback raises on failure (no swallowed
    # OSError) — the staged file is only removed once the archive write succeeded.
    if not await _mcp_write(archive_path, block, "append"):
        target = Path(settings.vault_path) / archive_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(block)
        _match_vault_owner(target, target.parent)

    if proc.exists():
        proc.unlink()
    return f"Archived to {archive_path} and inbox cleared."


def overwrite_guard(tool: str, inp: dict) -> str | None:
    """Refuse write_note calls that would silently overwrite an existing note.

    mcpvault's write_note defaults to mode 'overwrite'; a model picking an
    existing path would destroy its content with no recovery path. Appends,
    prepends, patch_note edits, and new-note creation all pass through.
    Returns a refusal message for the model, or None to allow the call.
    """
    if tool != "write_note":
        return None
    if inp.get("mode", "overwrite") != "overwrite":
        return None
    path = inp.get("path")
    if not path:
        return None
    if (Path(settings.vault_path) / str(path).lstrip("/")).exists():
        return (
            f"Refused: a note already exists at {path}. Overwriting would destroy its "
            "content. Use write_note with mode: 'append', or patch_note for targeted "
            "edits. Read the note first if you need its current content."
        )
    return None


# Top-level vault dirs whose churn is machine-generated or non-capture — never
# treated as user activity by the inbox processor or the daily summary.
_MACHINE_DIRS = {".obsidian", ".trash", "archive", "meta", "images", "Excalidraw"}


def is_machine_path(rel: Path) -> bool:
    """True for vault-relative paths that are machine-generated or non-capture:
    dot-dirs, the archive tree (inbox archives + audit logs), template/meta dirs,
    and the inbox capture files themselves."""
    if any(part.startswith(".") for part in rel.parts):
        return True
    if rel.parts and rel.parts[0] in _MACHINE_DIRS:
        return True
    inbox = Path(settings.inbox_filename)
    if rel.name in (inbox.name, _processing_path().name) or rel.name.endswith(".staging"):
        return True
    return False


def list_recent_files(hours: int = 24) -> list[str]:
    """Vault-relative paths of markdown notes modified within the last `hours`.

    Excludes machine-generated paths (see is_machine_path). Sorted most-recent
    first. Used as additional read-only input for the inbox processor.
    """
    vault_root = Path(settings.vault_path)
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    hits: list[tuple[float, str]] = []
    for md in vault_root.rglob("*.md"):
        rel = md.relative_to(vault_root)
        if is_machine_path(rel):
            continue
        try:
            mtime = md.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            hits.append((mtime, str(rel)))
    return [path for _, path in sorted(hits, reverse=True)]
