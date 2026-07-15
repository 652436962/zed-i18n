# Automated Version-Diff Translation References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate deterministic previous-version translation references from Git and have `prepare-translation` inject them automatically so the main orchestrator only runs one generator command.

**Architecture:** A focused `version_diff.py` module owns snapshot validation, deterministic candidate scoring, report schema validation, read-only Git loading, and atomic report generation. A small `version_references.py` consumer discovers one current report and projects locale-specific hints into `translation_pipeline.py`, following the existing `vscode_references` pattern. Translation and validation agents make semantic decisions from generated batch context; orchestration Markdown contains no matching or report-routing logic.

**Tech Stack:** Python 3.12 standard library (`argparse`, `dataclasses`, `difflib`, `json`, `pathlib`, `subprocess`, `tempfile`, `tomllib`), `unittest`, Markdown workflow documentation.

**Repository constraint:** Git is read-only. Do not create a worktree or branch and do not stage or commit. The user's explicit approval authorizes edits and tests in the current workspace only. Where the generic skills request commits, replace that step with read-only diff and test verification.

---

### Task 1: Build the pure version-diff and scoring engine

**Files:**
- Create: `tools/zed_i18n/version_diff.py`
- Create: `tests/test_version_diff.py`

- [x] **Step 1: Write failing snapshot and scoring tests**

Define the desired pure API in `tests/test_version_diff.py` before the module exists:

```python
from tools.zed_i18n.version_diff import VersionSnapshot, build_version_diff


def snapshot(version, sources, manifest, translations=None):
    return VersionSnapshot(
        version=version,
        catalog={source: source for source in sources},
        manifest=manifest,
        translations=translations or {},
    )


class VersionDiffTests(unittest.TestCase):
    def test_build_version_diff_reports_exact_sets_and_historical_translations(self):
        old = snapshot(
            "v1",
            {"Old label", "Unchanged"},
            {
                "Old label": {"status": "accepted", "occurrences": []},
                "Unchanged": {"status": "accepted", "occurrences": []},
            },
            {"ko-KR": {"Old label": "이전 레이블"}},
        )
        new = snapshot(
            "v2",
            {"New label", "Unchanged"},
            {
                "New label": {"status": "accepted", "occurrences": []},
                "Unchanged": {"status": "accepted", "occurrences": []},
            },
        )

        report = build_version_diff(old, new, base_commit="a" * 40)

        self.assertEqual(set(report["deleted"]), {"Old label"})
        self.assertEqual(set(report["added"]), {"New label"})
        self.assertEqual(
            report["deleted"]["Old label"]["translations"],
            {"ko-KR": "이전 레이블"},
        )

```

Add three more complete methods. The all-occurrence test uses four translated deleted sources, puts the only matching `file`/`kind`/`call` in the second occurrence, and asserts the exact three-item `old_source` order. The placeholder test links `{field_name} must be a number` to `{title} must be a number`, asserts `match_kind == "placeholder_shape"`, and recursively asserts that no `relation` key exists. The mismatch test gives `VersionSnapshot` a catalog key absent from its manifest and asserts `ValueError` with `catalog and manifest source keys differ`.

- [x] **Step 2: Run Task 1 tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_version_diff
```

Expected: import failure because `tools.zed_i18n.version_diff` does not exist.

- [x] **Step 3: Implement the minimal pure engine**

Create `VersionSnapshot` exactly as follows:

```python
@dataclass(frozen=True)
class VersionSnapshot:
    version: str
    catalog: dict[str, str]
    manifest: dict[str, dict[str, Any]]
    translations: dict[str, dict[str, str]]
