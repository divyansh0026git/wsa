"""Offline, task-selected excerpts from the skills bundled with WSA."""
from functools import lru_cache
from importlib.resources import files
import re


_CHANGE = re.compile(r"\b(build|create|make|implement|develop|add|extend|wire|integrate|refactor|design|redesign|style|restyle|polish|improve|clean up|unslop|fix|debug|review|audit)\b", re.I)
_UI = re.compile(r"\b(ui|ux|front[ -]?end|web\s?site|web\s?page|landing page|homepage|web\s?app|dashboard|admin panel|portfolio|layout|typography|component|responsive|checkout page|interface design|user interface|e-?commerce (?:site|store)|dark mode|tailwind|stylesheet|css)\b", re.I)
_STANDALONE_INTERFACE = re.compile(r"\binterface\b", re.I)
_NON_UI_INTERFACE = re.compile(
    r"\b(?:api|cli|command[- ]line|network|hardware|kernel|typescript|ts|java|go|golang|rust|c#|driver|storage|service|programming|plugin|protocol)\s+interface\b",
    re.I,
)
_FAILURE = re.compile(r"\b(debug(?:ging)?|troubleshoot(?:ing)?|bugs?|regressions?|crash(?:es|ing|ed)?|broken|fails?|failing|failures?|traceback|exceptions?|errors?|stack trace|timed? out|timeout|not working|doesn['’]t work|500 error)\b", re.I)
_SOFTWARE = re.compile(r"\b(api|endpoint|service|software|app(?:lication)?|web\s?app|cli|bot|feature|support for|command|plugin|function|module|authentication|auth|login|oauth2?|pagination|search|validation|cache|database|integration|rate limit(?:er|ing)?|tests?|code|script|server)\b", re.I)
_IMPLEMENT = re.compile(r"\b(build|create|make|implement|develop|add|extend|wire|integrate|refactor)\b", re.I)
_EXPLAIN = re.compile(r"^\s*(?:what (?:is|are)|explain (?:what|how)|define|what does)\b", re.I)
_SECTIONS = {
    "frontend-design": ("Design principles", "Restraint and self-critique"),
    "unslop": ("What good looks like", "Do not", "Checklist"),
    "systematic-debugging": ("Quick Reference",),
    "test-driven-development": ("Overview", "REFACTOR - Clean Up"),
}


def _is_ui_task(text: str) -> bool:
    if _UI.search(text):
        return True
    if _STANDALONE_INTERFACE.search(text) and not _NON_UI_INTERFACE.search(text):
        return True
    return False


def select_task_skills(request: str) -> tuple[str, ...]:
    text = str(request or "")
    explicit = [name for name in _SECTIONS if re.search(rf"(?<![\w-]){name}(?![\w-])", text, re.I)]
    if _EXPLAIN.search(text):
        return tuple(explicit)
    selected = list(explicit)
    if _is_ui_task(text) and (_CHANGE.search(text) or _FAILURE.search(text)):
        selected.append("frontend-design")
        selected.append("unslop")
    if _FAILURE.search(text) or (re.search(r"\bfix\b", text, re.I) and (_SOFTWARE.search(text) or _is_ui_task(text))):
        selected.append("systematic-debugging")
    if _IMPLEMENT.search(text) and _SOFTWARE.search(text):
        selected.append("test-driven-development")
    return tuple(dict.fromkeys(selected))


@lru_cache(maxsize=len(_SECTIONS))
def _excerpt(name: str) -> str:
    doc = files("wsa_agent").joinpath("skills", name, "SKILL.md").read_text(encoding="utf-8")
    paragraphs = []
    if name == "unslop":
        paragraphs.append("Credit: unslop by Damir Sokolovsky (@heydamir), CC BY 4.0.")
    for title in _SECTIONS[name]:
        section = re.search(rf"^#{{2,3}} {re.escape(title)}\s*\n(.*?)(?=^#{{2,3}} |\Z)", doc, re.M | re.S)
        if section is None:
            raise ValueError(f"Missing bundled skill section: {name}/{title}")
        body = section[1].strip()
        if name == "frontend-design" and title == "Design principles":
            body = "\n".join(body.splitlines()[:2])
        elif name == "unslop":
            if title == "What good looks like":
                body = "\n".join(ln for ln in body.splitlines() if ln.lstrip().startswith("- "))
            elif title == "Checklist":
                body = body.replace("follow [references/rtl.md](references/rtl.md)", "follow RTL rules")
        paragraphs.append(body)
    return f"[{name}]\n" + "\n".join(paragraphs)


def skill_guidance(names: tuple[str, ...]) -> str:
    if not names:
        return ""
    return (
        "WSA task skills (mandatory for this task). Follow these rules within the current stage, "
        "the user's brief, and existing project conventions.\n"
        + "\n\n".join(_excerpt(n) for n in names)
    )
