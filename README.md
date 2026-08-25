# Game Localization Toolkit (GLT)

Windows 11에서 일본 동인게임의 번역 가능한 문자열을 안전하게 다루기 위한
개인용 Python 도구입니다. 현재 버전은 **GLT 0.8.5**이며 RPG Maker MV/MZ의
엔진 감지, UTF-8 JSONL 추출, 별도 폴더 안전 적용, 독립 QA, dry-run,
fingerprint와 이식 가능한 번역 Project, 사용자 Glossary 및 JSONL Translation
Memory, 폰트 진단과 안전한 기본 폰트 패치를 구현합니다. 대용량 번역 JSONL은
독립 Split/Merge 유틸리티로 나누고 무결성을 검증하며 다시 합칠 수 있습니다.
WOLF RPG Editor는 자동 감지, archive 조사, 공식 `.Auto.txt`의 구조 분석·추출·QA·
안전 적용과 격리된 official Editor Text I/O validation을 지원합니다.

## 엔진 지원 상태

| Engine | Detect | Inspect | Extract | QA / Apply | Font |
| --- | --- | --- | --- | --- | --- |
| RPG Maker MV/MZ | Yes | Not yet | Yes | Yes | Yes |
| WOLF RPG Editor | Experimental | Yes (`.Auto.txt`) | Yes (`.Auto.txt`) | Yes (`.Auto.txt`) | Not yet |

WOLF native archive 해제·복호화·binary 직접 수정은 수행하지 않습니다. 공식 Editor
`-txtinput`은 명시적 opt-in validation에서만 원본 밖 격리 복사본에 실행됩니다.

## 설계 원칙

- 감지와 추출 과정에서 게임 원본 파일을 수정하지 않습니다.
- 번역 적용은 전체 게임을 새 output으로 복사한 뒤 staging 폴더에서 수행합니다.
- 경로는 실행 시 전달하며 특정 PC의 절대경로를 설정에 저장하지 않습니다.
- 출력되는 근거 경로는 게임 디렉터리 기준 상대경로입니다.
- 엔진별 구현은 `EnginePlugin` 인터페이스 뒤에 분리되어 있습니다.
- JSONL의 위치 기반 ID는 원문이 바뀌어도 동일한 위치에서 유지됩니다.
- TTF/OTF/WOFF cmap 검사는 표준 라이브러리 fallback을 제공하며, WOFF2 검사는
  `fontTools`를 사용합니다.

## 요구 환경

- Windows 11
- Python 3.11 이상 권장

## 사용법

저장소 루트에서 다음 명령을 실행합니다.

```powershell
python glt.py detect "D:\Games\SampleGame"
```

현재 디렉터리 기준 상대경로도 사용할 수 있습니다.

```powershell
python glt.py detect ".\games\SampleGame"
```

상세 진단 로그가 필요하면 전역 옵션을 명령 앞에 둡니다.

```powershell
python glt.py --verbose detect ".\games\SampleGame"
```

텍스트를 추출하려면 다음 명령을 사용합니다.

```powershell
python glt.py extract "D:\Games\SampleGame" --output ".\work\source.jsonl"
```

`--output`을 생략하면 현재 작업 폴더에 `source.jsonl`을 생성합니다.

번역된 JSONL을 새 게임 폴더에 적용하려면 다음 명령을 사용합니다.

```powershell
python glt.py apply "D:\Games\TestGame" ".\translated.jsonl" --output "D:\Games\TestGame_KR"
```

apply output은 반드시 존재하지 않는 새 경로여야 합니다. 원본과 같은 경로,
원본 내부 경로, 원본을 포함하는 상위 경로 및 기존 output은 거부합니다.

게임을 수정하거나 output을 만들지 않고 번역 프로젝트를 검사할 수 있습니다.

```powershell
python glt.py qa "D:\Games\TestGame" ".\translated.jsonl"
```

기본 보고서 경로는 현재 작업 폴더의 `reports`입니다. 다른 위치는
`--reports`로 지정합니다.

```powershell
python glt.py apply "D:\Games\TestGame" ".\translated.jsonl" `
  --output "D:\Games\TestGame_KR" --dry-run
```

dry-run은 실제 apply와 같은 preflight 코드를 사용하지만 output을 생성하거나
원본을 수정하지 않습니다. 변경 예정 파일과 ID는 CLI 및
`reports/dry_run_report.json`에 기록됩니다.

## Phase 5 Project 사용 흐름

게임별 번역 작업 폴더는 다음 명령으로 생성합니다.

```powershell
python glt.py project create "D:\Games\Game\www" --output "D:\Localization\Game_KR"
```

생성된 `translated.jsonl`과 선택적으로 Glossary/TM을 편집한 뒤 다음 순서로
사용할 수 있습니다.

```powershell
python glt.py project tm-fill "D:\Localization\Game_KR"
python glt.py project qa "D:\Localization\Game_KR" "D:\Games\Game\www"
python glt.py project apply "D:\Localization\Game_KR" "D:\Games\Game\www" `
  --output "D:\Games\Game_KR" --dry-run
python glt.py project apply "D:\Localization\Game_KR" "D:\Games\Game\www" `
  --output "D:\Games\Game_KR"
python glt.py project tm-update "D:\Localization\Game_KR"
```

Project 명령은 `project.json`의 engine/schema/fingerprint와 현재 게임을 먼저
비교합니다. fingerprint가 다르면 `GAME_FINGERPRINT_MISMATCH` conflict로
apply를 차단합니다. `project apply --dry-run`은 Project의 `reports`에 보고서를
기록하지만 게임 output은 만들지 않습니다. 기존 standalone `detect`,
`extract`, `qa`, `apply` 명령도 그대로 지원합니다.

폰트 기능은 번역 apply와 독립적으로 실행합니다.

```powershell
python glt.py font-check "D:\Games\Game\www"
python glt.py project font-check "D:\Localization\Game_KR" "D:\Games\Game\www"

python glt.py font-patch "D:\Games\Game\www" `
  --font "D:\Fonts\KoreanFont.ttf" --output "D:\Games\Game_FontPatched"

python glt.py project font-patch "D:\Localization\Game_KR" "D:\Games\Game\www" `
  --font "D:\Fonts\KoreanFont.ttf" --output "D:\Games\Game_FontPatched" --dry-run
```

Project 폰트 명령은 project/schema/engine 호환성을 확인하지만, 번역 apply 후의
게임에도 독립적으로 사용할 수 있도록 원본 game fingerprint 일치를 강제하지
않습니다. 기존 `project qa`와 `project apply`의 fingerprint 차단 정책은 그대로
유지됩니다.

출력 예:

```text
Detected Engine: RPG Maker MZ
Confidence: 99%

