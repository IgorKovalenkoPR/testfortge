"""TestForTge — Tedgie answering about *your* project, with numbers (E6.6).

Before this, Tedgie could tell you what severity means and how to choose
one, and could not tell you how many open bugs you had. Every layer in the
chain answers from static knowledge — the ISTQB corpus, the mentoring packs
— so "how many open bugs do I have" fell through to general advice. Useful
advice, and not an answer to the question.

Why this is not a mentoring pack
--------------------------------
``engine.mentoring`` is deliberately pure: YAML in, verbatim text out, no
database, no model, importable without Flask. Its whole argument is that a
house rule the model paraphrases is no longer a rule. A count of bugs is
not a house rule — it is a fact with a shelf life of minutes — and putting
it in a pack would mean either a template language inside the YAML or a
pack that cannot be answered without a database connection. Both give up
the property that makes that module trustworthy.

It also would not be safe: the packs compete on trigger score, and a new
pack with triggers like "bugs" and "test cases" is a pack that steals
questions from ``severity_priority``. The golden set gates that at 100%,
so the risk is visible, but the right answer is not to take the risk.

So this is its own layer, running **after** mentoring in
``chatbot.respond``. House rules keep priority; this catches what falls
through and is genuinely about the caller's own project.

A number is not an answer
-------------------------
E6's recorded trap is that the thing to guarantee is the **advice**, not
the term. Answering "7" is technically correct and useless: the person
asking is deciding what to do this afternoon. So every reply here pairs the
figure with what it means and what to do next, and the tests assert *that*
rather than any particular wording.

Nothing here raises
-------------------
A database blip must not turn a chat message into a 500. Every read is
guarded and the layer simply declines — the chain continues to the answer
it would have given before.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from engine.log import get_logger

log = get_logger(__name__)

#: Statuses that mean "still somebody's problem".
#:
#: ``Reopened`` counts as open, which is the whole reason this is a set and
#: not ``status == "Open"``: a bug that was closed and came back is the one
#: most worth surfacing, and it is the one a naive equality check hides.
OPEN_STATUSES = frozenset({"open", "in progress", "reopened"})

#: Severity order for reporting, worst first.
SEVERITY_ORDER = ("Critical", "Major", "Minor", "Trivial")


@dataclass(frozen=True)
class Facts:
    """What is true about one project right now."""

    project_id: str = ""
    project_name: str = ""
    bugs_open: int = 0
    bugs_total: int = 0
    open_by_severity: dict[str, int] = field(default_factory=dict)
    test_cases: int = 0
    checklist_items: int = 0
    last_run: dict | None = None

    @property
    def has_anything(self) -> bool:
        return bool(self.bugs_total or self.test_cases or self.checklist_items)


def collect(project_id: str) -> Facts | None:
    """Read the numbers for *project_id*, or ``None`` if unavailable.

    One call per artefact rather than a single clever query: these run on a
    chat message, the counts are small, and a bespoke aggregate here would
    be a second implementation of what the dashboard already computes.
    """
    if not project_id:
        return None
    from engine import db as _db

    try:
        bugs = _db.list_bugs(project_id) or []
        cases = _db.load_test_cases(project_id) or []
        checklist = _db.load_checklist(project_id) or []
        runs = _db.list_execution_runs(project_id, limit=1) or []
        project = _db.get_project(project_id) or {}
    except Exception as exc:      # pragma: no cover — chat must not 500
        log.warning("project facts unavailable for %s: %s",
                    project_id[:8], exc)
        return None

    open_bugs = [b for b in bugs
                 if str(b.get("status") or "Open").strip().lower()
                 in OPEN_STATUSES]
    by_severity: dict[str, int] = {}
    for bug in open_bugs:
        severity = str(bug.get("severity") or "").strip() or "Unspecified"
        by_severity[severity] = by_severity.get(severity, 0) + 1

    return Facts(
        project_id=project_id,
        project_name=str(project.get("name") or ""),
        bugs_open=len(open_bugs),
        bugs_total=len(bugs),
        open_by_severity=by_severity,
        test_cases=len(cases),
        checklist_items=len(checklist),
        last_run=runs[0] if runs else None,
    )


# ── Recognising a question about your own project ────────────────────

#: "how many", in both languages, plus the shapes people actually type.
_QUANTITY = re.compile(
    r"\b(how many|how much|what.s my|what is my|count of|number of)\b"
    r"|скільки|кількість", re.I)

#: A first-person cue. Required, and it is what keeps this layer off the
#: mentoring packs' questions: "how many test cases for a login form" is a
#: method question and belongs to ``process``; "how many test cases do I
#: have" is a fact about this project. Without the cue they are one regex.
_MINE = re.compile(
    r"\b(i have|i've got|do i have|my|mine|our|we have|this project|"
    r"the project|so far|right now|currently)\b"
    r"|в мене|у мене|маю|мого проєкту|мій проєкт|цьому проєкті|наразі|зараз",
    re.I)

#: Shapes that ask for a *recommendation*, not a count. Vetoed.
#:
#: Measured, not anticipated. The golden set's "How many severity levels
#: should we have?" clears both cues above — "how many" and "we have" — and
#: is saved only by naming no artefact. Change it to "how many bug severity
#: levels should we have" and this layer would answer a policy question
#: with an inventory. "How many bugs should we have open at once" is the
#: same trap with no typo required.
#:
#: So a modal veto: asking how many you *should* have is a method question
#: and belongs to the ``process`` pack, whatever nouns it contains.
_ADVICE_SHAPE = re.compile(
    r"\bshould\b|\bought to\b|\brecommend\w*\b|\bis too many\b"
    r"|\bare enough\b|\bis enough\b|\bdo you suggest\b"
    r"|\bвартo?\b|\bслід\b|\bтреба\b|\bрекоменд\w*\b|\bдостатньо\b",
    re.I)

_BUGS = re.compile(r"\bbugs?\b|\bdefects?\b|\bissues?\b|баг|дефект", re.I)
_CASES = re.compile(r"\btest cases?\b|\btcs?\b|тест-?кейс", re.I)
_CHECKS = re.compile(r"\bchecklists?\b|чек-?ліст", re.I)
_RUNS = re.compile(r"\bruns?\b|\bpass rate\b|\bexecution\b|прогін|прогон", re.I)


def wants_project_facts(message: str) -> str | None:
    """Which fact the message is asking for, or ``None``.

    Three conditions, and each was needed:

    * a **quantity** cue — obviously;
    * a **first-person** cue, which is what separates "how many test cases
      should I write for a login form" (method, ``process`` pack's) from
      "how many test cases do I have" (inventory, this layer's);
    * no **advice shape** — see :data:`_ADVICE_SHAPE`. The first two alone
      let "how many bugs should we have open" through, and answering a
      policy question with an inventory is a non sequitur delivered
      confidently.

    Plus a named artefact, checked below. That one is doing quiet work
    too: without it, the golden set's "How many severity levels should we
    have?" — which clears both cues — would land here.
    """
    text = (message or "").strip()
    if not text or not _QUANTITY.search(text) or not _MINE.search(text):
        return None
    if _ADVICE_SHAPE.search(text):
        return None
    # Order matters only for a message naming two artefacts; bugs first
    # because "how many bugs did my run find" is a bug question.
    if _BUGS.search(text):
        return "bugs"
    if _CASES.search(text):
        return "test_cases"
    if _CHECKS.search(text):
        return "checklist"
    if _RUNS.search(text):
        return "runs"
    return None


# ── Composing the reply ──────────────────────────────────────────────

def _severity_breakdown(facts: Facts) -> str:
    parts = [f"{facts.open_by_severity[s]} {s}"
             for s in SEVERITY_ORDER if facts.open_by_severity.get(s)]
    other = sorted(k for k in facts.open_by_severity
                   if k not in SEVERITY_ORDER)
    parts += [f"{facts.open_by_severity[k]} {k}" for k in other]
    return ", ".join(parts)


def _bugs_answer(facts: Facts, lang: str) -> str:
    if not facts.bugs_total:
        if lang == "ua":
            return ("**У цьому проєкті ще немає жодного бага.**\n\n"
                    "Це або дуже добре, або означає, що прогонів ще не "
                    "було. Якщо ви вже тестували й нічого не завели — "
                    "варто перевірити, чи не втрачаються знахідки: "
                    "найчастіше вони лишаються в нотатках, а не в "
                    "Bug Reports.")
        return ("**No bugs have been filed on this project yet.**\n\n"
                "That is either good news or a sign that nothing has been "
                "run. If you have been testing and filed nothing, the "
                "findings are probably sitting in notes rather than in Bug "
                "Reports — which is where they stop being actionable.")

    breakdown = _severity_breakdown(facts)
    closed = facts.bugs_total - facts.bugs_open
    worst = next((s for s in SEVERITY_ORDER
                  if facts.open_by_severity.get(s)), "")

    if lang == "ua":
        head = (f"**Відкритих багів: {facts.bugs_open}** "
                f"(усього заведено {facts.bugs_total}, закрито {closed}).")
        if breakdown:
            head += f"\n\nЗа серйозністю: {breakdown}."
        advice = (
            "\n\nЩо з цим робити:\n\n"
            "1. **Почніть з найсерйознішого.** "
            + (f"Зараз це {worst}. " if worst else "")
            + "Серйозність — це вплив, коли дефект трапляється, а не те, "
            "як часто.\n"
            "2. **Перевірте, чи не застряг хтось із них.** Баг, що довго "
            "висить відкритим, зазвичай чекає на рішення, а не на "
            "виправлення — і це варто сказати вголос на стендапі.\n"
            "3. **Подивіться, чи є повторні.** Reopened рахується тут як "
            "відкритий саме тому, що це найдорожчий вид бага.")
        return head + advice

    head = (f"**{facts.bugs_open} open bug"
            f"{'' if facts.bugs_open == 1 else 's'}** "
            f"({facts.bugs_total} filed in total, {closed} closed).")
    if breakdown:
        head += f"\n\nBy severity: {breakdown}."
    advice = (
        "\n\nWhat to do with that:\n\n"
        "1. **Start at the top of the severity list.** "
        + (f"Right now that is {worst}. " if worst else "")
        + "Severity is impact-when-hit, not how often it is hit — that is "
        "priority's job.\n"
        "2. **Look for one that has stopped moving.** A bug open for a long "
        "time is usually waiting on a decision rather than a fix, and that "
        "is worth saying out loud rather than re-testing.\n"
        "3. **Check the reopened ones.** They count as open here on "
        "purpose: a defect that came back is the most expensive kind, and "
        "an equality check on \"Open\" hides exactly those.")
    return head + advice


def _cases_answer(facts: Facts, lang: str) -> str:
    if lang == "ua":
        body = (f"**Тест-кейсів у проєкті: {facts.test_cases}**, "
                f"пунктів чек-ліста: {facts.checklist_items}.")
        if not facts.test_cases:
            return (body + "\n\nЩе нічого не згенеровано. Почніть із "
                    "модуля Test Cases — навіть чернетка з вимог дає "
                    "предметну розмову замість чистого аркуша.")
        return (body + "\n\nКорисніше за кількість — покриття: чи є кейси "
                "на негативні сценарії та на межі, чи тільки на щасливий "
                "шлях. Якщо всі кейси позитивні, число велике, а ризик "
                "не закритий.")
    body = (f"**{facts.test_cases} test case"
            f"{'' if facts.test_cases == 1 else 's'}** and "
            f"{facts.checklist_items} checklist item"
            f"{'' if facts.checklist_items == 1 else 's'} on this project.")
    if not facts.test_cases:
        return (body + "\n\nNothing has been generated yet. Start in the "
                "Test Cases module — even a rough draft from the "
                "requirements gives you something concrete to argue with, "
                "which a blank page does not.")
    return (body + "\n\nThe more useful question than the count is the "
            "shape: are there negative and boundary cases, or only the "
            "happy path? A large number of positive-only cases is a big "
            "number and an open risk.")


def _runs_answer(facts: Facts, lang: str) -> str:
    run = facts.last_run or {}
    stats = run.get("stats") or {}
    passed = int(stats.get("passed") or 0)
    failed = int(stats.get("failed") or 0)
    blocked = int(stats.get("blocked") or 0)
    total = passed + failed + blocked

    if not run or not total:
        if lang == "ua":
            return ("**Прогонів на цьому проєкті ще немає.**\n\nЗапустіть "
                    "перший у Test Execution — навіть ручне проходження "
                    "дає базову лінію, з якою можна порівнювати наступні.")
        return ("**No runs have been recorded on this project yet.**\n\n"
                "Start one in Test Execution — even a manual walk gives you "
                "a baseline the next run can be compared against.")

    rate = round(100.0 * passed / total, 1)
    if lang == "ua":
        return (f"**Останній прогін: {passed} пройдено, {failed} впало, "
                f"{blocked} заблоковано — {rate}%.**\n\n"
                "Заблоковані важливіші за впалі: вони означають, що "
                "перевірку не вдалося виконати, тож результат невідомий, "
                "а не негативний. Спершу розберіть їх, інакше відсоток "
                "описує менший набір, ніж здається.")
    return (f"**Last run: {passed} passed, {failed} failed, {blocked} "
            f"blocked — {rate}%.**\n\n"
            "The blocked ones matter more than the failures: blocked means "
            "the check could not be performed, so the result is unknown "
            "rather than negative. Clear those first, or the percentage is "
            "describing a smaller set of tests than it appears to.")


def answer(message: str, lang: str = "en", *,
           project_id: str | None = None) -> str | None:
    """A factual answer about the active project, or ``None`` to fall through.

    ``None`` is the contract, the same one ``mentoring.answer`` has: this
    layer never guesses. With no project selected it declines rather than
    inventing a number, because "0 open bugs" and "no project selected" are
    different sentences and only one of them is true.
    """
    wanted = wants_project_facts(message)
    if not wanted:
        return None
    if not project_id:
        if lang == "ua":
            return ("Щоб відповісти числом, мені потрібен активний проєкт "
                    "— виберіть його у списку зверху, і я порахую.")
        return ("I need an active project to answer that with a number — "
                "pick one from the project selector and ask me again.")

    facts = collect(project_id)
    if facts is None:
        return None

    if wanted == "bugs":
        return _bugs_answer(facts, lang)
    if wanted in ("test_cases", "checklist"):
        return _cases_answer(facts, lang)
    if wanted == "runs":
        return _runs_answer(facts, lang)
    return None      # pragma: no cover — wants_project_facts is closed


__all__ = [
    "OPEN_STATUSES", "SEVERITY_ORDER", "Facts",
    "collect", "wants_project_facts", "answer",
]
