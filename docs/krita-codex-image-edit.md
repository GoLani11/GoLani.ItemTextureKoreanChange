# Krita에서 Codex 기본 imagegen으로 선택 영역 편집하기

이 도구는 `선택 → 한국어 지시 → 새 미리보기 레이어` 흐름을 Krita 안에서 실행해요.
OpenAI API 키나 별도 이미지 API 코드는 쓰지 않고, 사용자가 ChatGPT 계정으로 로그인한
Codex CLI의 기본 `$imagegen` 스킬을 사용해요. 다만 완전 오프라인 기능은 아니며 생성은
Codex 사용량에 포함돼요.

검증 기준은 Codex CLI 0.148.0과 Krita 5.2/5.3의 Qt 5.14 이상 빌드·Krita 6(Qt6)의
RGBA/U8, `sRGB-elle-V2-srgbtrc.icc` 문서예요. Qt 5.12/5.13은 색공간을 확인할
`QColorSpace` API가 없어 지원하지 않아요.

## 동작 방식

1. 현재 Krita 선택의 정확한 0..255 마스크와 주변 문맥을 PNG로 저장해요.
2. 계속 실행 중인 `codex app-server`에 원본 crop과 흑백 마스크를 대화 이미지로 첨부하고,
   수정 지시와 시스템 `imagegen` 스킬 경로를 한 턴으로 보내요.
3. Codex가 기본 `gpt-image-2` 이미지 도구를 한 번 호출해요.
4. 결과 종횡비와 대기 중 문서·선택·문맥이 그대로인지 확인해요.
5. 생성 결과의 알파를 저장한 선택 마스크로 곱하고, 선택 밖 BGRA는 모두 0으로 만든 뒤
   detached paint layer에 기록해요.
6. 같은 색공간의 임시 Krita 문서에서 먼저 합성하고 선택 밖 변경 0px와 전체 알파 변경 0px를
   실측해요.
7. 실제 문서에는 숨긴 detached 레이어를 한 번만 붙인 뒤 표시·재검증해 Ctrl+Z 한 번으로
   되돌릴 수 있게 해요. 사후 검증이 실패하면 그 layer-add 한 건을 즉시 Undo해요.

Codex의 현재 기본 이미지 도구에는 모델 수준 `mask` 인자가 없어요. 흑백 마스크는 두 번째
시각 참조이고, 이미지 도구에는 경로 대신 이 턴에 첨부된 최근 이미지 2장을 사용하라고
지시해요. 선택 경계의 최종 보장은 5~7단계의 로컬 합성이 담당하므로 모델이 선택 밖을 다시
그려도 그 픽셀은 Krita 레이어에 들어오지 않아요.

## 준비

- Qt 5.14 이상으로 빌드된 Krita 5.2/5.3 또는 Krita 6과 Python Plugin 기능
- PATH에서 실행 가능한 Codex CLI 또는 `codex.cmd`의 전체 경로
- `codex login`으로 저장된 **ChatGPT 로그인**
- 이미지 생성이 허용된 ChatGPT 플랜·워크스페이스와 남은 Codex 사용량

플러그인은 자식 프로세스에서 `OPENAI_API_KEY`를 제거하고 공식 `openai` 공급자와
`forced_login_method="chatgpt"`를 강제한 뒤, `account/read` 결과가 `chatgpt`가 아니면
중단해요. 따라서 환경에 API 키나 사용자 모델 공급자 설정이 있어도 이 기능이 그 경로로
조용히 전환되지는 않아요.

Windows Krita는 Windows용 Codex CLI와 함께 쓰는 구성이 기본이에요. WSL 안의 Codex는
Windows 파일 경로와 스킬 경로를 그대로 읽을 수 없으므로 현재 버전에서는 지원하지 않아요.

## 패키지 만들기와 설치

저장소 루트에서 다음을 실행해요.

```bash
.venv/bin/python tools/krita_codex_image_edit/package.py
```

Windows에서 이 저장소의 v2 환경을 사용한다면 다음처럼 실행할 수 있어요.

```bat
work\.venv\Scripts\python.exe tools\krita_codex_image_edit\package.py
```

