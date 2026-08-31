# GLT 0.9.2 — MV Map-Based Plugin-Command Dispatch

GLT 0.9.2 extends the bounded RPG Maker MV plugin-command analyzer with one
generic dispatch shape. The analyzer does not execute JavaScript and does not
recognize plugins or commands by name.

## Supported evidence chain

A command is resolved only when the analyzer can prove the complete bounded
flow:

```text
command literal
→ local new Map() registry
→ deterministic Map.set registration
→ command-derived Map.get lookup
→ guarded computed method dispatch
→ argument-preserving forwarding
→ unique target method
→ existing display/internal sink classification
```

The supported registry form uses deterministic string keys and method names.
Keys may use fixed local string constants, fixed prefixes, and simple string
concatenation. Lookup normalization is limited to the command itself or a
bounded deterministic exact/upper/lower transform. The computed call must be
guarded by the resolved lookup result and must forward the original argument
array directly or through a bounded wrapper that preserves order and identity.

The final target is classified by the existing sink analysis. Map presence,
`Map.get()`, computed dispatch, an ASCII payload, or a command-like name alone
never makes a command verified or internal. Ambiguous evidence produces no new
observation and leaves the existing conservative classification in place.

## Safety bounds

- plugin source size and total source size retain their existing limits;
- local Map and registration counts have explicit caps;
- registration helpers and argument wrappers use restricted lexical shapes;
- command transforms, dispatch helpers, and sink helpers retain small depth
  limits and cycle guards;
- no JavaScript, `eval`, `Function()`, Node, or external JavaScript engine is
  executed;
- no unbounded runtime data-flow graph is constructed.

## Validation

Read-only validation on a real MV game resolved 14,053 plugin-command
occurrences that had previously remained conditional. Every occurrence was
classified `INTERNAL` through a unique registration, target, argument flow, and
internal sink. The translation entry count remained unchanged, and the source
game was unchanged. Synthetic positive and ambiguity regression cases use
unrelated names to confirm that the behavior is generic rather than
plugin-specific.

## Known limitations

The following remain unsupported and fall back conservatively:

- object or array dispatch tables;
- function-valued Map entries;
- multi-step or runtime-dependent registration;
- Map aliasing or passing the Map to external functions;
- complex or indirect guards;
- deeper unresolved helper chains;
- destructuring, spread, or argument-reordering forwarding;
- dynamic command keys or method values;
- duplicate or otherwise ambiguous registrations;
- minified, generated, or obfuscated patterns outside the bounded grammar.

This is a deterministic static-analysis capability for a narrow dispatch shape,
not universal MV plugin-command support. JSONL and artifact schema remain
version 1.
