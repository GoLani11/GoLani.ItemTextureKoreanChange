from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
PYKRITA = ROOT / "tools/krita_codex_image_edit/pykrita"
if str(PYKRITA) not in sys.path:
    sys.path.insert(0, str(PYKRITA))

from golani_codex_image_edit import app_server  # noqa: E402
from golani_codex_image_edit.app_server import (  # noqa: E402
    AppServerError,
    AppServerTimeout,
    ChatGptLoginRequired,
    CodexAppServer,
    GenerationResult,
    ImageGenerationFailed,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF"
    "gAJ/l9n0AAAAAElFTkSuQmCC"
)
HISTORY_IMAGE_PROMPT = (
    "$imagegen edit the two attached images with num_last_images_to_include=2. "
    "Never pass referenced_image_paths"
)


FAKE_SERVER = r'''
import base64
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
log_path = sys.argv[2]
windows_ready = mode == "windows-ready"
png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAFgAJ/l9n0AAAAAElFTkSuQmCC"

def send(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(str(method) + "\n")
    if method == "initialized":
        continue
    if method == "initialize":
        assert message["params"]["capabilities"]["experimentalApi"] is True
        windows_mode = mode.startswith("windows-")
        send({"id": request_id, "result": {
            "userAgent": "fake-codex/0.147.0",
            "platformFamily": "windows" if windows_mode else "unix",
            "platformOs": "windows" if windows_mode else "linux"
        }})
    elif method == "windowsSandbox/readiness":
        assert message["params"] is None
        send({"id": request_id, "result": {
            "status": "ready" if windows_ready else "notConfigured"
        }})
    elif method == "windowsSandbox/setupStart":
        assert message["params"]["mode"] == "elevated"
        assert Path(message["params"]["cwd"]).is_absolute()
        send({"id": request_id, "result": {"started": True}})
        success = mode != "windows-setup-fail"
        if success:
            windows_ready = True
        send({"method": "windowsSandbox/setupCompleted", "params": {
            "mode": "elevated", "success": success,
            "error": None if success else "user cancelled"
        }})
    elif method == "account/read":
        uses_api_key = mode == "api-key" or (
            mode == "env-check" and bool(os.environ.get("OPENAI_API_KEY"))
        )
        account = {"type": "apiKey"} if uses_api_key else {
            "type": "chatgpt", "email": None, "planType": "plus"
        }
        send({"id": request_id, "result": {"account": account, "requiresOpenaiAuth": True}})
    elif method == "skills/list":
        cwd = message["params"]["cwds"][0]
        send({"id": request_id, "result": {"data": [{
            "cwd": cwd,
            "errors": [],
            "skills": [{
                "name": "imagegen",
                "path": "/fake/repo/imagegen/SKILL.md",
                "description": "untrusted repo shadow",
                "enabled": True,
                "scope": "repo"
            }, {
                "name": "imagegen",
                "path": "/fake/imagegen/SKILL.md",
                "description": "fake",
                "enabled": True,
                "scope": "system"
            }]
        }]}})
    elif method == "config/read":
        send({"id": request_id, "result": {"config": {"mcp_servers": {}}}})
    elif method == "mcpServerStatus/list":
        send({"id": request_id, "result": {"data": [], "nextCursor": None}})
    elif method == "thread/start":
        if mode == "slow-thread-start":
            time.sleep(0.5)
        assert message["params"]["ephemeral"] is True
        assert message["params"]["modelProvider"] == "openai"
        profile_id = message["params"]["permissions"]
        assert profile_id.startswith("krita-imagegen-scoped-read-")
        assert "sandbox" not in message["params"]
        assert message["params"]["approvalPolicy"] == "never"
        assert "single-purpose raster image editor" in message["params"]["baseInstructions"]
        assert message["params"]["config"]["web_search"] == "disabled"
        assert message["params"]["config"]["openai_base_url"] == ""
        assert message["params"]["config"]["mcp_servers"] == {}
        profile = message["params"]["config"]["permissions"][profile_id]
        readable = profile["filesystem"]
        assert readable[":root"] == "deny"
        assert readable[":minimal"] == "read"
        assert sorted(
            Path(path).name for path in readable if path not in {":root", ":minimal"}
        ) == [
            "mask.png",
            "source.png",
        ]
        assert profile["network"]["enabled"] is False
        send({"id": request_id, "result": {
            "thread": {"id": "thread-test"},
            "modelProvider": "openai",
            "activePermissionProfile": {"id": profile_id, "extends": None}
        }})
    elif method == "turn/start":
        inputs = message["params"]["input"]
        kinds = [item["type"] for item in inputs]
        assert kinds == ["text", "skill", "localImage", "localImage"]
        assert inputs[0]["text"].startswith("$imagegen")
        assert inputs[1]["name"] == "imagegen"
        assert inputs[1]["path"] == "/fake/imagegen/SKILL.md"
        assert inputs[2]["path"] not in inputs[0]["text"]
        assert inputs[3]["path"] not in inputs[0]["text"]
        assert "num_last_images_to_include=2" in inputs[0]["text"]
        assert "Never pass referenced_image_paths" in inputs[0]["text"]
        send({"id": request_id, "result": {"turn": {
            "id": "turn-test", "status": "inProgress", "items": [], "error": None
        }}})
        send({"method": "item/started", "params": {
            "threadId": "thread-test", "turnId": "turn-test", "completedAtMs": 1,
            "item": {"type": "imageGeneration", "id": "image-test", "status": "in_progress", "result": ""}
        }})
        send({"method": "item/completed", "params": {
            "threadId": "thread-test", "turnId": "turn-test", "completedAtMs": 2,
            "item": {
                "type": "imageGeneration", "id": "image-test", "status": "completed",
                "result": png, "savedPath": None, "revisedPrompt": "fake revised"
            }
        }})
        if mode == "multiple":
            send({"method": "item/completed", "params": {
                "threadId": "thread-test", "turnId": "turn-test", "completedAtMs": 3,
                "item": {
                    "type": "imageGeneration", "id": "image-test-2", "status": "completed",
                    "result": png, "savedPath": None
                }
            }})
        if mode == "forbidden":
            send({"method": "item/started", "params": {
                "threadId": "thread-test", "turnId": "turn-test", "completedAtMs": 3,
                "item": {"type": "commandExecution", "id": "command-test", "status": "inProgress"}
            }})
        if mode == "plan":
            send({"method": "item/completed", "params": {
                "threadId": "thread-test", "turnId": "turn-test", "completedAtMs": 3,
                "item": {"type": "plan", "id": "plan-test", "text": "not allowed"}
            }})
        if mode == "request-input":
            send({
                "id": 9001,
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thread-test", "turnId": "turn-test", "itemId": "ask"}
            })
        if mode == "timeout":
            continue
        send({"method": "turn/completed", "params": {
            "threadId": "thread-test",
            "turn": {"id": "turn-test", "status": "completed", "items": [], "error": None}
        }})
    elif method == "turn/interrupt":
        send({"id": request_id, "result": {}})
    elif method == "thread/unsubscribe":
        if mode == "slow-unsubscribe":
            time.sleep(0.25)
        send({"id": request_id, "result": {}})
    elif method is None:
        continue
    else:
        send({"id": request_id, "error": {"code": -32601, "message": method}})
'''


