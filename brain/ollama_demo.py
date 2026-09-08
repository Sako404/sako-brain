"""Minimal local Ollama <-> Sako Brain integration demo (Phase 5B).

    question -> BrainGatewayClient.get_context() -> Ollama -> answer

Deliberately **read-only**: this script never writes to the Brain. It
demonstrates that a local model can be grounded in Brain context without
being handed write access. If a future version of this demo wants to add
memory capture, it MUST go through `client.queue_memory()` (Level 2 —
staged for human review), never `remember()`/`update_memory()` — the
Ollama model itself is never a trusted-enough source for an authoritative
write.

Uses only the Python standard library for the Ollama HTTP call (no new
dependency) against `http://127.0.0.1:11434` — Ollama's own default
bind, confirmed local-only on this machine (see `90_SYSTEM/mcp/README.md`).

Usage:
    python3 -m brain.ollama_demo "Where did Family Command Center finish?"
    python3 -m brain.ollama_demo "..." --model llama3:latest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import paths as paths_mod
from .gateway_client import BrainGatewayClient, BrainGatewayError

# Deployment constants, overridable without editing the package (OSS-2/C6).
OLLAMA_URL = os.environ.get("BRAIN_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
DEFAULT_MODEL = os.environ.get("BRAIN_OLLAMA_MODEL", "qwen3.5:latest")
REQUEST_TIMEOUT = 120


class OllamaError(RuntimeError):
    pass


def build_prompt(question: str, context: dict) -> str:
    lines = [
        "You are answering a question using ONLY the context below, drawn "
        "from a personal knowledge base. If the context doesn't contain the "
        "answer, say so plainly rather than guessing. Pay attention to "
        "whether each item is marked current or historical, and prefer "
        "current information unless the question asks about the past.",
        "",
        f"Question: {question}",
        "",
        "Context:",
    ]
    if not context.get("notes"):
        lines.append("(no relevant notes found)")
    for note in context.get("notes", []):
        currency = "current" if note.get("is_current") else "historical/superseded"
        source_bit = f", source: {note['source']}" if note.get("source") else ""
        date_bit = f" ({note['source_date']})" if note.get("source_date") else ""
        lines.append(
            f"- [{note['type']}, {currency}{source_bit}{date_bit}] {note['title']}: {note['snippet']}"
        )
    if context.get("projects"):
        lines.append("")
        lines.append("Related registered projects:")
        for p in context["projects"]:
            lines.append(f"- {p['name']} ({p['status']})")
    if context.get("timeline"):
        lines.append("")
        lines.append("Related timeline events:")
        for t in context["timeline"]:
            lines.append(f"- {t['date']}: {t['title']}")
    if context.get("restricted_omitted"):
        lines.append("")
        lines.append(
            f"(Note: {context['restricted_omitted']} restricted note(s) matched but were "
            "withheld from this context — restricted information is not exposed to this integration.)"
        )
    lines.append("")
    lines.append("Answer:")
    return "\n".join(lines)


def call_ollama(prompt: str, model: str = DEFAULT_MODEL) -> str:
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"could not reach Ollama at {OLLAMA_URL} ({exc}). Is the ollama service running?"
        ) from exc
    return body.get("response", "")


def _log(model: str, note_count: int, success: bool, detail: str = "", log_dir: Path | None = None) -> None:
    """Same redaction principle as the MCP server's own logging — never the
    question text or the model's answer, only shape/outcome."""
    try:
        log_dir = log_dir or paths_mod.default_config().logs_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"ollama-demo-{datetime.now():%Y-%m}.log"
        status = "OK" if success else "FAILED"
        line = f"{datetime.now().isoformat(timespec='seconds')}  model={model}  notes_used={note_count}  {status}"
        if detail:
            line += f"  {detail}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        paths_mod.ensure_private_file(log_path)
    except OSError:
        pass


def answer_question(question: str, model: str = DEFAULT_MODEL, limit: int = 8) -> tuple[str, dict]:
    """Returns (answer, context) — read-only, no Brain writes."""
    with BrainGatewayClient(client_name="ollama-demo") as brain:
        context = brain.get_context(question, limit=limit)

    prompt = build_prompt(question, context)
    try:
        answer = call_ollama(prompt, model=model)
        _log(model, len(context.get("notes", [])), success=True)
    except OllamaError as exc:
        _log(model, len(context.get("notes", [])), success=False, detail=str(exc)[:200])
        raise
    return answer, context


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ask a question grounded in Sako Brain context, answered by a local Ollama model.")
    parser.add_argument("question")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--show-context", action="store_true", help="Print the retrieved context before the answer")
    args = parser.parse_args(argv)

    try:
        answer, context = answer_question(args.question, model=args.model, limit=args.limit)
    except (BrainGatewayError, OllamaError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.show_context:
        print("--- Retrieved context ---")
        for note in context.get("notes", []):
            currency = "current" if note.get("is_current") else "historical"
            print(f"  [{currency}] {note['id']}: {note['title']}")
        print()

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
