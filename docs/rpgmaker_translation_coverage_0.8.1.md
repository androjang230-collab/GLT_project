# RPG Maker Translation Coverage Audit — GLT 0.8.1

## 1. Scope and conclusion

0.8.1 is a read-only evidence layer. It does not add extraction rules, create
`TranslationEntry` objects, change canonical IDs, or write to a game. The new
`rpgmaker-audit` command inventories stock event commands, move-route commands,
database fields, plugins, conditional candidates, mirrors, unsafe strings, and
unknowns. Reports contain hashes and structural metadata, not game text or
absolute paths.

The highest-confidence standard gaps are event codes 320, 324, and 325 and
`Classes.json[*].name`. Code 402 is an editor mirror of code 102, not a second
stock-runtime display source. MV 356, MZ 357, scripts, and variable script
operands require contextual rules and remain outside extraction.

## 2. Evidence model and sources

Grades used by this audit are:

| Grade | Meaning |
|---|---|
| A | Local game structure plus runtime semantics and independent documentation |
| B | Local game structure plus runtime/source semantics |
| C | Runtime/source semantics or a controlled synthetic runtime-shaped fixture |
| D | Official documentation only |
| E | Hypothesis requiring a real oracle |

Primary references:

- RPG Maker MZ's official [event command help](https://rpgmakerofficial.com/product/MZ_help-en/01_10.html)
- Official MZ [event-code parameter reference](https://rpgmakerofficial.com/product/mz/plugin/javascript/script_reference/eventcode.pdf)
- Official MZ [database reference](https://rpgmakerofficial.com/product/mz/plugin/javascript/script_reference/database.pdf)
- Official MZ [plugin command tutorial](https://rpgmakerofficial.com/product/mz/plugin/make/koushiki.html)
- Official MZ [plugin annotation guide](https://rpgmakerofficial.com/product/mz/plugin/make/annotation.html)
- RPG Maker MV's public [core-script Game_Interpreter](https://github.com/rpgtkoolmv/corescript/blob/master/js/rpg_objects/Game_Interpreter.js)

The local oracle was an MV deployment containing `data/Map002.json`,
`data/CommonEvents.json`, stock runtime scripts, and plugin metadata. No raw
oracle text or asset was copied into this repository.

## 3. Current GLT extraction scope

The extractor remains unchanged:

- Event 101 parameter 4: MZ speaker/name box
- Event 401 parameter 0: dialogue line
- Event 102 parameter 0 array items: choices
- Event 405 parameter 0: scrolling-text line
- Actors: `name`, `nickname`, `profile`
- Items, Weapons, Armors: `name`, `description`
- Skills: `name`, `description`, `message1`, `message2`
- States: `name`, `message1` through `message4`
- Enemies: `name`
- System: `gameTitle`, `currencyUnit`, player-visible type arrays, `terms`
- Map: `displayName` and the supported event commands
- CommonEvents and Troops: the supported event commands

Images, audio names, notes, scripts, plugin commands, editor event/common-event/
troop names, switches, and variables remain excluded.

## 4. Classification model

| Classification | Audit meaning | Automatic extraction in 0.8.1 |
|---|---|---|
| `VERIFIED_TRANSLATABLE` | Stock player-visible field with runtime evidence | Existing allowlist only |
| `CONDITIONAL_TRANSLATABLE` | Visible only for a particular plugin/API context | No |
| `MIRROR` | Editor branch label or annotation duplicating a canonical source | No |
| `INTERNAL` | Identifier, comment, asset reference, or control data | No |
| `UNSAFE` | Evaluated/opaque value whose edit can change behavior | No |
| `UNKNOWN` | No sufficient classification evidence | No |

Japanese-script detection is evidence only. Hiragana, katakana, CJK, and
control-code metadata never override the command/field classification.

## 5. Full event-code inventory

The machine report has one row for every catalogued code, including observed
parameter shapes and occurrence counts. The 124 stock MV/MZ event-list codes
are exhaustively assigned as follows:

| Class | Codes | Notes |
|---|---|---|
| Verified/current | 101, 102, 401, 405 | Existing GLT allowlist; only the documented text subpaths count |
| Verified/missed | 320, 324, 325 | Change Name, Nickname, Profile |
| Mirror | 402, 657 | Choice branch label; MZ plugin annotation |
| Conditional plugin | 356, 357 | MV raw and MZ structured plugin commands |
| Unsafe/script-bearing | 122, 355, 655 | Only the script-bearing 122 variant is unsafe; numeric variants are internal |
| Internal continuation/control | 0, 103, 104, 105, 111, 112, 113, 115, 117, 121, 123, 124, 125, 126, 127, 128, 129, 201, 202, 203, 204, 206, 211, 212, 213, 214, 216, 217, 221, 222, 223, 224, 225, 230, 232, 233, 234, 235, 236, 242, 243, 244, 246, 251, 281, 282, 285, 301, 302, 303, 311, 312, 313, 314, 315, 316, 317, 318, 319, 321, 326, 331, 332, 333, 334, 335, 336, 337, 339, 340, 342, 351, 352, 353, 354, 403, 404, 411, 412, 413, 505, 601, 602, 603, 604, 605 | Numeric flow/data, editor continuation rows, and internal IDs |
| Internal comment/label | 108, 118, 119, 408 | Text-bearing but not screen-visible |
| Internal asset/config reference | 132, 133, 138, 139, 140, 231, 241, 245, 249, 250, 261, 283, 284, 322, 323 | Audio/image names or configuration |

Unknown observed codes are added dynamically with `UNKNOWN`; they are not
silently treated as stock or translatable. `parameter_structure` gives exact
subpaths for every important string-bearing code, while
`observed_parameter_shapes` records bounded structural shapes for every code
seen in the audited game. The official event-code reference remains the
normative parameter list for unobserved non-string stock codes.

## 6. Key command findings

### 101 / 401 / 102 / 405

These remain verified. A 101 face filename is an asset reference; only MZ's
optional parameter 4 is a speaker. Code 102 owns the displayed choice array.
Codes 401 and 405 own message lines. Empty values are not counted as current
translation entries. Evidence: A for the local MV forms, C/D for MZ speaker.

### 102 ↔ 402

Stock MV `setupChoices` displays `102.parameters[0]`. `command402` tests only
the numeric branch index in `402.parameters[0]`; it does not read the editor
label in parameter 1. Current GLT writes 102 only. Therefore differing 102 and
402 labels are editor-representation drift, not a stock-runtime translation
failure. The audit links them by event list, indent, and choice index and reports
match status without synchronizing them.

Synthetic coverage includes one/many/nested choices, cancel branches, duplicate
labels, control codes, and empty choices. The local oracle contained 2,195 code
402 rows, with 1,800 textual differences. Inspection showed plugin-conditioned
choice prefixes and editor labels can intentionally differ; a blanket sync
would be unsafe. Evidence: A. A future optional editor-round-trip sync must be
plugin-aware and must never create a second canonical translation entry.

### 320 / 324 / 325

The stock runtime passes parameter 1 to `setName`, `setNickname`, and
`setProfile`. These values are subsequently shown by standard actor UI. The
current extractor misses them. They are `VERIFIED_TRANSLATABLE` candidates for
0.8.2. No occurrence was present in the audited MV oracle, so the conclusion is
grade C rather than A.

### 356 (MV plugin command)

The MV interpreter splits the raw command on whitespace, shifts the prefix, and
passes the remaining argument array to plugin code. Consequently the entire
string cannot be translated. The audit records a safe ASCII prefix (or a hash),
the untouched suffix hash/length, Japanese flags, and control codes. A future
rule must bind plugin family + command prefix + payload grammar. Evidence: A.

### 357 / 657 (MZ plugin command)

MZ plugin callbacks receive a structured argument object. Runtime-shaped data
commonly uses parameter 0 for plugin name, 1 for command name, 2 for an editor
display label, and 3 for the argument object. The audit recursively walks only
parameter 3 under strict depth/string limits. A key named `text` is evidence,
not proof: only plugin + command + argument-path rules can become verified.

Code 657 is catalogued as an editor annotation and linked backward through a
contiguous 657 group to its 357 command. It is a mirror, not a canonical entry.
The official MZ event-code PDF describes the plugin name, command name, and
arguments at a higher level; official plugin guidance confirms the object-form
callback arguments. This repository has synthetic 357/657 coverage, but the
available local oracle was MV and contained neither code. Precise four-slot and
657 conclusions are grade C/experimental pending a local MZ oracle.

### 355 / 655

These lines are concatenated and evaluated as JavaScript. A bounded regex can
flag known rendering APIs such as `$gameMessage.add`, `drawText`, and
`drawTextEx`, including a call split across 355/655. It cannot reliably identify
the displayed literal, aliases, computed values, filenames, or identifiers.
The result is candidate detection only: display-pattern blocks are conditional;
all others are unsafe. No script is persisted in reports. Evidence: B/C.

### 122

Only operand type 4 contains evaluated JavaScript in parameter 4. It is marked
unsafe. Constants, variable IDs, random ranges, and game-data operands remain
internal numeric data. Japanese characters do not make either form
translatable. Evidence: B/C.

### 108 / 408 / 118 / 119

Comments have no game effect. Labels and jump labels are matching internal
identifiers. All stay internal even when they contain Japanese. Evidence: A.

## 7. Move Route inventory

Move Route codes are kept separate from event code 205:

| Codes | Role | Classification |
|---|---|---|
| 0–40, 42–43 | movement, turn, timing, switch, visual control | `INTERNAL` |
| 41 | character image filename/index | `INTERNAL` asset reference |
| 44 | sound-effect object/name | `INTERNAL` asset reference |
| 45 | evaluated JavaScript | `UNSAFE` |

All 46 codes (0 through 45) appear in the machine inventory even when absent.
Move-route script strings are hash-only candidates and are never extracted.

## 8. Database field inventory

| File | Player-visible fields | Internal/asset/note fields | Current gap |
|---|---|---|---|
| Actors | name, nickname, profile | character/face/battler assets, note, IDs | None |
| Classes | name | traits, learnings, note, IDs | **name** |
| Skills | name, description, message1, message2 | animation/icon IDs, formula, note | None |
| Items | name, description | icon, effects, note | None |
| Weapons / Armors | name, description | icon, traits, note | None |
| Enemies | name | battlerName asset, actions, traits, note | None |
| Troops | event commands | troop name and member IDs | Name intentionally excluded |
| States | name, message1–4 | icon/traits/note | None |
| Animations | none verified | editor name and animation assets | None |
| Tilesets | none verified | editor name, tilesetNames assets, note | None |
| CommonEvents | event commands | editor name, switch ID | Name intentionally excluded |
| System | title, currency, terms, visible type arrays | switches, variables, audio/image config | None in current verified set |
| MapInfos | none verified | editor map name and hierarchy | Name intentionally excluded |
| MapXXX | displayName, supported event commands | event names, notes, image assets | Standard command gaps above |

The report provides observed counts per field. `Classes.name` is the confirmed
standard DB gap. Animation and tileset editor names must not be inferred as
player-visible merely because they contain Japanese.

## 9. Plugin inventory

`js/plugins.js` is parsed only for plugin name and enabled status. Parameter
values are not persisted. Bounded plugin-source scans inventory literal
`PluginManager.registerCommand` calls and `@command`, `@arg`, and string-like
`@type` annotations. Text-like argument names remain conditional evidence.
Dynamic registrations, minified/generated code, aliases, and runtime plugin
patches may not be discovered.

## 10. Actual MV sample statistics

The selected local oracle produced:

- 184,092 event commands, 77 unique observed codes, 124 stock codes catalogued
- 100,379 string-bearing command occurrences
- 33,365 nonblank observed verified event entries; all belonged to the current allowlist
- 10,266 conditional plugin candidates (all code 356)
- 2,195 code 402 rows, 2,177 nonblank mirror candidates, and 1,800 textual differences
- 34,032 internal string values and 450 verified DB values in the current allowlist
- 22,249 unsafe candidates/strings across scripts, script operands, and route scripts
- 2,186 source files before and after
- Selected-content SHA-256 unchanged before/after:
  `67ed43be5753b32ad1da55fcfd8545fb4f3f43e632867fa782a77ccb2e07ad27`
- Zero audit errors or warnings; no source output created

Focused files:

| File role | Selected observations |
|---|---|
| Map002 | code 205: 1; code 356: 6 |
| CommonEvents | 102: 151; 402: 395; 108: 560; 119: 720; 122: 1,935; 205: 13; 355: 108; 655: 199; 356: 3,150 |

This available oracle did not contain 320/324/325, 357/657, or 405. The lack of
those codes is not evidence that they are globally absent. The largest likely
cause of untranslated player-visible text in this game is plugin-defined MV 356
payloads, followed by possible display literals embedded in scripts. Both need
rule-based work, not generic extraction.

## 11. False-positive controls

Known examples that remain excluded include Japanese comments, Japanese label
identifiers, picture/audio filenames, plugin command prefixes, switch/variable
names, plugin parameter values, note tags, variable script operands, and script
state keys. CJK/kana detection never promotes them.

## 12. Safety and limits

- Maximum 20,000 files, 128 MiB per JSON, 8 MiB per plugin source
- Maximum 2,000,000 event commands
- Maximum nesting depth 32 and candidate string length 1 MiB
- Symlinks are not followed
- Reports and CSVs must be new files outside the game tree
- Before/after file count, total size, and selected-content SHA-256 must match
- Report paths are portable; raw source strings and absolute paths are absent
- JSON decode and malformed-command problems are explicit issues; unaffected
  files continue to be audited

## 13. CLI

```powershell
python glt.py rpgmaker-audit "D:\Games\Example" `
  --report ".\reports\rpgmaker_coverage.json" `
  --csv ".\reports\rpgmaker_candidates.csv"
```

Both outputs are optional. With no output option, the command prints aggregate
statistics only. Exit 0 means the audit completed without error issues, 2 means
the command could not run safely, and 3 means the bounded scan completed but
found error issues such as malformed JSON.

## 14. Known limitations and unknowns

- No local MZ 357/657 oracle was available for this audit.
- Plugin source inventory is lexical, not a JavaScript AST or runtime trace.
- Display-API regexes can produce false positives and false negatives.
- A 402 mismatch may be intentional under choice plugins; it is not auto-fixed.
- Dynamic/generated event commands remain `UNKNOWN` unless explicitly modeled.
- The audit quantifies observed verified coverage, not all hypothetical plugin UI.

## 15. Exact recommended 0.8.2 scope

1. Add stock event extraction/apply/QA rules for 320 parameter 1, 324 parameter
   1, and 325 parameter 1 with canonical location IDs and source-match checks.
2. Add `Classes.json[*].name` using the existing DB path-ID pattern.
3. Add a 102/402 editor-mirror QA warning, but do not make 402 canonical and do
   not blindly synchronize plugin-modified labels.
4. Obtain a real MZ oracle before any 357/657 extraction work.

Suggested later roadmap: 0.8.3 plugin-rule framework for MV 356 and MZ 357/657;
0.8.4 evidence-led System/DB expansion; 0.8.5 script/unknown candidate research.
No part of that roadmap is implemented in 0.8.1.