```

Add `build_version_diff(old: VersionSnapshot, new: VersionSnapshot, *, base_commit: str, min_score: float = 0.70, max_candidates: int = 3) -> dict[str, Any]` and `validate_version_diff_report(report: object) -> list[str]` with the schema and validation behavior below.

Implement exactly the approved algorithm:

```python
raw = " ".join(re.findall(r"[a-z0-9]+", source.casefold()))
text_score = max(raw_similarity, placeholder_normalized_similarity)
score = min(
    1.0,
    text_score
    + (0.10 if same_file else 0.0)
    + (0.06 if same_kind else 0.0)
    + (0.04 if same_call else 0.0),
)
```

Use `SequenceMatcher(None, old, new, autojunk=False).ratio()`. Scan placeholders with the same escaped-brace and Rust-unicode behavior as `rust_format_placeholders`; factor a small private replacement helper instead of using a brace regex. Compare every old/new occurrence pair. Always include every exact added/deleted source, but score only deleted sources with at least one non-empty historical translation. Omit scores below `0.70`, keep three, and sort by `(-score, old_source)`.

`validate_version_diff_report()` must return named errors rather than raise. Validate schema version, metadata types, 40-character commit, exact summary counts, occurrence field types, locale translations, candidate referential integrity, unique top-three candidates, finite scores, allowed match kinds/signals, and deterministic ordering.

- [x] **Step 4: Run Task 1 tests and verify GREEN**

Run the focused module tests. Expected: all Task 1 tests pass.

- [x] **Step 5: Refactor without changing behavior**

Keep normalization, occurrence signaling, scoring, serialization shape, and schema validation as separate private helpers. Re-run `tests.test_version_diff`; expected: PASS.

---

### Task 2: Add the read-only Git generator and CLI command

**Files:**
- Modify: `tools/zed_i18n/version_diff.py`
- Modify: `tools/zed_i18n/cli.py`
- Modify: `tests/test_version_diff.py`
- Modify: `tests/test_cli.py`

- [x] **Step 1: Write failing Git, atomic-write, and CLI tests**

Add a `GenerateVersionDiffTests` class with four complete tests. The Git test patches `subprocess.run` with ordered responses for a 40-character commit, baseline TOML, catalog, manifest, tree paths, and locale JSON; it asserts every `git show` contains the resolved commit and the invoked Git subcommands are limited to `rev-parse`, `show`, and `ls-tree`. The locale test feeds `translations/ko-KR.json`, `translations/ko-KR.gpt-5.5.json`, and `translations/nested/ja-JP.json` and asserts only `ko-KR` loads. The atomic test starts with an existing report, patches `os.replace` to raise `OSError`, and asserts the original bytes remain and no temporary file survives. The equal-version test asserts `ValueError` and that the output directory is absent.

Extend `tests/test_cli.py`:

```python
def test_parses_generate_version_diff_base_ref(self):
    args = build_parser().parse_args(
        ["generate-version-diff", "--base-ref", "v1.10.3-i18n.1"]
    )
    self.assertEqual(args.command, "generate-version-diff")
    self.assertEqual(args.base_ref, "v1.10.3-i18n.1")
```

- [x] **Step 2: Run the new tests and verify RED**

Expected failures: missing generator functions and unrecognized CLI command.

- [x] **Step 3: Implement Git snapshot loading and atomic generation**

Add this public result:

```python
@dataclass(frozen=True)
class GeneratedVersionDiff:
    output_path: Path
    report: dict[str, Any]
```

Add `generate_version_diff(root: Path, base_ref: str = "HEAD") -> GeneratedVersionDiff` with the exact read/build/validate/write sequence below.

Use only these Git operations:

```text
git rev-parse --verify <base-ref>^{commit}
git show <resolved-commit>:config/project.toml
git show <resolved-commit>:catalog/en-US.json
git show <resolved-commit>:manifest/ui-strings.json
git ls-tree -r --name-only <resolved-commit> -- translations
git show <resolved-commit>:translations/<locale>.json
```

Read the current snapshot from the working tree. Accept only direct `translations/<locale>.json` paths whose stem contains no period. Build and validate the complete report before writing. Write through a temporary file in the destination directory, close it, then `os.replace()` it over `reports/version-diff/<from>-to-<to>/key-changes.json`; clean the temporary file on every failure.

- [x] **Step 4: Wire `generate-version-diff` into `cli.py`**

Add parser and dispatch code:

```python
version_diff_parser = subparsers.add_parser("generate-version-diff")
version_diff_parser.add_argument("--base-ref", default="HEAD")

if args.command == "generate-version-diff":
    return run_generate_version_diff(root, args.base_ref)
```

`run_generate_version_diff()` calls the module function, prints added/deleted/candidate counts and the root-relative output path, and returns `0`. Do not expose scoring flags.

- [x] **Step 5: Run Task 1–2 tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_version_diff tests.test_cli
```

Expected: PASS with no real Git writes or report writes outside test roots.

---

### Task 3: Auto-load references in translation context assembly

