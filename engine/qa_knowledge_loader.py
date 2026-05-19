"""YAML-backed QA knowledge: checklist sections and test-case templates.

Loaded once on app boot. English fallback when a non-English locale
is missing. Malformed YAML fails boot via jsonschema validation —
never a user request.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any

import yaml

from .qa_persona import CheckItem, TCTemplate


_ROOT = pathlib.Path(__file__).parent / "qa_knowledge"


@dataclass(frozen=True)
class _SectionView:
    name: str
    prefix: str
    items: list[CheckItem]


def _load_schema(path: pathlib.Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _validate(payload: dict, schema: dict, source: pathlib.Path) -> None:
    try:
        import jsonschema
    except Exception:
        return
    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        raise RuntimeError(f"QA knowledge file {source} failed schema validation: {exc.message}") from exc


class QAKnowledgeLoader:
    def __init__(self, root: pathlib.Path | None = None,
                 default_locale: str = "en") -> None:
        self.root = pathlib.Path(root) if root else _ROOT
        self.default_locale = default_locale
        self._checklists: dict[tuple[str, str], list[_SectionView]] = {}
        self._testcases: dict[tuple[str, str], list[TCTemplate]] = {}
        self._section_prefix: dict[str, str] = {}
        self._area_section: dict[str, str] = {}
        self._checklist_schema = _load_schema(self.root / "schema" / "checklist.schema.json")
        self._testcase_schema = _load_schema(self.root / "schema" / "testcase.schema.json")
        self._load_all()

    def _load_all(self) -> None:
        cl_dir = self.root / "checklists"
        for path in sorted(cl_dir.glob("*.yaml")):
            self._load_checklist(path)
        tc_dir = self.root / "testcases"
        for path in sorted(tc_dir.glob("*.yaml")):
            self._load_testcase(path)

    def _load_checklist(self, path: pathlib.Path) -> None:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _validate(data, self._checklist_schema, path)
        area = data["area"]
        locale = data["locale"]
        sections: list[_SectionView] = []
        for sec in data.get("sections", []):
            prefix = sec.get("prefix") or ""
            name = sec["name"]
            if prefix:
                self._section_prefix.setdefault(name, prefix)
            items = [
                CheckItem(
                    objective=item["objective"],
                    category=item["category"],
                    priority=item.get("priority", "Medium"),
                    section=name,
                    testing_type=item.get("testing_type", ""),
                )
                for item in sec.get("items", [])
            ]
            sections.append(_SectionView(name=name, prefix=prefix, items=items))
        self._checklists[(area, locale)] = sections

    def _load_testcase(self, path: pathlib.Path) -> None:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _validate(data, self._testcase_schema, path)
        area = data["area"]
        locale = data["locale"]
        default_section = data.get("default_section", "")
        if default_section:
            self._area_section.setdefault(area, default_section)
        cases: list[TCTemplate] = []
        for case in data.get("cases", []):
            cases.append(TCTemplate(
                summary=case["summary"],
                preconditions=case.get("preconditions", ""),
                steps=list(case.get("steps", [])),
                test_data=case.get("test_data", ""),
                expected_result=case["expected_result"],
                category=case["category"],
                priority=case.get("priority", "Medium"),
                section=case.get("section", default_section),
                comment=case.get("comment", ""),
                testing_type=case.get("testing_type", ""),
            ))
        self._testcases[(area, locale)] = cases

    def _resolve(self, area: str, locale: str | None,
                bucket: dict) -> Any:
        loc = locale or self.default_locale
        if (area, loc) in bucket:
            return bucket[(area, loc)]
        if (area, self.default_locale) in bucket:
            return bucket[(area, self.default_locale)]
        return None

    def get_checklist(self, area: str,
                       locale: str | None = None) -> dict[str, list[CheckItem]]:
        sections = self._resolve(area, locale, self._checklists)
        if not sections:
            return {}
        out: dict[str, list[CheckItem]] = {}
        for sec in sections:
            out[sec.name] = list(sec.items)
        return out

    def get_test_cases(self, area: str,
                         locale: str | None = None) -> list[TCTemplate]:
        cases = self._resolve(area, locale, self._testcases)
        return list(cases) if cases else []

    def section_for_area(self, area: str) -> str:
        return self._area_section.get(area, "")

    def get_section_prefix(self, section_name: str) -> str | None:
        return self._section_prefix.get(section_name)

    def section_prefix_map(self) -> dict[str, str]:
        return dict(self._section_prefix)

    def areas(self) -> list[str]:
        return sorted({area for (area, _) in self._checklists.keys()})

    def reload(self) -> None:
        self._checklists.clear()
        self._testcases.clear()
        self._section_prefix.clear()
        self._area_section.clear()
        self._load_all()


LOADER = QAKnowledgeLoader()
