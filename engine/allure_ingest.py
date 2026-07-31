"""TestFortge — parse ``allure-results`` without a JVM.

The Allure CLI that renders the HTML report is a Java application. The web
service runs on a 512 MB instance and is not going to grow a JVM to read a
number off a run, so this module reads the raw results instead.

That is cheaper than it sounds. ``allure-results`` is a flat directory of
JSON: one ``*-result.json`` per test with a status, a duration, labels and
steps. Everything the Dashboard needs — pass rate, duration, which suite,
which test case, what failed — is in there. The HTML report is a rendering
of the same data, not a source of extra information.

What this deliberately does not do
----------------------------------
Render a report. If an operator wants the full Allure UI they run
``npm run report`` where they ran the suite, and may upload the built
bundle for TestForTge to serve statically. Reimplementing the report would
be a large amount of work to produce something worse than the real one.

Statuses
--------
Allure's vocabulary is ``passed`` / ``failed`` / ``broken`` / ``skipped`` /
``unknown``. They are kept distinct rather than collapsed:

* ``failed``  — an assertion did not hold. The product is wrong.
* ``broken``  — the test threw before it could assert. The test or the
  environment is wrong.
* ``skipped`` — nobody checked. This is what the generated suite reports
  for an assertion it cannot bind, and folding it into either of the above
  would be the exact dishonesty the generator exists to avoid.

Pass rate is therefore computed over ``passed + failed + broken`` and the
skipped count is reported alongside it, never inside it.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterable

from engine.log import get_logger

_logger = get_logger(__name__)

#: Guards on an upload. An allure-results directory for a few hundred tests
#: is a few MB; anything far past that is a mistake or an attack.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_RESULT_FILES = 5000
MAX_MEMBER_BYTES = 4 * 1024 * 1024

EXECUTED_STATUSES = ("passed", "failed", "broken")
ALL_STATUSES = EXECUTED_STATUSES + ("skipped", "unknown")

#: The tag the generated features carry, e.g. ``@TC-SC1_004``. Recovering
#: it is what lets a run link back to the test case it exercised.
_TC_TAG_RE = re.compile(r"^TC-(.+)$")


@dataclass
class TestResult:
    """One executed scenario."""
    name: str = ""
    status: str = "unknown"
    #: Allure's own message / trace, trimmed.
    message: str = ""
    duration_ms: int = 0
    suite: str = ""
    feature: str = ""
    #: TestForTge test-case id, recovered from the ``@TC-…`` tag.
    case_id: str = ""
    tags: list[str] = field(default_factory=list)
    #: True when Allure marked the test as a known flake or it was retried.
    flaky: bool = False
    #: The step that ended the test, when one can be identified.
    failed_step: str = ""

    @property
    def executed(self) -> bool:
        return self.status in EXECUTED_STATUSES


@dataclass
class RunSummary:
    """Everything the Dashboard shows for one automation run."""
    total: int = 0
    passed: int = 0
    failed: int = 0
    broken: int = 0
    skipped: int = 0
    unknown: int = 0
    duration_ms: int = 0
    results: list[TestResult] = field(default_factory=list)
    #: Findings about the results themselves, not about the product —
    #: an empty upload, unparseable files.
    warnings: list[str] = field(default_factory=list)

    @property
    def executed(self) -> int:
        """Tests that actually ran. Skipped ones did not."""
        return self.passed + self.failed + self.broken

    @property
    def pass_rate(self) -> float:
        """Percentage over EXECUTED tests, never over the total.

        Counting a skip as a pass inflates the number; counting it as a
        failure invents a defect. It belongs in neither, which is why
        ``skipped`` is reported next to this rather than inside it.
        """
        return round(100.0 * self.passed / self.executed, 1) \
            if self.executed else 0.0

    @property
    def flaky(self) -> list[str]:
        return [r.name for r in self.results if r.flaky]

    def by_suite(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.results:
            key = r.suite or r.feature or "(unnamed)"
            bucket = out.setdefault(key, {s: 0 for s in ALL_STATUSES})
            bucket[r.status] = bucket.get(r.status, 0) + 1
        return out

    def failures(self) -> list[TestResult]:
        return [r for r in self.results
                if r.status in ("failed", "broken")]

    def skipped_results(self) -> list[TestResult]:
        return [r for r in self.results if r.status == "skipped"]

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "executed": self.executed,
            "passed": self.passed,
            "failed": self.failed,
            "broken": self.broken,
            "skipped": self.skipped,
            "unknown": self.unknown,
            "pass_rate": self.pass_rate,
            "duration_ms": self.duration_ms,
            "flaky": self.flaky,
            "by_suite": self.by_suite(),
            "warnings": list(self.warnings),
            "failures": [
                {"name": r.name, "case_id": r.case_id, "status": r.status,
                 "message": r.message[:600], "failed_step": r.failed_step}
                for r in self.failures()
            ],
            "skipped_detail": [
                {"name": r.name, "case_id": r.case_id,
                 "message": r.message[:400]}
                for r in self.skipped_results()
            ],
        }


# ── Parsing one result file ──────────────────────────────────────────

def _labels(doc: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for label in (doc.get("labels") or []):
        if not isinstance(label, dict):
            continue
        name = str(label.get("name") or "").strip()
        value = str(label.get("value") or "").strip()
        if name and value:
            out.setdefault(name, []).append(value)
    return out


def _failed_step(doc: dict) -> str:
    """Name of the step that ended the test, if the results say.

    Allure nests steps, and the interesting one is the deepest
    failed/broken node — the outermost merely says "the scenario failed",
    which the operator already knows.
    """
    found = ""

    def walk(steps: Iterable[Any], depth: int = 0) -> None:
        nonlocal found
        # Recurse into EVERY step, not only the failed ones: a reporter may
        # leave an outer step green while a nested one failed, and stopping
        # at the first green parent loses the only useful name in the file.
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            if str(step.get("status") or "").lower() in ("failed", "broken"):
                name = str(step.get("name") or "").strip()
                if name:
                    # Deeper wins — the outermost merely restates that the
                    # scenario failed, which the caller already knows.
                    found = name
            walk(step.get("steps") or [], depth + 1)

    walk(doc.get("steps") or [])
    return found


def parse_result(doc: dict) -> TestResult | None:
    """One ``*-result.json`` document → :class:`TestResult`."""
    if not isinstance(doc, dict):
        return None
    name = str(doc.get("name") or "").strip()
    if not name:
        return None
    status = str(doc.get("status") or "unknown").strip().lower()
    if status not in ALL_STATUSES:
        status = "unknown"

    labels = _labels(doc)
    tags = list(labels.get("tag") or [])
    case_id = ""
    for tag in tags:
        m = _TC_TAG_RE.match(tag.lstrip("@"))
        if m:
            case_id = m.group(1)
            break
    # allure-playwright also emits an AS_ID / testCaseId; prefer the tag
    # because that is what TestForTge itself wrote.
    if not case_id:
        case_id = (labels.get("as_id") or [""])[0]

    start = doc.get("start")
    stop = doc.get("stop")
    duration = 0
    if isinstance(start, (int, float)) and isinstance(stop, (int, float)):
        duration = max(0, int(stop) - int(start))

    details = doc.get("statusDetails") or {}
    message = ""
    if isinstance(details, dict):
        message = (str(details.get("message") or "").strip()
                   or str(details.get("trace") or "").strip())

    return TestResult(
        name=name,
        status=status,
        message=message[:2000],
        duration_ms=duration,
        suite=(labels.get("suite") or labels.get("parentSuite") or [""])[0],
        feature=(labels.get("feature") or [""])[0],
        case_id=case_id,
        tags=tags,
        flaky=bool(isinstance(details, dict) and details.get("flaky")),
        failed_step=_failed_step(doc),
    )


# ── Parsing a directory or an archive ────────────────────────────────

def _is_result_name(name: str) -> bool:
    base = os.path.basename(name)
    return base.endswith("-result.json") and not base.startswith(".")


def summarise(docs: Iterable[dict]) -> RunSummary:
    """Aggregate parsed result documents."""
    summary = RunSummary()
    for doc in docs:
        result = parse_result(doc)
        if result is None:
            continue
        summary.results.append(result)
        summary.total += 1
        summary.duration_ms += result.duration_ms
        setattr(summary, result.status,
                getattr(summary, result.status, 0) + 1)
    if summary.total == 0:
        summary.warnings.append(
            "No *-result.json files were found. The suite ran without the "
            "allure-playwright reporter, or the wrong directory was "
            "uploaded — allure-results, not allure-report.")
    return summary


def parse_directory(path: str) -> RunSummary:
    """Read an ``allure-results`` directory from disk."""
    docs: list[dict] = []
    warnings: list[str] = []
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        summary = RunSummary()
        summary.warnings.append(f"Cannot read {path}: {exc}")
        return summary
    for name in names[:MAX_RESULT_FILES]:
        if not _is_result_name(name):
            continue
        full = os.path.join(path, name)
        try:
            with open(full, encoding="utf-8") as fh:
                docs.append(json.load(fh))
        except (OSError, ValueError) as exc:
            warnings.append(f"{name}: {type(exc).__name__}")
    summary = summarise(docs)
    summary.warnings.extend(warnings[:20])
    return summary


def parse_archive(data: bytes) -> RunSummary:
    """Read a zip of ``allure-results``.

    Accepts the folder zipped either way — ``allure-results/x-result.json``
    or a bare ``x-result.json`` — because both are what people actually
    produce, and refusing one of them would be a support ticket rather than
    a safety property.
    """
    summary = RunSummary()
    if not data:
        summary.warnings.append("Empty upload.")
        return summary
    if len(data) > MAX_UPLOAD_BYTES:
        summary.warnings.append(
            f"Upload is {len(data) // 1024 // 1024} MB, over the "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB limit.")
        return summary

    docs: list[dict] = []
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [m for m in zf.infolist()
                       if not m.is_dir() and _is_result_name(m.filename)]
            if len(members) > MAX_RESULT_FILES:
                warnings.append(
                    f"{len(members)} result files; only the first "
                    f"{MAX_RESULT_FILES} were read.")
                members = members[:MAX_RESULT_FILES]
            for member in members:
                # Decompressed size, not the compressed one — a small
                # archive can carry a very large member.
                if member.file_size > MAX_MEMBER_BYTES:
                    warnings.append(
                        f"{os.path.basename(member.filename)} is "
                        f"{member.file_size // 1024} KB; skipped.")
                    continue
                try:
                    docs.append(json.loads(zf.read(member).decode("utf-8")))
                except (ValueError, UnicodeDecodeError, OSError) as exc:
                    warnings.append(
                        f"{os.path.basename(member.filename)}: "
                        f"{type(exc).__name__}")
    except zipfile.BadZipFile:
        summary.warnings.append(
            "Not a zip archive. Zip the allure-results directory and upload "
            "that — `npm run upload` in the generated bundle does it.")
        return summary

    summary = summarise(docs)
    summary.warnings.extend(warnings[:20])
    return summary


# ── Linking back to test cases ───────────────────────────────────────

def statuses_by_case(summary: RunSummary) -> dict[str, str]:
    """``{test-case id: status}`` for the cases the run covered.

    Where one case ran more than once (a retry, or a matrix across
    browsers) the worst status wins: a case that failed on one browser is
    not passing.
    """
    rank = {"passed": 0, "skipped": 1, "unknown": 2, "broken": 3, "failed": 4}
    out: dict[str, str] = {}
    for result in summary.results:
        if not result.case_id:
            continue
        prev = out.get(result.case_id)
        if prev is None or rank.get(result.status, 2) > rank.get(prev, 2):
            out[result.case_id] = result.status
    return out


def to_metrics(summary: RunSummary) -> dict:
    """The subset the Dashboard card and the metric snapshot need."""
    return {
        "automation_total": summary.total,
        "automation_executed": summary.executed,
        "automation_passed": summary.passed,
        "automation_failed": summary.failed + summary.broken,
        "automation_skipped": summary.skipped,
        "automation_pass_rate": summary.pass_rate,
        "automation_duration_ms": summary.duration_ms,
        "automation_flaky": len(summary.flaky),
    }


__all__ = [
    "EXECUTED_STATUSES", "ALL_STATUSES", "MAX_UPLOAD_BYTES",
    "TestResult", "RunSummary",
    "parse_result", "summarise", "parse_directory", "parse_archive",
    "statuses_by_case", "to_metrics",
]
