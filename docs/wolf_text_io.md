# WOLF official text I/O research (GLT 0.7.2)

이 문서는 WOLF RPG Editor의 공식 `.Auto.txt` 표현을 GLT가 읽기 전용으로
다루기 위한 조사 기록이다. 공식 문서가 명시한 사실, 공개된 실제 export에서
관찰한 사실, GLT의 제안, 아직 모르는 부분을 의도적으로 분리한다.

## Evidence policy

공식 자료:

- [Editor 명령행과 `-txtoutput`/`-txtinput`](https://silversecond.com/WolfRPGEditor/Help/01control.html)
- [Editor 옵션과 `.Auto.txt` 입출력 폴더](https://silversecond.com/WolfRPGEditor/Help/02editor_option.html)
- [공식 release log의 DATANAME/COMMA 및 text I/O 수정 기록](https://silversecond.com/WolfRPGEditor/old_releaselog/ReleaseLog07.html)
- [공식 개발 블로그의 text output 관련 기록](https://smokingwolf.github.io/dev_blog/archive/2024/11.html)

실제 파일 구조는 공개 저장소
[nobihaza_udhita의 Data_AutoTXT](https://github.com/tomodesuyo/nobihaza_udhita)를
읽기 전용으로 조사했다. 저장소의 게임 문장은 GLT fixture나 코드에 복사하지
않았으며, 테스트는 자체 작성한 synthetic 문자열만 사용한다. 현재 관찰 표본은
한 저장소에 편중되어 있으므로 포맷 전체를 대표한다고 단정하지 않는다.

## Verified

- 공식 명령은 `Editor.exe -txtoutput TARGET`과
  `Editor.exe -txtinput TARGET`이며 target은 `ALL`, `BASIC`, `MAP`이다.
- 옵션 UI는 게임 폴더의 `Data_AutoTXT`를 공식 text I/O 위치로 설명한다.
- 공식 release log에는 database 이름 표식 `<<!--DATANAME--!>>`과 이름 안의
  comma를 나타내는 `<<COMMA>>`가 기록되어 있다.
- official text input은 `.Auto.txt`를 다시 native data에 반영하는 별도 작업이다.
  GLT 0.7.2는 이 명령을 호출하지 않는다.

## Observed

공개된 실제 export에서 다음 배치를 관찰했다.

```text
Data_AutoTXT/
├─ BasicData/
│  ├─ Game.dat.Auto.txt
│  ├─ Game.dat.PRO.Auto.txt
│  ├─ CommonEvent.dat.Auto.txt
│  ├─ DataBase.Auto.txt
│  ├─ CDataBase.Auto.txt
│  ├─ SysDataBase.Auto.txt
│  └─ TileSetData.dat.Auto.txt
└─ MapData/
   └─ *.mps.Auto.txt
```

관찰 표본은 UTF-8, BOM 없음, LF, final newline 있음이었다. 이는 모든 WOLF
버전의 고정 규칙이라는 뜻이 아니다. GLT는 ASCII, UTF-8, CP932, UTF-16 LE/BE를
strict 후보 순서로 판별하고 파일마다 encoding, BOM, newline, final newline을
별도로 보고한다. 판별 실패는 `TEXT_DECODE_FAILED`이며 replacement character로
조용히 복구하지 않는다.

관찰된 section과 구조:

- Game settings: `[GAMESETTING_TEXT_OUTPUT]` 또는
  `[GAMESETTING_PRO_TEXT_OUTPUT]` 뒤에 `KEY=value` 행이 있다.
- Map: `[MAPDATA_TEXT_OUTPUT]`, `[EVENTDATA_TEXT_OUTPUT]`, `EVENT_ID`,
  `EVENT_PAGE_NUM`, 0-based로 보이는 `EVENT_PAGE`, `COMMAND_NUM`이 있다.
- Common event: `[COMMON_EVENT_TEXT_OUTPUT]`, `COMMON_ID`, `COMMAND_NUM`이 있다.
- Event command: `WoditorEvCOMMAND_START/END` 사이에 machine-oriented command
  행, `[COMMAND_TEXT_START/END]` 사이에 `■`로 시작하는 사람이 읽을 수 있는
  대응 행이 있다.
- 관찰한 message command의 code는 `101`이었다. raw command의 문자열은
  double quote literal이며 `\n`, `\c` 같은 backslash 표기가 물리적으로 한
  행 안에 남는다.
- Database: `[DATABASE_TEXT_OUTPUT]`, `TYPE_ID`,
  `<<--CSV_START-->>`/`<<--CSV_END-->>`, header와 record 행이 있다.
  `<<!--DATANAME--!>>` 뒤 문자열이 database record 이름이다.

## Parser prototype

`WolfTextInspector`는 `.Auto.txt`만 재귀적으로 읽는다. symlink는 따라가지 않고,
파일 수와 개별 크기에 상한을 둔다. source bytes를 수정하지 않으며 다음 후보만
현재 구조화한다.

- 관찰된 Game title/title-bar field: `system`
- map/common raw command code `101`: `dialogue`
- 사람이 읽는 command label이 선택지로 보이는 경우: `choice` (proposed)
- 공식 DATANAME marker 뒤 database 이름: `database_name`

알 수 없는 문자열 포함 command/CSV 행은 제한된 `unknown_records`에 raw 형태로
남긴다. image/audio path, 임의 game setting, database의 marker 없는 cell은
번역 대상으로 추측하지 않는다.

## Escaping and multiline

`original`은 quote 안의 source representation을 unescape하거나 trim하지 않는다.
따라서 full-width space, tab, punctuation, Japanese typography, `\n`, `\"`,
control-like token은 입력 표현 그대로 유지된다. 편의를 위한
`normalized_view`만 관찰된 `\n`을 실제 newline으로 보여 주며 canonical ID나
향후 round-trip source로 사용하지 않는다. Database의 `<<COMMA>>`도 original에
남고 normalized view에서만 comma로 표시된다.

GLT는 backslash 문자 뒤 임의 alphabetic name과 선택적 bracket argument,
그리고 `<<...>>` 형태를 `control_codes` 후보로 관찰한다. `\n`은 multiline
representation으로 분리한다. 이는 보존용 lexer이며 RPG Maker 제어코드 의미를
WOLF에 적용하는 validator가 아니다.

## Proposed

- BASIC/MAP은 파일 배치로 domain을 나누고 둘이 함께 있으면 report target을
  `ALL`로 표시한다.
- raw command와 `■` command를 같은 순서로 대응시킨다. 개수가 다르면 warning을
  내고 확인 가능한 raw data만 유지한다.
- 선택지 label 판별은 현재 synthetic test로만 보호되는 보수적 prototype이다.
- canonical location/ID는 [별도 문서](wolf_canonical_id.md)의 provisional v1을
  사용한다.

## Unknown

- WOLF 버전별 encoding/BOM/newline 기본값과 모든 locale의 차이
- official import 후 export했을 때 byte-for-byte 및 semantic round-trip 규칙
- raw command 문법 전체, quote/backslash/tab/empty string의 모든 escape 규칙
- multiline message, choice branch, message options와 command index의 세대별 의미
- 모든 Game setting field와 Common/System/User/Variable database field schema
- description 및 nested database value의 번역 가능 여부와 안정적인 field ID
- 복수 command block, deleted event/page, reordered record에서 ID가 유지되는 범위
- Pro 및 최신 Editor에서 추가된 section/marker

실제 Editor가 있는 환경에서는 `GLT_WOLF_AUTOTXT_FIXTURE`에 외부 official export
디렉터리를 지정해 optional deterministic/read-only test를 실행할 수 있다.
Editor binary나 저작권이 있는 게임 텍스트는 repository에 포함하지 않는다.

0.7.3에서 strict decoder 후보가 둘 이상 성공하는 경우를 명시적으로 ambiguous로
분리하고 confidence/evidence를 report에 추가했다. verified-only JSONL 포함 정책은
[WOLF extraction validation](wolf_text_extraction.md)을 따른다.
