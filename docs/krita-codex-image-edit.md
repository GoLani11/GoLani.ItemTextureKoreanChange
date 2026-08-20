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

1. RGBA/U8 문서를 열고 자유 선택·사각 선택 등으로 바꿀 영역을 선택해요.
2. 도커에 수정 지시를 적어요. 바뀔 것과 유지할 것을 함께 쓰면 안정적이에요.
3. `작업 폴더`를 정해요. 기본값은 문서 폴더 아래 `KritaCodexEdits`예요. Git 저장소 루트와
   `.git` 폴더는 사용할 수 없어요.
4. 처음 한 번 `연결 확인`으로 ChatGPT 로그인과 기본 `imagegen` 스킬을 확인해요.
   Windows에서는 요청 범위를 제한하는 elevated sandbox가 아직 준비되지 않았다면 UAC
   확인창이 한 번 나타나요. 취소하거나 준비에 실패하면 편집을 시작하지 않아요.
5. `선택 영역 수정`을 눌러 기다려요. App Server 프로세스는 도커가 살아 있는 동안 재사용돼요.
6. 결과는 `[검증 전 AI 미리보기] ...` 레이어로 생겨요. 마음에 들지 않으면 Ctrl+Z 한 번으로
   지워요.

## SPT 준비 작업 모드

SPT 텍스처에서는 파일과 선택 영역을 직접 찾지 않아도 돼요. 도커 상단 모드를
`SPT 준비 작업`으로 바꾸고 다음 순서를 사용해요.

1. `SPT 프로젝트`에서 이 저장소 루트를 선택하고 `새로고침`을 눌러요.
2. 품목을 고른 뒤 `SPT 원본·추천 선택 불러오기`를 눌러요.
3. `새로고침`은 UI가 멈추지 않도록 백그라운드에서 공식 `review_record.py`의 analysis 게이트,
   profile의 bundle/Texture2D identity와 원본·review·5종 마스크의 현재 SHA를 검사해요. 목록에는
   `생성 준비됨`, `analysis·마스크 갱신 필요`, `원본·기록 확인 필요`, `생성 예산 승인 필요`를
   실제 검사 결과에 맞춰 표시해요.
4. 여러 품목이 잠겨 있으면 `전체 준비 요청 기록`을 한 번 눌러요. 플러그인이 모든 잠긴 품목과
   현재 profile·inventory·review·원본·5종 마스크 SHA를
   `workspace/krita-spt/preparation-requests/<fingerprint>/request.json`에 묶어요. 그 경로의
   전체 준비 요청을 순서대로 처리해 달라고 Codex에 한 번만 말하면 돼요. Codex는 품목 하나씩
   원본을 시각 판독하고 공식 analysis와 마스크 계약을 각각 통과시켜야 하며, 요청 파일 자체는
   어떤 게이트도 통과시키지 않아요.
5. 준비된 품목은 불변 원본 Diffuse를 열고 현재 라벨 면의 `editable` 픽셀만 Krita 선택으로
   적용해요. `라벨 면` 목록을 바꾸면 같은 품목의 다음 연결 면으로 이동해요. 준비가 덜 된
   품목은 원본과, 현재 해시·계약을 확인할 수 있는 경우 기존 전체 `editable` 참고 선택까지
   열어 줘요. 이 참고 상태에서는 `선택 영역 수정`이 잠기므로 오래된 기록으로 생성할 수
   없고, 상태줄에 갱신할 analysis·마스크와 Codex 요청 문구가 표시돼요.
6. 추천 선택은 줄여도 되지만 검증된 panel `editable` 밖으로 넓힐 수 없어요. 확장이 필요하면
   Krita에서 우회하지 말고 분석·마스크 기록을 먼저 갱신해야 해요.
7. 확정 한국어는 `review.json`에서 구조적으로 고정돼요. 프롬프트 칸에는 이번 시도의 추가
   시각 보정만 적고 `선택 영역 수정`을 눌러요.
8. 회전된 문구는 기록된 `rotation_deg`의 역각으로 임시 작업 패널만 정방향화해 생성하고,
   생성 패치를 원본 각도로 되돌린 뒤 저장된 선택 마스크로 잘라 미리보기 레이어에 넣어요.
   원본 또는 최종 Texture2D 전체는 회전·리사이즈하지 않아요.
9. 원본 배율에서 결과를 보고 `이 결과 선택` 또는 `결과 제외`를 눌러요. 선택은
   `decision.json`에 현재 review/source/mask/generated SHA를 묶어 기록할 뿐 후보를 승인하거나
   stage하지 않아요.

SPT 생성 기록은
`workspace/krita-spt/<target-id>/<panel-id>/<request-id>/`에 저장돼요. 연결 면당 현재 review에
기록된 시도와 플러그인 시도를 합쳐 기본 2회까지만 허용해요. 두 번째도 부적합하거나 이미
예산을 쓴 품목은 자동 반복하지 않으며, 추가 생성은 사용자의 명시적 요청을 review 증거로
남긴 뒤 Codex 작업에서 열어야 해요.

전체 준비 요청에는 현재 review의 기록된 시도 횟수와 기본 예산 소진 여부도 포함돼요. 이 요청을
만들거나 처리하는 행위는 추가 생성 승인으로 간주하지 않고, 시도 횟수를 낮추거나 초기화하지도
않아요. 예산을 소진한 라벨 면은 사용자가 추가 시도를 명시적으로 승인하고 그 증거가 별도로
기록될 때까지 계속 잠겨 있어요.

`이 결과 선택` 뒤에도 별도 서비스가 아니라 저장소 파이프라인에서 실행하는 패널 OCR,
승인 문자 패치 분리, 공식 compositor, 후보 OCR·시각 비교와 D/N/G·밉·번들 게이트가 남아요.
플러그인 자체에는 OCR 실행 기능이나 OCR 상태 버튼이 없고, 사람의 시각 선택과 해시 고정
`decision.json`만 만들어요. 플러그인 외부의 필수 저장소 검증 순서는 그대로 유지돼요.

프롬프트 예시는 다음처럼 대상과 불변 조건을 짧게 적어요.

```text
선택한 빨간 머그잔만 무광 검정으로 바꿔줘.
원래 조명, 손잡이 모양, 그림자와 나무 책상 질감은 그대로 유지해줘.
```

## 안전 제한

- 원본 레이어를 수정하지 않고 새 paint layer만 추가해요.
- 생성 중 문서 픽셀 또는 선택이 바뀌면 결과 파일만 보존하고 자동 적용하지 않아요.
- 선택 밖 픽셀과 알파가 적용 전과 다르면 직전에 추가한 레이어 한 건을 Undo하고 오류로
  처리해요.
- 현재는 RGBA/U8의 비선형 sRGB 프로필만 지원해요. linear RGB, Display P3와 사용자 ICC는
  색 변환 경로가 없어 시작 전에 차단해요.
- 선택 안 원본 알파가 255가 아닌 픽셀이 하나라도 있으면 합성 알파의 의미가 달라질 수 있어
  시작 전에 차단해요.
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

일반 모드는 저장소 아래 SPT 문서를 계속 fail-closed로 차단해요. SPT 문서는 위 전용 모드에서
공식 analysis 검사와 5종 마스크 계약을 통과했을 때만 생성할 수 있어요. 결과 레이어는
`generated_panel`의 원본 배율 시각 검토용이며 곧바로 후보나 승인본으로 사용할 수 없어요.
선택한 결과도 플러그인 밖에서 패널 OCR·승인 문자 패치·공식 compositor·후보 OCR·별도 시각
비교를 그대로 거쳐야 해요.

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
