"""The minimal agent loop shared by chat bots and "bot only" sessions.

model -> (tool calls -> tool results)* -> final text. Thinking from earlier steps of the same turn is sent
back as `reasoning_content` (only the last few steps, trimmed), which this server handles well.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .llm import Cancelled  # noqa: F401  (re-exported for callers)

KEEP_REASONING_STEPS = 3
REASONING_CHARS = 6000
TOOL_RESULT_CHARS = 14000      # only when the model's context window is unknown (test fakes)


@dataclass
class ToolResult:
    content: str                    # what the model sees
    stop: bool = False              # end the turn after this tool (ask_user, approvals)
    chip: dict = field(default_factory=dict)   # what the phone shows (label, detail, status)
    record: str | None = None        # full guarded original; never sent as a nonstandard API field
    protect: bool = False            # unresolved errors, approval waits, current test evidence
    record_offset: int = 0           # where `content` starts inside the archived record text
    images: list = field(default_factory=list)   # [{"mime", "data" (base64)}]: shown to the model after the results


@dataclass
class LoopResult:
    text: str
    stop_reason: str                # done | ask | max_steps
    steps: int
    input_tokens: int
    output_tokens: int
    calls: int
    first_input_tokens: int = 0     # prompt tokens of the first call (to correct the context estimate)
    step_texts: list = field(default_factory=list)  # the text of each step (the last one is the answer)
    tools_called: list = field(default_factory=list)
    claims: list = field(default_factory=list)      # said-but-not-done findings that were sent back to the model


# What a reply may claim, and the tool that must have been called for it (measured: "I'll remember that"
# without write_memory, twice). Checked once per turn; the model is asked to do it or correct itself.
CLAIMS = [
    ("write_memory", "that you will remember something",
     re.compile(r"\b(i'?ll remember|i will remember|i'?ve (noted|saved|remembered)|noted that|saved (it|that|this) (to|in) "
                r"(my )?memory|keep (that|this|it) in mind|added (it|that|this) to my memory)\b|覚えておきます|覚えました|"
                r"記憶しました|記憶しておきます|メモしておきます|メモしました|記憶に(残し|書き|保存)", re.I)),
    ("update_dashboard", "that you changed the dashboard",
     re.compile(r"\b(updated|changed|added (it )?to|put (it|that|this) on) the dashboard\b|ダッシュボードを(更新|書き換え)しました|"
                r"ダッシュボードに(出し|載せ|追加し)ました", re.I)),
    ("start_session", "that you started a session",
     re.compile(r"\b(i'?ve |i have )?(started|kicked off|launched) (a|the|your) (new )?(codex |coding |bot )?session\b|"
                r"セッションを(始めました|開始しました|立ち上げました|起動しました)", re.I)),
]


def claim_gaps(text: str, called: set[str], available: set[str], extra: list | None = None) -> list[tuple[str, str]]:
    """extra: (tool names, which to call, what is claimed, rule) entries, e.g. for MCP services (mcp_client)."""
    out = []
    for tool, what, rx in CLAIMS:
        if tool in available and tool not in called and rx.search(text or ""):
            out.append((tool, what))
    for tools, call, what, rule in extra or []:
        if tools & available and not tools & called and rule.search(text or ""):
            out.append((call, what))
    return out


def _compact(messages: list[dict], limit_tokens: int, keep_last: int = 2,
             references: dict | None = None) -> bool:
    """Shorten older tool results when the conversation grows past limit_tokens (estimate)."""
    from .provider_protocol import estimate_input
    total = estimate_input({"messages": messages})      # an image counts as an image, not as its base64 text
    if total <= limit_tokens:
        return False
    tools = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    changed = False
    for i in tools[:-keep_last] if keep_last else tools:
        c = messages[i].get("content") or ""
        ref = (references or {}).get(messages[i].get("tool_call_id"))
        if ref:
            if ref.get("protect"):
                continue
            if len(c) > 600 and not c.startswith("[Archived tool result"):
                messages[i]["content"] = (f"[Archived tool result {ref['record']}] "
                    "Untrusted reference material. Read the original with recall(record=\""
                    + ref["record"] + "\", from_char=0) when needed.\n" + c[:160])
                changed = True
            continue
        # Without a durable reference, retaining the original is safer than destructive clipping.
        if references is not None:
            continue
        if len(c) > 600 and not c.startswith("[earlier tool result, shortened]"):
            messages[i]["content"] = "[earlier tool result, shortened] " + c[:400] + " …"
            changed = True
    return changed


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    return text[:head] + f"\n…[{len(text) - limit} characters cut]…\n" + text[-(limit - head):]


def fit_tool_result(res: "ToolResult", messages: list[dict], tools, llm, max_tokens: int) -> str:
    """A tool result as large as the model's context window allows (measured: a fixed 14,000-character cut dropped
    45,000 characters of 140 X posts while most of a 131K-token window was free). What still does not fit is not
    lost: the start stays whole and a note says where the archived original continues."""
    content = res.content or ""
    cfg = getattr(llm, "api_cfg", None)
    window = cfg.get("context_limit") if isinstance(cfg, dict) else None
    if not isinstance(window, int) or window <= 0:
        return clip(content, TOOL_RESULT_CHARS)
    from .context import estimate_tokens
    from .provider_protocol import estimate_input
    reply = max(1, min(int(max_tokens or 4096), 32768, window // 4))
    room = window - estimate_input({"messages": messages, "tools": tools}) - reply - min(1024, window // 10) - 512
    need = estimate_tokens(content)
    if need <= room:
        return content
    keep = max(0, int(len(content) * max(room - 200, 0) / max(need, 1) * 0.97))
    if res.record:
        where = (f"The full result is record {res.record}; read on with recall(record=\"{res.record}\", "
                 f"from_char={res.record_offset + keep}).")
    else:
        where = "The rest could not be kept; ask for a narrower result (fewer items or one source at a time)."
    return (content[:keep] + f"\n\n[SnapLab: this result has {len(content)} characters and only the first {keep} fit "
            f"in your context window now. {where}]")


def _trim_reasoning(messages: list[dict]) -> None:
    idx = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("reasoning_content")]
    for i in idx[:-KEEP_REASONING_STEPS]:
        messages[i].pop("reasoning_content", None)


def step_cap(value) -> int:
    """Tool steps for one turn from a config value; 0 = no limit (a turn still ends when the model stops calling
    tools, and the user can stop it)."""
    from .config import limit
    cap = limit(value)
    return 10**9 if cap == float("inf") else int(cap)


def run_loop(llm, messages: list[dict], tools: list[dict] | None, execute, *, effort: str, max_steps: int,
             max_tokens: int, source: str, cancel=None, on_reasoning=None, on_content=None, on_step=None,
             kind: str = "chat", on_wait=None, effort_after_tools: str | None = None, check_claims: bool = False,
             compact_at: int | None = None, final_check=None, tool_references: dict | None = None, caller_id: str = "",
             extra_claims=None) -> LoopResult:
    """execute(name, args: dict | None (None = arguments were not valid JSON), call_id) -> ToolResult. on_step(step_index) is called before each model call.

    effort_after_tools: thinking depth once the turn has called a tool (the first call uses `effort`).
    check_claims: if the final reply says it remembered / changed the dashboard / started a session without
    calling that tool, send it back once to do it or correct itself. compact_at: shorten older tool results
    when the conversation passes this many tokens (estimate). final_check(text) -> note or None: one more
    check of the final reply (used for the reply language); the note is sent back once."""
    text_parts: list[str] = []
    tin = tout = calls = first_in = 0
    called: list[str] = []
    claims: list = []
    step_texts: list[str] = []
    checked = False
    references = tool_references if tool_references is not None else {}
    available = {t["function"]["name"] for t in tools or []}
    for step in range(max_steps + 1):
        final = step == max_steps
        if final:
            messages.append({"role": "user", "content": "[SnapLab] Step limit reached. Reply now with what you have "
                                                        "and what is left, without calling tools."})
        if on_step:
            on_step(step)
        _trim_reasoning(messages)
        if compact_at:
            _compact(messages, compact_at, references=references)
        depth = effort_after_tools if (effort_after_tools and called) else effort
        from .qwen_log import caller_id as log_caller
        token = log_caller.set(caller_id)
        try:
            r = llm.stream_chat(messages, tools=None if final else (tools or None), effort=depth, max_tokens=max_tokens,
                                on_reasoning=on_reasoning, on_content=on_content, cancel=cancel, source=source,
                                kind=kind, on_wait=on_wait)
        finally:
            log_caller.reset(token)
        if calls == 0:
            first_in = r.input_tokens
        calls += 1
        tin += r.input_tokens
        tout += r.output_tokens
        a: dict = {"role": "assistant", "content": r.content or ""}
        if r.reasoning:
            a["reasoning_content"] = clip(r.reasoning, REASONING_CHARS)
        if r.tool_calls and not final:
            used_ids = {c["id"] for m in messages for c in m.get("tool_calls") or []}
            for number, c in enumerate(r.tool_calls):
                while c["id"] in used_ids:
                    c["id"] += f"_{step}_{number}"
                used_ids.add(c["id"])
            a["tool_calls"] = [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]}} for c in r.tool_calls]
        messages.append(a)
        if r.content:
            text_parts.append(r.content)
        step_texts.append(r.content or "")
        if final or not r.tool_calls:
            if check_claims and not final and not claims:
                gaps = claim_gaps("\n".join(text_parts), set(called), available, extra_claims)
                if gaps:
                    claims.extend(t for t, _ in gaps)
                    tool, what = gaps[0]
                    messages.append({"role": "user", "content": f"[SnapLab check] Your reply says {what}, but you did not "
                                     f"call {tool} in this turn. If you meant it, call {tool} now. If not, reply with one "
                                     "short line that corrects what you said."})
                    continue
            if final_check and not final and not checked:
                checked = True
                note = final_check(r.content or "")
                if note:
                    claims.append("language")
                    text_parts.pop() if text_parts and text_parts[-1] == r.content else None
                    messages.append({"role": "user", "content": note})
                    continue
            return LoopResult("\n\n".join(text_parts).strip(), "max_steps" if final else "done", step + 1, tin, tout, calls,
                              first_in, step_texts, called, claims)
        stop = False
        shown: list = []
        for c in r.tool_calls:
            if cancel and cancel():
                raise Cancelled()
            try:
                args = json.loads(c["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except ValueError:
                args = None          # the executor reports bad arguments back to the model
            called.append(c["name"])
            res = execute(c["name"], args, c["id"])
            if res.record:
                references[c["id"]] = {"record": res.record, "protect": res.protect}
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": fit_tool_result(res, messages, tools, llm, max_tokens)})
            stop = stop or res.stop
            shown.extend(res.images or [])
            if stop:
                break
        if shown:       # tool messages carry text only: images a tool returned follow them as one user message
            messages.append({"role": "user", "content": [{"type": "text", "text": "[SnapLab] Images from the tool results above:"}]
                             + [{"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['data']}"}} for i in shown]})
        if stop:
            return LoopResult("\n\n".join(text_parts).strip(), "ask", step + 1, tin, tout, calls, first_in, step_texts, called, claims)
    return LoopResult("\n\n".join(text_parts).strip(), "max_steps", max_steps, tin, tout, calls, first_in, step_texts, called, claims)