Evidence:
- js/rmmz_core.js
- data/System.json
- data/Map001.json
- data/Actors.json
- js/plugins.js
- index.html
```

종료 코드는 성공 시 `0`, 알 수 없는 엔진이면 `1`, 경로·출력 오류면 `2`,
일부 파일을 건너뛰고 추출을 완료했거나 apply에 error/conflict가 있는 경우
`3`입니다.

## 감지 기준

RPG Maker MZ는 `js/rmmz_core.js`, MV는 `js/rpg_core.js`를 고유 증거로
사용합니다. 두 엔진 모두 `data/System.json`이 함께 있어야 확정합니다.
`data/Map*.json`, `data/Actors.json`, `js/plugins.js`, `index.html`은 신뢰도를
보강합니다. 고유 코어 파일 하나만으로는 엔진을 확정하지 않습니다.

## Phase 2 추출 대상

이벤트 명령은 맵, 공통 이벤트 및 전투 이벤트 페이지에서 다음 코드를
처리합니다.

- `101` Show Text 헤더의 MZ speaker/name box
- `401` Show Text 본문
- `102` Show Choices의 개별 선택지
- `405` Scroll Text 본문
- `320` Change Actor Name의 새 이름
- `324` Change Nickname의 새 별명
- `325` Change Profile의 새 프로필

DB 파일은 다음 필드만 명시적으로 처리합니다.

- `Actors.json`: `name`, `nickname`, `profile`
- `Classes.json`: `name`
- `Items.json`, `Weapons.json`, `Armors.json`: `name`, `description`
- `Skills.json`: `name`, `description`, `message1`, `message2`
- `States.json`: `name`, `message1`~`message4`
- `Enemies.json`: `name`
- `System.json`: `gameTitle`, `currencyUnit`, 표시용 타입 배열, `terms`
- `MapXXX.json`: `displayName`과 지원 이벤트 명령
- `CommonEvents.json`, `Troops.json`: 지원 이벤트 명령

이미지·오디오 이름, `note`, JavaScript/스크립트 명령, 플러그인 명령,
이벤트 내부 이름, 공통 이벤트/전투 그룹 내부 이름, System의 `switches`와
`variables`는 추출하지 않습니다.

## JSONL 형식

필수 필드 외에 정확한 위치와 제어코드 메타데이터를 기록합니다.

```json
{"id":"Map003:event17:page1:cmd401:index1:param0","engine":"rpgmaker_mz","file":"data/Map003.json","type":"dialogue","original":"\\C[2]何をしているの？","translation":"","speaker":"セリカ","json_path":"$.events[1].pages[0].list[1].parameters[0]","event_id":17,"page_id":1,"command_index":1,"parameter_index":0,"map_id":3,"map_name":"王都","control_codes":["\\C[2]"]}
```

이벤트 ID에는 명령 코드와 0부터 시작하는 실제 command 배열 인덱스가 모두
포함됩니다. 페이지 ID는 RPG Maker 편집기 표시에 맞춰 1부터 시작하며,
`json_path`에는 실제 0 기반 배열 위치가 기록됩니다. 제어코드는 원문에서
발견된 순서와 개수를 유지합니다. `MapInfos.json`에 해당 map 정보가 있으면
`map_id`와 원본 `map_name`을 선택적으로 추가하며, 이름을 추측하거나 번역하지
않습니다.

## Phase 3 안전 적용

apply는 다음 순서로 동작합니다.

1. 엔진, 입력 파일, output 경로 관계 및 기존 output 여부를 검사합니다.
2. Phase 2 추출기를 원본에 다시 실행하여 승인된 ID/location 카탈로그를 만듭니다.
3. JSONL의 중복 ID, engine, file, ID, `json_path`, `original`을 교차 검증합니다.
4. 원본 전체를 임시 staging 폴더에 복사하고 모든 파일의 SHA-256을 비교합니다.
5. 비어 있지 않은 번역 중 제어코드 검사를 통과한 항목만 정확한 path에 적용합니다.
6. object key, array 길이, 데이터 타입과 비허용 값을 재귀 비교합니다.
7. JSON을 같은 폴더의 임시 파일에 기록·재파싱·재검증한 뒤 원자적으로 교체합니다.
8. 비수정 파일의 SHA-256을 다시 확인하고 최종 output으로 이동합니다.

허용되는 의미적 변경은 검증된 번역 문자열 위치뿐입니다. `translation`이
비어 있거나 공백뿐이면 원문을 유지합니다.

제어코드는 종류와 값을 포함한 multiset으로 비교합니다. 따라서 누락, 추가,
값 변경과 동일 코드의 중복 개수 변경은 모두 `CONTROL_CODE_MISMATCH`이며 해당
항목을 적용하지 않습니다. 번역문에 히라가나 또는 가타카나가 남으면
`JAPANESE_TEXT_REMAINS` warning으로 보고하지만 적용은 허용합니다.

## Apply 보고서

요약은 CLI에 표시되며 다음 경로에 UTF-8 JSON으로 저장됩니다.

```text
<output_directory>/reports/apply_report.json
```

보고서에는 복사 파일 수, 수정 JSON 목록, 전체/번역/미번역/적용 수, warning,
error, conflict 및 항목별 `id`, `file`, `json_path`, 원문, 번역문, 사유가
포함됩니다. 대표 오류 코드는 다음과 같습니다.

- `DUPLICATE_ID`
- `UNKNOWN_ID`
- `INVALID_JSON_PATH`
- `LOCATION_MISMATCH`
- `SOURCE_TEXT_MISMATCH`
- `CONTROL_CODE_MISMATCH`
- `UNEXPECTED_DATA_CHANGE`

## Phase 4 독립 QA

QA는 중복·미존재 ID, engine/file/type/path/location mismatch, source mismatch,
빈 번역, malformed JSON, 제어코드 오류와 일본어 잔존을 검사합니다. 종료 코드는
warning/info만 있으면 `0`, 실행 불가능하면 `2`, error/conflict가 있으면
`3`입니다.

다음 artifact를 atomic write로 생성합니다.

```text
reports/qa_report.json
reports/qa_issues.csv
reports/untranslated.csv
reports/project_manifest.json
```

`qa_report.json`에는 전체·번역·미번역·완료율, warning/error/conflict,
Hiragana/Katakana/CJK 통계, 제어코드 오류와 source mismatch 수가 포함됩니다.
issue 행은 `id`, `file`, `json_path`, `type`, 원문, 번역문, severity,
`issue_code`, 사유를 포함합니다.

일본어 검사는 다음 정책을 사용합니다.

- Hiragana 또는 Katakana: `JAPANESE_TEXT_REMAINS` warning
- CJK Kanji만 존재: `CJK_KANJI_REMAINS` info
- `config/japanese_allowlist.txt`: 주석·빈 줄을 제외한 리터럴 substring allowlist

allowlist는 `--allowlist <file>`로 프로젝트별 파일을 지정할 수 있습니다.
Phase 5 Project QA는 같은 보고서 구조에 fingerprint, Glossary 및 동일 원문
번역 일관성 issue를 추가합니다.

## Fingerprint와 manifest

game fingerprint는 감지 엔진, Phase 2 번역 대상 JSON, 선택적
`data/MapInfos.json`의 상대경로와 파일별 SHA-256을 정렬하여 다시 SHA-256으로
계산합니다. 절대경로는 사용하지 않으므로 다른 PC나 드라이브에 동일 파일을
복사해도 값이 같습니다.

`project_manifest.json`에는 다음 정보가 저장됩니다.

- `tool_version`
- `schema_version`
- `engine`
- `game_fingerprint`
- 전체 및 fingerprint 대상 파일 수
- fingerprint 파일별 상대경로와 SHA-256
- 번역 항목 수
- manifest 생성 시각
- allowlist 항목 수

기존 Phase 2 JSONL과 호환하기 위해 각 행에는 새 필드를 강제하지 않고,
현재 artifact schema version `1`은 보고서와 manifest에 기록합니다.

## Phase 5 Project 구조와 project.json

`project create`는 존재하지 않는 새 디렉터리에 다음 구조를 atomic하게
생성합니다. 게임 폴더 내부나 게임을 포함하는 상위 경로는 거부합니다.

```text
Game_KR/
├─ project.json
├─ source.jsonl
├─ translated.jsonl
├─ glossary.csv
├─ translation_memory.jsonl
├─ config/
│  └─ japanese_allowlist.txt
└─ reports/
```

`project.json`은 프로젝트 루트 기준 상대경로만 저장합니다.

```json
{
  "project_version": 1,
  "schema_version": 1,
  "tool_version": "0.7.5",
  "engine": "rpgmaker_mz",
  "game_fingerprint": "<sha256>",
  "source_file": "source.jsonl",
  "translation_file": "translated.jsonl",
  "glossary_file": "glossary.csv",
  "translation_memory_file": "translation_memory.jsonl",
  "allowlist_file": "config/japanese_allowlist.txt"
}
```

따라서 프로젝트 폴더와 동일 게임을 다른 PC나 드라이브로 옮겨도 절대경로를
고칠 필요가 없습니다. 생성 시 `source.jsonl`과 빈 번역 상태의
`translated.jsonl`을 함께 만들고, Glossary는 헤더만, TM과 allowlist는 완전히
빈 파일로 만듭니다. 예시 용어나 기본 번역 사전은 자동 등록하지 않습니다.

## Glossary 검사

`glossary.csv` 형식은 `source,target,type,locked`입니다. `type`은 자유 문자열이며
등록된 행만 단순 literal substring 방식으로 검사합니다.

- `locked=true`: 원문에 `source`가 있는데 비어 있지 않은 번역에 `target`이
  없으면 `GLOSSARY_MISMATCH` warning을 기록합니다.
- `locked=false`: 참고용 metadata이며 번역을 강제하거나 QA를 실패시키지
  않습니다.
- 미등록 용어를 추측하지 않으며 번역문을 자동 수정하지 않습니다.

동일한 `original`에 서로 다른 비어 있지 않은 번역이 있으면 문맥 차이를
허용하기 위해 `INCONSISTENT_TRANSLATION` warning만 기록합니다.

## Translation Memory

`translation_memory.jsonl`은 Git/동기화에 적합한 UTF-8 JSONL이며 각 행은
`original`, `translation`, `type`, `approved`를 갖습니다. `speaker`, `notes`는
선택 필드입니다.

- `project tm-fill`: `approved=true`이고 original이 정확히 같은 TM만 사용해
  빈 번역을 채웁니다. 이미 번역된 항목은 덮어쓰지 않으며 fuzzy matching을
  하지 않습니다.
- `project tm-update`: 완료된 번역을 `approved=true`로 추가합니다. 같은
  original+translation 조합은 중복 저장하지 않습니다.
- 동일 original에 다른 번역이 있으면 자동 선택하거나 덮어쓰지 않고
  `TM_TRANSLATION_CONFLICT`를 반환합니다.

## Translation JSONL Split / Merge

이 기능은 GLT 엔진 처리와 분리된 보조 유틸리티입니다. 게임 파일, Project의
`source.jsonl`과 기존 `translated.jsonl`을 수정하지 않으며 새 작업 디렉터리와
새 병합 파일만 생성합니다.

기본 Split은 각 entry의 `file` 값을 기준으로 그룹화합니다.

```powershell
python tools\split_translation.py ".\translated.jsonl" `
  --output ".\translation_work"

# 같은 동작을 명시적으로 선택
python tools\split_translation.py ".\translated.jsonl" `
  --by-file --output ".\translation_work"
