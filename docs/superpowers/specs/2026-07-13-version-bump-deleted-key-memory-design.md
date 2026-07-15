# Automated Version-Diff Translation References Design

## Goal

Make version-to-version key tracking deterministic and executable. The main orchestrator should only run a report-generation command; `prepare-translation` should automatically add relevant historical translations to each generated batch. Translation and validation agents decide whether an old translation is semantically reusable while working with the current source context.

The generated report and translation workspaces remain under `reports/` and are ignored by Git. Git history remains the source of truth.

## Decision

Use a leaf-judgment hybrid:

1. A script performs every deterministic task: reading the Git baseline, finding added and deleted keys, recovering historical translations, ranking candidate links, and writing a shared report.
2. `prepare-translation` validates and loads that report, filters it to the exact current source and locale, and embeds compact references in batch entries.
3. Each translation agent decides whether to reuse, adapt, or ignore a reference using the current code context, context group, locale style guide, and glossary. The separate validation agent reviews the same evidence from the batch file.

The main orchestrator does not inspect, classify, rewrite, or distribute candidate records. A separate main-AI semantic classification phase is deliberately excluded from the default workflow because it creates a serial, non-deterministic artifact whose mistakes would affect every locale and model run.

## Alternatives considered

### Script assigns semantic relationships

Rejected. Code can prove lexical and structural facts but cannot reliably decide whether meaning is equivalent. Historical examples include highly similar strings whose scope changed, such as adding protected Git metadata exceptions.

### Main orchestrator classifies candidates once

Not selected for the default workflow. It would reduce repeated judgments across locales, but it would require another AI result schema, validation and retry behavior, and a reviewed-report merge step. It would also anchor every locale and compared model to one model's potentially incorrect classification.

This may be reconsidered later as an optional shared overlay only if measured candidate volume makes leaf judgment too expensive.

### Translation and validation agents judge supplied candidates

Selected. These agents already receive the current source, code context, context groups, locale-specific guidance, glossary, and existing translations. Candidate generation remains reproducible, while semantic mistakes remain isolated and independently reviewable.

## Components

### Version-diff generator

Add a CLI command:

```text
uv run zed-i18n generate-version-diff
```

The command defaults to Git `HEAD` as the baseline and accepts `--base-ref <ref>` for recovery or non-standard runs. It resolves the ref to a commit before reading any baseline file. All Git operations are read-only.

The implementation lives in a focused module such as `tools/zed_i18n/version_diff.py`. Pure diff and scoring functions take in-memory snapshots so most behavior can be unit tested without invoking Git.

The generator reads:

- baseline `config/project.toml`, `catalog/en-US.json`, and `manifest/ui-strings.json` through `git show`;
- baseline final `translations/<locale>.json` files discovered from the Git tree, excluding model-scoped file names;
- current working-tree config, catalog, and manifest.

For both baseline and current snapshots, `catalog/en-US.json` and `manifest/ui-strings.json` must be JSON objects with exactly the same source-key set. The generator fails on a mismatch and uses that validated key set for added/deleted calculation. Catalog values are not used for identity; exact JSON object keys are the source identities.

A baseline translation is treated as a final locale file only when it is directly under `translations/`, its name matches `<locale>.json`, and its stem contains no period. Model-scoped files follow `<locale>.<model>.json` and are therefore excluded. Locale file values must be string-to-string mappings; only non-empty translations are recovered.

It writes exactly one report:

```text
reports/version-diff/<from-version>-to-<to-version>/key-changes.json
```

The command fails without writing a partial report when the baseline cannot be resolved, required snapshots are missing or malformed, the source and target versions are equal, or the output path would not agree with report metadata. JSON is written atomically and deterministically.

### Candidate scoring

Added and deleted keys are exact set differences. Candidate links are ranked using facts rather than semantic labels:

- source similarity after lowercase alphanumeric token normalization;
- similarity after replacing placeholders with a common placeholder token;
- whether any old/new occurrence pair shares a file, kind, or call;
- whether differences are limited to mechanically observable surface or placeholder-shape changes.

Normalization and similarity are fully deterministic:

1. Raw normalization applies `casefold()`, extracts ASCII alphanumeric runs with `[a-z0-9]+`, and joins them with one space.
2. Placeholder normalization scans with the same escaped-brace and Rust-unicode rules as `rust_format_placeholders`, replaces every actual Rust format placeholder with the token `placeholder`, and then applies raw normalization.
3. `raw_similarity` and `placeholder_normalized_similarity` are `difflib.SequenceMatcher(None, old, new, autojunk=False).ratio()` over the respective non-empty normalized strings. An empty side is not comparable.
4. `normalized_text_equal` is true when the two non-empty raw-normalized strings are identical. `placeholder_shape_equal` is true when the two non-empty placeholder-normalized strings are identical.
5. `same_file`, `same_kind`, and `same_call` are true when any old occurrence and any new occurrence share the corresponding non-empty field.

