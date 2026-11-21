"""Wire events and launch configuration for the two remote coding agents.

This module is also copied into the worker's read-only configuration mount.
It must have no imports from SnapLab and never contain credentials.
"""
import json

# The longest wait Node's timers (Pi) take: 2^31-1 ms, about 24.8 days. Larger values fire at once in Node, so this
# stands for "no time limit" in the agents' MCP settings; stopping the session ends a call before that.
NO_TIMEOUT_S = 2147483


def pi_config(model, port, context, output):
    return {'providers': {'snaplab': {'baseUrl': f'http://127.0.0.1:{port}/v1',
        'api': 'openai-responses', 'apiKey': '${SNAPLAB_PROXY_TOKEN}',
        'models': [{'id': model, 'name': 'SnapLab', 'reasoning': True,
                    'input': ['text', 'image'], 'contextWindow': context, 'maxTokens': output}]}}}


def launch(req, home, folder, config_dir):
    from pathlib import Path
    if req.get('harness', 'codex') == 'codex':
        binary = str(home / req['codex_bin']) if not req['codex_bin'].startswith('/') else req['codex_bin']
        return [binary, 'exec', '--json', '--ephemeral', '--skip-git-repo-check',
                '--dangerously-bypass-approvals-and-sandbox', '-C', str(folder), '-']
    Path(config_dir, 'models.json').write_text(json.dumps(req['pi_config']), encoding='utf-8')
    Path(config_dir, 'settings.json').write_text(json.dumps({
        'enableInstallTelemetry': False, 'enableAnalytics': False, 'cacheWarming': 'off',
        # A request can fail on the way (the relay to SnapLab, a busy model server); without retries one failure
        # ended the whole turn as "stopped without saying it had finished" (2026-11-16, y8.com test).
        # No practical retry limit and no HTTP idle cut (2026-11-17): six retries ended two long turns after the
        # relay dropped their calls, and a reply that waits for a model slot can be silent for minutes. The wait
        # between retries still grows to at most one minute (Pi's maxAgentDelayMs), and stopping the session ends it.
        'retry': {'enabled': True, 'maxRetries': 100000, 'baseDelayMs': 2000}, 'httpIdleTimeoutMs': 0,
        'defaultProjectTrust': 'never',
        'extensions': ['-builtin:codemode', '-builtin:llama.cpp']}), encoding='utf-8')
    Path(config_dir, 'mcp.json').write_text(json.dumps({'mcpServers': {'snaplab_web': {
        'url': req['mcp_url'], 'headers': {'Authorization': 'Bearer ${SNAPLAB_WEB_TOKEN}'},
        'exposure': 'direct', 'timeout': NO_TIMEOUT_S}}}), encoding='utf-8')
    binary = req.get('pi_bin', '.local/bin/pi')
    binary = str(home / binary) if not binary.startswith('/') else binary
    package = home / '.local/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js'
    safety = "import { createBashToolDefinition } from " + json.dumps(str(package)) + ";\n" + """
export default function(pi) {
  pi.registerTool(createBashToolDefinition(process.cwd(), {
    exposeSessionEnvironment: false,
    spawnHook: ({command, cwd, env}) => ({command, cwd,
      env: Object.fromEntries(Object.entries(env).filter(([k]) => !/(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)/i.test(k)))})
  }));
  console.log(JSON.stringify({type: "snaplab.safety_ready"}));
}
"""
    Path(config_dir, 'pi-safety.mjs').write_text(safety, encoding='utf-8')
    return [binary, '--offline', '--no-approve', '-e', '/codex-config/pi-safety.mjs', '--mode' , 'json', '--print', '--provider', 'snaplab', '--model', req['model'],
            '--thinking', req['effort'], '--no-session', '--no-extensions', '--no-skills',
            '--no-prompt-templates', '-e', 'builtin:mcp',
            '--tools', 'read,bash,edit,write,mcp__snaplab_web__*',
            '--append-system-prompt', req['instructions']]


_PI_CALLS = {}   # toolCallId -> what was started, so the finished record says which command it was


