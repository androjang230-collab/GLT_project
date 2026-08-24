# GLT 0.8.0 WOLF native format research and reference model

Research snapshot: 2026-08-24. This is an evidence report and a read-only
reference architecture, not a promise of native write compatibility.

## 1. Decision summary

- The recommended 0.8.1 primary parser basis is an **independent Python parser
  informed first by the MIT Kaitai declarations in `wolf-rpg-formats`, then
  cross-checked against WolfTL and the local official-Editor oracle**.
- WolfTL is the strongest current behavioral reference for `Game.dat`,
  `CommonEvent.dat`, databases and `.mps`, including newer/Pro handling. It is
  not selected as a runtime dependency because it would add a C++/Windows ABI,
  packaging and audit boundary to GLT.
- `wolf-rpg-formats` is the clearest declarative reference. The local WOLF
  3.682 sample nevertheless has `.mps` marker values outside older constraints
  in that schema, so generated Kaitai code must not be adopted without version
  gates and differential fixtures.
- WolfTrans and rewolf-trans are valuable lineage and logical-location
  references. Their warnings, age and destructive output behavior make them
  unsuitable as a drop-in GLT backend.
- Archive access, individual `.wolfx` protection, native logical parsing,
  compression and text selection are separate layers. Phase 9 does not combine
  them.
- The current `wolf:v1` canonical ID is assessed as **V2_LIKELY**, not
  `V2_REQUIRED`: source/domain/event/page/command concepts survived the first
  native correlation, but database field identity and command-string ordinals
  need a parser-level cross-route proof before v1 can be frozen.

Evidence grades used below:

| Grade | Meaning |
|---|---|
| A | Actual sample + official Editor oracle + independent implementation |
| B | Actual sample + independent implementation |
| C | Multiple independent implementations |
| D | Single implementation/source |
| E | Hypothesis, unknown, or not investigated sufficiently |

## 2. Scope and non-goals

Phase 9 adds portable logical models, bounded binary primitives, a signature and
correlation probe, synthetic malformed-input tests, and this decision record.
It does not add extraction from native data, a native writer, archive unpacking,
decryption, compression, repacking, `TranslationEntry` production, or a change
to any Phase 1–8 workflow.

No external project was executed. Shallow source snapshots were held outside
the repository for inspection only. No external code or real game data was
copied into GLT.

## 3. Primary sources and license/activity review

The activity column is the observed latest source commit at the research
snapshot, not a guarantee of maintenance.