The initial score is:

```text
text_score = max(raw_similarity, placeholder_normalized_similarity)
score = min(1.0, text_score + 0.10 same_file + 0.06 same_kind + 0.04 same_call)
```

All occurrence pairs participate in the overlap signals; the implementation must not rely only on the first occurrence. Candidates below `0.70` are omitted. Each new source keeps at most three candidates, ordered by descending score and then exact `old_source` for stable ties. `match_kind` is `normalized_exact` when `normalized_text_equal` is true, otherwise `placeholder_shape` when `placeholder_shape_equal` is true, and otherwise `similarity`. Surface signals select this descriptive label but add no score beyond the two similarities already included in `text_score`.

The report always lists every exact added and deleted source, including sources without translations, so its key-change counts remain complete. Only deleted sources with at least one recovered non-empty historical translation can become candidates. Empty or non-comparable normalized sources are not scored.

These defaults recovered 29 of 32 manually identified historical pairs in the available v1.10.0 update material while limiting the candidate set to 66. The score remains evidence, not a statement of semantic equivalence.

### Report schema

The report is self-contained and locale-neutral:

```json
{
  "schema_version": 1,
  "from_version": "v1.10.3",
  "to_version": "v1.11.0",
  "base_commit": "<40-character commit>",
  "summary": {
    "added": 100,
    "deleted": 70,
    "candidate_pairs": 42
  },
  "deleted": {
    "Old source": {
      "status": "accepted",
      "occurrences": [],
      "translations": {
        "ko-KR": "과거 번역"
      }
    }
  },
  "added": {
    "New source": {
      "status": "accepted",
      "occurrences": [],
      "candidates": [
        {
          "old_source": "Old source",
          "score": 0.94,
          "match_kind": "similarity",
          "signals": {
            "normalized_text_equal": false,
            "placeholder_shape_equal": true,
            "same_file": true,
            "same_kind": true,
            "same_call": true
          }
        }
      ]
    }
  }
}
```

`match_kind` describes only the strongest mechanical match, such as `normalized_exact`, `placeholder_shape`, or `similarity`. The schema has no `equivalent`, `adapt`, or `terminology_only` field.

Repeated old sources and candidate arrays naturally support one-to-many and many-to-one changes. Historical occurrence and translation values are copied verbatim into the shared report.

Schema version 1 validation requires:

- a top-level object with `schema_version` exactly `1`, non-empty string versions, a 40-character hexadecimal `base_commit`, and object-valued `summary`, `deleted`, and `added`;
- non-negative integer summary counts consistent with the sizes of `added`, `deleted`, and all candidate arrays;
- source-keyed `deleted` and `added` objects whose keys and `old_source` values are strings;
- list-valued occurrence fields containing objects, and string-valued statuses; when present, `file`, `kind`, and `call` must be strings, `line` must be a positive integer, and `start_byte` and `end_byte` must be non-negative integers;
- translation objects mapping locale strings to non-empty strings;
- candidate lists of at most three unique `old_source` values, each referencing an existing `deleted` entry;
- finite numeric scores in the inclusive range `0.0` through `1.0`, an allowed `match_kind`, and boolean-valued signal fields;
- path `from`/`to` components that exactly equal the JSON versions.

Unknown fields may be ignored only within schema version 1; missing or wrongly typed required fields invalidate the entire report. Consumer discovery, file reading, JSON decoding, and schema validation are enclosed in a non-throwing boundary: any exception or validation error becomes a `plan.json` warning and an empty reference index.

### Automatic context assembly

`prepare_translation_batches()` is the context assembly boundary. It already adds `context_group` and `vscode_references`; previous-version references follow the same pattern.

For the current `zed_version`, preparation discovers canonical `key-changes.json` paths whose directory ends in `-to-<current-version>`. It uses a report only when exactly one canonical candidate file is discovered and that file is valid, with path versions equal to its JSON `from_version` and `to_version`. Multiple discovered files, or any malformed, stale, or path-mismatched candidate, cause all version-diff references to be ignored. These conditions add a warning to `plan.json` and otherwise leave translation behavior unchanged. Selection never uses timestamps or newest-file ordering.

For every batch entry, preparation performs these filters in code:

- the report's added key must exactly equal the entry's current source;
- the historical translation must exist for the current locale;
- no more than the report's three deterministically ordered candidates are included.