def tool_text(name, args):
    """One line saying what a tool call does: the shell command, the file, or the arguments."""
    if isinstance(args, dict):
        main = args.get('command') or args.get('cmd') or args.get('path') or args.get('file_path') or args.get('url')
        if isinstance(main, str) and main.strip():
            return f'{name}: {main.strip()}' if name and name != 'bash' else main.strip()
        try:
            return f'{name} {json.dumps(args, ensure_ascii=False)[:300]}'
        except (TypeError, ValueError):
            return str(name)
    return str(name)


def result_text(result):
    """The text a tool returned (its last part is the useful one for following progress)."""
    if isinstance(result, dict):
        parts = [x.get('text', '') for x in result.get('content') or [] if isinstance(x, dict) and x.get('type') == 'text']
        return '\n'.join(p for p in parts if p)[-2000:]
    return ''


def events(harness, event):
    """Return only common bounded progress records; no raw provider diagnostics."""
    typ = event.get('type')
    if harness == 'pi':
        if typ == 'message_end':
            msg = event.get('message') or {}
            if msg.get('role') != 'assistant':
                return []
            usage = msg.get('usage') or {}
            result = [{'kind': 'usage', 'input': usage.get('input', 0), 'output': usage.get('output', 0)}]
            reason = msg.get('stopReason')
            if reason not in ('stop', 'toolUse'):
                result.append({'kind': 'failure'})
                if msg.get('errorMessage'):      # kept in the session record, so a failed turn says why
                    result.append({'kind': 'error', 'text': f"Pi ({reason}): {msg['errorMessage']}"})
            if reason == 'stop':
                result += [{'kind': 'message', 'text': '\n'.join(
                    x.get('text', '') for x in msg.get('content', []) if x.get('type') == 'text')},
                           {'kind': 'done'}]
            return result
        if typ == 'tool_execution_start':
            text = tool_text(event.get('toolName', ''), event.get('args'))
            _PI_CALLS[str(event.get('toolCallId', ''))] = text
            while len(_PI_CALLS) > 256:
                _PI_CALLS.pop(next(iter(_PI_CALLS)))
            return [{'kind': 'start', 'text': text}]
        if typ == 'tool_execution_end':
            text = _PI_CALLS.pop(str(event.get('toolCallId', '')), None) or event.get('toolName', '')
            out = {'kind': 'command', 'text': text, 'exit_code': 1 if event.get('isError') else 0,
                   'output': result_text(event.get('result'))}
            if isinstance(event.get('durationMs'), (int, float)):
                out['duration_s'] = round(event['durationMs'] / 1000, 1)
            return [out]
        return []
    if typ == 'item.started':
        item = event.get('item') or {}
        if item.get('type') == 'command_execution':
            return [{'kind': 'start', 'text': item.get('command', '')}]
        return []
    if typ == 'turn.completed':
        u = event.get('usage') or {}
        return [{'kind': 'usage', 'input': u.get('input_tokens', 0), 'output': u.get('output_tokens', 0)},
                {'kind': 'done'}]
    if typ == 'turn.failed':
        why = (event.get('error') or {}).get('message') if isinstance(event.get('error'), dict) else event.get('error')
        return [{'kind': 'failure'}] + ([{'kind': 'error', 'text': f'Codex: {why}'}] if why else [])
    if typ == 'error' and event.get('message'):
        return [{'kind': 'error', 'text': f"Codex: {event['message']}"}]
    if typ == 'item.completed':
        item = event.get('item') or {}
        if item.get('type') == 'agent_message':
            return [{'kind': 'message', 'text': item.get('text', '')}]
        if item.get('type') == 'command_execution':
            return [{'kind': 'command', 'text': item.get('command', ''),
                     'output': item.get('aggregated_output', ''), 'exit_code': item.get('exit_code')}]
        if item.get('type') in ('image_view', 'mcp_tool_call'):
            return [{'kind': 'note', 'text': 'Viewed an image or desktop tool result.'}]
    return []
