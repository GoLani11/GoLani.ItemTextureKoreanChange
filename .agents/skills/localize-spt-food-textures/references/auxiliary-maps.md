# 보조맵 원본 보존·절차 파생

Normal·Gloss는 생성 이미지가 아니라 셰이더 입력 데이터다. Diffuse처럼 각각 다시 생성하면
글자 형상·좌표가 달라지고, 원래 주름·스크래치·UV seam과 채널 packing도 손상된다. 원본
보조맵을 불변 base로 두고, 실제로 존재하는 문자 재질 효과만 국소 처리한다.

## 목차

- [불변 조건](#불변-조건)
- [정책 선택](#정책-선택)
- [유일한 글자 형상](#유일한-글자-형상)
- [원문 효과 제거와 base 캐시](#원문-효과-제거와-base-캐시)
- [새 효과의 절차 파생](#새-효과의-절차-파생)
- [공유 맵과 구현 경계](#공유-맵과-구현-경계)
- [검증 기록](#검증-기록)

## 불변 조건

- 보조맵 전체를 이미지 생성 모델로 새로 만들지 않는다.
- 원본 Normal·Gloss는 참고 이미지가 아니라 불변 base다.
- 평면 인쇄는 보조맵 payload를 그대로 보존한다.
- 변경은 맵별 old-effect 마스크와 파생 effect 마스크 안에서만 허용한다.
- Diffuse의 binary `new_text`를 단순 resize해 보조맵 형상으로 사용하지 않는다.
- Normal·Gloss를 sRGB 색 이미지처럼 처리하지 않는다.
- 확인하지 않은 Gloss/Spec 채널을 추측해 변경하지 않는다.
- 보조맵의 공유 소비자, shader/property, texture PPtr, UV scale/offset을 먼저 고정한다.

## 정책 선택

| 관찰된 원문 재질 | 정책 | 처리 |
| --- | --- | --- |
| 평면 잉크 | `preserve` | N/G 원본 byte 보존 |
| 외국어 요철·광택만 제거 | `neutralize_old_text` | 맵 전용 old-effect 안에서만 중립화 |
| 한글에도 양각·음각·광택 필요 | `neutralize_and_derive` | 원문 효과를 제거한 base 위에 동일 master alpha로 절차 파생 |

원문 효과 존재 여부는 정면·사광·normal-only·gloss-only 비교로 판정한다. 불확실하면
`preserve`로 두고 `review`한다. 색이 달라 보인다는 이유만으로 N/G를 수정하지 않는다.

## 유일한 글자 형상

`edit_plan.data.compositor.regions[*].selected_lettering`의 연속 알파를 유일한 master geometry로
사용한다. `lettering_mask` SHA도 함께 묶어 승인된 support와 일치함을 증명한다. 글자 위치를
보조맵에서 다시 OCR하거나 생성 모델이 추측하게 하지 않는다.

각 맵에는 master alpha를 Mesh UV로 투영한 뒤 texel-center에서 다시 래스터화한다.

- Diffuse와 보조맵 크기가 달라도 이미지 resize 대신 UV에서 다시 샘플링한다.
- 연속 알파는 축소 시 area resampling하거나 같은 SDF를 target 해상도에서 평가한다.
- PNG 좌상단과 Unity UV의 V축 변환, 각 슬롯의 scale/offset을 명시한다.
- 중심 오차는 보조맵 기준 0.5 texel 이하, bbox 양자화 오차는 1 texel 이하, 회전 오차는
  0°여야 한다.
- 외곽선·그림자를 물리적 요철로 만들지 않을 때는 fill/outline/shadow submask를 별도 SHA로
  고정하고 fill만 파생한다.

## 원문 효과 제거와 base 캐시

원문 relief·광택의 폭은 Diffuse 잉크 폭과 다를 수 있으므로 각 보조맵에 전용 old-effect
마스크를 만든다. Diffuse `old_text`를 그대로 재사용하지 않는다. 마스크는 protected와
seam guard를 침범하면 안 된다.

`source map SHA + old-effect mask SHA + 제거 방식/설정 + patch SHA`를 canonical JSON으로
직렬화해 base-cache fingerprint를 만든다. 같은 fingerprint의 중립 base는 재사용해 글자 시안
반복 때 제거를 다시 실행하지 않는다. `patch` 방식은 patch 파일의 현재 SHA를 검증한다.
`inpaint` 방식은 알고리즘·버전·radius를 fingerprint에 포함한다.

현재 producer의 `neutralization_signature`는 patch면 `patch-copy:v1`, inpaint면 target 맵 크기로
계산한 `opencv-telea:v1:radius=<n>`이어야 한다. fingerprint는 mode, map identity, source-map SHA,
old-effect mask SHA, method, patch SHA와 이 signature를 정렬된 compact JSON으로 묶은 SHA-256이다.

## 새 효과의 절차 파생

새 효과는 이미지 생성이 아니라 수치 연산으로 만든다.

- 양각·음각: master coverage 또는 SDF에서 height/bevel을 만들고, UV 미분으로 tangent normal을
  계산한다. 원본 normal과 RNM 같은 검증된 벡터 합성을 한 뒤 정규화하고 원래 packing으로
  되돌린다. 이 저장소에서 확인된 DXT5nm는 X=A, Y=G이며 수정 계약은
  `packing: "dxt5nm-x-a-y-g"`, `used_channels: ["G", "A"]`로 기록한다.
- spot gloss·matte: 셰이더가 사용하는 scalar 채널을 확인하고 원문 효과 ROI와 인접 배경의
  차이를 측정해 같은 delta만 master alpha에 적용한다.
- metallic foil: shader가 metallic/specular를 실제 사용한다는 증거가 있을 때만 해당 채널을
  함께 수정한다.

효과 강도·bevel·blur·부호와 algorithm signature를 기록한다. 사용하지 않는 채널과 effect
마스크 밖 픽셀은 원본과 같아야 한다. Normal은 벡터 평균 후 밉마다 재정규화하고, Gloss는
확인된 선형 scalar만 area 평균한다.

## 공유 맵과 구현 경계

동일 Texture PPtr의 모든 Material 소비자를 역참조한다. 같은 맵을 쓰는 소비자들이 서로 다른
한글 형상·효과를 요구하면 한 payload로 해결할 수 없으므로 자동 파생을 중단한다. Texture2D
복제와 Material PPtr rebinding은 별도 명시 작업 없이는 수행하지 않는다.

공유 PPtr의 소비자별 property·role·UV ST·policy·old-effect mask·channel contract를 모두
기록한다. 하나라도 다르면 동일한 결정적 연산임을 증명하지 못한 것으로 보고 중단한다. 같은
PPtr가 Normal과 Gloss 역할에 동시에 연결되거나 diffuse 등 다른 역할에도 연결되면 자동
derive하지 않는다. `preserve`만은 payload를 바꾸지 않으므로 디자인·ST 충돌과 무관하게 안전하다.

현재 `localize.py derive`의 `neutralize_and_derive` v1은 같은 Material의 Diffuse/보조맵 UV
scale이 동일한 양수이고 offset도 같으며, 보조맵이 동일 크기 또는 같은 종횡비의 2^n
축소이고 양쪽 U/V wrap이 Repeat일 때만 처리한다. 연속 알파는 정수 block-area 평균으로 한 번
투영하고, 경계의 Normal 미분도 Repeat로 연결한다. 다른
ST·비정수/비등방 해상도·다른 Diffuse target을 공유하는 PPtr는 mesh triangle 충돌을 증명할
수 없으므로 `block`한다. 수동 생성 Normal/Gloss로 이 제한을 우회하지 않는다.

## 검증 기록

`material_validation.data.auxiliary_contract`에 `schema_version: 1`과 다음을 기록한다.

- `mode: "source-base+master-lettering-alpha-v1"`
- `master_geometry: "selected-lettering-continuous-alpha"`
- `whole_map_generation_used: false`, `binary_new_text_resampled: false`
- 영역별 `selected_lettering_sha256`, `lettering_mask_sha256`
- 맵별 bundle/path ID/texture/role/크기/포맷/UV ST, source-map 경로·SHA, policy와 공유 효과 호환 여부
- 변경 맵의 channel semantics 확인 방식·증거 SHA, packing·사용 채널과 neutralization signature
- old-effect mask SHA와 base-cache fingerprint
- `neutralize_and_derive`의 `derivation` v1: producer signature, 전체 master region ID,
  `physical_component`, Diffuse/target 크기와 양쪽 UV ST, V축·texel-center·area projection,
  hash-pinned 효과 측정, 역할별 파라미터와 중심 0.5/bbox 1 texel/회전 0° 허용치

`effect_measurement`는 임의 이미지나 메모가 아니라 UTF-8 JSON이어야 한다. `schema_version: 1`,
role, source-map SHA, old-effect mask SHA, 1 이상의 `sample_count`, 그리고 계약의
`effect_parameters`와 byte-for-byte 같은 `measured_parameters`를 기록한다. Normal은
`method: controlled-lighting-fit`, Gloss는 `method: source-effect-sampling`만 허용한다.

재질 사전 게이트는 위 입력·정책·증거를 검사한다. `derive`는 schema 3 manifest에 중립 base,
projected master alpha와 effect mask SHA, 마스크 밖 변경 0, packing·정규화, untouched-channel
변경 수와 영역별 중심·bbox·회전 오차를 기록하고 `repack`이 다시 검사한다. release는 현재
review SHA와 manifest·출력 SHA가 일치할 때만 진행하고 repack 보고서가 derived manifest SHA를
release·배포까지 고정한다. 변경하지 않은 채널은 같은 source mip 0의 no-op mip chain과
pre-compression 값이 같아야 한다. 압축 뒤에는 effect 마스크와 교차하는 4×4 BC 블록을 별도
허용 영역으로 보고, 그 밖의 블록과 seam guard 오차를 검사한다.

inventory schema 3은 `Windows.json` SHA, target Diffuse와 그 Material에서 직접 도달한
Normal/Gloss PPtr를 역의존하는 모든 Material 후보 bundle SHA, serialized assets file을
포함한 Material identity와 각 source override의 raw Material PPtr/ST graph SHA를 기록한다.
재패킹은 이를 현재 파일에서 다시 계산해 간접 공유 소비자·format·wrap·ST가
생겼거나 override graph가 달라지면 중단한다. 한 bundle의 서로 다른 serialized
assets file에서 Texture2D path ID가 충돌하면 오연결을 추측하지 않고 `block`한다.