기본 출력은 `workspace/krita-codex/dist/golani-codex-image-edit.zip`이에요. 같은 파일을
교체할 때만 명시적으로 `--force`를 붙여요.

Krita에서 다음 순서로 설치해요.

1. `도구 → 스크립트 → Python 플러그인 가져오기`에서 만든 ZIP을 선택해요.
2. Krita를 재시작해요.
3. `설정 → Krita 설정 → Python 플러그인 관리자`에서 `Codex Selection AI Edit`를 켜요.
4. 다시 시작한 뒤 `설정 → 도커 → Codex 선택 영역 AI 편집`을 열어요.

이 작업에서는 소스와 ZIP만 만들며 사용자 Krita 폴더에는 자동 설치하지 않아요.

## 사용

`일반 생성형 채우기` 모드의 기본 순서는 다음과 같아요.

1. RGBA/U8 문서를 열고 자유 선택·사각 선택 등으로 바꿀 영역을 선택해요.
2. 도커에 수정 지시를 적어요. 바뀔 것과 유지할 것을 함께 쓰면 안정적이에요.
3. `작업 폴더`를 정해요. 기본값은 문서 폴더 아래 `KritaCodexEdits`예요. Git 저장소 루트와
   `.git` 폴더는 사용할 수 없어요.
4. 처음 한 번 `연결 확인`으로 ChatGPT 로그인과 기본 `imagegen` 스킬을 확인해요.
   Windows에서는 요청 범위를 제한하는 elevated sandbox가 아직 준비되지 않았다면 UAC
   확인창이 한 번 나타나요. 취소하거나 준비에 실패하면 편집을 시작하지 않아요.
5. `생성형 채우기`를 눌러 기다려요. 진행 중에 멈추려면 `생성 취소`를 눌러요.
   App Server 프로세스는 도커가 살아 있는 동안 재사용돼요.
6. 결과는 `[검증 전 AI 미리보기] ...` 레이어로 생겨요. 마음에 들지 않으면 Ctrl+Z 한 번으로
   지워요.

## SPT 자유 선택 편집 모드

SPT에서도 Photoshop의 생성형 채우기처럼 Krita의 현재 선택과 프롬프트로 원하는
부분을 자유롭게 미리보기할 수 있어요.

1. 도커 상단에서 `SPT 자유 선택 편집`을 고르고, `SPT 프로젝트`에서 이 저장소
   루트를 선택한 뒤 `새로고침`을 눌러요.
2. 새로고침은 profile의 bundle/Texture2D identity와 원본·review SHA, 공식 analysis와
   5종 마스크의 최종 검증 준비 상태를 백그라운드에서 확인해요. analysis·마스크
   상태는 정식 후보 승격을 위한 정보이며, 자유 선택 미리보기의 선택 권한이 아니에요.
3. 품목을 고른 뒤 `SPT RGB 작업 뷰 열기`를 눌러요. 플러그인은 불변 원본 Diffuse의
   RGB를 byte 그대로 복사하고 표시용 알파만 255로 고정한
   `workspace/krita-spt/view-sources/` 작업 뷰를 열어요. 원본 PNG와 재질 알파는
   수정하지 않고 SHA로 고정해요.
4. 자유 선택·사각 선택 등으로 원하는 영역을 직접 선택하고, 프롬프트 칸에 그 선택에
   적용할 전체 변경 내용을 적어요. `라벨 면` 목록, 자동 추천 선택, panel
   `editable` 마스크와 그 마스크의 subset 제한은 없어요. 사용자의 현재 Krita 선택과
   프롬프트만 해당 생성 시도의 편집 권한이에요.
5. `생성형 채우기`를 눌러요. 현재 선택의 0..255 마스크와 문맥 crop을 해시로 고정하고,
   결과는 그 선택으로 다시 잘라 별도의 `[SPT 자유 선택 AI 미리보기]` 레이어에만 넣어요.
   선택 밖 변경은 0px이어야 하며 작업 뷰의 알파도 바뀌지 않아야 해요.
6. 기존 미리보기 레이어를 남겨 둔 상태에서 다른 영역을 선택하고 다른 프롬프트로
   반복해도 돼요. 각 결과는 별도 레이어이므로 눈 아이콘으로 비교하거나 Ctrl+Z로 지울
   수 있어요. 생성 중 문서·선택·문맥이 바뀌면 낡은 결과를 자동 적용하지 않아요.
