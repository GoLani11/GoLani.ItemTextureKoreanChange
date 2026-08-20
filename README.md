# GoLani.ItemTextureKoreanChange

SPT/Escape from Tarkov 음식·음료 포장의 외국어 인쇄를 한국어로 현지화하는 모드예요.

이 저장소의 개발 파이프라인은 원본 해상도를 유지하고, OCR과 독립 시각 판독을 교차 검증한
뒤 허용한 문자 영역만 바꾸도록 설계돼 있어요. Diffuse만 보지 않고 실제 Unity Material이
연결한 Normal·Gloss와 모든 밉, 사광·그림자 렌더까지 검증되지 않으면 release를 만들지 않아요.

## 사용자 설치 경로

```text
SPT/SPT_Runtime/user/mods/GoLani-ItemTextureKoreanChange
```

적용 후 SPT 런처에서 임시 파일을 삭제해야 이전 bundle 캐시가 남지 않아요.

## 개발 환경 준비

Windows에서 `0_설치.bat`을 실행하면 다음 두 환경을 따로 준비해요.

- `work/.venv`: UnityFS 추출·검증·재패킹
- `work/.venv-ocr`: PaddleOCR 3.5.0, EasyOCR 1.7.2와 공식 모델

OCR은 정확도 우선 `PP-OCRv5_server_det` 검출기에 영문·키릴·한글 인식기를 나눠 적용하고,
EasyOCR을 두 번째 엔진으로 사용해요. 모델이 준비됐는지는 다음 명령으로 확인할 수 있어요.

```bat
work\.venv-ocr\Scripts\python.exe localize.py ocr doctor
```

SPT가 `D:\SPT`가 아니면 `SPT_DIR` 환경변수 또는 `--spt-root`를 지정해 주세요.

## 안전한 작업 흐름

1. `1_추출.bat`: 원본 Texture2D와 실제 Material 연결을 `workspace/`에 기록해요.
2. 품목별 작업 기록을 만들고 Codex가 원본을 먼저 판독해요. 확대해도 모호한 번역 대상만
   `needs_ocr_fallback: true`로 기록한 뒤 해당 ROI에만 원본 OCR을 실행해요. 보호 미세 인쇄나
   전체 Texture2D에는 OCR을 실행하지 않아요.

```bat
work\.venv\Scripts\python.exe .agents\skills\localize-spt-food-textures\scripts\review_record.py init mayo
work\.venv-ocr\Scripts\python.exe localize.py ocr run mayo --phase source
```

3. 실제 Renderer→Material→Mesh 연결에서 UV 경계를 추출하고, OCR 문자열을 숨긴 확대 시트로
   Codex가 원본을 독립 판독해요. 그 뒤 OCR과 교차 검증해 번역·좌표·방향·UV 면을 확정해요.

```bat
work\.venv\Scripts\python.exe localize.py uv-review
work\.venv\Scripts\python.exe localize.py visual-sheets
work\.venv-ocr\Scripts\python.exe localize.py ocr batch --phase candidate --reference-approved
work\.venv\Scripts\python.exe localize.py legacy-layout-sheets
```

`legacy-layout-sheets`의 오른쪽 이미지는 번역·크기·위치 제안만 비교하는 용도예요. 과거
한글본 픽셀은 후보나 배경으로 복사하지 않아요.

4. 원본 크기의 `old_text`, `new_text`, `editable`, `protected`, `seam_guard` 마스크를 만들어요.
   `localize.py mask <target-id> <mask-recipe.json>`으로 좌표·색 조건과 실제 UV seam을 묶어
   재현 가능한 `old_text` 마스크를 만들 수 있어요. Normal·Gloss에 외국어 효과가 있으면
   `output_stem`을 달리한 재질 전용 마스크를 만들고, Diffuse의 넓은 마스크를 재사용하지 않아요.
5. 원문 배경은 `old_text` 안에서만 복원하고 원본·마스크·복원 설정 SHA로 캐시해요. 이미지
   생성기는 연결된 라벨 면 전체를 한 번에 편집하지만, 최종 후보에는 OCR과 시각 검사를 통과한
   `selected_lettering` RGBA와 `lettering_mask`만 합성해요. 일반 한글 폰트 픽셀은 최종 후보에
   사용할 수 없어요. schema v2 recipe 형식은
   [비전 패널 합성 계약](docs/vision-panel-compositor.md)에 있어요.

