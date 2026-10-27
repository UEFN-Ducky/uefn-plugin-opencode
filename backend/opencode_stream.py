"""OpenCode 2 session events → Ducky chat events and persisted blocks.

Text and reasoning deltas are batched (one push per 50 ms or 2 KB) so a long reply is
tens of UI updates, not one per token. Tool results keep a bounded tail.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

FLUSH_S = 0.05
FLUSH_CHARS = 2048
RESULT_CHARS = 8000

_LOGIN_HINTS = ("unauthorized", "api key", "apikey", "not authenticated", "authentication", "invalid key")
_LIMIT_HINTS = ("rate limit", "quota", "too many requests", "usage limit", "insufficient")


def display_tool_name(name: str) -> str:
    """``ducky-1a2b3c4d5e6f_workspace_read_file`` → ``workspace_read_file``."""
    if name.startswith("ducky-") and "_" in name:
        return name.split("_", 1)[1]
    return name


def _tail(text: str, limit: int = RESULT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…(truncated)\n" + text[-limit:]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text") or "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
    return ""


def classify_error(error: Any) -> tuple[str, str]:
    """(status, message) for a failed execution: needs_login, usage_limit or error."""
    err = error if isinstance(error, dict) else {"message": str(error or "")}
    message = str(err.get("message") or "OpenCode turn failed")
    status_code = int(err.get("status") or 0) if str(err.get("status") or "").isdigit() else 0
    low = (message + " " + str(err.get("type") or "")).lower()
    if status_code in (401, 403) or any(h in low for h in _LOGIN_HINTS):
        return "needs_login", message
    if status_code == 429 or any(h in low for h in _LIMIT_HINTS):
        return "usage_limit", f"The model's provider is limiting requests right now: {message}"
    return "error", message


class OpenCodeStream:
    END_TYPES = ("session.execution.succeeded", "session.execution.failed", "session.execution.interrupted")

    def __init__(self, conv_id: str, run_id: str, push: Callable[[dict[str, Any]], None],
                 *, clock: Callable[[], float] = time.monotonic) -> None:
        self.conv_id = conv_id
        self.run_id = run_id
        self.push = push
        self.clock = clock
        self.text_parts: list[str] = []
        self.blocks: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {}
        self.outcome = ""  # succeeded / failed / interrupted
        self.interrupt_reason = ""
        self.error_status = ""
        self.error_text = ""
        self.started = False
        self.pushes = 0
        self._seg_text: list[str] = []
        self._seg_thinking: list[str] = []
        self._pending_text = ""
        self._pending_thinking = ""
        self._last_flush = clock()
        self._text_live = False
        self._thinking_live = False
        self._tools: dict[str, dict[str, Any]] = {}
        self._inner: dict[str, str] = {}
        self._context_tokens = 0
        self.declined: list[str] = []

    # -- output -----------------------------------------------------------------------
    def _emit(self, event: dict[str, Any]) -> None:
        event.setdefault("conv_id", self.conv_id)
        if self.run_id:
            event.setdefault("run_id", self.run_id)
        self.pushes += 1
        self.push(event)

    def status(self, text: str) -> None:
        if text:
            self._emit({"type": "status", "text": text})

    def flush(self, force: bool = False) -> None:
        """Push batched deltas once 50 ms passed or 2 KB piled up (always with force)."""
        pending = len(self._pending_text) + len(self._pending_thinking)
        if not pending:
            return
        if not force and pending < FLUSH_CHARS and self.clock() - self._last_flush < FLUSH_S:
            return
        if self._pending_thinking:
            self._emit({"type": "thinking", "text": self._pending_thinking})
            self._pending_thinking = ""
        if self._pending_text:
            self._emit({"type": "text_delta", "text": self._pending_text})
            self._pending_text = ""
        self._last_flush = self.clock()

    def _add_text(self, text: str) -> None:
        if not text:
            return
        self.text_parts.append(text)
        self._seg_text.append(text)
        self._pending_text += text
        self.flush()

    def _add_thinking(self, text: str) -> None:
        if not text:
            return
        self._seg_thinking.append(text)
        self._pending_thinking += text
        self.flush()

    def _close_segments(self) -> None:
        """A tool call follows: move the narration so far into blocks."""
        self.flush(force=True)
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
        text = "".join(self._seg_text).strip()
        if text:
            self.blocks.append({"type": "text", "text": text})
        self._seg_thinking, self._seg_text = [], []

    # -- input ------------------------------------------------------------------------
    def on_event(self, event: dict[str, Any]) -> bool:
        """Apply one server event; True once the turn ended."""
        kind = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "session.execution.started":
            self.started = True
        elif kind == "session.text.delta":
            self._text_live = True
            self._add_text(str(data.get("delta") or ""))
        elif kind == "session.text.ended":
            if not self._text_live:  # a provider that sent the part whole
                self._add_text(str(data.get("text") or ""))
            self._text_live = False
        elif kind == "session.reasoning.delta":
            self._thinking_live = True
            self._add_thinking(str(data.get("delta") or ""))
        elif kind == "session.reasoning.ended":
            if not self._thinking_live:
                self._add_thinking(str(data.get("text") or ""))
            self._thinking_live = False
        elif kind == "session.tool.input.started":
            self._tool_start(str(data.get("id") or ""), str(data.get("name") or "tool"), None)
        elif kind == "session.tool.called":
            self._tool_start(str(data.get("id") or ""), "", data.get("input"))
        elif kind == "session.tool.progress":
            self._tool_progress(str(data.get("id") or ""), data.get("metadata") or {})
        elif kind == "session.tool.success":
            self._tool_done(str(data.get("id") or ""), ok=True, text=_content_text(data.get("content")))
        elif kind == "session.tool.failed":
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            self._tool_done(str(data.get("id") or ""), ok=False, text=str(err.get("message") or "Tool failed"))
        elif kind == "session.step.ended":
            tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            self._context_tokens = int(tokens.get("input") or 0) + int(cache.get("read") or 0)
        elif kind == "session.usage.updated":
            self._usage(data)
        elif kind == "session.retry.scheduled":
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            self.status(f"The model's provider is busy, retrying (attempt {data.get('attempt') or 2})… "
                        + str(err.get("message") or ""))
        elif kind in self.END_TYPES:
            self.flush(force=True)
            self.outcome = kind.rsplit(".", 1)[1]
            if kind == "session.execution.failed":
                self.error_status, self.error_text = classify_error(data.get("error"))
            elif kind == "session.execution.interrupted":
                self.interrupt_reason = str(data.get("reason") or "")
            return True
        return False

    def _usage(self, data: dict[str, Any]) -> None:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        cost = data.get("cost")
        self.usage = {
            # Session totals: core turns cumulative thread usage into this turn's share.
            "cumulative_thread": str(data.get("sessionID") or ""),
            "input_tokens": int(tokens.get("input") or 0),
            "output_tokens": int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0),
            "cache_read_tokens": int(cache.get("read") or 0),
            "cache_write_tokens": int(cache.get("write") or 0),
            "context_tokens": self._context_tokens,
            "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
            "num_turns": 0,
            "model": "",
        }

    def _tool_start(self, tool_id: str, name: str, args: Any) -> None:
        info = self._tools.get(tool_id)
        if info is None:
            self._close_segments()
            info = {"name": name or "tool", "arguments": {}, "started": self.clock(),
                    "started_wall": time.time(), "shown": False}
            self._tools[tool_id] = info
        if name:
            info["name"] = name
        if isinstance(args, dict):
            info["arguments"] = args
        if not info["shown"] and (isinstance(args, dict) or name):
            # Shown once its input is known; input.started alone shows the name early.
            if isinstance(args, dict):
                info["shown"] = True
                label = display_tool_name(info["name"])
                self._emit({"type": "tool", "text": f"⚙ {label}",
                            "tool": {"name": label, "arguments": info["arguments"], "status": "pending"}})

    def _tool_progress(self, tool_id: str, metadata: dict[str, Any]) -> None:
        # Code mode: one `execute` tool runs Ducky's MCP tools inside; name them as they run.
        calls = metadata.get("toolCalls") if isinstance(metadata, dict) else None
        if not isinstance(calls, list) or not calls:
            return
        last = calls[-1] if isinstance(calls[-1], dict) else {}
        line = f"{display_tool_name(str(last.get('tool') or 'tool'))} · {last.get('status') or 'running'}"
        if self._inner.get(tool_id) != line:
            self._inner[tool_id] = line
            self.status(line)

    def _tool_done(self, tool_id: str, *, ok: bool, text: str) -> None:
        info = self._tools.pop(tool_id, None)
        if info is None:
            self._tool_start(tool_id, "tool", {})
            info = self._tools.pop(tool_id)
        if not info["shown"]:
            label = display_tool_name(info["name"])
            self._emit({"type": "tool", "text": f"⚙ {label}",
                        "tool": {"name": label, "arguments": info["arguments"], "status": "pending"}})
        label = display_tool_name(info["name"])
        ms = int((self.clock() - info["started"]) * 1000)
        status = "success" if ok else "error"
        result = _tail(text)
        if not ok and "declined" in text.lower():
            self.declined.append(label)
        self.blocks.append({
            "type": "tool_call", "id": tool_id, "name": label, "arguments": info["arguments"],
            "started": info["started_wall"], "duration_ms": ms,
            "result": {"ok": ok, "data": result, "hint": ""}, "status": status,
        })
        self._emit({"type": "tool_done", "text": f"⚙ {label} · {status}" + (f" · {ms}ms" if ms else ""),
                    "success": ok,
                    "tool": {"name": label, "arguments": info["arguments"], "status": status,
                             "durationMs": ms, "result": result, "hint": ""}})

    def finish_open_tools(self, note: str) -> None:
        for tool_id in list(self._tools):
            self._tool_done(tool_id, ok=False, text=note)

    def finalize_blocks(self) -> list[dict[str, Any]]:
        self.flush(force=True)
        thinking = "".join(self._seg_thinking).strip()
        if thinking:
            self.blocks.append({"type": "thinking", "text": thinking})
            self._seg_thinking = []
        return self.blocks

    def trailing_text(self) -> str:
        return "".join(self._seg_text).strip()

    def all_text(self) -> str:
        return "".join(self.text_parts).strip()


def replay(lines: list[str], push: Callable[[dict[str, Any]], None]) -> OpenCodeStream:
    """Feed recorded JSON lines through a stream (tests and the recorder)."""
    stream = OpenCodeStream("conv", "run", push)
    for line in lines:
        line = line.strip()
        if line and stream.on_event(json.loads(line)):
            break
    stream.finalize_blocks()
    return stream