| Project | Language | License observed | Active/archived evidence | Supported versions stated | Stated/observed role | GLT suitability |
|---|---|---|---|---|---|---|
| [WolfTrans](https://github.com/elizagamedev/wolftrans) | Ruby | MPL-2.0 | Not marked archived; latest observed 2017-11-30, `dcf5d76` | UNKNOWN | Native translation extractor/writer; early reverse-engineering lineage | Conceptual and differential reference only |
| [rewolf-trans](https://github.com/KCFindstr/rewolf-trans) | TypeScript | MIT | Not marked archived; latest observed 2022-03-06, `74aee1f` | UNKNOWN | WolfTrans-derived reader/writer with encoding options and stronger location contexts | Logical-location and parser comparison reference |
| [wolf-rpg-formats](https://github.com/djytw/wolf-rpg-formats) | Kaitai Struct, C | MIT | Not marked archived; latest observed 2024-04-28, `5c70e64` | 2.2x, 3.0x, 3.3x (README claim) | Declarative native data formats | Primary structural reference, not blindly generated runtime code |
| [WolfTL](https://github.com/Sinflower/WolfTL) | C++ | MIT | Not marked archived; latest observed 2026-07-08, `bfc38fc` | Exact matrix UNKNOWN; Pro support stated | Reads/writes translation-relevant `.dat`/`.mps` | Strongest current behavioral reference; no runtime integration in 0.8.0 |
| [WolfDec](https://github.com/Sinflower/WolfDec) | C++ | **No repository license found** | Not marked archived; latest observed 2023-11-18, `974071d`; README supersedes it with UberWolf | Exact matrix UNKNOWN; new/Pro unsupported by README direction | DXArchive/WOLF archive decrypter | Facts/reference only; source reuse prohibited absent a license grant |
| [UberWolf](https://github.com/Sinflower/UberWolf) | C++ | MIT | Not marked archived; latest observed 2026-08-08, `f419137` | Exact matrix UNKNOWN; Pro support stated | Current archive/Pro/`.wolfx` decryption implementation and native parser family | Archive/protection research reference; intentionally outside native parser layer |

The [official WOLF Translation Support Tool manual](https://silversecond.com/WOLF_Translation_tool/Manual.html)
remains the product-level oracle for text I/O. The [official Pro individual-file
encryption help](https://smokingwolf.github.io/tool_wolf_rpg_editor/help/02_file_crypt_pro.html)
establishes that `.wolfx` is individual-file protection and must not be treated
as the ordinary `.wolf` archive container merely because both names include
“wolf”.

License policy:

- MIT sources can be studied and, if code is later reused, require copyright and
  permission notices in distributions.
- MPL-2.0 is file-level copyleft. Phase 9 copied no WolfTrans code, avoiding a
  mixed-source maintenance obligation.
- WolfDec has no detected license file. Public visibility is not permission to
  copy or redistribute its code.
- Phase 9 records independently observed facts (magic bytes, local hashes,
  control structure concepts) and implements its own small bounded probe.

## 4. Capability matrix

`Y` means source is present for the capability, `P` means partial/indirect,
`N` means absent, and `?` means not established by the inspected source.

| Project | Game.dat | CommonEvent.dat | DataBase.dat | CDataBase.dat | SysDataBase.dat | `.mps` | `.wolf` | `.wolfx` | Read | Write | Decrypt | Repack | Tests | Documentation |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| WolfTrans | Y | Y | Y | Y | Y | Y | N | N | Y | Y | N | N | one small Ruby suite | README/code |
| rewolf-trans | P | Y | Y | Y | Y | Y | N | N | Y | Y | P, legacy DAT crypto | N | no dedicated suite found | detailed README/code |
| wolf-rpg-formats | Y | Y | Y | Y | Y | Y | N | N | declarative | N | N | N | no fixture suite found | schemas/README |
| WolfTL | Y | Y | Y | Y | Y | Y | N | P, Pro data layer | Y | Y | Y | N | no dedicated suite found | README/code |
| WolfDec | N | N | N | N | N | N | Y | N | archive | N | Y | extraction only; repack UNKNOWN | no dedicated suite found | README/code |
| UberWolf | P, bundled parser family | P | P | P | P | P | Y | Y | Y | protection transform only; native writer not product claim | Y | full repack UNKNOWN | no dedicated suite found | README/code |

Source inspection found explicit magic checks, little-endian readers, counted
arrays and symmetric dump methods in the native translation families. WolfTL and
UberWolf also contain LZ4 and WOLF crypt components. UberWolf/WolfDec contain
DXArchive version-specific readers plus Huffman-related code. The inspected
sources did not establish one universal checksum contract, one alignment rule,
or full archive repack support across versions; those items remain UNKNOWN.

Version claims are kept at their source strength. `wolf-rpg-formats` states
2.2x/3.0x/3.3x. WolfTL states Pro support but does not publish a complete
version-by-version conformance matrix. WolfTrans and rewolf-trans do not provide
a trustworthy modern-version matrix. UberWolf is current protection/archive
evidence, not proof that every native logical record version is supported.

## 5. Layer model

```text
game path discovery
        |
        v
archive/container layer (.wolf/custom extension)
        |
        v
individual protection layer (.wolfx / Pro data protection)
        |
        v
compression/decompression layer
        |
        v
logical native documents (Game/CommonEvent/DB/mps)
        |
        v
logical records and text-field policy
        |
        v
existing GLT TranslationEntry adapter (future, not Phase 9)
```

Each boundary must accept bytes and emit a typed result with its own size,
version and integrity limits. A successful archive extraction does not prove a
valid native document; a valid native document does not make every string
translatable.

## 6. Format findings

### 6.1 Shared native string and version concepts

The Kaitai declarations describe little-endian length-prefixed, NUL-terminated
strings. WOLF v3 declarations use UTF-8 while older paths include Shift-JIS.
WolfTrans, rewolf-trans and WolfTL independently implement comparable bounded
reader concepts, providing grade B support for the broad model. Exact version
and encoding selection remains a document-version decision, not an encoding
guess.

### 6.2 Game.dat

The local header begins `00 57 00 00 4f 4c 00 46 4d 55` and matches the public
`Game.dat` declaration (grade B). Public references describe settings blocks,
game title/font strings and a trailing random-data region. The random area
explains the relatively high whole-file entropy and means file hashes can change
after Editor saves even when logical text does not. Phase 9 parses only the
signature and raw version marker.

### 6.3 CommonEvent.dat

The local header matches the `WOLF` + `FC` container family with a `0x55`
version header (grade B). Public sources agree on common-event records containing
event IDs, names, command arrays, memos and argument metadata (grade B).
Command arrays contain number parameters and separately counted string
parameters. A future parser must cap event, command, string and byte lengths at
every level.

### 6.4 Database files

`CDataBase.dat`, `DataBase.dat` and `SysDatabase.dat` match the `WOLF` + `FM`
family (grade B). Public formats model types, property positions, numeric blocks
and string blocks. `.project` metadata is needed to understand field semantics;
native data alone can recover strings but cannot safely decide whether every
string is translation text. DATANAME is therefore a field-policy problem above
the binary parser.

### 6.5 Map `.mps`

All four local maps match ten zero bytes followed by `WOLFM\0` and version-header
byte `0x55` (grade B). Public sources agree on map dimensions, tile blocks,
events, pages and event-command arrays (grade B). The local WOLF 3.682 sample has
later marker values `0x67` and `0x69`, while the inspected older Kaitai schema
constrains corresponding fields to `0x64` and `0x65/0x66`. This is direct evidence
that an unmodified generated parser would reject the sample.

### 6.6 `.wolf` and `.wolfx`

`.wolf` is an archive/container concern. DXArchive versions and key handling are
represented in WolfDec/UberWolf sources. `.wolfx` is documented by WOLF as Pro
individual-file encryption and has a different lifecycle. Neither is parsed by
the Phase 9 native probe. Full archive extraction, protection-key discovery and
repack remain separate, opt-in future work.

## 7. Local official-sample method

The user-supplied WOLF RPG Editor 3.682 directory was accessed read-only. The
existing official `ALL` export from Phase 7/8 was reused; the Editor was not
launched again. AutoBackup directories were excluded so historical copies did
not multiply or contradict runtime evidence.

The inventory stores only portable relative paths, byte counts, SHA-256,
bounded header hex and entropy. Known strings from the official export were
searched as UTF-8, CP932 and UTF-16LE. Persisted correlation rows contain only a
text SHA-256, character count, logical Auto location, encoding and bounded byte
offset evidence. No source string or absolute path is persisted.

Before and after a fresh probe, a full-tree portable hash was identical:

- SHA-256: `9052f8dc07bfd849ef3a2daa638b29b9827f99b77f7366d4c719156f2140ee77`
- files: 707
- bytes: 41,209,710

This fingerprint is research evidence only; it is not stored in application
configuration and does not identify the user path.

## 8. Local native inventory

The header column is the first 16 bytes, not a parsed structure dump.

| Portable source | Bytes | SHA-256 | Header | Entropy | Role |
|---|---:|---|---|---:|---|
| `Data/BasicData/CDataBase.dat` | 6167 | `f37a863a34c9c0d7cbc30269110ba83286f06d2cc628dde25b179a7a062cf8d5` | `005700004f4c55464d00c45742010004` | 4.818311 | database |
| `Data/BasicData/CommonEvent.dat` | 375207 | `7f3727f7c50b0188aa195aaee984e84bc1bbba986f07b71a5c94f2682ddbd4cd` | `005700004f4c55464300937f6b130094` | 6.363805 | common event |
| `Data/BasicData/DataBase.dat` | 17852 | `9c4eda538ab8f544931f09110163b7975a864a0beab972542bf3f208b4d23327` | `005700004f4c55464d00c4ad5c0100a9` | 6.035061 | database |
| `Data/BasicData/Game.dat` | 29209 | `583df6d6785f06227624aba4426406bb13d489da3b34887e384df2a69f1d5e7e` | `005700004f4c00464d55260000001008` | 7.005481 | game settings |
| `Data/BasicData/MapTree.dat` | 48 | `3135ebaf304fa5f9541606d48972111907c7fe1692433b2b4c2af115d13b435d` | `000000000000000000009004000000ff` | 1.712025 | unmodeled |
| `Data/BasicData/MapTreeOpenStatus.dat` | 29 | `2d92cd48e9eaab2a5c0a2035385d8910151887e90d7daa466009926247e599d1` | `00000000000000000000000000000000` | 0.785691 | unmodeled |
| `Data/BasicData/SysDatabase.dat` | 903 | `a2c0034ba4e0a1d4ab3e07b3da8467a7f80441cb6e0d7793ff68d8d4ae9e8ba7` | `005700004f4c55464d00c4cc0a000074` | 5.654513 | database |
| `Data/BasicData/TileSetData.dat` | 40344 | `8ca0067f26f6fa719585dafae33e18716f66330d60ac9252479278ac506250ca` | `005700004f4c55464d00d20600000004` | 1.697151 | unmodeled |
| `Data/MapData/Dungeon.mps` | 2804 | `d0d85651269aa0175aaac0f8cce757fe49cc665e23a45e0cdebde6e98d36f831` | `00000000000000000000574f4c464d00` | 6.234604 | map |
| `Data/MapData/SampleMapA.mps` | 24120 | `b28956f6476fa1ce5e21dfb34629ac7a9bf588ec6d062936ddd7326861e36274` | `00000000000000000000574f4c464d00` | 6.660529 | map |
| `Data/MapData/SampleMapB.mps` | 2756 | `aa928a961adb90de34cff067d9b7ad5564a6fb3e8dc370d64812f21d18285bc2` | `00000000000000000000574f4c464d00` | 6.129883 | map |
| `Data/MapData/TitleMap.mps` | 3049 | `eed564e593984faaa7f5dfd48f0b5771a8545f4a4d7286553ae577925765ce82` | `00000000000000000000574f4c464d00` | 6.551857 | map |

Nine of twelve files have a Phase 9 evaluated signature and all nine match. The
three `other_dat` files are inventoried but not guessed into an unrelated
format.

## 9. Native-to-Auto mapping

Exact portable filename counterparts were found for Game, CommonEvent, three
databases, TileSetData and four maps. `MapTree.dat` and
`MapTreeOpenStatus.dat` have no official Auto counterpart.

| Route | Auto records observed | Exact-byte correlations in bounded sample | Result |
|---|---:|---:|---|
| Game.dat | 2 | 1 UTF-8 | title route supported (A) |
| CommonEvent.dat | 69 dialogue | 0 | route exists by filename; command-text representation unresolved (C/E) |
| CDataBase/DataBase/SysDatabase | 3,646 combined | 19, all UTF-8; 2 also CP932 by byte coincidence | DATANAME/native string-block route supported (A) |
| four `.mps` maps | 224 combined | 1 UTF-8 at `SampleMapA`, command index 8 | one map event route supported; coverage insufficient (A/C) |

The correlation set was deliberately capped at 64 unique source strings per
file and eight offsets per encoding. A missing match is not proof that a record
is absent. It may reflect escaping, normalization, serialization, compression,
selection order or insufficient sampling.

The requested CommonEvent code-101 route was attempted against all 69 exported
dialogue records using raw, newline and trim variants in UTF-8/CP932. No exact
byte match was found. Phase 9 therefore does not claim a code-101 native mapping;
0.8.1 must parse the command array and compare decoded string parameters rather
than broad byte-search results.

## 10. Reference model

`NativeDocument -> NativeRecord -> NativeTextField -> NativeLocation` is the
proposed intermediate representation.

- `NativeDocument` owns a portable source, format family, observed version
  marker, parse scope and evidence grade.
- `NativeRecord` represents settings, DB types/rows, common events, map events,
  pages and commands without embedding translation policy.
- `NativeTextField` stores a source hash, length, encoding evidence and logical
  location. A future fully decoded parser may hold source text in memory, but a
  research report does not persist it.
- `NativeLocation.logical_components()` excludes byte offsets. Offsets live only
  in `byte_offset_evidence` because saves, encoding and block-size changes move
  them.
- The native model is not `TranslationEntry`. A later adapter selects verified
  translatable fields and produces common GLT entries only after cross-route
  conformance.

## 11. Canonical ID assessment

Assessment: **V2_LIKELY**.

Reasons to retain v1 for now:

- Portable logical source mapping works for the observed native/Auto pairs.
- Map event/domain/command concepts can identify the one confirmed map
  correlation without an offset.
- Changing IDs before native records are parsed would replace one provisional
  guess with another.

Reasons v2 is likely:

- Database `item0`/`item1` Auto fields need stable native type/property/record
  coordinates, not display labels.
- Common-event and map command string arrays need an explicit string-parameter
  ordinal distinct from command index.
- A versioned logical-source normalization is needed for `SysDatabase.dat` vs
  official `SysDataBase.Auto.txt` naming.
- Native documents may expose record identifiers that are more stable than
  Auto.txt presentation order.

Gate for `V2_REQUIRED`: at least two Editor versions and two routes per domain
must show that a v1 component is ambiguous, collides, or changes while the native
logical record remains the same. No v2 schema is implemented in 0.8.0.

## 12. Implemented read-only probe

```powershell
python glt.py wolf-native-probe <wolf_project> `
  --oracle <official_Data_AutoTXT> `
  --report <new_external_report.json>
```

The report is refused inside the game or oracle tree and is created atomically
without overwrite. The command:

- inventories direct runtime `.dat` and `.mps` sources;
- excludes Editor AutoBackup directories and symlinks;
- streams SHA-256 and entropy;
- reads at most 64 header bytes for signature classification;
- optionally correlates bounded known strings without persisting source text;
- never invokes the Editor or any researched third-party tool;
- never writes native or Auto files.

Safety limits include 10,000 native files, 256 MiB per probed file, 64 MiB per
correlation file, 64 candidate strings per file, 4,096 characters per string,
and eight saved offsets per encoding. `BoundedBinaryReader` also rejects negative
reads, truncated fields, oversized untrusted lengths and missing required NUL
terminators.

## 13. 0.8.1 parser recommendation

Implement **Game.dat read-only settings parser first**, then CommonEvent code-101.

Game.dat is the best initial target because the signature and one title string
have grade A cross-route evidence, the structure is smaller than event/database
graphs, and a parser can validate file-size/footer invariants without introducing
translation selection. The implementation should be independent Python using
`BoundedBinaryReader`, with `wolf-rpg-formats` as the declarative reference and
WolfTL as the differential behavioral reference.

0.8.1 acceptance gates:

1. parse WOLF 3.682 Game.dat settings with all lengths and end positions checked;
2. reproduce the official-export title hash and logical field without offsets;
3. reject malformed/truncated/oversized synthetic fixtures;
4. parse at least one older v2 or public legal fixture before claiming v2 support;
5. preserve current Auto.txt pipeline and keep native extraction disabled;
6. begin CommonEvent code-101 decoding only after the Game.dat parser passes
   differential tests.

Archive decryption and native writing should remain later milestones. A writer
requires parse/serialize byte equivalence, Editor-open validation, change-set
proof, version matrices and output-only atomic safety comparable to existing GLT
apply behavior.

## 14. Known limitations

- One local official WOLF version is not a compatibility matrix.
- The probe recognizes signatures, not complete documents.
- Entropy is descriptive and never used to assert encryption.
- Known-string correlation is capped and exact; negative results are
  inconclusive.
- No public project inspected provides a complete conformance suite covering
  all WOLF versions and Pro modes.
- `MapTree.dat`, `MapTreeOpenStatus.dat` and `TileSetData.dat` remain unmodeled.
- Archive custom extensions, keys, `.wolfx`, compression and repack are not
  exercised.
- No native writer, translation apply, or canonical v2 migration exists.

## 15. Reproducibility and repository hygiene

All committed tests use synthetic byte fixtures authored for GLT. The real WOLF
directory, official Auto export, shallow research clones and generated probe
report stay outside Git. The optional local experiment can be repeated with the
CLI above, but its machine-specific input paths must never be saved in project
configuration or committed documentation.