def _fake_command(tmp_path: Path, mode: str = "chatgpt") -> list[str]:
    script = tmp_path / "fake_app_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return [sys.executable, "-u", str(script), mode, str(tmp_path / "server.log")]


def test_production_command_forces_chatgpt_and_windows_elevated_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_server.shutil, "which", lambda _name: "C:/Tools/codex.exe")
    monkeypatch.setattr(app_server, "_is_native_windows", lambda: True)

    command = app_server._production_command("codex")

    assert 'forced_login_method="chatgpt"' in command
    assert 'windows.sandbox="elevated"' in command
    assert "view_image" in command
    config_values = [command[index + 1] for index, value in enumerate(command) if value == "-c"]
    default_value = next(
        value for value in config_values if value.startswith("default_permissions=")
    )
    bootstrap_id = default_value.split('"', 2)[1]
    assert bootstrap_id.startswith("krita-imagegen-scoped-read-bootstrap-")
    assert (
        f'permissions.{bootstrap_id}.filesystem={{":root"="deny",":minimal"="read"}}'
        in config_values
    )
    assert f"permissions.{bootstrap_id}.network.enabled=false" in config_values


def test_app_server_runs_imagegen_turn_and_materializes_base64(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    mask = tmp_path / "mask.png"
    source.write_bytes(PNG_1X1)
    mask.write_bytes(PNG_1X1)
    progress: list[str] = []
    client = CodexAppServer(
        server_command=_fake_command(tmp_path),
        request_timeout=3,
        generation_timeout=3,
    )
    try:
        result = client.generate_edit(
            cwd=tmp_path,
            prompt=HISTORY_IMAGE_PROMPT,
            source_path=source,
            mask_path=mask,
            progress=progress.append,
        )
        output = result.write_image(tmp_path / "generated.png")
    finally:
        client.close()

    assert output.read_bytes() == PNG_1X1
    assert result.saved_path is None
    assert result.revised_prompt == "fake revised"
    assert any("ChatGPT plus" in message for message in progress)
    assert any("이미지 생성 완료" in message for message in progress)


def test_app_server_refuses_api_key_login_mode(tmp_path: Path) -> None:
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, "api-key"),
        request_timeout=3,
    )
    try:
        with pytest.raises(ChatGptLoginRequired, match="ChatGPT 로그인"):
            client.start(tmp_path)
    finally:
        client.close()


