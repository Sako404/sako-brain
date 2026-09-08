"""Sako Brain daily assistant (Phase 5C) — `brain ask` / `brain chat`.

    question -> BrainGatewayClient.get_context() -> Ollama -> answer + sources

Builds on the Phase 5B gateway/`get_context`. Adds: a stronger prompt
policy teaching the model to distinguish current vs. historical/
researched/considered-but-not-adopted information, compact source
citations, a context budget (fewer, higher-relevance notes, current facts
ordered first, rather than raw volume), restricted-data opt-in, and
lightweight pending-memory-candidate detection.

Read-only with respect to authoritative Brain records — the only write
path available here is `queue_memory` (Level 2, pending human review),
never `remember`/`update_memory`. Ollama is never a trusted-enough source
for a direct authoritative write (same principle as the Phase 5B demo).

This is a separate module from `ollama_demo.py` (the minimal Phase 5B
demo) rather than a shared refactor of it — `ollama_demo.py` is small,
already tested, and not worth the risk of touching for this phase, whose
stated goal is usability, not re-architecture. Some logic (calling
Ollama, building a context-grounded prompt) is intentionally similar in
spirit but independently implemented here with the fuller Phase 5C
feature set.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paths as paths_mod
from .gateway_client import BrainGatewayClient, BrainGatewayError

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "qwen3.5:latest"  # Phase 5B testing showed this performed best with retrieved Brain context
REQUEST_TIMEOUT = 180

# Context budget: prefer a small number of highly-relevant notes over raw
# volume. get_context already ranks by relevance; ask() additionally
# orders current-before-historical within that budget (see _prioritize).
DEFAULT_CONTEXT_LIMIT = 6

POLICY_PREAMBLE = """You are answering a question using ONLY the structured context below, drawn from a personal knowledge base ("the Brain"). Follow these rules strictly:

