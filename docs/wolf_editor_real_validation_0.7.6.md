# GLT 0.7.6 WOLF real Editor validation evidence

## Scope

사용자가 제공한 local WOLF RPG Editor sample을 원본 밖 임시 workspace로 복사한 뒤
복사본의 Editor만 실행했다. 원본 경로와 실제 game text/binary는 이 문서와 Git에
포함하지 않는다. 모든 hash와 file path는 portable evidence다.

## Editor and source evidence

- Editor product: WOLF RPG Editor
- PE fixed file version: `3.682.2026.224`
- Editor SHA-256: `3d9d9067c100b581c5bd6ff5ef40f736aae9cc020e62dc8a4a1b4617dd39fa54`
- Original project fingerprint: `7067fda6157af6223a9e14754a59d6299f8209cd8e6f47e68e84fa538292ba3f`
- Original project: 707 files, 41,209,710 bytes
- Before/after project fingerprint: identical

Version은 PE fixed file info에서 읽었으며 파일명이나 release note로 추측하지 않았다.

## Observed failure and minimal fix

0.7.5 subprocess wrapper의 anonymous stdout/stderr PIPE를 사용한 실제 BASIC/ALL export는
완성 파일을 일부 만들고도 timeout 또는 `0xC000013A`로 종료했다. 동일 격리 runtime을
PIPE 없이 기다리면 BASIC 6개 파일이 exit code 0으로 완성되었고, file-backed temporary
capture에서도 같은 결과를 재현했다.

따라서 command, timeout, no-`-wait`, shell policy는 바꾸지 않고 stdout/stderr capture만
anonymous PIPE에서 temporary file로 변경했다. Report에는 계속 byte count와 SHA-256만
남기며 출력 내용은 저장하지 않는다.

## Successful ALL export

- Editor invocations: 11
- Exit codes: all 0
- Auto.txt files: 10 (BasicData 6, MapData 4)
- Export fingerprint: `80c22c4a8d9e1138292d2f5ec2cf1b477de5810d3af835007afcf69b32f77ba6`
- Encoding: UTF-8, no BOM
- Newline: CRLF
- Final newline: yes
- Parsed records: 3,941
- Verified JSONL entries: 664
- Parser warnings: 224
- Parser errors: 0

Warnings는 command human-text alignment 180개와 malformed DB CSV row 44개다. 경고를
숨기지 않았으며 verified field만 추출했다.

## Command and field evidence

- code 101 verified message records: 250
- code 102 choice option records: 43 (15 declarations)
- nested code 102 option records: 4
- DATANAME records: 952
- experimental DB string cells: 2,694
- control/special-token records: 80

실제 choice block에서 code 102 option declaration, code 401 branch start와 code 499 end를
관찰했다. Option order/location과 branch 비변경은 official round-trip으로 확인했지만
cancel/default parameter 의미는 검증하지 않았다. DATANAME 외 DB field allowlist는
확대하지 않았다.

## Round-trip results

| Trial | Editor input/re-export | Semantic | Byte | Re-export transport |
|---|---|---|---|---|
| Direct no-op | success | equal | equal | UTF-8 no BOM, CRLF |
| GLT no-op | success | equal | equal | UTF-8 no BOM, CRLF |
| Source encoding | success | equal | normalized | UTF-8 no BOM, CRLF |
| UTF-8 BOM input | success | equal | normalized | UTF-8 no BOM, CRLF |
| UTF-8 no-BOM input | success | equal | normalized | UTF-8 no BOM, CRLF |

각 Korean trial은 dialogue/control, choice, DATANAME 중 최대 3개만 변경했다. 일반 text는
`GLT 0.7.6 한국어 왕복 테스트입니다.`, DATANAME은 `검, 대형`을 사용했다.

- Korean exact semantic preservation: verified
- U+FFFD: not found
- `???` replacement: not found
- Control token order/value: preserved
- `검<<COMMA>> 대형` transport and logical `검, 대형`: verified
- Choice option translation and order: verified

Fixture source가 이미 UTF-8 Korean이므로 CP932→Korean 전환은 `NOT VERIFIED`다. UTF-8
BOM input은 accepted되지만 Editor re-export가 no-BOM으로 normalization한다.

## Status classification

- VERIFIED: Editor detection, MAP/BASIC/ALL txtoutput, direct no-op, GLT no-op,
  Korean source/BOM/no-BOM import/re-export, control preservation, DATANAME COMMA
- PARTIALLY VERIFIED: Choice (option/ordering/branch separation; cancel/default 제외),
  DB (DATANAME만)
- NOT VERIFIED: CP932→Korean, DB description/help/path allowlist, DB row identity,
  native `.dat`/`.mps` cross-route identity
- FAILED: none after the file-backed capture fix

Canonical `wolf:v1`은 계속 `provisional`이다.
