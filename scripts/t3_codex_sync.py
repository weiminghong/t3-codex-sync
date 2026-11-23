#!/usr/bin/env python3
"""
t3-codex-sync: Native-compatible Codex CLI -> T3 Code Desktop session sync & merge CLI.

Scans ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl and ~/.codex/session_index.jsonl,
parses records following T3 Code's native AgentSessionScanner / AgentSessionImporter
semantics, and atomically writes new import:codex:<sessionId> threads or incrementally
deduplicates & merges new messages into existing (including T3-forked) threads in
~/.t3/userdata/statev2.sqlite.

Zero external dependencies (Python 3.10+ standard library only).
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

# Ensure UTF-8 console output on Windows
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

DEFAULT_T3_DB = pathlib.Path.home() / ".t3" / "userdata" / "statev2.sqlite"
DEFAULT_CODEX_HOME = pathlib.Path.home() / ".codex"
DEFAULT_T3_EXE = pathlib.Path(os.environ.get("T3_CODE_EXE") or "T3 Code (Beta).exe")
DEFAULT_BACKUP_DIR = pathlib.Path.home() / ".t3" / "userdata" / "backups" / "t3-codex-sync"

IMPORT_EVENT_PREFIX = "agent-session-import:v2"
DEFAULT_CODEX_MODEL = "gpt-5.3-codex"
DEFAULT_RUNTIME_MODE = "full-access"
DEFAULT_INTERACTION_MODE = "default"
MAX_IMPORTED_MESSAGES = 200
MAX_BACKUPS_DEFAULT = 10

CODEX_ROLLOUT_ID_PATTERN = re.compile(
    r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))?"
    r"\.jsonl(?:\.zst)?$",
    re.IGNORECASE,
)

CODEX_TITLE_PREAMBLE = re.compile(
    r"^(?:\s*(?:<(recommended_plugins|environment_context|user_instructions|INSTRUCTIONS|"
    r"in-app-browser-context|external_codex_apps_open_page)(?:\s[^>]*)?>[\s\S]*?</\1>|"
    r"# AGENTS\.md instructions\b[^\n]*|## My request for Codex:))+"
)
WAKE_ENVELOPE = re.compile(r"^\s*<wake(?:\s[^>]*)?>([\s\S]*?)(?:</wake>|$)")


def utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def ms_to_iso(ms: int | float) -> str:
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_timestamp(value: Any, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    s = value.strip()
    try:
        if s.endswith("Z"):
            s_parse = s[:-1] + "+00:00"
        else:
            s_parse = s
        dt = datetime.datetime.fromisoformat(s_parse)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return fallback


def normalize_win_path(p: str) -> str:
    if not p:
        return ""
    expanded = os.path.expanduser(p.strip())
    norm = os.path.normpath(expanded)
    # Collapse slashes and lowercase on Windows
    norm = norm.replace("/", "\\").rstrip("\\")
    if len(norm) == 2 and norm[1] == ":":
        norm += "\\"
    return norm.lower() if sys.platform == "win32" else norm


def normalize_msg_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("text", "input_text", "output_text"):
            t = block.get("text")
            if isinstance(t, str):
                t = t.strip()
                if t:
                    parts.append(t)
    return "\n".join(parts)


def derive_codex_title(text: str) -> str | None:
    cleaned = CODEX_TITLE_PREAMBLE.sub("", text)
    cleaned = WAKE_ENVELOPE.sub(r"\1", cleaned).strip()
    if not cleaned:
        return None
    first_line = cleaned.split("\n", 1)[0][:100].strip()
    return first_line or None


def codex_turn_id(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    tid = metadata.get("turn_id")
    if isinstance(tid, str) and tid.strip():
        return tid
    return None


def file_identity(file_path: pathlib.Path, st: os.stat_result | None = None) -> dict[str, Any]:
    if st is None:
        st = os.stat(file_path)
    birthtime_ns = getattr(st, "st_birthtime_ns", None)
    if birthtime_ns is None:
        birthtime_ns = st.st_ctime_ns
    # Node/libuv on Windows stores the lower 32 bits of the volume serial number
    dev = int(st.st_dev) & 0xFFFFFFFF if sys.platform == "win32" else int(st.st_dev)
    return {
        "filePath": str(file_path),
        "size": int(st.st_size),
        "mtimeMs": int(st.st_mtime_ns // 1_000_000),
        "device": dev,
        "inode": str(st.st_ino),
        "birthtimeMs": int(birthtime_ns // 1_000_000),
    }


def same_transcript_identity(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    fp_a = normalize_win_path(str(stored.get("filePath") or ""))
    fp_b = normalize_win_path(str(current.get("filePath") or ""))
    if fp_a != fp_b:
        return False
    if stored.get("size") != current.get("size"):
        return False
    if stored.get("mtimeMs") != current.get("mtimeMs"):
        return False
    return True


@dataclass
class ParsedMessage:
    role: str  # "user" or "assistant"
    text: str
    created_at: str
    codex_response_user: bool = False


@dataclass
class ParsedCodexSession:
    session_id: str
    file_path: pathlib.Path
    identity: dict[str, Any]
    cwd: str
    title: str
    index_title: str | None
    model: str | None
    created_at: str
    updated_at: str
    all_messages: list[ParsedMessage]
    initial_messages: list[ParsedMessage]
    omitted_count: int


@dataclass
class ProjectInfo:
    project_id: str
    title: str
    workspace_root: str
    norm_root: str


@dataclass
class SyncAction:
    kind: str  # "new", "merge", "metadata_only", "up_to_date", "skipped_deleted", "skipped_unmatched", "skipped_empty", "skipped_error"
    session_id: str
    thread_id: str
    file_path: pathlib.Path
    project: ProjectInfo | None
    title: str
    new_messages: list[ParsedMessage] = field(default_factory=list)
    parsed: ParsedCodexSession | None = None
    existing_msg_count: int = 0
    existing_has_runs: bool = False
    reason: str = ""
    title_changed: bool = False


def read_codex_thread_names(codex_home: pathlib.Path) -> dict[str, str]:
    index_path = codex_home / "session_index.jsonl"
    names: dict[str, str] = {}
    if not index_path.exists():
        return names
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if isinstance(entry, dict):
                    sid = entry.get("id")
                    tname = entry.get("thread_name")
                    if isinstance(sid, str) and isinstance(tname, str) and tname.strip():
                        names[sid] = tname.strip()
    except OSError:
        pass
    return names


def read_codex_project_roots(codex_home: pathlib.Path) -> dict[str, list[str]]:
    state_db = codex_home / "state_5.sqlite"
    roots: dict[str, list[str]] = {}
    if not state_db.exists():
        return roots
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                """
                SELECT threads.id AS id, project_roots.path AS path
                FROM threads
                JOIN project_roots ON project_roots.project_id = threads.project_id
                ORDER BY threads.rowid DESC, project_roots.position
                LIMIT 50000
                """
            ).fetchall()
            for sid, root_path in rows:
                if isinstance(sid, str) and isinstance(root_path, str) and root_path.strip():
                    roots.setdefault(sid, []).append(root_path.strip())
        finally:
            conn.close()
    except Exception:
        pass
    return roots


def _read_jsonl_records(
    file_path: pathlib.Path,
    end_byte_offset: int | None = None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """
    Read a Codex rollout .jsonl (or .jsonl.zst if decompressor available).
    Uses a fast C-level bytes pre-filter to skip huge tool-output / reasoning lines
    without UTF-8 decoding or JSON parsing (10x-30x faster on 100MB+ rollouts).
    Returns (retained_records, cwd, is_subagent).
    """
    records: list[dict[str, Any]] = []
    cwd: str | None = None
    is_subagent = False

    def process_bytes_line(raw_bytes: bytes) -> None:
        nonlocal cwd, is_subagent
        if not raw_bytes or raw_bytes.isspace():
            return
        # Fast C-level filter on the line prefix: Codex always places "type" and payload "type" near the start
        head = raw_bytes[:300]
        if (
            b'"function_call_output"' in head
            or b'"custom_tool_call_output"' in head
            or b'"function_call"' in head
            or b'"custom_tool_call"' in head
            or b'"reasoning"' in head
            or b'"token_count"' in head
            or b'"agent_reasoning"' in head
        ):
            if cwd is not None or b'"cwd"' not in head:
                return
        might_match = (
            b'"session_meta"' in head
            or b'"turn_context"' in head
            or b'"user_message"' in head
            or b'"message"' in head
            or (cwd is None and b'"cwd"' in head)
        )
        if not might_match:
            return
        try:
            rec = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return
        if not isinstance(rec, dict):
            return
        rtype = rec.get("type")
        payload = rec.get("payload")
        if rtype == "session_meta" and isinstance(payload, dict):
            src = payload.get("source")
            if isinstance(src, dict) and ("subagent" in src or "subAgent" in src):
                is_subagent = True
        if cwd is None:
            c = rec.get("cwd")
            if not (isinstance(c, str) and c.strip()):
                c = payload.get("cwd") if isinstance(payload, dict) else None
            if isinstance(c, str) and c.strip():
                cwd = c.strip()

        # Retain only records needed for Codex parsing (keeps RAM low on 100MB+ rollouts)
        if rtype in ("session_meta", "turn_context"):
            records.append(rec)
        elif rtype == "event_msg" and isinstance(payload, dict) and payload.get("type") == "user_message":
            records.append(rec)
        elif (
            rtype == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "message"
            and payload.get("role") in ("user", "assistant")
        ):
            records.append(rec)

    if file_path.name.endswith(".jsonl.zst"):
        # Optional zstd support
        try:
            import zstandard  # type: ignore

            with file_path.open("rb") as fh:
                dctx = zstandard.ZstdDecompressor()
                with dctx.stream_reader(fh) as reader:
                    raw_bytes = reader.read(end_byte_offset) if end_byte_offset is not None else reader.read()
            for line in raw_bytes.splitlines():
                process_bytes_line(line)
        except ImportError:
            zstd_bin = shutil.which("zstd")
            if not zstd_bin:
                return [], None, False
            proc = subprocess.run(
                [zstd_bin, "-d", "-c", str(file_path)],
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                return [], None, False
            raw_bytes = proc.stdout[:end_byte_offset] if end_byte_offset is not None else proc.stdout
            for line in raw_bytes.splitlines():
                process_bytes_line(line)
        return records, cwd, is_subagent

    if end_byte_offset is not None:
        with file_path.open("rb") as f:
            data = f.read(end_byte_offset)
        for line in data.splitlines():
            process_bytes_line(line)
    else:
        with file_path.open("rb") as f:
            for line in f:
                process_bytes_line(line)

    return records, cwd, is_subagent


def _extract_history_base(records: list[dict[str, Any]]) -> tuple[str, int] | None:
    for rec in records:
        if rec.get("type") == "session_meta":
            payload = rec.get("payload")
            if isinstance(payload, dict):
                hb = payload.get("history_base")
                if isinstance(hb, dict):
                    tid = hb.get("thread_id")
                    off = hb.get("end_byte_offset")
                    if isinstance(tid, str) and tid.strip() and isinstance(off, int) and off >= 0:
                        return tid.strip(), off
            break
    return None


def parse_codex_records(
    records: list[dict[str, Any]],
    fallback_session_id: str,
    last_active_at_ms: int,
    index_title: str | None = None,
    max_initial_messages: int | None = MAX_IMPORTED_MESSAGES,
) -> tuple[str, str, str | None, str, str, list[ParsedMessage], list[ParsedMessage], int] | None:
    """
    Faithful Python port of T3 Code's parseAgentSessionRecords for source='codex',
    while preserving BOTH the full parsed message list (for accurate incremental merge)
    and T3's capped initial_messages + omitted_count (for native-compatible new imports).
    """
    fallback_ts = ms_to_iso(last_active_at_ms)
    provider_session_id = ""
    has_session_id = False
    model: str | None = None
    derived_title: str | None = None
    bare_command_title: str | None = None

    # Pass 1: Identify canonical response_item user messages duplicated by event_msg.user_message
    canonical_response_user_indices: set[int] = set()
    canonical_user_texts_in_turn: set[str] = set()
    response_users_in_turn: list[tuple[int, str, str]] = []

    def finish_codex_turn() -> None:
        nonlocal canonical_user_texts_in_turn, response_users_in_turn
        canonical_turn_ids = {
            turn_id
            for (_idx, turn_id, text) in response_users_in_turn
            if text in canonical_user_texts_in_turn
        }
        for idx, turn_id, _text in response_users_in_turn:
            if turn_id in canonical_turn_ids:
                canonical_response_user_indices.add(idx)
        canonical_user_texts_in_turn = set()
        response_users_in_turn = []

    for rec_idx, rec in enumerate(records):
        rtype = rec.get("type")
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        if rtype == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            finish_codex_turn()
            continue
        if rtype == "event_msg" and payload.get("type") == "user_message":
            msg = payload.get("message")
            text = msg.strip() if isinstance(msg, str) else ""
            if text:
                canonical_user_texts_in_turn.add(text)
            continue
        if rtype == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
            turn_id = codex_turn_id(payload.get("internal_chat_message_metadata_passthrough"))
            text = extract_text(payload.get("content"))
            if turn_id is not None and text:
                response_users_in_turn.append((rec_idx, turn_id, text))
    finish_codex_turn()

    # Pass 2: Build full deduplicated message timeline
    all_messages: list[ParsedMessage] = []
    first_user_message: ParsedMessage | None = None

    def retain_message(msg: ParsedMessage) -> None:
        nonlocal first_user_message, derived_title, bare_command_title
        if first_user_message is None and msg.role == "user":
            first_user_message = msg
        if derived_title is None and msg.role == "user":
            derived_title = derive_codex_title(msg.text)
            if bare_command_title is None:
                bare_command_title = derive_codex_title(msg.text)
        all_messages.append(msg)

    def has_matching_codex_event_in_turn(text: str) -> bool:
        comp = text.strip()
        for idx in range(len(all_messages) - 1, -1, -1):
            m = all_messages[idx]
            if m.role == "assistant":
                return False
            if m.role == "user" and not m.codex_response_user and m.text.strip() == comp:
                return True
        return False

    for rec_idx, rec in enumerate(records):
        rtype = rec.get("type")
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue

        if rtype == "session_meta":
            sid = (payload.get("id") or "").strip() or (payload.get("session_id") or "").strip()
            if not has_session_id and sid:
                provider_session_id = sid
                has_session_id = True
            continue

        if rtype == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                model = m.strip()
            continue

        if rtype == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message")
            if not isinstance(text, str) or not text.strip():
                continue
            for idx in range(len(all_messages) - 1, -1, -1):
                m = all_messages[idx]
                if m.role == "assistant":
                    break
                if m.codex_response_user and m.text.strip() == text.strip():
                    if first_user_message is m:
                        first_user_message = None
                    all_messages.pop(idx)
                    break
            retain_message(
                ParsedMessage(
                    role="user",
                    text=text,
                    created_at=normalize_timestamp(rec.get("timestamp"), fallback_ts),
                    codex_response_user=False,
                )
            )
            continue

        if rtype != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        extracted = extract_text(payload.get("content"))
        if not extracted:
            continue
        if role == "user" and rec_idx in canonical_response_user_indices:
            continue
        if role == "user" and has_matching_codex_event_in_turn(extracted):
            continue
        retain_message(
            ParsedMessage(
                role=role,
                text=extracted,
                created_at=normalize_timestamp(rec.get("timestamp"), fallback_ts),
                codex_response_user=(role == "user"),
            )
        )

    if not all_messages:
        return None
    if not provider_session_id:
        provider_session_id = fallback_session_id
    if not provider_session_id.strip() or first_user_message is None:
        return None

    # Compute T3-native capped initial import messages (first user + last N-1)
    if max_initial_messages is not None and len(all_messages) > max_initial_messages:
        tail = all_messages[-max_initial_messages:]
        if first_user_message in tail:
            initial_messages = list(tail)
        else:
            initial_messages = [first_user_message] + list(all_messages[-(max_initial_messages - 1):])
        omitted_count = len(all_messages) - len(initial_messages)
    else:
        initial_messages = list(all_messages)
        omitted_count = 0

    final_title = index_title or derived_title or bare_command_title or "Imported thread"
    created_at = initial_messages[0].created_at if initial_messages else fallback_ts
    updated_at = fallback_ts

    return (
        provider_session_id,
        final_title,
        model,
        created_at,
        updated_at,
        all_messages,
        initial_messages,
        omitted_count,
    )


def discover_codex_rollouts(
    codex_home: pathlib.Path,
    include_zst: bool = False,
) -> list[tuple[pathlib.Path, os.stat_result]]:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []
    found: list[tuple[pathlib.Path, os.stat_result]] = []
    for root, _dirs, files in os.walk(sessions_dir):
        for name in files:
            if not name.startswith("rollout-"):
                continue
            if name.endswith(".jsonl") or (include_zst and name.endswith(".jsonl.zst")):
                fp = pathlib.Path(root) / name
                try:
                    st = os.stat(fp)
                    found.append((fp, st))
                except OSError:
                    continue
    found.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
    return found


def build_rollout_lookup(codex_home: pathlib.Path) -> dict[str, pathlib.Path]:
    """Map rollout UUID (segment 2 or segment 1) -> file path for history_base resolution."""
    sessions_dir = codex_home / "sessions"
    lookup: dict[str, pathlib.Path] = {}
    if not sessions_dir.exists():
        return lookup
    for root, _dirs, files in os.walk(sessions_dir):
        for name in files:
            if not name.startswith("rollout-"):
                continue
            m = CODEX_ROLLOUT_ID_PATTERN.search(name)
            if not m:
                continue
            rid = m.group(2) or m.group(1)
            fp = pathlib.Path(root) / name
            # Prefer uncompressed .jsonl over .jsonl.zst if both exist
            if rid not in lookup or (lookup[rid].name.endswith(".zst") and name.endswith(".jsonl")):
                lookup[rid] = fp
    return lookup


def _peek_rollout_cwd_and_subagent(file_path: pathlib.Path) -> tuple[str | None, bool]:
    """Quickly read the first few lines of a .jsonl file to get cwd and subagent flag."""
    cwd: str | None = None
    is_subagent = False
    if file_path.name.endswith(".zst"):
        return None, False
    try:
        with file_path.open("rb") as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                if b'"session_meta"' not in line[:200] and b'"cwd"' not in line[:400]:
                    continue
                try:
                    rec = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                payload = rec.get("payload")
                if rec.get("type") == "session_meta" and isinstance(payload, dict):
                    src = payload.get("source")
                    if isinstance(src, dict) and ("subagent" in src or "subAgent" in src):
                        is_subagent = True
                        return None, True
                c = rec.get("cwd") or (payload.get("cwd") if isinstance(payload, dict) else None)
                if isinstance(c, str) and c.strip():
                    cwd = c.strip()
                    return cwd, False
    except OSError:
        pass
    return cwd, is_subagent


def parse_codex_file(
    file_path: pathlib.Path,
    st: os.stat_result,
    thread_names: dict[str, str],
    project_roots: dict[str, list[str]],
    rollout_lookup: dict[str, pathlib.Path],
    max_initial_messages: int | None = MAX_IMPORTED_MESSAGES,
    resolve_history_base: bool = True,
) -> tuple[ParsedCodexSession | None, str]:
    """Parse a single Codex rollout file including optional history_base chain."""
    records, cwd, is_subagent = _read_jsonl_records(file_path)
    if is_subagent:
        return None, "subagent"
    if not records:
        return None, "empty"

    # Resolve history_base prefixes if requested (needed for new imports; skipped for fast incremental merges)
    prefixes: list[list[dict[str, Any]]] = []
    if resolve_history_base:
        visited: set[str] = set()
        hb = _extract_history_base(records)
        while hb is not None:
            base_tid, end_off = hb
            if base_tid in visited:
                break
            visited.add(base_tid)
            base_path = rollout_lookup.get(base_tid)
            if base_path is None or not base_path.exists():
                break
            base_records, base_cwd, _ = _read_jsonl_records(base_path, end_byte_offset=end_off)
            if not base_records:
                break
            if cwd is None and base_cwd:
                cwd = base_cwd
            prefixes.insert(0, [r for r in base_records if r.get("type") != "session_meta"])
            hb = _extract_history_base(base_records)

    combined_records: list[dict[str, Any]] = []
    for pref in prefixes:
        combined_records.extend(pref)
    combined_records.extend(records)

    # Determine fallback session ID from filename
    m = CODEX_ROLLOUT_ID_PATTERN.search(file_path.name)
    fallback_sid = m.group(1) if m else file_path.stem

    # Fallback cwd from state_5.sqlite project_roots if missing or home directory
    home_norm = normalize_win_path(str(pathlib.Path.home()))
    if (not cwd or normalize_win_path(cwd) == home_norm) and fallback_sid in project_roots:
        cwd = project_roots[fallback_sid][0]
    if not cwd:
        return None, "no_cwd"

    ident = file_identity(file_path, st)
    # Determine actual session_id first to look up thread_names
    meta_sid = None
    for rec in combined_records:
        if rec.get("type") == "session_meta" and isinstance(rec.get("payload"), dict):
            meta_sid = (rec["payload"].get("id") or "").strip() or (rec["payload"].get("session_id") or "").strip()
            if meta_sid:
                break
    lookup_sid = meta_sid or fallback_sid
    idx_title = thread_names.get(lookup_sid)

    parsed = parse_codex_records(
        combined_records,
        fallback_session_id=fallback_sid,
        last_active_at_ms=ident["mtimeMs"],
        index_title=idx_title,
        max_initial_messages=max_initial_messages,
    )
    if parsed is None:
        return None, "empty"

    sid, title, model, created_at, updated_at, all_msgs, init_msgs, omitted = parsed
    return (
        ParsedCodexSession(
            session_id=sid,
            file_path=file_path,
            identity=ident,
            cwd=cwd,
            title=title,
            index_title=thread_names.get(sid),
            model=model,
            created_at=created_at,
            updated_at=updated_at,
            all_messages=all_msgs,
            initial_messages=init_msgs,
            omitted_count=omitted,
        ),
        "ok",
    )


def load_projects(conn: sqlite3.Connection) -> list[ProjectInfo]:
    rows = conn.execute(
        """
        SELECT project_id, title, workspace_root
        FROM projection_projects
        WHERE deleted_at IS NULL
        """
    ).fetchall()
    projects: list[ProjectInfo] = []
    for pid, title, root in rows:
        projects.append(
            ProjectInfo(
                project_id=pid,
                title=title,
                workspace_root=root,
                norm_root=normalize_win_path(root),
            )
        )
    # Sort longest norm_root first so most specific workspace matches first
    projects.sort(key=lambda p: len(p.norm_root), reverse=True)
    return projects


def match_project(cwd: str, projects: list[ProjectInfo]) -> ProjectInfo | None:
    norm_cwd = normalize_win_path(cwd)
    if not norm_cwd:
        return None
    for proj in projects:
        if norm_cwd == proj.norm_root:
            return proj
    for proj in projects:
        prefix = proj.norm_root.rstrip("\\") + "\\"
        if norm_cwd.startswith(prefix):
            return proj
    return None


def compute_incremental_messages(
    all_codex_messages: list[ParsedMessage],
    db_imported_messages: list[tuple[str, str, str]],  # (role, text, created_at) ordered chronologically
    db_run_messages: list[tuple[str, str, str]],  # (role, text, created_at) from T3 runs
) -> list[ParsedMessage]:
    """
    Smart deduplicated merge algorithm for existing (and possibly truncated or T3-forked) sessions:

    1. Locate the latest anchor in `all_codex_messages` matching the tail of `db_imported_messages`.
       This ensures previously truncated historical messages (before the first import window) are
       NEVER mistakenly resurrected as "new" messages.
    2. Take only the candidate slice strictly AFTER the anchor.
       (If no imported messages exist — e.g. a native T3 thread later continued in Codex CLI —
        anchor against `db_run_messages`.)
    3. Deduplicate the post-anchor candidate slice against `db_run_messages` (e.g., turns executed
       when T3 itself invoked Codex on the same native thread!) using a multiset counter on
       (role, normalized_text), preserving order and keeping only genuinely new Codex CLI messages.
    """
    if not all_codex_messages:
        return []

    codex_keys = [(m.role, normalize_msg_text(m.text)) for m in all_codex_messages]
    imp_keys = [(role, normalize_msg_text(txt)) for role, txt, _ts in db_imported_messages]
    run_keys = [(role, normalize_msg_text(txt)) for role, txt, _ts in db_run_messages]

    # Step 1: Find the latest anchor in all_codex_messages matching db_imported_messages
    anchor_idx = -1
    if imp_keys:
        # Prefer matching a 2-message suffix (or 1-message suffix) from the end of db_imported_messages
        # searching from the end of all_codex_messages backward.
        max_lookback = min(len(imp_keys), 25)
        for back in range(1, max_lookback + 1):
            target_key = imp_keys[-back]
            # Search from the end of codex_keys backward for this key
            for c_idx in range(len(codex_keys) - 1, -1, -1):
                if codex_keys[c_idx] == target_key:
                    # Verify previous imported message if available for extra confidence on short texts
                    if back < len(imp_keys) and c_idx > 0 and len(target_key[1]) < 40:
                        prev_imp = imp_keys[-back - 1]
                        # Look within a small window before c_idx
                        window_start = max(0, c_idx - 5)
                        if prev_imp not in codex_keys[window_start:c_idx]:
                            continue
                    anchor_idx = c_idx
                    break
            if anchor_idx >= 0:
                break

    if anchor_idx >= 0:
        candidates = all_codex_messages[anchor_idx + 1 :]
        # Build multiset of T3-run messages so T3-driven Codex turns are never duplicated
        run_counter = collections.Counter(run_keys)
        # Also guard against any late imported message that appeared after the matched anchor
        # (in case back > 1 matched an earlier imported message)
        imp_tail_counter = collections.Counter(imp_keys[-10:])
        # Only use run_counter + check exact match against all existing DB messages if created_at <= max_imp_ts
        max_imp_ts = max((ts for _, _, ts in db_imported_messages), default="")
        result: list[ParsedMessage] = []
        for msg in candidates:
            key = (msg.role, normalize_msg_text(msg.text))
            if run_counter[key] > 0:
                run_counter[key] -= 1
                continue
            if max_imp_ts and msg.created_at <= max_imp_ts and imp_tail_counter[key] > 0:
                imp_tail_counter[key] -= 1
                continue
            result.append(msg)
        return result

    # Fallback: No imported anchor found (e.g., native T3 thread or completely different history)
    existing_all = db_imported_messages + db_run_messages
    existing_counter = collections.Counter((r, normalize_msg_text(t)) for r, t, _ in existing_all)
    max_existing_ts = max((ts for _, _, ts in existing_all), default="")

    # Try anchoring to the last matching message in existing_all
    last_match_idx = -1
    existing_set = set(existing_counter.keys())
    for c_idx in range(len(codex_keys) - 1, -1, -1):
        if codex_keys[c_idx] in existing_set:
            last_match_idx = c_idx
            break

    if last_match_idx >= 0:
        candidates = all_codex_messages[last_match_idx + 1 :]
        return [m for m in candidates if (m.role, normalize_msg_text(m.text)) not in existing_set]

    # Last resort: timestamp filter + dedup
    result = []
    for msg in all_codex_messages:
        key = (msg.role, normalize_msg_text(msg.text))
        if existing_counter[key] > 0:
            existing_counter[key] -= 1
            continue
        if max_existing_ts and msg.created_at <= max_existing_ts:
            continue
        result.append(msg)
    return result


def plan_sync(
    conn: sqlite3.Connection,
    codex_home: pathlib.Path,
    session_filter: str | None = None,
    project_filter: str | None = None,
    force: bool = False,
    include_zst: bool = False,
    max_initial_messages: int | None = MAX_IMPORTED_MESSAGES,
) -> list[SyncAction]:
    projects = load_projects(conn)
    thread_names = read_codex_thread_names(codex_home)
    project_roots = read_codex_project_roots(codex_home)
    rollout_lookup = build_rollout_lookup(codex_home)

    # Load existing threads & native codex ownership
    threads_by_id: dict[str, sqlite3.Row] = {}
    for row in conn.execute(
        """
        SELECT thread_id, project_id, title, default_provider, provider_instance_id,
               runtime_mode, interaction_mode, active_provider_thread_id,
               created_at, updated_at, archived_at, deleted_at, payload_json
        FROM orchestration_v2_projection_threads
        """
    ).fetchall():
        threads_by_id[row["thread_id"]] = row

    native_owner: dict[str, str] = {}
    for row in conn.execute(
        """
        SELECT thread_id, json_extract(payload_json, '$.nativeThreadRef.nativeId') AS native_id
        FROM orchestration_v2_projection_provider_threads
        WHERE provider = 'codex'
        """
    ).fetchall():
        nid = row["native_id"]
        tid = row["thread_id"]
        if nid and tid:
            # Prefer non-deleted thread ownership
            prev = native_owner.get(nid)
            if prev is None or (prev in threads_by_id and threads_by_id[prev]["deleted_at"] is not None):
                native_owner[nid] = tid

    # Load provider_session_runtime importedTranscripts
    imported_by_tid: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT thread_id, runtime_payload_json FROM provider_session_runtime"
    ).fetchall():
        try:
            payload = json.loads(row["runtime_payload_json"] or "{}")
            if isinstance(payload, dict) and isinstance(payload.get("importedTranscripts"), list):
                imported_by_tid[row["thread_id"]] = payload["importedTranscripts"]
        except Exception:
            pass

    rollouts = discover_codex_rollouts(codex_home, include_zst=include_zst)
    seen_sessions: set[str] = set()
    actions: list[SyncAction] = []

    for fp, st in rollouts:
        m = CODEX_ROLLOUT_ID_PATTERN.search(fp.name)
        fname_sid = m.group(1) if m else fp.stem
        if session_filter and session_filter.lower() not in fname_sid.lower() and session_filter.lower() not in fp.name.lower():
            continue

        # Fast-path check before parsing huge files
        tentative_tid = native_owner.get(fname_sid) or f"import:codex:{fname_sid}"
        ident = file_identity(fp, st)
        tentative_row = threads_by_id.get(tentative_tid)

        # Fast project_filter check before full file parse
        if project_filter:
            if tentative_row is not None:
                t_proj = next((p for p in projects if p.project_id == tentative_row["project_id"]), None)
                if t_proj is None or (
                    project_filter.lower() not in t_proj.title.lower()
                    and project_filter.lower() not in t_proj.workspace_root.lower()
                ):
                    continue
            else:
                peek_cwd, peek_sub = _peek_rollout_cwd_and_subagent(fp)
                if peek_sub:
                    continue
                if peek_cwd:
                    p_proj = match_project(peek_cwd, projects)
                    if p_proj is None:
                        if project_filter.lower() not in peek_cwd.lower():
                            continue
                    elif (
                        project_filter.lower() not in p_proj.title.lower()
                        and project_filter.lower() not in p_proj.workspace_root.lower()
                    ):
                        continue

        if tentative_row is not None:
            if tentative_row["deleted_at"] is not None:
                if fname_sid not in seen_sessions:
                    seen_sessions.add(fname_sid)
                    actions.append(
                        SyncAction(
                            kind="skipped_deleted",
                            session_id=fname_sid,
                            thread_id=tentative_tid,
                            file_path=fp,
                            project=None,
                            title=tentative_row["title"],
                            reason="thread deleted in T3",
                        )
                    )
                continue

            if not force:
                stored_list = imported_by_tid.get(tentative_tid, [])
                if any(isinstance(s, dict) and same_transcript_identity(s, ident) for s in stored_list):
                    idx_title = thread_names.get(fname_sid)
                    if not idx_title or idx_title == tentative_row["title"]:
                        if fname_sid not in seen_sessions:
                            seen_sessions.add(fname_sid)
                            proj = next((p for p in projects if p.project_id == tentative_row["project_id"]), None)
                            actions.append(
                                SyncAction(
                                    kind="up_to_date",
                                    session_id=fname_sid,
                                    thread_id=tentative_tid,
                                    file_path=fp,
                                    project=proj,
                                    title=tentative_row["title"],
                                )
                            )
                        continue

        # Also skip already-seen sessions (older duplicate rollout files for the same session_id)
        if fname_sid in seen_sessions:
            continue

        # For existing threads, skip slow history_base .zst decompression since new messages are in the current file
        parsed, status = parse_codex_file(
            fp,
            st,
            thread_names=thread_names,
            project_roots=project_roots,
            rollout_lookup=rollout_lookup,
            max_initial_messages=max_initial_messages,
            resolve_history_base=(tentative_row is None),
        )
        if parsed is None:
            if status not in ("subagent",):
                if fname_sid not in seen_sessions:
                    seen_sessions.add(fname_sid)
                    actions.append(
                        SyncAction(
                            kind="skipped_empty",
                            session_id=fname_sid,
                            thread_id=tentative_tid,
                            file_path=fp,
                            project=None,
                            title="",
                            reason=status,
                        )
                    )
            continue

        sid = parsed.session_id
        if sid in seen_sessions:
            continue
        seen_sessions.add(sid)

        if session_filter and session_filter.lower() not in sid.lower() and session_filter.lower() not in fp.name.lower():
            continue

        thread_id = native_owner.get(sid) or f"import:codex:{sid}"
        existing_row = threads_by_id.get(thread_id)

        if existing_row is not None:
            proj = next((p for p in projects if p.project_id == existing_row["project_id"]), None)
            if project_filter and (
                proj is None
                or (
                    project_filter.lower() not in proj.title.lower()
                    and project_filter.lower() not in proj.workspace_root.lower()
                )
            ):
                continue

            if existing_row["deleted_at"] is not None:
                actions.append(
                    SyncAction(
                        kind="skipped_deleted",
                        session_id=sid,
                        thread_id=thread_id,
                        file_path=fp,
                        project=proj,
                        title=existing_row["title"],
                        reason="thread deleted in T3",
                    )
                )
                continue

            # Load existing messages for smart dedup
            msg_rows = conn.execute(
                """
                SELECT m.message_id, m.run_id, m.role, m.created_at,
                       json_extract(m.payload_json, '$.text') AS txt
                FROM orchestration_v2_projection_messages AS m
                LEFT JOIN orchestration_v2_projection_turn_items AS ti
                  ON ti.thread_id = m.thread_id
                  AND json_extract(ti.payload_json, '$.messageId') = m.message_id
                WHERE m.thread_id = ?
                ORDER BY COALESCE(ti.ordinal, 0), m.created_at, m.message_id
                """,
                (thread_id,),
            ).fetchall()

            db_imported: list[tuple[str, str, str]] = []
            db_runs: list[tuple[str, str, str]] = []
            for mr in msg_rows:
                txt = mr["txt"] if isinstance(mr["txt"], str) else ""
                if mr["run_id"] is None:
                    db_imported.append((mr["role"], txt, mr["created_at"]))
                else:
                    db_runs.append((mr["role"], txt, mr["created_at"]))

            run_count = conn.execute(
                "SELECT COUNT(*) FROM orchestration_v2_projection_runs WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]

            new_msgs = compute_incremental_messages(parsed.all_messages, db_imported, db_runs)
            title_changed = bool(parsed.index_title and parsed.index_title != existing_row["title"])
            stored_list = imported_by_tid.get(thread_id, [])
            stat_changed = not any(
                isinstance(s, dict) and same_transcript_identity(s, parsed.identity)
                for s in stored_list
            )

            if new_msgs:
                actions.append(
                    SyncAction(
                        kind="merge",
                        session_id=sid,
                        thread_id=thread_id,
                        file_path=fp,
                        project=proj,
                        title=parsed.index_title or existing_row["title"],
                        new_messages=new_msgs,
                        parsed=parsed,
                        existing_msg_count=len(msg_rows),
                        existing_has_runs=(run_count > 0),
                        title_changed=title_changed,
                    )
                )
            elif title_changed or stat_changed:
                actions.append(
                    SyncAction(
                        kind="metadata_only",
                        session_id=sid,
                        thread_id=thread_id,
                        file_path=fp,
                        project=proj,
                        title=parsed.index_title or existing_row["title"],
                        parsed=parsed,
                        existing_msg_count=len(msg_rows),
                        existing_has_runs=(run_count > 0),
                        title_changed=title_changed,
                        reason="stat/title updated, 0 new messages after dedup",
                    )
                )
            else:
                actions.append(
                    SyncAction(
                        kind="up_to_date",
                        session_id=sid,
                        thread_id=thread_id,
                        file_path=fp,
                        project=proj,
                        title=existing_row["title"],
                        existing_msg_count=len(msg_rows),
                        existing_has_runs=(run_count > 0),
                    )
                )
        else:
            proj = match_project(parsed.cwd, projects)
            if project_filter:
                if proj is None:
                    if project_filter.lower() not in parsed.cwd.lower():
                        continue
                elif (
                    project_filter.lower() not in proj.title.lower()
                    and project_filter.lower() not in proj.workspace_root.lower()
                ):
                    continue

            if proj is None:
                actions.append(
                    SyncAction(
                        kind="skipped_unmatched",
                        session_id=sid,
                        thread_id=thread_id,
                        file_path=fp,
                        project=None,
                        title=parsed.title,
                        parsed=parsed,
                        reason=f"cwd not matched to any T3 project: {parsed.cwd}",
                    )
                )
                continue

            actions.append(
                SyncAction(
                    kind="new",
                    session_id=sid,
                    thread_id=thread_id,
                    file_path=fp,
                    project=proj,
                    title=parsed.title,
                    new_messages=parsed.initial_messages,
                    parsed=parsed,
                )
            )

    return actions


def _next_stream_version(conn: sqlite3.Connection, thread_id: str) -> int:
    row = conn.execute(
        """
        SELECT MAX(stream_version)
        FROM orchestration_events
        WHERE aggregate_kind = 'thread' AND stream_id = ?
        """,
        (thread_id,),
    ).fetchone()
    return 0 if row[0] is None else int(row[0]) + 1


def _append_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    thread_id: str,
    stream_version: int,
    event_type: str,
    occurred_at: str,
    payload: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO orchestration_events (
            event_id, aggregate_kind, stream_id, stream_version,
            event_type, occurred_at, command_id, causation_event_id,
            correlation_id, actor_kind, payload_json, metadata_json,
            application_event_version
        )
        VALUES (?, 'thread', ?, ?, ?, ?, NULL, NULL, NULL, 'server', ?, ?, 2)
        """,
        (
            event_id,
            thread_id,
            stream_version,
            event_type,
            occurred_at,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def _upsert_imported_transcript_runtime(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    session_id: str,
    cwd: str,
    last_seen_at: str,
    source_entry: dict[str, Any],
) -> None:
    row = conn.execute(
        "SELECT runtime_payload_json, last_seen_at FROM provider_session_runtime WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()

    if row is None:
        runtime_payload = {
            "cwd": cwd,
            "importedTranscripts": [source_entry],
        }
        conn.execute(
            """
            INSERT INTO provider_session_runtime (
                thread_id, provider_name, adapter_key, runtime_mode,
                status, last_seen_at, resume_cursor_json,
                runtime_payload_json, provider_instance_id
            )
            VALUES (?, 'codex', 'codex', ?, 'stopped', ?, ?, ?, 'codex')
            """,
            (
                thread_id,
                DEFAULT_RUNTIME_MODE,
                last_seen_at,
                json.dumps({"threadId": session_id}, ensure_ascii=False, separators=(",", ":")),
                json.dumps(runtime_payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return

    try:
        payload = json.loads(row["runtime_payload_json"] or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    if not payload.get("cwd"):
        payload["cwd"] = cwd

    existing_list = payload.get("importedTranscripts")
    if not isinstance(existing_list, list):
        existing_list = []

    target_fp_norm = normalize_win_path(str(source_entry.get("filePath") or ""))
    filtered = [
        item
        for item in existing_list
        if not (
            isinstance(item, dict)
            and item.get("providerInstanceId") == "codex"
            and normalize_win_path(str(item.get("filePath") or "")) == target_fp_norm
        )
    ]
    filtered.append(source_entry)
    payload["importedTranscripts"] = filtered

    new_last_seen = max(str(row["last_seen_at"] or ""), last_seen_at)
    conn.execute(
        """
        UPDATE provider_session_runtime
        SET last_seen_at = ?,
            resume_cursor_json = COALESCE(resume_cursor_json, ?),
            runtime_payload_json = ?
        WHERE thread_id = ?
        """,
        (
            new_last_seen,
            json.dumps({"threadId": session_id}, ensure_ascii=False, separators=(",", ":")),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            thread_id,
        ),
    )


def _insert_message_and_turn_item(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    suffix_idx: int,
    ordinal: int,
    stream_version: int,
    msg: ParsedMessage,
) -> int:
    """Write message.updated + turn-item.updated events, projections, and position. Returns next stream_version."""
    # Ensure unique suffix_idx against existing events/messages
    while True:
        suffix = f"{suffix_idx:06d}"
        message_id = f"{thread_id}:{suffix}"
        msg_event_id = f"{IMPORT_EVENT_PREFIX}:message:{thread_id}:{suffix}"
        turn_item_id = f"{IMPORT_EVENT_PREFIX}:turn-item:{thread_id}:{suffix}"
        clash = conn.execute(
            """
            SELECT 1 FROM orchestration_events WHERE event_id IN (?, ?)
            UNION ALL
            SELECT 1 FROM orchestration_v2_projection_messages WHERE message_id = ?
            UNION ALL
            SELECT 1 FROM orchestration_v2_projection_turn_items WHERE turn_item_id = ?
            LIMIT 1
            """,
            (msg_event_id, turn_item_id, message_id, turn_item_id),
        ).fetchone()
        if clash is None:
            break
        suffix_idx += 1

    at = msg.created_at
    msg_payload: dict[str, Any] = {
        "createdBy": "user" if msg.role == "user" else "agent",
        "creationSource": "server",
        "id": message_id,
        "threadId": thread_id,
        "runId": None,
        "nodeId": None,
        "role": msg.role,
        "text": msg.text,
        "attachments": [],
        "streaming": False,
        "createdAt": at,
        "updatedAt": at,
    }

    ti_common: dict[str, Any] = {
        "id": turn_item_id,
        "threadId": thread_id,
        "runId": None,
        "nodeId": None,
        "providerThreadId": None,
        "providerTurnId": None,
        "nativeItemRef": None,
        "parentItemId": None,
        "ordinal": ordinal,
        "status": "completed",
        "title": None,
        "startedAt": at,
        "completedAt": at,
        "updatedAt": at,
    }

    if msg.role == "user":
        ti_type = "user_message"
        ti_payload = {
            **ti_common,
            "createdBy": "user",
            "creationSource": "server",
            "type": "user_message",
            "messageId": message_id,
            "inputIntent": "turn_start",
            "text": msg.text,
            "attachments": [],
        }
    else:
        ti_type = "assistant_message"
        ti_payload = {
            **ti_common,
            "type": "assistant_message",
            "messageId": message_id,
            "text": msg.text,
            "streaming": False,
        }

    # 1. orchestration_events
    _append_event(
        conn,
        event_id=msg_event_id,
        thread_id=thread_id,
        stream_version=stream_version,
        event_type="message.updated",
        occurred_at=at,
        payload=msg_payload,
        metadata={},
    )
    stream_version += 1

    _append_event(
        conn,
        event_id=turn_item_id,
        thread_id=thread_id,
        stream_version=stream_version,
        event_type="turn-item.updated",
        occurred_at=at,
        payload=ti_payload,
        metadata={},
    )
    stream_version += 1

    # 2. orchestration_v2_projection_messages (triggers FTS5 automatically)
    conn.execute(
        """
        INSERT INTO orchestration_v2_projection_messages (
            message_id, thread_id, run_id, node_id, role,
            streaming, created_at, updated_at, payload_json, search_text
        )
        VALUES (?, ?, NULL, NULL, ?, 0, ?, ?, ?, NULL)
        """,
        (
            message_id,
            thread_id,
            msg.role,
            at,
            at,
            json.dumps(msg_payload, ensure_ascii=False, separators=(",", ":")),
        ),
    )

    # 3. orchestration_v2_turn_item_positions
    conn.execute(
        """
        INSERT INTO orchestration_v2_turn_item_positions (thread_id, turn_item_id, ordinal)
        VALUES (?, ?, ?)
        """,
        (thread_id, turn_item_id, ordinal),
    )

    # 4. orchestration_v2_projection_turn_items (triggers item count tables automatically)
    conn.execute(
        """
        INSERT INTO orchestration_v2_projection_turn_items (
            turn_item_id, thread_id, run_id, node_id,
            provider_thread_id, provider_turn_id, parent_item_id,
            ordinal, type, status, updated_at, payload_json, inline_work_output
        )
        VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, 'completed', ?, ?, 0)
        """,
        (
            turn_item_id,
            thread_id,
            ordinal,
            ti_type,
            at,
            json.dumps(ti_payload, ensure_ascii=False, separators=(",", ":")),
        ),
    )

    return stream_version


def apply_new_session(conn: sqlite3.Connection, action: SyncAction) -> None:
    assert action.parsed is not None and action.project is not None
    parsed = action.parsed
    project = action.project
    thread_id = action.thread_id
    sid = parsed.session_id
    provider_thread_id = f"provider-thread:provider:codex:provider-instance:codex:native-thread:{sid}"
    model = parsed.model or DEFAULT_CODEX_MODEL
    title = parsed.title.strip() or "Untitled thread"
    created_at = parsed.created_at
    updated_at = parsed.updated_at

    app_thread: dict[str, Any] = {
        "createdBy": "system",
        "creationSource": "server",
        "id": thread_id,
        "projectId": project.project_id,
        "title": title,
        "providerInstanceId": "codex",
        "modelSelection": {"instanceId": "codex", "model": model},
        "runtimeMode": DEFAULT_RUNTIME_MODE,
        "interactionMode": DEFAULT_INTERACTION_MODE,
        "branch": None,
        "worktreePath": None,
        "linkedPullRequest": None,
        "branchPullRequest": None,
        "activeProviderThreadId": provider_thread_id,
        "historyOrigin": "v1_import",
        "lineage": {
            "parentThreadId": None,
            "relationshipToParent": None,
            "rootThreadId": thread_id,
        },
        "forkedFrom": None,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "archivedAt": None,
        "settledOverride": "settled",
        "settledAt": updated_at,
        "unsettledAt": None,
        "snoozedUntil": None,
        "snoozedAt": None,
        "pinnedAt": None,
        "pinOrderKey": None,
        "activeOrderKey": None,
        "lastVisitedAt": None,
        "deletedAt": None,
    }

    provider_thread: dict[str, Any] = {
        "id": provider_thread_id,
        "driver": "codex",
        "providerInstanceId": "codex",
        "providerSessionId": None,
        "appThreadId": thread_id,
        "ownerNodeId": None,
        "nativeThreadRef": {
            "driver": "codex",
            "nativeId": sid,
            "strength": "strong",
        },
        "nativeConversationHeadRef": None,
        "status": "idle",
        "firstRunOrdinal": None,
        "lastRunOrdinal": None,
        "handoffIds": [],
        "forkedFrom": None,
        "pendingBackgroundTasks": [],
        "contextUsage": None,
        "nativeMetadata": None,
        "goal": None,
        "createdAt": created_at,
        "updatedAt": updated_at,
    }

    sv = _next_stream_version(conn, thread_id)

    # 1. thread.created event + projection
    _append_event(
        conn,
        event_id=f"{IMPORT_EVENT_PREFIX}:thread:{thread_id}:created",
        thread_id=thread_id,
        stream_version=sv,
        event_type="thread.created",
        occurred_at=created_at,
        payload=app_thread,
        metadata={"providerInstanceId": "codex"},
    )
    sv += 1

    conn.execute(
        """
        INSERT INTO orchestration_v2_projection_threads (
            thread_id, project_id, title, default_provider, runtime_mode,
            interaction_mode, active_provider_thread_id, created_at, updated_at,
            archived_at, deleted_at, payload_json, provider_instance_id
        )
        VALUES (?, ?, ?, 'codex', ?, ?, ?, ?, ?, NULL, NULL, ?, 'codex')
        """,
        (
            thread_id,
            project.project_id,
            title,
            DEFAULT_RUNTIME_MODE,
            DEFAULT_INTERACTION_MODE,
            provider_thread_id,
            created_at,
            updated_at,
            json.dumps(app_thread, ensure_ascii=False, separators=(",", ":")),
        ),
    )

    ordinal = 1

    # 2. Optional history-truncated notice (exact T3 format)
    if parsed.omitted_count > 0:
        notice_id = f"{IMPORT_EVENT_PREFIX}:history-truncated:{thread_id}"
        unit = "message was" if parsed.omitted_count == 1 else "messages were"
        notice_payload: dict[str, Any] = {
            "id": notice_id,
            "threadId": thread_id,
            "runId": None,
            "nodeId": None,
            "providerThreadId": None,
            "providerTurnId": None,
            "nativeItemRef": None,
            "parentItemId": None,
            "ordinal": ordinal,
            "status": "completed",
            "title": None,
            "startedAt": created_at,
            "completedAt": created_at,
            "updatedAt": created_at,
            "type": "system_notice",
            "message": (
                f"{parsed.omitted_count} earlier {unit} not imported. "
                "This thread keeps the first user message and the most recent history. "
                "The original session transcript is unchanged."
            ),
        }
        _append_event(
            conn,
            event_id=notice_id,
            thread_id=thread_id,
            stream_version=sv,
            event_type="turn-item.updated",
            occurred_at=created_at,
            payload=notice_payload,
            metadata={},
        )
        sv += 1
        conn.execute(
            """
            INSERT INTO orchestration_v2_turn_item_positions (thread_id, turn_item_id, ordinal)
            VALUES (?, ?, ?)
            """,
            (thread_id, notice_id, ordinal),
        )
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_turn_items (
                turn_item_id, thread_id, run_id, node_id,
                provider_thread_id, provider_turn_id, parent_item_id,
                ordinal, type, status, updated_at, payload_json, inline_work_output
            )
            VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, ?, 'system_notice', 'completed', ?, ?, 0)
            """,
            (
                notice_id,
                thread_id,
                ordinal,
                created_at,
                json.dumps(notice_payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        ordinal += 1

    # 3. Messages + turn items
    for idx, msg in enumerate(action.new_messages):
        sv = _insert_message_and_turn_item(
            conn,
            thread_id=thread_id,
            suffix_idx=idx,
            ordinal=ordinal,
            stream_version=sv,
            msg=msg,
        )
        ordinal += 1

    # 4. provider-thread.updated event + projection
    _append_event(
        conn,
        event_id=f"{IMPORT_EVENT_PREFIX}:provider-thread:{provider_thread_id}",
        thread_id=thread_id,
        stream_version=sv,
        event_type="provider-thread.updated",
        occurred_at=updated_at,
        payload=provider_thread,
        metadata={"driver": "codex", "providerInstanceId": "codex"},
    )
    conn.execute(
        """
        INSERT INTO orchestration_v2_projection_provider_threads (
            provider_thread_id, thread_id, owner_node_id, provider,
            provider_session_id, status, first_run_ordinal, last_run_ordinal,
            updated_at, payload_json, driver, provider_instance_id
        )
        VALUES (?, ?, NULL, 'codex', NULL, 'idle', NULL, NULL, ?, ?, 'codex', 'codex')
        """,
        (
            provider_thread_id,
            thread_id,
            updated_at,
            json.dumps(provider_thread, ensure_ascii=False, separators=(",", ":")),
        ),
    )

    # 5. provider_session_runtime
    source_entry = {
        "provider": "codex",
        "providerInstanceId": "codex",
        "providerSessionId": sid,
        **parsed.identity,
    }
    _upsert_imported_transcript_runtime(
        conn,
        thread_id=thread_id,
        session_id=sid,
        cwd=parsed.cwd,
        last_seen_at=updated_at,
        source_entry=source_entry,
    )


def apply_merge_session(conn: sqlite3.Connection, action: SyncAction) -> None:
    assert action.parsed is not None
    parsed = action.parsed
    thread_id = action.thread_id
    sid = parsed.session_id

    t_row = conn.execute(
        """
        SELECT title, updated_at, payload_json, provider_instance_id
        FROM orchestration_v2_projection_threads
        WHERE thread_id = ?
        """,
        (thread_id,),
    ).fetchone()
    if t_row is None:
        return

    sv = _next_stream_version(conn, thread_id)

    # Determine next suffix index for message_id
    prefix = f"{thread_id}:"
    existing_ids = conn.execute(
        "SELECT message_id FROM orchestration_v2_projection_messages WHERE thread_id = ? AND message_id LIKE ?",
        (thread_id, f"{prefix}%"),
    ).fetchall()
    max_suffix = -1
    for (mid,) in existing_ids:
        tail = mid[len(prefix) :]
        if tail.isdigit():
            max_suffix = max(max_suffix, int(tail))
    next_suffix = max_suffix + 1

    # Determine next ordinal from MAX(ordinal) + 1 across positions & turn_items
    pos_max = conn.execute(
        "SELECT MAX(ordinal) FROM orchestration_v2_turn_item_positions WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()[0]
    ti_max = conn.execute(
        "SELECT MAX(ordinal) FROM orchestration_v2_projection_turn_items WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()[0]
    cur_max_ord = max(pos_max or 0, ti_max or 0)
    next_ordinal = cur_max_ord + 1

    for msg in action.new_messages:
        sv = _insert_message_and_turn_item(
            conn,
            thread_id=thread_id,
            suffix_idx=next_suffix,
            ordinal=next_ordinal,
            stream_version=sv,
            msg=msg,
        )
        next_suffix += 1
        next_ordinal += 1

    # Update thread metadata (updated_at, and title if session_index has a newer title)
    thread_payload = json.loads(t_row["payload_json"])
    latest_msg_ts = max((m.created_at for m in action.new_messages), default="")
    new_updated_at = max(str(t_row["updated_at"] or ""), parsed.updated_at, latest_msg_ts)
    new_title = parsed.index_title if (parsed.index_title and parsed.index_title != t_row["title"]) else t_row["title"]

    metadata_changed = (
        bool(action.new_messages)
        or new_title != t_row["title"]
        or new_updated_at != t_row["updated_at"]
    )
    if metadata_changed:
        thread_payload["title"] = new_title
        thread_payload["updatedAt"] = new_updated_at
        now_iso = utc_now_iso()
        meta_event_id = f"{IMPORT_EVENT_PREFIX}:sync-meta:{thread_id}:{sv}"
        _append_event(
            conn,
            event_id=meta_event_id,
            thread_id=thread_id,
            stream_version=sv,
            event_type="thread.metadata-updated",
            occurred_at=now_iso,
            payload=thread_payload,
            metadata={"providerInstanceId": t_row["provider_instance_id"] or "codex"},
        )
        conn.execute(
            """
            UPDATE orchestration_v2_projection_threads
            SET title = ?, updated_at = ?, payload_json = ?
            WHERE thread_id = ?
            """,
            (
                new_title,
                new_updated_at,
                json.dumps(thread_payload, ensure_ascii=False, separators=(",", ":")),
                thread_id,
            ),
        )

    # Update codex provider_thread updated_at if present
    pt_row = conn.execute(
        """
        SELECT provider_thread_id, updated_at, payload_json
        FROM orchestration_v2_projection_provider_threads
        WHERE thread_id = ? AND provider = 'codex'
        LIMIT 1
        """,
        (thread_id,),
    ).fetchone()
    if pt_row is not None:
        pt_payload = json.loads(pt_row["payload_json"])
        pt_updated = max(str(pt_row["updated_at"] or ""), parsed.updated_at, latest_msg_ts)
        if pt_updated != pt_row["updated_at"]:
            pt_payload["updatedAt"] = pt_updated
            conn.execute(
                """
                UPDATE orchestration_v2_projection_provider_threads
                SET updated_at = ?, payload_json = ?
                WHERE provider_thread_id = ?
                """,
                (
                    pt_updated,
                    json.dumps(pt_payload, ensure_ascii=False, separators=(",", ":")),
                    pt_row["provider_thread_id"],
                ),
            )

    # Update provider_session_runtime importedTranscripts
    source_entry = {
        "provider": "codex",
        "providerInstanceId": "codex",
        "providerSessionId": sid,
        **parsed.identity,
    }
    _upsert_imported_transcript_runtime(
        conn,
        thread_id=thread_id,
        session_id=sid,
        cwd=parsed.cwd,
        last_seen_at=new_updated_at,
        source_entry=source_entry,
    )


def update_projection_metadata(conn: sqlite3.Connection) -> int:
    max_seq = conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM orchestration_events").fetchone()[0]
    now_iso = utc_now_iso()
    conn.execute(
        """
        INSERT INTO orchestration_v2_projection_metadata (projection_name, schema_version, last_sequence, updated_at)
        VALUES ('thread-projections', 2, ?, ?)
        ON CONFLICT(projection_name) DO UPDATE SET
            last_sequence = excluded.last_sequence,
            updated_at = excluded.updated_at
        """,
        (max_seq, now_iso),
    )
    return int(max_seq)


def backup_sqlite_db(db_path: pathlib.Path, backup_dir: pathlib.Path, keep: int = MAX_BACKUPS_DEFAULT) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_path.stem}-{ts}{db_path.suffix}"
    counter = 1
    while dest.exists():
        dest = backup_dir / f"{db_path.stem}-{ts}-{counter}{db_path.suffix}"
        counter += 1

    # Use SQLite online backup API so WAL frames are consistently merged into the backup snapshot
    src_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15.0)
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    # Rotate old backups
    backups = sorted(
        backup_dir.glob(f"{db_path.stem}-*{db_path.suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def is_t3_running(exe_name: str = "T3 Code (Beta).exe") -> list[int]:
    if sys.platform != "win32":
        return []
    proc = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or "INFO:" in line:
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == exe_name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def stop_t3_gracefully(exe_name: str = "T3 Code (Beta).exe", timeout_sec: float = 12.0) -> bool:
    pids = is_t3_running(exe_name)
    if not pids:
        return True
    # Graceful WM_CLOSE first
    subprocess.run(["taskkill", "/IM", exe_name], capture_output=True, check=False)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_t3_running(exe_name):
            return True
        time.sleep(0.5)
    # Escalate to /T /F if still lingering
    subprocess.run(["taskkill", "/IM", exe_name, "/T", "/F"], capture_output=True, check=False)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not is_t3_running(exe_name):
            return True
        time.sleep(0.3)
    return not is_t3_running(exe_name)


def wait_db_writable(db_path: pathlib.Path, timeout_sec: float = 15.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            conn = sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
                return True
            finally:
                conn.close()
        except sqlite3.OperationalError:
            time.sleep(0.5)
    return False


def resolve_t3_executable(value: str | pathlib.Path) -> pathlib.Path | None:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    candidate = pathlib.Path(expanded)
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(expanded)
    return pathlib.Path(found).resolve() if found else None


def launch_t3(exe_path: pathlib.Path) -> bool:
    if not exe_path.exists():
        return False
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    subprocess.Popen(
        [str(exe_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )
    return True


def execute_sync(
    db_path: pathlib.Path,
    actions: list[SyncAction],
    backup_dir: pathlib.Path | None = DEFAULT_BACKUP_DIR,
    keep_backups: int = MAX_BACKUPS_DEFAULT,
    no_backup: bool = False,
) -> dict[str, Any]:
    writable = [a for a in actions if a.kind in ("new", "merge", "metadata_only")]
    if not writable:
        return {
            "backup_path": None,
            "new_sessions": 0,
            "merged_sessions": 0,
            "metadata_updated": 0,
            "added_messages": 0,
            "last_sequence": None,
        }

    backup_path: pathlib.Path | None = None
    if not no_backup and backup_dir is not None:
        backup_path = backup_sqlite_db(db_path, backup_dir, keep=keep_backups)

    conn = sqlite3.connect(str(db_path), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("BEGIN IMMEDIATE")
        new_sessions = 0
        merged_sessions = 0
        metadata_updated = 0
        added_messages = 0

        for action in writable:
            if action.kind == "new":
                apply_new_session(conn, action)
                new_sessions += 1
                added_messages += len(action.new_messages)
            elif action.kind == "merge":
                apply_merge_session(conn, action)
                merged_sessions += 1
                added_messages += len(action.new_messages)
            elif action.kind == "metadata_only":
                apply_merge_session(conn, action)
                metadata_updated += 1

        last_seq = update_projection_metadata(conn)
        conn.execute("COMMIT")
        return {
            "backup_path": str(backup_path) if backup_path else None,
            "new_sessions": new_sessions,
            "merged_sessions": merged_sessions,
            "metadata_updated": metadata_updated,
            "added_messages": added_messages,
            "last_sequence": last_seq,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def format_actions_report(actions: list[SyncAction], verbose: bool = False) -> str:
    by_kind: dict[str, list[SyncAction]] = collections.defaultdict(list)
    for a in actions:
        by_kind[a.kind].append(a)

    lines: list[str] = []
    new_list = by_kind["new"]
    merge_list = by_kind["merge"]
    meta_list = by_kind["metadata_only"]
    up_list = by_kind["up_to_date"]
    del_list = by_kind["skipped_deleted"]
    unm_list = by_kind["skipped_unmatched"]

    total_new_msgs = sum(len(a.new_messages) for a in new_list)
    total_merge_msgs = sum(len(a.new_messages) for a in merge_list)

    lines.append(
        f"摘要: 待新增会话 {len(new_list)} 个 ({total_new_msgs} 条消息) | "
        f"待增量合并 {len(merge_list)} 个 ({total_merge_msgs} 条新增消息) | "
        f"仅元数据更新 {len(meta_list)} 个 | "
        f"已最新 {len(up_list)} 个 | "
        f"已删除跳过 {len(del_list)} 个 | "
        f"未匹配项目 {len(unm_list)} 个"
    )

    if new_list:
        lines.append("\n[待新增会话 (NEW)]")
        for a in new_list:
            proj_name = a.project.title if a.project else "-"
            trunc = f", 省略早期 {a.parsed.omitted_count} 条" if a.parsed and a.parsed.omitted_count else ""
            lines.append(
                f"  + {a.session_id} | 项目: {proj_name} | 消息: {len(a.new_messages)}{trunc} | 标题: {a.title}"
            )

    if merge_list:
        lines.append("\n[待增量合并会话 (MERGE)]")
        for a in merge_list:
            proj_name = a.project.title if a.project else "-"
            fork_tag = " [T3已续聊/分叉]" if a.existing_has_runs else ""
            title_tag = " [标题更新]" if a.title_changed else ""
            lines.append(
                f"  ~ {a.session_id} | 项目: {proj_name} | 已有 {a.existing_msg_count} 条 -> +{len(a.new_messages)} 条{fork_tag}{title_tag} | 标题: {a.title}"
            )

    if meta_list and verbose:
        lines.append("\n[仅元数据/文件状态同步 (METADATA)]")
        for a in meta_list:
            proj_name = a.project.title if a.project else "-"
            lines.append(f"  . {a.session_id} | 项目: {proj_name} | {a.reason} | 标题: {a.title}")

    if unm_list:
        lines.append("\n[未匹配 T3 项目的 Codex 会话 (SKIPPED)]")
        for a in unm_list[:20]:
            cwd_str = a.parsed.cwd if a.parsed else ""
            lines.append(f"  ? {a.session_id} | cwd: {cwd_str} | 标题: {a.title}")
        if len(unm_list) > 20:
            lines.append(f"  ... 其余 {len(unm_list) - 20} 个未匹配会话已省略")

    if del_list and verbose:
        lines.append("\n[已在 T3 删除的会话 (SKIPPED)]")
        for a in del_list:
            lines.append(f"  x {a.session_id} | 标题: {a.title}")

    return "\n".join(lines)


def actions_to_json(actions: list[SyncAction]) -> list[dict[str, Any]]:
    out = []
    for a in actions:
        out.append(
            {
                "kind": a.kind,
                "session_id": a.session_id,
                "thread_id": a.thread_id,
                "file_path": str(a.file_path),
                "project": a.project.title if a.project else None,
                "project_id": a.project.project_id if a.project else None,
                "title": a.title,
                "new_message_count": len(a.new_messages),
                "existing_msg_count": a.existing_msg_count,
                "existing_has_runs": a.existing_has_runs,
                "title_changed": a.title_changed,
                "reason": a.reason,
            }
        )
    return out


def cmd_status_or_sync(args: argparse.Namespace, dry_run: bool) -> int:
    db_path = pathlib.Path(args.db)
    codex_home = pathlib.Path(args.codex_home)
    if not db_path.exists():
        print(f"错误: 找不到 T3 Code 数据库: {db_path}", file=sys.stderr)
        return 2

    max_init = None if getattr(args, "no_truncate", False) else MAX_IMPORTED_MESSAGES

    # Read-only scan first
    ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    ro_conn.row_factory = sqlite3.Row
    try:
        actions = plan_sync(
            ro_conn,
            codex_home=codex_home,
            session_filter=args.session,
            project_filter=args.project,
            force=args.force,
            include_zst=args.include_zst,
            max_initial_messages=max_init,
        )
    finally:
        ro_conn.close()

    if args.json:
        payload: dict[str, Any] = {"dry_run": dry_run, "actions": actions_to_json(actions)}
        if dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
    else:
        print(f"数据库: {db_path}")
        print(f"Codex 目录: {codex_home}")
        print(format_actions_report(actions, verbose=args.verbose))

    if dry_run:
        return 0

    writable = [a for a in actions if a.kind in ("new", "merge", "metadata_only")]
    if not writable:
        if not args.json:
            print("\n没有需要写入的变化，无需修改数据库。")
        else:
            print(json.dumps({"dry_run": False, "result": "no_changes"}, ensure_ascii=False, indent=2))
        return 0

    t3_exe = pathlib.Path(args.t3_exe)
    exe_name = t3_exe.name
    running_pids = is_t3_running(exe_name)

    restarted = False
    if running_pids:
        if args.restart:
            resolved_exe = resolve_t3_executable(t3_exe)
            if resolved_exe is None:
                print(
                    "错误: 找不到 T3 Code 可执行文件；请通过 --t3-exe 或 T3_CODE_EXE 指定，"
                    "或将程序所在目录加入 PATH。尚未关闭 T3 或写入数据库。",
                    file=sys.stderr,
                )
                return 2
            t3_exe = resolved_exe
            if not args.json:
                print(f"\n检测到 T3 Code 正在运行 (PID: {running_pids})，正在安全关闭...")
            if not stop_t3_gracefully(exe_name):
                print("错误: 无法关闭 T3 Code 进程，请手动关闭后重试。", file=sys.stderr)
                return 3
            if not wait_db_writable(db_path):
                print("错误: 等待 statev2.sqlite 解锁超时。", file=sys.stderr)
                return 4
            restarted = True
        elif not args.allow_live:
            print(
                f"\n安全拦截: 检测到 {exe_name} 正在运行 (PID: {running_pids})。\n"
                "为避免与运行中的 T3 服务端并发写入冲突（且界面需要重启才会加载新导入线程），请使用:\n"
                "  t3-codex-sync sync --restart\n"
                "或先手动关闭 T3 Code 后再运行 `t3-codex-sync sync`（如确需强制热写，可加 --allow-live）。",
                file=sys.stderr,
            )
            return 5

    # Re-plan right after T3 stops (so we capture any last-second events T3 flushed on exit)
    if restarted:
        ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        ro_conn.row_factory = sqlite3.Row
        try:
            actions = plan_sync(
                ro_conn,
                codex_home=codex_home,
                session_filter=args.session,
                project_filter=args.project,
                force=args.force,
                include_zst=args.include_zst,
                max_initial_messages=max_init,
            )
        finally:
            ro_conn.close()

    res = execute_sync(
        db_path=db_path,
        actions=actions,
        backup_dir=pathlib.Path(args.backup_dir),
        keep_backups=args.keep_backups,
        no_backup=args.no_backup,
    )

    if restarted:
        if not args.json:
            print(f"正在重新启动 {t3_exe} ...")
        launch_t3(t3_exe)

    if args.json:
        print(json.dumps({"dry_run": False, "restarted": restarted, "result": res}, ensure_ascii=False, indent=2))
    else:
        print("\n同步完成:")
        if res["backup_path"]:
            print(f"  备份文件: {res['backup_path']}")
        print(
            f"  新增会话: {res['new_sessions']} | 合并会话: {res['merged_sessions']} | "
            f"仅元数据更新: {res['metadata_updated']} | 写入消息: {res['added_messages']} | "
            f"最新事件序号: {res['last_sequence']}"
        )
        if not restarted and running_pids:
            print("  提示: 已在热写模式下完成，请重启 T3 Code 以刷新界面缓存。")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    db_path = pathlib.Path(args.db)
    codex_home = pathlib.Path(args.codex_home)
    interval = max(1.0, float(args.interval))
    print(f"[watch] 开始监听 {codex_home / 'sessions'} (每 {interval:.1f}s 检查一次，Ctrl+C 退出)")
    if args.auto_restart:
        print("[watch] 模式: 发现新消息后自动安全重启 T3 并同步 (--auto-restart)")
    elif args.auto_sync:
        print("[watch] 模式: 发现新消息后在 T3 未运行时自动同步 (--auto-sync)")
    else:
        print("[watch] 模式: 只读提醒（发现变动时提示执行 `t3-codex-sync sync --restart`）")

    last_sig: dict[str, tuple[int, int]] = {}
    first_round = True
    try:
        while True:
            rollouts = discover_codex_rollouts(codex_home, include_zst=False)
            cur_sig = {str(fp): (st.st_size, st.st_mtime_ns) for fp, st in rollouts}
            idx_path = codex_home / "session_index.jsonl"
            if idx_path.exists():
                ist = os.stat(idx_path)
                cur_sig[str(idx_path)] = (ist.st_size, ist.st_mtime_ns)

            if first_round or cur_sig != last_sig:
                last_sig = cur_sig
                ro_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
                ro_conn.row_factory = sqlite3.Row
                try:
                    actions = plan_sync(
                        ro_conn,
                        codex_home=codex_home,
                        session_filter=args.session,
                        project_filter=args.project,
                        force=False,
                        include_zst=False,
                    )
                finally:
                    ro_conn.close()

                pending = [a for a in actions if a.kind in ("new", "merge")]
                now_str = datetime.datetime.now().strftime("%H:%M:%S")
                if pending:
                    print(f"\n[{now_str}] 检测到待同步 Codex 会话变化:")
                    print(format_actions_report(pending, verbose=False))
                    if args.auto_restart:
                        sync_args = argparse.Namespace(
                            db=str(db_path),
                            codex_home=str(codex_home),
                            t3_exe=args.t3_exe,
                            backup_dir=args.backup_dir,
                            keep_backups=args.keep_backups,
                            no_backup=False,
                            session=args.session,
                            project=args.project,
                            force=False,
                            include_zst=False,
                            no_truncate=False,
                            restart=True,
                            allow_live=False,
                            verbose=False,
                            json=False,
                        )
                        cmd_status_or_sync(sync_args, dry_run=False)
                    elif args.auto_sync:
                        exe_name = pathlib.Path(args.t3_exe).name
                        if is_t3_running(exe_name):
                            print(f"[{now_str}] T3 Code 正在运行，暂跳过自动写入；可手动运行 `t3-codex-sync sync --restart`。")
                        else:
                            res = execute_sync(
                                db_path=db_path,
                                actions=actions,
                                backup_dir=pathlib.Path(args.backup_dir),
                                keep_backups=args.keep_backups,
                            )
                            print(
                                f"[{now_str}] 自动同步完成: 新增 {res['new_sessions']} 会话, "
                                f"合并 {res['merged_sessions']} 会话, +{res['added_messages']} 消息"
                            )
                    else:
                        print(f"[{now_str}] 提示: 运行 `t3-codex-sync sync --restart` 立即同步到 T3 Code。")
                elif first_round:
                    print(f"[{now_str}] 当前所有已匹配项目的 Codex 会话均为最新。")
                first_round = False

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[watch] 已停止监听。")
        return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    codex_home = pathlib.Path(args.codex_home)
    thread_names = read_codex_thread_names(codex_home)
    project_roots = read_codex_project_roots(codex_home)
    rollout_lookup = build_rollout_lookup(codex_home)
    rollouts = discover_codex_rollouts(codex_home, include_zst=args.include_zst)

    matches = [
        (fp, st)
        for fp, st in rollouts
        if args.session.lower() in fp.name.lower()
    ]
    if not matches:
        print(f"未找到匹配 '{args.session}' 的 Codex rollout 文件。", file=sys.stderr)
        return 1

    fp, st = matches[0]
    parsed, status = parse_codex_file(
        fp, st, thread_names, project_roots, rollout_lookup, max_initial_messages=None
    )
    if parsed is None:
        print(f"解析失败 ({status}): {fp}", file=sys.stderr)
        return 1

    print(f"会话 ID: {parsed.session_id}")
    print(f"标题: {parsed.title}")
    print(f"文件: {parsed.file_path} ({parsed.identity['size']:,} bytes)")
    print(f"工作目录: {parsed.cwd}")
    print(f"模型: {parsed.model or '-'}")
    print(f"消息总数: {len(parsed.all_messages)} | 创建: {parsed.created_at} | 更新: {parsed.updated_at}")

    tail_n = args.tail
    msgs = parsed.all_messages[-tail_n:] if tail_n > 0 else parsed.all_messages
    start_idx = len(parsed.all_messages) - len(msgs)
    for i, m in enumerate(msgs, start=start_idx):
        preview = m.text if args.full else (m.text[:240] + ("..." if len(m.text) > 240 else ""))
        print(f"\n--- [{i:03d}] {m.role.upper()} @ {m.created_at} ---")
        print(preview)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(DEFAULT_T3_DB), help=f"T3 Code statev2.sqlite 路径 (默认: {DEFAULT_T3_DB})")
    common.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME), help=f"Codex 目录 (默认: {DEFAULT_CODEX_HOME})")
    common.add_argument("--session", default=None, help="只处理包含该子串的会话 ID")
    common.add_argument("--project", default=None, help="只处理名称或路径包含该子串的 T3 项目")
    common.add_argument("--include-zst", action="store_true", help="同时扫描旧的 .jsonl.zst 压缩归档")
    common.add_argument("-v", "--verbose", action="store_true", help="显示更多细节（含仅元数据变更与已删除跳过）")
    common.add_argument("--json", action="store_true", help="输出 JSON 格式")

    parser = argparse.ArgumentParser(
        prog="t3-codex-sync",
        description="T3 Code Codex 会话及时刷新与智能去重增量合并 CLI 工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", parents=[common], help="只读预览待新增会话与待合并消息")
    p_status.add_argument("--force", action="store_true", help="忽略文件大小/mtime 缓存，强制重新解析比对")

    p_sync = sub.add_parser("sync", parents=[common], help="执行备份并同步/增量合并 Codex 会话到 statev2.sqlite")
    p_sync.add_argument("--dry-run", action="store_true", help="只读预览，不写入数据库")
    p_sync.add_argument("--restart", action="store_true", help="安全关闭 T3 Code -> 等待解锁 -> 原子同步 -> 自动重启 T3 Code")
    p_sync.add_argument("--allow-live", action="store_true", help="允许在 T3 Code 运行时直接写入（不推荐；仍需重启 T3 才能刷新界面）")
    p_sync.add_argument("--force", action="store_true", help="忽略文件大小/mtime 缓存，强制重新解析比对")
    p_sync.add_argument("--no-truncate", action="store_true", help="新导入会话不按 T3 原生 200 条截断，保留全量消息")
    p_sync.add_argument("--t3-exe", default=str(DEFAULT_T3_EXE), help="T3 Code 可执行文件路径（默认取 T3_CODE_EXE，否则查找 PATH 中的 T3 Code (Beta).exe）")
    p_sync.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help=f"滚动备份目录 (默认: {DEFAULT_BACKUP_DIR})")
    p_sync.add_argument("--keep-backups", type=int, default=MAX_BACKUPS_DEFAULT, help="保留最近备份份数 (默认: 10)")
    p_sync.add_argument("--no-backup", action="store_true", help="跳过写入前备份（仅测试用）")

    p_watch = sub.add_parser("watch", parents=[common], help="监听 ~/.codex/sessions 变动并实时提示或按需同步")
    p_watch.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒数 (默认: 5)")
    p_watch.add_argument("--auto-sync", action="store_true", help="当 T3 未运行时自动同步")
    p_watch.add_argument("--auto-restart", action="store_true", help="检测到新消息时自动重启 T3 并同步")
    p_watch.add_argument("--t3-exe", default=str(DEFAULT_T3_EXE), help="T3 Code 可执行文件路径（默认取 T3_CODE_EXE，否则查找 PATH 中的 T3 Code (Beta).exe）")
    p_watch.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR), help=f"滚动备份目录 (默认: {DEFAULT_BACKUP_DIR})")
    p_watch.add_argument("--keep-backups", type=int, default=MAX_BACKUPS_DEFAULT, help="保留最近备份份数 (默认: 10)")

    p_inspect = sub.add_parser("inspect", help="只读解析并查看某条 Codex 会话的最新消息")
    p_inspect.add_argument("session", help="会话 ID 或前缀/子串")
    p_inspect.add_argument("--codex-home", default=str(DEFAULT_CODEX_HOME))
    p_inspect.add_argument("--tail", type=int, default=10, help="显示最后 N 条消息 (0 表示全部，默认: 10)")
    p_inspect.add_argument("--full", action="store_true", help="输出完整正文，不截断")
    p_inspect.add_argument("--include-zst", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status_or_sync(args, dry_run=True)
    if args.command == "sync":
        return cmd_status_or_sync(args, dry_run=bool(args.dry_run))
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "inspect":
        return cmd_inspect(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