```

`data/Map001.json`은 `Map001.jsonl`처럼 안전한 basename으로 변환됩니다. 경로
separator와 특수문자는 제거되며, 이름이 충돌하면 source `file` 값의 SHA-256
일부를 붙입니다. 기존 output 디렉터리는 삭제하거나 덮어쓰지 않고 거부합니다.

한 그룹이 큰 경우 JSONL entry 수를 기준으로 추가 분할합니다. JSON object
내부를 나누지 않습니다.

```powershell
python tools\split_translation.py ".\translated.jsonl" `
  --by-file --max-lines 1000 --output ".\translation_work"
```

위 예에서 큰 맵 그룹은 `Map015_part001.jsonl`,
`Map015_part002.jsonl`처럼 생성됩니다. `file` 그룹과 무관한 일반 분할은 다음과
같습니다.

```powershell
python tools\split_translation.py ".\translated.jsonl" `
  --lines 1000 --output ".\translation_work"
```

`--by-file`과 `--lines`, 또는 `--lines`와 `--max-lines` 조합은 의미가 충돌하므로
거부합니다. Split은 원본 row의 내용과 각 그룹 내부 순서를 그대로 기록하며
`id`, `original`, `translation` 또는 metadata를 고치지 않습니다.

### Split manifest

`translation_work/split_manifest.json`은 절대경로 없이 다음 정보를 기록합니다.

```json
{
  "format_version": 1,
  "source_filename": "translated.jsonl",
  "source_sha256": "<sha256>",
  "total_entries": 33815,
  "split_mode": "by_file",
  "max_lines": 1000,
  "lines": null,
  "parts": [
    {
      "filename": "Map001.jsonl",
      "entry_count": 542,
      "first_id": "<first-id>",
      "last_id": "<last-id>",
      "source_file": "data/Map001.json"
    }
  ]
}
```

Merge는 canonical `--source`의 ID 순서를 사용하므로 작업 중 part row 순서가
바뀌어도 원래 순서로 복원합니다. 최종 row는 source entry를 복사한 뒤 검증된
`translation` 값만 반영합니다.

```powershell
python tools\merge_translation.py ".\translation_work" `
  --source ".\translated.jsonl" `
  --output ".\translated_merged.jsonl" `
  --dry-run

python tools\merge_translation.py ".\translation_work" `
  --source ".\translated.jsonl" `
  --output ".\translated_merged.jsonl"
```

