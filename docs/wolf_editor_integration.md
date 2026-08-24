# WOLF Official Editor integration validation (GLT 0.7.6)

## Current verification status

0.7.6 local validation에서 WOLF Editor 3.682 official sample의 격리 복사본을 사용한
`target=ALL` pipeline이 **VERIFIED**되었다. 실제 export/import/re-export, GLT no-op,
한국어 UTF-8 source/BOM/no-BOM, code 102 Choice option과 DATANAME `<<COMMA>>`를
검증했다. Choice cancel/default와 DATANAME 외 DB field, CP932→Korean은 여전히
미검증이다. Synthetic invoker는 계속 official evidence가 될 수 없다.

실제 수치와 hash는 [0.7.6 real Editor validation](wolf_editor_real_validation_0.7.6.md)에
개인 절대경로나 game text 없이 기록한다.

## Official CLI contract

공식 도움말이 문서화한 명령 형태를 그대로 사용한다.

```text
Editor.exe -txtoutput -txt_folder Data_AutoTXT -target ALL
Editor.exe -txtinput  -txt_folder Data_AutoTXT -target ALL
```

`target`은 `ALL`, `BASIC`, `MAP`만 허용한다. `txt_folder`는 Editor 기준 상대 폴더이며
`-wait`는 unattended validation에서 사용하지 않는다.

- 공식 Editor CLI 도움말: https://silversecond.com/WolfRPGEditor/Help/01control.html
- 공식 Editor option/Text I/O 설명: https://silversecond.com/WolfRPGEditor/Help/02editor_option.html
- 공식 3.50+ Text I/O release log: https://silversecond.com/WolfRPGEditor/old_releaselog/ReleaseLog07.html

## Availability and provenance

Editor locator는 다음 순서만 사용한다.

1. CLI의 explicit `--editor`
2. `GLT_WOLF_EDITOR` 환경변수
3. project root의 `Editor.exe` 또는 `EditorPro.exe`

시스템 전체 drive scan은 하지 않는다. 후보는 regular `.exe`, PE `MZ` signature,
recognized name 또는 adjacent WOLF project evidence를 조합한다. renamed executable은
explicit path와 project evidence가 필요하다. Version은 PE fixed file version을 읽을 수
있을 때만 기록하며 추측하지 않는다.

실제 subprocess invoker만 `self_generated_official_export` provenance를 부여한다.
Synthetic/emulated invoker는 report가 의미적으로 통과하더라도 official status를
`VERIFIED`로 만들 수 없다.

## Isolated execution and source protection

원본 project와 원본 Editor executable은 실행하거나 수정하지 않는다. 외부 integration
workspace에 project를 복사하고, 외부 Editor를 사용한 경우 Editor executable 및
top-level Editor/DLL runtime files만 복사한다. 모든 `txtoutput`/`txtinput`은 이 복사본을
working directory로 사용한다.

- `shell=False`와 argument list 사용
- stdout/stderr는 anonymous PIPE가 아닌 임시 파일로 capture하고 내용 대신 byte
  count/SHA-256만 기록
- exit code와 실행 시간 기록
- per-process timeout, retry 없음
- output folder 존재 여부 검사
- symlink input 거부
- original project before/after fingerprint 비교
- 성공 시 기본 cleanup, 실패 또는 `--keep-workspace` 시 forensic workspace 보존

`--allow-editor-import`가 없으면 `txtinput`은 절대 실행하지 않는다. Inspect mode는
Editor detection, isolated baseline `txtoutput`, `.Auto.txt` inspection/extraction,
GLT no-op writer까지만 수행한다.

## Opt-in validation sequence

```text
isolated native project
  -> official txtoutput (Auto A)
  -> direct no-op txtinput/re-export
  -> GLT no-op writer
  -> GLT output txtinput/re-export
  -> source encoding Korean trial
  -> UTF-8 BOM Korean trial
  -> UTF-8 no-BOM Korean trial
```

Direct 및 GLT no-op 비교는 file set, record/order, unknown records, sections, semantic text,
control tokens, encoding/BOM/newline/final newline, byte fingerprint를 각각 기록한다.
Editor normalization이 있으면 semantic equality와 byte equality를 구분한다.

## Korean and COMMA trials

각 trial은 verified extraction entry 중 최대 3개(dialogue/control, Choice, DATANAME)를
결정적으로 선택한다. 다른 수백/수천 entry는 빈 translation으로 유지한다. Control token
순서와 parameter는 0.7.4 QA로 그대로 보호한다. 일반 record에는
`GLT 0.7.6 한국어 왕복 테스트입니다.`, DATANAME에는 `검, 대형`을 사용하고 writer가
`검<<COMMA>> 대형`으로 source 표현하도록 한다.

Trial 결과는 `accepted`, `rejected`, `corrupted`, `normalized`, `unknown`으로 기록한다.
CP932 source-preserving writer가 한글을 encode하지 못하면 기존 정책대로 Editor 실행 전에
차단하며, 정책을 임의 완화하지 않는다. UTF-8 BOM/no-BOM trial은 별도 Auto.txt 복사본을
strict re-encode한 다음 다시 extract/QA/write한다. 실제 Editor re-export에서 정확한
한국어 semantic text, replacement character 부재, control/structure 일치를 확인한 경우에만
official Korean result가 `VERIFIED`가 된다.

## Choice, DB, and canonical ID

실제 export에서 code 102 선언 15개, option 43개, indent가 있는 nested option을
관찰했다. option literal 순서와 위치, branch control 비변경 및 Editor 재수출은
`PARTIALLY VERIFIED`되었고 code 102 option은 `VERIFIED_TRANSLATABLE`로 승격했다.
cancel/default 의미는 여전히 검증하지 않았다. DB allowlist는 `dataname`만 유지하고
description/help/display field를 추가하지 않았다. DB add/delete/reorder native
experiment도 `NOT RUN`이다.

Canonical ID는 `wolf:v1`, `schema_status=provisional`을 유지한다. Native `.dat`/`.mps`
cross-route identity가 검증되기 전에는 final/stable로 승격하지 않는다.

## Out of scope

Native parser/writer, `.wolf`/`.wolfx` decrypt/repack, packed game apply, executable patch,
ProjectManager integration, GUI와 다른 engine 지원은 0.7.5에 포함하지 않는다.
