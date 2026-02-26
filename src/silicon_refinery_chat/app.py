"""Local-first Apple Foundation Models chat app built with Toga + Briefcase.

Highlights:
- realtime streaming responses
- sqlite-backed multi-chat persistence
- auto-generated unique chat names based on first query
- steering controls with automatic mid-stream interjection + rerun
- Codex-style rolling context compaction
- familiar slash commands: /help, /new, /clear, /export
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import functools
import json
import os
import re
import shlex
import sqlite3
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import apple_fm_sdk as fm
import toga
from toga.style import Pack
from toga.style.pack import COLUMN, HIDDEN, ROW, VISIBLE

DB_FILENAME = "chat_history.sqlite3"
STREAM_UI_MIN_INTERVAL_SECONDS = 0.022
STREAM_UI_MAX_INTERVAL_SECONDS = 0.065
STREAM_UI_MIN_CHARS_DELTA = 8
STREAM_UI_BREAK_CHARS = {".", "!", "?", ":", ";", "\n"}
QUERY_PREVIEW_STEP_CHARS = 12
QUERY_PREVIEW_INTERVAL_SECONDS = 0.008
MAX_STREAM_RESTARTS = 5
STREAM_FIRST_CHUNK_TIMEOUT_SECONDS = 25.0
STREAM_CHUNK_IDLE_TIMEOUT_SECONDS = 12.0
STREAM_WORKER_JOIN_TIMEOUT_SECONDS = 0.4
LOADING_SHIMMER_FRAMES = [
    "░░▒▒▓▓▒▒",
    "░▒▒▓▓▒▒░",
    "▒▒▓▓▒▒░░",
    "▒▓▓▒▒░░▒",
    "▓▓▒▒░░▒▒",
    "▓▒▒░░▒▒▓",
]

# Visual design system
FONT_SIZE_TITLE = 16
FONT_SIZE_SECTION = 12
FONT_SIZE_BODY = 11
FONT_SIZE_META = 10
FONT_SIZE_TEXTAREA = 11.5

TEXTAREA_INSET_X = 10.0
TEXTAREA_INSET_Y = 8.0
TEXTAREA_LINE_FRAGMENT_PADDING = 2.0

COLOR_APP_BG = "#0E1218"
COLOR_SIDEBAR_BG = "#133A4CD9"
COLOR_PANEL_BG = "#151C26"
COLOR_STEERING_BG = "#141B25"
COLOR_INPUT_BG = "#0E141D"
COLOR_TEXTAREA_BG = "#1A1E26"
COLOR_ACCENT = "#5E9BFF"
COLOR_ACCENT_SOFT = "#1A2A42"
COLOR_DANGER_SOFT = "#432932"
COLOR_TEXT_PRIMARY = "#F6FAFF"
COLOR_TEXT_SECONDARY = "#D4DEEA"
COLOR_TEXT_MUTED = "#9AA8BC"
COLOR_TAB_IDLE = "#1A2736"
COLOR_TAB_ACTIVE = "#29445F"
SIDEBAR_TITLE_TEXT = "SiliconRefineryChat -Apple Foundation Models"


def configure_theme_for_mode(dark_mode: bool | None) -> None:
    """Set UI palette for light/dark appearance to preserve contrast."""
    global COLOR_ACCENT
    global COLOR_ACCENT_SOFT
    global COLOR_APP_BG
    global COLOR_DANGER_SOFT
    global COLOR_INPUT_BG
    global COLOR_PANEL_BG
    global COLOR_SIDEBAR_BG
    global COLOR_STEERING_BG
    global COLOR_TEXTAREA_BG
    global COLOR_TAB_ACTIVE
    global COLOR_TAB_IDLE
    global COLOR_TEXT_MUTED
    global COLOR_TEXT_PRIMARY
    global COLOR_TEXT_SECONDARY

    if dark_mode is False:
        COLOR_APP_BG = "#131A22"
        COLOR_SIDEBAR_BG = "#17495FD9"
        COLOR_PANEL_BG = "#172131"
        COLOR_STEERING_BG = "#1A2435"
        COLOR_INPUT_BG = "#121A26"
        COLOR_TEXTAREA_BG = "#1E242D"
        COLOR_ACCENT = "#68A8FF"
        COLOR_ACCENT_SOFT = "#1F3550"
        COLOR_DANGER_SOFT = "#4A2F39"
        COLOR_TEXT_PRIMARY = "#F5FAFF"
        COLOR_TEXT_SECONDARY = "#D8E1ED"
        COLOR_TEXT_MUTED = "#A0AFC4"
        COLOR_TAB_IDLE = "#213346"
        COLOR_TAB_ACTIVE = "#335575"
        return

    COLOR_APP_BG = "#0E1218"
    COLOR_SIDEBAR_BG = "#133A4CD9"
    COLOR_PANEL_BG = "#151C26"
    COLOR_STEERING_BG = "#141B25"
    COLOR_INPUT_BG = "#0E141D"
    COLOR_TEXTAREA_BG = "#1A1E26"
    COLOR_ACCENT = "#5E9BFF"
    COLOR_ACCENT_SOFT = "#1A2A42"
    COLOR_DANGER_SOFT = "#432932"
    COLOR_TEXT_PRIMARY = "#F6FAFF"
    COLOR_TEXT_SECONDARY = "#D4DEEA"
    COLOR_TEXT_MUTED = "#9AA8BC"
    COLOR_TAB_IDLE = "#1A2736"
    COLOR_TAB_ACTIVE = "#29445F"


SYSTEM_INSTRUCTIONS = (
    "You are a local-first assistant running entirely on Apple Foundation Models. "
    "Be accurate, practical, and explicit about uncertainty."
)

HELP_TEXT = """Slash Commands
/help                          Show command help
/new                           Start a fresh unsaved chat
/clear                         Alias for /new
/export [jsonl|md] [path]      Export current chat quickly
"""

VALID_TONES = ["Balanced", "Analytical", "Creative", "Executive"]
VALID_DEPTHS = ["Shallow", "Detailed", "Deep"]
VALID_VERBOSITY = ["Short", "Medium", "Long"]
VALID_CITATION_MODES = [
    "No citation requirement",
    "Inline citations + uncertainty",
    "Reference prior context points",
]
TRANSCRIPT_METADATA_ROW_PATTERN = re.compile(r"^(You|Assistant)\s\|[^\n]*$", re.MULTILINE)

T = TypeVar("T")


def utc_now_iso() -> str:
    """Return a stable UTC timestamp string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def slugify_filename(value: str) -> str:
    """Convert title to a filesystem-safe stem."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "chat-export"


def derive_chat_title(query: str) -> str:
    """Generate readable chat title from opening query."""
    words = re.findall(r"[A-Za-z0-9']+", query)
    if not words:
        return "Untitled Chat"

    def normalize_word(word: str) -> str:
        """Title-case words without upper-casing letters after apostrophes."""
        parts = word.split("'")
        first = parts[0]
        normalized_first = first[:1].upper() + first[1:].lower() if first else ""
        if len(parts) == 1:
            return normalized_first
        normalized_rest = [part.lower() for part in parts[1:]]
        return "'".join([normalized_first, *normalized_rest])

    title = " ".join(normalize_word(word) for word in words[:8]).strip()
    if len(title) <= 60:
        return title
    compact = title[:60].rsplit(" ", 1)[0].strip()
    return compact or title[:60]


def normalize_choice(value: str, allowed: list[str], fallback: str) -> str:
    """Normalize value by case-insensitive exact match against allowed values."""
    needle = value.strip().lower()
    for option in allowed:
        if option.lower() == needle:
            return option
    return fallback


def detect_runtime_threading_mode() -> tuple[bool, str]:
    """Detect whether running on a free-threaded Python runtime."""
    probe = getattr(sys, "_is_gil_enabled", None)
    if not callable(probe):
        return False, "standard-gil"
    try:
        gil_enabled = bool(probe())
    except Exception:
        return False, "standard-gil"
    return (not gil_enabled, "free-threaded" if not gil_enabled else "standard-gil")


@dataclass
class SteeringProfile:
    """User-selected steering knobs that shape generation."""

    tone: str = "Balanced"
    depth: str = "Detailed"
    verbosity: str = "Medium"
    citations: str = "No citation requirement"

    def to_prompt_block(self) -> str:
        """Render steering profile for prompt injection."""
        return "\n".join(
            [
                "Steering Profile:",
                f"- Tone: {self.tone}",
                f"- Depth: {self.depth}",
                f"- Verbosity: {self.verbosity}",
                f"- Citation behavior: {self.citations}",
            ]
        )

    def to_dict(self) -> dict[str, str]:
        """Persistable steering representation."""
        return {
            "tone": self.tone,
            "depth": self.depth,
            "verbosity": self.verbosity,
            "citations": self.citations,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SteeringProfile:
        """Load steering profile with safe defaults."""
        return cls(
            tone=normalize_choice(str(data.get("tone", "Balanced")), VALID_TONES, "Balanced"),
            depth=normalize_choice(str(data.get("depth", "Detailed")), VALID_DEPTHS, "Detailed"),
            verbosity=normalize_choice(
                str(data.get("verbosity", "Medium")), VALID_VERBOSITY, "Medium"
            ),
            citations=normalize_choice(
                str(data.get("citations", "No citation requirement")),
                VALID_CITATION_MODES,
                "No citation requirement",
            ),
        )


class ChatStore:
    """Fast sqlite persistence for chats, messages, steering, and summaries."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._tune_pragmas()
        self._init_schema()

    def _tune_pragmas(self) -> None:
        """Tune sqlite for local low-latency usage."""
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA temp_store = MEMORY")

    def close(self) -> None:
        """Close sqlite connection."""
        self.conn.close()

    def _init_schema(self) -> None:
        """Initialize schema and run tiny migrations."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                rolling_summary TEXT NOT NULL DEFAULT '',
                steering_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_created
            ON messages(chat_id, id);
            """
        )

        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(chats)").fetchall()}
        if "steering_json" not in columns:
            self.conn.execute(
                "ALTER TABLE chats ADD COLUMN steering_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "rolling_summary" not in columns:
            self.conn.execute(
                "ALTER TABLE chats ADD COLUMN rolling_summary TEXT NOT NULL DEFAULT ''"
            )
        self.conn.commit()

    def _title_exists(self, title: str, *, exclude_chat_id: int | None = None) -> bool:
        if exclude_chat_id is None:
            row = self.conn.execute(
                "SELECT 1 FROM chats WHERE title = ? LIMIT 1", (title,)
            ).fetchone()
            return row is not None

        row = self.conn.execute(
            "SELECT 1 FROM chats WHERE title = ? AND id != ? LIMIT 1",
            (title, exclude_chat_id),
        ).fetchone()
        return row is not None

    def ensure_unique_title(self, desired_title: str, *, exclude_chat_id: int | None = None) -> str:
        """Ensure chat title uniqueness by appending numeric suffixes."""
        base = desired_title.strip() or "Untitled Chat"
        title = base
        suffix = 2
        while self._title_exists(title, exclude_chat_id=exclude_chat_id):
            title = f"{base} ({suffix})"
            suffix += 1
        return title

    def list_chats(self) -> list[dict[str, Any]]:
        """List chats ordered by recent activity."""
        rows = self.conn.execute(
            """
            SELECT id, title, updated_at
            FROM chats
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def create_chat(self, first_query: str, steering: SteeringProfile) -> tuple[int, str]:
        """Create chat with unique query-derived title."""
        title = self.ensure_unique_title(derive_chat_title(first_query))
        now = utc_now_iso()
        cur = self.conn.execute(
            """
            INSERT INTO chats (title, created_at, updated_at, steering_json)
            VALUES (?, ?, ?, ?)
            """,
            (title, now, now, json.dumps(steering.to_dict(), ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid), title

    def delete_chat(self, chat_id: int) -> None:
        """Delete chat and all messages."""
        self.conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        self.conn.commit()

    def chat_title(self, chat_id: int) -> str | None:
        """Get chat title by id."""
        row = self.conn.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return None if row is None else str(row["title"])

    def load_messages(self, chat_id: int) -> list[dict[str, Any]]:
        """Load message history for a chat."""
        rows = self.conn.execute(
            """
            SELECT role, content, attachments_json, created_at
            FROM messages
            WHERE chat_id = ?
            ORDER BY id ASC
            """,
            (chat_id,),
        ).fetchall()

        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                attachments = json.loads(row["attachments_json"])
                if not isinstance(attachments, list):
                    attachments = []
            except json.JSONDecodeError:
                attachments = []

            messages.append(
                {
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "attachments": attachments,
                    "created_at": str(row["created_at"]),
                }
            )
        return messages

    def add_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a chat message and touch updated_at."""
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO messages (chat_id, role, content, attachments_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, role, content, json.dumps(attachments or [], ensure_ascii=False), now),
        )
        self.conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = ?",
            (now, chat_id),
        )
        self.conn.commit()

    def get_rolling_summary(self, chat_id: int) -> str:
        """Get chat rolling summary."""
        row = self.conn.execute(
            "SELECT rolling_summary FROM chats WHERE id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return ""
        return str(row["rolling_summary"])

    def set_rolling_summary(self, chat_id: int, summary: str) -> None:
        """Persist chat rolling summary."""
        self.conn.execute(
            "UPDATE chats SET rolling_summary = ?, updated_at = ? WHERE id = ?",
            (summary, utc_now_iso(), chat_id),
        )
        self.conn.commit()

    def get_steering(self, chat_id: int) -> SteeringProfile:
        """Load steering profile for chat."""
        row = self.conn.execute(
            "SELECT steering_json FROM chats WHERE id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return SteeringProfile()

        raw = str(row["steering_json"] or "{}")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        return SteeringProfile.from_dict(parsed)

    def set_steering(self, chat_id: int, steering: SteeringProfile) -> None:
        """Persist steering profile for chat."""
        self.conn.execute(
            "UPDATE chats SET steering_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(steering.to_dict(), ensure_ascii=False), utc_now_iso(), chat_id),
        )
        self.conn.commit()

    def export_jsonl(self, chat_id: int, target: Path) -> None:
        """Export chat to JSONL."""
        title = self.chat_title(chat_id) or "Untitled Chat"
        messages = self.load_messages(chat_id)
        target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "chat_metadata",
                        "chat_id": chat_id,
                        "title": title,
                        "exported_at": utc_now_iso(),
                        "steering": self.get_steering(chat_id).to_dict(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            for message in messages:
                record = {
                    "type": "message",
                    "role": message["role"],
                    "created_at": message["created_at"],
                    "content": message["content"],
                    "attachments": message["attachments"],
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def export_markdown(self, chat_id: int, target: Path) -> None:
        """Export chat to Markdown transcript."""
        title = self.chat_title(chat_id) or "Untitled Chat"
        messages = self.load_messages(chat_id)
        target.parent.mkdir(parents=True, exist_ok=True)

        lines = [f"# {title}", "", f"Exported: {utc_now_iso()}", ""]
        for message in messages:
            role = "User" if message["role"] == "user" else "Assistant"
            lines.append(f"## {role} ({message['created_at']})")
            lines.append("")
            lines.append(message["content"])
            lines.append("")

        target.write_text("\n".join(lines), encoding="utf-8")


class CodexStyleCompactor:
    """Recency + salience + action-ledger compaction strategy."""

    def __init__(self, base_budget_chars: int = 18_000, recent_turns: int = 8):
        self.base_budget_chars = base_budget_chars
        self.recent_turns = recent_turns

    def prepare_context(
        self,
        messages: list[dict[str, Any]],
        rolling_summary: str,
        steering: SteeringProfile,
    ) -> tuple[str, str, bool]:
        """Return context block, updated summary, and compaction flag."""
        budget = self._budget_for_steering(steering)
        full_context = self._render_messages(messages)
        if len(full_context) <= budget:
            return full_context, rolling_summary, False

        recent = messages[-self.recent_turns :]
        older = messages[: -self.recent_turns]
        extracted = self._extract_salient_points(older)
        merged = self._merge_summaries(rolling_summary, extracted)

        compacted = "\n".join(
            [
                "Compacted Memory (Codex-style)",
                merged or "(no prior summary)",
                "",
                "Recent Turns",
                self._render_messages(recent),
            ]
        )

        if len(compacted) > budget:
            compacted = compacted[-budget:]
        return compacted, merged, True

    def _budget_for_steering(self, steering: SteeringProfile) -> int:
        budget = self.base_budget_chars
        if steering.depth == "Deep":
            budget += 3_000
        if steering.verbosity == "Short":
            budget -= 2_000
        if steering.verbosity == "Long":
            budget += 2_000
        return max(8_000, budget)

    def _render_messages(self, messages: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for message in messages:
            speaker = "USER" if message.get("role") == "user" else "ASSISTANT"
            lines.append(f"{speaker} [{message.get('created_at', '')}]")

            lines.append(str(message.get("content", "")).strip())
            lines.append("")

        return "\n".join(lines).strip()

    def _extract_salient_points(self, messages: list[dict[str, Any]]) -> str:
        candidates: list[tuple[float, str]] = []
        keywords = {
            "must",
            "need",
            "required",
            "cannot",
            "local",
            "offline",
            "privacy",
            "decision",
            "decide",
            "todo",
            "next",
            "action",
            "export",
            "resume",
        }

        for message in messages:
            role = "User" if message.get("role") == "user" else "Assistant"
            text_blob = str(message.get("content", ""))
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", text_blob):
                cleaned = sentence.strip()
                if len(cleaned) < 18:
                    continue

                lowered = cleaned.lower()
                score = 1.0
                if role == "User":
                    score += 0.4
                score += 0.5 * sum(1 for token in keywords if token in lowered)
                score += min(len(cleaned) / 240.0, 1.0)
                candidates.append((score, f"{role}: {cleaned}"))

        if not candidates:
            return ""

        picked: list[str] = []
        seen: set[str] = set()
        for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
            key = re.sub(r"\W+", "", candidate.lower())[:160]
            if key in seen:
                continue
            seen.add(key)
            picked.append(candidate)
            if len(picked) >= 12:
                break

        highlights: list[str] = []
        decisions: list[str] = []
        constraints: list[str] = []
        actions: list[str] = []

        for item in picked:
            lowered = item.lower()
            if any(token in lowered for token in {"decide", "decision", "chosen", "prefer"}):
                decisions.append(item)
            elif any(token in lowered for token in {"next", "todo", "action", "follow up"}):
                actions.append(item)
            elif any(
                token in lowered for token in {"must", "cannot", "required", "offline", "local"}
            ):
                constraints.append(item)
            else:
                highlights.append(item)

        sections: list[str] = []
        if highlights:
            sections.append("Highlights:")
            sections.extend(f"- {line}" for line in highlights[:5])
        if decisions:
            sections.append("Decisions:")
            sections.extend(f"- {line}" for line in decisions[:4])
        if constraints:
            sections.append("Constraints:")
            sections.extend(f"- {line}" for line in constraints[:4])
        if actions:
            sections.append("Action Ledger:")
            sections.extend(f"- {line}" for line in actions[:4])

        return "\n".join(sections).strip()

    def _merge_summaries(self, current: str, update: str) -> str:
        merged_lines: list[str] = []
        seen: set[str] = set()

        for raw in (current + "\n" + update).splitlines():
            line = raw.strip()
            if not line:
                continue
            key = re.sub(r"\W+", "", line.lower())[:120]
            if key in seen:
                continue
            seen.add(key)
            merged_lines.append(line)

        merged = "\n".join(merged_lines)
        if len(merged) > 4_500:
            merged = merged[-4_500:]
        return merged


def build_prompt(
    query: str,
    context_block: str,
    steering: SteeringProfile,
) -> str:
    """Build structured prompt envelope for SDK string-based prompts."""
    citation_behavior = "Citations are optional. Keep the response crisp and readable."
    if steering.citations == "Reference prior context points":
        citation_behavior = "When helpful, reference prior context points briefly."
    elif steering.citations == "Inline citations + uncertainty":
        citation_behavior = "Use inline citations and mention uncertainty where appropriate."

    # TODO(local-chat-roadmap): Add support for attachments with a nonblocking,
    # streaming-safe pipeline once the demo UI/perf work is stabilized.
    envelope = {
        "query": query,
        "steering": steering.to_dict(),
    }

    return "\n\n".join(
        [
            "You are responding to the next turn of a local-first chat app.",
            steering.to_prompt_block(),
            "Conversation Context:",
            context_block or "(no prior context)",
            "Turn Envelope JSON:",
            json.dumps(envelope, ensure_ascii=False, indent=2),
            f"Respond in Markdown. {citation_behavior}",
        ]
    )


class SiliconRefineryChatApp(toga.App):
    """Toga desktop app for local Apple Foundation Models chat."""

    def startup(self) -> None:
        """Build UI, initialize data/model, and load chats."""
        configure_theme_for_mode(self.dark_mode)
        self.store = ChatStore(self._db_path())
        self.compactor = CodexStyleCompactor()

        self.current_chat_id: int | None = None
        self.current_messages: list[dict[str, Any]] = []
        self.chat_index: dict[int, str] = {}
        self.chat_tab_button_map: dict[int, int] = {}
        self.is_busy = False
        self._steering_profile = SteeringProfile()
        self.settings_window: toga.Window | None = None
        self.settings_tone_select: toga.Selection | None = None
        self.settings_depth_select: toga.Selection | None = None
        self.settings_verbosity_select: toga.Selection | None = None
        self.settings_citation_select: toga.Selection | None = None

        self._active_stream_task: asyncio.Task | None = None
        self._active_stream_cancel_event: threading.Event | None = None
        self._stream_restart_requested = False
        self._pending_steering_interjection = ""
        self._loading_task: asyncio.Task | None = None
        self._theme_refresh_task: asyncio.Task | None = None
        self._send_shortcut_task: asyncio.Task | None = None
        self._status_raw_text = ""
        self._enter_to_send_enabled = False
        self._loading_caption = "Inference running"
        self._loading_nonce = 0
        self.free_threading_enabled, self.runtime_threading_mode = detect_runtime_threading_mode()
        # Cocoa text command delegation through rubicon can segfault on macOS 26.x.
        # Keep send action button-driven for release stability.
        self._enter_to_send_enabled = False
        cpu_workers = max(2, min(8, os.cpu_count() or 4))
        worker_count = cpu_workers if self.free_threading_enabled else min(4, cpu_workers)
        self._worker_pool = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="lfc-worker",
        )
        self._blocking_gate = asyncio.Semaphore(worker_count)
        self._sidebar_width = 312
        self._chat_tab_button_width = 268

        self.model = fm.SystemLanguageModel()
        self.model_available, self.model_unavailable_reason = self.model.is_available()

        self._build_ui()
        self._install_app_commands()
        self._refresh_chat_tabs()
        self._set_idle_state()

        if self.model_available:
            if self.free_threading_enabled:
                self._set_status_text(
                    "Model available (free-threaded no-gil). "
                    "Enter-send disabled for stability. /help for commands."
                )
            else:
                self._set_status_text(
                    "Model available (standard-gil). "
                    "Use CPython 3.13t/3.14t for no-gil mode. /help for commands."
                )
        else:
            self._set_status_text(
                "Model unavailable. "
                f"Reason: {self.model_unavailable_reason}. "
                "You can still browse/export saved chats."
            )

        self.main_window.show()
        self._theme_refresh_task = asyncio.create_task(self._post_show_theme_refresh())

    def _db_path(self) -> Path:
        """Resolve app-local sqlite path."""
        try:
            data_dir = Path(self.paths.data)
        except Exception:
            data_dir = Path.home() / ".silicon_refinery_chat"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / DB_FILENAME

    def _section_label(self, text: str) -> toga.Label:
        """Create a consistent section label."""
        return toga.Label(
            text,
            style=Pack(
                color=COLOR_TEXT_SECONDARY,
                font_size=FONT_SIZE_SECTION,
                font_weight="bold",
                margin=(12, 14, 6, 14),
            ),
        )

    async def _run_blocking(self, func: Any, /, *args: Any, **kwargs: Any) -> T:
        """Run blocking work in a thread with bounded concurrency."""
        async with self._blocking_gate:
            loop = asyncio.get_running_loop()
            bound = functools.partial(func, *args, **kwargs)
            return await loop.run_in_executor(self._worker_pool, bound)

    async def _prepare_context_block(
        self,
        messages: list[dict[str, Any]],
        rolling_summary: str,
        steering: SteeringProfile,
    ) -> tuple[str, str, bool]:
        """Offload context compaction prep to avoid UI jank on long chats."""
        message_snapshot = copy.deepcopy(messages)
        return await self._run_blocking(
            self.compactor.prepare_context,
            message_snapshot,
            rolling_summary,
            steering,
        )

    async def _post_show_theme_refresh(self) -> None:
        """Re-apply native textarea colors after Cocoa layout settles."""
        await asyncio.sleep(0)
        self._refresh_textarea_theme()
        await asyncio.sleep(0.2)
        self._refresh_textarea_theme()

    def _refresh_textarea_theme(self) -> None:
        """Apply native textarea theming to all multiline views."""
        for widget in (self.transcript_view, self.prompt_input):
            self._apply_native_textarea_theme(widget)

    def _window_width(self) -> int:
        """Best-effort current window width for responsive sizing."""
        if not hasattr(self, "main_window") or self.main_window is None:
            return 1280
        try:
            size = self.main_window.size
            return int(getattr(size, "width", size[0]))
        except Exception:
            return 1280

    def _window_height(self) -> int:
        """Best-effort current window height for responsive sizing."""
        if not hasattr(self, "main_window") or self.main_window is None:
            return 760
        try:
            size = self.main_window.size
            return int(getattr(size, "height", size[1]))
        except Exception:
            return 760

    def _wrap_status_text(self, text: str) -> str:
        """Wrap status text for narrow windows while preserving readability."""
        width = self._window_width()
        if width >= 980:
            return text
        available = max(300, width - self._sidebar_width - 120)
        max_chars = max(38, int(available / 7.2))
        wrapped_lines = [
            textwrap.fill(
                line,
                width=max_chars,
                break_long_words=False,
                break_on_hyphens=False,
            )
            for line in text.splitlines()
        ]
        return "\n".join(wrapped_lines)

    def _set_status_text(self, text: str) -> None:
        """Set status message and apply responsive wrapping."""
        self._status_raw_text = text
        self.status_label.text = self._wrap_status_text(text)

    def _refresh_status_text_layout(self) -> None:
        """Reflow current status message after resize changes."""
        self.status_label.text = self._wrap_status_text(self._status_raw_text)

    def _initial_window_size(self) -> tuple[int, int]:
        """Compute an on-screen-safe initial window size."""
        default = (980, 520)
        try:
            screen = self.screens[0]
            screen_size = screen.size
            sw = int(getattr(screen_size, "width", screen_size[0]))
            sh = int(getattr(screen_size, "height", screen_size[1]))
            width = max(780, min(1100, int(sw * 0.72)))
            height = max(430, min(700, int(sh * 0.55)))
            return (width, height)
        except Exception:
            return default

    def _apply_chat_tab_button_width(self) -> None:
        """Apply current responsive tab width to all tab buttons."""
        for child in self.chat_tabs_box.children:
            if isinstance(child, toga.Button):
                child.style.width = self._chat_tab_button_width

    def _wrapped_sidebar_title(self, sidebar_width: int) -> str:
        """Wrap the sidebar title manually to avoid clipping on narrower widths."""
        max_chars = max(16, min(34, int((sidebar_width - 34) / 8.5)))
        return textwrap.fill(
            SIDEBAR_TITLE_TEXT,
            width=max_chars,
            break_long_words=False,
            break_on_hyphens=False,
        )

    def _apply_responsive_layout(self) -> None:
        """Adjust widths/heights for current window size."""
        width = self._window_width()
        height = self._window_height()
        sidebar_width = max(220, min(350, int(width * 0.29)))
        if width < 1024 or height < 680:
            sidebar_width = max(200, min(286, int(width * 0.33)))
            self.transcript_view.style.height = 198
            self.prompt_input.style.height = 124
        else:
            self.transcript_view.style.height = 252
            self.prompt_input.style.height = 136
        if height < 560:
            self.transcript_view.style.height = 164
            self.prompt_input.style.height = 104

        self._sidebar_width = sidebar_width
        self._chat_tab_button_width = max(148, sidebar_width - 48)
        self.chat_column.style.width = sidebar_width
        self.sidebar_title_label.style.width = max(140, sidebar_width - 24)
        self.sidebar_title_label.text = self._wrapped_sidebar_title(sidebar_width)
        if width < 980:
            self.status_panel.style.margin = (0, 14, 14, 14)
        else:
            self.status_panel.style.margin = (12, 14, 14, 14)
        self._apply_chat_tab_button_width()
        self._refresh_status_text_layout()

    async def on_window_resize(self, window: toga.Window) -> None:
        """Resize handler that keeps the app readable across widths."""
        del window
        self._apply_responsive_layout()

    def _has_compose_text(self) -> bool:
        """Return True if compose box contains non-whitespace content."""
        return bool((self.prompt_input.value or "").strip())

    def _refresh_send_enabled(self) -> None:
        """Enable send only when idle and compose input has content."""
        enabled = (not self.is_busy) and self._has_compose_text()
        self.send_button.enabled = enabled
        send_command = getattr(self, "send_command", None)
        if send_command is not None:
            send_command.enabled = enabled

    def on_prompt_change(self, widget: toga.Widget) -> None:
        """Update send button state when compose text changes."""
        del widget
        self._refresh_send_enabled()

    def _install_app_commands(self) -> None:
        """Install hardened app-level commands with native shortcut wiring."""
        self.send_command = toga.Command(
            self._on_send_command,
            text="Send Message",
            shortcut=toga.Key.MOD_1 + toga.Key.ENTER,
            tooltip="Send the current compose message",
            group=toga.Group.EDIT,
            section=1,
            order=20,
            enabled=False,
            id="send-message",
        )
        self.commands.add(self.send_command)

    def _on_send_command(self, widget: object | None = None) -> None:
        """Handle Cmd+Enter send shortcut without Cocoa delegate interception."""
        del widget
        if self.is_busy or not self._has_compose_text():
            return
        if self._send_shortcut_task is not None and not self._send_shortcut_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._send_shortcut_task = loop.create_task(self.on_send(self.send_button))

    def _apply_native_textarea_theme(self, widget: toga.MultilineTextInput) -> None:
        """Apply Cocoa-native colors where backend theme mapping can override Pack colors."""
        try:
            from toga import colors
            from toga_cocoa.colors import native_color  # type: ignore
            from toga_cocoa.libs import (  # type: ignore
                NSAttributedString,
                NSFont,
                NSFontAttributeName,
                NSForegroundColorAttributeName,
                NSMutableDictionary,
                NSNoBorder,
                NSSize,
            )
        except Exception:
            return

        impl = getattr(widget, "_impl", None)
        native_scroll = getattr(impl, "native", None)
        native_text = getattr(impl, "native_text", None)
        if native_scroll is None or native_text is None:
            return

        try:
            bg_color = colors.color(COLOR_TEXTAREA_BG)
            fg_color = colors.color(COLOR_TEXT_PRIMARY)
            muted_color = colors.color(COLOR_TEXT_MUTED)
        except Exception:
            return

        bg = native_color(bg_color)
        fg = native_color(fg_color)
        muted = native_color(muted_color)
        setters = [
            lambda: setattr(native_scroll, "borderType", NSNoBorder),
            lambda: setattr(native_scroll, "drawsBackground", True),
            lambda: setattr(native_scroll, "backgroundColor", bg),
            lambda: setattr(native_scroll.contentView, "drawsBackground", True),
            lambda: setattr(native_scroll.contentView, "backgroundColor", bg),
            lambda: setattr(native_text, "drawsBackground", True),
            lambda: setattr(native_text, "backgroundColor", bg),
            lambda: setattr(native_text, "textColor", fg),
            lambda: setattr(native_text, "usesAdaptiveColorMappingForDarkAppearance", False),
            lambda: setattr(native_text, "insertionPointColor", fg),
        ]
        for setter in setters:
            try:
                setter()
            except Exception:
                continue
        with contextlib.suppress(Exception):
            native_text.font = NSFont.systemFontOfSize(FONT_SIZE_TEXTAREA)
        with contextlib.suppress(Exception):
            native_text.textContainerInset = NSSize(TEXTAREA_INSET_X, TEXTAREA_INSET_Y)
        with contextlib.suppress(Exception):
            text_container = native_text.textContainer
            if text_container is not None:
                text_container.lineFragmentPadding = TEXTAREA_LINE_FRAGMENT_PADDING
        with contextlib.suppress(Exception):
            typing_attrs = NSMutableDictionary.alloc().init()
            typing_attrs[NSFontAttributeName] = NSFont.systemFontOfSize(FONT_SIZE_TEXTAREA)
            typing_attrs[NSForegroundColorAttributeName] = fg
            native_text.typingAttributes = typing_attrs
        with contextlib.suppress(Exception):
            placeholder = (widget.placeholder or "").strip()
            if placeholder:
                placeholder_attrs = NSMutableDictionary.alloc().init()
                placeholder_attrs[NSFontAttributeName] = NSFont.systemFontOfSize(FONT_SIZE_TEXTAREA)
                placeholder_attrs[NSForegroundColorAttributeName] = muted
                native_text.placeholderAttributedString = NSAttributedString.alloc().initWithString(
                    placeholder,
                    attributes=placeholder_attrs,
                )

    def _set_transcript_value(self, text: str) -> None:
        """Set transcript text and apply metadata emphasis when supported."""
        self.transcript_view.value = text
        self._apply_transcript_metadata_typography()

    def _apply_transcript_metadata_typography(self) -> None:
        """Bold transcript metadata rows while keeping message bodies regular weight."""
        try:
            from toga_cocoa.libs import (  # type: ignore
                NSFont,
                NSFontAttributeName,
                NSMutableDictionary,
                NSRange,
            )
        except Exception:
            return

        impl = getattr(self.transcript_view, "_impl", None)
        native_text = getattr(impl, "native_text", None)
        if native_text is None:
            return

        transcript_text = str(getattr(native_text, "string", "") or "")
        if not transcript_text:
            return
        text_storage = getattr(native_text, "textStorage", None)
        if text_storage is None:
            return

        regular_attrs = NSMutableDictionary.alloc().init()
        regular_attrs[NSFontAttributeName] = NSFont.systemFontOfSize(FONT_SIZE_TEXTAREA)
        metadata_attrs = NSMutableDictionary.alloc().init()
        metadata_attrs[NSFontAttributeName] = NSFont.boldSystemFontOfSize(FONT_SIZE_TEXTAREA)

        try:
            text_storage.beginEditing()
            text_storage.addAttributes_range_(regular_attrs, NSRange(0, len(transcript_text)))
            for match in TRANSCRIPT_METADATA_ROW_PATTERN.finditer(transcript_text):
                text_storage.addAttributes_range_(
                    metadata_attrs,
                    NSRange(match.start(), match.end() - match.start()),
                )
        except Exception:
            return
        finally:
            with contextlib.suppress(Exception):
                text_storage.endEditing()

    def _build_ui(self) -> None:
        """Construct application widgets and layout."""
        self.new_chat_button = toga.Button(
            "New Chat",
            on_press=self.on_new_chat,
            style=Pack(
                flex=1,
                margin=(6, 8, 6, 0),
                background_color=COLOR_ACCENT,
                color="#FFFFFF",
                font_weight="bold",
                font_size=11,
            ),
        )
        self.delete_chat_button = toga.Button(
            "Delete",
            on_press=self.on_delete_chat,
            style=Pack(
                flex=1,
                margin=(6, 0, 6, 8),
                background_color=COLOR_DANGER_SOFT,
                color=COLOR_TEXT_PRIMARY,
                font_weight="bold",
                font_size=11,
            ),
        )

        chat_button_row = toga.Box(style=Pack(direction=ROW, margin=(8, 10, 12, 10)))
        chat_button_row.add(self.new_chat_button)
        chat_button_row.add(self.delete_chat_button)

        self.chat_tabs_box = toga.Box(style=Pack(direction=COLUMN, margin_top=6))
        self.chat_tabs_scroll = toga.ScrollContainer(
            horizontal=False,
            vertical=True,
            content=self.chat_tabs_box,
            style=Pack(
                flex=1,
                margin=(0, 10, 12, 10),
                background_color=COLOR_PANEL_BG,
            ),
        )

        self.sidebar_title_label = toga.Label(
            self._wrapped_sidebar_title(self._sidebar_width),
            style=Pack(
                margin=(12, 12, 0, 12),
                width=max(140, self._sidebar_width - 24),
                font_size=FONT_SIZE_TITLE,
                font_weight="bold",
                color=COLOR_TEXT_PRIMARY,
            ),
        )
        sidebar_subtitle = toga.Label(
            "Private, on-device assistant",
            style=Pack(
                margin=(4, 12, 10, 12),
                font_size=FONT_SIZE_META,
                color=COLOR_TEXT_MUTED,
            ),
        )

        chat_column = toga.Box(
            style=Pack(
                direction=COLUMN,
                width=self._sidebar_width,
                margin=(12, 10, 12, 12),
                background_color=COLOR_SIDEBAR_BG,
            )
        )
        chat_column.add(self.sidebar_title_label)
        chat_column.add(sidebar_subtitle)
        chat_column.add(chat_button_row)
        chat_column.add(self.chat_tabs_scroll)

        self.status_label = toga.Label(
            "",
            style=Pack(
                flex=1,
                color=COLOR_TEXT_SECONDARY,
                font_size=FONT_SIZE_BODY,
                margin=(2, 8, 0, 8),
            ),
        )
        self.loading_spinner = toga.ActivityIndicator(
            running=False,
            style=Pack(
                width=14,
                height=14,
                margin=(0, 10, 0, 8),
                visibility=HIDDEN,
            ),
        )
        self.loading_label = toga.Label(
            "",
            style=Pack(
                color=COLOR_ACCENT,
                font_size=FONT_SIZE_META,
                margin=(2, 8, 4, 8),
                visibility=HIDDEN,
            ),
        )
        self.status_text_column = toga.Box(
            style=Pack(
                direction=COLUMN,
                flex=1,
                justify_content="center",
            )
        )
        self.status_text_column.add(self.status_label)
        self.status_text_column.add(self.loading_label)
        self.status_panel = toga.Box(
            style=Pack(
                direction=ROW,
                align_items="center",
                margin=(12, 14, 14, 14),
                background_color=COLOR_ACCENT_SOFT,
            )
        )
        self.status_panel.add(self.status_text_column)
        self.status_panel.add(self.loading_spinner)

        self.transcript_view = toga.MultilineTextInput(
            readonly=True,
            style=Pack(
                height=252,
                margin=(0, 14, 14, 14),
                background_color=COLOR_TEXTAREA_BG,
                font_size=FONT_SIZE_TEXTAREA,
                color=COLOR_TEXT_PRIMARY,
            ),
        )
        self.prompt_input = toga.MultilineTextInput(
            placeholder="Message local assistant... (/help for commands)",
            on_change=self.on_prompt_change,
            style=Pack(
                height=136,
                margin=(0, 14, 10, 14),
                background_color=COLOR_TEXTAREA_BG,
                font_size=FONT_SIZE_TEXTAREA,
                color=COLOR_TEXT_PRIMARY,
            ),
        )
        self._refresh_textarea_theme()
        self.settings_button = toga.Button(
            "Settings",
            on_press=self.on_open_settings,
            style=Pack(
                flex=1,
                margin=(6, 8, 6, 0),
                background_color=COLOR_PANEL_BG,
                color=COLOR_TEXT_PRIMARY,
                font_size=11,
            ),
        )

        self.export_button = toga.Button(
            "Export",
            on_press=self.on_export_chat,
            style=Pack(
                flex=1,
                margin=(6, 8, 6, 0),
                background_color=COLOR_PANEL_BG,
                color=COLOR_TEXT_PRIMARY,
                font_size=11,
            ),
        )
        self.send_button = toga.Button(
            "Send",
            on_press=self.on_send,
            style=Pack(
                flex=1,
                margin=(6, 0, 6, 8),
                background_color=COLOR_ACCENT,
                color="#FFFFFF",
                font_weight="bold",
                font_size=11,
            ),
        )

        self.action_row = toga.Box(
            style=Pack(direction=ROW, margin=(4, 0, 0, 0), background_color=COLOR_APP_BG)
        )
        self.action_row.add(self.settings_button)
        self.action_row.add(self.export_button)
        self.action_row.add(self.send_button)

        self.command_hint_label = toga.Label(
            "Commands: /help /new /clear /export",
            style=Pack(
                font_size=FONT_SIZE_META,
                color=COLOR_TEXT_MUTED,
                margin=(0, 2, 2, 2),
            ),
        )
        if not self._enter_to_send_enabled:
            self.command_hint_label.text = (
                "Commands: /help /new /clear /export  |  Send: button only for stability"
            )

        self.right_column = toga.Box(
            style=Pack(direction=COLUMN, flex=1, background_color=COLOR_APP_BG)
        )
        self.right_column.add(self.status_panel)
        self.right_column.add(self._section_label("Transcript"))
        self.right_column.add(self.transcript_view)
        self.right_column.add(self._section_label("Compose"))
        self.right_column.add(self.prompt_input)
        self.right_scroll = toga.ScrollContainer(
            horizontal=False,
            vertical=True,
            content=self.right_column,
            style=Pack(
                flex=1,
                margin=(12, 12, 2, 4),
                background_color=COLOR_APP_BG,
            ),
        )
        self.right_bottom_controls = toga.Box(
            style=Pack(direction=COLUMN, margin=(4, 12, 12, 12), background_color=COLOR_APP_BG)
        )
        self.right_bottom_controls.add(self.command_hint_label)
        self.right_bottom_controls.add(self.action_row)
        self.right_pane = toga.Box(
            style=Pack(direction=COLUMN, flex=1, background_color=COLOR_APP_BG)
        )
        self.right_pane.add(self.right_scroll)
        self.right_pane.add(self.right_bottom_controls)

        self.root_box = toga.Box(style=Pack(direction=ROW, flex=1, background_color=COLOR_APP_BG))
        self.chat_column = chat_column
        self.root_box.add(self.chat_column)
        self.root_box.add(self.right_pane)

        self.main_window = toga.MainWindow(
            title=self.formal_name,
            size=self._initial_window_size(),
            resizable=True,
            on_resize=self.on_window_resize,
        )
        self.main_window.content = self.root_box
        self._apply_responsive_layout()
        self._refresh_send_enabled()

    def _new_settings_row(self, label: str, control: toga.Selection) -> toga.Box:
        """Build one settings row for the settings popup."""
        row = toga.Box(style=Pack(direction=ROW, margin=(0, 0, 10, 0)))
        row.add(
            toga.Label(
                label,
                style=Pack(
                    width=96,
                    margin_top=6,
                    color=COLOR_TEXT_SECONDARY,
                    font_size=FONT_SIZE_BODY,
                ),
            )
        )
        row.add(control)
        return row

    async def on_open_settings(self, widget: toga.Widget) -> None:
        """Open a compact settings popup for response style controls."""
        if self.settings_window is not None:
            try:
                self.settings_window.show()
                return
            except Exception:
                self.settings_window = None

        steering = self._active_steering()
        self.settings_tone_select = toga.Selection(
            items=VALID_TONES,
            value=steering.tone,
            style=Pack(flex=1),
        )
        self.settings_depth_select = toga.Selection(
            items=VALID_DEPTHS,
            value=steering.depth,
            style=Pack(flex=1),
        )
        self.settings_verbosity_select = toga.Selection(
            items=VALID_VERBOSITY,
            value=steering.verbosity,
            style=Pack(flex=1),
        )
        self.settings_citation_select = toga.Selection(
            items=VALID_CITATION_MODES,
            value=steering.citations,
            style=Pack(flex=1),
        )

        form = toga.Box(style=Pack(direction=COLUMN, margin=14, background_color=COLOR_PANEL_BG))
        form.add(
            toga.Label(
                "Response Settings",
                style=Pack(
                    color=COLOR_TEXT_PRIMARY,
                    font_size=FONT_SIZE_SECTION,
                    font_weight="bold",
                    margin_bottom=8,
                ),
            )
        )
        form.add(self._new_settings_row("Tone", self.settings_tone_select))
        form.add(self._new_settings_row("Depth", self.settings_depth_select))
        form.add(self._new_settings_row("Length", self.settings_verbosity_select))
        form.add(self._new_settings_row("Citations", self.settings_citation_select))

        button_row = toga.Box(style=Pack(direction=ROW, margin_top=8))
        button_row.add(
            toga.Button(
                "Cancel",
                on_press=self.on_cancel_settings,
                style=Pack(
                    flex=1,
                    margin=(6, 8, 6, 0),
                    background_color=COLOR_PANEL_BG,
                    color=COLOR_TEXT_PRIMARY,
                    font_size=11,
                ),
            )
        )
        button_row.add(
            toga.Button(
                "Save",
                on_press=self.on_save_settings,
                style=Pack(
                    flex=1,
                    margin=(6, 0, 6, 8),
                    background_color=COLOR_ACCENT,
                    color="#FFFFFF",
                    font_weight="bold",
                    font_size=11,
                ),
            )
        )
        form.add(button_row)

        self.settings_window = toga.Window(
            title="Response Settings",
            size=(460, 280),
            resizable=False,
        )
        self.settings_window.content = form
        self.settings_window.show()
        del widget

    async def on_cancel_settings(self, widget: toga.Widget) -> None:
        """Close settings popup without applying changes."""
        del widget
        if self.settings_window is not None:
            self.settings_window.close()
        self.settings_window = None

    async def on_save_settings(self, widget: toga.Widget) -> None:
        """Apply settings from popup and close it."""
        del widget
        if (
            self.settings_tone_select is None
            or self.settings_depth_select is None
            or self.settings_verbosity_select is None
            or self.settings_citation_select is None
        ):
            return

        steering = SteeringProfile(
            tone=normalize_choice(
                str(self.settings_tone_select.value or "Balanced"), VALID_TONES, "Balanced"
            ),
            depth=normalize_choice(
                str(self.settings_depth_select.value or "Detailed"), VALID_DEPTHS, "Detailed"
            ),
            verbosity=normalize_choice(
                str(self.settings_verbosity_select.value or "Medium"), VALID_VERBOSITY, "Medium"
            ),
            citations=normalize_choice(
                str(self.settings_citation_select.value or "No citation requirement"),
                VALID_CITATION_MODES,
                "No citation requirement",
            ),
        )
        await self._handle_steering_changed(steering)
        if self.settings_window is not None:
            self.settings_window.close()
        self.settings_window = None

    def _clear_box_children(self, box: toga.Box) -> None:
        """Remove all children from a Box."""
        while box.children:
            box.remove(box.children[0])

    def _refresh_chat_tabs(self, selected_chat_id: int | None = None) -> None:
        """Rebuild vertical chat tab stack."""
        chats = self.store.list_chats()
        self.chat_index = {int(chat["id"]): str(chat["title"]) for chat in chats}
        self.chat_tab_button_map = {}

        active_id = selected_chat_id if selected_chat_id is not None else self.current_chat_id
        self._clear_box_children(self.chat_tabs_box)

        if not chats:
            self.chat_tabs_box.add(
                toga.Label(
                    "No chats yet",
                    style=Pack(
                        color=COLOR_TEXT_MUTED,
                        margin=(12, 12, 10, 12),
                        font_size=FONT_SIZE_BODY,
                    ),
                )
            )
            return

        for chat in chats:
            chat_id = int(chat["id"])
            title = str(chat["title"])
            label = title if len(title) <= 42 else f"{title[:39]}..."
            is_active = chat_id == active_id
            button = toga.Button(
                label,
                on_press=self.on_chat_tab_pressed,
                style=Pack(
                    width=self._chat_tab_button_width,
                    margin=(8, 10, 6, 10),
                    text_align="left",
                    background_color=COLOR_TAB_ACTIVE if is_active else COLOR_TAB_IDLE,
                    color=COLOR_TEXT_PRIMARY,
                    font_weight="bold" if is_active else "normal",
                    font_size=FONT_SIZE_BODY,
                ),
            )
            self.chat_tab_button_map[id(button)] = chat_id
            self.chat_tabs_box.add(button)

    async def on_chat_tab_pressed(self, widget: toga.Widget) -> None:
        """Switch to chat selected from left sidebar tabs."""
        chat_id = self.chat_tab_button_map.get(id(widget))
        if chat_id is None:
            return
        await self._activate_chat(chat_id)

    async def _activate_chat(self, chat_id: int) -> None:
        """Load chat history + steering into active view."""
        self.current_chat_id = chat_id
        self.current_messages = self.store.load_messages(chat_id)
        self._set_active_steering(self.store.get_steering(chat_id))
        self._refresh_chat_tabs(selected_chat_id=chat_id)
        self._render_transcript()
        self._set_status_text(f"Resumed chat: {self.store.chat_title(chat_id) or chat_id}")

    def _active_steering(self) -> SteeringProfile:
        """Return current steering profile."""
        return SteeringProfile(**self._steering_profile.to_dict())

    def _set_active_steering(self, steering: SteeringProfile) -> None:
        """Update in-memory steering profile."""
        self._steering_profile = SteeringProfile(**steering.to_dict())

    async def _handle_steering_changed(self, steering: SteeringProfile) -> None:
        """Persist steering and apply mid-stream interjection semantics."""
        self._set_active_steering(steering)
        if self.current_chat_id is not None:
            self.store.set_steering(self.current_chat_id, steering)

        if self.is_busy and self._active_stream_task is not None:
            if not self._stream_restart_requested:
                self._stream_restart_requested = True
                self._pending_steering_interjection = self._make_steering_interjection()
                self._set_loading_caption("Regenerating with settings")
                self._set_status_text("Settings changed. Interrupting and regenerating...")
                if self._active_stream_cancel_event is not None:
                    self._active_stream_cancel_event.set()
                self._active_stream_task.cancel()
            return

        self._set_status_text("Response settings updated.")

    def _set_busy(self, busy: bool) -> None:
        """Enable/disable actions while generating."""
        self.is_busy = busy
        enabled = not busy
        self.new_chat_button.enabled = enabled
        self.delete_chat_button.enabled = enabled
        self._refresh_send_enabled()
        if busy:
            self._start_loading_animation()
        else:
            self._stop_loading_animation()

    def _set_loading_caption(self, caption: str) -> None:
        """Set status text used by shimmer loading indicator."""
        self._loading_caption = caption

    def _start_loading_animation(self) -> None:
        """Start nonblocking shimmer indicator while inference is active."""
        self._loading_nonce += 1
        nonce = self._loading_nonce
        self.loading_label.style.visibility = VISIBLE
        self.loading_spinner.style.visibility = VISIBLE
        with contextlib.suppress(Exception):
            self.loading_spinner.start()
        if self._loading_task is not None and not self._loading_task.done():
            return
        self._loading_task = asyncio.create_task(self._loading_animation_loop(nonce))

    def _stop_loading_animation(self) -> None:
        """Stop shimmer indicator."""
        self._loading_nonce += 1
        task = self._loading_task
        self._loading_task = None
        if task is not None and not task.done():
            task.cancel()
        self.loading_label.text = ""
        self.loading_label.style.visibility = HIDDEN
        with contextlib.suppress(Exception):
            self.loading_spinner.stop()
        self.loading_spinner.style.visibility = HIDDEN

    async def _loading_animation_loop(self, nonce: int) -> None:
        """Render a lightweight shimmer text indicator."""
        frame_index = 0
        try:
            while self.is_busy and nonce == self._loading_nonce:
                frame = LOADING_SHIMMER_FRAMES[frame_index % len(LOADING_SHIMMER_FRAMES)]
                self.loading_label.text = f"{self._loading_caption}  {frame}"
                frame_index += 1
                await asyncio.sleep(0.09)
        except asyncio.CancelledError:
            return
        finally:
            if not self.is_busy and nonce == self._loading_nonce:
                self.loading_label.text = ""
                self.loading_label.style.visibility = HIDDEN
                with contextlib.suppress(Exception):
                    self.loading_spinner.stop()
                self.loading_spinner.style.visibility = HIDDEN

    async def _stream_response_on_worker(self, prompt: str, cancel_event: threading.Event) -> Any:
        """Run SDK streaming on a worker thread and forward snapshots to UI loop."""
        ui_loop = asyncio.get_running_loop()
        event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        worker_done = threading.Event()

        def producer_sync() -> None:
            async def producer() -> None:
                session = fm.LanguageModelSession(
                    model=self.model, instructions=SYSTEM_INSTRUCTIONS
                )
                try:
                    async for snapshot in session.stream_response(prompt):
                        if cancel_event.is_set():
                            break
                        ui_loop.call_soon_threadsafe(
                            event_queue.put_nowait, ("chunk", str(snapshot))
                        )
                except Exception as exc:
                    ui_loop.call_soon_threadsafe(event_queue.put_nowait, ("error", exc))
                    return
                ui_loop.call_soon_threadsafe(event_queue.put_nowait, ("done", None))

            try:
                asyncio.run(producer())
            except Exception as exc:
                ui_loop.call_soon_threadsafe(event_queue.put_nowait, ("error", exc))
            finally:
                worker_done.set()

        worker_thread = threading.Thread(
            target=producer_sync,
            name="lfc-stream-worker",
            daemon=True,
        )
        worker_thread.start()
        first_chunk_seen = False

        try:
            while True:
                timeout = (
                    STREAM_CHUNK_IDLE_TIMEOUT_SECONDS
                    if first_chunk_seen
                    else STREAM_FIRST_CHUNK_TIMEOUT_SECONDS
                )
                try:
                    kind, payload = await asyncio.wait_for(event_queue.get(), timeout=timeout)
                except TimeoutError as exc:
                    label = "response stream" if first_chunk_seen else "first response chunk"
                    raise TimeoutError(
                        f"Timed out waiting for {label} after {timeout:.0f}s."
                    ) from exc
                if kind == "chunk":
                    first_chunk_seen = True
                    yield str(payload)
                    continue
                if kind == "error":
                    if isinstance(payload, Exception):
                        raise payload
                    raise RuntimeError(str(payload))
                break
        finally:
            cancel_event.set()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    worker_done.wait,
                    STREAM_WORKER_JOIN_TIMEOUT_SECONDS,
                )

    def _set_idle_state(self) -> None:
        """Reset chat view to fresh state."""
        self.current_chat_id = None
        self.current_messages = []
        self._set_transcript_value("Start a new chat and send a message.")
        self.prompt_input.value = ""
        self._refresh_chat_tabs()
        self._refresh_textarea_theme()
        self._refresh_send_enabled()

    def _message_lines(self, message: dict[str, Any]) -> list[str]:
        """Render one message into transcript lines."""
        role = "You" if message["role"] == "user" else "Assistant"
        lines = [f"{role} | {message.get('created_at', '')}"]
        lines.append(str(message.get("content", "")))
        lines.append("")
        return lines

    def _render_messages_text(self, messages: list[dict[str, Any]]) -> str:
        """Render list of messages into transcript text."""
        if not messages:
            return ""
        lines: list[str] = []
        for message in messages:
            lines.extend(self._message_lines(message))
        return "\n".join(lines).strip()

    def _render_streaming_assistant_frame(
        self, prefix_text: str, assistant_message: dict[str, Any], assistant_text: str
    ) -> None:
        """Update transcript during streaming without rebuilding all message blocks."""
        assistant_header = f"Assistant | {assistant_message.get('created_at', '')}"
        if prefix_text:
            self._set_transcript_value(
                f"{prefix_text}\n\n{assistant_header}\n{assistant_text}".strip()
            )
            return
        self._set_transcript_value(f"{assistant_header}\n{assistant_text}".strip())

    def _should_commit_stream_frame(
        self, current_text: str, previous_text: str, last_commit_time: float, now: float
    ) -> bool:
        """Decide when to push next streamed frame to UI."""
        if current_text == previous_text:
            return False
        if not previous_text:
            return bool(current_text)

        delta_chars = max(0, len(current_text) - len(previous_text))
        elapsed = now - last_commit_time
        tail = current_text[-1] if current_text else ""

        if delta_chars >= STREAM_UI_MIN_CHARS_DELTA:
            return True
        if tail in STREAM_UI_BREAK_CHARS and elapsed >= STREAM_UI_MIN_INTERVAL_SECONDS:
            return True
        return elapsed >= STREAM_UI_MAX_INTERVAL_SECONDS

    def _render_transcript(self) -> None:
        """Render in-memory messages to transcript view."""
        if not self.current_messages:
            self._set_transcript_value("No messages yet in this chat.")
            return

        self._set_transcript_value(self._render_messages_text(self.current_messages))

    def _append_local_note(self, text: str) -> None:
        """Append local command output into transcript pane."""
        if self.current_messages:
            self._set_transcript_value(f"{self.transcript_view.value}\n\n[local]\n{text}")
        else:
            self._set_transcript_value(f"[local]\n{text}")

    async def _stream_user_query_preview(self, user_message: dict[str, Any], query: str) -> None:
        """Stream the submitted query into transcript for realtime feedback."""
        if not query:
            return
        if len(query) <= QUERY_PREVIEW_STEP_CHARS * 2:
            user_message["content"] = query
            self._render_transcript()
            return

        for idx in range(
            QUERY_PREVIEW_STEP_CHARS,
            len(query) + QUERY_PREVIEW_STEP_CHARS,
            QUERY_PREVIEW_STEP_CHARS,
        ):
            user_message["content"] = query[:idx]
            self._render_transcript()
            await asyncio.sleep(QUERY_PREVIEW_INTERVAL_SECONDS)
        user_message["content"] = query
        self._render_transcript()

    def _make_steering_interjection(self) -> str:
        """Generate emphasized steering interjection to splice into history."""
        steering = self._active_steering()
        return "\n".join(
            [
                "STEERING_INTERJECTION (HIGH PRIORITY)",
                "The user changed steering while inference was in progress.",
                "Discard partial answer and regenerate using this updated steering profile.",
                steering.to_prompt_block(),
            ]
        )

    def _create_or_activate_chat_from_query(self, query: str, steering: SteeringProfile) -> None:
        """Ensure we have an active persisted chat before writing messages."""
        if self.current_chat_id is not None:
            self.store.set_steering(self.current_chat_id, steering)
            return

        chat_id, title = self.store.create_chat(query, steering)
        self.current_chat_id = chat_id
        self._refresh_chat_tabs(selected_chat_id=chat_id)
        self._set_status_text(f"Created new chat: {title}")

    async def _maybe_run_slash_command(self, raw_text: str) -> bool:
        """Execute familiar slash commands. Returns True if handled."""
        if not raw_text.startswith("/"):
            return False

        try:
            tokens = shlex.split(raw_text)
        except ValueError as exc:
            self._set_status_text(f"Command parse error: {exc}")
            return True

        if not tokens:
            self._set_status_text("Empty command.")
            return True

        command = tokens[0].lower()
        args = tokens[1:]

        if command == "/help":
            self._set_status_text("Command help shown in transcript.")
            self._append_local_note(HELP_TEXT)
            return True

        if command in {"/new", "/clear"}:
            await self.on_new_chat(self.new_chat_button)
            return True

        if command == "/export":
            if self.current_chat_id is None:
                self._set_status_text("No active chat to export.")
                return True

            fmt = "jsonl"
            destination: Path | None = None
            if args:
                first = args[0].lower()
                if first in {"jsonl", "md"}:
                    fmt = first
                    if len(args) > 1:
                        destination = Path(args[1]).expanduser()
                else:
                    destination = Path(args[0]).expanduser()

            title = self.store.chat_title(self.current_chat_id) or "chat"
            if destination is None:
                suffix = ".md" if fmt == "md" else ".jsonl"
                destination = Path.cwd() / f"{slugify_filename(title)}{suffix}"

            if fmt == "md" or destination.suffix.lower() == ".md":
                if destination.suffix.lower() != ".md":
                    destination = destination.with_suffix(".md")
                self.store.export_markdown(self.current_chat_id, destination)
            else:
                if destination.suffix.lower() != ".jsonl":
                    destination = destination.with_suffix(".jsonl")
                self.store.export_jsonl(self.current_chat_id, destination)

            self._set_status_text(f"Exported chat to {destination}")
            self._append_local_note(f"Export complete: {destination}")
            return True

        self._set_status_text(f"Unknown command: {command}. Try /help.")
        return True

    async def on_new_chat(self, widget: toga.Widget) -> None:
        """Start a fresh unsaved chat."""
        self._set_idle_state()
        self._set_status_text("New chat ready. Title auto-generates from first message.")

    async def on_delete_chat(self, widget: toga.Widget) -> None:
        """Delete currently active chat."""
        if self.current_chat_id is None:
            self._set_status_text("No chat selected to delete.")
            return

        title = self.store.chat_title(self.current_chat_id) or "this chat"
        confirmed = await self.main_window.dialog(
            toga.ConfirmDialog("Delete Chat", f"Delete '{title}' permanently?")
        )
        if not confirmed:
            return

        self.store.delete_chat(self.current_chat_id)
        self._set_idle_state()
        self._set_status_text("Chat deleted.")

    async def on_export_chat(self, widget: toga.Widget) -> None:
        """Export current chat via file dialog."""
        if self.current_chat_id is None:
            self._set_status_text("Select a chat first to export.")
            return

        title = self.store.chat_title(self.current_chat_id) or "chat"
        suggested = f"{slugify_filename(title)}.jsonl"

        target = await self.main_window.dialog(
            toga.SaveFileDialog(
                "Export chat",
                suggested_filename=suggested,
                file_types=["jsonl", "md"],
            )
        )
        if not target:
            return

        path = Path(str(target))
        if path.suffix.lower() == ".md":
            self.store.export_markdown(self.current_chat_id, path)
        else:
            if path.suffix.lower() != ".jsonl":
                path = path.with_suffix(".jsonl")
            self.store.export_jsonl(self.current_chat_id, path)

        self._set_status_text(f"Exported chat to {path}")

    async def on_send(self, widget: toga.Widget) -> None:
        """Send user query or execute slash command."""
        if self.is_busy:
            return

        raw_query = (self.prompt_input.value or "").strip()
        if not raw_query:
            self._set_status_text("Type a message first.")
            return

        if await self._maybe_run_slash_command(raw_query):
            self.prompt_input.value = ""
            self._refresh_send_enabled()
            return

        if not self.model_available:
            self._set_status_text("Model unavailable. Cannot run inference.")
            return

        steering = self._active_steering()
        self._create_or_activate_chat_from_query(raw_query, steering)
        if self.current_chat_id is None:
            self._set_status_text("Unable to create chat session.")
            return

        user_message = {
            "role": "user",
            "content": "",
            "created_at": utc_now_iso(),
        }
        self.current_messages.append(user_message)

        assistant_message = {
            "role": "assistant",
            "content": "",
            "created_at": utc_now_iso(),
        }
        self.current_messages.append(assistant_message)

        self.prompt_input.value = ""
        self._refresh_send_enabled()
        self._render_transcript()

        self._set_loading_caption("Streaming query")
        self._set_busy(True)
        self._active_stream_task = asyncio.current_task()
        self._stream_restart_requested = False
        self._pending_steering_interjection = ""

        final_assistant_text = ""
        final_status_text = ""
        restart_count = 0
        query_stream_task: asyncio.Task | None = None

        try:
            query_stream_task = asyncio.create_task(
                self._stream_user_query_preview(user_message, raw_query)
            )
            await query_stream_task

            self._render_transcript()
            self.store.add_message(self.current_chat_id, "user", raw_query)

            while restart_count <= MAX_STREAM_RESTARTS:
                steering_for_turn = self._active_steering()
                self.store.set_steering(self.current_chat_id, steering_for_turn)

                if restart_count > 0 and self._pending_steering_interjection:
                    interjection = {
                        "role": "user",
                        "content": self._pending_steering_interjection,
                        "created_at": utc_now_iso(),
                    }
                    self.current_messages.insert(len(self.current_messages) - 1, interjection)
                    self.store.add_message(
                        self.current_chat_id,
                        "user",
                        self._pending_steering_interjection,
                    )
                    self._pending_steering_interjection = ""

                rolling_summary = self.store.get_rolling_summary(self.current_chat_id)
                context_block, updated_summary, _compacted = await self._prepare_context_block(
                    self.current_messages[:-1],
                    rolling_summary,
                    steering_for_turn,
                )
                if updated_summary != rolling_summary:
                    self.store.set_rolling_summary(self.current_chat_id, updated_summary)

                prompt = build_prompt(raw_query, context_block, steering_for_turn)

                assistant_text = ""
                last_ui_update_text = ""
                last_ui_update_time = time.monotonic()
                self._stream_restart_requested = False
                self._set_loading_caption("Streaming response")
                stream_prefix_text = self._render_messages_text(self.current_messages[:-1])
                stream_cancel_event = threading.Event()
                self._active_stream_cancel_event = stream_cancel_event

                try:
                    async for snapshot in self._stream_response_on_worker(
                        prompt, stream_cancel_event
                    ):
                        assistant_text = str(snapshot)
                        now = time.monotonic()
                        should_update = self._should_commit_stream_frame(
                            assistant_text,
                            last_ui_update_text,
                            last_ui_update_time,
                            now,
                        )
                        if should_update:
                            assistant_message["content"] = assistant_text
                            self._render_streaming_assistant_frame(
                                stream_prefix_text, assistant_message, assistant_text
                            )
                            last_ui_update_text = assistant_text
                            last_ui_update_time = now

                    assistant_message["content"] = assistant_text
                    self._render_streaming_assistant_frame(
                        stream_prefix_text, assistant_message, assistant_text
                    )
                    final_assistant_text = assistant_text
                    break

                except asyncio.CancelledError:
                    stream_cancel_event.set()
                    if self._stream_restart_requested:
                        restart_count += 1
                        self._set_loading_caption("Regenerating response")
                        assistant_message["content"] = (
                            "[Interrupted by settings update. Regenerating with interjection...]"
                        )
                        self._render_streaming_assistant_frame(
                            stream_prefix_text,
                            assistant_message,
                            assistant_message["content"],
                        )
                        continue
                    raise
                finally:
                    if self._active_stream_cancel_event is stream_cancel_event:
                        self._active_stream_cancel_event = None

            else:
                final_assistant_text = (
                    "Settings changed too many times during this turn. "
                    "Please send again once settings settle."
                )
                assistant_message["content"] = final_assistant_text
                self._render_transcript()

        except TimeoutError as exc:
            final_assistant_text = (
                "The local model timed out while streaming this response. Please retry."
            )
            assistant_message["content"] = final_assistant_text
            self._render_transcript()
            final_status_text = str(exc)
        except Exception as exc:
            final_assistant_text = f"Local model error: {exc}"
            assistant_message["content"] = final_assistant_text
            self._render_transcript()
            final_status_text = str(exc)
            await self.main_window.dialog(toga.ErrorDialog("Generation error", str(exc)))
        finally:
            if query_stream_task is not None and not query_stream_task.done():
                query_stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await query_stream_task
            self._active_stream_task = None
            if self._active_stream_cancel_event is not None:
                self._active_stream_cancel_event.set()
            self._active_stream_cancel_event = None
            self._stream_restart_requested = False
            self._pending_steering_interjection = ""
            self._set_busy(False)

        if not final_status_text:
            final_status_text = f"Response streamed (restarts={restart_count})."
        self.store.add_message(self.current_chat_id, "assistant", final_assistant_text)
        self._refresh_chat_tabs(selected_chat_id=self.current_chat_id)
        self._set_status_text(final_status_text)

    def on_exit(self) -> bool:
        """Close sqlite connection when app exits."""
        if self._theme_refresh_task is not None and not self._theme_refresh_task.done():
            self._theme_refresh_task.cancel()
        if self._loading_task is not None and not self._loading_task.done():
            self._loading_task.cancel()
        self._worker_pool.shutdown(wait=False, cancel_futures=True)
        self.store.close()
        return True


def main() -> SiliconRefineryChatApp:
    """Briefcase entrypoint."""
    return SiliconRefineryChatApp(
        formal_name="SiliconRefineryChat",
        app_id="com.siliconrefinery.chat",
    )