- Newer evidence is not automatically better — only prefer it over older evidence when both describe the SAME fact (e.g. a later note about the same employer supersedes an earlier one; a later note about a different topic does not override anything).
- Never present historical or superseded information as if it were current. If a note is marked historical/superseded, say so explicitly when you use it.
- "Latest known" is not the same as "current confirmed". If the context only shows what was true as of a specific date, say "as of <date>, ..." rather than implying it is still true today.
- A vehicle, job, or option that was researched or considered is NOT the same as one that was owned, taken, or adopted. Do not conflate consideration with commitment.
- A project idea or proposal is NOT the same as an adopted decision. Only call something "decided" if the context marks it that way.
- If the context does not contain enough evidence to answer, say so plainly — do not guess or invent plausible-sounding detail.
- Keep the answer concise and direct."""


class OllamaError(RuntimeError):
    pass


class ModelNotFoundError(OllamaError):
    pass


@dataclass
class AskResult:
    answer: str
    sources: list = field(default_factory=list)   # list[dict] — the notes actually shown to the model
    context: dict = field(default_factory=dict)
    restricted_used: bool = False
    queued_memory: dict | None = None
    note_count: int = 0
    duration_seconds: float = 0.0
    no_context: bool = False


# ---- durable-statement heuristic (Level 2 candidate detection) -----------

DURABLE_PATTERNS = [
    re.compile(r"\bi(?:'ve| have)? (?:just )?(?:sold|bought|purchased)\b", re.I),
    re.compile(r"\bi(?:'m| am) no longer\b", re.I),
    re.compile(r"\bi (?:quit|resigned|left|started|joined)\b", re.I),
    re.compile(r"\bwe (?:moved|decided|sold|bought)\b", re.I),
    re.compile(r"\bmy new (?:job|car|address|role|employer)\b", re.I),
    re.compile(r"\bi got a new\b", re.I),
    re.compile(r"\bi'?m now (?:working|driving|living)\b", re.I),
    re.compile(r"\bi don'?t work at\b", re.I),
]


def detect_memory_candidate(text: str) -> str | None:
    """Lightweight regex heuristic, not NLP/ML — deliberately conservative.
    Returns the original text as the candidate fact if it looks like a
    durable personal-fact statement, else None."""
    for pattern in DURABLE_PATTERNS:
        if pattern.search(text):
            return text.strip()
    return None


# ---- Ollama call -----------------------------------------------------------

def call_ollama(prompt: str, model: str = DEFAULT_MODEL) -> str:
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if exc.code == 404 or "not found" in detail.lower():
            raise ModelNotFoundError(
                f"model '{model}' does not appear to be available (HTTP {exc.code}). "
                f"Run 'ollama list' to see installed models, or 'ollama pull {model}' to fetch it "
                "(not done automatically)."
            ) from exc
        raise OllamaError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"could not reach Ollama at {OLLAMA_URL} ({exc.reason if hasattr(exc, 'reason') else exc}). "
            "Is the ollama service running? (systemctl status ollama)"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Ollama returned a malformed (non-JSON) response: {exc}") from exc

    if isinstance(body, dict) and body.get("error"):
        message = str(body["error"])
        if "not found" in message.lower():
            raise ModelNotFoundError(f"model '{model}' not found: {message}")
        raise OllamaError(f"Ollama error: {message}")

    return body.get("response", "") if isinstance(body, dict) else ""


# ---- context shaping / prompt / sources -----------------------------------

def _prioritize(notes: list[dict]) -> list[dict]:
    """Current-before-historical, stable otherwise (preserves the
    relevance ranking get_context already computed within each group)."""
    current = [n for n in notes if n.get("is_current")]
    historical = [n for n in notes if not n.get("is_current")]
    return current + historical


def build_prompt(question: str, context: dict) -> str:
    notes = _prioritize(context.get("notes", []))
    lines = [POLICY_PREAMBLE, "", f"Question: {question}", "", "Context:"]
    if not notes:
        lines.append("(no relevant notes found in the Brain)")
    for note in notes:
        currency = "current" if note.get("is_current") else "historical/superseded"
        bits = []
        if note.get("source"):
            bits.append(f"source: {note['source']}")
        if note.get("source_date"):
            bits.append(f"date: {note['source_date']}")
        if note.get("confidence"):
            bits.append(f"confidence: {note['confidence']}")
        meta = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- [{note['id']} | {note.get('type', '')} | {currency}]{meta} {note['title']}: {note['snippet']}")
    if context.get("projects"):
        lines.append("")
        lines.append("Related registered projects:")
        for p in context["projects"]:
            lines.append(f"- {p['id']}: {p['name']} (status: {p['status']})")
    if context.get("timeline"):
        lines.append("")
        lines.append("Related timeline events:")
        for t in context["timeline"]:
            lines.append(f"- {t['id']} ({t['date']}): {t['title']}")
    if context.get("restricted_omitted"):
        lines.append("")
        lines.append(
            f"({context['restricted_omitted']} restricted note(s) matched but were withheld — not used in this answer.)"
        )
    lines.append("")
    lines.append("Answer:")
    return "\n".join(lines)


def format_sources(notes: list[dict]) -> str:
    if not notes:
        return "Sources: (none)"
    lines = ["Sources:"]
    for n in notes:
        bits = []
        if n.get("source_date"):
            bits.append(n["source_date"])
        if n.get("confidence"):
            bits.append(n["confidence"])
        meta = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- {n['id']}{meta}")
    return "\n".join(lines)


# ---- logging (Phase 5C: adds duration; never content) --------------------

def _resolve_log_dir(brain_root=None) -> Path:
    """Logs are runtime state, so they follow the vault's configured state
    directory — never the package's own location on disk (OSS-1)."""
    return paths_mod.default_config(brain_root).logs_dir


def _log(model: str, note_count: int, success: bool, duration: float,
          detail: str = "", log_dir: Path | None = None) -> None:
    try:
        log_dir = log_dir or _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"assistant-{datetime.now():%Y-%m}.log"
        status = "OK" if success else "FAILED"
        line = (
            f"{datetime.now().isoformat(timespec='seconds')}  model={model}  "
            f"notes={note_count}  duration={duration:.2f}s  {status}"
        )
        if detail:
            line += f"  {detail}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        paths_mod.ensure_private_file(log_path)
    except OSError:
        pass


# ---- the main entry point --------------------------------------------------

