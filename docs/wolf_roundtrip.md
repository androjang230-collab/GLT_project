# WOLF translation QA and lossless Auto.txt writer (GLT 0.7.4)

## Scope and evidence

0.7.4의 write 대상은 WOLF RPG Editor의 Text I/O가 출력한 `.Auto.txt` 복사본뿐이다.
`Editor.exe -txtinput`, native `.dat`/`.mps`, 암호화 archive와 실행 파일은 실행하거나
수정하지 않는다. 공식 도움말은 Text I/O의 `-txtoutput`/`-txtinput` 대상과 동작을
설명하고, 공식 3.50 release log는 DATANAME marker 및 `<<COMMA>>` 처리를 명시한다.

- 공식 CLI 도움말: https://silversecond.com/WolfRPGEditor/Help/01control.html
- 공식 editor option/Text I/O 도움말: https://silversecond.com/WolfRPGEditor/Help/02editor_option.html
- 공식 3.50 release log: https://silversecond.com/WolfRPGEditor/old_releaselog/ReleaseLog07.html
- 공식 special-word 문법: https://silversecond.com/WolfRPGEditor/Help/06specialword.html
- 공식 database 설명: https://silversecond.com/WolfRPGEditor/Help/04ev_db.html

Fixture provenance 값은 `synthetic`, `official_export`,
`self_generated_official_export`, `public_repository_observed`를 구분한다. 0.7.6에서
local official Editor 3.682 sample을 격리 실행했지만 실제 game binary/text는
repository에 포함하지 않는다. 환경 변수
`GLT_WOLF_AUTOTXT_FIXTURE`로 사용자가 제공한 official export에 대해 선택적 read-only
integration regression을 실행할 수 있다.

## Choice and database decision

실제 3.682 export에서 code 102 declaration/option order, code 401 branch, code 499 end와
nested indent를 관찰했다. option literal은 GLT writer와 official import/re-export를
통과해 `VERIFIED_TRANSLATABLE`로 승격했다. branch/end record는 추출하거나 수정하지
않으며 cancel/default parameter 의미는 계속 미검증이다.

DB writer allowlist는 `VERIFIED_DATABASE_TEXT_FIELDS = {"dataname"}`이다. 공식 marker
`<<!--DATANAME--!>>` 뒤의 player-visible name만 verified이다. `DATATYPE_n >= 2000`
문자열은 구조가 관찰되었더라도 field 의미가 확정되지 않아 experimental이다.
description/help/display text, asset path, filename, identifier, tag와 opaque metadata는
allowlist에 없다.

DB canonical location의 `record_id`는 CSV 구조 순서이므로 insert/delete/reorder에
취약하다. 따라서 `wolf:v1`과 `schema_status=provisional`을 유지한다. 기존 ID 계산은
조용히 변경하지 않았다. native parser와 Text I/O parser가 동일 record를 가리킨다는
cross-route 검증 후에만 v2/final을 검토한다.

## QA policy

`wolf-text-qa`와 writer preflight는 같은 `WolfTextQa`를 사용한다.

- blocker: malformed JSONL/structural metadata, duplicate/colliding/unknown ID,
  canonical/file/type/location/original mismatch, source/file fingerprint mismatch
- error: verified control-like token이나 variable reference의 type, parameter,
  multiplicity 또는 order 변경
- warning: empty/whitespace-only translation, legacy JSONL의 fingerprint 부재,
  source parser warning

Control token은 WOLF parser가 관찰한 `\name[...]`/`\name` 계열만 순서 있는 tuple로
비교한다. `\v`, `\variable`, `\s`, `\self`, `\cself`, `\udb`, `\cdb`, `\sdb`,
`\sys` 계열은 variable-reference mismatch를 별도로 보고한다. 의미가 확인되지 않은
escape를 RPG Maker 규칙으로 정규화하지 않는다. DATANAME 안의 쉼표는 source write
단계에서 `<<COMMA>>`로 표현하며, 이 transport marker 자체를 번역문의 의미 token
추가/삭제 오류로 취급하지 않는다.

0.7.3 JSONL처럼 fingerprint가 없는 파일은 호환을 위해 warning으로 허용하지만,
0.7.4 extraction은 절대경로 없는 directory SHA-256과 각 source file SHA-256을
entry/report에 기록한다.

## Source-oriented writer and round-trip checks

Writer는 source tree 전체를 임시 디렉터리에 byte copy한 뒤 승인된 physical line의
정확한 field span만 바꾼다. system key-value의 value, verified event raw literal,
DATANAME CSV cell 이외는 writer allowlist 밖이다.

- strict source encoding을 재사용하고 replacement character를 쓰지 않는다.
- UTF-8/UTF-16 BOM을 그대로 유지한다.
- `splitlines(keepends=True)`로 CRLF/LF/CR/mixed line endings와 final newline을 보존한다.
- leading/trailing ASCII/full-width spaces, tab, backslash, raw `\n` representation을 유지한다.
- event literal의 실제 quote는 source syntax용 `\"`로 escape한다.
- DATANAME comma는 `<<COMMA>>`, CSV quote는 doubled quote로 source 표현한다.
- 실제 CR/LF가 들어간 translation은 source line 구조를 깨므로 blocker이다.
- source encoding으로 표현할 수 없는 translation도 blocker이다.

패치 후 임시 tree를 다시 inspect한다. 모든 canonical record ID, 비대상 record value,
unknown/unsupported raw record, encoding/BOM/newline/final-newline metadata가 원본과
같고 대상 value만 기대 번역과 같은지 검증한다. 실패하면 output directory를 노출하지
않는다. no-op은 전체 파일 byte copy이므로 A와 B가 byte-for-byte 동일하다.

`--dry-run`도 동일한 임시 patch와 round-trip 검증을 수행하지만 지정한 output
directory를 만들지 않는다. report에는 source/output prospective fingerprint,
translated/applicable/applied/skipped, modified/untouched files 및 모든 issue가 들어간다.

## Known limitations

- code 102 option과 nested 구조는 부분 검증했지만 cancel/default 의미는 미검증이다.
- DATANAME 외 DB field는 writer 대상이 아니다.
- canonical v1 DB row ordinal은 reorder에 안정적이지 않다.
- 실제 newline을 포함하는 번역은 multiline source syntax가 확인될 때까지 차단한다.
- Editor import와 native/archive mutation은 0.7.4 범위 밖이다.