def test_terminal_shutdown_prevents_late_worker_restart(tmp_path: Path) -> None:
    client = CodexAppServer(
        server_command=_fake_command(tmp_path),
        request_timeout=3,
    )
    client.check_connection(tmp_path)
    client.shutdown()

    with pytest.raises(AppServerError, match="다시 시작할 수 없어요"):
        client.check_connection(tmp_path)

    methods = (tmp_path / "server.log").read_text(encoding="utf-8").splitlines()
    assert methods.count("initialize") == 1


def test_app_server_removes_api_key_from_child_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-must-not-reach-child")
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, "env-check"),
        request_timeout=3,
    )
    try:
        info = client.check_connection(tmp_path)
    finally:
        client.close()

    assert info.account_type == "chatgpt"


@pytest.mark.parametrize("mode", ["windows-ready", "windows-setup"])
def test_app_server_requires_ready_windows_elevated_sandbox(
    tmp_path: Path,
    mode: str,
) -> None:
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, mode),
        request_timeout=3,
    )
    try:
        info = client.check_connection(tmp_path)
    finally:
        client.close()

    assert info.account_type == "chatgpt"
    methods = (tmp_path / "server.log").read_text(encoding="utf-8").splitlines()
    assert "windowsSandbox/readiness" in methods
    assert ("windowsSandbox/setupStart" in methods) is (mode == "windows-setup")


def test_app_server_stops_when_windows_sandbox_setup_fails(tmp_path: Path) -> None:
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, "windows-setup-fail"),
        request_timeout=3,
    )
    try:
        with pytest.raises(AppServerError, match="취소되었거나 실패"):
            client.check_connection(tmp_path)
    finally:
        client.close()


@pytest.mark.parametrize(
    "mode",
    ["multiple", "forbidden", "plan", "request-input"],
)
def test_app_server_rejects_extra_tool_calls_and_cleans_up(
    tmp_path: Path,
    mode: str,
) -> None:
    source = tmp_path / "source.png"
    mask = tmp_path / "mask.png"
    source.write_bytes(PNG_1X1)
    mask.write_bytes(PNG_1X1)
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, mode),
        request_timeout=3,
        generation_timeout=3,
    )
    try:
        with pytest.raises(ImageGenerationFailed):
            client.generate_edit(
                cwd=tmp_path,
                prompt=HISTORY_IMAGE_PROMPT,
                source_path=source,
                mask_path=mask,
            )
    finally:
        client.close()

    methods = (tmp_path / "server.log").read_text(encoding="utf-8").splitlines()
    assert "turn/interrupt" in methods
    assert "thread/unsubscribe" in methods


