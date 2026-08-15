# 영어·러시아어 텍스처 OCR 선별 도구

이 도구는 모든 텍스처를 결과 폴더에 풀어놓는 대신, 이미 추출된 컬러 이미지를
스트리밍으로 검사하고 **라틴 문자 또는 키릴 문자가 의심되는 텍스처만** 남기기 위한
개발용 파이프라인이다.

현재 단계에서는 도구만 준비되어 있다. 게임 에셋 OCR 선별은 아직 실행하지 않았다.

## 안전 원칙

- `tools/auto.py extract`를 내부에서 호출하지 않는다. 기존 `tools/map.json`을 덮어쓰지 않는다.
- `plan`은 이미지 디코딩, OCR, 모델 다운로드, 파일 생성을 하지 않는다.
- `scan`은 `--execute`가 없으면 중단한다.
- manifest 또는 입력 누락이 있으면 중단하며, 의도한 누락만 `--allow-missing`으로 계속한다.
- 모델 다운로드는 `--allow-model-download`를 함께 준 경우에만 허용한다.
- OCR 오류나 엔진 누락은 `rejected`가 아니라 `processing.status=error`로 기록한다.
- 동일한 픽셀을 여러 맵·아이템이 참조하면 OCR은 한 번만 하고 모든 참조를 보존한다.
- 캐시, 모델, 실행 보고서는 Git에서 제외된 `work/ocr_selection/` 아래에 둔다.
- `materialize`도 `--execute` 없이는 실제 파일을 복사하지 않는다.

## 도구 구성

```text
tools/ocr_select.py                 CLI 진입점
tools/texture_ocr/                  OCR·캐시·보고서 구현
tools/texture_ocr/default_config.json
tools/requirements-ocr.txt
work/ocr_selection/
├─ cache.sqlite3                    중단 후 재개 가능한 raw OCR 캐시
├─ models/                          선택적 로컬 모델
├─ latest.json
└─ runs/<run_id>/
   ├─ run.json
   ├─ results.jsonl
   ├─ summary.csv
   ├─ report.html
   └─ previews/                     후보 이미지만 생성
```

`results.jsonl`과 `summary.csv`에는 재개·감사를 위해 정상 `rejected/skipped`도 기록하지만,
HTML 본문과 `previews/`에는 후보 및 오류 항목만 표시한다.

## OCR 조합

기본 설정은 다음 두 엔진을 순서대로 사용한다.

1. PaddleOCR 3.5.0
   - 검출: `PP-OCRv5_mobile_det`
   - 인식: `eslav_PP-OCRv5_mobile_rec`
   - 동슬라브어(러시아어 포함), 영어, 숫자 인식
2. EasyOCR 1.7.2
   - PaddleOCR에서 확정하지 못한 이미지만 보충 검사
   - `Reader(["ru", "en"])`, CRAFT + `cyrillic_g2`

두 프로젝트 모두 Apache-2.0이다.

- PaddleOCR 3.5 문서: <https://www.paddleocr.ai/v3.5.0/en/version3.x/pipeline_usage/OCR.html>
- PaddlePaddle 설치: <https://www.paddleocr.ai/v3.5.0/en/version3.x/paddlepaddle_installation.html>
- EasyOCR 문서: <https://www.jaided.ai/easyocr/documentation/>

## 격리 환경 준비

현재 WSL 기본 Python 3.14에는 필요한 패키지가 없고 OCR 프레임워크 휠 호환성도
불확실하다. 기존 전역 Python을 건드리지 말고 Python 3.11 또는 3.12 환경을
`work/.venv-ocr`에 만드는 것을 권장한다.

Windows CPU 예시:

```bat
py -3.12 -m venv work\.venv-ocr
work\.venv-ocr\Scripts\python -m pip install --upgrade pip
work\.venv-ocr\Scripts\python -m pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
work\.venv-ocr\Scripts\python -m pip install -r tools\requirements-ocr.txt
```

GPU는 드라이버와 CUDA 계열에 따라 PaddlePaddle 및 PyTorch 설치 명령이 달라진다.
공식 설치 안내에서 환경에 맞는 패키지를 선택한 뒤 `requirements-ocr.txt`를 설치한다.

설치 후 상태 확인:

```bat
work\.venv-ocr\Scripts\python tools\ocr_select.py doctor
```

`doctor`는 패키지를 import하거나 모델을 실행하지 않고 설치 버전과 로컬 모델 파일만
점검한다. 첫 OCR 실행에서 모델을 받게 하려면 반드시
`--allow-model-download`를 명시한다.

Python 3.11/3.12 표시는 호환성이 검증된 권장 환경이며 그 자체로 실행을 차단하지는
않는다. 대신 OCR 어댑터가 검증된 PaddleOCR/EasyOCR 버전과 다르면 `doctor`와
`scan`이 함께 불일치를 알린다. Paddle 모델은 단순히 폴더가 존재하는지만 보지 않고
`inference.pdiparams`와 `inference.json`(또는 구형 `inference.pdmodel`)을 확인한다.

EasyOCR 자동 다운로드와 v2 현지화 OCR은 공통 캐시인 `work/ocr-models/easyocr`를 사용한다.
PaddleOCR 자동 다운로드는 프레임워크 기본 캐시인 `~/.paddlex/official_models`를 쓸
수 있으며 `doctor`는 설정 폴더와 기본 캐시를 모두 확인한다. 실제 모델 파일 내용도
OCR 캐시 서명에 포함되므로 같은 이름의 weight가 바뀌면 과거 판정을 재사용하지 않는다.

## 사용 순서

