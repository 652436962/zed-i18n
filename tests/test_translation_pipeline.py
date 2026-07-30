import json
import shutil
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from tools.zed_i18n.translation_pipeline import (
    PrepareTranslationOptions,
    cleanup_translation_workspace,
    merge_translation_results,
    prepare_translation_batches,
)


class TranslationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path.cwd() / "tests" / ".tmp" / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        (self.root / "manifest").mkdir()
        (self.root / "translations").mkdir()
        (self.root / "prompts" / "translation").mkdir(parents=True)
        (self.root / "reports").mkdir()
        self.zed_root = self.root / ".cache" / "zed" / "v-test-clean"
        self.zed_root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_prepare_translation_batches_include_context_and_agent_instructions(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Already translated": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 2,
                            "call": "Label::new",
                            "kind": "label",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                },
                "Rate Limit Reached": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 4,
                            "call": "render_error_callout",
                            "kind": "callout_title",
                            "start_byte": 0,
                            "end_byte": 0,
                            "translation_note": "Treat this as one complete virtual message.",
                        }
                    ],
                },
                "Internal": {
                    "status": "ignored",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 6,
                            "call": "Label::new",
                            "kind": "label",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                },
            },
        )
        self._write_json(self.root / "translations" / "ko-KR.json", {"Already translated": "번역됨"})
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            "\n".join(
                [
                    "fn render() {",
                    '    Label::new("Already translated");',
                    "    self.render_error_callout(",
                    '        "Rate Limit Reached",',
                    "        message,",
                    "    );",
                    "}",
                ]
            ),
            encoding="utf-8",
        )

        report = prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(batch_size=1, context_lines=1),
        )

        self.assertEqual(report.source_count, 1)
        self.assertEqual(report.batch_count, 1)
        plan = self._read_json(self.root / "reports" / "translation" / "ko-KR" / "plan.json")
        self.assertEqual(plan["language"], "ko-KR")
        self.assertEqual(plan["source_count"], 1)
        agent_workflow = "\n".join(plan["agent_workflow"])
        self.assertNotIn("merge-translation", agent_workflow)
        self.assertIn("partial validation", agent_workflow)
        batch_path = self.root / plan["batches"][0]["batch_file"]
        prompt_path = self.root / plan["batches"][0]["prompt_file"]
        batch = self._read_json(batch_path)
        self.assertEqual(batch["entries"][0]["source"], "Rate Limit Reached")
        self.assertEqual(batch["entries"][0]["kind"], "callout_title")
        self.assertIn("translation_note", batch["entries"][0])
        self.assertEqual(
            batch["entries"][0]["translation_note"],
            "Treat this as one complete virtual message.",
        )
        self.assertIn('        "Rate Limit Reached",', batch["entries"][0]["code_context"])
        self.assertIn("results/batch-001.json", batch["output"]["result_file"])
        self.assertEqual(batch["output"]["format"], {"source": "translation"})
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Base ko-KR prompt", prompt)
        self.assertIn("Rate Limit Reached", prompt)
        self.assertIn("Treat this as one complete virtual message.", prompt)
        self.assertIn("prompt-component context", prompt)
        self.assertIn("source_comment", prompt)
        self.assertIn("settings_enum_variant_label", prompt)

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(
                missing_only=False,
                output_dir=self.root / "reports" / "translation-all" / "ko-KR",
            ),
        )

        all_plan = self._read_json(
            self.root / "reports" / "translation-all" / "ko-KR" / "plan.json"
        )
        all_agent_workflow = "\n".join(all_plan["agent_workflow"])
        self.assertIn("merge-translation", all_agent_workflow)
        self.assertIn("--output <translation-output.json>", all_agent_workflow)

    def test_prepare_translation_batches_keeps_setting_group_atomic_and_includes_sibling_context(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Line Width": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/settings_ui/src/page_data.rs",
                            "line": 3,
                            "call": "SettingItem.title",
                            "kind": "setting_title",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                },
                "The width of the indent guides in pixels, between 1 and 10.": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/settings_ui/src/page_data.rs",
                            "line": 4,
                            "call": "SettingItem.description",
                            "kind": "setting_description",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                },
            },
        )
        self._write_json(
            self.root / "translations" / "ko-KR.json",
            {
                "The width of the indent guides in pixels, between 1 and 10.": (
                    "들여쓰기 가이드의 너비(픽셀), 1에서 10 사이입니다."
                )
            },
        )
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "settings_ui" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "settings_ui" / "src" / "page_data.rs").write_text(
            "\n".join(
                [
                    "fn page() {",
                    "    SettingsPageItem::SettingItem(SettingItem {",
                    '        title: "Line Width",',
                    '        description: "The width of the indent guides in pixels, between 1 and 10.",',
                    "        field: Box::new(SettingField {",
                    '            json_path: Some("editor.indent_guides.line_width"),',
                    "        }),",
                    "    });",
                    "}",
                ]
            ),
            encoding="utf-8",
        )

        report = prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(batch_size=1, context_lines=1),
        )

        self.assertEqual(report.source_count, 1)
        self.assertEqual(report.batch_count, 1)
        batch = self._read_json(
            self.root / "reports" / "translation" / "ko-KR" / "batches" / "batch-001.json"
        )
        self.assertEqual([entry["source"] for entry in batch["entries"]], ["Line Width"])
        context_group = batch["entries"][0]["context_group"]
        self.assertEqual(context_group["type"], "setting")
        self.assertEqual(context_group["context_key"], "editor.indent_guides.line_width")
        self.assertEqual(
            [(entry["role"], entry["source"], entry["target"]) for entry in context_group["entries"]],
            [
                ("title", "Line Width", True),
                (
                    "description",
                    "The width of the indent guides in pixels, between 1 and 10.",
                    False,
                ),
            ],
        )
        self.assertEqual(
            context_group["entries"][1]["current_translation"],
            "들여쓰기 가이드의 너비(픽셀), 1에서 10 사이입니다.",
        )

    def test_prepare_translation_options_rejects_invalid_batch_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "batch size must be positive"):
            PrepareTranslationOptions(batch_size=0)

    def test_prepare_translation_options_rejects_negative_context_lines(self) -> None:
        with self.assertRaisesRegex(ValueError, "context lines must be zero or positive"):
            PrepareTranslationOptions(context_lines=-1)

    def test_prepare_translation_batches_fall_back_to_template_prompt(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Open Settings": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "MenuItem::action",
                            "kind": "menu_item",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                }
            },
        )
        (self.root / "prompts" / "translation" / "TEMPLATE.md").write_text(
            "Generic translation prompt for {language}",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'MenuItem::action("Open Settings", OpenSettings);',
            encoding="utf-8",
        )

        prepare_translation_batches(
            root=self.root,
            language="ja-JP",
            zed_root=self.zed_root,
        )

        prompt = (
            self.root
            / "reports"
            / "translation"
            / "ja-JP"
            / "prompts"
            / "batch-001.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Generic translation prompt for ja-JP", prompt)

    def test_prepare_translation_batches_includes_vscode_references_when_available(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Clone": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "MenuItem::action",
                            "kind": "menu_item",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                }
            },
        )
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'MenuItem::action("Clone", Clone);',
            encoding="utf-8",
        )
        vscode_loc_root = self.root / ".cache" / "vscode-loc"
        vscode_pack_root = vscode_loc_root / "i18n" / "vscode-language-pack-ko"
        (vscode_pack_root / "translations" / "extensions").mkdir(parents=True)
        vscode_source_root = self.root / ".cache" / "vscode-upstream"
        (vscode_source_root / "extensions" / "git").mkdir(parents=True)
        self._write_json(
            vscode_pack_root / "package.json",
            {
                "contributes": {
                    "localizations": [
                        {
                            "languageId": "ko",
                            "translations": [
                                {
                                    "id": "vscode.git",
                                    "path": "./translations/extensions/vscode.git.i18n.json",
                                }
                            ],
                        }
                    ]
                }
            },
        )
        self._write_json(
            vscode_pack_root / "translations" / "extensions" / "vscode.git.i18n.json",
            {"contents": {"package": {"command.clone": "클론"}}},
        )
        self._write_json(
            vscode_source_root / "extensions" / "git" / "package.json",
            {"publisher": "vscode", "name": "git"},
        )
        self._write_json(
            vscode_source_root / "extensions" / "git" / "package.nls.json",
            {"command.clone": "Clone"},
        )

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(
                vscode_loc_root=vscode_loc_root,
                vscode_source_root=vscode_source_root,
            ),
        )

        batch = self._read_json(
            self.root / "reports" / "translation" / "ko-KR" / "batches" / "batch-001.json"
        )
        references = batch["entries"][0]["vscode_references"]
        self.assertEqual(references[0]["source"], "Clone")
        self.assertEqual(references[0]["translation"], "클론")

    def test_prepare_translation_batches_skips_vscode_references_for_code_like_sources(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                '"auto"': {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "SharedString::from",
                            "kind": "shared_string",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                },
                " (case-sensitive)": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 2,
                            "call": "SharedString::from",
                            "kind": "shared_string",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                },
            },
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'SharedString::from("\\"auto\\"");\nSharedString::from(" (case-sensitive)");\n',
            encoding="utf-8",
        )
        vscode_loc_root = self.root / ".cache" / "vscode-loc"
        vscode_pack_root = vscode_loc_root / "i18n" / "vscode-language-pack-ko"
        (vscode_pack_root / "translations").mkdir(parents=True)
        self._write_json(
            vscode_pack_root / "package.json",
            {
                "contributes": {
                    "localizations": [
                        {
                            "languageId": "ko",
                            "translations": [
                                {
                                    "id": "vscode",
                                    "path": "./translations/main.i18n.json",
                                }
                            ],
                        }
                    ]
                }
            },
        )
        self._write_json(
            vscode_pack_root / "translations" / "main.i18n.json",
            {
                "contents": {
                    "package": {
                        "Auto": "자동",
                        "Toggle Case Sensitive": "대/소문자 구분 전환",
                    }
                }
            },
        )

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(vscode_loc_root=vscode_loc_root),
        )

        batch = self._read_json(
            self.root / "reports" / "translation" / "ko-KR" / "batches" / "batch-001.json"
        )
        self.assertNotIn("vscode_references", batch["entries"][0])
        self.assertNotIn("vscode_references", batch["entries"][1])

    def test_prepare_translation_batches_includes_language_glossary_file(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Open Settings": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "MenuItem::action",
                            "kind": "menu_item",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                }
            },
        )
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt",
            encoding="utf-8",
        )
        (self.root / "prompts" / "translation" / "glossary").mkdir(parents=True)
        (self.root / "prompts" / "translation" / "glossary" / "ko-KR.md").write_text(
            "# Glossary for ko-KR (curated)\n\n| English | Context | Translation |\n|---------|---------|-------------|\n| Settings | | 설정 |\n",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'MenuItem::action("Open Settings", OpenSettings);',
            encoding="utf-8",
        )

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
        )

        prompt = (
            self.root
            / "reports"
            / "translation"
            / "ko-KR"
            / "prompts"
            / "batch-001.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Base ko-KR prompt", prompt)
        self.assertIn("# Glossary for ko-KR (curated)", prompt)
        self.assertIn("| English | Context | Translation |", prompt)
        self.assertIn("| Settings | | 설정 |", prompt)

    def test_prepare_translation_batches_does_not_append_glossary_when_prompt_has_internal_glossary(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Open Settings": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "MenuItem::action",
                            "kind": "menu_item",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                }
            },
        )
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt\n\n## GLOSSARY — use these exact translations\n\n| English | Korean |\n|---------|--------|\n| Workspace | 작업 영역 |\n",
            encoding="utf-8",
        )
        (self.root / "prompts" / "translation" / "glossary").mkdir(parents=True)
        (self.root / "prompts" / "translation" / "glossary" / "ko.md").write_text(
            "# Glossary for ko (curated)\n\n| English | Context | Translation |\n|---------|---------|-------------|\n| Workspace | | 워크스페이스 |\n",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'MenuItem::action("Open Settings", OpenSettings);',
            encoding="utf-8",
        )

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
        )

        prompt = (
            self.root
            / "reports"
            / "translation"
            / "ko-KR"
            / "prompts"
            / "batch-001.md"
        ).read_text(encoding="utf-8")
        self.assertIn("| Workspace | 작업 영역 |", prompt)
        self.assertNotIn("# Glossary for ko", prompt)
        self.assertNotIn("| Workspace | | 워크스페이스 |", prompt)

    def test_prepare_translation_batches_uses_case_insensitive_glossary_locale(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Open Settings": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "MenuItem::action",
                            "kind": "menu_item",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                }
            },
        )
        (self.root / "prompts" / "translation" / "TEMPLATE.md").write_text(
            "Base {language} prompt",
            encoding="utf-8",
        )
        (self.root / "prompts" / "translation" / "glossary").mkdir(parents=True)
        (self.root / "prompts" / "translation" / "glossary" / "pt-br.md").write_text(
            "# Glossary for pt-br (curated)\n\n| English | Context | Translation |\n|---------|---------|-------------|\n| Settings | | Configurações |\n",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'MenuItem::action("Open Settings", OpenSettings);',
            encoding="utf-8",
        )

        prepare_translation_batches(
            root=self.root,
            language="pt-BR",
            zed_root=self.zed_root,
        )

        prompt = (
            self.root
            / "reports"
            / "translation"
            / "pt-BR"
            / "prompts"
            / "batch-001.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Base pt-BR prompt", prompt)
        self.assertIn("# Glossary for pt-br (curated)", prompt)
        self.assertIn("| Settings | | Configurações |", prompt)

    def test_prepare_translation_batches_removes_stale_agent_workspace(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Open Settings": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "MenuItem::action",
                            "kind": "menu_item",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                }
            },
        )
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'MenuItem::action("Open Settings", OpenSettings);',
            encoding="utf-8",
        )
        stale_result = (
            self.root
            / "reports"
            / "translation"
            / "ko-KR"
            / "results"
            / "stale.json"
        )
        stale_result.parent.mkdir(parents=True)
        stale_result.write_text('{"Old": "오래됨"}', encoding="utf-8")

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
        )

        self.assertFalse(stale_result.exists())
        self.assertTrue(
            (
                self.root
                / "reports"
                / "translation"
                / "ko-KR"
                / "batches"
                / "batch-001.json"
            ).exists()
        )
        self.assertEqual(
            list((self.root / "reports" / "translation" / "ko-KR" / "results").iterdir()),
            [],
        )

    def test_cleanup_translation_workspace_removes_only_one_language_directory(self) -> None:
        ko_result = self.root / "reports" / "translation" / "ko-KR" / "results" / "batch.json"
        ja_result = self.root / "reports" / "translation" / "ja-JP" / "results" / "batch.json"
        ko_result.parent.mkdir(parents=True)
        ja_result.parent.mkdir(parents=True)
        ko_result.write_text('{"Open": "열기"}', encoding="utf-8")
        ja_result.write_text('{"Open": "開く"}', encoding="utf-8")

        report = cleanup_translation_workspace(self.root, "ko-KR")

        self.assertTrue(report.removed)
        self.assertFalse((self.root / "reports" / "translation" / "ko-KR").exists())
        self.assertTrue(ja_result.exists())

    def test_prepare_translation_batches_allows_new_custom_output_dir(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Open Settings": {
                    "status": "accepted",
                    "occurrences": [
                        {
                            "file": "crates/app/src/lib.rs",
                            "line": 1,
                            "call": "MenuItem::action",
                            "kind": "menu_item",
                            "start_byte": 0,
                            "end_byte": 0,
                        }
                    ],
                }
            },
        )
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt",
            encoding="utf-8",
        )
        (self.zed_root / "crates" / "app" / "src").mkdir(parents=True)
        (self.zed_root / "crates" / "app" / "src" / "lib.rs").write_text(
            'MenuItem::action("Open Settings", OpenSettings);',
            encoding="utf-8",
        )

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(output_dir=self.root / "custom-agent-batches"),
        )

        self.assertTrue((self.root / "custom-agent-batches" / "plan.json").exists())

    def test_prepare_translation_batches_injects_compact_version_references_for_every_model_run(self) -> None:
        self._write_version_reference_translation_fixture()
        report_path = self._write_version_reference_report()
        original_report = report_path.read_bytes()
        first_model_references: list[dict[str, object]] | None = None

        for model in ("model-a", "model-b"):
            output_dir = (
                self.root
                / "reports"
                / "translation-runs"
                / "ko-KR"
                / model
            )
            prepare_translation_batches(
                root=self.root,
                language="ko-KR",
                zed_root=self.zed_root,
                options=PrepareTranslationOptions(
                    current_version="v2",
                    output_dir=output_dir,
                ),
            )

            batch = self._read_json(output_dir / "batches" / "batch-001.json")
            entry = next(
                item for item in batch["entries"] if item["source"] == "New source"
            )
            references = entry["previous_version_references"]
            if model == "model-b":
                self.assertEqual(references, first_model_references)
                self.assertEqual(report_path.read_bytes(), original_report)
                continue

            first_model_references = references
            reference = references[0]
            self.assertEqual(reference["historical_translation"], "과거 번역")
            self.assertNotIn("translations", reference)
            self.assertNotIn("old_occurrences", reference)
            self.assertNotIn(
                "previous_version_references",
                next(
                    item
                    for item in batch["entries"]
                    if item["source"] == "No history"
                ),
            )

            prompt = (output_dir / "prompts" / "batch-001.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("previous_version_references", prompt)
            self.assertIn("optional historical translation hints", prompt)
            self.assertIn("current placeholders and protected tokens win", prompt)
            self.assertNotIn("以前の翻訳", prompt)

            plan = self._read_json(output_dir / "plan.json")
            self.assertEqual(
                plan["version_diff"],
                {
                    "status": "loaded",
                    "path": "reports/version-diff/v1-to-v2/key-changes.json",
                    "reference_source_count": 1,
                    "warnings": [],
                },
            )
            serialized_plan = json.dumps(plan, ensure_ascii=False)
            self.assertNotIn("Old source", serialized_plan)
            self.assertNotIn("과거 번역", serialized_plan)

    def test_prepare_translation_batches_continues_when_version_diff_report_is_malformed(self) -> None:
        self._write_version_reference_translation_fixture()
        report_path = (
            self.root
            / "reports"
            / "version-diff"
            / "v1-to-v2"
            / "key-changes.json"
        )
        report_path.parent.mkdir(parents=True)
        report_path.write_text("{", encoding="utf-8")
        output_dir = self.root / "custom" / "malformed-version-diff"

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(
                current_version="v2",
                output_dir=output_dir,
            ),
        )

        batch = self._read_json(output_dir / "batches" / "batch-001.json")
        self.assertTrue(
            all(
                "previous_version_references" not in entry
                for entry in batch["entries"]
            )
        )
        metadata = self._read_json(output_dir / "plan.json")["version_diff"]
        self.assertEqual(metadata["status"], "invalid")
        self.assertEqual(metadata["reference_source_count"], 0)
        self.assertTrue(
            any("malformed" in warning.lower() for warning in metadata["warnings"]),
            metadata["warnings"],
        )

    def test_prepare_translation_batches_disables_version_references_without_current_version(self) -> None:
        self._write_version_reference_translation_fixture()
        self._write_version_reference_report()
        output_dir = self.root / "custom" / "disabled"

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(output_dir=output_dir),
        )

        plan = self._read_json(output_dir / "plan.json")
        self.assertEqual(
            plan["version_diff"],
            {
                "status": "disabled",
                "path": None,
                "reference_source_count": 0,
                "warnings": [],
            },
        )
        batch = self._read_json(output_dir / "batches" / "batch-001.json")
        self.assertTrue(
            all(
                "previous_version_references" not in entry
                for entry in batch["entries"]
            )
        )

    def test_prepare_translation_batches_records_projection_failures_as_warnings(self) -> None:
        self._write_version_reference_translation_fixture()
        self._write_version_reference_report()
        output_dir = self.root / "custom" / "projection-failure"

        with patch(
            "tools.zed_i18n.version_references._best_old_occurrence",
            side_effect=RuntimeError("projection exploded"),
        ):
            prepare_translation_batches(
                root=self.root,
                language="ko-KR",
                zed_root=self.zed_root,
                options=PrepareTranslationOptions(
                    current_version="v2",
                    output_dir=output_dir,
                ),
            )

        batch = self._read_json(output_dir / "batches" / "batch-001.json")
        self.assertTrue(
            all(
                "previous_version_references" not in entry
                for entry in batch["entries"]
            )
        )
        metadata = self._read_json(output_dir / "plan.json")["version_diff"]
        self.assertEqual(metadata["status"], "loaded")
        self.assertEqual(metadata["reference_source_count"], 0)
        self.assertTrue(
            any("projection" in warning.lower() for warning in metadata["warnings"]),
            metadata["warnings"],
        )

    def test_prepare_translation_batches_rejects_output_paths_overlapping_reserved_version_diff_paths(self) -> None:
        self._write_version_reference_translation_fixture()

        cases = (
            ("loaded-equal", "v2"),
            ("loaded-ancestor", "v2"),
            ("loaded-descendant", "v2"),
            ("disabled-equal", None),
        )
        for relationship, current_version in cases:
            with self.subTest(relationship=relationship):
                shutil.rmtree(
                    self.root / "reports" / "version-diff",
                    ignore_errors=True,
                )
                report_path = self._write_version_reference_report()
                original_report = report_path.read_bytes()
                if relationship in ("loaded-equal", "disabled-equal"):
                    output_dir = self.root / "reports" / "version-diff"
                elif relationship == "loaded-ancestor":
                    output_dir = self.root / "reports"
                else:
                    output_dir = report_path.parent
                (output_dir / "plan.json").parent.mkdir(parents=True, exist_ok=True)
                (output_dir / "plan.json").write_text("{}\n", encoding="utf-8")
                seeded_plan = (output_dir / "plan.json").read_bytes()
                reserved_before = sorted(
                    path.as_posix()
                    for path in (self.root / "reports" / "version-diff").rglob("*")
                )

                with self.assertRaisesRegex(ValueError, "version-diff report"):
                    prepare_translation_batches(
                        root=self.root,
                        language="ko-KR",
                        zed_root=self.zed_root,
                        options=PrepareTranslationOptions(
                            current_version=current_version,
                            output_dir=output_dir,
                        ),
                    )

                self.assertTrue(report_path.exists())
                self.assertEqual(report_path.read_bytes(), original_report)
                self.assertEqual(
                    sorted(
                        path.as_posix()
                        for path in (
                            self.root / "reports" / "version-diff"
                        ).rglob("*")
                    ),
                    reserved_before,
                )
                self.assertEqual((output_dir / "plan.json").read_bytes(), seeded_plan)
                for subdirectory in ("batches", "prompts", "results"):
                    self.assertFalse((output_dir / subdirectory).exists())

    def test_prepare_translation_batches_sanitizes_invalid_schema_warnings(self) -> None:
        self._write_version_reference_translation_fixture()
        report_path = self._write_version_reference_report()
        report = self._read_json(report_path)
        old_entry = report["deleted"].pop("Old source")
        old_entry["translations"]["ko-KR"] = ""
        report["deleted"]["Sensitive old source"] = old_entry
        new_entry = report["added"].pop("New source")
        new_entry["candidates"][0]["old_source"] = "Sensitive old source"
        new_entry["candidates"][0]["score"] = 2.0
        report["added"]["Sensitive new source"] = new_entry
        self._write_json(report_path, report)
        output_dir = self.root / "custom" / "sanitized-schema-warning"

        prepare_translation_batches(
            root=self.root,
            language="ko-KR",
            zed_root=self.zed_root,
            options=PrepareTranslationOptions(
                current_version="v2",
                output_dir=output_dir,
            ),
        )

        plan = self._read_json(output_dir / "plan.json")
        metadata = plan["version_diff"]
        self.assertEqual(metadata["status"], "invalid")
        self.assertTrue(
            any("invalid schema" in warning.lower() for warning in metadata["warnings"]),
            metadata["warnings"],
        )
        serialized_plan = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("Sensitive old source", serialized_plan)
        self.assertNotIn("Sensitive new source", serialized_plan)

    def test_prepare_translation_batches_rejects_outputs_resolving_to_version_diff_aliases(self) -> None:
        self._write_version_reference_translation_fixture()
        version_diff_dir = self.root / "reports" / "version-diff"
        original_resolve = Path.resolve

        for alias_kind in ("child", "root"):
            with self.subTest(alias_kind=alias_kind):
                shutil.rmtree(version_diff_dir, ignore_errors=True)
                report_path = self._write_version_reference_report()
                original_report = report_path.read_bytes()
                version_directory = report_path.parent
                output_dir = self.root / f"{alias_kind}-alias-target"
                output_dir.mkdir()
                (output_dir / "plan.json").write_text("{}\n", encoding="utf-8")

                def resolve_path(
                    path: Path,
                    *args: object,
                    **kwargs: object,
                ) -> Path:
                    if alias_kind == "child":
                        if path == version_directory:
                            return output_dir
                        if path == report_path:
                            return output_dir / "key-changes.json"
                    elif path == version_diff_dir:
                        return output_dir
                    return original_resolve(path, *args, **kwargs)

                reserved_before = sorted(
                    path.as_posix() for path in version_diff_dir.rglob("*")
                )
                with patch.object(
                    Path,
                    "resolve",
                    autospec=True,
                    side_effect=resolve_path,
                ):
                    with self.assertRaisesRegex(ValueError, "version-diff"):
                        prepare_translation_batches(
                            root=self.root,
                            language="ko-KR",
                            zed_root=self.zed_root,
                            options=PrepareTranslationOptions(output_dir=output_dir),
                        )

                self.assertTrue(report_path.exists())
                self.assertEqual(report_path.read_bytes(), original_report)
                self.assertEqual(
                    sorted(path.as_posix() for path in version_diff_dir.rglob("*")),
                    reserved_before,
                )
                for subdirectory in ("batches", "prompts", "results"):
                    self.assertFalse((output_dir / subdirectory).exists())

    def test_prepare_translation_batches_continues_when_version_diff_path_inspection_fails(self) -> None:
        self._write_version_reference_translation_fixture()
        report_path = self._write_version_reference_report()
        original_report = report_path.read_bytes()
        version_diff_dir = self.root / "reports" / "version-diff"
        original_resolve = Path.resolve

        def resolve_path(path: Path, *args: object, **kwargs: object) -> Path:
            if path == version_diff_dir:
                raise RuntimeError("symlink loop")
            return original_resolve(path, *args, **kwargs)

        cases = (
            (
                "enumeration-denied",
                patch.object(
                    Path,
                    "iterdir",
                    side_effect=PermissionError("enumeration denied"),
                ),
                PrepareTranslationOptions(
                    current_version="v2",
                    output_dir=self.root / "custom" / "enumeration-denied",
                ),
                "invalid",
                "discovery",
            ),
            (
                "root-resolve-loop",
                patch.object(
                    Path,
                    "resolve",
                    autospec=True,
                    side_effect=resolve_path,
                ),
                PrepareTranslationOptions(
                    output_dir=self.root / "custom" / "root-resolve-loop",
                ),
                "disabled",
                None,
            ),
        )

        for error_kind, path_patch, options, expected_status, warning_text in cases:
            with self.subTest(error_kind=error_kind), path_patch:
                prepare_translation_batches(
                    root=self.root,
                    language="ko-KR",
                    zed_root=self.zed_root,
                    options=options,
                )

            output_dir = Path(options.output_dir)
            metadata = self._read_json(output_dir / "plan.json")["version_diff"]
            self.assertEqual(metadata["status"], expected_status)
            self.assertEqual(metadata["reference_source_count"], 0)
            if warning_text is not None:
                self.assertTrue(
                    any(
                        warning_text in warning.lower()
                        for warning in metadata["warnings"]
                    ),
                    metadata["warnings"],
                )
            batch = self._read_json(output_dir / "batches" / "batch-001.json")
            self.assertTrue(
                all(
                    "previous_version_references" not in entry
                    for entry in batch["entries"]
                )
            )
            self.assertTrue(report_path.exists())
            self.assertEqual(report_path.read_bytes(), original_report)

    def test_prepare_translation_batches_skips_broken_alias_and_protects_other_targets(self) -> None:
        self._write_version_reference_translation_fixture()
        protected_report = self._write_version_reference_report()
        broken_report = self._write_version_reference_report(
            directory="v0-to-v2",
            from_version="v0",
        )
        original_reports = {
            protected_report: protected_report.read_bytes(),
            broken_report: broken_report.read_bytes(),
        }
        version_diff_dir = self.root / "reports" / "version-diff"
        output_dir = self.root / "custom-target"
        output_dir.mkdir()
        (output_dir / "plan.json").write_text("{}\n", encoding="utf-8")
        original_iterdir = Path.iterdir
        original_resolve = Path.resolve

        def iter_directory(path: Path) -> Iterator[Path]:
            if path == version_diff_dir:
                return iter([broken_report.parent, protected_report.parent])
            return original_iterdir(path)

        def resolve_path(path: Path, *args: object, **kwargs: object) -> Path:
            if path == broken_report.parent:
                raise RuntimeError("symlink loop")
            if path == protected_report.parent:
                return output_dir
            if path == protected_report:
                return output_dir / "key-changes.json"
            return original_resolve(path, *args, **kwargs)

        with patch.object(
            Path,
            "iterdir",
            autospec=True,
            side_effect=iter_directory,
        ):
            with patch.object(Path, "resolve", autospec=True, side_effect=resolve_path):
                with self.assertRaisesRegex(ValueError, "version-diff"):
                    prepare_translation_batches(
                        root=self.root,
                        language="ko-KR",
                        zed_root=self.zed_root,
                        options=PrepareTranslationOptions(output_dir=output_dir),
                    )

        for report_path, original_bytes in original_reports.items():
            self.assertTrue(report_path.exists())
            self.assertEqual(report_path.read_bytes(), original_bytes)

    def test_merge_translation_results_skips_invalid_agent_outputs(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Failed to open {path}": {"status": "accepted", "occurrences": []},
                "Keep Original": {"status": "accepted", "occurrences": []},
                "Bad Placeholder {name}": {"status": "accepted", "occurrences": []},
                "Open `settings.json`": {"status": "accepted", "occurrences": []},
            },
        )
        self._write_json(
            self.root / "translations" / "ko-KR.json",
            {
                "Keep Original": "기존 번역",
                "Open `settings.json`": "`settings.json` 열기",
            },
        )
        results_dir = self.root / "reports" / "translation" / "ko-KR" / "results"
        results_dir.mkdir(parents=True)
        self._write_json(
            results_dir / "batch-001.json",
            {
                "Failed to open {path}": "{path}을(를) 열지 못했습니다",
                "Keep Original": None,
                "Bad Placeholder {name}": "플레이스홀더가 빠졌습니다",
                "Open `settings.json`": "설정 파일 열기",
                "Unknown": "알 수 없음",
            },
        )

        report = merge_translation_results(
            root=self.root,
            language="ko-KR",
            results_dir=results_dir,
        )

        translations = self._read_json(self.root / "translations" / "ko-KR.json")
        self.assertEqual(translations["Failed to open {path}"], "{path}을(를) 열지 못했습니다")
        self.assertEqual(translations["Keep Original"], "기존 번역")
        self.assertEqual(translations["Open `settings.json`"], "`settings.json` 열기")
        self.assertNotIn("Bad Placeholder {name}", translations)
        self.assertNotIn("Unknown", translations)
        self.assertEqual(report.merged, ["Failed to open {path}"])
        self.assertEqual(report.null_values, ["Keep Original"])
        self.assertEqual(report.placeholder_mismatches, ["Bad Placeholder {name}"])
        self.assertEqual(report.protected_token_mismatches, ["Open `settings.json`"])
        self.assertEqual(report.unknown_sources, ["Unknown"])

    def test_merge_translation_results_can_write_to_comparison_output(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "Open Settings": {"status": "accepted", "occurrences": []},
                "Keep Original": {"status": "accepted", "occurrences": []},
            },
        )
        translations_path = self.root / "translations" / "ko-KR.json"
        self._write_json(
            translations_path,
            {
                "Keep Original": "기존 번역",
            },
        )
        results_dir = self.root / "reports" / "translation-runs" / "ko-KR" / "model-a" / "results"
        results_dir.mkdir(parents=True)
        self._write_json(
            results_dir / "batch-001.json",
            {
                "Open Settings": "설정 열기",
            },
        )
        comparison_output = self.root / "translations" / "ko-KR.model-a.json"

        merge_translation_results(
            root=self.root,
            language="ko-KR",
            results_dir=results_dir,
            output_path=comparison_output,
        )

        self.assertEqual(self._read_json(translations_path), {"Keep Original": "기존 번역"})
        self.assertEqual(
            self._read_json(comparison_output),
            {
                "Keep Original": "기존 번역",
                "Open Settings": "설정 열기",
            },
        )

    def test_merge_translation_results_rejects_missing_results_dir(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {"Open Settings": {"status": "accepted", "occurrences": []}},
        )

        with self.assertRaisesRegex(ValueError, "results directory does not exist"):
            merge_translation_results(
                root=self.root,
                language="ko-KR",
                results_dir=self.root / "reports" / "missing" / "results",
            )

    def test_merge_translation_results_rejects_empty_results_dir(self) -> None:
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {"Open Settings": {"status": "accepted", "occurrences": []}},
        )
        results_dir = self.root / "reports" / "translation" / "ko-KR" / "results"
        results_dir.mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "no translation result JSON files"):
            merge_translation_results(
                root=self.root,
                language="ko-KR",
                results_dir=results_dir,
            )

    def _write_version_reference_translation_fixture(self) -> None:
        occurrences = [
            {
                "file": "crates/app/src/lib.rs",
                "line": 1,
                "call": "Label::new",
                "kind": "label",
                "start_byte": 0,
                "end_byte": 10,
            }
        ]
        self._write_json(
            self.root / "manifest" / "ui-strings.json",
            {
                "New source": {"status": "accepted", "occurrences": occurrences},
                "No history": {"status": "accepted", "occurrences": []},
            },
        )
        (self.root / "prompts" / "translation" / "ko-KR.md").write_text(
            "Base ko-KR prompt",
            encoding="utf-8",
        )
        source_path = self.zed_root / "crates" / "app" / "src" / "lib.rs"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text('Label::new("New source");', encoding="utf-8")

    def _write_version_reference_report(
        self,
        *,
        directory: str = "v1-to-v2",
        from_version: str = "v1",
    ) -> Path:
        path = (
            self.root
            / "reports"
            / "version-diff"
            / directory
            / "key-changes.json"
        )
        self._write_json(
            path,
            {
                "schema_version": 1,
                "from_version": from_version,
                "to_version": "v2",
                "base_commit": "a" * 40,
                "summary": {"added": 1, "deleted": 1, "candidate_pairs": 1},
                "deleted": {
                    "Old source": {
                        "status": "accepted",
                        "occurrences": [
                            {
                                "file": "crates/app/src/lib.rs",
                                "line": 8,
                                "call": "Label::new",
                                "kind": "label",
                                "start_byte": 2,
                                "end_byte": 12,
                            }
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
                                "file": "crates/app/src/lib.rs",
                                "line": 1,
                                "call": "Label::new",
                                "kind": "label",
                                "start_byte": 0,
                                "end_byte": 10,
                            }
                        ],
                        "candidates": [
                            {
                                "old_source": "Old source",
                                "score": 0.91,
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
            },
        )
        return path

    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
