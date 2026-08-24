# WOLF official fixture validation and extraction policy (GLT 0.7.3)

이 문서는 `.Auto.txt` record를 공통 GLT JSONL에 포함할 수 있는지 결정한 근거를
정리한다. `Verified`, `Observed`, `Experimental`, `Unknown`은 포맷 전체의
완전성을 뜻하지 않는다. 특히 `VERIFIED_TRANSLATABLE`은 현재 증거로 기본 추출을
허용한다는 제품 정책이며 WOLF가 공개한 영구 binary ABI라는 뜻이 아니다.

## Fixture inventory

### Verified

- 공식 도움말은 `-txtoutput/-txtinput`, `ALL/BASIC/MAP`, `Data_AutoTXT` 및
  `.Auto.txt`의 Editor 전용 입출력을 정의한다.
- 공식 Ver3.50 release log는 이 Text I/O 기능의 도입과 DATANAME marker,
  database CSV, map/common event text output을 기록한다.
- 따라서 WOLF 2.x와 3.0~3.39에는 동일한 official `.Auto.txt` 기능이 없으며,
  “구버전 `.Auto.txt` fixture”를 만들어 버전 호환 근거로 사용할 수 없다.

### Observed

- 공개 저장소 `tomodesuyo/nobihaza_udhita`의 WOLF Pro `Data_AutoTXT` 배치를
  원문을 repository에 복사하지 않고 조사했다.
- map/common export에서 raw command block, human-readable command block,
  event/page/common ID, command order를 관찰했다.
- raw code `101`의 첫 string slot과 `■文章` 의미가 같은 정렬 가능한 map block을
  대조했다. 독립 공개 command-code 분석도 code `101`을 message로 기록한다.
- database에는 `TYPE_ID`, `DATATYPE_n`, `ITEMNAME_n`, CSV block과 DATANAME이
  함께 나타났다. `DATATYPE_n`의 2000 계열과 string field의 관계도 관찰했다.
- 공식 개발 블로그는 실제 개발 프로젝트에서도 `CommonEvent.Auto.txt`,
  `DataBase.Auto.txt`, `CDataBase.Auto.txt`가 생성된다고 설명한다.

### Synthetic

repository test fixture는 실제 게임 원문을 포함하지 않는다. command, database,
encoding, whitespace, collision을 검증하는 최소 자체 문자열만 사용한다.

## Record decisions

| Record | Evidence tier | GLT classification | Default JSONL |
|---|---|---|---|
| code 101, string slot 0 | Observed in public Editor export | `VERIFIED_TRANSLATABLE` | Include |
| code 101, extra slot | Experimental | `EXPERIMENTAL_TRANSLATABLE` | Exclude |
| label-only message with other code | Experimental | `EXPERIMENTAL_TRANSLATABLE` | Exclude |
| choice label/literals | Experimental | `EXPERIMENTAL_TRANSLATABLE` | Exclude |
| DATANAME marker value | Official marker + observed structure | `VERIFIED_TRANSLATABLE` | Include |
| DATATYPE 2000-series cell | Observed, field semantics incomplete | `EXPERIMENTAL_TRANSLATABLE` | Exclude |
| approved Game title fields | Observed explicit player-facing keys | `VERIFIED_TRANSLATABLE` | Include |
| arbitrary command/string cell | Unknown | `UNKNOWN` | Exclude |

## Command code 101

숫자가 RPG Maker와 같다는 이유는 근거로 사용하지 않는다. 현재 default extraction은
WOLF public Editor export의 raw/human representation 대조와 독립적인 WOLF command
code 자료를 근거로 한다. official help의 “文章の表示” 문서는 special character
사용을 확인하지만 numeric code mapping 자체를 규격으로 보장하지 않는다.

보수적으로 첫 string slot만 내보낸다. label이 없어도 raw code 101의 첫 slot은
동일하게 취급하지만, 다른 code가 우연히 `■文章` label과 정렬된 경우는 experimental
record로 남긴다.