7. 각 결과와 요청 기록은 `free-selection-preview`이며 `candidate_approved: false`예요.
   원본·작업 뷰·imagegen 입력 crop·실제 선택 마스크·생성 결과 SHA를 남기지만, 이는
   정식 후보 승인이나 stage가 아니에요.

`최종 검증 준비 요청 만들기`는 현재 profile·inventory·review·원본·5종 마스크 SHA와
준비가 필요한 품목을 `workspace/krita-spt/preparation-requests/<fingerprint>/request.json`에
묶어 정식 검증 준비를 요청해요. 이 요청을 만들거나 처리하는 행위는 미리보기를
승인하거나 게이트를 통과시키지 않아요.

자유 선택 미리보기를 최종 stage에 사용하려면 저장소 전용 스킬의 기존 순서를 따라야 해요.
공식 analysis, 원본 크기의 `editable`·`old_text`·`new_text`·`protected`·`seam_guard`,
패널 OCR, 승인 문자 패치 분리, 공식 compositor, 후보 OCR·시각 비교와 D/N/G·밉·번들
게이트는 그대로 필수예요. 플러그인의 자유 선택은 이 필수 검증용 마스크를 바꾸거나 대체하지 않아요.

프롬프트 예시는 다음처럼 대상과 불변 조건을 짧게 적어요.

```text
선택한 빨간 머그잔만 무광 검정으로 바꿔줘.
원래 조명, 손잡이 모양, 그림자와 나무 책상 질감은 그대로 유지해줘.
```

## 안전 제한

- 범용 모드에서는 원본 레이어를 수정하지 않고 새 paint layer만 추가해요. SPT 모드에서는
  Git에 들어가지 않는 해시 고정 불투명 RGB 작업 뷰에만 레이어를 추가하고 불변 원본은 열거나
  저장 대상으로 삼지 않아요.
- 생성 중 문서 픽셀 또는 선택이 바뀌면 결과 파일만 보존하고 자동 적용하지 않아요.
- 선택 밖 픽셀과 알파가 적용 전과 다르면 직전에 추가한 레이어 한 건을 Undo하고 오류로
  처리해요.
- 현재는 RGBA/U8의 비선형 sRGB 프로필만 지원해요. Qt 작업 뷰의 embedded `sRGB`,
  `sRGB built-in`, `sRGB-elle-V2/V4-srgbtrc.icc`를 허용하고, linear RGB, Display P3와
  사용자 ICC는 색 변환 경로가 없어 시작 전에 차단해요.
- 범용 모드는 선택 안 원본 알파가 255가 아닌 픽셀이 하나라도 있으면 합성 의미가
  달라지므로 시작 전에 차단해요. SPT 모드는 `material` 알파를 원본에 고정하고
  RGB만 같은 불투명 작업 뷰에서 합성해요. 공식 compositor가 최종 후보의 알파를 원본에서
  byte 그대로 복원해요.
- 생성 결과 종횡비가 문맥 crop과 다르면 이미지를 늘이지 않고 차단해요.
- 문맥 crop은 최대 2048×2048 상당, 생성 PNG는 최대 16M 픽셀·100MiB로 제한해요.
- 입력 PNG에는 sRGB를 명시해요. 생성 PNG가 다른 유효 색공간이면 차단하고, 색공간 태그가
  없으면 Codex imagegen의 sRGB 출력으로 간주해요.
- App Server 프로세스에서 shell·unified exec·파일 변경·앱·플러그인·브라우저·멀티
  에이전트 기능을 끄고, 턴별로 사용자 MCP 설정 전체를 `enabled: false`로 덮은 뒤 실제 노출
  도구가 0개인지 확인해요. 이벤트에서 imagegen 이외 도구 호출이나 사용자 입력 요청이 보이면
  즉시 interrupt하고 연결을 닫아요.