def ask(question: str, model: str = DEFAULT_MODEL, limit: int = DEFAULT_CONTEXT_LIMIT,
        include_restricted: bool = False, brain_root=None) -> AskResult:
    start = time.monotonic()

    with BrainGatewayClient(client_name="brain-ask", brain_root=brain_root) as brain:
        context = brain.get_context(question, limit=limit, include_restricted=include_restricted)

        queued = None
        candidate = detect_memory_candidate(question)
        if candidate:
            try:
                queued = brain.queue_memory(
                    candidate_fact=candidate, source="brain ask/chat conversation",
                    confidence="assumption",
                    reason="statement in a brain-ask/chat question looked like a durable personal-fact change",
                )
            except BrainGatewayError:
                queued = None  # never let candidate-queueing break the main answer flow

    notes = _prioritize(context.get("notes", []))[:limit]
    restricted_used = include_restricted and any(n.get("sensitivity") == "restricted" for n in notes)

    total_matches = len(context.get("notes", [])) + len(context.get("projects", [])) + len(context.get("timeline", []))
    if total_matches == 0:
        duration = time.monotonic() - start
        _log(model, 0, success=True, duration=duration, detail="no-context",
             log_dir=_resolve_log_dir(brain_root))
        return AskResult(
            answer=(
                "I couldn't find any relevant Brain context for this question, so I'm not going to guess. "
                "Try rephrasing, or use 'brain search \"...\"' to explore what's actually recorded."
            ),
            sources=[], context=context, restricted_used=include_restricted,
            queued_memory=queued, note_count=0, duration_seconds=duration, no_context=True,
        )

    prompt = build_prompt(question, context)
    try:
        answer = call_ollama(prompt, model=model)
    except OllamaError as exc:
        duration = time.monotonic() - start
        _log(model, len(notes), success=False, duration=duration, detail=str(exc)[:200],
             log_dir=_resolve_log_dir(brain_root))
        raise

    duration = time.monotonic() - start
    _log(model, len(notes), success=True, duration=duration,
         log_dir=_resolve_log_dir(brain_root))

    return AskResult(
        answer=answer, sources=notes, context=context, restricted_used=restricted_used,
        queued_memory=queued, note_count=len(notes), duration_seconds=duration,
    )


# ---- brain chat: interactive REPL, in-memory only -------------------------

CHAT_HELP = "Commands: /sources /context /model <name> /restricted on|off /exit"


def run_chat(model: str = DEFAULT_MODEL, include_restricted: bool = False, brain_root=None,
             input_fn=input, print_fn=print) -> list[dict]:
    """Interactive loop. Session state (history, last answer/sources) lives
    in memory only for the duration of this call — nothing is written to
    disk or the Brain by the chat loop itself. Each question retrieves
    FRESH Brain context; earlier chat turns are never fed back in as if
    they were established Brain facts — every `ask()` call here is
    independent, exactly like a fresh `brain ask` invocation.

    Returns the in-memory history list (mainly for tests) — the caller is
    responsible for discarding it; this function never persists it.
    """
    print_fn(f"{paths_mod.APP_NAME} chat — model={model}, restricted={'on' if include_restricted else 'off'}")
    print_fn(CHAT_HELP)
    print_fn("(Nothing in this session is saved automatically — it exists only for this conversation.)")
    print_fn()

    state = {"model": model, "restricted": include_restricted, "last_result": None}
    history: list[dict] = []

    while True:
        try:
            line = input_fn("> ")
        except (EOFError, KeyboardInterrupt):
            print_fn()
            break
        line = line.strip()
        if not line:
            continue

        if line == "/exit":
            break

        if line == "/sources":
            if state["last_result"] is None:
                print_fn("No question asked yet.")
            else:
                print_fn(format_sources(state["last_result"].sources))
            continue

        if line == "/context":
            if state["last_result"] is None:
                print_fn("No question asked yet.")
            else:
                for n in state["last_result"].sources:
                    currency = "current" if n.get("is_current") else "historical"
                    print_fn(f"  [{currency}] {n['id']}: {n['title']}")
            continue

        if line.startswith("/model"):
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                state["model"] = parts[1].strip()
                print_fn(f"Model set to {state['model']}")
            else:
                print_fn(f"Current model: {state['model']}")
            continue

        if line.startswith("/restricted"):
            parts = line.split(maxsplit=1)
            choice = parts[1].strip().lower() if len(parts) == 2 else ""
            if choice in ("on", "off"):
                state["restricted"] = choice == "on"
                print_fn(f"Restricted context: {'on' if state['restricted'] else 'off'}")
            else:
                print_fn("Usage: /restricted on|off")
            continue

        if line.startswith("/"):
            print_fn(f"Unknown command '{line}'. {CHAT_HELP}")
            continue

        try:
            result = ask(line, model=state["model"], include_restricted=state["restricted"], brain_root=brain_root)
        except OllamaError as exc:
            print_fn(f"Error: {exc}")
            continue
        except BrainGatewayError as exc:
            print_fn(f"Error talking to the Brain gateway: {exc}")
            continue

        state["last_result"] = result
        history.append({"question": line, "answer": result.answer})  # in-memory only, this call's lifetime

        print_fn(result.answer)
        if not result.no_context:
            print_fn()
            print_fn(format_sources(result.sources))
        if result.restricted_used:
            print_fn()
            print_fn("(Restricted Brain context was included in this answer.)")
        if result.queued_memory:
            print_fn()
            print_fn(f"Queued pending-memory candidate: {result.queued_memory.get('queued_id')} "
                      f"(review with 'brain memory review')")
        print_fn()

    return history
