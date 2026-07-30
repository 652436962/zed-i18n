from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import ast
import re
import warnings

_RUST_UNICODE_ESCAPE_RE = re.compile(r"\\u\{([0-9A-Fa-f_]{1,6})\}")
_ZERO_PRECISION_FORMAT_SPEC = ".0"
_ZERO_PRECISION_SUFFIX_PLACEHOLDER = f"{{:{_ZERO_PRECISION_FORMAT_SPEC}}}"
_ZERO_PRECISION_SUFFIX_SOURCES = {
    "Resolve Merge Conflict{} with Agent": frozenset({0}),
    "Show {} warning{}": frozenset({1}),
    "{errors} error{}": frozenset({0}),
    "{warnings} warning{}": frozenset({0}),
    "{} Comment{}": frozenset({1}),
}


@dataclass(frozen=True)
class _RustFormatPlaceholder:
    raw: str
    start: int
    end: int
    argument: str
    format_spec: str


@dataclass(frozen=True)
class _RustFormatScan:
    placeholders: tuple[_RustFormatPlaceholder, ...]
    valid: bool


def rust_format_placeholders(text: str) -> list[str]:
    return [placeholder.raw for placeholder in _scan_rust_format_string(text).placeholders]


def _scan_rust_format_string(text: str) -> _RustFormatScan:
    placeholders: list[_RustFormatPlaceholder] = []
    index = 0
    while index < len(text):
        char = text[index]
        unicode_escape = _RUST_UNICODE_ESCAPE_RE.match(text, index)
        if unicode_escape is not None and _valid_rust_unicode_escape(unicode_escape.group(1)):
            index = unicode_escape.end()
            continue
        if char == "{" and index + 1 < len(text) and text[index + 1] == "{":
            index += 2
            continue
        if char == "}" and index + 1 < len(text) and text[index + 1] == "}":
            index += 2
            continue
        if char == "}":
            return _RustFormatScan(tuple(placeholders), False)
        if char == "{":
            end = text.find("}", index + 1)
            if end == -1:
                return _RustFormatScan(tuple(placeholders), False)
            inner = text[index + 1 : end]
            if "{" in inner:
                return _RustFormatScan(tuple(placeholders), False)
            argument, separator, spec = inner.partition(":")
            placeholders.append(
                _RustFormatPlaceholder(
                    raw=text[index : end + 1],
                    start=index,
                    end=end + 1,
                    argument=argument,
                    format_spec=f":{spec}" if separator else "",
                )
            )
            index = end + 1
            continue
        index += 1
    return _RustFormatScan(tuple(placeholders), True)


def rust_format_placeholders_compatible(source: str, translation: str) -> bool:
    source_scan = _scan_rust_format_string(source)
    translation_scan = _scan_rust_format_string(translation)
    if not source_scan.valid:
        return _legacy_rust_format_placeholders_compatible(source, translation)
    if not translation_scan.valid:
        return False
    if _uses_dynamic_format_arguments(source_scan) or _uses_dynamic_format_arguments(
        translation_scan
    ):
        return _legacy_rust_format_placeholders_compatible(source, translation)

    source_positional, source_named = _resolved_placeholder_profile(source_scan)
    translation_positional, translation_named = _resolved_placeholder_profile(translation_scan)
    if source_named != translation_named:
        return False

    normalized_translation = translation_positional.copy()
    for argument_index in _zero_precision_argument_indices(source, source_scan):
        replacement_count = normalized_translation.pop(
            (argument_index, f":{_ZERO_PRECISION_FORMAT_SPEC}"),
            0,
        )
        if replacement_count:
            normalized_translation[(argument_index, "")] += replacement_count
    return source_positional == normalized_translation


def rewrite_rust_positional_placeholders(
    text: str,
    real_indices: tuple[int, ...],
) -> str:
    scan = _scan_rust_format_string(text)
    if not scan.valid:
        raise ValueError("invalid Rust format string")
    if _uses_dynamic_format_arguments(scan):
        raise ValueError("dynamic Rust format arguments cannot be remapped")

    rewritten: list[str] = []
    previous_end = 0
    implicit_index = 0
    for placeholder in scan.placeholders:
        rewritten.append(text[previous_end : placeholder.start])
        if placeholder.argument == "":
            virtual_index = implicit_index
            implicit_index += 1
        elif placeholder.argument.isdecimal():
            virtual_index = int(placeholder.argument)
        else:
            rewritten.append(placeholder.raw)
            previous_end = placeholder.end
            continue
        if virtual_index >= len(real_indices):
            raise ValueError(f"virtual Rust format argument out of range: {virtual_index}")
        rewritten.append(f"{{{real_indices[virtual_index]}{placeholder.format_spec}}}")
        previous_end = placeholder.end
    rewritten.append(text[previous_end:])
    return "".join(rewritten)


