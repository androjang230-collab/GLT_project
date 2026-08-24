# WOLF canonical location and ID (provisional schema v1, reviewed in 0.7.3)

GLT 0.7.2의 WOLF ID는 아직 배포용 확정 규격이 아니다. 공식 문서가 `.Auto.txt`
전체 grammar와 stable identity contract를 제공하지 않고, 현재 검증한 실제 export
표본도 제한되어 있으므로 status를 명시적으로 `provisional`로 둔다.

## Design goals

- deterministic and human-debuggable
- 같은 원문이 반복되어도 구조적 위치별로 구분
- absolute path, drive letter, line number, 원문, 번역과 독립
- RPG Maker ID와 분리된 `wolf` namespace
- delimiter가 들어간 값도 percent encoding으로 모호하지 않게 표현

## Internal model

문자열 ID를 직접 조립하지 않고 먼저 `WolfLocation`을 만든다.

| Component | Meaning |
|---|---|
| `domain` | `basic`, `map`, `common`, `database` |
| `source` | export root 기준 forward-slash 상대 `.Auto.txt` 경로 |
| `container_kind/id` | map event, common event, database 또는 setting container |
| `page_id` | export에서 읽은 map `EVENT_PAGE` 값 |
| `type_id` | database `TYPE_ID` |
| `record_id` | CSV block에서 header를 제외한 0-based record 순서 |
| `command_index` | 해당 command block 안 raw command의 0-based 순서 |
| `field` | 안정적으로 관찰된 setting key 또는 `dataname` |
| `text_index` | 한 raw command 안 quote literal의 0-based 순서 |

물리적 line number는 diagnostics의 `raw_context`에만 남고 location/ID에는 넣지
않는다. original/normalized/translation도 ID 구성 요소가 아니다.

## Serialization rule

고정 순서는 다음과 같다.

```text
wolf:v1:<domain>:<source>:
  <container_kind>=<container_id>:
  page=<page_id>:type=<type_id>:record=<record_id>:
  command=<command_index>:field=<field>:text=<text_index>
```

없는 optional component는 생략한다. 각 component는 UTF-8 의미의 URL percent
encoding을 사용한다. source의 `/`는 portable path 가독성을 위해 유지한다.
구분자인 `:`와 `=`는 값 안에서 encoded된다.

Synthetic example:

```text
wolf:v1:map:MapData/Map001.mps.Auto.txt:event=7:page=1:command=3:text=0
wolf:v1:common:BasicData/CommonEvent.dat.Auto.txt:common=12:command=0:text=0
wolf:v1:database:BasicData/DataBase.Auto.txt:database=DataBase.Auto.txt:type=2:record=0:field=dataname
wolf:v1:basic:BasicData/Game.dat.Auto.txt:container=game_settings:field=GAME_TITLE_MAIN
```

## Stability and collision properties

- export root를 다른 drive/PC로 이동해도 source relative path와 ID는 같다.
- 같은 fixture를 같은 parser로 반복해서 읽으면 record order와 ID가 같다.
- 같은 원문이라도 source/event/page/command/text slot 중 하나가 다르면 ID가 다르다.
- path나 field에 space/colon/equal sign이 있어도 encoding되어 component boundary와
  충돌하지 않는다.
- 한 report 안 duplicate ID가 나오면 `WOLF_CANONICAL_ID_COLLISION` error로
  기록한다. 자동 suffix를 붙여 숨기지 않는다.

## Known limits

- event/page/command나 CSV record가 앞에서 삽입·삭제되면 순서 기반 component가
  이동할 수 있다.
- command block이 한 container/page에 여러 번 나타나는 구조는 충분히 검증되지
  않았다.
- database record의 native stable identifier가 CSV에 따로 존재하는지 아직
  확정하지 못했으므로 현재 `record_id`는 export 순서다.
- choice command label과 literal slot 의미는 실제 official fixture가 더 필요하다.
- schema가 바뀌면 기존 `v1`을 다른 의미로 재사용하지 않고 `v2` namespace를
  추가해야 한다.

따라서 0.7.2 report에는 `location_schema_version: 1`과
`location_schema_status: provisional`을 항상 함께 기록한다. 이 단계에서는
WOLF TranslationEntry/JSONL을 만들지 않으므로 provisional ID가 장기 번역 자산에
아직 사용되지는 않는다.

## GLT 0.7.3 review

0.7.3의 experimental JSONL extraction도 v1을 final로 승격하지 않는다. 기존 ID
직렬화는 바꾸지 않고 location JSON에 `logical_source`를 추가했다. 예를 들어
`MapData/Map001.mps.Auto.txt`는 향후 native route 후보
`Data/MapData/Map001.mps`로 표현된다. JSONL metadata의 `wolf_logical_source`도
같은 값을 사용한다.

이 값은 cross-route 설계 검토용이며 현재 canonical ID 계산에는 참여하지 않는다.

## GLT 0.7.4 review

Safe writer/QA를 추가했지만 v1 계산식은 변경하지 않았다. 현재 DB `record_id`가 CSV
row ordinal이라는 점은 reorder/insert/delete에 취약하며, 검증되지 않은 Choice branch
구조도 stable ID 설계에 반영할 수 없다. 따라서 `wolf:v1`은 계속 `provisional`이다.
0.7.4 source fingerprint는 다른 게임/version 오적용을 막는 preflight 정보이며
canonical ID 구성요소가 아니다. native `.dat`/`.mps` parser와 Text I/O route가 동일
object를 가리킨다는 cross-route 검증 후 v2를 별도로 제안한다.

## GLT 0.7.5 review

Official Editor integration report는 v1 ID를 기준으로 txtoutput/import/re-export record를
비교할 수 있게 되었지만 현재 환경에는 실제 Editor fixture가 없어 cross-route evidence를
확보하지 못했다. Choice는 experimental, DB row ordinal 위험은 그대로다. 따라서
`wolf:v1`, `schema_status=provisional`, `decision=keep v1`을 유지하며 v2 proposal은
실제 official fixture 및 native parser evidence 이후 재평가한다.
native parser, repeated official export와 DB reorder 안정성을 확인한 뒤 v1 유지 또는
새 `v2` 도입을 결정한다.