def test_cancel_during_unsubscribe_prevents_completed_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.png"
    mask = tmp_path / "mask.png"
    source.write_bytes(PNG_1X1)
    mask.write_bytes(PNG_1X1)
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, "slow-unsubscribe"),
        request_timeout=3,
        generation_timeout=3,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.generate_edit,
                cwd=tmp_path,
                prompt=HISTORY_IMAGE_PROMPT,
                source_path=source,
                mask_path=mask,
            )
            log_path = tmp_path / "server.log"
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if log_path.is_file() and "thread/unsubscribe" in log_path.read_text(
                    encoding="utf-8"
                ):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("generation did not reach thread/unsubscribe")

            assert client.cancel() is True
            with pytest.raises(ImageGenerationFailed, match="취소"):
                future.result(timeout=3)
    finally:
        client.close()


def test_cancel_before_turn_id_does_not_wait_for_request_timeout(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    mask = tmp_path / "mask.png"
    source.write_bytes(PNG_1X1)
    mask.write_bytes(PNG_1X1)
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, "slow-thread-start"),
        request_timeout=3,
        generation_timeout=3,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.generate_edit,
                cwd=tmp_path,
                prompt=HISTORY_IMAGE_PROMPT,
                source_path=source,
                mask_path=mask,
            )
            log_path = tmp_path / "server.log"
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if log_path.is_file() and "thread/start" in log_path.read_text(
                    encoding="utf-8"
                ):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("generation did not reach thread/start")

            started = time.monotonic()
            assert client.cancel() is True
            assert time.monotonic() - started < 0.2
            with pytest.raises(ImageGenerationFailed, match="취소"):
                future.result(timeout=2)
    finally:
        client.close()


def test_generation_timeout_interrupts_and_unsubscribes(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    mask = tmp_path / "mask.png"
    source.write_bytes(PNG_1X1)
    mask.write_bytes(PNG_1X1)
    client = CodexAppServer(
        server_command=_fake_command(tmp_path, "timeout"),
        request_timeout=3,
        generation_timeout=0.05,
    )
    try:
        with pytest.raises(AppServerTimeout):
            client.generate_edit(
                cwd=tmp_path,
                prompt=HISTORY_IMAGE_PROMPT,
                source_path=source,
                mask_path=mask,
            )
    finally:
        client.close()

    methods = (tmp_path / "server.log").read_text(encoding="utf-8").splitlines()
    assert "turn/interrupt" in methods
    assert "thread/unsubscribe" in methods


def test_generation_result_rejects_invalid_or_oversized_base64(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = dict(
        thread_id="thread",
        turn_id="turn",
        tool_item_id="image",
        status="completed",
        saved_path=None,
        revised_prompt=None,
        transparent_background=None,
        user_agent="fake",
        imagegen_skill_path="/fake/SKILL.md",
        permission_profile_id="krita-imagegen-scoped-read-test",
    )
    invalid = GenerationResult(image_base64="not-base64", **base)
    with pytest.raises(ImageGenerationFailed, match="Base64"):
        invalid.write_image(tmp_path / "invalid.png")

    monkeypatch.setattr(app_server, "MAX_BASE64_CHARACTERS", 8)
    oversized = GenerationResult(image_base64="A" * 9, **base)
    with pytest.raises(ImageGenerationFailed, match="허용 크기"):
        oversized.write_image(tmp_path / "oversized.png")


def test_generation_result_never_reads_saved_path(tmp_path: Path) -> None:
    local_png = tmp_path / "must-not-be-read.png"
    local_png.write_bytes(PNG_1X1)
    result = GenerationResult(
        thread_id="thread",
        turn_id="turn",
        tool_item_id="image",
        status="completed",
        saved_path=str(local_png),
        image_base64=None,
        revised_prompt=None,
        transparent_background=None,
        user_agent="fake",
        imagegen_skill_path="/fake/SKILL.md",
        permission_profile_id="krita-imagegen-scoped-read-test",
    )

    with pytest.raises(ImageGenerationFailed, match="Base64"):
        result.write_image(tmp_path / "output.png")