Merge는 manifest의 source filename·SHA-256·entry 수·예상 part·각 part entry
수를 검증합니다. 모든 source metadata(`control_codes`와 선택 필드 포함)는
`translation`을 제외하고 정확히 같아야 합니다. `DUPLICATE_ID`, `UNKNOWN_ID`,
`MISSING_ID`, `METADATA_MISMATCH`, `MALFORMED_JSONL`,
`SOURCE_JSONL_MISMATCH` 중 하나라도 있으면 output을 만들지 않습니다. 빈 번역은
정상적인 미번역 상태로 허용합니다.

`--dry-run`은 동일한 검증과 통계를 실행하고 `translation_work/merge_report.json`
만 갱신합니다. 정상 Merge는 임시 파일을 flush한 뒤 원자적으로 새 output을
게시하며, 이미 존재하는 output은 덮어쓰지 않습니다. 보고서 예시는 다음과
같습니다.

```json
{
  "source_entries": 33815,
  "part_entries": 33815,
  "merged_entries": 33815,
  "translated_entries": 9200,
  "untranslated_entries": 24615,
  "duplicate_ids": 0,
  "unknown_ids": 0,
  "missing_ids": 0,
  "metadata_mismatches": 0,
  "errors": 0,
  "status": "success",
  "dry_run": false,
  "issues": []
}
```

권장 작업 흐름은 다음과 같습니다.

```text
project create
    -> translated.jsonl
    -> split_translation.py
    -> translation_work/*.jsonl의 translation 필드만 번역
    -> merge_translation.py --dry-run
    -> merge_translation.py
    -> translated_merged.jsonl
    -> project qa
    -> 정상 확인 후 사용자가 translated.jsonl로 교체
    -> project apply
```

Split/Merge는 실제 번역문 안의 RPG Maker 제어코드를 수정하거나 최종 검증하지
않습니다. 병합 후 `project qa`가 그 검사를 담당합니다.

## Phase 5.5 Font Check

`font-check`는 원본을 수정하지 않고 다음 위치에서 실제로 존재하는 파일만
분석합니다.

- `fonts/`, `css/`, `index.html`
- `js/*.js`, `js/plugins/*.js`, `js/plugins.js`
- MV의 `fonts/gamefont.css`
- MZ의 `data/System.json` `advanced.mainFontFilename`

`.ttf`, `.otf`, `.woff`, `.woff2` 파일과 CSS `@font-face`, `font-family`,
폰트 URL, JavaScript `fontFace`/`fontFamily`, 플러그인 참조 및 외부·누락 참조를
기록합니다. 파일명으로 한글 지원을 추측하지 않고 Unicode cmap에서 다음 영역을
직접 검사합니다.

- Hangul Syllables (`U+AC00–U+D7A3`)
- Hangul Jamo (`U+1100–U+11FF`)
- Hangul Compatibility Jamo (`U+3130–U+318F`)

Hangul Syllables 전체 11,172자를 기준으로 실제 coverage를 계산합니다.

- `FULL`: 11,172자, 100%
- `PARTIAL`: 1~11,171자
- `NONE`: 0자

보고서에는 상태와 함께 `hangul_syllables_count`, `hangul_syllables_total`,
`hangul_coverage_percent`를 기록합니다. 두 Jamo 영역은 별도 boolean/count로
보고하며 전체 coverage 비율에는 포함하지 않습니다.

TTF/OTF/WOFF는 내장 cmap parser로 검사할 수 있습니다. WOFF2는 선택적
`fontTools`가 없으면 `FONT_PARSE_FAILED`로 명확히 보고합니다. 기본 보고서는
`reports/font_report.json`에 저장되며 Project 명령은 Project의 `reports/`를
사용합니다.

주요 issue는 다음과 같습니다.

- INFO: `DEFAULT_FONT_FULL_HANGUL_COVERAGE`,
  `HANGUL_COVERAGE_FALLBACK_FONT_FOUND`
- WARNING: `DEFAULT_FONT_PARTIAL_HANGUL_COVERAGE`,
  `DEFAULT_FONT_NO_HANGUL_COVERAGE`, `MULTIPLE_FONTS_USED`,
  `PLUGIN_FONT_REFERENCE`, `EXTERNAL_FONT_REFERENCE`
- ERROR: `MISSING_FONT_REFERENCE`, `FONT_PARSE_FAILED`

## Phase 5.5 Font Patch

`font-patch`는 사용자가 지정한 `.ttf` 또는 `.otf`만 사용합니다. 기본 폰트를
포함하거나 다운로드하지 않으며 라이선스를 판정하지 않습니다.

1. 폰트 signature와 cmap을 파싱하고 11,172자 대비 coverage를 계산합니다.
2. output 경로와 기존 파일 충돌을 검사합니다.
3. 원본 전체를 staging output으로 복사하고 SHA-256을 비교합니다.
4. 사용자 폰트를 `fonts/`로 복사합니다. 이름이 충돌하면 `-glt` 이름을
   사용하며 기존 파일을 덮어쓰지 않습니다.
5. MV는 유일한 `GameFont` face의 URL 값만 최소 변경합니다.
6. MZ는 `System.json`의 명확한 `advanced.mainFontFilename`을 우선 사용하고,
   그렇지 않으면 유일한 GameFont CSS만 수정합니다.
7. 예상한 CSS/JSON 한 곳과 새 폰트 외 변경이 없는지 검증하고 atomic하게
   output으로 이동합니다.

Patch coverage 정책은 다음과 같습니다.