```bat
work\.venv\Scripts\python.exe localize.py compose mayo workspace\reviews\mayo\compose-recipe-v2.json
```
6. 후보 OCR과 Codex 시각 비교를 모두 기록한 뒤 `stage`해요.

```bat
work\.venv-ocr\Scripts\python.exe localize.py ocr run mayo --phase candidate
work\.venv\Scripts\python.exe localize.py candidate-check mayo
work\.venv\Scripts\python.exe localize.py stage mayo 후보.png
```

7. Normal·Gloss는 원본 맵을 불변 base로 두고, 평면 인쇄는 보존하며 실제 외국어 재질 효과만
   맵 전용 마스크 안에서 제거해요. 한글에도 같은 효과가 필요하면 승인된
   `selected_lettering` 연속 알파 하나를 동일한 양수 UV scale·동일 offset·U/V Repeat의 보조맵
   해상도로 재래스터화해 Normal은 height/RNM, Gloss는 검증된 선형 채널 delta로 절차
   파생해요. 맵 identity·UV ST·source SHA와 승인 글자 SHA를 고정한 뒤 모든
   D/N/G·밉·압축·번들·게임 렌더 기록까지 통과하면 `2_적용.bat`이 release를
   만들어요.
   이 기능을 처음 사용하는 기존 작업은 `1_준비.bat`을 다시 실행해 inventory schema 3의
   `Windows.json`·Diffuse에서 도달한 모든 Normal/Gloss 역의존 bundle·raw PPtr/ST
   증거를 새로 만들어야 해요.
8. `3_배포.bat`은 그 release만 기존 설치 백업 후 배포해요.

실제 세부 필드와 중단 조건은
[저장소 전용 스킬](.agents/skills/localize-spt-food-textures/SKILL.md)과
[파이프라인 구조](docs/architecture-v2.md)에 있어요.

## 중요한 제한

- 전체 이미지를 재생성하거나 크기를 맞추기 위해 리사이즈하지 않아요.
- Normal·Gloss 전체를 이미지 생성하거나 맵마다 글자 위치를 다시 추측하지 않아요. 새 한글
  재질 효과는 동일한 양수 UV scale·동일 offset·U/V Repeat와 동일/2^n 축소를 검증한 v1 producer
  범위에서만 추가해요. 다른 UV, 비정수 축소와 서로 다른 Diffuse가 공유하는 보조맵은 자동
  차단해요.
- 예전 `work/2_edited`, 루트 `bundles/`, `tools/auto.py build/deploy` 결과는 새 release로
  간주하지 않아요.
- OCR 무검출은 “문자가 없음”의 증거가 아니며, OCR 결과만으로 승인하지 않아요.
- 후보 OCR은 승인된 번역 ROI를 임의 원문 각도의 역각으로 정방향화한 뒤 NFC 기준으로
  공백·줄바꿈·구두점·숫자·단위를 보존해 완전일치해야 해요. 부분일치는 통과하지 않아요.
- schema v1 고정 폰트 조판은 과거 배치 참고용 레거시이며 새 후보 게이트를 통과할 수 없어요.
- 현재 `workspace/approved`의 옛 결과는 마스크·이중 검토·재질 증거가 없으므로 새 게이트에서
  의도적으로 실패해요. 품목별로 다시 검증해야 해요.
- 원본보다 큰 4096 업스케일은 실제 디테일을 늘리지 않고 흐림·메모리 사용을 키워 사용하지 않아요.

소스 코드는 출처와 주소를 남기면 자유롭게 활용할 수 있어요.

## Krita + Codex 선택 영역 AI 편집

Krita에서 영역을 선택하고 한국어로 지시하면 Codex CLI의 기본 `$imagegen` 결과를 원래
마스크로 잘라 새 미리보기 레이어에 넣는 도구를 함께 제공해요. 설치·안전 제한과 SPT 후보
게이트가 연결되기 전의 사용 금지는 [Krita Codex 선택 영역 편집](docs/krita-codex-image-edit.md)에
있어요.
