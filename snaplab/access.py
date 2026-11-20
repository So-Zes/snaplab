"""Project access, resolved from current ownership and explicit read-only shares."""


def project_ids(app, bot, *, manage=False):
    current = app.store.bot(bot.get("id") or "") or {}
    if not current:
        return []
    return [p["id"] for p in app.store.projects()
            if current.get("orchestrator") or p.get("bot") == current["id"]
            or (not manage and current["id"] in (p.get("shared_with") or []))]


def check_project(app, bot, pid, *, manage=False):
    if pid not in project_ids(app, bot, manage=manage):
        raise ValueError("only the owner or lead bot can manage this project" if manage else
                         "this project is private; its owner or the lead bot must share it first")