### 1. 읽기 전용 계획

```bat
work\.venv-ocr\Scripts\python tools\ocr_select.py plan
```

기본 입력은 다음과 같다.

- 이미지: `work/1_raw/`
- manifest: `tools/map.json`

다른 추출 결과를 사용할 수 있다.

```bat
work\.venv-ocr\Scripts\python tools\ocr_select.py plan ^
  --input work\map_raw ^
  --manifest work\map_manifest.jsonl
```

### 2. 명시적 OCR 실행

처음에는 작은 교정 실행부터 권장한다.

```bat
work\.venv-ocr\Scripts\python tools\ocr_select.py scan ^
  --limit 20 ^
  --allow-model-download ^
  --execute
```

정상 완료 결과와 `rejected`도 캐시되므로 같은 설정·모델·픽셀은 다시 OCR하지 않는다.
OCR 오류는 캐시하지 않아 다음 실행에서 재시도된다. `--force`를 주면 정상 캐시도
무시한다.

`--limit`은 정렬된 입력의 앞부분에서 지정한 수의 고유 컬러 후보를 찾는 즉시
fingerprint/OCR 준비를 멈춘다. 따라서 교정 실행은 전체 맵 텍스처를 미리 디코딩하지
않지만, 뒤쪽에 있는 동일 이미지의 추가 참조는 그 교정 run에 포함되지 않을 수 있다.

누락을 허용한 실행은 누락 레코드 전체를 `run.json`의 `missing`에 보존한다.

### 3. 덱스 검수 큐

```bat
work\.venv-ocr\Scripts\python tools\ocr_select.py queue --json
```

기본 큐에는 `confirmed`, `probable`, `needs_review`가 들어간다. 각 항목에는 절대
미리보기 경로, OCR 문자열, 원본 참조가 있어 이미지 검수에 바로 사용할 수 있다.

### 4. 최종 결정 기록

```bat
work\.venv-ocr\Scripts\python tools\ocr_select.py review ^
  --asset-id tex_0123456789abcdef0123 ^
  --decision confirmed ^
  --reviewer dex ^
  --note "러시아어 경고문 확인"
```

결정을 지우려면 `--decision clear`를 사용한다. 검수 기록은 raw OCR 캐시와 분리되어
임계값이나 보고서를 바꿔도 유지된다.

### 5. 선택된 원본만 카탈로그로 복사

```bat
work\.venv-ocr\Scripts\python tools\ocr_select.py materialize ^
  --destination catalog ^
  --execute
```

manifest 메타데이터를 바탕으로 다음처럼 정리한다.

```text
catalog/
├─ maps/<map 또는 _shared>/
├─ items/<category 또는 _shared>/
└─ unknown/_unassigned/
```

동일 픽셀이 맵과 아이템 양쪽에서 쓰이면 두 카탈로그에 각각 배치한다. 알려진
`map`/`item` 참조와 `unknown` 참조가 섞인 경우에는 알려진 분류를 우선한다. 같은
종류 안에서 여러 맵이나 여러 아이템 분류에 공유되면 해당 종류의 `_shared`로 간다.

각 이미지 옆에는 전체 OCR 결과와 원본 참조를 담은 `.json`이 생성된다. Windows 예약
이름(`NUL`, `CON`, `COM1` 등), 금지문자, 경로 탈출과 파일명 충돌을 방어한다.
기존 카탈로그 파일은 기본적으로 보존하며, 정말 교체할 때만 `--overwrite`를 추가한다.
이미지 또는 sidecar 중 하나만 기록된 부분 완료 상태는 원본 해시와 에셋 ID를 확인한
뒤 다음 실행에서 누락된 파일만 복구한다.

## 범용 manifest

기존 `tools/map.json`의 `png`, `key`, `texture` 형식을 바로 읽는다. 맵 추출기는 다음
JSON 또는 JSONL 형식을 사용하면 된다.

```json
{
  "asset_id": "stable-source-id",
  "source": "work/map_raw/sign_001.png",
  "bundle_key": "assets/...",
  "texture_name": "shop_sign_d",
  "asset_type": "map",
  "groups": ["woods", "customs"],
  "scene_path": "Assets/Content/Locations/..."
}
```

`groups`가 여러 개인 맵·아이템은 materialize 때 `_shared`로 들어간다.

## 등급 규칙

- `confirmed`: 대상 문자 3자 이상이며 점수 0.80 이상, 또는 두 엔진의 유효한 합의
- `probable`: 대상 문자 2자 이상이며 점수 0.45 이상
- `needs_review`: 라틴/키릴 증거가 약하거나 detector-only 영역
- `rejected`: 모든 필요한 OCR이 정상 종료됐지만 라틴/키릴 증거 없음
- `error`: 이미지 또는 OCR 엔진 처리 실패
- `skipped`: 파일명/manifest상 노멀·광택·마스크 계열

점수 기준은 `default_config.json`을 직접 고치기보다 별도 override JSON을 만들어
`--config`로 전달한다. 현재 파이프라인은 확정 증거가 나오면 남은 회전·보조 엔진을
생략하는 적응형 실행이므로, 분류 임계값도 캐시 키에 포함된다. 임계값을 바꾸면 기존
캐시를 잘못 재해석하지 않고 새 OCR 실행 대상으로 취급한다.

## 테스트

OCR 라이브러리 없이도 순수 로직 테스트가 동작한다.

```bat
python -m unittest discover -s tests -p "test_texture_ocr_*.py" -v
```

테스트는 실제 게임 이미지, OCR 모델, `work/1_raw`를 읽지 않는다.
