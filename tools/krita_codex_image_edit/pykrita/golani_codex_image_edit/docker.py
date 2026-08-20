from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from krita import DockWidget, DockWidgetFactory, DockWidgetFactoryBase, Krita, Selection


try:
    _KRITA_MAJOR_VERSION = int(Krita.instance().version().split(".", 1)[0])
except (AttributeError, TypeError, ValueError) as exc:
    raise ImportError("Codex Selection AI Edit could not identify the Krita version") from exc

if _KRITA_MAJOR_VERSION >= 6:
    from PyQt6.QtCore import QByteArray, QObject, QStandardPaths, Qt, pyqtSignal
    from PyQt6.QtGui import QColorSpace, QImage, QImageReader, QTransform
    from PyQt6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )

    _TEXT_SELECTABLE = Qt.TextInteractionFlag.TextSelectableByMouse
    _IGNORE_ASPECT_RATIO = Qt.AspectRatioMode.IgnoreAspectRatio
    _SMOOTH_TRANSFORMATION = Qt.TransformationMode.SmoothTransformation
    _FORMAT_ARGB32 = QImage.Format.Format_ARGB32
    _FORMAT_GRAYSCALE8 = QImage.Format.Format_Grayscale8
    _FORMAT_INDEXED8 = QImage.Format.Format_Indexed8
    _DOCUMENTS_LOCATION = QStandardPaths.StandardLocation.DocumentsLocation
    _SRGB_COLOR_SPACE = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
else:
    from PyQt5.QtCore import (
        QT_VERSION_STR,
        QByteArray,
        QObject,
        QStandardPaths,
        Qt,
        pyqtSignal,
    )

    if tuple(int(part) for part in QT_VERSION_STR.split(".")[:2]) < (5, 14):
        raise ImportError(
            "Codex Selection AI Edit requires Qt 5.14 or newer for color-safe PNG handling"
        )
    from PyQt5.QtGui import QColorSpace, QImage, QImageReader, QTransform
    from PyQt5.QtWidgets import (
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )

    _TEXT_SELECTABLE = Qt.TextSelectableByMouse
    _IGNORE_ASPECT_RATIO = Qt.IgnoreAspectRatio
    _SMOOTH_TRANSFORMATION = Qt.SmoothTransformation
    _FORMAT_ARGB32 = QImage.Format_ARGB32
    _FORMAT_GRAYSCALE8 = QImage.Format_Grayscale8
    _FORMAT_INDEXED8 = QImage.Format_Indexed8
    _DOCUMENTS_LOCATION = QStandardPaths.DocumentsLocation
    _SRGB_COLOR_SPACE = QColorSpace(QColorSpace.SRgb)

from .app_server import CodexAppServer, ConnectionInfo
from .core import (
    CropRect,
    build_edit_prompt,
    context_crop,
    ensure_selection_is_spt_subset,
    ensure_selected_pixels_are_opaque,
    find_spt_project_root,
    is_supported_srgb_profile,
    masked_bgra_layer,
    safe_stem,
    sha256_bytes,
    spt_panel_mask,
    validate_spt_mask_contract,
    validate_projection_invariants,
)
from .spt import (
    MAX_GENERATION_ATTEMPTS,
    SptPanel,
    SptTarget,
    build_spt_prompt,
    current_generation_attempts,
    load_spt_target,
    scan_spt_targets,
)


PLUGIN_ID = "golani_codex_image_edit"
MAX_RESULT_FILE_BYTES = 100 * 1024 * 1024
MAX_RESULT_PIXELS = 16 * 1024 * 1024
MAX_CONTEXT_PIXELS = 4 * 1024 * 1024


@dataclass(frozen=True)
class _Snapshot:
    document: Any
    root_id: str
    canvas_x: int
    canvas_y: int
    canvas_width: int
    canvas_height: int
    color_model: str
    color_depth: str
    color_profile: str
    selection_bbox: CropRect
    crop: CropRect
    source_bgra: bytes
    selection_mask: bytes
    source_sha256: str
    mask_sha256: str
    job_dir: Path
    source_path: Path
    mask_path: Path
    request_path: Path
    prompt: str
    instruction: str
    model_width: int
    model_height: int
    deskew_rotation_deg: float
    spt: dict[str, Any] | None


class _Signals(QObject):
    status = pyqtSignal(str)
    connection_ready = pyqtSignal(object)
    generation_ready = pyqtSignal(str)
    failed = pyqtSignal(str)