**Files:**
- Create: `tools/zed_i18n/version_references.py`
- Modify: `tools/zed_i18n/translation_pipeline.py`
- Modify: `tools/zed_i18n/cli.py`
- Modify: `tests/test_version_diff.py`
- Modify: `tests/test_translation_pipeline.py`

- [x] **Step 1: Write failing discovery and projection tests**

Import `load_version_references` from the not-yet-created consumer module. Add table-driven subtests for absent, one valid, two valid, malformed JSON, stale `to_version`, and path/field mismatch; every case except one-valid asserts an empty index and a warning containing the failure category. In the one-valid case, call `references_for("New source", "ko-KR", [{"file": "same.rs", "line": 50, "kind": "label"}])`, assert `historical_translation == "과거 번역"`, and assert the projection contains neither the all-locale `translations` object nor the full `old_occurrences` list.

Add a failing pipeline test following the existing VS Code reference fixture. Write manifest, translation, and report fixtures; call `prepare_translation_batches()` with `PrepareTranslationOptions(current_version="v2")`; assert `previous_version_references` appears in batch JSON and Markdown, contains only the `ko-KR` historical translation, and `plan["version_diff"]` exactly reports loaded status, report path, reference source count `1`, and no warnings.

Also test report absence, invalid/ambiguous warning behavior, identical references under two model output directories, and unchanged report bytes after preparation.

- [x] **Step 2: Run Task 3 tests and verify RED**

Expected: import failure for `version_references` and missing option/entry fields.

- [x] **Step 3: Implement the consumer module**

Create `VersionReferenceIndex` with `references_for(source: str, language: str, current_occurrences: list[dict[str, Any]]) -> list[dict[str, Any]]`. Create frozen `VersionReferenceLoadResult` fields `status: str`, `path: str | None`, `warnings: list[str]`, and `index: VersionReferenceIndex`, plus a class method returning the disabled/empty result. Add `load_version_references(root: Path, current_version: str) -> VersionReferenceLoadResult`.

Discover only `reports/version-diff/*-to-<current>/key-changes.json`. Require exactly one discovered file and a valid schema/path match. Enclose discovery, I/O, JSON decode, schema validation, indexing, and projection in non-throwing boundaries that return warnings and no references.

Project `old_source`, current locale `historical_translation`, score, match kind, signals, and one deterministic best old occurrence. Select the old/new pair by maximum `0.10 same_file + 0.06 same_kind + 0.04 same_call`; break ties with the normalized occurrence tuple specified in the design.

- [x] **Step 4: Inject references through `prepare_translation_batches()`**

Extend the existing frozen `PrepareTranslationOptions` with `current_version: str | None = None`. At the start of preparation, set `version_references` to `load_version_references(root, options.current_version)` when a version is present and to `VersionReferenceLoadResult.disabled()` otherwise.

Pass the index and `language` into `_translation_entry()`. Add `previous_version_references` only when non-empty. Add this generated batch instruction:

```text
`previous_version_references` are optional historical translation hints and do not guarantee equivalent meaning. Decide whether to reuse, adapt, or ignore them using the current source, code context, context group, style guide, and glossary; current placeholders and protected tokens win, and only the current source key may be output.
```

Write only aggregate `version_diff` metadata (`status`, `path`, `reference_source_count`, `warnings`) to the actual output directory's `plan.json`.

Move config loading in `run_prepare_translation()` before options construction and pass `config.zed_version` as `current_version`. Do not add a public prepare flag.

