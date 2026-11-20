"""Best-effort, secret-filtered records of complete and interrupted model streams."""
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

caller_id = ContextVar('qwen_caller_id', default='')
_lock = threading.Lock()
JST = timezone(timedelta(hours=9))


class QwenLog:
    def __init__(self, data_dir, guard, enabled=True):
        self.folder = Path(data_dir) / 'qwen_log'
        self.guard = guard
        self.enabled = enabled

    def write(self, kind, ident, model, reply):
        if not self.enabled or 'qwen' not in model.lower():
            return
        try:
            now = datetime.now(JST)
            record = dict(timestamp=now.isoformat(), caller_type=kind, caller_id=ident,
                          model=model, reasoning=reply.reasoning, content=reply.content,
                          tool_calls=reply.tool_calls, input_tokens=reply.input_tokens,
                          output_tokens=reply.output_tokens, finished=reply.finished)
            record['reasoning_tokens'] = getattr(reply, 'reasoning_tokens', None)
            record['finish_reason'] = getattr(reply, 'finish_reason', '')
            line = self.guard.clean(json.dumps(record, ensure_ascii=False)) + '\n'
            with _lock:
                self.folder.mkdir(parents=True, exist_ok=True)
                with (self.folder / (now.strftime('%Y-%m-%d') + '.jsonl')).open('a', encoding='utf-8') as f:
                    f.write(line)
        except Exception:
            pass  # Recording must never prevent a reply or tool call.

    def wire(self, ident, request_id, phase, **data):
        """Only response bodies and tool schemas, never request headers or credentials."""
        if not self.enabled:
            return
        if data.get('raw'):
            capture = Capture()
            for line in data['raw'].splitlines():
                if line.startswith('data:'):
                    try:
                        capture.observe(json.loads(line[5:].strip()))
                    except (ValueError, TypeError):
                        pass
            joined = capture.reply.content + capture.reply.reasoning + ''.join(c['arguments'] for c in capture.reply.tool_calls)
            if self.guard.contains(joined):
                data['raw'] = '[Raw stream withheld because reconstructed content contains a credential.]'
        now = datetime.now(JST)
        record = dict(timestamp=now.isoformat(), caller_id=ident, request_id=request_id, phase=phase, **data)
        line = self.guard.clean(json.dumps(record, ensure_ascii=False)) + '\n'
        with _lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder / ('wire-' + now.strftime('%Y-%m-%d') + '.jsonl')).open('a', encoding='utf-8') as f:
                f.write(line)


class WireResponse:
    def __init__(self, response):
        self.response = response
        self.lines = []

    def __iter__(self):
        for raw in self.response:
            self.lines.append(raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw))
            yield raw

    def close(self):
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Capture:
    """Observe raw SSE before the UI can disconnect; retain partial tool arguments."""
    def __init__(self):
        from .llm import Reply
        self.reply = Reply(finished=False)
        self.calls = {}

    def observe(self, event):
        from .provider_protocol import tool_fragments
        r = self.reply
        u = event.get('usage') or {}
        r.input_tokens = u.get('prompt_tokens') or r.input_tokens
        r.output_tokens = u.get('completion_tokens') or r.output_tokens
        for choice in event.get('choices') or []:
            d = choice.get('delta') or {}
            r.reasoning += d.get('reasoning_content') or d.get('reasoning') or ''
            r.content += d.get('content') or ''
            r.finished = choice.get('finish_reason') in ('stop', 'tool_calls', 'function_call') or r.finished
            if choice.get('finish_reason'):
                r.finish_reason = choice['finish_reason']
            for tc in tool_fragments(d):
                c = self.calls.setdefault(tc.get('index', 0), dict(name='', arguments=''))
                fn = tc.get('function') or {}
                c['name'] += fn.get('name') or ''
                c['arguments'] += fn.get('arguments') or ''
        r.tool_calls = list(self.calls.values())
