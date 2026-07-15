from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import tempfile
import tomllib
from typing import Any


_BASE_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
_RAW_TOKEN_RE = re.compile(r"[a-z0-9]+")
_RUST_UNICODE_ESCAPE_RE = re.compile(r"\\u\{([0-9A-Fa-f_]{1,6})\}")
_MATCH_KINDS = frozenset({"normalized_exact", "placeholder_shape", "similarity"})
_SIGNAL_NAMES = (
    "normalized_text_equal",
    "placeholder_shape_equal",
    "same_file",
    "same_kind",
    "same_call",
)


@dataclass(frozen=True)
class VersionSnapshot:
    version: str
    catalog: dict[str, str]
    manifest: dict[str, dict[str, Any]]
    translations: dict[str, dict[str, str]]


@dataclass(frozen=True)
class GeneratedVersionDiff:
    output_path: Path
    report: dict[str, Any]


def generate_version_diff(
    root: Path,
    base_ref: str = "HEAD",
) -> GeneratedVersionDiff:
    root = Path(root).resolve()
    base_commit = _resolve_base_commit(root, base_ref)
    old_version = _parse_version(
        _git(root, "show", f"{base_commit}:config/project.toml")
    )
    new_version = _parse_version(
        (root / "config" / "project.toml").read_text(encoding="utf-8")
    )
    if old_version == new_version:
        raise ValueError("old and new versions must be different")

    old_catalog = json.loads(
        _git(root, "show", f"{base_commit}:catalog/en-US.json")
    )
    old_manifest = json.loads(
        _git(root, "show", f"{base_commit}:manifest/ui-strings.json")
    )
    translation_listing = _git(
        root,
        "ls-tree",
        "-r",
        "--name-only",
        base_commit,
        "--",
        "translations",
    )
    old_translations = {
        PurePosixPath(path).stem: json.loads(
            _git(root, "show", f"{base_commit}:{path}")
        )
        for path in _final_translation_paths(translation_listing)
    }
    new_catalog = json.loads(
        (root / "catalog" / "en-US.json").read_text(encoding="utf-8")
    )
    new_manifest = json.loads(
        (root / "manifest" / "ui-strings.json").read_text(encoding="utf-8")
    )

    old = VersionSnapshot(
        version=old_version,
        catalog=old_catalog,
        manifest=old_manifest,
        translations=old_translations,
    )
    new = VersionSnapshot(
        version=new_version,
        catalog=new_catalog,
        manifest=new_manifest,
        translations={},
    )
    report = build_version_diff(old, new, base_commit=base_commit)

    output_path = _version_diff_output_path(
        root,
        from_version=old_version,
        to_version=new_version,
    )
    _write_json_atomically(output_path, report)
    return GeneratedVersionDiff(output_path=output_path, report=report)


def _resolve_base_commit(root: Path, base_ref: str) -> str:
    commit = _git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}").strip()
    if _BASE_COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("resolved base commit must be a 40-character hexadecimal commit")
    return commit


def _git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root.as_posix()}",
                *arguments,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        message = f"git {arguments[0]} failed with exit status {error.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise ValueError(message) from error
    return completed.stdout


def _parse_version(text: str) -> str:
    version = tomllib.loads(text).get("zed_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("zed_version must be a non-empty string")
    return version


def _final_translation_paths(listing: str) -> list[str]:
    paths: list[str] = []
    for raw_path in listing.splitlines():
        path = PurePosixPath(raw_path)
        if (
            len(path.parts) == 2
            and path.parts[0] == "translations"
            and path.suffix == ".json"
            and "." not in path.stem
        ):
            paths.append(path.as_posix())
    return sorted(paths)


def _version_diff_output_path(
    root: Path,
    *,
    from_version: str,
    to_version: str,
) -> Path:
    _validate_version_component("from_version", from_version)
    _validate_version_component("to_version", to_version)
    reports_path = root / "reports"
    reports_root = reports_path / "version-diff"
    version_path = reports_root / f"{from_version}-to-{to_version}"
    output_path = version_path / "key-changes.json"
    components = (
        reports_path,
        reports_root,
        version_path,
        output_path,
    )
    for component in components:
        if _path_uses_link(component):
            raise ValueError(
                "version diff output path uses a symbolic link or junction: "
                f"{component}"
            )

    resolved_components = tuple(component.resolve() for component in components)
    resolved_reports_root = resolved_components[1]
    resolved_output_path = resolved_components[3]
    try:
        resolved_reports_root.relative_to(root)
        resolved_output_path.relative_to(resolved_reports_root)
    except ValueError as error:
        raise ValueError("version diff output path escapes reports/version-diff") from error
    if any(
        component != resolved
        for component, resolved in zip(components, resolved_components, strict=True)
    ):
        raise ValueError(
            "version diff lexical output path does not match its canonical path"
        )
    return resolved_output_path


def _path_uses_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _validate_version_component(name: str, value: str) -> None:
    windows_path = PureWindowsPath(value)
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or bool(windows_path.drive)
        or bool(windows_path.root)
        or len(windows_path.parts) != 1
        or windows_path.name != value
    ):
        raise ValueError(f"{name} must be a single safe directory component")