- Codex 0.148.0의 실험적 권한 프로필을 요청마다 새 이름으로 만들어 사용자 설정과 병합되지
  않게 해요. 프로필에는 filesystem root 거부, 플랫폼 최소 경로와 해당 요청의 `source.png`,
  `mask.png` 읽기만 요청해요. App Server가 그 요청 전용 프로필을 실제로 활성화했다고
  응답하지 않으면 생성 전에 차단해요. Linux/WSL의 지원 sandbox에서는 이 경로 범위를
  집행해요.
- 기본 이미지 도구에는 `referenced_image_paths`를 넘기지 않고, 턴에 첨부된 최근 이미지
  2장만 `num_last_images_to_include=2`로 쓰라고 시스템·사용자 지시에 모두 고정해요. 현재
  App Server 완료 이벤트에는 실제 imagegen 인자가 노출되지 않아 이 조건은 명령 계층의
  방어이며, 암호학적으로 검증되는 경계는 아니에요.
- Native Windows에서는 App Server를 elevated sandbox 모드로 고정하고 준비 상태가 `ready`인지
  별도로 확인해요. 초기 UAC 준비도 임의 이름의 `:minimal` 전용 프로필로 실행해 이 플러그인이
  사용자 홈·작업 폴더 전체 읽기 ACL을 추가하지 않게 해요. 취소하거나 준비에 실패하면 넓은
  파일 권한으로 대체하지 않고 fail-closed로 중단해요.
- Native Windows의 sandbox 계정과 ACL은 다른 Codex 세션과 공유되고 지속될 수 있어요.
  Codex 0.148.0에서 filesystem root 거부는 과거에 추가된 넓은 읽기 allow ACL을 상쇄하지
  않으므로, 활성 권한 프로필만으로 “두 파일 외에는 OS 수준에서 절대 읽을 수 없다”고 보장할
  수 없어요. 민감한 파일에 강한 격리가 필요하면 Codex 전용 VM·Windows Sandbox·별도 머신을
  사용해야 해요. 별도 Windows 사용자 계정만으로는 고정 sandbox 그룹/계정이 공유돼 충분하지
  않아요.
- 위 제한은 같은 사용자 계정에서 실행되는 Codex 프로세스를 별도 VM으로 격리하는 기능은
  아니에요. Codex 자체의 설정·인증·시스템 스킬 로딩은 호스트에서 이뤄지고, 모델이 호출하는
  도구의 작업 파일 접근만 위 프로필로 제한해요.
- 생성 PNG와 요청 기록은 작업 폴더에 남지만 Base64 원문은 로그에 쓰지 않아요.

## SPT 텍스처에서의 위치

일반 모드는 저장소 아래 SPT 문서를 계속 fail-closed로 차단해요. SPT 원본은
`SPT 자유 선택 편집`에서 불투명 RGB 작업 뷰로만 불러와 편집해요. 결과는
`free-selection-preview`이고 `candidate_approved: false`이므로 곧바로 후보나 승인본이 될 수
없어요. 최종 stage 전에는 플러그인 밖에서 analysis·5종 마스크·패널 OCR·승인 문자
패치·공식 compositor·후보 OCR·시각 비교·D/N/G·밉·번들 게이트를 그대로 거쳐야 해요.

상세 게이트는 [저장소 전용 스킬](../.agents/skills/localize-spt-food-textures/SKILL.md)과
[비전 패널 합성 계약](vision-panel-compositor.md)을 따라요.

## 참고한 오픈소스

- [openai/codex](https://github.com/openai/codex): App Server JSONL 프로토콜과 기본 imagegen
  결과 이벤트의 기준이에요.
- [Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion): 선택 기반 Fill UX,
  문맥 crop, detached layer 후 attach하는 undo 패턴을 설계 참고로 삼았어요. GPL-3.0 코드는
  복사하지 않았어요.
- [cyyprezz/krita-codex-mcp](https://github.com/cyyprezz/krita-codex-mcp): Codex와 Krita를
  연결하는 초기 MIT 프로젝트로, 문서 동기화와 제한된 이미지 로딩 패턴을 참고했어요.

공식 인터페이스 설명은 [Codex App Server](https://learn.chatgpt.com/docs/app-server),
[Codex 이미지 생성](https://learn.chatgpt.com/docs/image-generation),
[Krita Python 플러그인 작성법](https://docs.krita.org/en/user_manual/python_scripting/krita_python_plugin_howto.html)을
참고해요.