def rust_zero_precision_placeholder(index: int) -> str:
    if index < 0:
        raise ValueError("Rust format argument index must be non-negative")
    return f"{{{index}:{_ZERO_PRECISION_FORMAT_SPEC}}}"


def _legacy_rust_format_placeholders_compatible(source: str, translation: str) -> bool:
    source_implicit, source_explicit = _rust_format_placeholder_profile(source)
    translation_implicit, translation_explicit = _rust_format_placeholder_profile(translation)
    return (
        _implicit_placeholders_compatible(source, source_implicit, translation_implicit)
        and source_explicit == translation_explicit
    )


def _uses_dynamic_format_arguments(scan: _RustFormatScan) -> bool:
    return any(
        "$" in placeholder.format_spec or ".*" in placeholder.format_spec
        for placeholder in scan.placeholders
    )


def _resolved_placeholder_profile(
    scan: _RustFormatScan,
) -> tuple[Counter[tuple[int, str]], Counter[str]]:
    positional: Counter[tuple[int, str]] = Counter()
    named: Counter[str] = Counter()
    implicit_index = 0
    for placeholder in scan.placeholders:
        if placeholder.argument == "":
            positional[(implicit_index, placeholder.format_spec)] += 1
            implicit_index += 1
        elif placeholder.argument.isdecimal():
            positional[(int(placeholder.argument), placeholder.format_spec)] += 1
        else:
            named[placeholder.raw] += 1
    return positional, named


def _zero_precision_argument_indices(
    source: str,
    scan: _RustFormatScan,
) -> frozenset[int]:
    allowed_placeholder_indices = _ZERO_PRECISION_SUFFIX_SOURCES.get(source, frozenset())
    allowed_argument_indices: set[int] = set()
    implicit_index = 0
    for placeholder in scan.placeholders:
        if placeholder.argument != "":
            continue
        if implicit_index in allowed_placeholder_indices and placeholder.format_spec == "":
            allowed_argument_indices.add(implicit_index)
        implicit_index += 1
    return frozenset(allowed_argument_indices)


def _implicit_placeholders_compatible(
    source: str,
    source_placeholders: list[str],
    translation_placeholders: list[str],
) -> bool:
    if source_placeholders == translation_placeholders:
        return True
    if len(source_placeholders) != len(translation_placeholders):
        return False

    zero_precision_indices = _ZERO_PRECISION_SUFFIX_SOURCES.get(source, frozenset())
    for index, (source_placeholder, translation_placeholder) in enumerate(
        zip(source_placeholders, translation_placeholders, strict=True)
    ):
        if source_placeholder == translation_placeholder:
            continue
        if (
            index in zero_precision_indices
            and source_placeholder == "{}"
            and translation_placeholder == _ZERO_PRECISION_SUFFIX_PLACEHOLDER
        ):
            continue
        return False
    return True


def _rust_format_placeholder_profile(text: str) -> tuple[list[str], Counter[str]]:
    implicit: list[str] = []
    explicit: Counter[str] = Counter()
    for placeholder in rust_format_placeholders(text):
        if _is_implicit_rust_format_placeholder(placeholder):
            implicit.append(placeholder)
        else:
            explicit[placeholder] += 1
    return implicit, explicit


def _is_implicit_rust_format_placeholder(placeholder: str) -> bool:
    inner = placeholder[1:-1]
    return inner == "" or inner.startswith(":")


def parse_rust_string_literal(literal: str) -> str:
    if literal.startswith('"') and literal.endswith('"'):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                return ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            return literal[1:-1]
    return literal


def rust_string_literal(value: str) -> str:
    escaped: list[str] = ['"']
    index = 0
    while index < len(value):
        unicode_escape = _RUST_UNICODE_ESCAPE_RE.match(value, index)
        if unicode_escape is not None and _valid_rust_unicode_escape(unicode_escape.group(1)):
            escaped.append(unicode_escape.group(0))
            index = unicode_escape.end()
            continue

        char = value[index]
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        else:
            escaped.append(char)
        index += 1
    escaped.append('"')
    return "".join(escaped)


def _valid_rust_unicode_escape(digits: str) -> bool:
    try:
        codepoint = int(digits.replace("_", ""), 16)
    except ValueError:
        return False
    return codepoint <= 0x10FFFF and not 0xD800 <= codepoint <= 0xDFFF