def _write_json_atomically(path: Path, value: object) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(
                value,
                temporary_file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            temporary_file.write("\n")
        os.replace(temporary_path, path)
        temporary_path = None
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def build_version_diff(
    old: VersionSnapshot,
    new: VersionSnapshot,
    *,
    base_commit: str,
    min_score: float = 0.70,
    max_candidates: int = 3,
) -> dict[str, Any]:
    _validate_build_inputs(
        old,
        new,
        base_commit=base_commit,
        min_score=min_score,
        max_candidates=max_candidates,
    )

    deleted_sources = sorted(set(old.catalog) - set(new.catalog))
    added_sources = sorted(set(new.catalog) - set(old.catalog))

    deleted: dict[str, dict[str, Any]] = {}
    for source in deleted_sources:
        manifest_entry = old.manifest[source]
        translations = {
            locale: locale_translations[source]
            for locale, locale_translations in sorted(old.translations.items())
            if isinstance(locale, str)
            and isinstance(locale_translations, dict)
            and isinstance(locale_translations.get(source), str)
            and locale_translations[source] != ""
        }
        deleted[source] = {
            "status": deepcopy(manifest_entry.get("status")),
            "occurrences": deepcopy(manifest_entry.get("occurrences")),
            "translations": translations,
        }

    added: dict[str, dict[str, Any]] = {}
    for source in added_sources:
        manifest_entry = new.manifest[source]
        candidates: list[dict[str, Any]] = []
        for old_source in deleted_sources:
            if not deleted[old_source]["translations"]:
                continue
            candidate = _candidate(
                old_source,
                source,
                old.manifest[old_source].get("occurrences"),
                manifest_entry.get("occurrences"),
            )
            if candidate is not None and candidate["score"] >= min_score:
                candidates.append(candidate)
        candidates.sort(key=lambda candidate: (-candidate["score"], candidate["old_source"]))
        added[source] = {
            "status": deepcopy(manifest_entry.get("status")),
            "occurrences": deepcopy(manifest_entry.get("occurrences")),
            "candidates": candidates[:max_candidates],
        }

    report: dict[str, Any] = {
        "schema_version": 1,
        "from_version": old.version,
        "to_version": new.version,
        "base_commit": base_commit,
        "summary": {
            "added": len(added),
            "deleted": len(deleted),
            "candidate_pairs": sum(len(entry["candidates"]) for entry in added.values()),
        },
        "deleted": deleted,
        "added": added,
    }
    errors = validate_version_diff_report(report)
    if errors:
        raise ValueError("invalid version diff: " + "; ".join(errors))
    return report


def validate_version_diff_report(report: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report: expected object"]

    schema_version = report.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        _error(errors, "schema_version", "expected integer 1")
    _validate_nonempty_string(errors, "from_version", report.get("from_version"))
    _validate_nonempty_string(errors, "to_version", report.get("to_version"))
    if (
        isinstance(report.get("from_version"), str)
        and report.get("from_version")
        and report.get("from_version") == report.get("to_version")
    ):
        _error(errors, "to_version", "must differ from from_version")
    base_commit = report.get("base_commit")
    if not isinstance(base_commit, str) or _BASE_COMMIT_RE.fullmatch(base_commit) is None:
        _error(errors, "base_commit", "expected 40-character hexadecimal commit")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        _error(errors, "summary", "expected object")
        summary = None
    else:
        for name in ("added", "deleted", "candidate_pairs"):
            value = summary.get(name)
            if not _is_nonnegative_integer(value):
                _error(errors, f"summary.{name}", "expected non-negative integer")

    deleted = report.get("deleted")
    if not isinstance(deleted, dict):
        _error(errors, "deleted", "expected object")
        deleted = None
    else:
        for source, entry in deleted.items():
            path = _entry_path("deleted", source)
            if not isinstance(source, str):
                _error(errors, path, "source key must be a string")
            _validate_manifest_entry(errors, path, entry)
            if isinstance(entry, dict):
                _validate_translations(errors, f"{path}.translations", entry.get("translations"))

    added = report.get("added")
    candidate_count = 0
    if not isinstance(added, dict):
        _error(errors, "added", "expected object")
        added = None
    else:
        deleted_keys = set(deleted) if isinstance(deleted, dict) else set()
        for source, entry in added.items():
            path = _entry_path("added", source)
            if not isinstance(source, str):
                _error(errors, path, "source key must be a string")
            _validate_manifest_entry(errors, path, entry)
            if not isinstance(entry, dict):
                continue
            candidates = entry.get("candidates")
            candidates_path = f"{path}.candidates"
            if not isinstance(candidates, list):
                _error(errors, candidates_path, "expected list")
                continue
            candidate_count += len(candidates)
            if len(candidates) > 3:
                _error(errors, candidates_path, "must contain at most three candidates")
            _validate_candidates(errors, candidates_path, candidates, deleted_keys)

    if summary is not None:
        if isinstance(added, dict) and _is_nonnegative_integer(summary.get("added")):
            if summary["added"] != len(added):
                _error(errors, "summary.added", f"expected {len(added)}")
        if isinstance(deleted, dict) and _is_nonnegative_integer(summary.get("deleted")):
            if summary["deleted"] != len(deleted):
                _error(errors, "summary.deleted", f"expected {len(deleted)}")
        if _is_nonnegative_integer(summary.get("candidate_pairs")):
            if summary["candidate_pairs"] != candidate_count:
                _error(errors, "summary.candidate_pairs", f"expected {candidate_count}")

    return errors


def _validate_build_inputs(
    old: VersionSnapshot,
    new: VersionSnapshot,
    *,
    base_commit: object,
    min_score: object,
    max_candidates: object,
) -> None:
    for name, snapshot in (("old", old), ("new", new)):
        if not isinstance(snapshot, VersionSnapshot):
            raise ValueError(f"{name} snapshot must be a VersionSnapshot")
        if not isinstance(snapshot.version, str) or not snapshot.version.strip():
            raise ValueError(f"{name} version must be a non-empty string")
        if not isinstance(snapshot.catalog, dict) or not all(
            isinstance(source, str) and isinstance(value, str)
            for source, value in snapshot.catalog.items()
        ):
            raise ValueError(f"{name} catalog must map strings to strings")
        if not isinstance(snapshot.manifest, dict) or not all(
            isinstance(source, str) and isinstance(entry, dict)
            for source, entry in snapshot.manifest.items()
        ):
            raise ValueError(f"{name} manifest must map strings to objects")
        if set(snapshot.catalog) != set(snapshot.manifest):
            raise ValueError(f"{name} snapshot catalog and manifest keys differ")
        if not isinstance(snapshot.translations, dict):
            raise ValueError(f"{name} translations must be an object")
        for locale, translations in snapshot.translations.items():
            if not isinstance(locale, str) or locale == "":
                raise ValueError(
                    f"{name} translations locale must be a non-empty string"
                )
            if not isinstance(translations, dict):
                raise ValueError(f"{name} translations.{locale} must be an object")
            for source, translation in translations.items():
                if not isinstance(source, str):
                    raise ValueError(
                        f"{name} translations.{locale} source keys must be strings"
                    )
                if not isinstance(translation, str):
                    raise ValueError(
                        f"{name} translations.{locale}.{source} must be a string"
                    )
    if old.version == new.version:
        raise ValueError("old and new versions must be different")
    if not isinstance(base_commit, str) or _BASE_COMMIT_RE.fullmatch(base_commit) is None:
        raise ValueError("base_commit must be a 40-character hexadecimal commit")
    if (
        isinstance(min_score, bool)
        or not isinstance(min_score, (int, float))
        or not math.isfinite(min_score)
        or not 0.0 <= min_score <= 1.0
    ):
        raise ValueError("min_score must be a finite number between 0.0 and 1.0")
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or not 0 < max_candidates <= 3
    ):
        raise ValueError(
            "max_candidates must be a positive integer no greater than the "
            "schema maximum of 3"
        )


def _candidate(
    old_source: str,
    new_source: str,
    old_occurrences: object,
    new_occurrences: object,
) -> dict[str, Any] | None:
    raw_old = _raw_normalize(old_source)
    raw_new = _raw_normalize(new_source)
    placeholder_old = _placeholder_normalize(old_source)
    placeholder_new = _placeholder_normalize(new_source)

    similarities = [
        similarity
        for similarity in (
            _similarity(raw_old, raw_new),
            _similarity(placeholder_old, placeholder_new),
        )
        if similarity is not None
    ]
    if not similarities:
        return None

    normalized_text_equal = bool(raw_old and raw_new and raw_old == raw_new)
    placeholder_shape_equal = bool(
        placeholder_old and placeholder_new and placeholder_old == placeholder_new
    )
    same_file = _occurrences_share(old_occurrences, new_occurrences, "file")
    same_kind = _occurrences_share(old_occurrences, new_occurrences, "kind")
    same_call = _occurrences_share(old_occurrences, new_occurrences, "call")
    score = min(
        1.0,
        max(similarities)
        + (0.10 if same_file else 0.0)
        + (0.06 if same_kind else 0.0)
        + (0.04 if same_call else 0.0),
    )
    if normalized_text_equal:
        match_kind = "normalized_exact"
    elif placeholder_shape_equal:
        match_kind = "placeholder_shape"
    else:
        match_kind = "similarity"
    return {
        "old_source": old_source,
        "score": score,
        "match_kind": match_kind,
        "signals": {
            "normalized_text_equal": normalized_text_equal,
            "placeholder_shape_equal": placeholder_shape_equal,
            "same_file": same_file,
            "same_kind": same_kind,
            "same_call": same_call,
        },
    }


def _raw_normalize(text: str) -> str:
    return " ".join(_RAW_TOKEN_RE.findall(text.casefold()))


def _placeholder_normalize(text: str) -> str:
    pieces: list[str] = []
    index = 0
    while index < len(text):
        unicode_escape = _RUST_UNICODE_ESCAPE_RE.match(text, index)
        if unicode_escape is not None and _valid_rust_unicode_escape(
            unicode_escape.group(1)
        ):
            pieces.append(unicode_escape.group(0))
            index = unicode_escape.end()
            continue
        if text[index] == "{" and index + 1 < len(text) and text[index + 1] == "{":
            pieces.append("{{")
            index += 2
            continue
        if text[index] == "}" and index + 1 < len(text) and text[index + 1] == "}":
            pieces.append("}}")
            index += 2
            continue
        if text[index] == "{":
            end = text.find("}", index + 1)
            if end != -1:
                pieces.append(" placeholder ")
                index = end + 1
                continue
        pieces.append(text[index])
        index += 1
    return _raw_normalize("".join(pieces))


def _valid_rust_unicode_escape(digits: str) -> bool:
    try:
        codepoint = int(digits.replace("_", ""), 16)
    except ValueError:
        return False
    return codepoint <= 0x10FFFF and not 0xD800 <= codepoint <= 0xDFFF


def _similarity(old: str, new: str) -> float | None:
    if not old or not new:
        return None
    return SequenceMatcher(None, old, new, autojunk=False).ratio()


def _occurrences_share(old: object, new: object, field: str) -> bool:
    if not isinstance(old, list) or not isinstance(new, list):
        return False
    old_values = {
        occurrence.get(field)
        for occurrence in old
        if isinstance(occurrence, dict)
        and isinstance(occurrence.get(field), str)
        and occurrence.get(field)
    }
    return any(
        isinstance(occurrence, dict)
        and isinstance(occurrence.get(field), str)
        and occurrence.get(field) in old_values
        for occurrence in new
    )


def _validate_manifest_entry(errors: list[str], path: str, entry: object) -> None:
    if not isinstance(entry, dict):
        _error(errors, path, "expected object")
        return
    if not isinstance(entry.get("status"), str):
        _error(errors, f"{path}.status", "expected string")
    _validate_occurrences(errors, f"{path}.occurrences", entry.get("occurrences"))


def _validate_occurrences(errors: list[str], path: str, occurrences: object) -> None:
    if not isinstance(occurrences, list):
        _error(errors, path, "expected list")
        return
    for index, occurrence in enumerate(occurrences):
        occurrence_path = f"{path}[{index}]"
        if not isinstance(occurrence, dict):
            _error(errors, occurrence_path, "expected object")
            continue
        for name in ("file", "kind", "call"):
            if name in occurrence and not isinstance(occurrence[name], str):
                _error(errors, f"{occurrence_path}.{name}", "expected string")
        if "line" in occurrence and not _is_positive_integer(occurrence["line"]):
            _error(errors, f"{occurrence_path}.line", "expected positive integer")
        for name in ("start_byte", "end_byte"):
            if name in occurrence and not _is_nonnegative_integer(occurrence[name]):
                _error(
                    errors,
                    f"{occurrence_path}.{name}",
                    "expected non-negative integer",
                )


def _validate_translations(errors: list[str], path: str, translations: object) -> None:
    if not isinstance(translations, dict):
        _error(errors, path, "expected object")
        return
    for locale, translation in translations.items():
        locale_path = f"{path}.{locale}" if isinstance(locale, str) else f"{path}[{locale!r}]"
        if not isinstance(locale, str):
            _error(errors, locale_path, "locale must be a string")
        if not isinstance(translation, str) or translation == "":
            _error(errors, locale_path, "expected non-empty string")


def _validate_candidates(
    errors: list[str],
    path: str,
    candidates: list[object],
    deleted_keys: set[object],
) -> None:
    seen: set[str] = set()
    order: list[tuple[float, str]] = []
    order_is_valid = True
    for index, candidate in enumerate(candidates):
        candidate_path = f"{path}[{index}]"
        if not isinstance(candidate, dict):
            _error(errors, candidate_path, "expected object")
            order_is_valid = False
            continue
        old_source = candidate.get("old_source")
        if not isinstance(old_source, str):
            _error(errors, f"{candidate_path}.old_source", "expected string")
            order_is_valid = False
        else:
            if old_source in seen:
                _error(errors, f"{candidate_path}.old_source", "must be unique")
            seen.add(old_source)
            if old_source not in deleted_keys:
                _error(
                    errors,
                    f"{candidate_path}.old_source",
                    "must reference a deleted source",
                )

        score = candidate.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0.0 <= score <= 1.0
        ):
            _error(errors, f"{candidate_path}.score", "expected finite number from 0.0 to 1.0")
            order_is_valid = False
        elif isinstance(old_source, str):
            order.append((-float(score), old_source))

        if candidate.get("match_kind") not in _MATCH_KINDS:
            _error(
                errors,
                f"{candidate_path}.match_kind",
                "expected normalized_exact, placeholder_shape, or similarity",
            )
        signals = candidate.get("signals")
        if not isinstance(signals, dict):
            _error(errors, f"{candidate_path}.signals", "expected object")
        else:
            for name in _SIGNAL_NAMES:
                if not isinstance(signals.get(name), bool):
                    _error(errors, f"{candidate_path}.signals.{name}", "expected boolean")

    if order_is_valid and len(order) == len(candidates) and order != sorted(order):
        _error(errors, path, "candidates are not in deterministic order")


def _validate_nonempty_string(errors: list[str], path: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        _error(errors, path, "expected non-empty string")


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _entry_path(section: str, source: object) -> str:
    return f"{section}.{source}" if isinstance(source, str) else f"{section}[{source!r}]"


def _error(errors: list[str], path: str, message: str) -> None:
    errors.append(f"{path}: {message}")
