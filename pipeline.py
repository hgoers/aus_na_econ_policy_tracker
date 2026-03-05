#!/usr/bin/env python3
"""Daily Economic Intelligence Pipeline runner.

This script executes the prompt-defined Anthropic agent in a tool-use loop and
implements four tools: web_search, read_file, write_file, send_email.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import time
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from anthropic import Anthropic
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS
try:
    import markdown as md
except ImportError:  # pragma: no cover
    md = None
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from zoneinfo import ZoneInfo

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


TOOL_DEFS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use for retrieving news, policy "
            "announcements, data releases, and content from primary government and "
            "institutional sources. Always prefer primary sources over press aggregators. "
            "Each search should be targeted and specific - do not use broad queries when "
            "a targeted query will suffice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query. Keep it specific and targeted. For primary "
                        "source retrieval, include the site domain where possible."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default 5.",
                    "default": 5,
                },
                "recency_hours": {
                    "type": "integer",
                    "description": "Optional freshness filter in hours (e.g. 24 for last day).",
                },
                "after_date": {
                    "type": "string",
                    "description": "Optional lower date bound (YYYY-MM-DD or ISO datetime).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file from the local file system.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path from the pipeline working directory.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write or append content to a file on the local file system. Specify the "
            "write mode carefully."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path from the pipeline working directory.",
                },
                "content": {
                    "type": "string",
                    "description": "The full content to write or append.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["write", "append"],
                    "description": "'write' overwrites, 'append' appends.",
                    "default": "write",
                },
            },
            "required": ["path", "content", "mode"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Send the completed Morning Brief by email after quality checks and file saves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address."
                },
                "subject": {
                    "type": "string",
                    "description": "Email subject line."
                },
                "body": {
                    "type": "string",
                    "description": "The full Morning Brief content."
                },
                "cc": {
                    "type": "string",
                    "description": "Optional CC address.",
                    "default": "",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
]


@dataclass
class PipelineConfig:
    root: Path
    prompt_file: Path
    model: str
    max_tokens: int
    timezone: str
    dry_run_email: bool
    max_turns: int
    max_web_search_calls: int


class PipelineError(RuntimeError):
    pass


@dataclass
class RunState:
    staged_writes: dict[str, str]
    web_cache: dict[str, dict[str, Any]]
    web_search_calls: int
    web_search_calls_by_phase: dict[int, int]
    phase: int
    email_step_completed: bool


PHASE_WEB_SEARCH_LIMITS = {1: 25, 2: 35, 3: 10, 4: 5}


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def ensure_required_structure(root: Path) -> None:
    briefs_dir = root / "briefs"
    logs_dir = root / "logs"
    data_dir = root / "data"

    (briefs_dir / "failed_delivery").mkdir(parents=True, exist_ok=True)
    (logs_dir / "api_archive").mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    brief_index = briefs_dir / "brief_index.md"
    if not brief_index.exists():
        brief_index.write_text(
            "# Brief Index\n"
            "BRIEF-2026-000 | 2026-01-01 | LEAD: Initial placeholder entry\n",
            encoding="utf-8",
        )

    running_notes = logs_dir / "running_notes.md"
    if not running_notes.exists():
        running_notes.write_text(
            "---\n"
            "Initial running notes file created.\n"
            "---\n",
            encoding="utf-8",
        )

    pipeline_log = logs_dir / "pipeline_log.md"
    if not pipeline_log.exists():
        pipeline_log.write_text("", encoding="utf-8")

    trade_register = data_dir / "trade_exposure_register.md"
    if not trade_register.exists():
        trade_register.write_text(
            "# Trade Exposure Register\n"
            "\n"
            "Placeholder: populate with sector-level Australian exposure data (quarterly update).\n",
            encoding="utf-8",
        )


def extract_system_prompt(prompt_text: str) -> str:
    marker = "# PART 1 — SYSTEM PROMPT"
    start = prompt_text.find(marker)
    if start == -1:
        raise PipelineError("Could not locate Part 1 in prompt.txt")
    part = prompt_text[start:]
    body = part.replace(marker, "", 1).lstrip()
    # Runtime-only prompt format: no wrapper fence at the top.
    if not body.startswith("```"):
        return body.strip()

    # Backward-compatible legacy format: system prompt wrapped in first top-level fence.
    fence_start = part.find("```")
    fence_end = part.find("```", fence_start + 3)
    if fence_start != -1 and fence_end != -1:
        return part[fence_start + 3 : fence_end].strip()
    raise PipelineError("Could not extract system prompt from prompt.txt")


def build_invocation_message(tz_name: str) -> str:
    now = dt.datetime.now(ZoneInfo(tz_name))
    date_str = now.strftime("%A %d %B %Y")
    time_str = now.strftime("%I:%M %p %Z")
    return (
        f"Execute the Daily Economic Intelligence Pipeline for today, {date_str}.\n\n"
        f"Washington DC local time: {time_str}\n"
        "Brief number: auto-increment from index\n\n"
        "Run all four phases in sequence. Do not skip steps. Apply all writing "
        "discipline checks before sending. Deliver the completed brief by email on completion."
    )


def safe_rel_path(root: Path, rel_path: str) -> Path:
    if not rel_path:
        raise PipelineError("Path cannot be empty")
    full_path = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if root_resolved not in full_path.parents and full_path != root_resolved:
        raise PipelineError(f"Path escapes workspace: {rel_path}")
    return full_path


def _best_effort_parse_date(value: str) -> dt.datetime | None:
    if not value:
        return None
    value = value.strip()
    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a, %d %b %Y %H:%M:%S %Z",
    ]
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(value, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed
        except ValueError:
            continue
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


def _retry_with_backoff(fn: Any, attempts: int = 3, base_delay: float = 1.0) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == attempts:
                break
            time.sleep(base_delay * (2 ** (attempt - 1)))
    raise PipelineError(f"Operation failed after {attempts} attempts: {last_exc}")


def _update_phase(state: RunState, assistant_text_blocks: list[str]) -> None:
    combined = "\n".join(assistant_text_blocks).upper()
    if "PHASE 4" in combined:
        state.phase = 4
    elif "PHASE 3" in combined:
        state.phase = 3
    elif "PHASE 2" in combined:
        state.phase = 2
    elif "PHASE 1" in combined:
        state.phase = 1


def _filter_results_by_date(
    results: list[dict[str, str]], recency_hours: int | None, after_date: str | None
) -> list[dict[str, str]]:
    if recency_hours is None and not after_date:
        return results

    cutoff: dt.datetime | None = None
    if recency_hours is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(1, recency_hours))

    after_dt = _best_effort_parse_date(after_date or "")
    filtered: list[dict[str, str]] = []
    for item in results:
        published_raw = item.get("published", "")
        published_dt = _best_effort_parse_date(published_raw)
        if published_dt is None:
            filtered.append(item)
            continue
        if cutoff and published_dt < cutoff:
            continue
        if after_dt and published_dt < after_dt:
            continue
        filtered.append(item)
    return filtered


def tool_web_search(config: PipelineConfig, state: RunState, payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    if not query:
        raise PipelineError("web_search.query is required")

    max_results = int(payload.get("max_results", 5))
    max_results = max(1, min(max_results, 20))
    recency_hours_raw = payload.get("recency_hours")
    recency_hours = int(recency_hours_raw) if recency_hours_raw is not None else None
    after_date = str(payload.get("after_date", "")).strip() or None
    cache_key = json.dumps(
        {
            "q": query,
            "n": max_results,
            "h": recency_hours,
            "a": after_date,
        },
        sort_keys=True,
    )

    if cache_key in state.web_cache:
        cached = dict(state.web_cache[cache_key])
        cached["cached"] = True
        return cached

    phase_limit = PHASE_WEB_SEARCH_LIMITS.get(state.phase, config.max_web_search_calls)
    phase_count = state.web_search_calls_by_phase.get(state.phase, 0)
    if phase_count >= phase_limit:
        raise PipelineError(
            f"Web search budget exceeded for phase {state.phase} (limit {phase_limit}). "
            "Use existing findings and proceed."
        )
    if state.web_search_calls >= config.max_web_search_calls:
        raise PipelineError(
            f"Web search budget exceeded for run (limit {config.max_web_search_calls})."
        )

    results: list[dict[str, str]] = []
    def _search() -> list[dict[str, str]]:
        tmp: list[dict[str, str]] = []
        with DDGS() as ddgs:
            for hit in ddgs.text(query, max_results=max_results):
                tmp.append(
                    {
                        "title": hit.get("title", ""),
                        "url": hit.get("href", ""),
                        "snippet": hit.get("body", ""),
                        "published": hit.get("date", ""),
                    }
                )
        return tmp

    results = _retry_with_backoff(_search, attempts=3, base_delay=1.0)
    results = _filter_results_by_date(results, recency_hours=recency_hours, after_date=after_date)
    state.web_search_calls += 1
    state.web_search_calls_by_phase[state.phase] = phase_count + 1

    response = {
        "query": query,
        "count": len(results),
        "results": results,
        "phase": state.phase,
        "search_calls_used": state.web_search_calls,
        "search_calls_limit": config.max_web_search_calls,
        "cached": False,
        "filters": {"recency_hours": recency_hours, "after_date": after_date},
    }
    state.web_cache[cache_key] = response
    return response


def tool_read_file(root: Path, state: RunState, payload: dict[str, Any]) -> dict[str, Any]:
    rel_path = str(payload.get("path", "")).strip()
    target = safe_rel_path(root, rel_path)
    staged_key = str(target)
    if staged_key in state.staged_writes:
        return {"path": rel_path, "exists": True, "content": state.staged_writes[staged_key], "staged": True}
    if not target.exists():
        return {"path": rel_path, "exists": False, "content": ""}
    content = target.read_text(encoding="utf-8")
    return {"path": rel_path, "exists": True, "content": content, "staged": False}


def tool_write_file(root: Path, state: RunState, payload: dict[str, Any]) -> dict[str, Any]:
    rel_path = str(payload.get("path", "")).strip()
    content = str(payload.get("content", ""))
    mode = str(payload.get("mode", "write")).strip()

    if mode not in {"write", "append"}:
        raise PipelineError("write_file.mode must be 'write' or 'append'")

    target = safe_rel_path(root, rel_path)
    staged_key = str(target)

    if mode == "write":
        state.staged_writes[staged_key] = content
    else:
        existing = state.staged_writes.get(staged_key)
        if existing is None:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
        state.staged_writes[staged_key] = existing + content

    return {
        "path": rel_path,
        "mode": mode,
        "bytes": len(content.encode("utf-8")),
        "staged": True,
    }


def _resolve_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (root / candidate).resolve()


def _markdown_to_html(markdown_text: str) -> str:
    if md is not None:
        rendered = md.markdown(
            markdown_text,
            extensions=["extra", "sane_lists", "nl2br"],
        )
    else:
        escaped = (
            markdown_text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        rendered = f"<pre>{escaped}</pre>"

    return (
        "<html><body style=\"font-family: Arial, sans-serif; line-height: 1.5;\">"
        f"{rendered}"
        "</body></html>"
    )


def _strip_email_header_lines(markdown_text: str) -> str:
    out: list[str] = []
    for line in markdown_text.splitlines():
        normalized = line.strip().lower()
        if normalized.startswith("for:") or normalized.startswith("**for**:"):
            continue
        out.append(line)
    return "\n".join(out)


def _load_gmail_credentials(root: Path) -> Credentials:
    token_file = os.getenv("GMAIL_TOKEN_FILE", ".secrets/gmail_token.json").strip()
    token_path = _resolve_path(root, token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if creds and creds.valid:
        return creds

    raise PipelineError(
        f"Gmail token not valid or missing at '{token_path}'. "
        "Run: python3 pipeline.py --init-gmail-auth"
    )


def initialize_gmail_auth(root: Path) -> Path:
    credentials_file = os.getenv(
        "GMAIL_CREDENTIALS_FILE", ".secrets/gmail_credentials.json"
    ).strip()
    token_file = os.getenv("GMAIL_TOKEN_FILE", ".secrets/gmail_token.json").strip()
    creds_path = _resolve_path(root, credentials_file)
    token_path = _resolve_path(root, token_file)

    if not creds_path.exists():
        raise PipelineError(
            f"Gmail OAuth client file not found: '{creds_path}'. "
            "Set GMAIL_CREDENTIALS_FILE to your Google OAuth client JSON."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return token_path


def tool_send_email(root: Path, payload: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    configured_to = os.getenv("RECIPIENT_EMAIL", "").strip()
    to_addr = configured_to
    subject = str(payload.get("subject", "")).strip()
    body = str(payload.get("body", ""))
    email_body = _strip_email_header_lines(body)
    cc_addr = str(payload.get("cc", "")).strip() or os.getenv("CC_EMAIL", "").strip()

    if not to_addr:
        raise PipelineError("Recipient missing: set RECIPIENT_EMAIL in .env.")
    if not subject:
        raise PipelineError("Email subject is required")

    if dry_run:
        result = {
            "sent": False,
            "dry_run": True,
            "to": to_addr,
            "cc": cc_addr,
            "subject": subject,
            "body_preview": email_body[:300],
        }
        return result

    from_addr = os.getenv("GMAIL_SENDER", "").strip() or os.getenv("EMAIL_FROM", "").strip() or to_addr
    creds = _load_gmail_credentials(root)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    if cc_addr:
        msg["Cc"] = cc_addr
    msg.set_content(email_body)
    msg.add_alternative(_markdown_to_html(email_body), subtype="html")
    encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    def _send() -> dict[str, Any]:
        return (
            service.users()
            .messages()
            .send(userId="me", body={"raw": encoded_message})
            .execute()
        )

    sent = _retry_with_backoff(_send, attempts=3, base_delay=1.0)

    return {
        "sent": True,
        "dry_run": False,
        "to": to_addr,
        "cc": cc_addr,
        "subject": subject,
        "gmail_message_id": sent.get("id", ""),
    }


def call_tool(config: PipelineConfig, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if name == "web_search":
        return tool_web_search(config, config_state(), payload)
    if name == "read_file":
        return tool_read_file(config.root, config_state(), payload)
    if name == "write_file":
        return tool_write_file(config.root, config_state(), payload)
    if name == "send_email":
        result = tool_send_email(config.root, payload, dry_run=config.dry_run_email)
        config_state().email_step_completed = True
        return result
    raise PipelineError(f"Unknown tool requested by model: {name}")


_RUN_STATE: RunState | None = None


def config_state() -> RunState:
    global _RUN_STATE
    if _RUN_STATE is None:
        raise PipelineError("Run state is not initialized")
    return _RUN_STATE


def flush_staged_writes(root: Path, state: RunState) -> int:
    written = 0
    for path_str, content in state.staged_writes.items():
        path = Path(path_str)
        if root.resolve() not in path.resolve().parents and path.resolve() != root.resolve():
            raise PipelineError(f"Staged path escaped workspace: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written += 1
    return written


def response_to_dict(resp: Any) -> dict[str, Any]:
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if hasattr(resp, "to_dict"):
        return resp.to_dict()
    if isinstance(resp, dict):
        return resp
    return {"repr": repr(resp)}


def append_pipeline_log(root: Path, line: str) -> None:
    log_path = root / "logs" / "pipeline_log.md"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def run_pipeline(config: PipelineConfig) -> str:
    global _RUN_STATE
    load_dotenv(config.root / ".env")
    ensure_required_structure(config.root)
    _RUN_STATE = RunState(
        staged_writes={},
        web_cache={},
        web_search_calls=0,
        web_search_calls_by_phase={1: 0, 2: 0, 3: 0, 4: 0},
        phase=1,
        email_step_completed=False,
    )

    api_key = os.getenv("ANTHROPIC_API_KEY_POLICY_TRACKER", "").strip()
    if not api_key:
        raise PipelineError("ANTHROPIC_API_KEY_POLICY_TRACKER is not set")

    prompt_text = config.prompt_file.read_text(encoding="utf-8")
    system_prompt = extract_system_prompt(prompt_text)
    invocation = build_invocation_message(config.timezone)

    client = Anthropic(api_key=api_key)
    messages: list[dict[str, Any]] = [{"role": "user", "content": invocation}]

    started = dt.datetime.now(dt.timezone.utc)
    final_text_parts: list[str] = []

    for turn in range(1, config.max_turns + 1):
        response = _retry_with_backoff(
            lambda: client.messages.create(
                model=config.model,
                max_tokens=config.max_tokens,
                system=system_prompt,
                tools=TOOL_DEFS,
                messages=messages,
            ),
            attempts=3,
            base_delay=1.0,
        )

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_path = config.root / "logs" / "api_archive" / f"response_{stamp}_turn{turn:02d}.json"
        archive_path.write_text(json.dumps(response_to_dict(response), indent=2), encoding="utf-8")

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        text_blocks = [block.text for block in assistant_content if getattr(block, "type", "") == "text"]
        if text_blocks:
            final_text_parts.extend(text_blocks)
            _update_phase(config_state(), text_blocks)

        tool_uses = [block for block in assistant_content if getattr(block, "type", "") == "tool_use"]
        if not tool_uses:
            state = config_state()
            if state.staged_writes and not state.email_step_completed:
                raise PipelineError(
                    "Model ended without completing email step; staged file changes were not committed."
                )
            if state.staged_writes:
                flush_staged_writes(config.root, state)
            runtime_s = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds())
            append_pipeline_log(
                config.root,
                f"[{dt.datetime.now().isoformat()}] | PIPELINE RUN END | Turns: {turn} | Runtime: {runtime_s}s | Dry run email: {'Y' if config.dry_run_email else 'N'}",
            )
            return "\n".join(final_text_parts).strip()

        tool_results: list[dict[str, Any]] = []
        for tool_call in tool_uses:
            name = tool_call.name
            payload = tool_call.input
            tool_use_id = tool_call.id
            try:
                result = call_tool(config, name, payload)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": True,
                        "content": json.dumps({"error": str(exc)}, ensure_ascii=False),
                    }
                )

        messages.append({"role": "user", "content": tool_results})

    raise PipelineError(f"Exceeded max turns ({config.max_turns}) without completion")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Daily Economic Intelligence Pipeline")
    parser.add_argument("--root", default=".", help="Pipeline working directory")
    parser.add_argument("--prompt-file", default="prompt.txt", help="Path to prompt file")
    parser.add_argument("--model", default="claude-sonnet-4-20250514", help="Anthropic model")
    parser.add_argument("--max-tokens", type=int, default=16000, help="Max output tokens per API turn")
    parser.add_argument("--timezone", default="America/New_York", help="Timezone for invocation timestamp")
    parser.add_argument("--max-turns", type=int, default=100, help="Maximum tool loop turns")
    parser.add_argument(
        "--max-web-search-calls",
        type=int,
        default=60,
        help="Maximum number of web_search tool calls per run.",
    )
    parser.add_argument(
        "--dry-run-email",
        action="store_true",
        help="Do not send live email; return a simulated send result",
    )
    parser.add_argument(
        "--init-gmail-auth",
        action="store_true",
        help="Run interactive OAuth flow and store Gmail token JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root_path = Path(args.root).resolve()
    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = (root_path / prompt_path).resolve()

    config = PipelineConfig(
        root=root_path,
        prompt_file=prompt_path,
        model=args.model,
        max_tokens=args.max_tokens,
        timezone=args.timezone,
        dry_run_email=bool(args.dry_run_email),
        max_turns=args.max_turns,
        max_web_search_calls=args.max_web_search_calls,
    )

    try:
        if args.init_gmail_auth:
            token_path = initialize_gmail_auth(config.root)
            print(f"Gmail token written to: {token_path}")
            return 0
        result = run_pipeline(config)
        if result:
            print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        append_pipeline_log(
            config.root,
            f"[{dt.datetime.now().isoformat()}] | PIPELINE ERROR | {exc}",
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
