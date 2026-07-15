from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import shutil
import subprocess
import tomllib
import unittest
from unittest.mock import patch

from tools.zed_i18n.version_diff import (
    GeneratedVersionDiff,
    VersionSnapshot,
    build_version_diff,
    generate_version_diff,
    validate_version_diff_report,
)
from tools.zed_i18n.version_references import load_version_references


BASE_COMMIT = "a" * 40


def _occurrence(
    file: str,
    line: int,
    *,
    kind: str = "label",
    call: str = "Label::new",
    start_byte: int = 0,
    end_byte: int = 1,
) -> dict[str, object]:
    return {
        "file": file,
        "line": line,
        "kind": kind,
        "call": call,
        "start_byte": start_byte,
        "end_byte": end_byte,
    }


def _snapshot(
    version: str,
    sources: list[str],
    *,
    manifest: dict[str, dict[str, object]] | None = None,
    translations: dict[str, dict[str, str]] | None = None,
) -> VersionSnapshot:
    return VersionSnapshot(
        version=version,
        catalog={source: source for source in sources},
        manifest=manifest
        or {
            source: {"status": "accepted", "occurrences": []}
            for source in sources
        },
        translations=translations or {},
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(key for key in value if isinstance(key, str)),
            *(nested for item in value.values() for nested in _all_keys(item)),
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _all_keys(item)}
    return set()


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class VersionDiffGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path.cwd() / "tests" / ".tmp" / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_current(
        self,
        *,
        version: str = "v2.0.0",
        catalog_text: str | None = None,
        manifest_text: str | None = None,
        config_text: str | None = None,
    ) -> None:
        config = self.tmp / "config" / "project.toml"
        catalog = self.tmp / "catalog" / "en-US.json"
        manifest = self.tmp / "manifest" / "ui-strings.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        catalog.parent.mkdir(parents=True, exist_ok=True)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            config_text if config_text is not None else f'zed_version = "{version}"\n',
            encoding="utf-8",
        )
        catalog.write_text(
            catalog_text
            if catalog_text is not None
            else json.dumps({"Open Settings!": "Open Settings!"}),
            encoding="utf-8",
        )
        manifest.write_text(
            manifest_text
            if manifest_text is not None
            else json.dumps(
                {
                    "Open Settings!": {
                        "status": "accepted",
                        "occurrences": [_occurrence("crates/new.rs", 20)],
                    }
                }
            ),
            encoding="utf-8",
        )

    def _git_results(
        self,
        *,
        old_version: str = "v1.0.0",
        translation_text: str = '{"Open Settings": "설정 열기"}',
        config_text: str | None = None,
        catalog_text: str | None = None,
        manifest_text: str | None = None,
        tree_text: str = "\n".join(
            [
                "translations/ko-KR.json",
                "translations/ko-KR.gpt-5.5.json",
                "translations/nested/ja-JP.json",
                "",
            ]
        ),
    ) -> list[subprocess.CompletedProcess[str]]:
        return [
            _completed(BASE_COMMIT + "\n"),
            _completed(
                config_text
                if config_text is not None
                else f'zed_version = "{old_version}"\n'
            ),
            _completed(
                catalog_text
                if catalog_text is not None
                else '{"Open Settings": "Open Settings"}'
            ),
            _completed(
                manifest_text
                if manifest_text is not None
                else json.dumps(
                    {
                        "Open Settings": {
                            "status": "accepted",
                            "occurrences": [_occurrence("crates/old.rs", 10)],
                        }
                    }
                )
            ),
            _completed(tree_text),
            _completed(translation_text),
        ]

    def test_resolves_once_uses_commit_for_all_reads_and_writes_report(self) -> None:
        self._write_current()

        with patch(
            "tools.zed_i18n.version_diff.subprocess.run",
            side_effect=self._git_results(),
        ) as run:
            generated = generate_version_diff(self.tmp, base_ref="release/v1")

        self.assertIsInstance(generated, GeneratedVersionDiff)
        expected_path = (
            self.tmp
            / "reports"
            / "version-diff"
            / "v1.0.0-to-v2.0.0"
            / "key-changes.json"
        ).resolve()
        self.assertEqual(generated.output_path, expected_path)
        self.assertEqual(generated.report["from_version"], "v1.0.0")
        self.assertEqual(generated.report["to_version"], "v2.0.0")
        self.assertEqual(generated.report["base_commit"], BASE_COMMIT)
        self.assertEqual(
            generated.report["deleted"]["Open Settings"]["translations"],
            {"ko-KR": "설정 열기"},
        )
        self.assertEqual(
            json.loads(expected_path.read_text(encoding="utf-8")),
            generated.report,
        )
        self.assertTrue(expected_path.read_bytes().endswith(b"\n"))
        self.assertIn("설정 열기".encode("utf-8"), expected_path.read_bytes())

        commands = [entry.args[0] for entry in run.call_args_list]
        read_only_subcommands = {"rev-parse", "show", "ls-tree"}
        expected_prefix = [
            "git",
            "-c",
            f"safe.directory={self.tmp.resolve().as_posix()}",
        ]
        for command in commands:
            self.assertEqual(command[:3], expected_prefix)
            self.assertIn(command[3], read_only_subcommands)
        subcommands = [command[3] for command in commands]
        self.assertEqual(subcommands.count("rev-parse"), 1)
        rev_parse = next(command for command in commands if "rev-parse" in command)
        self.assertEqual(
            rev_parse[rev_parse.index("rev-parse") + 1 :],
            ["--verify", "release/v1^{commit}"],
        )
        ls_tree_tails = [
            command[command.index("ls-tree") + 1 :]
            for command in commands
            if "ls-tree" in command
        ]
        self.assertEqual(
            ls_tree_tails,
            [["-r", "--name-only", BASE_COMMIT, "--", "translations"]],
        )
        for command in commands:
            if "show" in command:
                revision_path = command[command.index("show") + 1]
                self.assertTrue(revision_path.startswith(f"{BASE_COMMIT}:"))
        shown_paths = {
            command[command.index("show") + 1].split(":", 1)[1]
            for command in commands
            if "show" in command
        }
        self.assertEqual(
            shown_paths,
            {
                "config/project.toml",
                "catalog/en-US.json",
                "manifest/ui-strings.json",
                "translations/ko-KR.json",
            },
        )
        for entry in run.call_args_list:
            self.assertEqual(entry.kwargs["cwd"], self.tmp.resolve())
            self.assertTrue(entry.kwargs["check"])

    def test_replace_failure_preserves_existing_report_and_removes_temp(self) -> None:
        self._write_current()
        output_path = (
            self.tmp
            / "reports"
            / "version-diff"
            / "v1.0.0-to-v2.0.0"
            / "key-changes.json"
        )
        output_path.parent.mkdir(parents=True)
        old_bytes = b'{"existing":true}\r\n'
        output_path.write_bytes(old_bytes)

        with (
            patch(
                "tools.zed_i18n.version_diff.subprocess.run",
                side_effect=self._git_results(),
            ),
            patch(
                "tools.zed_i18n.version_diff.os.replace",
                side_effect=OSError("replace failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                generate_version_diff(self.tmp)

        self.assertEqual(output_path.read_bytes(), old_bytes)
        self.assertEqual(list(output_path.parent.iterdir()), [output_path])

    def test_rejects_linked_output_components_without_mutation(self) -> None:
        self._write_current()
        reports_path = self.tmp / "reports"
        version_diff_path = reports_path / "version-diff"
        version_path = version_diff_path / "v1.0.0-to-v2.0.0"
        output_path = version_path / "key-changes.json"
        components = {
            "reports": reports_path,
            "version-diff": version_diff_path,
            "version-directory": version_path,
            "report": output_path,
        }
        link_cases = [
            ("symlink", component_name, linked_path)
            for component_name, linked_path in components.items()
        ]
        link_cases.append(("junction", "version-directory", version_path))
        sentinel_bytes = b'{"sentinel":true}\r\n'

        for link_kind, component_name, linked_path in link_cases:
            with self.subTest(
                link_kind=link_kind,
                component=component_name,
            ):
                shutil.rmtree(reports_path, ignore_errors=True)
                output_path.parent.mkdir(parents=True)
                output_path.write_bytes(sentinel_bytes)

                def is_mocked_link(path: Path) -> bool:
                    return path == linked_path

                with (
                    patch(
                        "tools.zed_i18n.version_diff.subprocess.run",
                        side_effect=self._git_results(),
                    ),
                    patch.object(
                        Path,
                        "is_symlink",
                        autospec=True,
                        side_effect=(
                            is_mocked_link
                            if link_kind == "symlink"
                            else lambda path: False
                        ),
                    ),
                    patch.object(
                        Path,
                        "is_junction",
                        autospec=True,
                        side_effect=(
                            is_mocked_link
                            if link_kind == "junction"
                            else lambda path: False
                        ),
                    ),
                    patch(
                        "tools.zed_i18n.version_diff._write_json_atomically"
                    ) as write_json,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "link|junction",
                    ):
                        generate_version_diff(self.tmp)

                write_json.assert_not_called()
                self.assertEqual(output_path.read_bytes(), sentinel_bytes)
                self.assertEqual(list(output_path.parent.iterdir()), [output_path])

    def test_equal_versions_fail_without_creating_output(self) -> None:
        self._write_current(version="v1.0.0")

        with patch(
            "tools.zed_i18n.version_diff.subprocess.run",
            side_effect=self._git_results(old_version="v1.0.0"),
        ):
            with self.assertRaisesRegex(ValueError, "different"):
                generate_version_diff(self.tmp)

        self.assertFalse((self.tmp / "reports" / "version-diff").exists())

    def test_malformed_current_json_fails_without_creating_output(self) -> None:
        self._write_current(catalog_text="{")

        with patch(
            "tools.zed_i18n.version_diff.subprocess.run",
            side_effect=self._git_results(),
        ):
            with self.assertRaises(json.JSONDecodeError):
                generate_version_diff(self.tmp)

        self.assertFalse((self.tmp / "reports" / "version-diff").exists())

    def test_malformed_baseline_toml_fails_without_creating_output(self) -> None:
        self._write_current()

        with patch(
            "tools.zed_i18n.version_diff.subprocess.run",
            side_effect=self._git_results(config_text='zed_version = "unterminated'),
        ):
            with self.assertRaises(tomllib.TOMLDecodeError):
                generate_version_diff(self.tmp)

        self.assertFalse((self.tmp / "reports" / "version-diff").exists())

    def test_git_failure_surfaces_stderr_without_creating_output(self) -> None:
        self._write_current()
        failure = subprocess.CalledProcessError(
            128,
            ["git", "rev-parse"],
            stderr="fatal: Needed a single revision\n",
        )

        with patch(
            "tools.zed_i18n.version_diff.subprocess.run", side_effect=failure
        ):
            with self.assertRaisesRegex(
                ValueError,
                r"git rev-parse failed with exit status 128: "
                r"fatal: Needed a single revision",
            ):
                generate_version_diff(self.tmp)

        self.assertFalse((self.tmp / "reports" / "version-diff").exists())

    def test_rejects_non_string_historical_translation_before_writing(self) -> None:
        self._write_current()

        with patch(
            "tools.zed_i18n.version_diff.subprocess.run",
            side_effect=self._git_results(
                translation_text='{"Open Settings": 7}'
            ),
        ):
            with self.assertRaisesRegex(ValueError, "translations.ko-KR"):
                generate_version_diff(self.tmp)

        self.assertFalse((self.tmp / "reports" / "version-diff").exists())

    def test_rejects_unsafe_version_components_before_writing(self) -> None:
        unsafe_baseline_versions = (
            "v1/child",
            r"v1\child",
            ".",
            "..",
            "C:relative",
            r"C:\absolute",
        )
        cases = [
            ("from_version", version) for version in unsafe_baseline_versions
        ]
        cases.append(("to_version", "v2/child"))

        for error_field, version in cases:
            with self.subTest(error_field=error_field, version=version):
                shutil.rmtree(self.tmp / "reports", ignore_errors=True)
                if error_field == "from_version":
                    self._write_current()
                    git_results = self._git_results(
                        config_text=f"zed_version = '{version}'\n"
                    )
                else:
                    self._write_current(
                        config_text=f"zed_version = '{version}'\n"
                    )
                    git_results = self._git_results()

                with patch(
                    "tools.zed_i18n.version_diff.subprocess.run",
                    side_effect=git_results,
                ):
                    with self.assertRaisesRegex(ValueError, error_field):
                        generate_version_diff(self.tmp)
                self.assertFalse(
                    (self.tmp / "reports" / "version-diff").exists()
                )


class VersionDiffBuildTests(unittest.TestCase):
    def test_reports_exact_key_sets_and_nonempty_historical_translations(self) -> None:
        old_occurrences = [_occurrence("crates/old.rs", 7)]
        old = _snapshot(
            "v1.0.0",
            ["Keep", "Old translated", "Old without translation"],
            manifest={
                "Keep": {"status": "accepted", "occurrences": []},
                "Old translated": {
                    "status": "needs_review",
                    "occurrences": old_occurrences,
                },
                "Old without translation": {
                    "status": "ignored",
                    "occurrences": [],
                },
            },
            translations={
                "ko-KR": {
                    "Keep": "유지",
                    "Old translated": "이전 번역",
                    "Old without translation": "",
                },
                "ja-JP": {"Old translated": "以前の翻訳"},
            },
        )
        new_occurrences = [_occurrence("crates/new.rs", 11)]
        new = _snapshot(
            "v1.1.0",
            ["Keep", "Brand new"],
            manifest={
                "Keep": {"status": "accepted", "occurrences": []},
                "Brand new": {
                    "status": "needs_review",
                    "occurrences": new_occurrences,
                },
            },
        )

        report = build_version_diff(old, new, base_commit=BASE_COMMIT)

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["from_version"], "v1.0.0")
        self.assertEqual(report["to_version"], "v1.1.0")
        self.assertEqual(report["base_commit"], BASE_COMMIT)
        self.assertEqual(
            set(report["deleted"]),
            {"Old translated", "Old without translation"},
        )
        self.assertEqual(set(report["added"]), {"Brand new"})
        self.assertEqual(
            report["deleted"]["Old translated"],
            {
                "status": "needs_review",
                "occurrences": old_occurrences,
                "translations": {
                    "ja-JP": "以前の翻訳",
                    "ko-KR": "이전 번역",
                },
            },
        )
        self.assertEqual(
            report["deleted"]["Old without translation"]["translations"],
            {},
        )
        self.assertEqual(
            report["added"]["Brand new"]["occurrences"], new_occurrences
        )
        self.assertEqual(report["added"]["Brand new"]["candidates"], [])
        self.assertEqual(
            report["summary"], {"added": 1, "deleted": 2, "candidate_pairs": 0}
        )

    def test_uses_all_occurrences_and_stably_limits_tied_candidates_to_three(self) -> None:
        old_sources = ["OPEN FILE", "Open File!", "Open-File", "open file."]
        old_manifest = {
            source: {
                "status": "accepted",
                "occurrences": [_occurrence(f"crates/{index}.rs", index + 1)],
            }
            for index, source in enumerate(old_sources)
        }
        old_manifest["Open File!"]["occurrences"] = [
            _occurrence("crates/unrelated.rs", 1, kind="button", call="Button::new"),
            _occurrence("crates/shared.rs", 2, kind="label", call="Label::new"),
        ]
        new_source = "Open file?"
        new_manifest = {
            new_source: {
                "status": "accepted",
                "occurrences": [
                    _occurrence(
                        "crates/other.rs", 10, kind="button", call="Button::new"
                    ),
                    _occurrence("crates/shared.rs", 11, kind="label", call="Label::new"),
                ],
            }
        }
        old = _snapshot(
            "v1",
            old_sources,
            manifest=old_manifest,
            translations={
                "ko-KR": {source: f"번역 {index}" for index, source in enumerate(old_sources)}
            },
        )
        new = _snapshot("v2", [new_source], manifest=new_manifest)

        report = build_version_diff(old, new, base_commit=BASE_COMMIT)
        candidates = report["added"][new_source]["candidates"]

        self.assertEqual(
            [candidate["old_source"] for candidate in candidates],
            sorted(old_sources)[:3],
        )
        shared = next(
            candidate
            for candidate in candidates
            if candidate["old_source"] == "Open File!"
        )
        self.assertEqual(shared["score"], 1.0)
        self.assertEqual(shared["match_kind"], "normalized_exact")
        self.assertEqual(
            shared["signals"],
            {
                "normalized_text_equal": True,
                "placeholder_shape_equal": True,
                "same_file": True,
                "same_kind": True,
                "same_call": True,
            },
        )

    def test_one_deleted_source_can_reference_multiple_added_sources(self) -> None:
        old = _snapshot(
            "v1",
            ["Save file"],
            translations={"ko-KR": {"Save file": "파일 저장"}},
        )
        new = _snapshot("v2", ["Save file!", "Save file?"])

        report = build_version_diff(old, new, base_commit=BASE_COMMIT)

        self.assertEqual(
            report["added"]["Save file!"]["candidates"][0]["old_source"],
            "Save file",
        )
        self.assertEqual(
            report["added"]["Save file?"]["candidates"][0]["old_source"],
            "Save file",
        )

    def test_candidate_limit_is_respected_and_schema_maximum_is_enforced(self) -> None:
        old_sources = ["OPEN FILE", "Open File!", "Open-File", "open file."]
        old = _snapshot(
            "v1",
            old_sources,
            translations={"ko-KR": {source: "과거" for source in old_sources}},
        )
        new = _snapshot("v2", ["Open file?"])

        for max_candidates in (1, 2, 3):
            with self.subTest(max_candidates=max_candidates):
                report = build_version_diff(
                    old,
                    new,
                    base_commit=BASE_COMMIT,
                    max_candidates=max_candidates,
                )
                self.assertEqual(
                    [
                        candidate["old_source"]
                        for candidate in report["added"]["Open file?"]["candidates"]
                    ],
                    sorted(old_sources)[:max_candidates],
                )

        with self.assertRaisesRegex(ValueError, "max_candidates"):
            build_version_diff(
                old,
                new,
                base_commit=BASE_COMMIT,
                max_candidates=4,
            )

    def test_placeholder_rename_is_a_placeholder_shape_match_without_semantics(self) -> None:
        old_source = "{field_name} must be a number"
        new_source = "{title} must be a number"
        old = _snapshot(
            "v1",
            [old_source],
            translations={"ko-KR": {old_source: "{field_name}은 숫자여야 합니다"}},
        )
        new = _snapshot("v2", [new_source])

        report = build_version_diff(old, new, base_commit=BASE_COMMIT)
        candidate = report["added"][new_source]["candidates"][0]

        self.assertEqual(candidate["score"], 1.0)
        self.assertEqual(candidate["match_kind"], "placeholder_shape")
        self.assertFalse(candidate["signals"]["normalized_text_equal"])
        self.assertTrue(candidate["signals"]["placeholder_shape_equal"])
        self.assertNotIn("relation", _all_keys(report))

    def test_placeholder_scan_preserves_escaped_braces_and_valid_unicode_escapes(self) -> None:
        old_sources = [
            "Show {{alpha}} {field_name}",
            "Code \\u{2026} {field_name}",
        ]
        new_sources = [
            "Show {{beta}} {title}",
            "Code \\u{2027} {title}",
        ]
        old = _snapshot(
            "v1",
            old_sources,
            translations={"ko-KR": {source: "과거" for source in old_sources}},
        )
        new = _snapshot("v2", new_sources)

        report = build_version_diff(
            old, new, base_commit=BASE_COMMIT, min_score=0.0
        )

        for source in new_sources:
            best = report["added"][source]["candidates"][0]
            self.assertEqual(best["match_kind"], "similarity")
            self.assertFalse(best["signals"]["placeholder_shape_equal"])

    def test_invalid_unicode_escapes_are_scanned_as_placeholders(self) -> None:
        escape_pairs = [
            ("D800", "DFFF"),
            ("110000", "FFFFFF"),
            ("____", "____"),
        ]
        for old_digits, new_digits in escape_pairs:
            with self.subTest(old_digits=old_digits, new_digits=new_digits):
                old_source = f"Code \\u{{{old_digits}}} {{field_name}}"
                new_source = f"Code \\u{{{new_digits}}} {{title}}"
                old = _snapshot(
                    "v1",
                    [old_source],
                    translations={"ko-KR": {old_source: "과거"}},
                )
                new = _snapshot("v2", [new_source])

                report = build_version_diff(
                    old, new, base_commit=BASE_COMMIT, min_score=0.0
                )
                best = report["added"][new_source]["candidates"][0]

                self.assertEqual(best["match_kind"], "placeholder_shape")
                self.assertTrue(best["signals"]["placeholder_shape_equal"])

    def test_raw_normalization_casefolds_before_extracting_ascii_tokens(self) -> None:
        old_source = "Straße—File_42!"
        new_source = "STRASSE file 42"
        old = _snapshot(
            "v1",
            [old_source],
            translations={"ko-KR": {old_source: "과거"}},
        )
        new = _snapshot("v2", [new_source])

        report = build_version_diff(old, new, base_commit=BASE_COMMIT)
        candidate = report["added"][new_source]["candidates"][0]

        self.assertEqual(candidate["match_kind"], "normalized_exact")
        self.assertEqual(candidate["score"], 1.0)

    def test_similarity_uses_sequence_matcher_and_inclusive_threshold(self) -> None:
        old_source = "Save item"
        new_source = "Save items"
        expected = SequenceMatcher(
            None, "save item", "save items", autojunk=False
        ).ratio()
        old = _snapshot(
            "v1",
            [old_source],
            translations={"ko-KR": {old_source: "항목 저장"}},
        )
        new = _snapshot("v2", [new_source])

        included = build_version_diff(
            old, new, base_commit=BASE_COMMIT, min_score=expected
        )
        excluded = build_version_diff(
            old, new, base_commit=BASE_COMMIT, min_score=expected + 0.001
        )

        self.assertEqual(
            included["added"][new_source]["candidates"][0]["score"], expected
        )
        self.assertEqual(excluded["added"][new_source]["candidates"], [])

    def test_default_threshold_admits_unsaturated_similarity_candidates(self) -> None:
        old_source = "Delete branch"
        new_source = "Delete branches"
        expected = SequenceMatcher(
            None, "delete branch", "delete branches", autojunk=False
        ).ratio()
        self.assertGreater(expected, 0.70)
        self.assertLess(expected, 1.0)
        old = _snapshot(
            "v1",
            [old_source],
            translations={"ko-KR": {old_source: "브랜치 삭제"}},
        )
        new = _snapshot("v2", [new_source])

        report = build_version_diff(old, new, base_commit=BASE_COMMIT)
        candidates = report["added"][new_source]["candidates"]

        self.assertEqual(
            [candidate["old_source"] for candidate in candidates],
            [old_source],
        )
        self.assertEqual(candidates[0]["score"], expected)
        self.assertEqual(candidates[0]["match_kind"], "similarity")

    def test_unsaturated_score_adds_all_occurrence_bonuses_and_skips_untranslated(self) -> None:
        translated_source = "Alpha settings"
        untranslated_source = "Beta setting"
        new_source = "Beta settings"
        shared_occurrence = _occurrence(
            "crates/shared.rs", 20, kind="menu", call="Menu::new"
        )
        old = _snapshot(
            "v1",
            [translated_source, untranslated_source],
            manifest={
                translated_source: {
                    "status": "accepted",
                    "occurrences": [
                        _occurrence(
                            "crates/old.rs", 1, kind="label", call="Label::new"
                        ),
                        shared_occurrence,
                    ],
                },
                untranslated_source: {"status": "accepted", "occurrences": []},
            },
            translations={"ko-KR": {translated_source: "알파 설정"}},
        )
        new = _snapshot(
            "v2",
            [new_source],
            manifest={
                new_source: {
                    "status": "accepted",
                    "occurrences": [
                        _occurrence(
                            "crates/new.rs", 10, kind="button", call="Button::new"
                        ),
                        shared_occurrence,
                    ],
                }
            },
        )
        base_score = SequenceMatcher(
            None, "alpha settings", "beta settings", autojunk=False
        ).ratio()
        expected_score = base_score + 0.10 + 0.06 + 0.04
        self.assertLess(expected_score, 1.0)

        report = build_version_diff(
            old, new, base_commit=BASE_COMMIT, min_score=0.0
        )
        candidates = report["added"][new_source]["candidates"]

        self.assertEqual(
            [candidate["old_source"] for candidate in candidates],
            [translated_source],
        )
        self.assertAlmostEqual(candidates[0]["score"], expected_score)

    def test_occurrence_signals_may_come_from_different_occurrence_pairs(self) -> None:
        old_source = "Alpha settings"
        new_source = "Beta settings"
        old = _snapshot(
            "v1",
            [old_source],
            manifest={
                old_source: {
                    "status": "accepted",
                    "occurrences": [
                        _occurrence(
                            "crates/file.rs", 1, kind="label", call="Label::new"
                        ),
                        _occurrence(
                            "crates/other.rs", 2, kind="menu", call="Menu::new"
                        ),
                    ],
                }
            },
            translations={"ko-KR": {old_source: "알파 설정"}},
        )
        new = _snapshot(
            "v2",
            [new_source],
            manifest={
                new_source: {
                    "status": "accepted",
                    "occurrences": [
                        _occurrence(
                            "crates/file.rs", 10, kind="menu", call="Other::new"
                        ),
                        _occurrence(
                            "crates/third.rs", 11, kind="button", call="Menu::new"
                        ),
                    ],
                }
            },
        )
        base_score = SequenceMatcher(
            None, "alpha settings", "beta settings", autojunk=False
        ).ratio()

        report = build_version_diff(
            old, new, base_commit=BASE_COMMIT, min_score=0.0
        )
        candidate = report["added"][new_source]["candidates"][0]

        self.assertEqual(
            candidate["signals"],
            {
                "normalized_text_equal": False,
                "placeholder_shape_equal": False,
                "same_file": True,
                "same_kind": True,
                "same_call": True,
            },
        )
        self.assertAlmostEqual(
            candidate["score"], base_score + 0.10 + 0.06 + 0.04
        )

    def test_rejects_catalog_manifest_key_mismatches_for_each_snapshot(self) -> None:
        valid_old = _snapshot("v1", ["Old"])
        valid_new = _snapshot("v2", ["New"])
        mismatched_old = VersionSnapshot(
            version="v1",
            catalog={"Old": "Old"},
            manifest={},
            translations={},
        )
        mismatched_new = VersionSnapshot(
            version="v2",
            catalog={"New": "New"},
            manifest={},
            translations={},
        )

        with self.assertRaisesRegex(ValueError, "old.*catalog.*manifest"):
            build_version_diff(mismatched_old, valid_new, base_commit=BASE_COMMIT)
        with self.assertRaisesRegex(ValueError, "new.*catalog.*manifest"):
            build_version_diff(valid_old, mismatched_new, base_commit=BASE_COMMIT)

    def test_rejects_invalid_build_parameters(self) -> None:
        old = _snapshot("v1", ["Old"])
        new = _snapshot("v2", ["New"])
        cases = [
            ({"base_commit": "abc"}, "base_commit"),
            ({"base_commit": "z" * 40}, "base_commit"),
            ({"base_commit": BASE_COMMIT, "min_score": -0.01}, "min_score"),
            ({"base_commit": BASE_COMMIT, "min_score": 1.01}, "min_score"),
            ({"base_commit": BASE_COMMIT, "min_score": math.nan}, "min_score"),
            ({"base_commit": BASE_COMMIT, "max_candidates": 0}, "max_candidates"),
        ]
        for kwargs, field in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, field):
                    build_version_diff(old, new, **kwargs)

        with self.assertRaisesRegex(ValueError, "version"):
            build_version_diff(
                _snapshot("", ["Old"]), new, base_commit=BASE_COMMIT
            )
        with self.assertRaisesRegex(ValueError, "version"):
            build_version_diff(
                old, _snapshot("", ["New"]), base_commit=BASE_COMMIT
            )
        with self.assertRaisesRegex(ValueError, "different"):
            build_version_diff(
                old, _snapshot("v1", ["New"]), base_commit=BASE_COMMIT
            )

    def test_rejects_malformed_translation_maps_with_named_errors(self) -> None:
        new = _snapshot("v2", ["New"])
        cases = [
            ({1: {"Old": "과거"}}, "old translations locale"),
            ({"": {"Old": "과거"}}, "old translations locale"),
            ({"ko-KR": []}, "old translations.ko-KR"),
            ({"ko-KR": {1: "과거"}}, "old translations.ko-KR source"),
            ({"ko-KR": {"Old": 7}}, "old translations.ko-KR.Old"),
        ]
        for translations, error in cases:
            with self.subTest(error=error):
                old = VersionSnapshot(
                    version="v1",
                    catalog={"Old": "Old"},
                    manifest={
                        "Old": {"status": "accepted", "occurrences": []}
                    },
                    translations=translations,  # type: ignore[arg-type]
                )
                with self.assertRaisesRegex(ValueError, error):
                    build_version_diff(old, new, base_commit=BASE_COMMIT)


class VersionDiffValidationTests(unittest.TestCase):
    def _valid_report(self) -> dict[str, object]:
        old_sources = ["OPEN FILE", "Open File!", "Open-File"]
        occurrences = [
            _occurrence(
                "crates/shared.rs",
                2,
                start_byte=12,
                end_byte=21,
            )
        ]
        old = _snapshot(
            "v1",
            old_sources,
            manifest={
                source: {"status": "accepted", "occurrences": occurrences}
                for source in old_sources
            },
            translations={
                "ko-KR": {source: f"번역 {index}" for index, source in enumerate(old_sources)}
            },
        )
        new = _snapshot(
            "v2",
            ["Open file?"],
            manifest={
                "Open file?": {"status": "needs_review", "occurrences": occurrences}
            },
        )
        return build_version_diff(old, new, base_commit=BASE_COMMIT)

    def assertHasError(self, report: object, path: str) -> None:
        errors = validate_version_diff_report(report)
        self.assertTrue(
            any(error == path or error.startswith(f"{path}:") for error in errors),
            errors,
        )

    def test_accepts_valid_schema_v1_and_ignores_unknown_fields(self) -> None:
        report = self._valid_report()
        report["future_top_level"] = True
        report["deleted"]["OPEN FILE"]["future_deleted"] = {"x": 1}
        report["deleted"]["OPEN FILE"]["occurrences"][0]["future_occurrence"] = 1
        report["added"]["Open file?"]["future_added"] = True
        report["added"]["Open file?"]["candidates"][0]["future_candidate"] = True
        report["added"]["Open file?"]["candidates"][0]["signals"]["future_signal"] = True

        self.assertEqual(validate_version_diff_report(report), [])

    def test_reports_named_top_level_type_and_identity_errors(self) -> None:
        self.assertHasError([], "report")
        cases = [
            ("schema_version", 2),
            ("schema_version", 1.0),
            ("schema_version", True),
            ("from_version", ""),
            ("to_version", 7),
            ("to_version", "v1"),
            ("base_commit", "g" * 40),
            ("summary", []),
            ("deleted", []),
            ("added", []),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                report = self._valid_report()
                report[field] = value
                self.assertHasError(report, field)

    def test_reports_named_summary_and_entry_errors(self) -> None:
        mutations = [
            ("summary.added", lambda report: report["summary"].__setitem__("added", 99)),
            (
                "summary.deleted",
                lambda report: report["summary"].__setitem__("deleted", -1),
            ),
            (
                "summary.candidate_pairs",
                lambda report: report["summary"].__setitem__("candidate_pairs", 0),
            ),
            (
                "deleted.OPEN FILE.status",
                lambda report: report["deleted"]["OPEN FILE"].__setitem__("status", 1),
            ),
            (
                "deleted.OPEN FILE.occurrences",
                lambda report: report["deleted"]["OPEN FILE"].__setitem__(
                    "occurrences", "bad"
                ),
            ),
            (
                "deleted.OPEN FILE.occurrences[0].line",
                lambda report: report["deleted"]["OPEN FILE"]["occurrences"][0].__setitem__(
                    "line", 0
                ),
            ),
            (
                "deleted.OPEN FILE.occurrences[0].start_byte",
                lambda report: report["deleted"]["OPEN FILE"]["occurrences"][0].__setitem__(
                    "start_byte", -1
                ),
            ),
            (
                "deleted.OPEN FILE.occurrences[0].file",
                lambda report: report["deleted"]["OPEN FILE"]["occurrences"][0].__setitem__(
                    "file", 3
                ),
            ),
            (
                "deleted.OPEN FILE.translations.ko-KR",
                lambda report: report["deleted"]["OPEN FILE"]["translations"].__setitem__(
                    "ko-KR", ""
                ),
            ),
            (
                "added.Open file?.status",
                lambda report: report["added"]["Open file?"].__setitem__("status", None),
            ),
            (
                "added.Open file?.candidates",
                lambda report: report["added"]["Open file?"].__setitem__(
                    "candidates", {}
                ),
            ),
        ]
        for path, mutate in mutations:
            with self.subTest(path=path):
                report = self._valid_report()
                mutate(report)
                self.assertHasError(report, path)

    def test_reports_broken_duplicate_invalid_and_unsorted_candidates(self) -> None:
        candidate_path = "added.Open file?.candidates"

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"][0]["old_source"] = "Missing"
        self.assertHasError(report, f"{candidate_path}[0].old_source")

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"][1]["old_source"] = report[
            "added"
        ]["Open file?"]["candidates"][0]["old_source"]
        self.assertHasError(report, f"{candidate_path}[1].old_source")

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"][0]["score"] = math.nan
        self.assertHasError(report, f"{candidate_path}[0].score")

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"][0]["score"] = True
        self.assertHasError(report, f"{candidate_path}[0].score")

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"][0] = "not an object"
        self.assertHasError(report, f"{candidate_path}[0]")

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"][0]["match_kind"] = "equivalent"
        self.assertHasError(report, f"{candidate_path}[0].match_kind")

        report = self._valid_report()
        del report["added"]["Open file?"]["candidates"][0]["signals"]["same_file"]
        self.assertHasError(report, f"{candidate_path}[0].signals.same_file")

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"][0]["signals"]["same_kind"] = 1
        self.assertHasError(report, f"{candidate_path}[0].signals.same_kind")

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"].reverse()
        self.assertHasError(report, candidate_path)

        report = self._valid_report()
        report["added"]["Open file?"]["candidates"].append(
            deepcopy(report["added"]["Open file?"]["candidates"][0])
        )
        self.assertHasError(report, candidate_path)


class VersionReferenceConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path.cwd() / "tests" / ".tmp" / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _report(
        self,
        *,
        from_version: str = "v1",
        to_version: str = "v2",
        old_occurrences: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "from_version": from_version,
            "to_version": to_version,
            "base_commit": BASE_COMMIT,
            "summary": {"added": 1, "deleted": 1, "candidate_pairs": 1},
            "deleted": {
                "Old source": {
                    "status": "accepted",
                    "occurrences": old_occurrences
                    if old_occurrences is not None
                    else [
                        {
                            "file": "unrelated.rs",
                            "line": 10,
                            "kind": "button",
                            "call": "Button::new",
                        },
                        {
                            "file": "same.rs",
                            "line": 20,
                            "kind": "label",
                            "call": "Label::new",
                        },
                    ],
                    "translations": {
                        "ja-JP": "以前の翻訳",
                        "ko-KR": "과거 번역",
                    },
                }
            },
            "added": {
                "New source": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "same.rs",
                            "line": 50,
                            "kind": "label",
                            "call": "Label::new",
                        }
                    ],
                    "candidates": [
                        {
                            "old_source": "Old source",
                            "score": 0.94,
                            "match_kind": "similarity",
                            "signals": {
                                "normalized_text_equal": False,
                                "placeholder_shape_equal": False,
                                "same_file": True,
                                "same_kind": True,
                                "same_call": True,
                            },
                        }
                    ],
                }
            },
        }

    def _write_report(
        self,
        report: object,
        *,
        directory: str = "v1-to-v2",
        text: str | None = None,
    ) -> Path:
        path = (
            self.tmp
            / "reports"
            / "version-diff"
            / directory
            / "key-changes.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            text
            if text is not None
            else json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def test_loads_one_valid_report_and_projects_only_the_exact_locale(self) -> None:
        path = self._write_report(self._report())

        loaded = load_version_references(self.tmp, "v2")
        references = loaded.index.references_for(
            "New source",
            "ko-KR",
            [{"file": "same.rs", "line": 50, "kind": "label", "call": "Label::new"}],
        )

        self.assertEqual(loaded.status, "loaded")
        self.assertEqual(
            loaded.path,
            path.relative_to(self.tmp).as_posix(),
        )
        self.assertEqual(loaded.warnings, [])
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["historical_translation"], "과거 번역")
        self.assertEqual(
            set(references[0]),
            {
                "old_source",
                "historical_translation",
                "score",
                "match_kind",
                "signals",
                "old_occurrence",
            },
        )
        self.assertEqual(
            references[0]["signals"],
            {"same_file": True, "same_kind": True, "same_call": True},
        )
        self.assertEqual(references[0]["old_occurrence"]["line"], 20)
        self.assertNotIn("translations", references[0])
        self.assertNotIn("old_occurrences", references[0])
        self.assertNotIn("ja-JP", json.dumps(references, ensure_ascii=False))
        self.assertEqual(
            loaded.index.references_for("New source", "fr-FR", []),
            [],
        )
        self.assertEqual(
            loaded.index.references_for("new source", "ko-KR", []),
            [],
        )

    def test_missing_malformed_stale_mismatched_and_ambiguous_reports_are_ignored(self) -> None:
        cases = (
            ("missing", "missing"),
            ("malformed", "invalid"),
            ("stale", "invalid"),
            ("path mismatch", "invalid"),
            ("ambiguous", "ambiguous"),
        )

        for category, expected_status in cases:
            with self.subTest(category=category):
                shutil.rmtree(self.tmp / "reports", ignore_errors=True)
                if category == "malformed":
                    self._write_report({}, text="{")
                elif category == "stale":
                    self._write_report(self._report(to_version="v3"))
                elif category == "path mismatch":
                    self._write_report(self._report(from_version="v0"))
                elif category == "ambiguous":
                    self._write_report(self._report(), directory="v1-to-v2")
                    self._write_report(
                        self._report(from_version="v0"),
                        directory="v0-to-v2",
                    )

                loaded = load_version_references(self.tmp, "v2")

                self.assertEqual(loaded.status, expected_status)
                self.assertTrue(
                    any(category in warning.lower() for warning in loaded.warnings),
                    loaded.warnings,
                )
                self.assertEqual(
                    loaded.index.references_for("New source", "ko-KR", []),
                    [],
                )

    def test_unsafe_current_versions_are_rejected_before_discovery(self) -> None:
        self._write_report(self._report())
        unsafe_versions = ("", ".", "..", "v2/../v2", "v2\\x", "C:relative")

        for version in unsafe_versions:
            with self.subTest(version=version):
                loaded = load_version_references(self.tmp, version)

                self.assertEqual(loaded.status, "invalid")
                self.assertTrue(
                    any(
                        "safe directory component" in warning
                        for warning in loaded.warnings
                    ),
                    loaded.warnings,
                )
                self.assertEqual(
                    loaded.index.references_for("New source", "ko-KR", []),
                    [],
                )

    def test_unsafe_from_version_directories_are_skipped_not_ambiguous(self) -> None:
        path = self._write_report(self._report())
        for name in ("-to-v2", "..-to-v2"):
            unsafe = self.tmp / "reports" / "version-diff" / name / "key-changes.json"
            unsafe.parent.mkdir(parents=True)
            unsafe.write_text("{}", encoding="utf-8")

        loaded = load_version_references(self.tmp, "v2")

        self.assertEqual(loaded.status, "loaded")
        self.assertEqual(loaded.path, path.relative_to(self.tmp).as_posix())
        self.assertEqual(
            loaded.index.references_for("New source", "ko-KR", [])[0][
                "historical_translation"
            ],
            "과거 번역",
        )

    def test_projection_keeps_best_old_occurrence_without_current_occurrences(self) -> None:
        self._write_report(
            self._report(
                old_occurrences=[
                    {"file": "z.rs", "line": 10},
                    {"file": "a.rs", "line": 20},
                ]
            )
        )
        loaded = load_version_references(self.tmp, "v2")

        references = loaded.index.references_for("New source", "ko-KR", [])

        self.assertEqual(references[0]["historical_translation"], "과거 번역")
        self.assertEqual(
            references[0]["old_occurrence"], {"file": "a.rs", "line": 20}
        )
        self.assertEqual(loaded.warnings, [])

    def test_projection_uses_the_normalized_occurrence_tuple_for_support_ties(self) -> None:
        self._write_report(
            self._report(
                old_occurrences=[
                    {"file": "z.rs", "line": 10},
                    {"file": "a.rs", "line": 20},
                ]
            )
        )
        loaded = load_version_references(self.tmp, "v2")

        references = loaded.index.references_for(
            "New source",
            "ko-KR",
            [
                {"file": "z.rs", "line": 100},
                {"file": "a.rs", "line": 200},
            ],
        )

        self.assertEqual(references[0]["old_occurrence"]["file"], "a.rs")

    def test_projection_preserves_global_signals_from_different_occurrence_pairs(self) -> None:
        self._write_report(
            self._report(
                old_occurrences=[
                    {
                        "file": "same.rs",
                        "line": 10,
                        "kind": "button",
                        "call": "Button::new",
                    },
                    {
                        "file": "other.rs",
                        "line": 20,
                        "kind": "label",
                        "call": "Button::new",
                    },
                    {
                        "file": "third.rs",
                        "line": 30,
                        "kind": "button",
                        "call": "Label::new",
                    },
                ]
            )
        )
        loaded = load_version_references(self.tmp, "v2")

        references = loaded.index.references_for(
            "New source",
            "ko-KR",
            [{"file": "same.rs", "line": 50, "kind": "label", "call": "Label::new"}],
        )

        self.assertEqual(references[0]["old_occurrence"]["file"], "same.rs")
        self.assertEqual(
            references[0]["signals"],
            {"same_file": True, "same_kind": True, "same_call": True},
        )

    def test_projection_exceptions_never_escape(self) -> None:
        self._write_report(self._report())
        loaded = load_version_references(self.tmp, "v2")

        class ExplodingOccurrences(list):
            def __iter__(self):
                raise RuntimeError("projection exploded")

        self.assertEqual(
            loaded.index.references_for(
                "New source",
                "ko-KR",
                ExplodingOccurrences(),
            ),
            [],
        )
        self.assertTrue(
            any("projection" in warning.lower() for warning in loaded.warnings),
            loaded.warnings,
        )

    def test_report_read_errors_are_nonblocking(self) -> None:
        self._write_report(self._report())

        with patch.object(
            Path,
            "read_text",
            side_effect=OSError("read denied"),
        ):
            loaded = load_version_references(self.tmp, "v2")

        self.assertEqual(loaded.status, "invalid")
        self.assertTrue(
            any("i/o" in warning.lower() for warning in loaded.warnings),
            loaded.warnings,
        )
        self.assertEqual(
            loaded.index.references_for("New source", "ko-KR", []),
            [],
        )

    def test_report_discovery_errors_are_nonblocking(self) -> None:
        (self.tmp / "reports" / "version-diff").mkdir(parents=True)

        with patch.object(
            Path,
            "iterdir",
            side_effect=PermissionError("discovery denied"),
        ):
            loaded = load_version_references(self.tmp, "v2")

        self.assertEqual(loaded.status, "invalid")
        self.assertTrue(
            any("discovery" in warning.lower() for warning in loaded.warnings),
            loaded.warnings,
        )
        self.assertEqual(
            loaded.index.references_for("New source", "ko-KR", []),
            [],
        )

    def test_symlink_or_junction_reports_are_ignored(self) -> None:
        self._write_report(self._report())

        with patch(
            "tools.zed_i18n.version_references._path_uses_link",
            return_value=True,
        ):
            loaded = load_version_references(self.tmp, "v2")

        self.assertEqual(loaded.status, "invalid")
        self.assertTrue(
            any("path mismatch" in warning.lower() for warning in loaded.warnings),
            loaded.warnings,
        )
        self.assertEqual(
            loaded.index.references_for("New source", "ko-KR", []),
            [],
        )

    def test_reports_resolving_outside_version_diff_are_ignored(self) -> None:
        report_path = self._write_report(self._report())
        original_resolve = Path.resolve
        escaped_path = self.tmp.parent / "escaped-key-changes.json"

        def resolve_path(path: Path, *args: object, **kwargs: object) -> Path:
            if path == report_path:
                return escaped_path
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=resolve_path):
            loaded = load_version_references(self.tmp, "v2")

        self.assertEqual(loaded.status, "invalid")
        self.assertTrue(
            any("escapes" in warning.lower() for warning in loaded.warnings),
            loaded.warnings,
        )
        self.assertEqual(
            loaded.index.references_for("New source", "ko-KR", []),
            [],
        )

if __name__ == "__main__":
    unittest.main()
