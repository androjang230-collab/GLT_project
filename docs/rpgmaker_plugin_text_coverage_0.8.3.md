# GLT 0.8.3 — RPG Maker Plugin Text Coverage

## Scope

0.8.3 adds a deliberately narrow plugin-command rule layer. It does not scan
plugin JavaScript, event scripts, move routes, or every string in plugin
arguments. Schema version remains 1; rule metadata is optional JSONL metadata.

## Verified translation rules

| Engine/code | Verified context | Translation leaf | Apply behavior |
| --- | --- | --- | --- |
| MV 356 | command prefix `インフォ表示` | payload after the preserved separator | rebuild the original prefix/separator plus translation |
| MZ 357 | plugin `MNKR_TMLogWindowMZ`, command `addLog` | `parameters[3].text` | replace only that JSON string leaf |

Both rules use the existing location ID, source equality, JSON path, type,
control-code, staging copy, semantic diff, and atomic-write checks. Empty
translations remain no-ops. Optional JSONL fields identify `source_kind`,
`classification`, plugin/command, argument path, and rule; schema migration is
not required.

## Conditional and internal candidates

MV `P_SHAKE`, `P_SPIN_RELATIVE`, `D_TEXT_SETTING`, non-text payloads, and
path-like payloads are internal. Unknown text-like payloads are conditional
audit candidates and never enter automatic apply.

For MZ 357, parameters 0–2 are identifiers/editor labels, not translation
targets. Argument scanning is recursive and bounded. Keys such as `text`,
`message`, `messageText`, `displayText`, `description`, `help`, `title`,
`label`, `caption`, and `name`, plus Japanese-bearing values, may be reported
as conditional. File/path/script/ID/config-like keys are excluded.

Code 657 is never an independent translation entry. A following `key = value`
annotation is linked to its 357 command in the audit. During verified apply, a
matching `text` mirror is synchronized; a mismatch is preserved and reported
as `PLUGIN_MIRROR_MISMATCH`. Standalone or unresolved annotations stay audit
only.

## Privacy and source protection

Candidate artifacts retain hashes, lengths, classifications, portable source
locations, and rule context—not raw game text or absolute paths. Audit is
read-only and verifies its selected-source fingerprint before and after.

The local MV oracle audit observed 10,266 code-356 commands: 734 verified,
3,395 conditional, and 6,137 internal. `Map002.json` contained 6 occurrences
(all internal); `CommonEvents.json` contained 3,150 occurrences
(468 verified, 564 conditional, 2,118 internal). No 357 or 657 command was
present. The selected-source SHA-256 was identical before and after:
`67ed43be5753b32ad1da55fcfd8545fb4f3f43e632867fa782a77ccb2e07ad27`.
No oracle text or generated oracle report is stored in this repository.

## Known limits

- Conditional candidates require a future explicit rule before apply.
- 657 synchronization supports exact top-level `key = value` annotations only.
- No local MZ oracle containing 357/657 was available; those paths use synthetic
  regression fixtures.
- Plugin JavaScript/source rewriting and event script code 355/655 remain out
  of scope.
