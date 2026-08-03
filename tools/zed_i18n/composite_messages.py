from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .rust_ast import make_rust_parser, node_text, walk_nodes
from .rust_strings import (
    rewrite_rust_positional_placeholders,
    rust_format_placeholders_compatible,
    rust_string_literal,
    rust_zero_precision_placeholder,
)


@dataclass(frozen=True)
class SuppressedArgument:
    index: int
    producer: str
    type: Literal["string"]


@dataclass(frozen=True)
class CompositeMessageRule:
    id: str
    file: str
    enclosing_fn: str
    fingerprint: str
    ordinal: int
    anchor: str | None
    virtual_source: str
    kind: str
    call: str
    visible_args: tuple[int, ...]
    suppressed_args: tuple[SuppressedArgument, ...]
    translation_note: str


@dataclass(frozen=True)
class CompositeMessageMatch:
    rule: CompositeMessageRule
    literal_start_byte: int
    literal_end_byte: int
    line: int


_PROJECT_RULES_FILE = "crates/agent_ui/src/conversation_view/thread_view.rs"
_COMPOSITE_MESSAGE_RULES = (
    CompositeMessageRule(
        id="agent.project_rules_count",
        file=_PROJECT_RULES_FILE,
        enclosing_fn="TokenUsageTooltip::render",
        fingerprint=(
            'format!("{} {}",project_rules_count,'
            'pluralize("project rule",project_rules_count))'
        ),
        ordinal=0,
        anchor="open-project-rules",
        virtual_source="{} project rules",
        kind="button",
        call="Button::new",
        visible_args=(0,),
        suppressed_args=(
            SuppressedArgument(
                index=1,
                producer='pluralize("project rule",project_rules_count)',
                type="string",
            ),
        ),
        translation_note=(
            'Complete compact count label next to "1 global rule". The placeholder is '
            "the project-rule count; keep one template natural for positive counts."
        ),
    ),
)


def find_composite_message_matches(
    source_bytes: bytes,
    relative_path: str,
) -> list[CompositeMessageMatch]:
    rules = tuple(rule for rule in _COMPOSITE_MESSAGE_RULES if rule.file == relative_path)
    if not rules:
        return []

    tree = make_rust_parser().parse(source_bytes)
    matches: list[CompositeMessageMatch] = []
    for rule in rules:
        candidates: list[CompositeMessageMatch] = []
        for node in walk_nodes(tree.root_node):
            if node.type != "macro_invocation" or _fingerprint(source_bytes, node) != rule.fingerprint:
                continue
            if _enclosing_function(source_bytes, node) != rule.enclosing_fn:
                continue
            if rule.anchor is not None and not _has_call_anchor(source_bytes, node, rule):
                continue

            arguments = _macro_arguments(source_bytes, node)
            if len(arguments) < 2:
                continue
            format_arguments = arguments[1:]
            if any(index >= len(format_arguments) for index in rule.visible_args):
                continue
            if any(
                argument.index >= len(format_arguments)
                or argument.type != "string"
                or format_arguments[argument.index][0] != argument.producer
                for argument in rule.suppressed_args
            ):
                continue

            literal_node = _first_node_of_type(arguments[0][1], "string_literal")
            if literal_node is None:
                continue
            candidates.append(
                CompositeMessageMatch(
                    rule=rule,
                    literal_start_byte=literal_node.start_byte,
                    literal_end_byte=literal_node.end_byte,
                    line=literal_node.start_point[0] + 1,
                )
            )
        if rule.ordinal < len(candidates):
            matches.append(candidates[rule.ordinal])
    return matches


def required_composite_message_rule_ids() -> frozenset[str]:
    return frozenset(rule.id for rule in _COMPOSITE_MESSAGE_RULES)


def render_composite_translation(
    rule: CompositeMessageRule,
    translation: str,
) -> str:
    if not rust_format_placeholders_compatible(rule.virtual_source, translation):
        raise ValueError(f"placeholder mismatch for composite message rule {rule.id!r}")
    rendered = rewrite_rust_positional_placeholders(translation, rule.visible_args)
    return rendered + "".join(
        rust_zero_precision_placeholder(argument.index) for argument in rule.suppressed_args
    )


def rewrite_composite_message_source(
    text: str,
    relative_path: str,
    rule_id: str,
    translation: str,
) -> str | None:
    source_bytes = text.encode("utf-8")
    matches = [
        match
        for match in find_composite_message_matches(source_bytes, relative_path)
        if match.rule.id == rule_id
    ]
    if len(matches) != 1:
        return None

    match = matches[0]
    replacement = rust_string_literal(
        render_composite_translation(match.rule, translation)
    ).encode("utf-8")
    rewritten = (
        source_bytes[: match.literal_start_byte]
        + replacement
        + source_bytes[match.literal_end_byte :]
    )
    return rewritten.decode("utf-8")


def _fingerprint(source_bytes: bytes, node) -> str:
    if not node.children:
        return node_text(source_bytes, node)
    return "".join(_fingerprint(source_bytes, child) for child in node.children)


def _macro_arguments(source_bytes: bytes, macro_node) -> list[tuple[str, tuple[object, ...]]]:
    token_tree = next(
        (child for child in macro_node.children if child.type == "token_tree"),
        None,
    )
    if token_tree is None:
        return []

    arguments: list[tuple[str, tuple[object, ...]]] = []
    current: list[object] = []
    for child in token_tree.children[1:-1]:
        if child.type == ",":
            if current:
                arguments.append(
                    ("".join(_fingerprint(source_bytes, part) for part in current), tuple(current))
                )
                current = []
            continue
        current.append(child)
    if current:
        arguments.append(
            ("".join(_fingerprint(source_bytes, part) for part in current), tuple(current))
        )
    return arguments


def _first_node_of_type(nodes: tuple[object, ...], node_type: str):
    for node in nodes:
        if node.type == node_type:
            return node
        nested = _first_node_of_type(tuple(node.children), node_type)
        if nested is not None:
            return nested
    return None


def _enclosing_function(source_bytes: bytes, node) -> str | None:
    function_node = None
    current = node.parent
    while current is not None:
        if current.type == "function_item":
            function_node = current
            break
        current = current.parent
    if function_node is None:
        return None

    name_node = function_node.child_by_field_name("name")
    if name_node is None:
        return None
    function_name = node_text(source_bytes, name_node)

    current = function_node.parent
    while current is not None:
        if current.type == "impl_item":
            type_node = current.child_by_field_name("type")
            if type_node is not None:
                return f"{node_text(source_bytes, type_node)}::{function_name}"
            break
        if current.type == "function_item":
            break
        current = current.parent
    return function_name


def _has_call_anchor(source_bytes: bytes, node, rule: CompositeMessageRule) -> bool:
    current = node.parent
    while current is not None and current.type != "function_item":
        if current.type == "call_expression":
            function_node = current.child_by_field_name("function")
            arguments_node = current.child_by_field_name("arguments")
            if function_node is not None and arguments_node is not None:
                call = _fingerprint(source_bytes, function_node)
                arguments = list(arguments_node.named_children)
                if call == rule.call and arguments:
                    anchor = node_text(source_bytes, arguments[0])
                    return anchor == f'"{rule.anchor}"'
        current = current.parent
    return False
