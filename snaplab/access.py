"""Project access. Every bot may read and manage every project (until 2026-11-21 only the owner and the lead bot
managed a project and shares were read-only). Ownership (a project's "bot") and shares ("shared_with") still say
who looks after a project; they no longer limit anyone."""


def project_ids(app, bot, *, manage=False):
    current = app.store.bot(bot.get("id") or "") or {}
    if not current:
        return []
    return [p["id"] for p in app.store.projects()]


def check_project(app, bot, pid, *, manage=False):
    if pid not in project_ids(app, bot, manage=manage):
        raise ValueError("no such project")


def looked_after(app, bot):
    """The projects a bot owns or was given a share of: what its context shows. This only sets the focus."""
    current = app.store.bot(bot.get("id") or "") or {}
    if not current:
        return []
    return [p["id"] for p in app.store.projects()
            if p.get("bot") == current["id"] or current["id"] in (p.get("shared_with") or [])]
