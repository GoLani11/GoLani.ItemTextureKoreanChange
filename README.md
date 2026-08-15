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
2. 품목별 작업 기록을 만들고 원본 OCR을 실행해요. 전체 배치는 완료 보고서의 입력 SHA와
   모델 서명이 같으면 안전하게 이어서 실행해요.

```bat
work\.venv\Scripts\python.exe .agents\skills\localize-spt-food-textures\scripts\review_record.py init mayo
work\.venv-ocr\Scripts\python.exe localize.py ocr run mayo --phase source
work\.venv-ocr\Scripts\python.exe localize.py ocr batch --phase source
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
5. AI 편집은 글자 배경 복구 초안에만 쓰고, 최종 글자는 해시 고정 마스크·글꼴·좌표 recipe로
   결정적으로 합성해요.

```bat
work\.venv\Scripts\python.exe localize.py compose mayo workspace\reviews\mayo\compose-recipe.json
```
6. 후보 OCR과 Codex 시각 비교를 모두 기록한 뒤 `stage`해요.

```bat
work\.venv-ocr\Scripts\python.exe localize.py ocr run mayo --phase candidate --image 후보.png
work\.venv\Scripts\python.exe localize.py candidate-check mayo
work\.venv\Scripts\python.exe localize.py stage mayo 후보.png
```

7. 모든 품목의 실제 D/N/G·밉·압축·번들·게임 렌더 기록까지 통과하면
   `2_적용.bat`이 해시 고정 release를 만들어요.
8. `3_배포.bat`은 그 release만 기존 설치 백업 후 배포해요.

실제 세부 필드와 중단 조건은
[저장소 전용 스킬](.agents/skills/localize-spt-food-textures/SKILL.md)과
[파이프라인 구조](docs/architecture-v2.md)에 있어요.

## 중요한 제한

- 전체 이미지를 재생성하거나 크기를 맞추기 위해 리사이즈하지 않아요.
- 예전 `work/2_edited`, 루트 `bundles/`, `tools/auto.py build/deploy` 결과는 새 release로
  간주하지 않아요.
- OCR 무검출은 “문자가 없음”의 증거가 아니며, OCR 결과만으로 승인하지 않아요.
- 현재 `workspace/approved`의 옛 결과는 마스크·이중 검토·재질 증거가 없으므로 새 게이트에서
  의도적으로 실패해요. 품목별로 다시 검증해야 해요.
- 원본보다 큰 4096 업스케일은 실제 디테일을 늘리지 않고 흐림·메모리 사용을 키워 사용하지 않아요.

소스 코드는 출처와 주소를 남기면 자유롭게 활용할 수 있어요.
