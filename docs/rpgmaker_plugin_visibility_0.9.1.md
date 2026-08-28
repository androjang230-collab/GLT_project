# GLT 0.9.1 — Dynamic-Meta Tokenized Contracts

GLT 0.9.1 extends the conservative RPG Maker plugin-visible text pipeline with
one behavior-oriented pattern. It does not recognize a plugin by name and it
never executes plugin JavaScript.

```text
dynamic meta source
→ parse / transform
→ bounded property-state propagation
→ delayed display sink
```

The feature builds on the 0.9.0 active inventory, semantic roles, exact storage
binding, preflight, staging, structural comparison, and atomic write safety.
JSONL and artifact schema remain version 1.

## Deterministic dynamic keys

A dynamic access such as `object.meta[key]` is eligible only when `key` has one
deterministic value under bounded lexical analysis. Supported evidence includes:

- a local string literal;
- a simple alias of a fixed literal;
- a fixed scalar plugin parameter;
- a bounded helper whose only result is a fixed literal;
- a function parameter supplied by one fixed literal argument.

Runtime input, unresolved concatenation, array lookup, loop-dependent values,
callback parameters, arbitrary function results, and generated keys remain
`UNKNOWN`. Findings record the resolved key, resolution path, transforms,
property-state evidence, helper chain, sink, and classification reason.

## Bounded property and delayed-render flow

The analyzer follows simple direct property assignment and read operations. It
allows at most two property-state writes and retains the existing small helper
depth bound. A value may therefore be parsed during setup, stored on an object,
read during a later refresh/update method, and reach a recognized text sink.

Multiple or unresolved writers, ambiguous receiver aliases, callbacks,
closures, recursion, asynchronous mutation, dynamic arrays/maps, and arbitrary
object graphs are not verified. Mixed display and control use does not become
translatable text.

## `tokenized_visible_segment`

The contract is created only when all of the following are deterministic:

- dynamic meta key resolution;
- actual writable data JSON `note` field;
- unique source substring;
- literal token delimiter and segment index;
- `TRANSLATABLE_TEXT` semantic role;
- bounded delayed display path;
- reconstruction grammar.

Only the segment reaching the text sink is translated. For a conceptual value
such as:

```text
<tag:Visible Text|12|black>
```

where the first `|`-delimited segment is displayed, `Visible Text` is the
translation unit. The tag syntax, delimiter, numeric formatting value, and
color/config token are preserved. A duplicate meta key in the same note is
audit-only because the derived meta value cannot be bound to one unique source
span conservatively.

## Safe reconstruction

Extraction records the storage identity, JSON path, source and grammar
fingerprints, parser evidence, segment ordinal/span, whitespace policy, and
contract ID. Apply performs a fresh extraction and re-resolves every
precondition before planning a write.

Only the approved segment is replaced. Other tags, note content, formatting and
control tokens, object fields, and files are unchanged semantically. Multiple
edits in a JSON file are aggregated using position-safe replacement. Overlap or
changed grammar blocks the affected edit. Dry-run uses the same validation, and
an already-applied value is reported as `NO_CHANGE` without a write plan.

## Validation

Release validation used a completed localized MV game only as a read-only
sample:

- 43 tokenized entries extracted;
- 43/43 full preflight applicable;
- eight representative entries applied on a disposable copy across seven maps;
- only eight approved note paths changed semantically;
- all 43 entries re-extracted with identical ID and storage-identity sets;
- eight already-applied entries produced `NO_CHANGE` and no write plan;
- all source-game files remained byte-identical.

A separately named synthetic plugin, tag, variable set, and property set follow
the same analysis and contract path. The implementation contains no plugin-name
or tag-name exception.

## Known limitations

The following remain unsupported or audit-only:

- unresolved dynamic concatenation or complex runtime-computed meta keys;
- callbacks, asynchronous flow, and closures;
- arbitrary object graphs and prototype mutation;
- ambiguous receiver aliasing;
- multiple unresolved writers;
- ambiguous duplicate meta source binding;
- arrays or maps with dynamic keys;
- recursion and unbounded helper propagation;
- minified, generated, or obfuscated JavaScript;
- any flow whose real writable source or visible segment cannot be proven.

This is a bounded behavioral contract, not universal RPG Maker plugin support.