## Choice

공식 도움말은 choice command, branch, cancel/default 동작을 설명하지만 현재 확보한
`.Auto.txt` fixture에서 declaration과 labels, branch index, nested/cancel/default의
raw 관계를 안정적으로 재구성하지 못했다. 따라서 parser inspection에는 후보를
남기되 JSONL에는 포함하지 않는다.

## Database

DATANAME marker는 official release note가 의미를 명시하므로 포함한다. ID는
database file, `TYPE_ID`, CSV header를 제외한 record order, `field=dataname`으로
만든다. 이 record order가 편집 삽입/삭제를 견디는 native stable ID인지는 아직
확인되지 않았으므로 schema는 provisional이다.

관찰된 `DATATYPE_n >= 2000` cell은 `database_text` experimental record로 파싱해
description/help/name 후보 조사에 활용한다. 그러나 field별 player visibility와
identifier/path 여부가 확정되지 않아 기본 extraction에서 제외한다.

## Encoding decision

- matching BOM + strict decode: `confirmed`
- strong UTF-16 null-byte pattern + strict decode: `probable`
- BOM 없이 UTF-8 또는 CP932 한 가지만 성공: `probable`
- ASCII-only: `ascii`, 실제 source encoding은 `ambiguous`
- UTF-8과 CP932 모두 성공: `unknown/ambiguous`, record parsing 중단
- 모두 실패: `unknown/none`, `TEXT_DECODE_FAILED`

replacement decoding은 사용하지 않는다. 각 파일 report에 confidence와 evidence를
보존한다.

## Common JSONL mapping

기존 `TranslationEntry`의 required field order를 그대로 사용한다.

```text
id, engine, file, type, original, translation
```

기존 optional field 뒤에 WOLF metadata를 추가한다. 주요 metadata는 `location`,
`wolf_domain`, `source_auto_txt`, `target_type`, `record_classification`,
`fixture_confidence`, `wolf_logical_source`, container/database/field/text slot과
command code이다. control-like tokens는 기존 `control_codes`에 순서와 중복을
유지해 저장한다.

## Canonical location

canonical ID는 0.7.2의 `wolf:v1:...`를 유지하고 status도 `provisional`이다.
source component가 `.Auto.txt` representation을 포함하는 현재 한계를 숨기지 않는다.
별도 `logical_source`는 `.Auto.txt` suffix를 제거하고 `Data/` 기준 native 후보를
제공하지만 아직 ID 계산에는 쓰지 않는다. native parser가 생기기 전에 여러 Editor
version과 repeated export/DB reorder를 검증한 뒤 v1 유지 또는 v2 전환을 결정한다.

## Safety boundary

0.7.3은 export directory를 읽기만 한다. JSONL/report는 source 밖에만 생성하고
기존 파일을 덮어쓰지 않는다. collision은 suffix나 silent overwrite 없이 전체
JSONL 생성을 차단한다. `.Auto.txt` writer, `-txtinput`, native data, archive,
executable 수정은 구현하지 않는다.

## Sources

- [Official command-line Text I/O](https://silversecond.com/WolfRPGEditor/Help/01control.html)
- [Official Editor `.Auto.txt` options](https://silversecond.com/WolfRPGEditor/Help/02editor_option.html)
- [Official Ver3.50+ Text I/O release log](https://silversecond.com/WolfRPGEditor/old_releaselog/ReleaseLog07.html)
- [Official message command help](https://silversecond.com/WolfRPGEditor/Help/04ev_text.html)
- [Official database operation model](https://silversecond.com/WolfRPGEditor/Help/04ev_db.html)
- [Official development blog](https://smokingwolf.github.io/dev_blog/category/owh2dev/001.html)
- [Public observed Data_AutoTXT](https://github.com/tomodesuyo/nobihaza_udhita)
- [Independent WOLF command-code analysis](https://kameske027.cloudfree.jp/woditor_analysis/pages/eventCodeSpecification.html)