- `FULL` 100%: 적용 허용
- `PARTIAL` 95% 이상: `PATCH_FONT_PARTIAL_HANGUL_COVERAGE` warning 후 허용
- `PARTIAL` 95% 미만: `PATCH_FONT_INSUFFICIENT_HANGUL_COVERAGE` error로 차단
- `NONE`: `PATCH_FONT_NO_HANGUL_COVERAGE` error로 차단

95% 임계값은 보고서의 `minimum_patch_hangul_coverage_percent`에도 기록됩니다.

대상이 여러 개이거나 구조가 불명확하면 자동 수정하지 않고
`MANUAL_FONT_REFERENCE_REVIEW_REQUIRED`를 반환합니다. 플러그인의 fontFace와
fontFamily는 파일·줄 번호·값만 보고하며 절대 수정하지 않습니다.

`--dry-run`은 output을 만들지 않고 예정 폰트, 수정 파일과 참조 변경을
`reports/font_patch_report.json`에 기록합니다. 실제 patch 보고서는 output의
`reports/`에도 저장됩니다. 사용자 폰트의 절대경로는 보고서나 `project.json`에
저장하지 않습니다.

## 소스 저장소 구조

```text
.
├─ glt.py
├─ core/                   # 공통 인터페이스, registry, 모델, QA 결과, fingerprint
├─ engines/
│  ├─ registry.py          # 내장 engine adapter 등록 지점
│  ├─ rpgmaker/            # 감지, 추출, 삽입, QA, fingerprint adapter
│  │  └─ fonts.py          # 폰트 cmap/참조 진단과 안전 patch
│  └─ wolf/                # 실험적 read-only 감지와 구조 조사
├─ projects/               # Project, Glossary, JSONL TM 관리
├─ tools/                  # 번역 JSONL Split/Merge 보조 유틸리티
├─ config/                 # 향후 이식 가능한 설정
├─ docs/                   # 향후 상세 문서
└─ tests/                  # 표준 unittest 테스트
```

## 0.6.0 Common Core + Engine Adapter 기반

CLI와 ProjectManager는 RPG Maker 구현을 직접 생성하지 않고 다음 경계를
통과합니다.

```text
CLI / ProjectManager
        ↓
EngineRegistry
        ↓
EnginePlugin
        ↓
RpgMakerEngine adapter
        ↓
기존 detector / extractor / inserter / QA / fingerprint / font 모듈
```

`core/registry.py`는 adapter 등록, engine ID 중복 방지, 자동 감지와 adapter
선택을 담당합니다. `engines/registry.py`가 현재 내장 adapter 목록을 구성합니다.
새 엔진은 `EnginePlugin`을 구현한 뒤 이 목록에 한 번 등록하면 detect/extract/
QA/apply/Project orchestration에서 같은 선택 경계를 사용할 수 있습니다.

공통 Core는 모든 게임이 JSON이거나 RPG Maker command index를 가진다고
가정하지 않습니다. `json_path`, event/page/command metadata는 기존 RPG Maker
호환을 위해 그대로 유지되는 선택 필드입니다. 향후 adapter 전용 metadata는
`TranslationEntry.extra_metadata`를 통해 평탄화하여 손실 없이 JSONL에 기록할
수 있습니다.

기존 `engines.rpgmaker.detector.RpgMakerEngine` import 경로는 그대로 유지되며,
새 canonical adapter import인 `engines.rpgmaker.engine.RpgMakerEngine`과 동일한
클래스를 가리킵니다. 기존 0.5.6 `project.json`은 migration 없이 load, QA,
apply할 수 있고 artifact schema/project version은 계속 `1`입니다.

## 0.7.0 WOLF Detection + Structure Inspection

WOLF 감지는 `Game.exe`/`GamePro.exe` 하나만으로 확정하지 않습니다. 공식 WOLF
구조인 `Data/BasicData/Game.dat`, `CommonEvent.dat`, `*DataBase.dat`, `.mps`
map, `Data.wolf` 또는 `Data/BasicData.wolf`를 조합하여 80점 이상일 때만 확정합니다. `Game.exe`
단독은 30점, `Data.wolf` 단독은 55점의 미확정 evidence입니다.

```powershell
python glt.py detect "D:\Games\WolfGame"
python glt.py inspect "D:\Games\WolfGame"
python glt.py inspect "D:\Games\WolfGame" --json ".\wolf_structure.json"
```

`inspect`는 다음 정보를 상대경로로 출력합니다.

- executable 후보
- Data 및 media 디렉터리
- `.wolf`, `.wolfx`, `.assets` archive 후보
- `Game.dat`, `CommonEvent.dat`, `*DataBase.dat`, `.mps`, TXT/CSV 후보
- map/common-event/database 후보의 개별 목록
- font 후보
- unknown binary 파일
- unpacked/packed/mixed/unknown packaging 상태
- probably_encrypted/not_detected/unknown encryption 상태
- 파일 크기, binary header 최대 16바이트
- 1 MiB 이하 핵심 BasicData 파일의 선택적 SHA-256

`.wolf`와 `.wolfx`는 암호화 가능성을 `probably_encrypted`로 표시합니다.
Pro판은 `.assets`처럼 확장자를 바꿀 수 있으므로 `.assets`만으로 암호화를
확정하지 않습니다. 검증 가능한 WOLF version marker가 없으면 version과
confidence를 모두 `unknown`으로 둡니다. JSON report는 기존 파일을 덮어쓰지
않으며 원본 게임 디렉터리 내부에는 생성할 수 없습니다.

