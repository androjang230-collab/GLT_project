# GLT 0.9.0 — RPG Maker Plugin-Visible Contracts

GLT 0.9.0 adds a conservative path from active RPG Maker plugin evidence to
portable translation entries and safe reconstruction. It does not execute
plugin JavaScript and does not treat every value affecting a display as text.

## Inventory and bounded analysis

GLT reads the bounded `var $plugins = [...]` registry without `eval`,
`Function()`, Node, or another JavaScript engine. Active plugins are selected by
their registry status and connected to exact same-name files below
`js/plugins/`. Load order, parameters, source availability, and source hashes
remain evidence for analysis.

The analyzer follows bounded source → transform → sink relationships. Literal
parameter access, statically understood note grammars, limited helper flows,
and recognized display sinks can produce evidence. Dynamic or ambiguous flows
remain audit-only instead of being guessed.

## Semantic roles

| Role | Meaning | Automatically extracted |
| --- | --- | --- |
| `TRANSLATABLE_TEXT` | Verified textual display use, safe grammar boundary, and exact storage | Only with a supported contract |
| `VISIBLE_FORMATTING` | Visible size, position, color, spacing, opacity, timing, or style value | No |
| `INTERNAL_CONTROL` | ID, switch, flag, filename, control keyword, or configuration | No |
| `MIXED_USE` | A source is used both as displayed text and a logic token | No |
| `UNSAFE_TEXT` | Evidence reaches executable or otherwise unsafe syntax | No |
| `UNKNOWN` | Source, transform, sink, semantics, or storage cannot be proven | No |

A player-visible effect is therefore not sufficient proof that a value is a
translation string. This distinction prevents font sizes, coordinates, and
logic tokens from entering translation JSONL.

## Contract support

GLT's contract model describes storage and grammar behavior rather than a list
of plugin names.

### Full extraction and apply

- `scalar_parameter_text` binds a visible scalar parameter to its exact JSON
  string token in `js/plugins.js`.
- `delimited_block_text` binds only a visible body between deterministic
  opening and closing lines in a data JSON `note` field.

### Not apply-supported in 0.9.0

- `regex_capture_text`
- `meta_value_text`
- `tokenized_visible_segment`

These model types may provide audit or extraction evidence when their boundary
is understood, but apply rejects them unless a future version supplies a fully
validated reconstruction contract.

## Storage and apply safety

Every plugin-consumed entry records its contract ID and fingerprint, storage
identity, source and grammar fingerprints, parser and sink evidence, segment
ordinal/span, whitespace policy, and whether apply is supported. The JSONL and
artifact schema remain version 1.

Before planning a write, GLT performs a fresh extraction from the current game
and verifies the entry against that authoritative result. It checks file and
storage identity, contract type, original value, fingerprints, token or body
span, and surrounding grammar. Changed or unresolved sources are rejected.

For `plugins.js`, GLT re-resolves the active plugin/load-order/parameter key,
escapes the translated string, patches only the exact token span, validates a
temporary registry, and atomically replaces the staging file. It preserves
unrelated bytes, line endings, BOM state, order, whitespace, and trailing comma
style.

For delimited notes, GLT re-resolves the JSON path, block grammar, ordinal, and
body span. Only the body changes; delimiters and unrelated note content remain
unchanged. JSON is reparsed and structurally compared before an atomic staging
write. Multiple edits are aggregated in descending position order. Unexpected
overlap blocks the affected storage unit.

The original game is never a write target. Normal apply first creates and
hash-verifies a staging copy. Dry-run uses the same preflight and edit planning
without creating output. A plugin contract whose translation already equals
the validated source is reported as `NO_CHANGE` and omitted from the write
plan.

## Known limitations

The following remain unsupported or audit-only because exact semantics or
writable storage cannot be proven conservatively:

- dynamic meta property access;
- delayed object-property rendering such as TMNamePop-style flows;
- JSON-in-string parameter payloads and complex object configuration;
- computed `PluginManager.parameters` arguments;
- complex callbacks, closures, or asynchronous state propagation;
- arbitrary object state tracking;
- direct JavaScript literals without a supported writable origin;
- obfuscated, generated, or heavily minified JavaScript.

No plugin-specific hardcoded exception is used to bypass these limits.

## Validation summary

The 0.9.0 real-game validation found 66 supported plugin-consumed entries: 10
scalar parameters and 56 delimited note bodies. All 66 passed dry-run with no
precondition failure or overlap. A controlled six-entry apply on a disposable
copy preserved plugin registry bytes outside the selected tokens, changed only
the approved note bodies semantically, and re-extracted all 66 entries with
stable IDs and storage identities. The source game remained byte-identical.
