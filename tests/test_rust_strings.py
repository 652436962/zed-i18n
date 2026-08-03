import unittest

from tools.zed_i18n.rust_strings import (
    rust_format_placeholders,
    rust_format_placeholders_compatible,
    rust_string_literal,
)


class RustStringTests(unittest.TestCase):
    def test_extracts_format_placeholders_without_escaped_braces(self) -> None:
        self.assertEqual(
            rust_format_placeholders("Updated to {app_name} {} {{literal}} {err:#}"),
            ["{app_name}", "{}", "{err:#}"],
        )

    def test_placeholder_order_does_not_matter_for_named_placeholders(self) -> None:
        self.assertEqual(
            rust_format_placeholders("Failed to open {path:?}: {error}"),
            ["{path:?}", "{error}"],
        )

    def test_allows_equivalent_explicit_positional_reordering(self) -> None:
        self.assertTrue(rust_format_placeholders_compatible("{} {}", "{1} {0}"))
        self.assertTrue(rust_format_placeholders_compatible("{:?} {}", "{1} {0:?}"))

    def test_rejects_wrong_positional_arguments(self) -> None:
        self.assertFalse(rust_format_placeholders_compatible("{} {}", "{0} {0}"))
        self.assertFalse(rust_format_placeholders_compatible("{} {}", "{2} {0}"))
        self.assertFalse(rust_format_placeholders_compatible("{} {}", "{1:?} {0}"))

    def test_rejects_unescaped_braces(self) -> None:
        self.assertFalse(rust_format_placeholders_compatible("Value {}", "값 {} {"))
        self.assertFalse(rust_format_placeholders_compatible("Value {}", "값 {} }"))
        self.assertTrue(rust_format_placeholders_compatible("Value {{}} {}", "값 {{}} {}"))

    def test_keeps_literal_braces_for_non_format_source(self) -> None:
        self.assertTrue(
            rust_format_placeholders_compatible(
                'Example: {"log": {"client": "warn"}}',
                '예: {"log": {"client": "warn"}}',
            )
        )

    def test_rewrites_virtual_positions_to_real_positions(self) -> None:
        try:
            from tools.zed_i18n.rust_strings import (
                rewrite_rust_positional_placeholders,
                rust_zero_precision_placeholder,
            )
        except ImportError:
            self.fail("positional placeholder rewrite helpers are missing")

        self.assertEqual(
            rewrite_rust_positional_placeholders("{1} / {}", (0, 2)),
            "{2} / {0}",
        )
        self.assertEqual(rust_zero_precision_placeholder(1), "{1:.0}")

    def test_ignores_rust_unicode_escape_braces(self) -> None:
        self.assertEqual(rust_format_placeholders("New Thread\\u{2026}"), [])

    def test_does_not_ignore_invalid_rust_unicode_escape_braces(self) -> None:
        self.assertEqual(rust_format_placeholders("Bad escape \\u{ZZ}"), ["{ZZ}"])

    def test_allows_zero_precision_placeholder_for_known_plural_suffix(self) -> None:
        self.assertTrue(
            rust_format_placeholders_compatible(
                "Resolve Merge Conflict{} with Agent",
                "에이전트로 병합 충돌 해결{:.0}",
            )
        )
        self.assertTrue(
            rust_format_placeholders_compatible(
                "Show {} warning{}",
                "{} 件の警告を表示{:.0}",
            )
        )
        self.assertTrue(
            rust_format_placeholders_compatible(
                "{} Comment{}",
                "{} 件のコメント{:.0}",
            )
        )
        self.assertTrue(
            rust_format_placeholders_compatible(
                "{errors} error{}",
                "{errors} 件のエラー{:.0}",
            )
        )
        self.assertTrue(
            rust_format_placeholders_compatible(
                "{warnings} warning{}",
                "{warnings} 件の警告{:.0}",
            )
        )

    def test_keeps_plural_suffix_placeholder_for_known_plural_suffix(self) -> None:
        self.assertTrue(
            rust_format_placeholders_compatible(
                "{errors} error{}",
                "{errors} erreur{}",
            )
        )

    def test_rejects_zero_precision_placeholder_for_unlisted_source(self) -> None:
        self.assertFalse(
            rust_format_placeholders_compatible(
                "Move {} to {}",
                "{} に移動{:.0}",
            )
        )

    def test_rejects_zero_precision_placeholder_for_non_suffix_argument(self) -> None:
        self.assertFalse(
            rust_format_placeholders_compatible(
                "Show {} warning{}",
                "{:.0} 件の警告を表示{}",
            )
        )

    def test_rust_string_literal_preserves_rust_unicode_escapes(self) -> None:
        self.assertEqual(
            rust_string_literal("Saved to {sep}\\u{2039}name\\u{203A}"),
            '"Saved to {sep}\\u{2039}name\\u{203A}"',
        )


if __name__ == "__main__":
    unittest.main()
