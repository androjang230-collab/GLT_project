# WOLF archive research and access strategy (GLT 0.7.1)

이 문서는 WOLF RPG Editor 배포 게임의 `.wolf`/`.wolfx`를 번역 상호운용성
목적으로 조사한 결과를 정리한다. GLT 0.7.1은 아카이브를 풀거나 복호화하거나
수정하지 않는다. 공식 문서가 보장하는 사실, 파일 배치로부터의 추정, 아직
검증하지 못한 사항을 분리한다.

## Sources and evidence policy

1차 근거는 WOLF RPG Editor 공식 도움말과 공식 Translation Support Tool
매뉴얼이다. 공개 구현은 포맷 연구 후보의 존재와 범위를 확인하는 데만
사용했으며 GLT 코드나 dependency로 포함하지 않았다.

공식 자료:

- [파일 설명](https://silversecond.com/WolfRPGEditor/Help/01filelist.html)
- [게임 데이터 생성 및 암호화](https://silversecond.com/WolfRPGEditor/Help/02gamemake.html)
- [Editor 명령행, 암호화 세대, text I/O](https://silversecond.com/WolfRPGEditor/Help/01control.html)
- [Editor의 `.Auto.txt` 입출력](https://silversecond.com/WolfRPGEditor/Help/02editor_option.html)
- [Pro 기능과 custom archive extension](https://silversecond.com/WolfRPGEditor/Help/06pro_version.html)
- [Pro `.wolfx` 개별 파일 암호화](https://silversecond.com/WolfRPGEditor/Help/02_file_crypt_pro.html)
- [공식 Translation Support Tool](https://silversecond.com/WOLF_Translation_tool/Manual.html)

검토한 공개 구현:

- [GARbro DXA reader](https://github.com/morkt/GARbro/blob/master/ArcFormats/DxLib/ArcDX.cs):
  MIT 라이선스의 read-only DX archive 구현이다. `.wolf`를 후보 확장자로
  등록하고 여러 구형 DX archive version을 처리하지만, 이 사실만으로 최신
  WOLF Pro나 `.wolfx` 호환성을 보장할 수 없다.
- [WolfDec](https://github.com/Sinflower/WolfDec): 공개된 WOLF archive
  decrypter이며 자체 README가 최신 게임에는 UberWolf 사용을 권한다. GLT는
  이 구현을 포함하지 않았다.
- [UberWolf](https://github.com/Sinflower/UberWolf): README와 release가 Pro,
  custom extension, `.wolfx`, 최신 암호화 지원을 표방한다. 자동 key 탐색과
  protection 관련 기능도 포함하므로 이번 read-only metadata probe의
  dependency로 채택하지 않았다.
- [wolf-rpg-formats](https://github.com/djytw/wolf-rpg-formats): unpacked native
  WOLF data용 Kaitai schema 후보이다. archive layer 자체의 공식 사양은 아니다.

## Verified

다음은 공식 문서로 확인한 사항이다.

- `Editor.exe -gamedata -crypt ALL`은 `Data` 전체를 `Data.wolf`로 만든다.
  `DIR`/`DIR_ALL`은 Data 내부 폴더를 개별적으로 암호화할 수 있다.
- 공식 명령은 `-c_ver`로 암호화 버전을 선택할 수 있다. 공식 예에는 일반
  버전 번호, protect-key 버전, 최신 버전 선택값이 있다. 서로 다른 암호화
  버전의 파일 혼용은 읽기 실패를 일으킬 수 있다고 경고한다.
- Pro는 보통 `.wolf`인 암호화 파일의 확장자와 이름을 `.assets` 같은 값으로
  바꿀 수 있다. 따라서 `.assets`만으로 WOLF archive라고 확정할 수 없다.
- `.wolfx`는 Pro의 개별 파일 암호화 결과이다. 원래 파일명 뒤에 `.wolfx`가
  붙으며 이미지·텍스트뿐 아니라 font와 `.mps`도 대상이 될 수 있다.
- `.wolfx`는 runtime 복호 키, 문자열 변수/키, 숫자 변수/키 조건을 선택적으로
  사용할 수 있다. 같은 원래 파일과 `.wolfx`가 있으면 게임 실행 중 `.wolfx`
  쪽을 우선할 수 있다.
- `GamePro.exe`는 Pro 게임 실행 파일의 공식 이름이다. 아이콘/정보 변경과
  archive 이름 변경이 가능하므로 파일 이름이나 PE 표시만으로 세대·판본을
  완전히 판정할 수 없다.
- 공식 Translation Support Tool은 배포 게임의 `Game.exe` 또는 `GamePro.exe`를
  선택하여 위치 코드가 포함된 XLSX 대역을 만들고, 원본을 수정하지 않은 새
  번역 게임 폴더를 생성한다고 설명한다. 라이선스별 사용 조건과 제한이 있다.

## Probable

다음은 공식 명명 관례와 실제 Sample A/B의 파일명에 근거한 휴리스틱이다.
내부 content를 확인한 결과가 아니다.

| Filename | Probable role | Text likelihood |
|---|---|---|
| `BasicData.wolf` | possible basic data | high |
| `MapData.wolf` | possible map data | high |
| `Text_Script.wolf`, `Script.wolf` | possible script/text data | high |
| `mdb.wolf`, `tdb.wolf` | possible database/text data | high |
| `Game.wolf` | possible game data | medium |
| `SystemFile.wolf` | possible system data | medium |
| `*.ttf.wolfx` | possible encrypted font asset | low |

`.wolf` 또는 custom extension 파일이 WOLF 실행 파일과 Data 배치 곁에 있으면
WOLF archive 후보일 가능성이 높다. 그러나 확장자와 엔트로피 수치만으로
포맷, 암호화 여부, key 또는 generation을 증명하지 않는다.

## Unknown

- 모든 `.wolf` 세대에 공통으로 안전하게 쓸 수 있는 공식 공개 header signature
- bounded header bytes만으로 archive generation을 판정하는 공식 규칙
- 특정 파일에 실제로 적용된 compression, encryption generation과 key
- 최신 free/Pro/custom extension 전체를 포괄하는 공식 entry table 사양
- `.wolfx` payload/header/key derivation의 공식 공개 binary contract
- `Game.exe`/`GamePro.exe` PE version resource에서 신뢰할 수 있게 WOLF archive
  generation을 얻는 marker
- 공식 Editor text I/O가 배포용 packed archive를 직접 읽는다는 보장
- `.Auto.txt` 자체의 고정 encoding 계약. 공식 문서는 WOLF v3 native data가
  UTF-8로 바뀌었다고 설명하지만, 이를 모든 `.Auto.txt` 출력의 고정 encoding
  보장으로 확대하지 않는다.

이 미확인 항목 때문에 GLT 0.7.1은 header magic 판정, version 판정, entry
listing을 구현하지 않는다.

## Read-only probe contract

```powershell
python glt.py inspect-archive "D:\Game\Data\BasicData.wolf"
python glt.py inspect-archive "D:\Game\Data\BasicData.wolf" `
  --json ".\wolf_archive_report.json"
```

probe는 seek 가능한 파일에서 최대 4개 × 4096-byte window만 읽는다. 출력은
파일 크기, 첫/끝 32 bytes, 분산 sample의 Shannon entropy/zero/printable 비율,
확장자, 주변 companion 파일, Game/GamePro/renamed executable 후보, PE `MZ`
존재, filename 역할 휴리스틱이다. archive 전체 hash/scan과 entry parse를 하지
않는다. JSON에는 절대경로를 저장하지 않으며 기존 report를 덮어쓰거나 game
directory 안에 report를 만들지 않는다.

엔트로피는 암호화를 판정하는 증거가 아니라 관찰값이다. 짧은 파일, 0-byte
파일, synthetic fixture에서도 안전하게 동작하도록 설계했다.

## Official Editor text export/import

공식 명령은 다음과 같다.

```text
Editor.exe -txtoutput -txt_folder Data_AutoTXT -target ALL -wait
Editor.exe -txtinput  -txt_folder Data_AutoTXT -target ALL -wait
```

- `Editor.exe`가 필요하다. `-txtoutput`과 `-txtinput`은 동시에 사용할 수 없다.
- `-txt_folder`는 `Editor.exe` 기준 상대 폴더이며 기본값은 `Data_AutoTXT`다.
- `ALL`: BASIC + 모든 map, `BASIC`: game settings/3 DB/common events/tile
  settings, `MAP`: 모든 `.mps`다.
- 기본 데이터는 출력 폴더의 `BasicData`, map은 Data 아래 구조를 재현한
  `*.mps.Auto.txt`로 나온다. `-txtinput`은 해당 text에서 game data를 복원한다.
- 공식 설명상 목적은 Editor project를 Git 등으로 버전 관리할 때의 text
  round-trip이다. `.Auto.txt`는 게임 runtime이 직접 읽는 형식이 아니다.
- 배포 게임에 `Editor.exe`와 원본 project가 없거나 packed archive만 있는
  경우 직접 동작한다는 공식 보장은 확인하지 못했다. 따라서 GLT는 이를
  배포 archive unpacker로 간주하지 않는다.
- encoding은 다음 단계에서 실제 Editor 버전별 fixture를 왕복해 BOM/encoding,
  newline과 escaping을 측정하기 전까지 `unknown`으로 유지한다.

공식 Translation Support Tool은 별도 제품/경로다. 배포 게임에서 XLSX 추출과
새 번역 게임 생성을 공식 지원하지만 라이선스별 조건이 있으므로, 사용자가
적법한 라이선스를 보유하고 해당 조건을 확인한 경우에만 별도 연동 후보가 된다.

## Archive access strategies

| Strategy | Prerequisites | Safety | Coverage | Round trip | Limits / difficulty |
|---|---|---|---|---|---|
| A. Official Editor text export/import | matching `Editor.exe`, editable project/native data | highest when run on a copy | BASIC + MAP as documented | official text input restores native data | packed distribution-only games are not verified; encoding/version fixture work required |
| B. Unpacked native parser | user-supplied unpacked `Data/BasicData/*.dat`, `.mps` | high with read-only parser and copied output | native DB/common event/map targets | possible only after exact per-version writer/structure validation | binary schema/version/encoding work remains substantial |
| C. Packed reader/decryptor | verified archive generation and authorized keys/access path | lowest; strict read-only staging required | needed for Sample A/B packed-first reality | repack is a separate, harder problem and is out of scope | newest Pro/custom extension/`.wolfx` matrix, licensing and security boundaries require review |
| D. User-supplied unpacked data | user or official tool provides an external unpacked copy | high; GLT never touches original archive | same as B after validation | depends on how the copy was produced | simplest safe fallback, but requires external preparation and provenance/fingerprint checks |

권장 순서는 A 또는 D를 먼저 검증하고, B의 read-only parser를 만든 다음 C를
독립 layer로 연구하는 것이다. C를 구현하더라도 extraction과 repack/apply를
분리하고, 원본 archive·실행 파일·키를 수정하지 않아야 한다.

## Decision for GLT 0.7.1

- 구현: metadata catalog, bounded `.wolf`/`.wolfx`/custom candidate probe,
  portable `ArchiveReport`, Game/GamePro/renamed metadata, role heuristic,
  `StructureReport` 연동.
- 미구현: entry listing, parser, decryption, key discovery, extraction, JSONL,
  repack, apply, executable patch.
- dependency: 추가 없음.
- 법적/안전 경계: 사용자가 보유한 게임의 번역 상호운용성만 목적으로 하며,
  DRM·인증·라이선스 검사·구매 검증·실행 제한 우회 기능은 다루지 않는다.