class CodexSelectionEditDocker(DockWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Codex 선택 영역 AI 편집")
        self._signals = _Signals()
        self._signals.status.connect(self._set_status)
        self._signals.connection_ready.connect(self._connection_ready)
        self._signals.generation_ready.connect(self._generation_ready)
        self._signals.failed.connect(self._operation_failed)
        self._session_lock = threading.Lock()
        self._session: CodexAppServer | None = None
        self._session_executable: str | None = None
        self._pending_snapshot: _Snapshot | None = None
        self._spt_target: SptTarget | None = None
        self._spt_panel: SptPanel | None = None
        self._spt_allowed_mask: bytes | None = None
        self._last_spt_job_dir: Path | None = None
        self._busy = False
        self._build_ui()
        self.destroyed.connect(self._shutdown)

    def canvasChanged(self, canvas: Any) -> None:  # noqa: N802 - Krita API name
        del canvas

    def _build_ui(self) -> None:
        container = QWidget(self)
        layout = QVBoxLayout(container)

        explanation = QLabel(
            "Krita 선택을 흑백 가이드로 Codex 기본 imagegen에 보내고, "
            "결과를 원래 마스크로 잘라 새 레이어에 넣어요."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        warning = QLabel(
            "SPT 모드는 analysis 통과 기록과 현재 SHA의 5종 마스크가 있는 품목만 열어요. "
            "결과는 패널 OCR 전 검증 전 미리보기이며 후보 승인본이 아니에요."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #c58a35;")
        layout.addWidget(warning)

        self._mode = QComboBox()
        self._mode.addItem("일반 이미지 선택 편집", "generic")
        self._mode.addItem("SPT 준비 작업", "spt")
        saved_mode = self._read_setting("mode", "generic")
        self._mode.setCurrentIndex(1 if saved_mode == "spt" else 0)
        self._mode.currentIndexChanged.connect(self._mode_changed)
        layout.addWidget(self._mode)

        spt_form = QFormLayout()
        self._spt_root = QLineEdit(self._read_setting("spt_root", ""))
        spt_root_row = QHBoxLayout()
        spt_root_row.addWidget(self._spt_root)
        self._spt_browse = QPushButton("찾기")
        self._spt_browse.clicked.connect(self._browse_spt_root)
        spt_root_row.addWidget(self._spt_browse)
        self._spt_refresh = QPushButton("새로고침")
        self._spt_refresh.clicked.connect(self._refresh_spt_targets)
        spt_root_row.addWidget(self._spt_refresh)
        spt_form.addRow("SPT 프로젝트", spt_root_row)
        self._spt_target_box = QComboBox()
        spt_form.addRow("품목", self._spt_target_box)
        self._spt_panel_box = QComboBox()
        self._spt_panel_box.currentIndexChanged.connect(self._spt_panel_changed)
        spt_form.addRow("라벨 면", self._spt_panel_box)
        layout.addLayout(spt_form)

        self._spt_open = QPushButton("SPT 원본·추천 선택 불러오기")
        self._spt_open.clicked.connect(self._load_spt_work)
        layout.addWidget(self._spt_open)

        self._prompt = QPlainTextEdit()
        self._prompt.setPlaceholderText(
            "일반 모드: 전체 수정 지시\n"
            "SPT 모드: 확정 번역은 review.json에서 고정되므로 이번 시도의 추가 시각 보정만 입력"
        )
        self._prompt.setMinimumHeight(110)
        layout.addWidget(self._prompt)

        form = QFormLayout()
        self._padding = QSpinBox()
        self._padding.setRange(0, 1024)
        try:
            saved_padding = int(self._read_setting("padding", "128"))
        except ValueError:
            saved_padding = 128
        self._padding.setValue(saved_padding)
        self._padding.setSuffix(" px")
        form.addRow("문맥 여백", self._padding)

        self._workspace = QLineEdit(
            self._read_setting("workspace", str(_default_workspace()))
        )
        workspace_row = QHBoxLayout()
        workspace_row.addWidget(self._workspace)
        browse = QPushButton("찾기")
        browse.clicked.connect(self._browse_workspace)
        workspace_row.addWidget(browse)
        form.addRow("작업 폴더", workspace_row)

        self._codex_executable = QLineEdit(self._read_setting("codex", "codex"))
        self._codex_executable.setPlaceholderText("codex 또는 codex.cmd의 전체 경로")
        form.addRow("Codex 실행 파일", self._codex_executable)
        layout.addLayout(form)

        self._acknowledge_preview = QCheckBox(
            "결과가 검증 전 미리보기이며 선택 밖 픽셀은 로컬 마스크로 보호됨을 확인"
        )
        self._acknowledge_preview.setChecked(True)
        layout.addWidget(self._acknowledge_preview)

        button_row = QHBoxLayout()
        self._check_button = QPushButton("연결 확인")
        self._check_button.clicked.connect(self._check_connection)
        button_row.addWidget(self._check_button)
        self._edit_button = QPushButton("선택 영역 수정")
        self._edit_button.clicked.connect(self._start_edit)
        button_row.addWidget(self._edit_button)
        self._cancel_button = QPushButton("취소")
        self._cancel_button.setEnabled(False)
        self._cancel_button.clicked.connect(self._cancel)
        button_row.addWidget(self._cancel_button)
        layout.addLayout(button_row)

        decision_row = QHBoxLayout()
        self._spt_accept_button = QPushButton("초안 채택 → OCR 대기")
        self._spt_accept_button.setEnabled(False)
        self._spt_accept_button.clicked.connect(
            lambda: self._record_spt_decision("selected-for-panel-ocr")
        )
        decision_row.addWidget(self._spt_accept_button)
        self._spt_reject_button = QPushButton("초안 거절")
        self._spt_reject_button.setEnabled(False)
        self._spt_reject_button.clicked.connect(
            lambda: self._record_spt_decision("rejected")
        )
        decision_row.addWidget(self._spt_reject_button)
        layout.addLayout(decision_row)

        self._status = QLabel("준비됨")
        self._status.setWordWrap(True)
        self._status.setTextInteractionFlags(_TEXT_SELECTABLE)
        layout.addWidget(self._status)
        layout.addStretch(1)
        self.setWidget(container)
        self._mode_changed()

    def _is_spt_mode(self) -> bool:
        return self._mode.currentData() == "spt"

    def _mode_changed(self) -> None:
        spt_mode = self._is_spt_mode()
        for widget in (
            self._spt_root,
            self._spt_browse,
            self._spt_refresh,
            self._spt_target_box,
            self._spt_panel_box,
            self._spt_open,
        ):
            widget.setEnabled(spt_mode and not self._busy)
        self._workspace.setEnabled(not spt_mode and not self._busy)
        self._write_setting("mode", "spt" if spt_mode else "generic")

    def _browse_spt_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "SPT 음식 텍스처 프로젝트 루트",
            self._spt_root.text().strip(),
        )
        if selected:
            self._spt_root.setText(selected)
            self._refresh_spt_targets()

    def _refresh_spt_targets(self) -> None:
        if self._busy:
            return
        try:
            root = Path(self._spt_root.text().strip()).expanduser().resolve()
            summaries = scan_spt_targets(root)
        except Exception as exc:
            self._operation_failed(str(exc))
            return
        self._write_setting("spt_root", str(root))
        self._spt_target_box.blockSignals(True)
        self._spt_target_box.clear()
        for item in summaries:
            self._spt_target_box.addItem(
                f"{item.name_ko} ({item.target_id}) · {item.state}",
                item.target_id,
            )
        self._spt_target_box.blockSignals(False)
        self._spt_target = None
        self._spt_panel = None
        self._spt_allowed_mask = None
        self._spt_panel_box.clear()
        self._set_status(f"SPT 품목 {len(summaries)}개를 확인했어요")

    def _load_spt_work(self) -> None:
        if self._busy:
            return
        target_id = self._spt_target_box.currentData()
        if not isinstance(target_id, str) or not target_id:
            self._operation_failed("불러올 SPT 품목을 선택해 주세요")
            return
        try:
            root = Path(self._spt_root.text().strip()).expanduser().resolve()
            target = load_spt_target(root, target_id)
        except Exception as exc:
            self._operation_failed(str(exc))
            return
        try:
            self._spt_target = target
            self._spt_panel_box.blockSignals(True)
            self._spt_panel_box.clear()
            for panel in target.panels:
                self._spt_panel_box.addItem(panel.label, panel.panel_id)
            self._spt_panel_box.blockSignals(False)
            if not target.panels:
                raise ValueError("통과한 번역 기록에 불러올 라벨 면이 없어요")
            self._spt_panel_box.setCurrentIndex(0)
            self._open_spt_panel(target, target.panels[0])
        except Exception as exc:
            self._operation_failed(str(exc))

    def _spt_panel_changed(self) -> None:
        if self._busy or self._spt_target is None:
            return
        panel_id = self._spt_panel_box.currentData()
        panel = next(
            (item for item in self._spt_target.panels if item.panel_id == panel_id),
            None,
        )
        if panel is None:
            return
        try:
            self._open_spt_panel(self._spt_target, panel)
        except Exception as exc:
            self._operation_failed(str(exc))

    def _open_spt_panel(self, target: SptTarget, panel: SptPanel) -> None:
        current = load_spt_target(target.project_root, target.target_id)
        panel = next((item for item in current.panels if item.panel_id == panel.panel_id), None)
        if panel is None:
            raise ValueError("review.json이 바뀌어 선택한 라벨 면을 다시 찾지 못했어요")
        attempts = current_generation_attempts(current, panel)
        if attempts >= MAX_GENERATION_ATTEMPTS:
            raise ValueError(
                f"이 라벨 면은 기본 생성 예산 {MAX_GENERATION_ATTEMPTS}회를 이미 사용했어요. "
                "추가 시도는 Codex 작업에서 사용자 승인 증거를 먼저 기록해야 해요"
            )
        mask_pixels = {
            name: _read_mask_bytes(artifact.path, artifact.width, artifact.height)
            for name, artifact in current.masks.items()
        }
        validate_spt_mask_contract(
            mask_pixels["old_text"],
            mask_pixels["new_text"],
            mask_pixels["editable"],
            mask_pixels["protected"],
            mask_pixels["seam_guard"],
            current.source.width,
            current.source.height,
        )
        panel_padding = max(4, round(max(current.source.width, current.source.height) * 0.01))
        allowed = spt_panel_mask(
            mask_pixels["editable"],
            current.source.width,
            current.source.height,
            [region.bbox for region in panel.regions],
            panel_padding,
        )
        document = _open_or_activate_document(current.source.path)
        if document.width() != current.source.width or document.height() != current.source.height:
            raise ValueError("Krita에 열린 SPT 원본 크기가 review.json과 달라요")
        _verify_document_matches_source(document, current.source.path)
        selection = Selection()
        selection.setPixelData(
            QByteArray(allowed),
            0,
            0,
            current.source.width,
            current.source.height,
        )
        document.setSelection(selection)
        document.waitForDone()
        document.refreshProjection()
        self._spt_target = current
        self._spt_panel = panel
        self._spt_allowed_mask = allowed
        self._last_spt_job_dir = None
        self._spt_accept_button.setEnabled(False)
        self._spt_reject_button.setEnabled(False)
        self._prompt.setPlainText("")
        texts = ", ".join(region.final_text_ko.replace("\n", " / ") for region in panel.regions)
        self._set_status(
            f"{current.name_ko} · {panel.label} 선택을 적용했어요 · "
            f"생성 {attempts}/{MAX_GENERATION_ATTEMPTS}회 · 확정 문구: {texts}"
        )

    def _browse_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Codex 편집 작업 폴더",
            self._workspace.text().strip(),
        )
        if selected:
            self._workspace.setText(selected)

    def _check_connection(self) -> None:
        if self._busy:
            return
        try:
            workspace, executable = self._capture_settings()
        except Exception as exc:
            self._operation_failed(str(exc))
            return
        self._set_busy(True, cancellable=False)
        self._set_status("Codex App Server와 ChatGPT 로그인을 확인하고 있어요")

        def run() -> None:
            try:
                info = self._session_for(executable).check_connection(workspace)
                self._signals.connection_ready.emit(info)
            except Exception as exc:  # UI boundary: show a concise error
                self._signals.failed.emit(str(exc))

        threading.Thread(target=run, name="krita-codex-check", daemon=True).start()

    def _start_edit(self) -> None:
        if self._busy:
            return
        if not self._acknowledge_preview.isChecked():
            self._operation_failed("검증 전 미리보기 확인란을 먼저 선택해 주세요")
            return
        if not self._is_spt_mode() and not self._prompt.toPlainText().strip():
            self._operation_failed("수정 지시를 입력해 주세요")
            return
        try:
            if self._is_spt_mode():
                target, panel, allowed = self._validated_spt_context()
                workspace = (
                    target.project_root
                    / "workspace"
                    / "krita-spt"
                    / target.target_id
                    / panel.panel_id
                )
                workspace.mkdir(parents=True, exist_ok=True)
                executable = self._capture_executable()
                snapshot = self._capture_snapshot(
                    workspace,
                    spt_target=target,
                    spt_panel=panel,
                    spt_allowed_mask=allowed,
                )
            else:
                workspace, executable = self._capture_settings()
                snapshot = self._capture_snapshot(workspace)
        except Exception as exc:
            self._operation_failed(str(exc))
            return

        self._pending_snapshot = snapshot
        self._set_busy(True, cancellable=True)
        self._set_status("선택 영역과 문맥을 저장했어요 · Codex 기본 imagegen을 시작해요")

        def run() -> None:
            try:
                result = self._session_for(executable).generate_edit(
                    cwd=snapshot.job_dir,
                    prompt=snapshot.prompt,
                    source_path=snapshot.source_path,
                    mask_path=snapshot.mask_path,
                    progress=self._signals.status.emit,
                )
                generated_path = result.write_image(snapshot.job_dir / "generated.png")
                _update_request(
                    snapshot.request_path,
                    status="generated",
                    generation={
                        "thread_id": result.thread_id,
                        "turn_id": result.turn_id,
                        "tool_item_id": result.tool_item_id,
                        "status": result.status,
                        "saved_path_reported_but_not_read": bool(result.saved_path),
                        "revised_prompt": result.revised_prompt,
                        "transparent_background": result.transparent_background,
                        "codex_user_agent": result.user_agent,
                        "imagegen_skill_path": result.imagegen_skill_path,
                        "permission_profile_id": result.permission_profile_id,
                        "artifact": {
                            "path": generated_path.name,
                            "sha256": _sha256_file(generated_path),
                        },
                    },
                )
                self._signals.generation_ready.emit(str(generated_path))
            except Exception as exc:  # UI boundary: preserve the generated job evidence
                try:
                    _update_request(snapshot.request_path, status="error", error=str(exc))
                except Exception:
                    pass
                self._signals.failed.emit(str(exc))

        threading.Thread(target=run, name="krita-codex-generate", daemon=True).start()

    def _capture_settings(self) -> tuple[Path, str]:
        workspace_value = self._workspace.text().strip()
        executable = self._capture_executable()
        if not workspace_value:
            raise ValueError("작업 폴더를 지정해 주세요")
        workspace = Path(workspace_value).expanduser().resolve()
        if workspace.name.lower() == ".git" or (workspace / ".git").exists():
            raise ValueError("Git 저장소 루트나 .git 폴더를 작업 폴더로 사용할 수 없어요")
        workspace.mkdir(parents=True, exist_ok=True)
        self._write_setting("workspace", str(workspace))
        self._write_setting("codex", executable)
        self._write_setting("padding", str(self._padding.value()))
        return workspace, executable

    def _capture_executable(self) -> str:
        executable = self._codex_executable.text().strip()
        if not executable:
            raise ValueError("Codex 실행 파일을 지정해 주세요")
        self._write_setting("codex", executable)
        return executable

    def _validated_spt_context(self) -> tuple[SptTarget, SptPanel, bytes]:
        if self._spt_target is None or self._spt_panel is None or self._spt_allowed_mask is None:
            raise ValueError("먼저 SPT 원본·추천 선택을 불러와 주세요")
        target = load_spt_target(
            self._spt_target.project_root,
            self._spt_target.target_id,
        )
        panel = next(
            (item for item in target.panels if item.panel_id == self._spt_panel.panel_id),
            None,
        )
        if panel is None:
            raise ValueError("review.json이 바뀌어 현재 라벨 면을 다시 불러와야 해요")
        attempts = current_generation_attempts(target, panel)
        if attempts >= MAX_GENERATION_ATTEMPTS:
            raise ValueError(
                f"이 라벨 면의 기본 생성 예산 {MAX_GENERATION_ATTEMPTS}회를 모두 사용했어요"
            )
        editable = _read_mask_bytes(
            target.masks["editable"].path,
            target.source.width,
            target.source.height,
        )
        padding = max(4, round(max(target.source.width, target.source.height) * 0.01))
        allowed = spt_panel_mask(
            editable,
            target.source.width,
            target.source.height,
            [region.bbox for region in panel.regions],
            padding,
        )
        if allowed != self._spt_allowed_mask:
            raise ValueError("review.json 또는 editable 마스크가 바뀌어 SPT 작업을 다시 불러와야 해요")
        return target, panel, allowed

    def _capture_snapshot(
        self,
        workspace: Path,
        *,
        spt_target: SptTarget | None = None,
        spt_panel: SptPanel | None = None,
        spt_allowed_mask: bytes | None = None,
    ) -> _Snapshot:
        application = Krita.instance()
        document = application.activeDocument()
        if document is None:
            raise ValueError("먼저 Krita 문서를 열어 주세요")
        if document.colorModel() != "RGBA" or document.colorDepth() != "U8":
            raise ValueError("현재 버전은 RGBA/U8 문서만 안전하게 지원해요")
        color_profile = document.colorProfile()
        if not is_supported_srgb_profile(color_profile):
            raise ValueError(
                "현재 버전은 비선형 sRGB 프로필만 지원해요. "
                "문서 프로필을 sRGB-elle-V2-srgbtrc.icc로 변환해 주세요"
            )
        spt_root = find_spt_project_root(workspace, document.fileName())
        if spt_root is not None and spt_target is None:
            raise ValueError(
                "SPT 저장소 자산은 일반 모드로 생성할 수 없어요. "
                "SPT 준비 작업 모드에서 통과 기록과 마스크를 불러와 주세요"
            )
        if spt_target is not None:
            if spt_root != spt_target.project_root:
                raise ValueError("현재 문서가 선택한 SPT 프로젝트 원본이 아니에요")
            if Path(document.fileName()).resolve() != spt_target.source.path:
                raise ValueError("현재 문서가 선택한 SPT 품목의 불변 원본 PNG가 아니에요")
            if spt_panel is None or spt_allowed_mask is None:
                raise ValueError("SPT 라벨 면 선택 증거가 없어요")
            _verify_document_matches_source(document, spt_target.source.path)
        selection = document.selection()
        if selection is None or selection.width() <= 0 or selection.height() <= 0:
            raise ValueError("수정할 영역을 먼저 선택해 주세요")

        document.waitForDone()
        document.refreshProjection()
        document.waitForDone()
        bounds = document.bounds()
        canvas_x = int(bounds.x())
        canvas_y = int(bounds.y())
        canvas_width = int(bounds.width())
        canvas_height = int(bounds.height())
        selected = CropRect(
            int(selection.x()),
            int(selection.y()),
            int(selection.width()),
            int(selection.height()),
        )
        crop = context_crop(
            selected,
            canvas_width,
            canvas_height,
            int(self._padding.value()),
            canvas_x=canvas_x,
            canvas_y=canvas_y,
        )
        if crop.width * crop.height > MAX_CONTEXT_PIXELS:
            raise ValueError(
                "문맥 crop이 2048×2048 상당의 안전 제한을 넘어요. "
                "선택 영역 또는 문맥 여백을 줄여 주세요"
            )
        source_bgra = bytes(document.pixelData(crop.x, crop.y, crop.width, crop.height))
        selection_mask = bytes(
            selection.pixelData(crop.x, crop.y, crop.width, crop.height)
        )
        expected_pixels = crop.width * crop.height
        if len(source_bgra) != expected_pixels * 4:
            raise ValueError("Krita 문서 픽셀을 RGBA/U8로 읽지 못했어요")
        if len(selection_mask) != expected_pixels or not any(selection_mask):
            raise ValueError("Krita 선택 마스크를 읽지 못했어요")
        ensure_selected_pixels_are_opaque(
            source_bgra,
            selection_mask,
            crop.width,
            crop.height,
        )

        manual_selection_reduced = False
        if spt_target is not None and spt_allowed_mask is not None:
            full_selection = bytes(
                selection.pixelData(
                    canvas_x,
                    canvas_y,
                    canvas_width,
                    canvas_height,
                )
            )
            if (canvas_x, canvas_y) != (0, 0) or (
                canvas_width,
                canvas_height,
            ) != (spt_target.source.width, spt_target.source.height):
                raise ValueError("SPT 원본 문서의 캔버스 좌표 또는 크기가 review.json과 달라요")
            manual_selection_reduced = ensure_selection_is_spt_subset(
                full_selection,
                spt_allowed_mask,
            )

        document_name = document.name() or Path(document.fileName()).stem or "untitled"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if spt_target is not None:
            job_dir = workspace / f"{timestamp}-{uuid4().hex[:8]}"
        else:
            job_dir = (
                workspace
                / safe_stem(document_name)
                / f"{timestamp}-{uuid4().hex[:8]}"
            )
        source_path = job_dir / "source.png"
        mask_path = job_dir / "selection-mask.png"
        model_source_bgra = source_bgra
        model_selection_mask = selection_mask
        model_width = crop.width
        model_height = crop.height
        deskew_rotation = 0.0
        if spt_panel is not None and spt_panel.rotation_deg % 360:
            deskew_rotation = -spt_panel.rotation_deg
            (
                model_source_bgra,
                model_selection_mask,
                model_width,
                model_height,
            ) = _deskew_panel(
                source_bgra,
                selection_mask,
                crop.width,
                crop.height,
                deskew_rotation,
            )
            if model_width * model_height > MAX_CONTEXT_PIXELS:
                raise ValueError(
                    "정방향화한 SPT 작업 패널이 2048×2048 상당의 안전 제한을 넘어요. "
                    "문맥 여백을 줄여 주세요"
                )
        if spt_target is not None and spt_panel is not None:
            prompt = build_spt_prompt(
                spt_target,
                spt_panel,
                self._prompt.toPlainText(),
                model_width,
                model_height,
            )
        else:
            prompt = build_edit_prompt(
                self._prompt.toPlainText(),
                model_width,
                model_height,
            )
        job_dir.mkdir(parents=True, exist_ok=False)
        _save_bgra_png(source_path, model_source_bgra, model_width, model_height)
        _save_mask_png(mask_path, model_selection_mask, model_width, model_height)

        root_id = _node_id(document.rootNode())
        request_path = job_dir / "request.json"
        record = {
            "schema_version": 1,
            "status": "pending",
            "preview_only": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "document": {
                "name": document_name,
                "file": document.fileName(),
                "root_id": root_id,
                "bounds_xywh": [canvas_x, canvas_y, canvas_width, canvas_height],
                "width": canvas_width,
                "height": canvas_height,
                "color_model": document.colorModel(),
                "color_depth": document.colorDepth(),
                "color_profile": color_profile,
            },
            "selection": {
                "bbox_xywh": selected.as_list(),
                "context_crop_xywh": crop.as_list(),
                "context_padding_px": int(self._padding.value()),
                "mask_sha256": sha256_bytes(selection_mask),
            },
            "source": {
                "path": source_path.name,
                "file_sha256": _sha256_file(source_path),
                "pixel_sha256": sha256_bytes(source_bgra),
            },
            "mask": {
                "path": mask_path.name,
                "sha256": _sha256_file(mask_path),
                "meaning": "0=protected, 1..255=editable selectedness",
            },
            "instruction": self._prompt.toPlainText().strip(),
            "prompt": prompt,
            "codex": {
                "transport": "app-server-stdio-jsonl",
                "authentication": "chatgpt-login-required",
                "skill": "imagegen",
                "built_in_model": "gpt-image-2",
                "openai_api_key_required": False,
                "model_provider": "openai",
                "api_key_environment": "removed-from-child",
                "permission_profile": "unique-per-generation:krita-imagegen-scoped-read-*",
                "image_reference_mode": "conversation-history-last-2",
                "referenced_image_paths": "forbidden-by-turn-instructions",
                "filesystem_permission_request": (
                    "root-deny-plus-platform-minimal-plus-two-inputs"
                ),
                "linux_sandbox_boundary": "request-profile-enforced",
                "native_windows_acl_boundary": (
                    "shared-persistent-best-effort-not-hard-isolation"
                ),
                "approval_policy": "never",
                "external_agent_tools": "disabled-and-runtime-audited",
                "mcp_servers": "disabled-per-thread-and-verified-empty",
            },
        }
        spt_metadata: dict[str, Any] | None = None
        if spt_target is not None and spt_panel is not None:
            attempts = current_generation_attempts(spt_target, spt_panel)
            spt_metadata = {
                "mode": "vision-panel-localization-preview",
                "target_id": spt_target.target_id,
                "panel_id": spt_panel.panel_id,
                "face": spt_panel.face,
                "review_path": spt_target.review_path.relative_to(
                    spt_target.project_root
                ).as_posix(),
                "review_sha256": spt_target.review_sha256,
                "source_sha256": spt_target.source.sha256,
                "mask_sha256": {
                    name: artifact.sha256 for name, artifact in spt_target.masks.items()
                },
                "region_ids": [region.region_id for region in spt_panel.regions],
                "exact_text": [region.final_text_ko for region in spt_panel.regions],
                "generation_attempt": attempts + 1,
                "generation_budget": MAX_GENERATION_ATTEMPTS,
                "manual_selection_reduced": manual_selection_reduced,
                "panel_transform": {
                    "coordinate_space": "source-mip0",
                    "crop_bbox_xywh": crop.as_list(),
                    "source_rotation_deg": spt_panel.rotation_deg,
                    "deskew_rotation_deg": deskew_rotation,
                    "inverse_rotation_deg": -deskew_rotation,
                    "model_panel_size": [model_width, model_height],
                    "selected_lettering_restored_to_source": False,
                    "source_texture_resampled": False,
                    "final_texture_resampled": False,
                },
                "decision": "pending-visual-review",
            }
            record["spt"] = spt_metadata
        _write_json(request_path, record)
        return _Snapshot(
            document=document,
            root_id=root_id,
            canvas_x=canvas_x,
            canvas_y=canvas_y,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            color_model=document.colorModel(),
            color_depth=document.colorDepth(),
            color_profile=color_profile,
            selection_bbox=selected,
            crop=crop,
            source_bgra=source_bgra,
            selection_mask=selection_mask,
            source_sha256=sha256_bytes(source_bgra),
            mask_sha256=sha256_bytes(selection_mask),
            job_dir=job_dir,
            source_path=source_path,
            mask_path=mask_path,
            request_path=request_path,
            prompt=prompt,
            instruction=self._prompt.toPlainText().strip(),
            model_width=model_width,
            model_height=model_height,
            deskew_rotation_deg=deskew_rotation,
            spt=spt_metadata,
        )

    def _generation_ready(self, generated_value: str) -> None:
        snapshot = self._pending_snapshot
        if snapshot is None:
            self._operation_failed("적용할 선택 영역 스냅샷이 없어요")
            return
        generated_path = Path(generated_value)
        try:
            layer_name, transform = self._apply_generated(snapshot, generated_path)
        except Exception as exc:
            try:
                _update_request(
                    snapshot.request_path,
                    status="generated-not-applied",
                    error=str(exc),
                )
            except Exception:
                pass
            self._operation_failed(
                f"생성본은 보존했지만 문서에는 적용하지 않았어요: {exc}\n{snapshot.job_dir}"
            )
            return

        record_error: str | None = None
        try:
            _update_request(
                snapshot.request_path,
                status="preview-layer-added",
                applied={
                    "layer_name": layer_name,
                    "outside_selection_changes": 0,
                    "alpha_changes": 0,
                    "undo": "single layer-add command",
                    "transform": transform,
                },
            )
        except Exception as exc:
            record_error = str(exc)

        self._pending_snapshot = None
        self._set_busy(False)
        if snapshot.spt is not None:
            self._last_spt_job_dir = snapshot.job_dir
            self._spt_accept_button.setEnabled(True)
            self._spt_reject_button.setEnabled(True)
            message = (
                "SPT 검증 전 미리보기를 추가했어요. 원본 배율로 확인한 뒤 "
                "초안 채택 또는 거절을 기록해 주세요. 채택은 후보 승인이 아니라 패널 OCR 대기예요."
                f"\n{snapshot.job_dir}"
            )
        else:
            message = (
                "새 미리보기 레이어를 추가했어요. Ctrl+Z 한 번으로 되돌릴 수 있어요."
                f"\n{snapshot.job_dir}"
            )
        if record_error:
            message += f"\n주의: 적용 기록을 갱신하지 못했어요: {record_error}"
        self._set_status(message)

    def _apply_generated(
        self,
        snapshot: _Snapshot,
        generated_path: Path,
    ) -> tuple[str, dict[str, Any]]:
        document = snapshot.document
        if not any(open_document == document for open_document in Krita.instance().documents()):
            raise ValueError("생성 중 원래 Krita 문서가 닫혔어요")
        if not _is_active_document(document):
            raise ValueError("생성 중 다른 문서로 전환되어 원래 문서에 자동 적용하지 않았어요")
        bounds = document.bounds()
        if (
            int(bounds.x()) != snapshot.canvas_x
            or int(bounds.y()) != snapshot.canvas_y
            or int(bounds.width()) != snapshot.canvas_width
            or int(bounds.height()) != snapshot.canvas_height
            or document.colorModel() != snapshot.color_model
            or document.colorDepth() != snapshot.color_depth
            or document.colorProfile() != snapshot.color_profile
        ):
            raise ValueError("생성 중 문서 경계 또는 색 공간이 바뀌었어요")

        document.waitForDone()
        document.refreshProjection()
        document.waitForDone()
        crop = snapshot.crop
        current_source = bytes(document.pixelData(crop.x, crop.y, crop.width, crop.height))
        if sha256_bytes(current_source) != snapshot.source_sha256:
            raise ValueError("생성 중 문맥 픽셀이 바뀌어 오래된 결과를 자동 적용하지 않았어요")
        selection = document.selection()
        if selection is None:
            raise ValueError("생성 중 선택 영역이 해제됐어요")
        current_selection_bbox = CropRect(
            int(selection.x()),
            int(selection.y()),
            int(selection.width()),
            int(selection.height()),
        )
        if current_selection_bbox != snapshot.selection_bbox:
            raise ValueError("생성 중 선택 영역 경계가 바뀌었어요")
        current_mask = bytes(selection.pixelData(crop.x, crop.y, crop.width, crop.height))
        if sha256_bytes(current_mask) != snapshot.mask_sha256:
            raise ValueError("생성 중 선택 영역이 바뀌었어요")

        generated_bgra, generated_width, generated_height = _read_result_bgra(
            generated_path,
            snapshot.model_width,
            snapshot.model_height,
        )
        if snapshot.deskew_rotation_deg:
            generated_bgra = _restore_deskewed_bgra(
                generated_bgra,
                snapshot.model_width,
                snapshot.model_height,
                crop.width,
                crop.height,
                -snapshot.deskew_rotation_deg,
            )
        layer_pixels = masked_bgra_layer(
            generated_bgra,
            snapshot.selection_mask,
            crop.width,
            crop.height,
        )
        _preflight_composite(snapshot, layer_pixels)
        if not _is_active_document(document):
            raise ValueError("합성 검사 중 다른 문서로 전환되어 자동 적용하지 않았어요")

        if snapshot.spt is not None:
            layer_name = (
                f"[SPT 패널 OCR 전 미리보기] {snapshot.spt['target_id']} · "
                f"{snapshot.spt['panel_id']}"
            )
        else:
            layer_name = f"[검증 전 AI 미리보기] {snapshot.instruction[:42]}"
        layer = document.createNode(layer_name, "paintlayer")
        if layer is None:
            raise ValueError("Krita 미리보기 레이어를 만들지 못했어요")
        if not layer.setPixelData(
            QByteArray(layer_pixels),
            crop.x,
            crop.y,
            crop.width,
            crop.height,
        ):
            raise ValueError("미리보기 레이어 픽셀을 기록하지 못했어요")
        layer.setColorLabel(3)
        layer.setVisible(False)
        root = document.rootNode()
        layer_id = _node_id(layer)
        if not root.addChildNode(layer, None):
            raise ValueError("미리보기 레이어를 문서에 추가하지 못했어요")

        try:
            children = root.childNodes()
            attached_ids = [_node_id(child) for child in children]
            if layer_id not in attached_ids:
                raise RuntimeError(
                    "Krita가 레이어 추가 성공을 보고했지만 문서에서 찾을 수 없어요"
                )
            if not children or _node_id(children[-1]) != layer_id:
                raise RuntimeError("미리보기 레이어가 문서 최상단에 추가되지 않았어요")
            layer.setVisible(True)
            document.refreshProjection()
            document.waitForDone()
            after = bytes(document.pixelData(crop.x, crop.y, crop.width, crop.height))
            validate_projection_invariants(
                snapshot.source_bgra,
                after,
                snapshot.selection_mask,
                crop.width,
                crop.height,
            )
            document.setActiveNode(layer)
        except Exception as validation_error:
            try:
                _undo_failed_attach(document, layer, snapshot)
            except Exception as rollback_error:
                still_attached = any(
                    _node_id(child) == layer_id
                    for child in document.rootNode().childNodes()
                )
                rollback_state = (
                    "문제 레이어를 숨겨 두었어요"
                    if still_attached
                    else "실패 레이어는 제거됐지만 원본 투영 복구를 확인하지 못했어요"
                )
                raise RuntimeError(
                    f"{validation_error}; 자동 Undo 검증 실패 — {rollback_state}: "
                    f"{rollback_error}"
                ) from validation_error
            raise validation_error
        return layer_name, {
            "generated_size": [generated_width, generated_height],
            "context_size": [crop.width, crop.height],
            "model_panel_size": [snapshot.model_width, snapshot.model_height],
            "deskew_rotation_deg": snapshot.deskew_rotation_deg,
            "inverse_rotation_deg": -snapshot.deskew_rotation_deg,
            "generated_preview_resampled": (
                generated_width != snapshot.model_width
                or generated_height != snapshot.model_height
                or bool(snapshot.deskew_rotation_deg)
            ),
            "source_texture_resampled": False,
            "final_texture_resampled": False,
        }

    def _cancel(self) -> None:
        self._set_status("Codex 이미지 편집 취소를 요청하고 있어요")

        def run() -> None:
            with self._session_lock:
                session = self._session
            if session is None or not session.cancel():
                self._signals.status.emit("취소할 활성 이미지 편집이 없어요")

        threading.Thread(target=run, name="krita-codex-cancel", daemon=True).start()

    def _record_spt_decision(self, decision: str) -> None:
        job_dir = self._last_spt_job_dir
        if job_dir is None:
            self._operation_failed("판정할 SPT 미리보기가 없어요")
            return
        request_path = job_dir / "request.json"
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            spt = request.get("spt")
            generation = request.get("generation")
            if not isinstance(spt, dict) or not isinstance(generation, dict):
                raise ValueError("SPT 생성 기록이 완전하지 않아요")
            artifact = generation.get("artifact")
            if not isinstance(artifact, dict) or not isinstance(artifact.get("sha256"), str):
                raise ValueError("SPT 생성 이미지 SHA 기록이 없어요")
            decision_record = {
                "schema_version": 1,
                "status": decision,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "target_id": spt.get("target_id"),
                "panel_id": spt.get("panel_id"),
                "review_sha256": spt.get("review_sha256"),
                "source_sha256": spt.get("source_sha256"),
                "mask_sha256": spt.get("mask_sha256"),
                "generated_sha256": artifact["sha256"],
                "request": "request.json",
                "next_gate": (
                    "panel-ocr-and-official-compositor"
                    if decision == "selected-for-panel-ocr"
                    else "none"
                ),
                "candidate_approved": False,
            }
            _write_json(job_dir / "decision.json", decision_record)
            _update_request(request_path, spt={**spt, "decision": decision})
        except Exception as exc:
            self._operation_failed(f"SPT 초안 판정을 기록하지 못했어요: {exc}")
            return
        self._spt_accept_button.setEnabled(False)
        self._spt_reject_button.setEnabled(False)
        if decision == "selected-for-panel-ocr":
            self._set_status(
                "초안을 패널 OCR 대기로 기록했어요. 아직 후보 승인이 아니며 Codex의 "
                f"공식 합성·후보 OCR·시각 비교가 남아 있어요.\n{job_dir}"
            )
        else:
            self._set_status(
                "초안을 거절로 기록했어요. 레이어는 비교를 위해 남겨 두었고 "
                f"Ctrl+Z로 제거할 수 있어요.\n{job_dir}"
            )

    def _session_for(self, executable: str) -> CodexAppServer:
        with self._session_lock:
            if self._session is not None and self._session_executable != executable:
                self._session.close()
                self._session = None
            if self._session is None:
                self._session = CodexAppServer(executable)
                self._session_executable = executable
            return self._session

    def _connection_ready(self, info: ConnectionInfo) -> None:
        self._set_busy(False)
        self._set_status(
            "연결됨 · "
            f"ChatGPT {info.plan_type or 'unknown'} · imagegen 사용 가능 · {info.user_agent}"
        )

    def _operation_failed(self, message: str) -> None:
        self._pending_snapshot = None
        self._set_busy(False)
        self._set_status(f"오류: {message}")
        QMessageBox.warning(self, "Codex 선택 영역 AI 편집", message)

    def _set_busy(self, value: bool, *, cancellable: bool = False) -> None:
        self._busy = value
        self._check_button.setEnabled(not value)
        self._edit_button.setEnabled(not value)
        self._cancel_button.setEnabled(value and cancellable)
        self._mode.setEnabled(not value)
        self._mode_changed()

    def _set_status(self, message: str) -> None:
        self._status.setText(message)

    def _read_setting(self, key: str, default: str) -> str:
        return Krita.instance().readSetting(PLUGIN_ID, key, default)

    def _write_setting(self, key: str, value: str) -> None:
        Krita.instance().writeSetting(PLUGIN_ID, key, value)

    def _shutdown(self) -> None:
        with self._session_lock:
            session = self._session
            self._session = None
        if session is not None:
            session.close()


def _default_workspace() -> Path:
    documents = QStandardPaths.writableLocation(_DOCUMENTS_LOCATION)
    return Path(documents or str(Path.home() / "Documents")) / "KritaCodexEdits"


def _save_bgra_png(path: Path, pixels: bytes, width: int, height: int) -> None:
    image = QImage(pixels, width, height, width * 4, _FORMAT_ARGB32).copy()
    image.setColorSpace(_SRGB_COLOR_SPACE)
    if image.isNull() or not image.save(str(path), "PNG"):
        raise ValueError(f"편집 원본 PNG를 저장하지 못했어요: {path}")


def _save_mask_png(path: Path, pixels: bytes, width: int, height: int) -> None:
    image = QImage(pixels, width, height, width, _FORMAT_GRAYSCALE8).copy()
    if image.isNull() or not image.save(str(path), "PNG"):
        raise ValueError(f"선택 마스크 PNG를 저장하지 못했어요: {path}")


def _read_mask_bytes(path: Path, width: int, height: int) -> bytes:
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError(f"SPT 마스크가 없거나 허용 파일 크기를 초과했어요: {path}")
    reader = QImageReader(str(path))
    reader.setAutoTransform(False)
    declared = reader.size()
    if not declared.isValid() or (declared.width(), declared.height()) != (width, height):
        raise ValueError(f"SPT 마스크 선언 크기가 review.json과 달라요: {path}")
    image = reader.read()
    if image.isNull():
        raise ValueError(f"SPT 마스크를 읽지 못했어요: {reader.errorString()}")
    if image.format() not in {_FORMAT_GRAYSCALE8, _FORMAT_INDEXED8} or not image.isGrayscale():
        raise ValueError(f"SPT 마스크는 lossless 단일 채널 grayscale PNG여야 해요: {path}")
    image = image.convertToFormat(_FORMAT_GRAYSCALE8)
    return _grayscale_image_bytes(image, width, height)


def _open_or_activate_document(path: Path) -> Any:
    application = Krita.instance()
    wanted = path.resolve()
    document = next(
        (
            item
            for item in application.documents()
            if item.fileName() and Path(item.fileName()).resolve() == wanted
        ),
        None,
    )
    if document is None:
        document = application.openDocument(str(wanted))
        if document is None:
            raise ValueError(f"Krita가 SPT 원본을 열지 못했어요: {wanted}")
        window = application.activeWindow()
        if window is None:
            raise ValueError("SPT 원본을 표시할 Krita 창이 없어요")
        window.addView(document)
    application.setActiveDocument(document)
    document.waitForDone()
    document.refreshProjection()
    document.waitForDone()
    return document


def _verify_document_matches_source(document: Any, source_path: Path) -> None:
    bounds = document.bounds()
    if int(bounds.x()) != 0 or int(bounds.y()) != 0:
        raise ValueError("SPT 불변 원본 문서의 캔버스 offset이 0이 아니에요")
    width = int(bounds.width())
    height = int(bounds.height())
    if width * height > MAX_CONTEXT_PIXELS:
        raise ValueError("SPT 원본이 안전 픽셀 제한을 초과해 전체 불변성을 확인할 수 없어요")
    source_pixels, source_width, source_height = _read_result_bgra(
        source_path,
        width,
        height,
    )
    if (source_width, source_height) != (width, height):
        raise ValueError("SPT 원본 PNG 크기와 Krita 문서 크기가 달라요")
    document_pixels = bytes(document.pixelData(0, 0, width, height))
    if document_pixels != source_pixels:
        raise ValueError(
            "열린 SPT 문서가 불변 원본과 달라요. 기존 미리보기 레이어를 Ctrl+Z로 "
            "제거하거나 저장하지 않고 문서를 다시 연 뒤 작업해 주세요"
        )


def _deskew_panel(
    source_bgra: bytes,
    selection_mask: bytes,
    width: int,
    height: int,
    rotation_deg: float,
) -> tuple[bytes, bytes, int, int]:
    source = QImage(source_bgra, width, height, width * 4, _FORMAT_ARGB32).copy()
    source.setColorSpace(_SRGB_COLOR_SPACE)
    mask = QImage(selection_mask, width, height, width, _FORMAT_GRAYSCALE8).copy()
    transform = QTransform().rotate(rotation_deg)
    rotated_source = source.transformed(transform, _SMOOTH_TRANSFORMATION)
    rotated_mask = mask.transformed(transform, _SMOOTH_TRANSFORMATION)
    if rotated_source.isNull() or rotated_mask.isNull():
        raise ValueError("SPT 라벨 면 정방향 작업 패널을 만들지 못했어요")
    if (rotated_source.width(), rotated_source.height()) != (
        rotated_mask.width(),
        rotated_mask.height(),
    ):
        raise ValueError("SPT 정방향 원본과 선택 마스크 크기가 달라요")
    rotated_source = rotated_source.convertToFormat(_FORMAT_ARGB32)
    rotated_mask = rotated_mask.convertToFormat(_FORMAT_GRAYSCALE8)
    return (
        _argb32_image_bytes(rotated_source),
        _grayscale_image_bytes(
            rotated_mask,
            rotated_mask.width(),
            rotated_mask.height(),
        ),
        rotated_source.width(),
        rotated_source.height(),
    )


def _restore_deskewed_bgra(
    pixels: bytes,
    width: int,
    height: int,
    target_width: int,
    target_height: int,
    inverse_rotation_deg: float,
) -> bytes:
    image = QImage(pixels, width, height, width * 4, _FORMAT_ARGB32).copy()
    image.setColorSpace(_SRGB_COLOR_SPACE)
    restored = image.transformed(
        QTransform().rotate(inverse_rotation_deg),
        _SMOOTH_TRANSFORMATION,
    ).convertToFormat(_FORMAT_ARGB32)
    if restored.width() < target_width or restored.height() < target_height:
        raise ValueError("SPT 역회전 결과가 원본 crop보다 작아 안전하게 복원할 수 없어요")
    x = (restored.width() - target_width) // 2
    y = (restored.height() - target_height) // 2
    restored = restored.copy(x, y, target_width, target_height)
    if restored.isNull() or (restored.width(), restored.height()) != (
        target_width,
        target_height,
    ):
        raise ValueError("SPT 생성 패널을 원본 crop 좌표로 역변환하지 못했어요")
    return _argb32_image_bytes(restored)


def _argb32_image_bytes(image: QImage) -> bytes:
    width = image.width()
    height = image.height()
    row_bytes = width * 4
    pointer = image.constBits()
    byte_count = image.sizeInBytes() if hasattr(image, "sizeInBytes") else image.byteCount()
    pointer.setsize(byte_count)
    raw = bytes(pointer)
    stride = image.bytesPerLine()
    return b"".join(raw[y * stride : y * stride + row_bytes] for y in range(height))


def _grayscale_image_bytes(image: QImage, width: int, height: int) -> bytes:
    pointer = image.constBits()
    byte_count = image.sizeInBytes() if hasattr(image, "sizeInBytes") else image.byteCount()
    pointer.setsize(byte_count)
    raw = bytes(pointer)
    stride = image.bytesPerLine()
    pixels = b"".join(raw[y * stride : y * stride + width] for y in range(height))
    if len(pixels) != width * height:
        raise ValueError("SPT grayscale 마스크 픽셀 길이가 예상과 달라요")
    return pixels


def _read_result_bgra(
    path: Path,
    target_width: int,
    target_height: int,
) -> tuple[bytes, int, int]:
    if target_width * target_height > MAX_CONTEXT_PIXELS:
        raise ValueError("적용할 문맥 crop이 안전 픽셀 제한을 초과했어요")
    if not path.is_file() or path.stat().st_size > MAX_RESULT_FILE_BYTES:
        raise ValueError("생성 이미지 파일이 없거나 허용 크기를 초과했어요")
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    declared_size = reader.size()
    if not declared_size.isValid():
        raise ValueError("생성 이미지 헤더에서 크기를 읽지 못했어요")
    if declared_size.width() * declared_size.height() > MAX_RESULT_PIXELS:
        raise ValueError("생성 이미지의 선언된 픽셀 수가 안전 제한을 초과했어요")
    image = reader.read()
    if image.isNull():
        raise ValueError(f"생성 이미지를 읽지 못했어요: {reader.errorString()}")
    if image.width() * image.height() > MAX_RESULT_PIXELS:
        raise ValueError("생성 이미지의 픽셀 수가 안전 제한을 초과했어요")
    color_space = image.colorSpace()
    if color_space.isValid() and not _is_srgb_color_space(color_space):
        raise ValueError("생성 이미지가 sRGB가 아니어서 색 변형 없이 적용할 수 없어요")
    if not color_space.isValid():
        image.setColorSpace(_SRGB_COLOR_SPACE)
    if image.width() * target_height != image.height() * target_width:
        raise ValueError(
            "생성 이미지 종횡비가 문맥 crop과 달라서 늘이지 않고 적용을 중단했어요"
        )
    generated_width = image.width()
    generated_height = image.height()
    image = image.convertToFormat(_FORMAT_ARGB32)
    if image.width() != target_width or image.height() != target_height:
        image = image.scaled(
            target_width,
            target_height,
            _IGNORE_ASPECT_RATIO,
            _SMOOTH_TRANSFORMATION,
        )
    pointer = image.constBits()
    byte_count = image.sizeInBytes() if hasattr(image, "sizeInBytes") else image.byteCount()
    pointer.setsize(byte_count)
    pixels = bytes(pointer)
    if len(pixels) != target_width * target_height * 4:
        raise ValueError("생성 이미지의 BGRA/U8 픽셀 길이가 예상과 달라요")
    return pixels, generated_width, generated_height


def _is_srgb_color_space(color_space: Any) -> bool:
    return (
        color_space.primaries() == _SRGB_COLOR_SPACE.primaries()
        and color_space.transferFunction() == _SRGB_COLOR_SPACE.transferFunction()
    )


def _node_id(node: Any) -> str:
    value = node.uniqueId()
    return value.toString() if hasattr(value, "toString") else str(value)


def _is_active_document(document: Any) -> bool:
    active = Krita.instance().activeDocument()
    return active is not None and active == document


def _preflight_composite(snapshot: _Snapshot, layer_pixels: bytes) -> None:
    crop = snapshot.crop
    temporary = Krita.instance().createDocument(
        crop.width,
        crop.height,
        "Codex composite preflight",
        "RGBA",
        "U8",
        snapshot.color_profile,
        72.0,
    )
    if temporary is None:
        raise ValueError("임시 합성 검사 문서를 만들지 못했어요")
    try:
        children = temporary.rootNode().childNodes()
        if not children:
            raise ValueError("임시 합성 검사 문서에 기본 paint layer가 없어요")
        base = children[0]
        if not base.setPixelData(
            QByteArray(snapshot.source_bgra),
            0,
            0,
            crop.width,
            crop.height,
        ):
            raise ValueError("임시 합성 검사에 원본 픽셀을 기록하지 못했어요")
        candidate = temporary.createNode("Codex preflight candidate", "paintlayer")
        if candidate is None or not candidate.setPixelData(
            QByteArray(layer_pixels),
            0,
            0,
            crop.width,
            crop.height,
        ):
            raise ValueError("임시 합성 검사에 후보 픽셀을 기록하지 못했어요")
        if not temporary.rootNode().addChildNode(candidate, None):
            raise ValueError("임시 합성 검사 레이어를 추가하지 못했어요")
        temporary.refreshProjection()
        temporary.waitForDone()
        projected = bytes(temporary.pixelData(0, 0, crop.width, crop.height))
        validate_projection_invariants(
            snapshot.source_bgra,
            projected,
            snapshot.selection_mask,
            crop.width,
            crop.height,
        )
    finally:
        temporary.close()


def _undo_failed_attach(document: Any, layer: Any, snapshot: _Snapshot) -> None:
    layer.setVisible(False)
    document.refreshProjection()
    document.waitForDone()
    if not _is_active_document(document):
        raise RuntimeError("원래 문서가 활성 상태가 아니라 전역 Undo를 실행하지 않았어요")
    action = Krita.instance().action("edit_undo")
    if action is None or not action.isEnabled():
        raise RuntimeError("Krita edit_undo 액션을 실행할 수 없어요")
    layer_id = _node_id(layer)
    action.trigger()
    document.waitForDone()
    document.refreshProjection()
    document.waitForDone()
    if any(_node_id(child) == layer_id for child in document.rootNode().childNodes()):
        raise RuntimeError("Undo 뒤에도 실패 레이어가 문서에 남아 있어요")
    crop = snapshot.crop
    restored = bytes(document.pixelData(crop.x, crop.y, crop.width, crop.height))
    if restored != snapshot.source_bgra:
        raise RuntimeError("Undo 뒤 문서 투영이 적용 전 픽셀로 복구되지 않았어요")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _update_request(path: Path, **updates: Any) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(updates)
    value["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(path, value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


Krita.instance().addDockWidgetFactory(
    DockWidgetFactory(
        "golaniCodexImageEditDocker",
        DockWidgetFactoryBase.DockRight,
        CodexSelectionEditDocker,
    )
)
