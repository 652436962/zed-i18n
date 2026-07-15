from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PureWindowsPath
from typing import Any

from .version_diff import validate_version_diff_report


_OCCURRENCE_STRING_FIELDS = ("file", "kind", "call")
_OCCURRENCE_INTEGER_FIELDS = ("line", "start_byte", "end_byte")


class VersionReferenceIndex:
    def __init__(
        self,
        added: dict[str, Any] | None = None,
        deleted: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self._added = added or {}
        self._deleted = deleted or {}
        self._warnings = warnings if warnings is not None else []

    @classmethod
    def empty(cls) -> VersionReferenceIndex:
        return cls()

    def references_for(
        self,
        source: str,
        language: str,
        current_occurrences: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        try:
            return self._references_for(source, language, current_occurrences)
        except Exception:
            warning = (
                "version-diff projection failure; affected references were ignored"
            )
            if warning not in self._warnings:
                self._warnings.append(warning)
            return []

    def _references_for(
        self,
        source: str,
        language: str,
        current_occurrences: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(source, str) or not isinstance(language, str):
            return []
        added_entry = self._added.get(source)
        if not isinstance(added_entry, dict):
            return []
        candidates = added_entry.get("candidates")
        if not isinstance(candidates, list):
            return []

        references: list[dict[str, Any]] = []
        for candidate in candidates[:3]:
            if not isinstance(candidate, dict):
                continue
            old_source = candidate.get("old_source")
            if not isinstance(old_source, str):
                continue
            deleted_entry = self._deleted.get(old_source)
            if not isinstance(deleted_entry, dict):
                continue
            translations = deleted_entry.get("translations")
            if not isinstance(translations, dict):
                continue
            historical_translation = translations.get(language)
            if not isinstance(historical_translation, str) or not historical_translation:
                continue

            candidate_signals = candidate["signals"]
            signals = {
                name: candidate_signals[name]
                for name in ("same_file", "same_kind", "same_call")
            }
            best_occurrence = _best_old_occurrence(
                deleted_entry.get("occurrences"),
                current_occurrences,
            )
            reference: dict[str, Any] = {
                "old_source": old_source,
                "historical_translation": historical_translation,
                "score": candidate["score"],
                "match_kind": candidate["match_kind"],
                "signals": signals,
            }
            if best_occurrence is not None:
                reference["old_occurrence"] = _compact_occurrence(best_occurrence)
            references.append(reference)
        return references


@dataclass(frozen=True)
class VersionReferenceLoadResult:
    status: str
    path: str | None
    warnings: list[str]
    index: VersionReferenceIndex

    @classmethod
    def disabled(cls) -> VersionReferenceLoadResult:
        return cls(
            status="disabled",
            path=None,
            warnings=[],
            index=VersionReferenceIndex.empty(),
        )


def load_version_references(
    root: Path,
    current_version: str,
) -> VersionReferenceLoadResult:
    try:
        return _load_version_references(Path(root), current_version)
    except Exception as error:
        return _empty_result(
            "invalid",
            f"version-diff discovery failure: {error}",
        )


def _load_version_references(
    root: Path,
    current_version: str,
) -> VersionReferenceLoadResult:
    root = root.resolve()
    if not _safe_component(current_version):
        return _empty_result(
            "invalid",
            "version-diff path mismatch: current version is not a safe directory component",
        )

    reports_dir = root / "reports"
    version_diff_dir = reports_dir / "version-diff"
    if not version_diff_dir.exists():
        return _empty_result(
            "missing",
            f"version-diff report missing for {current_version}",
        )
    if _path_uses_link(reports_dir) or _path_uses_link(version_diff_dir):
        return _empty_result(
            "invalid",
            "version-diff path mismatch: symlink or junction is not allowed",
        )
    try:
        version_diff_dir.resolve().relative_to(root)
    except ValueError:
        return _empty_result(
            "invalid",
            "version-diff path mismatch: report directory escapes the repository",
        )

    suffix = f"-to-{current_version}"
    discovered: list[tuple[Path, str]] = []
    for directory in version_diff_dir.iterdir():
        if not directory.name.endswith(suffix):
            continue
        from_version = directory.name[: -len(suffix)]
        if not _safe_component(from_version):
            continue
        report_path = directory / "key-changes.json"
        if report_path.is_file():
            discovered.append((report_path, from_version))
    discovered.sort(key=lambda item: item[0].parent.name)

    if not discovered:
        return _empty_result(
            "missing",
            f"version-diff report missing for {current_version}",
        )
    if len(discovered) != 1:
        return _empty_result(
            "ambiguous",
            f"version-diff report ambiguous for {current_version}: found {len(discovered)} candidates",
        )

    report_path, path_from_version = discovered[0]
    display_path = _display_path(root, report_path)
    if _path_uses_link(report_path.parent) or _path_uses_link(report_path):
        return _empty_result(
            "invalid",
            "version-diff path mismatch: symlink or junction is not allowed",
            path=display_path,
        )
    try:
        report_path.resolve().relative_to(version_diff_dir.resolve())
    except ValueError:
        return _empty_result(
            "invalid",
            "version-diff path mismatch: report escapes reports/version-diff",
            path=display_path,
        )

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return _empty_result(
            "invalid",
            f"version-diff report malformed JSON: {error}",
            path=display_path,
        )
    except OSError as error:
        return _empty_result(
            "invalid",
            f"version-diff report I/O failure: {error}",
            path=display_path,
        )

    schema_errors = validate_version_diff_report(report)
    if schema_errors:
        error_label = "error" if len(schema_errors) == 1 else "errors"
        return _empty_result(
            "invalid",
            f"version-diff report invalid schema: {len(schema_errors)} {error_label}",
            path=display_path,
        )
    if report["to_version"] != current_version:
        return _empty_result(
            "invalid",
            "version-diff report stale: to_version does not match the current version",
            path=display_path,
        )
    if report["from_version"] != path_from_version:
        return _empty_result(
            "invalid",
            "version-diff path mismatch: from_version does not match its directory",
            path=display_path,
        )

    warnings: list[str] = []
    try:
        index = VersionReferenceIndex(
            report["added"],
            report["deleted"],
            warnings,
        )
    except Exception as error:
        return _empty_result(
            "invalid",
            f"version-diff indexing failure: {error}",
            path=display_path,
        )
    return VersionReferenceLoadResult(
        status="loaded",
        path=display_path,
        warnings=warnings,
        index=index,
    )


def _empty_result(
    status: str,
    warning: str,
    *,
    path: str | None = None,
) -> VersionReferenceLoadResult:
    return VersionReferenceLoadResult(
        status=status,
        path=path,
        warnings=[warning],
        index=VersionReferenceIndex.empty(),
    )


def _safe_component(value: object) -> bool:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    windows_path = PureWindowsPath(value)
    return (
        "/" not in value
        and "\\" not in value
        and not windows_path.drive
        and not windows_path.root
        and len(windows_path.parts) == 1
        and windows_path.name == value
    )


def _path_uses_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _best_old_occurrence(
    old_occurrences: object,
    current_occurrences: object,
) -> dict[str, Any] | None:
    if not isinstance(old_occurrences, list):
        return None
    old_items = [
        (index, occurrence)
        for index, occurrence in enumerate(old_occurrences)
        if isinstance(occurrence, dict)
    ]
    if not old_items:
        return None

    new_items = []
    if isinstance(current_occurrences, list):
        new_items = [
            (index, occurrence)
            for index, occurrence in enumerate(current_occurrences)
            if isinstance(occurrence, dict)
        ]
    if not new_items:
        new_items = [(0, {})]

    pairs: list[
        tuple[
            float,
            tuple[Any, ...],
            tuple[Any, ...],
            dict[str, Any],
        ]
    ] = []
    for old_index, old_occurrence in old_items:
        for new_index, new_occurrence in new_items:
            signals = {
                f"same_{field}": _same_nonempty_string(
                    old_occurrence,
                    new_occurrence,
                    field,
                )
                for field in ("file", "kind", "call")
            }
            support = (
                (0.10 if signals["same_file"] else 0.0)
                + (0.06 if signals["same_kind"] else 0.0)
                + (0.04 if signals["same_call"] else 0.0)
            )
            pairs.append(
                (
                    -support,
                    _occurrence_sort_key(old_occurrence, old_index),
                    _occurrence_sort_key(new_occurrence, new_index),
                    old_occurrence,
                )
            )
    _, _, _, occurrence = min(
        pairs,
        key=lambda pair: (pair[0], pair[1], pair[2]),
    )
    return occurrence


def _same_nonempty_string(
    old_occurrence: dict[str, Any],
    new_occurrence: dict[str, Any],
    field: str,
) -> bool:
    old_value = old_occurrence.get(field)
    new_value = new_occurrence.get(field)
    return (
        isinstance(old_value, str)
        and bool(old_value)
        and isinstance(new_value, str)
        and old_value == new_value
    )


def _occurrence_sort_key(
    occurrence: dict[str, Any],
    index: int,
) -> tuple[Any, ...]:
    return (
        _string_sort_value(occurrence.get("file")),
        _integer_sort_value(occurrence.get("line")),
        _string_sort_value(occurrence.get("kind")),
        _string_sort_value(occurrence.get("call")),
        _integer_sort_value(occurrence.get("start_byte")),
        _integer_sort_value(occurrence.get("end_byte")),
        index,
    )


def _string_sort_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _integer_sort_value(value: object) -> tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return (0, value)
    return (1, 0)


def _compact_occurrence(occurrence: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for field in _OCCURRENCE_STRING_FIELDS:
        value = occurrence.get(field)
        if isinstance(value, str):
            compact[field] = value
    for field in _OCCURRENCE_INTEGER_FIELDS:
        value = occurrence.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            compact[field] = value
    return compact