- [x] **Step 5: Run Task 1–3 tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_version_diff tests.test_translation_pipeline tests.test_cli
```

Expected: PASS. Confirm generated model output JSON formats remain unchanged.

---

### Task 4: Replace orchestration prose with executable workflow documentation

**Files:**
- Modify: `AGENTS.md`
- Modify when present: `AGENTS.override.md`
- Modify: `prompts/commands/translation-start-new-keys.md`
- Modify: `tests/test_translation_prompt_contracts.py`
- Modify: `docs/readme/ko-KR.md`
- Verify: `docs/superpowers/specs/2026-07-13-version-bump-deleted-key-memory-design.md`

- [x] **Step 1: Replace large prose contracts with failing minimal contract tests**

Rewrite only the previously added producer/consumer tests. Require:

```python
required_agents_tokens = (
    "generate-version-diff",
    "reports/version-diff/<from-version>-to-<to-version>/key-changes.json",
    "prepare-translation",
    "automatically",
)
required_prompt_tokens = (
    "previous_version_references",
    "automatically",
    "optional historical translation hints",
)
forbidden_prompt_tokens = (
    "Load Optional Version-Bump Deleted-Key Memory",
    "deleted-key-memory.json",
    "exactly one valid candidate exists",
    "Pass only those relevant candidate records directly",
    "deleted_key_memory_sources",
    "relation-dependent review context",
    "terminology_only",
)
```

Keep `AGENTS.override.md` checks conditional when the local-only file is absent. Add an assertion that the new-key orchestration prompt does not contain concrete Git history commands or manual report discovery.

- [x] **Step 2: Run the prompt contracts and verify RED**

Expected: failures because the current instructions still define manual deleted-key-memory production and consumption.

- [x] **Step 3: Simplify `AGENTS.md` and synchronize the override**

Add `generate-version-diff` to the CLI table and pipeline immediately after `extract`. Replace the current multi-paragraph AI producer contract with concise executable guidance:

```text
After a version-bump extract, run `uv run zed-i18n generate-version-diff`.
The command reads the Git baseline, writes the gitignored `key-changes.json`,
and `prepare-translation` automatically injects locale-specific references.
The main orchestrator must not classify or distribute key-change candidates.
```

Describe the generated report path under Generated Files/Key Paths. Remove the old `deleted-key-memory.json` path and relation schema. Apply the same shared-section changes to the local override while preserving its local-only prefix.

- [x] **Step 4: Shrink `translation-start-new-keys.md`**

Remove manual report discovery, deterministic-selection prose, candidate filtering, direct task-context handoff, aggregate bookkeeping, report mutation guards, and deleted-memory anomaly clauses. Do not replace them with equivalent orchestration prose.

Retain only that `prepare-translation` automatically injects references into generated batch prompts and that translation/validation agents follow the batch interpretation rule. Renumber procedure phases and final-report fields after removal.

- [x] **Step 5: Document the executable workflow in the Korean README**

Edit the source-of-truth `docs/readme/ko-KR.md` first:

```powershell
uv run zed-i18n extract --zed-root .cache/zed/<version>-clean-extract
uv run zed-i18n generate-version-diff
uv run zed-i18n audit-candidates --zed-root .cache/zed/<version>-clean-extract
```

Explain in one paragraph that the generated gitignored report is automatically consumed by `prepare-translation`, and that batch agents decide whether historical translations remain semantically applicable. Do not edit other localized README files in this task; report that they need propagation, as required by the project instructions.

- [x] **Step 6: Run prompt and documentation tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_translation_prompt_contracts
```

Expected: PASS, with the override sync test passing locally or skipping in a clean clone.

---

### Task 5: Complete integration verification

**Files:**
- Verify all files changed by Tasks 1–4

- [x] **Step 1: Run focused tests together**

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_version_diff tests.test_translation_pipeline tests.test_cli tests.test_translation_prompt_contracts
```

Expected: PASS.

- [x] **Step 2: Run the full suite outside the sandbox if needed**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Expected: all tests pass. If sandbox execution produces `WinError 5` under `tests/.tmp`, rerun the exact command outside the sandbox; do not change tests to hide that environment failure.

- [x] **Step 3: Verify the CLI surface without generating a real same-version report**

```powershell
.\.venv\Scripts\python.exe -m tools.zed_i18n.cli generate-version-diff --help
```

Expected: usage shows optional `--base-ref`. Do not run generation against the current repository when baseline and current versions are equal.

- [x] **Step 4: Inspect the read-only Git diff and ignored output contract**

```powershell
git -c safe.directory=E:/Programming/Github/zed-i18n diff --check
git -c safe.directory=E:/Programming/Github/zed-i18n status --short
git -c safe.directory=E:/Programming/Github/zed-i18n check-ignore -v reports/version-diff/v1-to-v2/key-changes.json
```

Expected: no whitespace errors; only approved source, test, prompt, and documentation changes; the generated report path is ignored. Do not stage or commit.

- [x] **Step 5: Final specification and quality review**

Run separate read-only reviews for specification compliance and code quality. Resolve every actionable finding, rerun affected tests, then rerun the full suite before reporting completion.
