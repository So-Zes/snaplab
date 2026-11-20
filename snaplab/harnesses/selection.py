"""Small explicit routing rules. No learned rankings or model calls."""
import re


def task_kind(prompt):
    if re.search(r'画面.*操作|マウス|キーボード|Mousepad|LibreOffice|desktop|click-text|GUI|mouse and keyboard|интерфейс.*управ|мыш[ьи]|клавиатур', prompt, re.I):
        return 'desktop'
    if re.search(r'web.?app|ウェブ.*アプリ|Web.*アプリ|HTML|веб.*прилож', prompt, re.I):
        return 'web'
    return 'code'


def select(app, prompt, requested=None, kind=None):
    if requested:
        return requested
    if (app.cfg.get('codex') or {}).get('backend', 'local') == 'local':
        return 'codex'
    if re.search(r'\bcodex\b', prompt, re.I) or kind == 'desktop' or task_kind(prompt) == 'desktop':
        return 'codex'
    return 'pi'


def completion_error(result, folder, required_files=(), tests_required=False, require_artifacts=False):
    from .remote_worker import safe_path
    if not result.ok:
        return result.error or 'Work did not complete.'
    if not result.last_message.strip():
        return 'The final explanation is missing.'
    for name in required_files:
        try:
            path = safe_path(folder, name)
            if not path.is_file() or not path.stat().st_size:
                return 'A required output is missing: ' + name
        except (ValueError, OSError):
            return 'A required output could not be validated.'
    if require_artifacts:
        from .remote_worker import inventory
        scaffold = {'AGENTS.md', 'BRIEF.md', '.gitignore', 'PLAN.md'}
        if not any(not n.startswith('.snaplab/') and n not in scaffold for n in inventory(folder)):
            return 'The requested work has no output files.'
    tests = result.extra.get('tests') or {}
    # A project with nothing to run has not failed; the result card still says "no tests".
    if tests_required and tests.get('status') not in ('passed', 'no tests', 'unchanged'):
        return 'The required tests have not passed.'
    return None
