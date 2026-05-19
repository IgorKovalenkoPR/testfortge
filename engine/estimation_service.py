"""Single source of truth for the estimation pipeline.

Both the sync route (``/estimation/run``) and the async worker
(``/estimation/run-async``) call :func:`run_estimation`; they only
differ in how the :class:`EstimationInput` is assembled (request form
vs closure-captured primitives) and how warnings/errors are surfaced.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from engine.log import get_logger
from engine.qa_estimator import (
    compute_estimation,
    features_from_text,
    features_from_site_analysis,
)
from engine.site_crawler import crawl_site
from engine.file_parser import parse_file

log = get_logger(__name__)


@dataclass
class EstimationInput:
    source_choice: str = "text"
    url: str = ""
    text_input: str = ""
    figma_url: str = ""
    mockup_context: str = ""
    attachment_path: str = ""
    mockup_paths: list[str] = field(default_factory=list)
    project_name: str = ""
    rate_usd: float = 0.0
    additional_platforms: int = 9
    minutes_per_tc: int = 5
    buffer: float = 1.12
    primary_platform: str = "Windows 10"
    compatibility_rate: float = 0.003
    bug_report_rate: float = 0.15
    pm_overhead: float = 0.08
    max_testing_stretch: float = 1.5
    team_size: int = 1


@dataclass
class EstimationOutput:
    result_dict: dict
    extracted_text: str = ""
    source_label: str = ""
    source_ref: str = ""
    features_count: int = 0
    warnings: list[str] = field(default_factory=list)


_DEFAULT_COMPAT_PLATFORMS = [
    "Windows 11", "Apple MacBook Air 2025", "Apple MacBook Pro",
    "MacBook Air 13 256Gb 2020", "Mac Mini 2018",
    "iPhone 16 Pro Max iOS 18", "iPhone 15 iOS 17",
    "iPhone 16 iOS 18.6", "iPad (9th generation) iOS 18",
]


def run_estimation(inp: EstimationInput) -> EstimationOutput:
    """Compute an estimation from a normalised input. Raises
    ``RuntimeError`` if no testable features can be extracted from
    any branch (including the pasted-text fallback)."""
    features: list = []
    source = "manual"
    source_ref = ""
    extracted_text = ""
    warnings: list[str] = []

    src = inp.source_choice

    if src in ("text", "attachment"):
        lines: list[str] = []
        if inp.text_input:
            lines.append(inp.text_input)
        if inp.attachment_path:
            try:
                base = os.path.basename(inp.attachment_path)
                parsed_lines, err = parse_file(inp.attachment_path, base)
                if err:
                    warnings.append(f"Attachment parse warning: {err}")
                if parsed_lines:
                    lines.extend(parsed_lines)
                source_ref = base
            except Exception as exc:
                log.warning("estimation_service parse_file failed: %s", exc)
                warnings.append(
                    f"Attachment parse failed: {type(exc).__name__}")
        if lines:
            features = features_from_text("\n".join(lines))
            source = "text" if not source_ref else "attachment"
            if not source_ref:
                source_ref = "pasted input"

    elif src == "mockups":
        from engine.mockup_vision import analyse as _vision_analyse
        try:
            vres = _vision_analyse(
                file_paths=list(inp.mockup_paths),
                figma_url=inp.figma_url,
                context=inp.mockup_context,
            )
            for w in (vres.warnings or []):
                warnings.append(w)
            if vres.error and not vres.text:
                raise RuntimeError(f"Mockup analysis: {vres.error}")
            if vres.text:
                features = features_from_text(vres.text)
                source = "mockups"
                bits = []
                if inp.mockup_paths:
                    bits.append(f"{len(inp.mockup_paths)} file(s)")
                if inp.figma_url:
                    bits.append("Figma URL")
                source_ref = (vres.source_label
                              or " + ".join(bits)
                              or "uploaded mockups")
                extracted_text = vres.text
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Mockup analysis failed: {type(exc).__name__} — {exc}"
            ) from exc

    elif src == "url":
        if not inp.url:
            warnings.append("URL tab is selected but no URL was provided.")
        else:
            try:
                analysis = crawl_site(inp.url)
                features = features_from_site_analysis(analysis)
                if features:
                    source, source_ref = "url", inp.url
                else:
                    warnings.append(
                        f"Crawled {inp.url} but no testable features "
                        f"were extracted. Try the Text or Mockups tab, "
                        f"or check that the URL serves real HTML content.")
            except Exception as exc:
                log.warning("estimation_service site crawl failed: %s", exc)
                raise RuntimeError(
                    f"Could not crawl {inp.url}: "
                    f"{type(exc).__name__} — {str(exc)[:200]}"
                ) from exc

    # Pasted-text fallback regardless of tab — restores pre-3-tab
    # behaviour operators relied on. Errors are logged, never raised:
    # if the fallback can't parse, the empty-features branch below
    # produces the canonical RuntimeError.
    if not features and inp.text_input:
        try:
            features = features_from_text(inp.text_input)
            if features:
                source = source or "text"
                source_ref = source_ref or "pasted input (fallback)"
        except Exception as exc:
            log.warning(
                "estimation_service text fallback failed: %s", exc)

    if not features:
        raise RuntimeError(
            "No features could be extracted from the selected source. "
            "Pick a different tab, paste more detailed content, or "
            "check the URL is reachable.")

    platforms = _DEFAULT_COMPAT_PLATFORMS[:inp.additional_platforms]
    result = compute_estimation(
        features=features,
        rate_usd=inp.rate_usd,
        additional_platforms=inp.additional_platforms,
        minutes_per_tc=inp.minutes_per_tc,
        buffer=inp.buffer,
        project_name=inp.project_name,
        primary_platform=inp.primary_platform,
        platforms_list=platforms,
        source=source,
        source_ref=source_ref,
        compatibility_rate=inp.compatibility_rate,
        bug_report_rate=inp.bug_report_rate,
        pm_overhead=inp.pm_overhead,
        max_testing_stretch=inp.max_testing_stretch,
        team_size=inp.team_size,
    )

    return EstimationOutput(
        result_dict=asdict(result),
        extracted_text=extracted_text,
        source_label=source,
        source_ref=source_ref,
        features_count=sum(1 for f in features if not f.is_section),
        warnings=warnings,
    )


__all__ = ["EstimationInput", "EstimationOutput", "run_estimation"]