The entry receives a compact projection:

```json
{
  "source": "New source",
  "previous_version_references": [
    {
      "old_source": "Old source",
      "historical_translation": "과거 번역",
      "score": 0.94,
      "match_kind": "similarity",
      "signals": {
        "same_file": true,
        "same_kind": true,
        "same_call": true
      },
      "old_occurrence": {
        "file": "crates/example/src/example.rs",
        "line": 42,
        "kind": "label",
        "call": "Label::new"
      }
    }
  ]
}
```

Only the current locale's historical translation and the best supporting old occurrence are projected. For every old/new occurrence pair, support is `0.10 same_file + 0.06 same_kind + 0.04 same_call`, using the same non-empty equality signals as candidate scoring. The pair with the greatest support is selected. Equal-support pairs use ascending normalized tuples of `file`, `line`, `kind`, `call`, `start_byte`, `end_byte`, and original list index for the old occurrence and then the new occurrence; missing strings sort as `""` and missing integers after present values. If there is no old occurrence, `old_occurrence` is omitted. Other locales and full old occurrence arrays stay in the shared report to control prompt size.

Because batch JSON and batch Markdown are the generated context delivered to agents, they intentionally contain these references. Candidate content remains forbidden in final/model translation JSON and summary files. `plan.json` records only aggregate metadata under `version_diff`: `status`, `path`, `reference_source_count`, and `warnings`.

### Agent interpretation

The shared batch instruction is short:

> `previous_version_references` are optional historical translation hints and do not guarantee equivalent meaning. Decide whether to reuse, adapt, or ignore them using the current source, code context, context group, style guide, and glossary; current placeholders and protected tokens win, and only the current source key may be output.

Translation agents receive this instruction through generated batch prompts. Validation agents already receive the same batch files, so they can independently review the decision without any orchestrator-side candidate handoff.

## Workflow and instruction changes

The version-bump workflow becomes:

```text
fetch-zed
  → extract
  → generate-version-diff
  → audit-candidates
  → candidate/status review
  → prepare-translation  (automatically injects references)
  → translation and validation agents
```

The main orchestration instruction is limited to running `generate-version-diff` after extraction. It does not ask the main AI to compare keys or make semantic judgments.

The current manual report discovery, validation, filtering, direct handoff, and aggregate bookkeeping in `translation-start-new-keys.md` are removed. That prompt only states that `prepare-translation` injects optional references automatically and that agents must apply the generated batch instruction.

The existing `deleted-key-memory.json` prose contract and semantic relation schema are replaced by this executable `key-changes.json` contract.

## Error handling

- Generator input or Git failures are command failures and must be visible to the orchestrator; no partial report is accepted.
- An existing report is replaced only after a complete new report has been validated and serialized.
- Consumer discovery problems are non-blocking because translation must remain possible without historical references.
- Consumer warnings are stored in the actual output workspace's `plan.json`, including custom `--output-dir` runs.
- A report is always read-only during preparation. Preparation never repairs, enriches, or rewrites it.
- Old/deleted source strings can appear only inside reference context, never as translation result keys.

## Testing

### Generator tests

- exact added and deleted sets;
- baseline version, manifest, locale translation, and occurrence recovery;
- exclusion of model-scoped translation files;
- placeholder normalization and all-occurrence overlap signals;
- stable score, threshold, ordering, and top-three limit;
- one-to-many and many-to-one candidates;
- malformed baseline, equal versions, and atomic-write failure behavior;
- no semantic relationship labels in output.

### Translation pipeline tests

- valid current report is selected automatically;
- missing, invalid, stale, or ambiguous reports produce warnings and no references;
- exact current source and current locale filtering;
- only compact locale-specific projections reach batch JSON and Markdown;
- model-scoped output directories receive identical references;
- deleted source strings never enter result keys or final/model translation objects;
- the shared report is not modified.

### CLI and prompt tests

- command parsing and optional `--base-ref`;
- read-only Git command construction;
- the short batch interpretation rule remains present;
- the main orchestration prompt runs the generator but contains no manual discovery or semantic-classification workflow.

Most of the large prose-presence tests added for the previous design are removed in favor of these behavioral tests.

## Success criteria

- The main orchestrator only runs the generator command; it performs no key matching or semantic review.
- A generated report can be reproduced from a fixed Git baseline and working tree.
- `prepare-translation` automatically supplies useful, locale-specific historical references without orchestration logic.
- Report absence or invalidity never prevents normal translation.
- Model comparison runs receive the same deterministic reference evidence.
- Prompt instructions remain short and focused on interpretation, not mechanics.
- No report or memory artifact is tracked by Git.
