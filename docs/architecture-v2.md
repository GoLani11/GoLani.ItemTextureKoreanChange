# 음식 텍스처 현지화 파이프라인 v2

## 목표

SPT의 음식·음료 포장에서 정상 게임 화면에 보이는 제품명·브랜드·짧은 핵심 문구를 자연스러운
한국어로 바꾸되, 작은 원재료·법정표시·주소·영양·인증·바코드·날짜 인쇄와 원본 UV·알파·
재질·밉맵·UnityFS 구조는 보존한다. 이미지 생성은 시각 참고와 초안에 사용하고, 승인된 문구와
보존 조건은 결정적인 검증 단계로 통제한다.

게임 자산과 추출 PNG는 Git에 넣지 않는다. 저장소에는 코드, 대상 프로필, 번역 명세,
스킬과 합성·검증 규칙만 둔다.

## 구조

```text
localizer/golani_texture_localizer/  범용 Python 패키지
profiles/food/collection.json        SPT 4.0 음식 대상과 한국어 문구
.agents/skills/localize-spt-food-textures/  저장소 전용 Codex 실행 절차
tests/localizer/                     자산 독립 단위 테스트
workspace/                           원본·판독·시안·승인본·보고서·번들(Git 제외)
mod/                                 향후 C# 게임 적용 코드를 이관할 위치
```

기존 `tools/auto.py`, OCR 도구와 C# 프로젝트는 v2가 같은 결과를 낼 때까지 유지한다.
옛 파일을 먼저 삭제하지 않는다.

## 작업 흐름

1. `python localize.py --spt-root D:/SPT inventory`: Texture2D와 실제
   Renderer→Material→Mesh PPtr, UV0와 submesh 연결을 읽는다.
2. `python localize.py --spt-root D:/SPT extract`: 원본과 검증용 mesh를
   `workspace/source/`, `workspace/meshes/`에 추출한다.
3. Codex 비전 우선 판독: 원본 mip 0에서 편집 허용 목록의 문구만 뜻·좌표·방향과 글꼴 특징을
   확정한다. 제외한 미세 인쇄는 보호 bbox로 묶고, 대상 중 확대해도 모호한 영역에만 OCR을
   보조로 실행해 시각 판독과 교차검증한다.
4. 편집 명세: 영역별 원문·뜻·번역·좌표·방향을 기록하고 `uv-review`가 실제 target submesh의
   UV island 경계에서 만든 seam guard와 편집·보호 마스크를 확정한다.
5. 이미지 편집: 연결된 라벨 면을 한 호출에서 현지화하고 글꼴 인상·스타일·ink 크기·실루엣·
   기준선·간격·방향·효과를 원본에 잠근다. 선택한 문자 패치만 원본 좌표에 합성하며 고정 한글
   폰트 조판은 최종물로 사용하지 않는다.
6. 생성 후 검증: 결과 OCR과 별도 Codex 시각 비교로 누락·오자·중복·잔상, typography lock과
   비문자 구조 보존을 확인한다.
7. 보조맵 처리: 실제 Material 연결을 따라 원본 N/G identity·UV ST·공유 소비자를 고정한다.
   평면 인쇄는 byte 보존하고, 외국어 효과만 맵별 old-effect 안에서 제거한다. 보조맵 전체를
   생성하거나 binary 글자 마스크를 resize하지 않는다. 새 효과 producer가 구현되면 승인된
   `selected_lettering` 연속 알파 하나에서 UV 기준으로 절차 파생한다.
8. 밉·재패킹: 맵 역할별 밉과 원본 포맷을 유지해 동일 크기 payload만 교체한다.
9. 검증: 모든 밉, 압축 왕복, 번들 레이아웃과 실제 조명 렌더를 확인한다.
10. 설치: 모든 해시 고정 게이트 통과 후 별도 사용자 승인과 백업을 거쳐 적용한다.

## 완료 조건

- 현지화 대상 39개가 승인되고 외국어가 없는 3개가 보존 판정을 받는다.
- 모든 현지화 대상 원문이 Codex 시각 판독으로 기록되고 필요한 영역만 OCR과 교차검증했다.
- 제외한 미세 인쇄는 보호 영역에서 원본과 픽셀이 같다.
- 모든 승인본의 크기·비율·색 모드·알파가 원본과 같고 리사이즈 이력이 없다.
- 편집 마스크 밖, 보호 영역과 seam guard의 변경 픽셀이 0이다.
- 확정 한국어가 지정 횟수·방향으로 있고 편집 영역 안에 라틴·키릴 잔상이 없다.
- UV 섬, 이음선, 로고 도형, 그림 방향, 주름, 오염과 비문자 영역이 이동하지 않는다.
- 실제 Material의 D/N/G 연결과 공유 소비자를 확인했고 동일 글자 효과가 정렬된다.
- 사광·그림자·normal-only·gloss-only 렌더에서 편집 대상의 기존 외국어가 나타나지 않으며
  보호 미세 인쇄는 원본과 같다.
- 원본 텍스처 포맷·밉 수·stream path/offset/size가 같다.
- 역할별로 생성한 모든 밉에서 글자와 seam을 검사하고 ROI p95·p99 기준을 통과한다.
- UnityFS 디렉터리와 payload 밖 바이트가 원본과 같다.
- 재패킹 번들을 다시 열어 모든 대상 텍스처를 추출할 수 있다.
- SPT 클라이언트가 사용하는 로컬 경로에서 재패킹 번들의 전체 자산을 로드할 수 있다.
- 원본·판독·번역·마스크·후보·보조맵·밉·렌더·번들의 해시와 백업 기록이 있다.

`stage`는 작업 기록·마스크 SHA와 후보 SHA를 다시 계산하고 마스크 밖 변경을 실측한다.
`derive`는 actual Material graph, map identity·UV ST, source SHA와 source-base 계약이 기록과
같을 때만 명시된 보조맵 영역을 처리한다. `neutralize_and_derive`는 승인된
`selected_lettering` 연속 알파 하나를 동일한 양수 UV scale·동일 offset·U/V Repeat의 보조맵으로
2^n 정수 area 재래스터화한 뒤, Normal은 height/RNM/DXT5nm G·A로, Gloss는 검증된 선형 채널
delta로만 절차 파생한다. 다른 UV ST·wrap·비정수 해상도·다른 Diffuse를 함께 쓰는 공유 PPtr는
자동 차단한다. inventory schema 3은 `Windows.json`, target Diffuse와 그 Material에서
도달한 Normal/Gloss PPtr의 전체 역의존 후보 bundle, serialized assets file까지 포함한
Material identity와 source override의 raw Material PPtr/ST graph SHA를 고정한다. `repack`은 이 외부
graph가 현재 파일과
같은지 다시 확인하고, 승인·derived SHA가 오래되었거나 어떤 품목이라도 실패하면
중단한다. 전역 edge F1과 전체 평균 MAE는 참고 지표일 뿐 위 조건을 대신하지
않으며, 실제 렌더 기록까지 있는 작업 기록만
`release`로 승격한다.

## 현재 범위

SPT 4.0 데이터베이스의 Food/Drink 직접 하위 42개 레코드를 조사했다. 보드카 중복
레코드를 합치면 실제 제품은 41종이며, 공유·잔존 텍스처를 포함한 컬러 작업 대상은
42장이다. 이 중 39장은 현지화하고 외국어 인쇄가 없는 3장은 보존한다. 내용물이나
숟가락처럼 별도 인쇄가 없는 추가 diffuse 8장은 작업 대상에서 제외한다.
