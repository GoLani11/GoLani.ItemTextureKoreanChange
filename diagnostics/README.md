# 선택적 진단 도구

이 폴더의 도구는 품목별 `prepare → validate → package` 경로와 독립적입니다.
기본 설치나 패키징에서 자동 실행하지 않습니다.

## OCR 선별

전체 텍스처에서 문자가 있는 후보를 찾는 도구입니다. 품목별 한글화 결과의 승인 도구는 아닙니다.
별도 가상환경에 `requirements-ocr.txt`를 설치하고 `ocr_select.py doctor`로 모델 상태를 확인합니다.

```bat
py -3.11 -m venv workspace/toolchain/ocr-env
workspace\toolchain\ocr-env\Scripts\python.exe -m pip install -r diagnostics/requirements-ocr.txt
workspace\toolchain\ocr-env\Scripts\python.exe diagnostics/ocr_select.py doctor
workspace\toolchain\ocr-env\Scripts\python.exe diagnostics/ocr_select.py plan --input workspace/catalog/images --manifest workspace/catalog/manifest.json
```

PaddlePaddle의 별도 런타임과 모델 준비는 `doctor`가 안내합니다. 모델 다운로드는 실제 scan에
`--allow-model-download --execute`를 명시한 경우에만 수행합니다.
결과·캐시는 `workspace/ocr/` 아래에 보관합니다. 기존 캐시는 원본 폴더에 보존되어 있으며 자동 이관하지 않습니다.
상세 옵션은 각 명령의 `--help`에서 확인합니다.

## AssetPicker

`GoLani.AssetPicker/`는 게임에서 에셋을 조사하는 기존 BepInEx 플러그인입니다.
필요할 때만 별도로 빌드·설치합니다. 기본 모드 패키지에는 포함하지 않습니다.

```bat
dotnet build diagnostics/GoLani.AssetPicker/GoLani.AssetPicker.csproj -c Release -p:SptDir=D:/SPT
```

게임 버전별 플러그인 동작은 별도 확인이 필요합니다. 이번 핵심 작업 경로 검사는 플러그인을 실행하지 않습니다.
