# GLT 0.7.7 WOLF Project integration

## Scope

GLT 0.7.7은 WOLF RPG Editor의 공식 Text I/O export인 `Data_AutoTXT`를 기존
공통 ProjectManager에 연결한다. 별도 WOLF project schema나 별도 TranslationEntry를
만들지 않는다. 기존 RPG Maker Project, standalone `wolf-text-*` 명령, split/merge,
Glossary와 exact-match Translation Memory의 동작은 유지한다.

Project workflow가 지원하는 WOLF source mode는 현재 `auto_txt` 하나다. 네이티브
Editor project를 직접 입력받아 자동 export/import하는 mode는 이번 버전에 포함하지
않는다. 기존 `wolf-editor-validate`의 격리 Text I/O 검증 기능은 그대로 유지된다.

## Commands

```powershell
python glt.py project create <Data_AutoTXT> --engine wolf_rpg_editor --output <project>
python glt.py project qa <project> <Data_AutoTXT>
python glt.py project apply <project> <Data_AutoTXT> --output <translated_AutoTXT> --dry-run
python glt.py project apply <project> <Data_AutoTXT> --output <translated_AutoTXT>
```

유효한 `.Auto.txt` directory는 `--engine` 없이도 project source detector가 찾는다.
명시적 `--engine wolf_rpg_editor`는 사용자의 의도를 고정하고 잘못된 source를 즉시
거부한다.

## Common Project artifacts

```text
project/
├─ project.json
├─ source.jsonl
├─ translated.jsonl
├─ glossary.csv
├─ translation_memory.jsonl
├─ config/japanese_allowlist.txt
└─ reports/
   ├─ qa_report.json
   ├─ qa_issues.csv
   ├─ untranslated.csv
   ├─ project_manifest.json
   └─ dry_run_report.json
```

`source.jsonl`과 최초 `translated.jsonl`은 공통 serialization을 사용하며 모든
`translation`은 빈 문자열이다. WOLF의 `location`, source hash, command metadata는
기존 `TranslationEntry.extra_metadata`의 flattened fields로 보존된다. experimental
record는 JSONL에 넣지 않는다.

`project.json`은 schema/project version 1을 유지하고 선택적 `engine_metadata`
object만 추가한다. 새 필드는 없는 기존 Project도 그대로 load된다.

```json
{
  "project_version": 1,
  "schema_version": 1,
  "tool_version": "0.7.7",
  "engine": "wolf_rpg_editor",
  "game_fingerprint": "<sha256>",
  "engine_metadata": {
    "engine_id": "wolf_rpg_editor",
    "source_mode": "auto_txt",
    "wolf_location_schema": {"version": 1, "status": "provisional"},
    "source_fingerprint": "<sha256>",
    "source_file_count": 10,
    "encoding_observations": [
      {"encoding": "utf-8", "bom": "none", "newline_style": "CRLF", "file_count": 10}
    ],
    "editor_validation": {
      "status": "not_recorded_for_auto_txt_source",
      "version": null,
      "sha256": null
    }
  }
}
```

절대경로는 저장하지 않으며 extension metadata를 load할 때도 Windows/POSIX
absolute path를 거부한다.

## QA and apply routing

ProjectManager는 `project.json.engine`으로 adapter를 선택한다. WOLF adapter는 기존
`WolfTextQa`와 `WolfTextWriter`를 호출하고 결과를 공통 `QaResult`/`ApplyReport`로
변환한다. ProjectManager가 제공하는 locked Glossary와 inconsistent-translation
검사도 동일 record view에 적용된다.

다음 조건은 apply를 차단한다.

- Project engine 또는 source mode 불일치
- directory fingerprint 또는 row/file fingerprint 불일치
- duplicate/unknown/canonical location ID
- file/type/location/original mismatch
- control-like token 또는 variable-reference mismatch
- malformed JSONL, parser error, round-trip structure/data mismatch

빈 번역은 warning과 미번역 통계로 남고 원문을 유지한다. dry-run도 writer의 임시
복사·patch·재parse·fingerprint 검증을 수행하지만 사용자가 지정한 output directory는
만들지 않는다. 실제 apply는 기존 output을 덮어쓰지 않고 번역된 Auto.txt tree만 새로
생성한다. source tree는 QA, dry-run, apply 어느 경로에서도 수정하지 않는다.

## Verified and excluded scope

- verified: command code 101의 검증된 message slot
- verified: command code 102의 option literal
- verified: 명시적 system title/title-bar fields
- verified database allowlist: 공식 `DATANAME` marker field만
- excluded: choice cancel/default 의미, label-only message 후보, marker 없는
  `DATATYPE_n` string, asset path, unknown/experimental record

Choice cancel/default와 native `.dat`/`.mps` cross-route ID는 검증되지 않았으므로
`wolf:v1` location schema는 계속 `provisional`이다.

## Native Editor limitation

0.7.7 Project 명령은 native Editor project의 자동 `-txtoutput`/`-txtinput`을 수행하지
않는다. 특히 배포용 `.wolf`/`.wolfx`, native binary, Game executable을 번역했다고
주장하지 않는다. native integration은 Editor version별 정책, 명시적 import opt-in,
workspace lifetime과 실패 복구를 Project transaction에 결합해야 하므로 후속 범위로
남긴다.