구조 판단 근거는 WOLF RPG Editor 공식 문서의
[파일 설명](https://silversecond.com/WolfRPGEditor/Help/01filelist.html),
[게임 데이터 생성 및 암호화](https://silversecond.com/WolfRPGEditor/Help/02gamemake.html),
[Editor 명령과 Data.wolf](https://silversecond.com/WolfRPGEditor/Help/01control.html),
[공식 번역 도구의 대상 파일 설명](https://silversecond.com/WOLF_Translation_tool/Manual.html)을
기준으로 했습니다.

## 0.7.1 WOLF Archive Research + Read-only Probe

`.wolf` archive와 Pro의 개별 암호화 파일 `.wolfx`를 같은 형식으로 가정하지
않는 bounded read-only probe를 추가했습니다.

```powershell
python glt.py inspect-archive "D:\Games\WolfGame\Data\BasicData.wolf"
python glt.py inspect-archive "D:\Games\WolfGame\Data\BasicData.wolf" `
  --json ".\wolf_archive_report.json"
```

probe는 archive 전체를 읽거나 hash하지 않습니다. 최대 4개의 4096-byte
window만 읽어 다음 정보를 기록합니다.

- 상대경로, 크기, 확장자와 첫/끝 32 bytes
- sample entropy, zero byte, printable ASCII 비율
- `.wolf`, `.wolfx`, `.assets`의 분리된 후보 유형
- `Game.exe`, `GamePro.exe`, renamed executable 후보와 PE `MZ` 관찰
- 주변 companion 파일
- `BasicData`, `MapData`, `Text_Script`, `Script`, `mdb`, `tdb`, `Game` 등의
  filename heuristic 역할과 text likelihood
- Verified/Probable/Unknown으로 분리된 format knowledge

관찰한 header를 검증된 signature로 사용하지 않으며 archive generation은
`unknown`으로 유지합니다. 충분히 검증된 최신 세대 전체의 format contract가
없으므로 entry listing, extraction과 decryption은 지원하지 않습니다. JSON
report는 기존 파일을 덮어쓰지 않고 game/archive 디렉터리 외부에만 생성합니다.

기존 `inspect GAME`의 archive 행에도 `metadata` 아래 `archive_type`,
`probable_role`, `role_basis: filename_heuristic`, `text_likelihood`가 추가됩니다.
상세 조사 근거, 공식 `Editor.exe -txtoutput/-txtinput` 조건과 향후 접근 전략은
[WOLF archive research](docs/wolf_archive_research.md)에 정리했습니다.

## 0.7.2 WOLF Official Text I/O + Read-only Parser Prototype

WOLF Editor가 외부에 생성한 `Data_AutoTXT` 디렉터리를 수정 없이 분석합니다.

```powershell
python glt.py wolf-text-inspect "D:\Data_AutoTXT"
python glt.py wolf-text-inspect "D:\Data_AutoTXT" `
  --json ".\wolf_text_report.json"
```

보고서에는 `.Auto.txt`별 encoding/BOM/newline/final newline, BASIC/MAP/ALL,
section, 식별한 문자열 후보, 미지원 raw record와 parser issue가 들어갑니다.
JSON 보고서는 export 디렉터리 밖에만 새로 만들며 기존 파일을 덮어쓰지 않습니다.

현재 구조화하는 후보는 관찰된 game title/title-bar field, map/common event의
message command code `101`, 공식 DATANAME marker가 붙은 database 이름입니다.
choice 판별은 사람이 읽는 command label을 이용한 proposed prototype입니다.
원문의 whitespace와 escape 표현은 `original`에 그대로 남기고, `\n` 및
`<<COMMA>>`를 보기 좋게 바꾼 값은 별도 `normalized_view`에만 둡니다.

WOLF location은 `WolfLocation`에서 만든 `wolf:v1:...` ID를 사용하지만, 실제
official export 표본과 round-trip 검증이 아직 부족하므로 schema status는
`provisional`입니다. WOLF source JSONL 생성, QA, apply나 `-txtinput` 실행에는
연결하지 않았습니다.

상세 내용:

- [WOLF official text I/O research](docs/wolf_text_io.md)
- [WOLF canonical location and ID](docs/wolf_canonical_id.md)

## 0.7.3 WOLF verified-only Text Extraction Prototype

검증 기준을 통과한 `.Auto.txt` record만 기존 GLT `TranslationEntry` JSONL로
내보냅니다. source export는 수정하지 않으며 output과 report는 export 디렉터리
밖에만 새로 생성합니다.

```powershell
python glt.py wolf-text-extract "D:\Data_AutoTXT" `
  --output ".\wolf_source.jsonl" `
  --report ".\wolf_extraction_report.json"
```

기본 포함:

- public Editor export에서 message로 대조된 command code `101`의 첫 string slot
- 공식 DATANAME marker가 붙은 database name
- 명시적으로 허용한 Game title/title-bar field

기본 제외:

- choice 후보
- code `101`의 추가 string slot
- label만 message처럼 보이는 다른 command code
- `DATATYPE_n >= 2000`으로 관찰된 marker 없는 DB string cell
- unknown command, empty/numeric/punctuation/control-only record

encoding에는 `confirmed`, `probable`, `ambiguous`, `none` confidence와 evidence를
기록합니다. BOM 없는 byte sequence가 UTF-8과 CP932 양쪽에서 strict decode되면
임의 선택하지 않고 `TEXT_ENCODING_AMBIGUOUS`로 제외합니다.

WOLF ID v1은 계속 `provisional`입니다. 향후 native parser와의 대응을 검토할 수
있도록 representation-independent 후보 `wolf_logical_source`를 metadata에
추가했지만 canonical ID 자체는 0.7.2와 동일합니다. 상세 검증 근거와 제외 정책은
[WOLF text extraction validation](docs/wolf_text_extraction.md)에 정리했습니다.

## 테스트

```powershell
python -m unittest discover -s tests -v
```

## 0.7.3 범위 밖

자동 AI 번역·Review, OpenAI/Gemini API, SQLite/fuzzy TM, 형태소 기반 Glossary,
Glossary 자동 생성·수정, context 필드 기본 출력, 제어코드 placeholder 치환,
폰트 자동 다운로드·라이선스 판정, 플러그인 폰트 자동 변경, 폰트/UI 크기 및
줄바꿈 자동 조정, NBSP 변환, WOLF native binary parser·archive 복호화·repack·
`.Auto.txt` writer·QA·JSONL applier·font patch, Ren'Py/KiriKiri/Unity 지원,
GUI와 PyInstaller는
구현하지 않았습니다.

Python JSON parser를 사용하므로 수정한 JSON 파일의 공백·들여쓰기 같은 표현상
formatting은 달라질 수 있습니다. key 순서와 데이터 타입은 유지하고, 저장한
JSON을 다시 파싱하여 번역 문자열 외 의미적 변경이 없는지 검증합니다.

## 0.7.4 WOLF Translation QA + Safe Auto.txt Writer

0.7.4는 native WOLF data나 archive를 수정하지 않고, 공식 Text I/O가 만든
`Data_AutoTXT` 계열 디렉터리를 별도 디렉터리로 안전하게 복제·패치합니다.

```powershell
python glt.py wolf-text-qa translated.jsonl `
  --source "D:\Data_AutoTXT" `
  --report "reports\wolf_qa_report.json"

python glt.py wolf-text-apply translated.jsonl `
  --source "D:\Data_AutoTXT" `
  --output "D:\Data_AutoTXT_KR" `
  --dry-run `
  --report "reports\wolf_dry_run_report.json"

python glt.py wolf-text-apply translated.jsonl `
  --source "D:\Data_AutoTXT" `
  --output "D:\Data_AutoTXT_KR" `
  --report "reports\wolf_apply_report.json"
```

QA는 canonical ID/location, exact original, file/type, 중복 ID, source fingerprint,
WOLF control-like token과 variable reference를 검사합니다. warning, error, blocker를
구분하며 error 또는 blocker가 있으면 writer는 output을 만들지 않습니다. 빈 번역은
warning으로 집계하고 원문을 유지합니다.

Writer는 검증된 code `101` 첫 문자열, 명시적 system text field, 공식 DATANAME
marker 값만 교체합니다. source encoding/BOM, 실제 line ending, final newline,
whitespace와 미지원 raw record는 보존합니다. 임시 복사본을 재파싱해 승인된 값 외
record ID/value, unknown record, transport metadata가 같음을 확인한 후에만 output을
원자적으로 노출합니다. 자세한 정책은
[WOLF 0.7.4 QA/writer 문서](docs/wolf_roundtrip.md)를 참고하십시오.

Choice와 marker 없는 `DATATYPE_n` DB text cell은 계속 experimental이며 JSONL/writer
대상이 아닙니다. `wolf:v1` canonical schema도 native parser cross-route 검증 전까지
`provisional`입니다. `Editor.exe -txtinput`, native `.dat`/`.mps`, `.wolf`/`.wolfx`
수정은 구현하지 않았습니다.

## 0.7.5 WOLF Official Editor Integration Validation

0.7.5는 공식 WOLF Editor Text I/O CLI를 격리된 test-project 복사본에서 검증할 수
있는 opt-in integration layer를 제공합니다. 현재 개발 환경에는 실제 Editor가 없어
official integration 결과는 **NOT VERIFIED**이며, synthetic subprocess emulator 결과는
절대로 official evidence로 승격되지 않습니다.

```powershell
python glt.py wolf-editor-check "D:\WolfEditor\Editor.exe" `
  --project "D:\WolfTestProject" `
  --report "reports\wolf_editor_check.json"

# 기본 inspect mode: isolated copy에서 txtoutput과 GLT no-op 계획까지만 수행
python glt.py wolf-editor-validate "D:\WolfTestProject" `
  --editor "D:\WolfEditor\Editor.exe" `
  --target ALL `
  --report "reports\wolf_editor_integration.json"

# txtinput은 이 명시적 opt-in이 있을 때만 isolated copies에서 실행
python glt.py wolf-editor-validate "D:\WolfTestProject" `
  --editor "D:\WolfEditor\Editor.exe" `
  --target ALL `
  --allow-editor-import `
  --keep-workspace `
  --report "reports\wolf_editor_integration.json"
```

Editor 검색 범위는 explicit path, `GLT_WOLF_EDITOR`, project root의 `Editor.exe` 또는
`EditorPro.exe`뿐입니다. system-wide 검색은 하지 않습니다. 후보는 filename만 보지
않고 regular file, `.exe`, PE `MZ` signature와 project evidence를 함께 확인합니다.

실행은 `shell=False`, argument list, working directory, timeout, captured output 및
exit-code 기록을 사용합니다. report의 command/path는 portable form이며 stdout/stderr
내용은 저장하지 않고 크기와 SHA-256만 기록합니다. 원본 project와 Editor는 실행하지
않고 외부 workspace의 복사본만 사용합니다. 자세한 정책과 현재 검증 상태는
[WOLF Editor integration](docs/wolf_editor_integration.md)에 정리했습니다.

## 0.7.6 WOLF real Editor validation

WOLF Editor 3.682의 local official sample project를 원본 밖에 복사해 `ALL` Text I/O를
실행했습니다. 실제 stdout/stderr anonymous pipe가 BASIC/ALL 종료를 방해하는 현상을
재현하여 file-backed capture로 교체했습니다. 수정 후 baseline export, direct no-op,
GLT no-op, source UTF-8, UTF-8 BOM, UTF-8 no-BOM의 11개 Editor 호출이 모두 정상
종료했습니다.

실제 export는 UTF-8(no BOM), CRLF, final newline 형식이었고 Editor re-export도 모두
그 형식으로 정규화했습니다. 한국어 dialogue, code 102 Choice option, DATANAME을 각
trial에서 최대 3개만 변경해 exact semantic preservation, control token 보존,
`<<COMMA>>` transport와 mojibake 부재를 확인했습니다. Choice option은 verified
추출/writer 대상으로 승격했지만 cancel/default 의미와 native `.dat`/`.mps` cross-route
ID는 미검증이므로 `wolf:v1`은 계속 `provisional`입니다. Portable evidence는
[0.7.6 real Editor validation](docs/wolf_editor_real_validation_0.7.6.md)에 있습니다.

## 0.7.7 WOLF Project integration

공식 WOLF Editor Text I/O가 만든 `Data_AutoTXT`를 기존 GLT ProjectManager에
직접 연결합니다. RPG Maker와 같은 `project.json`, `source.jsonl`,
`translated.jsonl`, Glossary, Translation Memory, QA, dry-run, apply 흐름을
사용하며 WOLF 전용 프로젝트 포맷은 만들지 않습니다.

```powershell
python glt.py project create "D:\Data_AutoTXT" `
  --engine wolf_rpg_editor `
  --output "D:\Localization\WolfGame_KR"

python glt.py project qa `
  "D:\Localization\WolfGame_KR" `
  "D:\Data_AutoTXT"

python glt.py project apply `
  "D:\Localization\WolfGame_KR" `
  "D:\Data_AutoTXT" `
  --output "D:\Data_AutoTXT_KR" `
  --dry-run

python glt.py project apply `
  "D:\Localization\WolfGame_KR" `
  "D:\Data_AutoTXT" `
  --output "D:\Data_AutoTXT_KR"
```

`--engine`을 생략해도 유효한 `.Auto.txt` export는 자동 감지합니다. Project의
fingerprint와 `source_mode=auto_txt`가 현재 source와 다르면 apply를 차단합니다.
WOLF QA 결과는 Project의 `reports/qa_report.json`, `qa_issues.csv`,
`untranslated.csv`, `project_manifest.json`에 저장됩니다. dry-run은 output을 만들지
않고 같은 WOLF preflight와 round-trip 검증을 수행합니다.

현재 Project apply의 output은 번역된 `Data_AutoTXT` 복사본입니다. 네이티브
`.dat`/`.mps` 프로젝트에 대한 `Editor.exe -txtinput` 자동 적용은 0.7.7 Project
명령에 연결하지 않았습니다. 필요한 경우 기존 `wolf-editor-validate`로 원본 밖의
격리 복사본에서 Text I/O를 검증한 뒤 Auto.txt Project 흐름을 사용하십시오.
상세 구조와 안전 정책은
[WOLF Project integration](docs/wolf_project_integration_0.7.7.md)에 있습니다.

## 0.8.0 WOLF native format research

0.8.0은 native writer나 archive 해제를 추가하지 않습니다. unpacked WOLF
프로젝트의 `.dat`/`.mps`를 읽기 전용으로 인벤토리하고, 선택적으로 기존 공식
`Data_AutoTXT`와 hash-only known-string correlation을 수행하는 연구 명령을
제공합니다.

```powershell
python glt.py wolf-native-probe "D:\WolfProject" `
  --oracle "D:\Evidence\Data_AutoTXT" `
  --report ".\reports\wolf_native_research.json"
```

보고서는 게임 및 oracle 폴더 밖의 새 JSON이어야 합니다. 원문과 절대경로는
저장하지 않으며 byte offset은 증거일 뿐 canonical ID로 사용하지 않습니다.
`wolf:v1`은 native parser 교차 검증이 더 필요하므로 `V2_LIKELY`로 평가되었지만,
0.8.0에서 schema를 변경하지 않습니다. 구현체·라이선스 비교, 실제 WOLF 3.682
관찰 결과, 레이어 아키텍처와 0.8.1 권장 parser는
[WOLF native format research](docs/wolf_native_research_0.8.0.md)에 있습니다.

## 0.8.1 RPG Maker translation coverage audit

0.8.1은 기존 RPG Maker extraction/apply 범위를 늘리지 않고, 표준 event code,
Move Route, DB field, MV/MZ plugin command, script candidate와 mirror 관계를 읽기
전용으로 조사합니다.

```powershell
python glt.py rpgmaker-audit "D:\Games\Example" `
  --report ".\reports\rpgmaker_coverage.json" `
  --csv ".\reports\rpgmaker_candidates.csv"
```

보고서와 CSV는 새 파일이어야 하고 게임 폴더 밖에 있어야 합니다. 실제 게임
원문과 절대경로는 저장하지 않으며, source file count/size/content hash를 실행
전후 비교합니다. 320/324/325와 `Classes.json` name 누락, 102/402 mirror 의미,
356·357·657 및 script 안전성 결론은
[RPG Maker translation coverage audit](docs/rpgmaker_translation_coverage_0.8.1.md)에
정리되어 있습니다.

## 0.8.2 RPG Maker standard coverage expansion

0.8.1 audit에서 표준 player-visible text로 확인한 event code `320`
(Change Actor Name), `324` (Change Nickname), `325` (Change Profile)의
`parameters[1]`과 `Classes.json`의 `name`을 기존 canonical ID와 DB path 규칙으로
추가했습니다. 이 항목들은 기존 extract, QA, apply, Project, Glossary, TM 및
split/merge 흐름을 그대로 사용합니다.

0.8.2 Project fingerprint에는 `Classes.json`이 포함됩니다. 0.8.1 이하에서 만든
기존 RPG Maker Project는 pre-Classes fingerprint도 검증된 legacy fingerprint로
인식하므로 schema migration 없이 계속 사용할 수 있습니다. Plugin command,
script, move-route, 102/402 synchronization은 이번 버전에 포함되지 않습니다.

## 0.8.3 RPG Maker plugin text coverage

MV event code `356`은 검증된 `インフォ表示` 명령의 payload만 번역 단위로
추출하며 apply 시 원래 명령 prefix와 공백을 그대로 재조립합니다. `P_SHAKE`,
`P_SPIN_RELATIVE`, `D_TEXT_SETTING`과 비텍스트 payload는 internal로 유지하고,
그 밖의 text-like payload는 audit의 conditional candidate로만 노출합니다.

MZ event code `357`은 `MNKR_TMLogWindowMZ` / `addLog` /
`parameters[3].text` 규칙만 자동 추출·QA·apply에 연결합니다. 다른 text-like
argument는 bounded audit candidate이며 plugin name, command와 argument path를
기록합니다. 뒤따르는 code `657`의 `text = ...` annotation이 원문과 정확히
일치할 때만 함께 갱신하고, 불일치 annotation은 보존하면서 warning을 냅니다.
standalone `657`은 번역 단위가 아닙니다. 자세한 규칙과 검증 결과는
[RPG Maker plugin text coverage](docs/rpgmaker_plugin_text_coverage_0.8.3.md)에
정리되어 있습니다.

## 0.8.4 RPG Maker MV plugin command discovery

MV `plugins.js`의 enabled/load order와 정확히 같은 이름의 plugin source를
read-only로 연결해 `pluginCommand`의 literal branch, argument flow, 최대 1단계
helper, display/internal sink를 bounded lexical analysis로 추적합니다. JavaScript를
실행하거나 전체 literal을 추출하지 않으며 dynamic/minified/ambiguous handler는
UNKNOWN 또는 CONDITIONAL로 남깁니다.

```powershell
python glt.py rpgmaker-audit "D:\Games\Example" `
  --report ".\reports\rpgmaker_coverage.json"

# 분리 보관된 read-only evidence를 사용할 때만 지정
python glt.py rpgmaker-audit "D:\Games\Example" `
  --plugins-config "D:\Evidence\plugins.js" `
  --plugin-source "D:\Evidence\plugins" `
  --report ".\reports\rpgmaker_coverage.json"
```

지원 argument mode는 `single_token`, `fixed_index`, `joined_remainder`,
`joined_slice`, `multiple_fixed`, `numeric`, `identifier`, `unknown`입니다.
reconstruction까지 확정된 `APPLY_VERIFIED`만 기존 JSONL extraction/apply에
연결합니다. single-token 번역에 일반 공백이 생기면
`PLUGIN_ARGUMENT_SPACE_UNSAFE`로 차단하며 NBSP 자동 변환은 하지 않습니다.
기존 `ShowInfo`/`インフォ表示` 위치 ID와 schema version 1은 유지됩니다.

## 0.8.5 MV helper / transform resolution

0.8.4 discovery에 결정적인 command transform helper, args join helper, local
command/args alias와 최대 2단계의 정적 helper dispatch를 추가했습니다. 표시 text
state로 직접 전달되는 흐름과 rendering configuration을 구분하며 computed method,
callback, recursion, eval은 계속 UNKNOWN/UNSAFE로 유지합니다.

`joined_optional_numeric_tail`은 공백으로 재구성한 text 뒤의 명시적 숫자 option을
원문에 보존합니다. runtime 제어코드 때문에 숫자 여부가 불명확하면 추출하지
않습니다. 기존 ID, schema version 1과 `mv_info_display` 규칙은 변경하지 않습니다.
