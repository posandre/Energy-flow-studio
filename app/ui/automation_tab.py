from __future__ import annotations

from dataclasses import replace
import json
import re
import uuid
from typing import Iterable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal, QTimer, QLineF
from PySide6.QtGui import QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QFileDialog,
    QMessageBox,
    QSpinBox,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QScrollArea,
    QSizePolicy,
    QStyle,
)

from app.services.automations import (
    AutomationAction,
    AutomationCondition,
    AutomationRule,
    build_flow_graph_from_blocks,
    build_rule_analytics,
    deserialize_rules,
    flow_blocks_from_graph,
)
from app.services.i18n import tr
from app.services.tuya_api import TuyaDevice
from app.ui.dialogs import ask_compact_confirmation

BLOCK_MIME = "application/x-energyflow-automation-block"
INVERTER_DEVICE_ID = "__inverter__"


class BlockPalette(QListWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        item = self.currentItem()
        if item is None:
            return
        block_type = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not block_type:
            return
        mime = QMimeData()
        mime.setData(BLOCK_MIME, block_type.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class PaletteTileButton(QToolButton):
    block_requested = Signal(str)

    def __init__(self, block_type: str, icon: str, title: str, tooltip: str, parent=None) -> None:
        super().__init__(parent)
        self._block_type = block_type
        self._drag_start = None
        self._dragging = False
        self.setObjectName("AutomationPaletteTile")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("")
        self.setText(title)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setIcon(self._symbol_icon(icon))
        self.setIconSize(QSize(30, 30))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCheckable(False)

    @staticmethod
    def _symbol_icon(symbol: str) -> QIcon:
        pixmap = QPixmap(72, 72)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor("#dbeafe"))
        font = QFont()
        font.setPixelSize(52)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, symbol)
        painter.end()
        return QIcon(pixmap)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position().toPoint()
            self._dragging = False
            self.setDown(False)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return super().mouseMoveEvent(event)
        if self._drag_start is None:
            return super().mouseMoveEvent(event)
        if (event.position().toPoint() - self._drag_start).manhattanLength() < QApplication.startDragDistance():
            return super().mouseMoveEvent(event)
        self._dragging = True
        mime = QMimeData()
        mime.setData(BLOCK_MIME, self._block_type.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        drag.exec(Qt.DropAction.CopyAction)
        self._reset_interaction_state()
        event.accept()
        return

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        if self._dragging:
            self._dragging = False
            self._reset_interaction_state()
            event.accept()
            return
        if self._drag_start is not None:
            self.block_requested.emit(self._block_type)
        self._reset_interaction_state()
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._reset_interaction_state()
        super().leaveEvent(event)

    def _reset_interaction_state(self) -> None:
        self.setDown(False)
        self.clearFocus()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_start = None
        self._dragging = False
        self.update()


class FlowCanvas(QListWidget):
    block_added = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_MIME):
            block_type = bytes(event.mimeData().data(BLOCK_MIME)).decode("utf-8").strip()
            if block_type:
                self.block_added.emit(block_type)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class FlowDiagramView(QWidget):
    block_selected = Signal(int)
    block_added = Signal(str)
    palette_block_dropped_to_gate = Signal(str, int)
    palette_block_dropped_to_nested_gate = Signal(str, int, int)
    blocks_reordered = Signal(list)
    block_delete_requested = Signal(int)
    condition_dropped_to_gate = Signal(int, int)
    condition_dropped_to_nested_gate = Signal(int, int, int)
    gate_condition_delete_requested = Signal(int, int)
    gate_condition_selected = Signal(int, int)
    gate_condition_reordered = Signal(int, int, int)
    gate_condition_extract_requested = Signal(int, int, int)
    gate_condition_dropped_to_nested_gate = Signal(int, int, int)
    gate_condition_dropped_to_parent_gate = Signal(int, int)
    branch_connected = Signal(int, str, int, int, int)
    branch_deleted = Signal(int, str, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._blocks: list[dict[str, object]] = []
        self._labels: list[str] = []
        self._selected_index = -1
        self._runtime_active_index = -1
        self._runtime_active_branch: tuple[int, str] | None = None
        self._selected_gate_condition: tuple[int, int] | None = None
        self._hit_areas: list[QRectF] = []
        self._delete_hit_areas: list[QRectF] = []
        self._move_hit_areas: list[QRectF] = []
        self._drag_origin_index = -1
        self._drag_insert_index = -1
        self._drag_gate_target_index = -1
        self._drag_active = False
        self._nested_drag_active = False
        self._nested_drag_gate_index = -1
        self._nested_drag_condition_index = -1
        self._nested_drag_insert_index = -1
        self._nested_drag_reorder_target = -1
        self._drag_nested_gate_target: tuple[int, int] | None = None
        self._drag_hover_block_index = -1
        self._drag_side_target_index = -1
        self._dash_phase = 0.0
        self._dash_timer = QTimer(self)
        self._dash_timer.setInterval(60)
        self._dash_timer.timeout.connect(self._advance_dash_phase)
        self._content_h = 220
        self._gate_condition_delete_hit_areas: list[tuple[int, int, QRectF]] = []
        self._gate_condition_row_hit_areas: list[tuple[int, int, QRectF]] = []
        self._gate_condition_drag_hit_areas: list[tuple[int, int, QRectF]] = []
        self._input_socket_hit_areas: list[tuple[int, int, QRectF]] = []
        self._input_label_hit_areas: list[tuple[int, QRectF]] = []
        self._true_socket_hit_areas: list[tuple[int, QRectF]] = []
        self._true_label_hit_areas: list[tuple[int, QRectF]] = []
        self._false_socket_hit_areas: list[tuple[int, QRectF]] = []
        self._false_label_hit_areas: list[tuple[int, QRectF]] = []
        self._next_socket_hit_areas: list[tuple[int, int, QRectF]] = []
        self._next_label_hit_areas: list[tuple[int, QRectF]] = []
        self._resize_left_hit_areas: list[tuple[int, QRectF]] = []
        self._resize_right_hit_areas: list[tuple[int, QRectF]] = []
        self._connection_drag_active = False
        self._connection_source_index = -1
        self._connection_branch = ""
        self._connection_hover_target_index = -1
        self._connection_hover_target_socket_index = -1
        self._connection_cursor_point = QPointF()
        self._connection_source_rect = QRectF()
        self._connection_source_socket_index = 0
        self._connection_drag_mode = ""
        self._connection_drag_changed = False
        self._connection_drag_start = QPointF()
        self._connection_drag_from_label = False
        self._resize_drag_active = False
        self._resize_drag_index = -1
        self._resize_drag_side = ""
        self._resize_anchor_x = 0.0
        self._align_drag_active = False
        self._align_drag_index = -1
        self._align_drag_changed = False
        self._socket_drag_active = False
        self._socket_drag_index = -1
        self._socket_drag_socket_index = -1
        self._socket_drag_kind = ""
        self._socket_drag_changed = False
        self._connection_hit_areas: list[tuple[int, str, int, QLineF]] = []
        self._connection_hover: tuple[int, str, int] | None = None
        self._connection_delete_hit_areas: list[tuple[int, str, int, QRectF]] = []
        self._connection_shelf_hit_areas: list[tuple[int, str, int, str, QRectF]] = []
        self._connection_shelf_drag_active = False
        self._connection_shelf_drag_source = -1
        self._connection_shelf_drag_target = -1
        self._connection_shelf_drag_branch = ""
        self._connection_shelf_drag_axis = "y"
        self._connection_shelf_drag_changed = False
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setMinimumHeight(220)

    def set_blocks(self, blocks: list[dict[str, object]], labels: list[str]) -> None:
        self._blocks = [dict(item) for item in blocks]
        self._normalize_row_groups()
        self._labels = [str(item) for item in labels]
        if self._selected_index >= len(self._blocks):
            self._selected_index = len(self._blocks) - 1
        self._content_h = self._content_height()
        self.updateGeometry()
        self.update()

    def set_selected_index(self, index: int) -> None:
        if self._selected_index != index:
            self._selected_gate_condition = None
        self._selected_index = index
        self.update()

    def set_selected_gate_condition(self, gate_index: int, condition_index: int) -> None:
        if gate_index < 0 or condition_index < 0:
            self._selected_gate_condition = None
            self.update()
            return
        self._selected_index = gate_index
        self._selected_gate_condition = (gate_index, condition_index)
        self.update()

    def selected_index(self) -> int:
        return self._selected_index

    def set_runtime_active_index(self, index: int) -> None:
        normalized = index if 0 <= index < len(self._blocks) else -1
        if self._runtime_active_index == normalized:
            return
        self._runtime_active_index = normalized
        self.update()

    def set_runtime_active_branch(self, index: int, branch: str) -> None:
        normalized_branch = str(branch or "").strip().lower()
        if normalized_branch not in {"true", "false", "next"}:
            normalized = None
        elif 0 <= index < len(self._blocks):
            normalized = (index, normalized_branch)
        else:
            normalized = None
        if self._runtime_active_branch == normalized:
            return
        self._runtime_active_branch = normalized
        self.update()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_MIME):
            self._update_external_drag_target(event.position(), event.mimeData())
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_MIME):
            self._update_external_drag_target(event.position(), event.mimeData())
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(BLOCK_MIME):
            block_type = bytes(event.mimeData().data(BLOCK_MIME)).decode("utf-8").strip()
            if block_type:
                nested_target = self._nested_gate_drop_target_for_palette(event.position())
                if nested_target is not None:
                    gate_index, child_index = nested_target
                    self.block_selected.emit(gate_index)
                    self.palette_block_dropped_to_nested_gate.emit(block_type, gate_index, child_index)
                    event.acceptProposedAction()
                    return
                gate_index = self._gate_drop_target_for_palette(event.position())
                if gate_index >= 0:
                    self.block_selected.emit(gate_index)
                    self.palette_block_dropped_to_gate.emit(block_type, gate_index)
                else:
                    self.block_added.emit(block_type)
            event.acceptProposedAction()
            self._drag_hover_block_index = -1
            self._drag_nested_gate_target = None
            self._ensure_dash_animation(False)
            self.update()
            return
        super().dropEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        point = event.position()
        # Sockets must have higher priority than connection hit areas, otherwise
        # a nearby line/delete handle can "steal" click/drag from the socket.
        for idx, socket_index, rect in self._input_socket_hit_areas:
            if rect.contains(point):
                if not self._has_input_connection(idx, socket_index):
                    self._socket_drag_active = False
                    self._connection_drag_active = False
                    self.block_selected.emit(idx)
                    self.setCursor(Qt.CursorShape.CrossCursor)
                    return
                self._connection_drag_active = False
                self._socket_drag_active = True
                self._socket_drag_index = idx
                self._socket_drag_socket_index = socket_index
                self._socket_drag_kind = "input"
                self._socket_drag_changed = False
                self.block_selected.emit(idx)
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        for idx, socket_index, rect in self._next_socket_hit_areas:
            if rect.contains(point):
                if not self._has_output_connection(idx, "next", socket_index):
                    self._socket_drag_active = False
                    self._connection_drag_active = True
                    self._connection_source_index = idx
                    self._connection_source_socket_index = socket_index
                    self._connection_branch = "next"
                    self._connection_hover_target_index = -1
                    self._connection_hover_target_socket_index = -1
                    self._connection_cursor_point = QPointF(point)
                    self._connection_drag_mode = "connect"
                    self._connection_drag_from_label = False
                    self.setCursor(Qt.CursorShape.CrossCursor)
                    self.update()
                    self.block_selected.emit(idx)
                    return
                self._connection_drag_active = False
                self._socket_drag_active = True
                self._socket_drag_index = idx
                self._socket_drag_socket_index = socket_index
                self._socket_drag_kind = "next"
                self._socket_drag_changed = False
                self.block_selected.emit(idx)
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        for idx, rect in self._true_socket_hit_areas:
            if rect.contains(point):
                if not self._has_output_connection(idx, "true", 0):
                    self._socket_drag_active = False
                    self._connection_drag_active = True
                    self._connection_source_index = idx
                    self._connection_source_socket_index = 0
                    self._connection_branch = "true"
                    self._connection_hover_target_index = -1
                    self._connection_hover_target_socket_index = -1
                    self._connection_cursor_point = QPointF(point)
                    self._connection_drag_mode = "connect"
                    self._connection_drag_from_label = False
                    self.setCursor(Qt.CursorShape.CrossCursor)
                    self.update()
                    self.block_selected.emit(idx)
                    return
                self._connection_drag_active = False
                self._socket_drag_active = True
                self._socket_drag_index = idx
                self._socket_drag_socket_index = 0
                self._socket_drag_kind = "true"
                self._socket_drag_changed = False
                self.block_selected.emit(idx)
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        for idx, rect in self._false_socket_hit_areas:
            if rect.contains(point):
                if not self._has_output_connection(idx, "false", 0):
                    self._socket_drag_active = False
                    self._connection_drag_active = True
                    self._connection_source_index = idx
                    self._connection_source_socket_index = 0
                    self._connection_branch = "false"
                    self._connection_hover_target_index = -1
                    self._connection_hover_target_socket_index = -1
                    self._connection_cursor_point = QPointF(point)
                    self._connection_drag_mode = "connect"
                    self._connection_drag_from_label = False
                    self.setCursor(Qt.CursorShape.CrossCursor)
                    self.update()
                    self.block_selected.emit(idx)
                    return
                self._connection_drag_active = False
                self._socket_drag_active = True
                self._socket_drag_index = idx
                self._socket_drag_socket_index = 0
                self._socket_drag_kind = "false"
                self._socket_drag_changed = False
                self.block_selected.emit(idx)
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        for source_index, branch, target_index, rect in self._connection_delete_hit_areas:
            if rect.contains(point):
                self.branch_deleted.emit(source_index, branch, target_index)
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        for source_index, branch, target_index, axis, rect in self._connection_shelf_hit_areas:
            if rect.contains(point):
                self._connection_shelf_drag_active = True
                self._connection_shelf_drag_source = source_index
                self._connection_shelf_drag_target = target_index
                self._connection_shelf_drag_branch = branch
                self._connection_shelf_drag_axis = axis
                self._connection_shelf_drag_changed = False
                self.block_selected.emit(source_index)
                self.setCursor(Qt.CursorShape.SizeVerCursor if axis == "y" else Qt.CursorShape.SizeHorCursor)
                return
        for idx, rect in self._resize_left_hit_areas:
            if rect.contains(point):
                self._resize_drag_active = True
                self._resize_drag_index = idx
                self._resize_drag_side = "left"
                source_rect = self._hit_areas[idx] if 0 <= idx < len(self._hit_areas) else QRectF()
                self._resize_anchor_x = source_rect.right()
                self.block_selected.emit(idx)
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        for idx, rect in self._resize_right_hit_areas:
            if rect.contains(point):
                self._resize_drag_active = True
                self._resize_drag_index = idx
                self._resize_drag_side = "right"
                source_rect = self._hit_areas[idx] if 0 <= idx < len(self._hit_areas) else QRectF()
                self._resize_anchor_x = source_rect.left()
                self.block_selected.emit(idx)
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        self._drag_nested_gate_target = None
        for gate_index, condition_index, rect in self._gate_condition_drag_hit_areas:
            if rect.contains(point):
                self._nested_drag_active = True
                self._nested_drag_gate_index = gate_index
                self._nested_drag_condition_index = condition_index
                self._nested_drag_insert_index = -1
                self._nested_drag_reorder_target = -1
                self._drag_active = False
                self._selected_gate_condition = (gate_index, condition_index)
                self.block_selected.emit(gate_index)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return
        for gate_index, condition_index, rect in self._gate_condition_delete_hit_areas:
            if rect.contains(point):
                self._drag_active = False
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self._selected_gate_condition = (gate_index, condition_index)
                self.block_selected.emit(gate_index)
                self.gate_condition_delete_requested.emit(gate_index, condition_index)
                self.update()
                return
        for gate_index, condition_index, rect in self._gate_condition_row_hit_areas:
            if rect.contains(point):
                self._drag_active = False
                self._nested_drag_active = False
                self._selected_gate_condition = (gate_index, condition_index)
                self.block_selected.emit(gate_index)
                self.gate_condition_selected.emit(gate_index, condition_index)
                self.update()
                self._update_hover_cursor(point)
                return
        for idx, rect in enumerate(self._delete_hit_areas):
            if rect.contains(point):
                self._drag_active = False
                self._nested_drag_active = False
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.block_delete_requested.emit(idx)
                return
        for idx, rect in enumerate(self._move_hit_areas):
            if rect.contains(point):
                self._drag_origin_index = idx
                self._drag_insert_index = idx
                self._drag_gate_target_index = -1
                self._drag_active = True
                self._nested_drag_active = False
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.block_selected.emit(idx)
                return
        for idx, rect in enumerate(self._hit_areas):
            if rect.contains(point):
                self._drag_origin_index = idx
                self._drag_insert_index = idx
                self._drag_gate_target_index = -1
                self._drag_active = False
                self._nested_drag_active = False
                self._selected_gate_condition = None
                if self._block_width_ratio(self._blocks[idx]) < 0.999:
                    self._align_drag_active = True
                    self._align_drag_index = idx
                    self._align_drag_changed = False
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                else:
                    self._update_hover_cursor(point)
                self.block_selected.emit(idx)
                return
        self._update_hover_cursor(point)
        self._drag_active = False
        self._nested_drag_active = False
        self._selected_gate_condition = None
        self._selected_index = -1
        self.block_selected.emit(-1)
        self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        point = event.position()
        if self._connection_shelf_drag_active:
            if self._set_connection_shelf_position(
                source_index=self._connection_shelf_drag_source,
                branch=self._connection_shelf_drag_branch,
                target_index=self._connection_shelf_drag_target,
                point=QPointF(point),
                axis=self._connection_shelf_drag_axis,
            ):
                self._connection_shelf_drag_changed = True
                self.update()
            self.setCursor(Qt.CursorShape.SizeVerCursor if self._connection_shelf_drag_axis == "y" else Qt.CursorShape.SizeHorCursor)
            super().mouseMoveEvent(event)
            return
        if self._resize_drag_active and self._resize_drag_index >= 0:
            source_rect = self._hit_areas[self._resize_drag_index] if 0 <= self._resize_drag_index < len(self._hit_areas) else QRectF()
            if not source_rect.isValid():
                super().mouseMoveEvent(event)
                return
            canvas_width = self._canvas_inner_width()
            block = self._blocks[self._resize_drag_index] if 0 <= self._resize_drag_index < len(self._blocks) else {}
            block_align = self._block_align(block) if isinstance(block, dict) else "left"
            partner_idx = self._row_partner_index(self._resize_drag_index)
            max_ratio = 1.0
            if partner_idx >= 0 and 0 <= partner_idx < len(self._blocks):
                partner = self._blocks[partner_idx]
                if isinstance(partner, dict):
                    partner_ratio = self._block_width_ratio(partner)
                    max_ratio = max(0.35, min(1.0, 1.0 - partner_ratio))
            max_width = canvas_width * max_ratio
            if block_align == "center":
                center_x = source_rect.center().x()
                new_width = self._clamp(abs(point.x() - center_x) * 2.0, canvas_width * 0.35, max_width)
                changed = self._set_block_layout(self._resize_drag_index, width_ratio=(new_width / canvas_width), align="center")
            elif self._resize_drag_side == "left":
                new_width = self._clamp(self._resize_anchor_x - point.x(), canvas_width * 0.35, max_width)
                changed = self._set_block_layout(self._resize_drag_index, width_ratio=(new_width / canvas_width), align="right")
            else:
                new_width = self._clamp(point.x() - self._resize_anchor_x, canvas_width * 0.35, max_width)
                changed = self._set_block_layout(self._resize_drag_index, width_ratio=(new_width / canvas_width), align="left")
            if changed:
                self.update()
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            super().mouseMoveEvent(event)
            return
        if self._socket_drag_active and self._socket_drag_index >= 0 and self._socket_drag_kind:
            block_rect = self._hit_areas[self._socket_drag_index] if 0 <= self._socket_drag_index < len(self._hit_areas) else QRectF()
            if block_rect.width() > 1.0:
                ratio = (point.x() - block_rect.left()) / block_rect.width()
                if self._set_socket_ratio(
                    self._socket_drag_index,
                    self._socket_drag_kind,
                    ratio,
                    socket_index=self._socket_drag_socket_index,
                ):
                    self._socket_drag_changed = True
                    self.update()
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            super().mouseMoveEvent(event)
            return
        if self._align_drag_active and self._align_drag_index >= 0:
            block = self._blocks[self._align_drag_index] if 0 <= self._align_drag_index < len(self._blocks) else {}
            if isinstance(block, dict) and self._block_width_ratio(block) < 0.999:
                canvas_left = 16.0
                canvas_width = self._canvas_inner_width()
                relative_x = self._clamp((point.x() - canvas_left) / canvas_width, 0.0, 1.0)
                if relative_x < 0.34:
                    preview_align = "left"
                elif relative_x > 0.66:
                    preview_align = "right"
                else:
                    preview_align = "center"
                if self._set_block_layout(self._align_drag_index, align=preview_align):
                    self._align_drag_changed = True
                    self.update()
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            super().mouseMoveEvent(event)
            return
        if self._connection_drag_active:
            self._connection_cursor_point = QPointF(point)
            target_index, target_socket_index = self._input_target_for_point(point, self._connection_source_index)
            self._connection_hover_target_index = target_index
            self._connection_hover_target_socket_index = target_socket_index
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            super().mouseMoveEvent(event)
            return
        hovered_connection = self._connection_at_point(point)
        if hovered_connection != self._connection_hover:
            self._connection_hover = hovered_connection
            self.update()
        self._update_hover_cursor(point)
        if self._nested_drag_active and self._nested_drag_gate_index >= 0 and self._nested_drag_condition_index >= 0:
            nested_target = self._nested_gate_row_drop_target(
                self._nested_drag_gate_index,
                point,
                self._nested_drag_condition_index,
            )
            reorder_target = self._nested_gate_row_reorder_target(
                self._nested_drag_gate_index,
                point,
                self._nested_drag_condition_index,
            )
            if nested_target >= 0:
                self._drag_nested_gate_target = (self._nested_drag_gate_index, nested_target)
                self._nested_drag_reorder_target = -1
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(True)
            elif reorder_target >= 0:
                self._drag_nested_gate_target = None
                self._nested_drag_reorder_target = reorder_target
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(True)
            else:
                self._drag_nested_gate_target = None
                self._nested_drag_reorder_target = -1
                source_gate_rect = self._hit_areas[self._nested_drag_gate_index] if 0 <= self._nested_drag_gate_index < len(self._hit_areas) else QRectF()
                if source_gate_rect.contains(point):
                    # Allow dropping nested items back into the parent (root) gate.
                    self._drag_hover_block_index = self._nested_drag_gate_index
                    self._ensure_dash_animation(True)
                else:
                    self._drag_hover_block_index = -1
                    self._ensure_dash_animation(False)
            source_gate_rect = self._hit_areas[self._nested_drag_gate_index] if 0 <= self._nested_drag_gate_index < len(self._hit_areas) else QRectF()
            if source_gate_rect.contains(point):
                self._nested_drag_insert_index = -1
            else:
                self._nested_drag_insert_index = self._top_level_insert_index_for_point(point.y())
            self.update()
            super().mouseMoveEvent(event)
            return
        if not self._drag_active or self._drag_origin_index < 0:
            self._ensure_dash_animation(False)
            self._drag_hover_block_index = -1
            self._drag_nested_gate_target = None
            self._drag_side_target_index = -1
            super().mouseMoveEvent(event)
            return
        nested_target = self._nested_gate_drop_target_for_point(point)
        if nested_target is not None:
            gate_index, flat_index = nested_target
            if self._drag_origin_index != gate_index:
                self._drag_nested_gate_target = (gate_index, flat_index)
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(True)
            else:
                self._drag_nested_gate_target = None
                gate_target = self._gate_drop_target_at(point, self._drag_origin_index)
                self._drag_hover_block_index = gate_target if gate_target >= 0 else -1
                self._ensure_dash_animation(gate_target >= 0)
        else:
            self._drag_nested_gate_target = None
            gate_target = self._gate_drop_target_at(point, self._drag_origin_index)
            self._drag_hover_block_index = gate_target if gate_target >= 0 else -1
            self._ensure_dash_animation(gate_target >= 0)
        side_target = self._side_slot_target_for_point(point, self._drag_origin_index)
        if side_target >= 0:
            self._drag_side_target_index = side_target
            self._drag_hover_block_index = -1
            self._drag_gate_target_index = -1
            self._drag_insert_index = side_target + 1
            self._ensure_dash_animation(True)
            self.update()
            super().mouseMoveEvent(event)
            return
        self._drag_side_target_index = -1
        self._drag_gate_target_index = self._drag_hover_block_index
        insert_index = len(self._hit_areas)
        for idx, rect in enumerate(self._hit_areas):
            if point.y() < rect.center().y():
                insert_index = idx
                break
        self._drag_insert_index = insert_index
        self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        drop_point = event.position()
        if self._connection_shelf_drag_active:
            changed = self._connection_shelf_drag_changed
            self._connection_shelf_drag_active = False
            self._connection_shelf_drag_source = -1
            self._connection_shelf_drag_target = -1
            self._connection_shelf_drag_branch = ""
            self._connection_shelf_drag_axis = "y"
            self._connection_shelf_drag_changed = False
            if changed:
                self.blocks_reordered.emit([dict(item) for item in self._blocks if isinstance(item, dict)])
            self._update_hover_cursor(drop_point)
            self.update()
            super().mouseReleaseEvent(event)
            return
        if self._resize_drag_active:
            self._resize_drag_active = False
            self._resize_drag_index = -1
            self._resize_drag_side = ""
            self._resize_anchor_x = 0.0
            self._drag_origin_index = -1
            self._drag_insert_index = -1
            self._normalize_row_groups()
            self.blocks_reordered.emit([dict(item) for item in self._blocks if isinstance(item, dict)])
            self._update_hover_cursor(drop_point)
            self.update()
            super().mouseReleaseEvent(event)
            return
        if self._socket_drag_active:
            changed = self._socket_drag_changed
            self._socket_drag_active = False
            self._socket_drag_index = -1
            self._socket_drag_socket_index = -1
            self._socket_drag_kind = ""
            self._socket_drag_changed = False
            self._drag_origin_index = -1
            self._drag_insert_index = -1
            if changed:
                self._normalize_row_groups()
                self.blocks_reordered.emit([dict(item) for item in self._blocks if isinstance(item, dict)])
            self._update_hover_cursor(drop_point)
            self.update()
            super().mouseReleaseEvent(event)
            return
        if self._align_drag_active:
            changed = self._align_drag_changed
            self._align_drag_active = False
            self._align_drag_index = -1
            self._align_drag_changed = False
            self._drag_origin_index = -1
            self._drag_insert_index = -1
            if changed:
                self._normalize_row_groups()
                self.blocks_reordered.emit([dict(item) for item in self._blocks if isinstance(item, dict)])
            self._update_hover_cursor(drop_point)
            self.update()
            super().mouseReleaseEvent(event)
            return
        if self._connection_drag_active:
            target_index, target_socket_index = self._input_target_for_point(drop_point, self._connection_source_index)
            if (
                target_index >= 0
                and self._connection_source_index >= 0
                and self._connection_branch in {"true", "false", "next"}
            ):
                self.branch_connected.emit(
                    self._connection_source_index,
                    self._connection_branch,
                    target_index,
                    max(0, target_socket_index),
                    max(0, self._connection_source_socket_index),
                )
            self._connection_drag_active = False
            self._connection_source_index = -1
            self._connection_source_socket_index = 0
            self._connection_branch = ""
            self._connection_hover_target_index = -1
            self._connection_hover_target_socket_index = -1
            self._connection_source_rect = QRectF()
            self._connection_drag_mode = ""
            self._connection_drag_changed = False
            self._connection_drag_from_label = False
            self._ensure_dash_animation(False)
            self._drag_side_target_index = -1
            self._update_hover_cursor(drop_point)
            self.update()
            super().mouseReleaseEvent(event)
            return
        if (
            self._nested_drag_active
            and self._nested_drag_gate_index >= 0
            and self._nested_drag_condition_index >= 0
        ):
            nested_gate_target = self._nested_gate_row_drop_target(
                self._nested_drag_gate_index,
                drop_point,
                self._nested_drag_condition_index,
            )
            if nested_gate_target >= 0:
                source_parent_path = self._gate_child_parent_path(
                    self._nested_drag_gate_index,
                    self._nested_drag_condition_index,
                )
                target_parent_path = self._gate_child_parent_path(
                    self._nested_drag_gate_index,
                    nested_gate_target,
                )
                if (
                    source_parent_path is not None
                    and target_parent_path is not None
                    and source_parent_path == target_parent_path
                ):
                    # If pointer is over a gate on the same nesting level, treat it as reorder.
                    self.gate_condition_reordered.emit(
                        self._nested_drag_gate_index,
                        self._nested_drag_condition_index,
                        nested_gate_target,
                    )
                    self._nested_drag_active = False
                    self._nested_drag_gate_index = -1
                    self._nested_drag_condition_index = -1
                    self._nested_drag_insert_index = -1
                    self._nested_drag_reorder_target = -1
                    self._drag_nested_gate_target = None
                    self._update_hover_cursor(drop_point)
                    self._drag_hover_block_index = -1
                    self._ensure_dash_animation(False)
                    self.update()
                    super().mouseReleaseEvent(event)
                    return
                self.gate_condition_dropped_to_nested_gate.emit(
                    self._nested_drag_gate_index,
                    self._nested_drag_condition_index,
                    nested_gate_target,
                )
                self._nested_drag_active = False
                self._nested_drag_gate_index = -1
                self._nested_drag_condition_index = -1
                self._nested_drag_insert_index = -1
                self._nested_drag_reorder_target = -1
                self._drag_nested_gate_target = None
                self._update_hover_cursor(drop_point)
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(False)
                self.update()
                super().mouseReleaseEvent(event)
                return
            if self._nested_drag_reorder_target >= 0:
                self.gate_condition_reordered.emit(
                    self._nested_drag_gate_index,
                    self._nested_drag_condition_index,
                    self._nested_drag_reorder_target,
                )
                self._nested_drag_active = False
                self._nested_drag_gate_index = -1
                self._nested_drag_condition_index = -1
                self._nested_drag_insert_index = -1
                self._nested_drag_reorder_target = -1
                self._drag_nested_gate_target = None
                self._update_hover_cursor(drop_point)
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(False)
                self.update()
                super().mouseReleaseEvent(event)
                return
            reorder_target = self._nested_gate_row_reorder_target(
                self._nested_drag_gate_index,
                drop_point,
                self._nested_drag_condition_index,
            )
            if reorder_target >= 0:
                self.gate_condition_reordered.emit(
                    self._nested_drag_gate_index,
                    self._nested_drag_condition_index,
                    reorder_target,
                )
                self._nested_drag_active = False
                self._nested_drag_gate_index = -1
                self._nested_drag_condition_index = -1
                self._nested_drag_insert_index = -1
                self._nested_drag_reorder_target = -1
                self._drag_nested_gate_target = None
                self._update_hover_cursor(drop_point)
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(False)
                self.update()
                super().mouseReleaseEvent(event)
                return
            source_gate_rect = self._hit_areas[self._nested_drag_gate_index] if 0 <= self._nested_drag_gate_index < len(self._hit_areas) else QRectF()
            if source_gate_rect.contains(drop_point):
                self.gate_condition_dropped_to_parent_gate.emit(
                    self._nested_drag_gate_index,
                    self._nested_drag_condition_index,
                )
                self._nested_drag_active = False
                self._nested_drag_gate_index = -1
                self._nested_drag_condition_index = -1
                self._nested_drag_insert_index = -1
                self._nested_drag_reorder_target = -1
                self._drag_nested_gate_target = None
                self._update_hover_cursor(drop_point)
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(False)
                self.update()
                super().mouseReleaseEvent(event)
                return
            if self._nested_drag_insert_index >= 0:
                self.gate_condition_extract_requested.emit(
                    self._nested_drag_gate_index,
                    self._nested_drag_condition_index,
                    self._nested_drag_insert_index,
                )
            self._nested_drag_active = False
            self._nested_drag_gate_index = -1
            self._nested_drag_condition_index = -1
            self._nested_drag_insert_index = -1
            self._nested_drag_reorder_target = -1
            self._drag_nested_gate_target = None
            self._update_hover_cursor(drop_point)
            self._drag_hover_block_index = -1
            self._ensure_dash_animation(False)
            self.update()
            super().mouseReleaseEvent(event)
            return
        if (
            self._drag_active
            and self._drag_origin_index >= 0
            and self._drag_insert_index >= 0
            and 0 <= self._drag_origin_index < len(self._blocks)
        ):
            source_block = self._blocks[self._drag_origin_index]
            source_type = str(source_block.get("type", "")).strip().lower()
            nested_target = self._nested_gate_drop_target_for_point(drop_point)
            if (
                nested_target is not None
                and source_type in {"condition", "gate"}
                and self._drag_origin_index != nested_target[0]
            ):
                gate_index, child_index = nested_target
                self.block_selected.emit(gate_index)
                self.condition_dropped_to_nested_gate.emit(self._drag_origin_index, gate_index, child_index)
                self._drag_origin_index = -1
                self._drag_insert_index = -1
                self._drag_gate_target_index = -1
                self._drag_active = False
                self._drag_nested_gate_target = None
                self._drag_side_target_index = -1
                self._update_hover_cursor(drop_point)
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(False)
                self.update()
                super().mouseReleaseEvent(event)
                return
            drop_gate_index = self._drag_gate_target_index
            if drop_gate_index >= 0 and source_type in {"condition", "gate"}:
                self.block_selected.emit(drop_gate_index)
                self.condition_dropped_to_gate.emit(self._drag_origin_index, drop_gate_index)
                self._drag_origin_index = -1
                self._drag_insert_index = -1
                self._drag_gate_target_index = -1
                self._drag_active = False
                self._drag_nested_gate_target = None
                self._drag_side_target_index = -1
                self._update_hover_cursor(drop_point)
                self._drag_hover_block_index = -1
                self._ensure_dash_animation(False)
                self.update()
                super().mouseReleaseEvent(event)
                return
            block = self._blocks.pop(self._drag_origin_index)
            label = self._labels.pop(self._drag_origin_index)
            if self._drag_side_target_index >= 0:
                target_index = self._drag_side_target_index
                if target_index > self._drag_origin_index:
                    target_index -= 1
                target_index = min(max(0, target_index + 1), len(self._blocks))
                if 0 <= target_index - 1 < len(self._blocks):
                    anchor = self._blocks[target_index - 1]
                    if isinstance(anchor, dict):
                        anchor_ratio = self._block_width_ratio(anchor)
                        block["ui_width_ratio"] = anchor_ratio
                        anchor_align = self._block_align(anchor)
                        block["ui_align"] = "right" if anchor_align != "right" else "left"
            else:
                target_index = self._drag_insert_index
                if target_index > self._drag_origin_index:
                    target_index -= 1
                target_index = min(max(0, target_index), len(self._blocks))
            self._blocks.insert(target_index, block)
            self._labels.insert(target_index, label)
            if self._drag_side_target_index >= 0:
                anchor_idx = target_index - 1
                if 0 <= anchor_idx < len(self._blocks):
                    self._pair_blocks_in_row(anchor_idx, target_index)
            self._normalize_row_groups()
            self._selected_index = target_index
            if target_index != self._drag_origin_index:
                self.blocks_reordered.emit([dict(item) for item in self._blocks])
        self._drag_origin_index = -1
        self._drag_insert_index = -1
        self._drag_gate_target_index = -1
        self._drag_side_target_index = -1
        self._drag_active = False
        self._nested_drag_active = False
        self._nested_drag_gate_index = -1
        self._nested_drag_condition_index = -1
        self._nested_drag_insert_index = -1
        self._nested_drag_reorder_target = -1
        self._drag_nested_gate_target = None
        self._drag_hover_block_index = -1
        self._ensure_dash_animation(False)
        self._update_hover_cursor(drop_point)
        self.update()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._connection_drag_active:
            self.unsetCursor()
        self._drag_hover_block_index = -1
        self._drag_nested_gate_target = None
        self._drag_side_target_index = -1
        self._connection_hover = None
        self._ensure_dash_animation(False)
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(8, 15, 30, 180))

        if not self._blocks:
            self._hit_areas = []
            self._delete_hit_areas = []
            self._move_hit_areas = []
            painter.setPen(QColor(120, 148, 174))
            painter.drawText(self.rect().adjusted(16, 12, -16, -12), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, tr("No blocks in flow."))
            return

        margin_x = 16.0
        top = 14.0
        gap = 40.0
        width = max(120.0, self.width() - margin_x * 2.0)
        self._hit_areas = []
        self._delete_hit_areas = []
        self._move_hit_areas = []
        self._gate_condition_delete_hit_areas = []
        self._gate_condition_row_hit_areas = []
        self._gate_condition_drag_hit_areas = []
        self._input_socket_hit_areas = []
        self._input_label_hit_areas = []
        self._true_socket_hit_areas = []
        self._true_label_hit_areas = []
        self._false_socket_hit_areas = []
        self._false_label_hit_areas = []
        self._next_socket_hit_areas = []
        self._next_label_hit_areas = []
        self._resize_left_hit_areas = []
        self._resize_right_hit_areas = []
        self._connection_hit_areas = []
        self._connection_delete_hit_areas = []
        self._connection_shelf_hit_areas = []
        current_top = top

        connectivity = self._connectivity_state()
        hover_point = self.mapFromGlobal(QCursor.pos())
        hover_pointf = QPointF(float(hover_point.x()), float(hover_point.y()))
        for idx, block in enumerate(self._blocks):
            block_height = float(self._block_height(block))
            block_type = str(block.get("type", "")).strip().lower()
            immutable_controls = block_type in {"start", "end"}
            block_ratio = self._block_width_ratio(block)
            block_width = width * block_ratio
            block_align = self._block_align(block)
            if (
                idx > 0
                and self._blocks_share_row(self._blocks[idx - 1], block)
                and abs(self._hit_areas[idx - 1].top() - current_top) > 0.1
            ):
                current_top = self._hit_areas[idx - 1].top()
            if block_ratio >= 0.999:
                block_left = margin_x
            elif block_align == "right":
                block_left = margin_x + (width - block_width)
            elif block_align == "center":
                block_left = margin_x + ((width - block_width) / 2.0)
            else:
                block_left = margin_x
            rect = QRectF(block_left, current_top, block_width, block_height)
            self._hit_areas.append(rect)
            move_rect = QRectF()
            delete_rect = QRectF()
            if not immutable_controls:
                move_rect = QRectF(rect.right() - 54.0, rect.top() + 6.0, 20.0, 20.0)
                delete_rect = QRectF(rect.right() - 28.0, rect.top() + 6.0, 20.0, 20.0)
            self._move_hit_areas.append(move_rect)
            self._delete_hit_areas.append(delete_rect)
            left_resize_rect = QRectF()
            right_resize_rect = QRectF()
            left_resize_rect = QRectF(rect.left() - 4.0, rect.center().y() - 12.0, 8.0, 24.0)
            right_resize_rect = QRectF(rect.right() - 4.0, rect.center().y() - 12.0, 8.0, 24.0)
            self._resize_left_hit_areas.append((idx, left_resize_rect))
            self._resize_right_hit_areas.append((idx, right_resize_rect))

            gate_nested_selected = (
                self._selected_gate_condition is not None
                and self._selected_gate_condition[0] == idx
            )
            is_dragged_block = self._drag_active and idx == self._drag_origin_index
            runtime_active = idx == self._runtime_active_index
            selected = idx == self._selected_index and not gate_nested_selected
            invalid_connection = connectivity.get(idx, False)
            border = QColor(34, 211, 238) if selected else QColor(45, 71, 103)
            background = QColor(12, 28, 52, 220 if selected else 180)
            if runtime_active:
                border = QColor(45, 212, 191)
                background = QColor(10, 58, 74, 230 if selected else 212)
            if invalid_connection:
                border = QColor(248, 113, 113)
                background = QColor(54, 19, 28, 205 if selected else 175)
            if is_dragged_block:
                border = QColor(103, 232, 249)
                # Dim the block while dragging so the transfer source is obvious.
                background = QColor(10, 20, 36, 140)
            if self._drag_active and idx == self._drag_gate_target_index:
                border = QColor(45, 212, 191)
                background = QColor(10, 42, 60, 230)
            painter.setPen(QPen(border, 1.6))
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 12.0, 12.0)
            if idx == self._drag_hover_block_index and (self._drag_active or self._nested_drag_active or self._dash_timer.isActive()):
                dash_pen = QPen(QColor(34, 211, 238), 1.8)
                dash_pen.setStyle(Qt.PenStyle.CustomDashLine)
                dash_pen.setDashPattern([5.0, 3.5])
                dash_pen.setDashOffset(self._dash_phase)
                painter.setPen(dash_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), 12.0, 12.0)
            if idx == self._drag_side_target_index and self._drag_active:
                side_rect = self._paired_slot_rect_for_index(idx)
                if side_rect.isValid():
                    dash_pen = QPen(QColor(34, 211, 238), 1.8)
                    dash_pen.setStyle(Qt.PenStyle.CustomDashLine)
                    dash_pen.setDashPattern([5.0, 3.5])
                    dash_pen.setDashOffset(self._dash_phase)
                    painter.setPen(dash_pen)
                    painter.setBrush(QColor(9, 22, 40, 110))
                    painter.drawRoundedRect(side_rect.adjusted(1.5, 1.5, -1.5, -1.5), 12.0, 12.0)
            if self._align_drag_active and idx == self._align_drag_index and block_ratio < 0.999:
                preview_align = self._block_align(block)
                if preview_align == "right":
                    marker_x = rect.right() - 6.0
                elif preview_align == "center":
                    marker_x = rect.center().x()
                else:
                    marker_x = rect.left() + 6.0
                painter.setPen(QPen(QColor(34, 211, 238), 2.0))
                painter.drawLine(QPointF(marker_x, rect.top() + 6.0), QPointF(marker_x, rect.bottom() - 6.0))

            left_hover = left_resize_rect.contains(hover_pointf)
            right_hover = right_resize_rect.contains(hover_pointf)
            if left_hover or right_hover:
                glow_pen = QPen(QColor(34, 211, 238), 1.8)
            else:
                glow_pen = QPen(QColor(56, 92, 130), 1.2)
            painter.setPen(glow_pen)
            painter.setBrush(QColor(10, 28, 50, 240))
            painter.drawRoundedRect(left_resize_rect, 3.0, 3.0)
            painter.drawRoundedRect(right_resize_rect, 3.0, 3.0)

            if not immutable_controls:
                painter.setPen(QPen(QColor(48, 83, 118), 1.2))
                painter.setBrush(QColor(9, 24, 45, 180 if is_dragged_block else 230))
                painter.drawRoundedRect(move_rect, 6.0, 6.0)
                painter.setPen(QColor(188, 202, 220) if is_dragged_block else QColor(227, 240, 252))
                painter.drawText(move_rect, Qt.AlignmentFlag.AlignCenter, "≡")

                painter.setPen(QPen(QColor(48, 83, 118), 1.2))
                painter.setBrush(QColor(9, 24, 45, 180 if is_dragged_block else 230))
                painter.drawRoundedRect(delete_rect, 6.0, 6.0)
                painter.setPen(QColor(188, 202, 220) if is_dragged_block else QColor(227, 240, 252))
                painter.drawText(delete_rect, Qt.AlignmentFlag.AlignCenter, "✕")

            if block_type != "start":
                input_centers = self._socket_centers(rect, block, "input")
                if input_centers:
                    for socket_idx, center in enumerate(input_centers):
                        input_rect = QRectF(center.x() - 6.0, center.y() - 6.0, 12.0, 12.0)
                        self._input_socket_hit_areas.append((idx, socket_idx, input_rect))
                        self._draw_input_marker(painter, center)
                        if (
                            self._connection_hover_target_index == idx
                            and self._connection_hover_target_socket_index == socket_idx
                        ):
                            glow_pen = QPen(QColor(34, 211, 238), 1.6)
                            painter.setPen(glow_pen)
                            painter.setBrush(Qt.BrushStyle.NoBrush)
                            painter.drawEllipse(input_rect.adjusted(-2.0, -2.0, 2.0, 2.0))

            text_left = rect.left() + 12.0
            text_right = right_resize_rect.left() - 8.0
            if not immutable_controls and move_rect.isValid():
                text_right = min(text_right, move_rect.left() - 8.0)
            text_width = max(40.0, text_right - text_left)

            if str(block.get("type", "")).strip().lower() == "gate":
                title_rect = QRectF(text_left, rect.top() + 20.0, text_width, 18.0)
                mode_rect = QRectF(text_left, rect.top() + 40.0, text_width, 16.0)
                painter.setPen(QColor(205, 220, 236) if is_dragged_block else QColor(236, 246, 255))
                title_text = QFontMetrics(painter.font()).elidedText(
                    self._title_for_block(block),
                    Qt.TextElideMode.ElideRight,
                    max(1, int(title_rect.width())),
                )
                painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title_text)
                painter.setPen(QColor(126, 146, 168) if is_dragged_block else QColor(152, 176, 201))
                subtitle = self._labels[idx] if idx < len(self._labels) else ""
                subtitle_text = QFontMetrics(painter.font()).elidedText(
                    subtitle,
                    Qt.TextElideMode.ElideRight,
                    max(1, int(mode_rect.width())),
                )
                painter.drawText(mode_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle_text)
                self._draw_gate_children(
                    painter=painter,
                    root_gate_index=idx,
                    gate_block=block,
                    row_top=rect.top() + 64.0,
                    outer_rect=rect,
                    level=0,
                    flat_counter=[0],
                )
            else:
                title_rect = QRectF(text_left, rect.top() + 20.0, text_width, 18.0)
                subtitle_rect = QRectF(text_left, rect.top() + 42.0, text_width, 16.0)
                painter.setPen(QColor(205, 220, 236) if is_dragged_block else QColor(236, 246, 255))
                title_text = QFontMetrics(painter.font()).elidedText(
                    self._title_for_block(block),
                    Qt.TextElideMode.ElideRight,
                    max(1, int(title_rect.width())),
                )
                painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title_text)
                painter.setPen(QColor(126, 146, 168) if is_dragged_block else QColor(152, 176, 201))
                subtitle = self._labels[idx] if idx < len(self._labels) else ""
                subtitle_text = QFontMetrics(painter.font()).elidedText(
                    subtitle,
                    Qt.TextElideMode.ElideRight,
                    max(1, int(subtitle_rect.width())),
                )
                painter.drawText(subtitle_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle_text)

            if block_type in {"condition", "gate"}:
                true_center = self._socket_center(rect, block, "true")
                false_center = self._socket_center(rect, block, "false")
                true_rect = QRectF(true_center.x() - 6.0, true_center.y() - 6.0, 12.0, 12.0)
                false_rect = QRectF(false_center.x() - 6.0, false_center.y() - 6.0, 12.0, 12.0)
                true_label_rect = QRectF(true_rect.left() - 52.0, true_rect.top() - 1.0, 46.0, 14.0)
                false_label_rect = QRectF(false_rect.left() - 54.0, false_rect.top() - 1.0, 48.0, 14.0)
                self._true_socket_hit_areas.append((idx, true_rect))
                self._true_label_hit_areas.append((idx, true_label_rect))
                self._false_socket_hit_areas.append((idx, false_rect))
                self._false_label_hit_areas.append((idx, false_label_rect))
                painter.setPen(QPen(QColor(34, 211, 238), 1.2))
                painter.setBrush(QColor(8, 84, 120, 235))
                painter.drawEllipse(true_rect)
                painter.setPen(QPen(QColor(245, 158, 11), 1.2))
                painter.setBrush(QColor(82, 52, 8, 235))
                painter.drawEllipse(false_rect)
                label_pen_true = QColor(110, 220, 255)
                label_pen_false = QColor(245, 185, 96)
                painter.setPen(label_pen_true)
                painter.drawText(true_label_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tr("True"))
                painter.setPen(label_pen_false)
                painter.drawText(false_label_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tr("False"))
            elif block_type != "end":
                next_centers = self._socket_centers(rect, block, "next")
                if next_centers:
                    for socket_idx, center in enumerate(next_centers):
                        next_rect = QRectF(center.x() - 6.0, center.y() - 6.0, 12.0, 12.0)
                        self._next_socket_hit_areas.append((idx, socket_idx, next_rect))
                        self._draw_output_marker(painter, center)
            current_top = rect.bottom() + gap

        self._draw_top_level_connections(painter)
        if self._connection_drag_active and self._connection_source_index >= 0:
            start_socket_index = self._connection_source_socket_index if self._connection_branch == "next" else 0
            start = self._branch_socket_center(
                self._connection_source_index,
                self._connection_branch,
                socket_index=start_socket_index,
            )
            if start is not None:
                preview_color = QColor(34, 211, 238) if self._connection_branch == "true" else QColor(245, 158, 11) if self._connection_branch == "false" else QColor(96, 165, 250)
                preview_pen = QPen(preview_color, 1.8)
                preview_pen.setStyle(Qt.PenStyle.CustomDashLine)
                preview_pen.setDashPattern([5.0, 3.0])
                preview_pen.setDashOffset(self._dash_phase)
                painter.setPen(preview_pen)
                painter.drawLine(start, self._connection_cursor_point)
                self._ensure_dash_animation(True)

        if self._drag_active and self._drag_origin_index >= 0 and self._drag_insert_index >= 0:
            line_y = self._insertion_line_y(self._drag_insert_index)
            painter.setPen(QPen(QColor(34, 211, 238), 3.0))
            painter.drawLine(QPointF(margin_x + 8.0, line_y), QPointF(margin_x + width - 8.0, line_y))
        if self._nested_drag_active and self._nested_drag_insert_index >= 0:
            line_y = self._insertion_line_y(self._nested_drag_insert_index)
            painter.setPen(QPen(QColor(45, 212, 191), 3.0))
            painter.drawLine(QPointF(margin_x + 8.0, line_y), QPointF(margin_x + width - 8.0, line_y))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, max(220, int(self._content_h)))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 220)

    def _content_height(self) -> int:
        if not self._blocks:
            return 220
        gap = 40
        top = 14
        bottom = 16
        heights = [self._block_height(block) for block in self._blocks]
        return top + sum(heights) + (max(0, len(heights) - 1) * gap) + bottom

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _canvas_inner_width(self) -> float:
        margin_x = 16.0
        return max(120.0, self.width() - margin_x * 2.0)

    def _block_width_ratio(self, block: dict[str, object]) -> float:
        raw = float(block.get("ui_width_ratio", 1.0) or 1.0)
        return self._clamp(raw, 0.35, 1.0)

    @staticmethod
    def _block_align(block: dict[str, object]) -> str:
        align = str(block.get("ui_align", "left")).strip().lower()
        if align == "right":
            return "right"
        if align == "center":
            return "center"
        return "left"

    @staticmethod
    def _row_id(block: dict[str, object]) -> str:
        return str(block.get("ui_row_id", "")).strip()

    @staticmethod
    def _clear_row_id(block: dict[str, object]) -> None:
        block.pop("ui_row_id", None)

    def _normalize_row_groups(self) -> None:
        grouped: dict[str, list[int]] = {}
        for idx, block in enumerate(self._blocks):
            if not isinstance(block, dict):
                continue
            row_id = self._row_id(block)
            if row_id:
                grouped.setdefault(row_id, []).append(idx)
        for row_id, indices in grouped.items():
            if len(indices) != 2:
                for idx in indices:
                    if 0 <= idx < len(self._blocks) and isinstance(self._blocks[idx], dict):
                        self._clear_row_id(self._blocks[idx])
                continue
            left_idx, right_idx = sorted(indices)
            left_block = self._blocks[left_idx]
            right_block = self._blocks[right_idx]
            if not isinstance(left_block, dict) or not isinstance(right_block, dict):
                continue
            valid = (
                right_idx == left_idx + 1
                and self._block_width_ratio(left_block) < 0.999
                and self._block_width_ratio(right_block) < 0.999
                and (self._block_width_ratio(left_block) + self._block_width_ratio(right_block)) <= 1.001
                and self._block_align(left_block) in {"left", "right"}
                and self._block_align(right_block) in {"left", "right"}
                and self._block_align(left_block) != self._block_align(right_block)
            )
            if not valid:
                self._clear_row_id(left_block)
                self._clear_row_id(right_block)

    def _pair_blocks_in_row(self, first_index: int, second_index: int) -> None:
        if first_index == second_index:
            return
        if not (0 <= first_index < len(self._blocks) and 0 <= second_index < len(self._blocks)):
            return
        first = self._blocks[first_index]
        second = self._blocks[second_index]
        if not isinstance(first, dict) or not isinstance(second, dict):
            return
        row_id = uuid.uuid4().hex
        first["ui_row_id"] = row_id
        second["ui_row_id"] = row_id
        self._normalize_row_groups()

    def _blocks_share_row(self, left: dict[str, object], right: dict[str, object]) -> bool:
        left_row_id = self._row_id(left)
        right_row_id = self._row_id(right)
        if not left_row_id or not right_row_id or left_row_id != right_row_id:
            return False
        left_ratio = self._block_width_ratio(left)
        right_ratio = self._block_width_ratio(right)
        if left_ratio >= 0.999 or right_ratio >= 0.999:
            return False
        return self._block_align(left) != self._block_align(right)

    def _row_partner_index(self, index: int) -> int:
        if index < 0 or index >= len(self._blocks):
            return -1
        own = self._blocks[index]
        if not isinstance(own, dict):
            return -1
        if index + 1 < len(self._blocks):
            other = self._blocks[index + 1]
            if isinstance(other, dict) and self._blocks_share_row(own, other):
                return index + 1
        if index - 1 >= 0:
            other = self._blocks[index - 1]
            if isinstance(other, dict) and self._blocks_share_row(other, own):
                return index - 1
        return -1

    def _paired_slot_rect_for_index(self, index: int) -> QRectF:
        if index < 0 or index >= len(self._hit_areas):
            return QRectF()
        if self._row_partner_index(index) >= 0:
            return QRectF()
        rect = self._hit_areas[index]
        block = self._blocks[index] if 0 <= index < len(self._blocks) else {}
        if not isinstance(block, dict):
            return QRectF()
        ratio = self._block_width_ratio(block)
        if ratio >= 0.999:
            return QRectF()
        if self._block_align(block) == "center":
            return QRectF()
        canvas_width = self._canvas_inner_width()
        slot_width = canvas_width * ratio
        margin_x = 16.0
        slot_align = "right" if self._block_align(block) == "left" else "left"
        slot_left = margin_x if slot_align == "left" else margin_x + (canvas_width - slot_width)
        return QRectF(slot_left, rect.top(), slot_width, rect.height())

    def _side_slot_target_for_point(self, point: QPointF, source_index: int) -> int:
        if source_index < 0:
            return -1
        source_block = self._blocks[source_index] if 0 <= source_index < len(self._blocks) else {}
        if not isinstance(source_block, dict):
            return -1
        if self._block_width_ratio(source_block) >= 0.999:
            return -1
        for idx, _rect in enumerate(self._hit_areas):
            if idx == source_index:
                continue
            block = self._blocks[idx] if 0 <= idx < len(self._blocks) else {}
            if not isinstance(block, dict):
                continue
            if self._block_width_ratio(block) >= 0.999:
                continue
            if self._row_partner_index(idx) >= 0:
                continue
            slot_rect = self._paired_slot_rect_for_index(idx)
            if slot_rect.isValid() and slot_rect.contains(point):
                return idx
        return -1

    def _set_block_layout(self, index: int, *, width_ratio: float | None = None, align: str | None = None) -> bool:
        if index < 0 or index >= len(self._blocks):
            return False
        block = self._blocks[index]
        if not isinstance(block, dict):
            return False
        changed = False
        if width_ratio is not None:
            normalized = self._clamp(float(width_ratio), 0.35, 1.0)
            if abs(float(block.get("ui_width_ratio", 1.0) or 1.0) - normalized) > 1e-4:
                block["ui_width_ratio"] = normalized
                changed = True
        if align is not None:
            requested_align = str(align).strip().lower()
            if requested_align == "right":
                normalized_align = "right"
            elif requested_align == "center":
                normalized_align = "center"
            else:
                normalized_align = "left"
            if str(block.get("ui_align", "left")).strip().lower() != normalized_align:
                block["ui_align"] = normalized_align
                self._clear_row_id(block)
                changed = True
        if self._block_width_ratio(block) >= 0.999:
            if self._row_id(block):
                self._clear_row_id(block)
                changed = True
        return changed

    def _socket_ratio(self, block: dict[str, object], kind: str) -> float:
        key_map = {
            "input": "ui_input_x",
            "next": "ui_output_x",
            "true": "ui_true_output_x",
            "false": "ui_false_output_x",
        }
        default_map = {
            "input": 0.5,
            "next": 0.5,
            "true": 0.25,
            "false": 0.75,
        }
        key = key_map.get(kind, "ui_output_x")
        default = default_map.get(kind, 0.5)
        raw = float(block.get(key, default) or default)
        return self._clamp(raw, 0.08, 0.92)

    def _set_socket_ratio(self, index: int, kind: str, ratio: float, socket_index: int = 0) -> bool:
        if index < 0 or index >= len(self._blocks):
            return False
        block = self._blocks[index]
        if not isinstance(block, dict):
            return False
        key_map = {
            "input": "ui_input_x",
            "next": "ui_output_x",
            "true": "ui_true_output_x",
            "false": "ui_false_output_x",
        }
        key = key_map.get(kind)
        if not key:
            return False
        normalized = self._clamp(float(ratio), 0.08, 0.92)
        ratios = self._socket_ratios(block, kind)
        if not ratios:
            return False
        safe_index = max(0, min(int(socket_index), len(ratios) - 1))
        current = ratios[safe_index]
        if abs(current - normalized) <= 1e-4:
            return False
        ratios[safe_index] = normalized
        ratios = [self._clamp(value, 0.08, 0.92) for value in ratios]
        array_key = f"{key}s"
        block[array_key] = ratios
        if kind in {"input", "next", "true", "false"}:
            block[key] = ratios[min(safe_index, len(ratios) - 1)]
        return True

    def _socket_center(self, rect: QRectF, block: dict[str, object], kind: str) -> QPointF:
        x = rect.left() + (rect.width() * self._socket_ratio(block, kind))
        y = rect.top() + 6.0 if kind == "input" else rect.bottom() - 10.0
        return QPointF(x, y)

    def _insertion_line_y(self, insert_index: int) -> float:
        if not self._hit_areas:
            return 0.0
        if insert_index <= 0:
            return self._hit_areas[0].top() - 8.0
        if insert_index >= len(self._hit_areas):
            return self._hit_areas[-1].bottom() + 8.0
        return (self._hit_areas[insert_index - 1].bottom() + self._hit_areas[insert_index].top()) / 2.0

    def _top_level_insert_index_for_point(self, y: float) -> int:
        if not self._hit_areas:
            return 0
        for idx, rect in enumerate(self._hit_areas):
            if y < rect.center().y():
                return idx
        return len(self._hit_areas)

    def _gate_drop_target_at(self, point: QPointF, source_index: int) -> int:
        source_block = self._blocks[source_index] if 0 <= source_index < len(self._blocks) else {}
        if not isinstance(source_block, dict):
            return -1
        source_type = str(source_block.get("type", "")).strip().lower()
        if source_type not in {"condition", "gate"}:
            return -1

        for idx, rect in enumerate(self._hit_areas):
            if idx == source_index:
                continue
            expanded_rect = rect.adjusted(-10.0, -8.0, 10.0, 8.0)
            if not expanded_rect.contains(point):
                continue
            if idx >= len(self._blocks):
                continue
            block = self._blocks[idx]
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")).strip().lower() == "gate":
                return idx
        return -1

    def _gate_drop_target_for_palette(self, point: QPointF) -> int:
        for idx, rect in enumerate(self._hit_areas):
            expanded_rect = rect.adjusted(-10.0, -8.0, 10.0, 8.0)
            if not expanded_rect.contains(point):
                continue
            if idx >= len(self._blocks):
                continue
            block = self._blocks[idx]
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")).strip().lower() == "gate":
                return idx
        return -1

    def _nested_gate_drop_target_for_palette(self, point: QPointF) -> tuple[int, int] | None:
        return self._nested_gate_drop_target_for_point(point)

    def _nested_gate_drop_target_for_point(self, point: QPointF) -> tuple[int, int] | None:
        for gate_index, flat_index, rect in self._gate_condition_row_hit_areas:
            if not rect.contains(point):
                continue
            if gate_index < 0 or gate_index >= len(self._blocks):
                continue
            gate_block = self._blocks[gate_index]
            if not isinstance(gate_block, dict):
                continue
            child_path = self._gate_child_path_from_flat_index(gate_block, flat_index)
            if child_path is None:
                continue
            child = self._gate_child_at_path(gate_block, child_path)
            if not isinstance(child, dict):
                continue
            if str(child.get("type", "")).strip().lower() == "gate":
                return (gate_index, flat_index)
        return None

    def _block_height(self, block: dict[str, object]) -> int:
        block_type = str(block.get("type", "")).strip().lower()
        if block_type != "gate":
            return 84
        visible_rows = self._gate_visible_rows(block)
        if visible_rows <= 0:
            return 84
        return 84 + (visible_rows * 30) + 12

    @staticmethod
    def _gate_children(block: dict[str, object]) -> list[dict[str, object]]:
        raw = block.get("conditions", [])
        if not isinstance(raw, list):
            return []
        result: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")).strip().lower() not in {"condition", "gate"}:
                continue
            result.append(item)
        return result

    @classmethod
    def _gate_visible_rows(cls, block: dict[str, object]) -> int:
        rows = 0
        for child in cls._gate_children(block):
            rows += 1
            if str(child.get("type", "")).strip().lower() == "gate":
                rows += cls._gate_visible_rows(child)
        return rows

    def _nested_gate_row_drop_target(self, gate_index: int, point: QPointF, source_child_index: int) -> int:
        if gate_index < 0 or gate_index >= len(self._blocks):
            return -1
        gate_block = self._blocks[gate_index]
        if not isinstance(gate_block, dict):
            return -1
        source_path = self._gate_child_path_from_flat_index(gate_block, source_child_index)
        if source_path is None:
            return -1
        source_child = self._gate_child_at_path(gate_block, source_path)
        source_is_gate = isinstance(source_child, dict) and str(source_child.get("type", "")).strip().lower() == "gate"
        for hit_gate_index, flat_index, rect in self._gate_condition_row_hit_areas:
            if hit_gate_index != gate_index or flat_index == source_child_index:
                continue
            if not rect.contains(point):
                continue
            target_path = self._gate_child_path_from_flat_index(gate_block, flat_index)
            if target_path is None:
                continue
            if source_is_gate and len(target_path) >= len(source_path) and target_path[: len(source_path)] == source_path:
                continue
            child = self._gate_child_at_path(gate_block, target_path)
            if not isinstance(child, dict):
                continue
            if str(child.get("type", "")).strip().lower() == "gate":
                return flat_index
        return -1

    def _gate_child_parent_path(self, gate_index: int, child_flat_index: int) -> tuple[int, ...] | None:
        if gate_index < 0 or gate_index >= len(self._blocks):
            return None
        gate_block = self._blocks[gate_index]
        if not isinstance(gate_block, dict):
            return None
        path = self._gate_child_path_from_flat_index(gate_block, child_flat_index)
        if path is None:
            return None
        return path[:-1]

    def _nested_gate_row_reorder_target(self, gate_index: int, point: QPointF, source_child_index: int) -> int:
        best_index = -1
        best_distance = 99999.0
        for hit_gate_index, flat_index, rect in self._gate_condition_row_hit_areas:
            if hit_gate_index != gate_index or flat_index == source_child_index:
                continue
            expanded = rect.adjusted(0.0, -10.0, 0.0, 10.0)
            if expanded.contains(point):
                distance = abs(point.y() - rect.center().y())
                if distance < best_distance:
                    best_distance = distance
                    best_index = flat_index
        return best_index

    def _draw_gate_children(
        self,
        painter: QPainter,
        root_gate_index: int,
        gate_block: dict[str, object],
        row_top: float,
        outer_rect: QRectF,
        level: int,
        flat_counter: list[int],
    ) -> float:
        children = self._gate_children(gate_block)
        left_offset = 12.0 + (level * 18.0)
        right_padding = 24.0 + (level * 8.0)
        row_width = max(120.0, outer_rect.width() - left_offset - right_padding)
        for condition in children:
            condition_index = flat_counter[0]
            flat_counter[0] += 1
            cond_rect = QRectF(outer_rect.left() + left_offset, row_top, row_width, 24.0)
            condition_selected = self._selected_gate_condition == (root_gate_index, condition_index)
            is_dragged_nested = (
                self._nested_drag_active
                and self._nested_drag_gate_index == root_gate_index
                and self._nested_drag_condition_index == condition_index
            )
            nested_target = self._drag_nested_gate_target == (root_gate_index, condition_index)
            reorder_target = (
                self._nested_drag_active
                and self._nested_drag_gate_index == root_gate_index
                and self._nested_drag_reorder_target == condition_index
            )
            cond_border = QColor(34, 211, 238) if (condition_selected or nested_target or reorder_target) else QColor(48, 83, 118)
            cond_bg = QColor(10, 84, 122, 236) if (condition_selected or nested_target or reorder_target) else QColor(8, 60, 92, 220)
            if is_dragged_nested:
                cond_bg = QColor(7, 40, 62, 150)
            painter.setPen(QPen(cond_border, 1.2 if condition_selected else 1.0))
            painter.setBrush(cond_bg)
            painter.drawRoundedRect(cond_rect, 9.0, 9.0)
            if nested_target and (self._drag_active or self._nested_drag_active or self._dash_timer.isActive()):
                dash_pen = QPen(QColor(34, 211, 238), 1.6)
                dash_pen.setStyle(Qt.PenStyle.CustomDashLine)
                dash_pen.setDashPattern([4.5, 3.0])
                dash_pen.setDashOffset(self._dash_phase)
                painter.setPen(dash_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(cond_rect.adjusted(1.0, 1.0, -1.0, -1.0), 9.0, 9.0)
            if reorder_target and self._nested_drag_active:
                line_pen = QPen(QColor(34, 211, 238), 2.4)
                painter.setPen(line_pen)
                painter.drawLine(
                    QPointF(cond_rect.left() + 8.0, cond_rect.top() - 2.0),
                    QPointF(cond_rect.right() - 8.0, cond_rect.top() - 2.0),
                )

            drag_rect_nested = QRectF(cond_rect.right() - 48.0, cond_rect.top() + 3.0, 18.0, 18.0)
            delete_rect_nested = QRectF(cond_rect.right() - 26.0, cond_rect.top() + 3.0, 18.0, 18.0)
            self._gate_condition_row_hit_areas.append((root_gate_index, condition_index, cond_rect))
            self._gate_condition_drag_hit_areas.append((root_gate_index, condition_index, drag_rect_nested))
            self._gate_condition_delete_hit_areas.append((root_gate_index, condition_index, delete_rect_nested))
            action_border = QColor(34, 211, 238) if condition_selected else QColor(48, 83, 118)
            action_bg = QColor(8, 38, 66, 238) if condition_selected else QColor(9, 24, 45, 230)
            if is_dragged_nested:
                action_bg = QColor(9, 24, 45, 170)
            painter.setPen(QPen(action_border, 1.0))
            painter.setBrush(action_bg)
            painter.drawRoundedRect(drag_rect_nested, 5.0, 5.0)
            painter.drawRoundedRect(delete_rect_nested, 5.0, 5.0)
            painter.setPen(QColor(188, 202, 220) if is_dragged_nested else QColor(227, 240, 252))
            painter.drawText(drag_rect_nested, Qt.AlignmentFlag.AlignCenter, "≡")
            painter.drawText(delete_rect_nested, Qt.AlignmentFlag.AlignCenter, "✕")

            painter.setPen(QColor(188, 202, 220) if is_dragged_nested else QColor(227, 240, 252))
            painter.drawText(
                cond_rect.adjusted(10.0, 0.0, -52.0, 0.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._gate_child_text(condition),
            )
            row_top += 30.0
            if str(condition.get("type", "")).strip().lower() == "gate":
                row_top = self._draw_gate_children(
                    painter=painter,
                    root_gate_index=root_gate_index,
                    gate_block=condition,
                    row_top=row_top,
                    outer_rect=outer_rect,
                    level=level + 1,
                    flat_counter=flat_counter,
                )
        return row_top

    @classmethod
    def _gate_child_path_from_flat_index(cls, gate_block: dict[str, object], flat_index: int) -> tuple[int, ...] | None:
        paths = cls._gate_child_paths(gate_block)
        if flat_index < 0 or flat_index >= len(paths):
            return None
        return paths[flat_index]

    @classmethod
    def _gate_child_paths(cls, gate_block: dict[str, object], prefix: tuple[int, ...] = ()) -> list[tuple[int, ...]]:
        paths: list[tuple[int, ...]] = []
        children = cls._gate_children(gate_block)
        for idx, child in enumerate(children):
            path = prefix + (idx,)
            paths.append(path)
            if str(child.get("type", "")).strip().lower() == "gate":
                paths.extend(cls._gate_child_paths(child, path))
        return paths

    @classmethod
    def _gate_child_at_path(cls, gate_block: dict[str, object], path: tuple[int, ...]) -> dict[str, object] | None:
        current = dict(gate_block)
        for depth, idx in enumerate(path):
            children = cls._gate_children(current)
            if idx < 0 or idx >= len(children):
                return None
            child = dict(children[idx])
            if depth == len(path) - 1:
                return child
            if str(child.get("type", "")).strip().lower() != "gate":
                return None
            current = child
        return None

    @staticmethod
    def _gate_child_text(block: dict[str, object]) -> str:
        block_type = str(block.get("type", "")).strip().lower()
        if block_type == "gate":
            mode = str(block.get("mode", "and")).strip().lower() or "and"
            mode_label = tr("All conditions") if mode == "and" else tr("Any condition")
            child_count = 0
            nested = block.get("conditions", [])
            if isinstance(nested, list):
                child_count = len(
                    [
                        item
                        for item in nested
                        if isinstance(item, dict) and str(item.get("type", "")).strip().lower() in {"condition", "gate"}
                    ]
                )
            return tr("Gate: {mode} ({count})").format(mode=mode_label, count=child_count)
        metric = str(block.get("metric_key", "")).strip() or "metric"
        operator = str(block.get("operator", ">=")).strip() or ">="
        value = float(block.get("value", 0.0) or 0.0)
        return tr("Condition: {metric} {operator} {value}").format(metric=metric, operator=operator, value=f"{value:g}")

    def content_height(self) -> int:
        return self._content_h

    def _update_hover_cursor(self, point: QPointF) -> None:
        if self._connection_drag_active:
            self.setCursor(Qt.CursorShape.CrossCursor)
            return
        if self._connection_shelf_drag_active:
            self.setCursor(Qt.CursorShape.SizeVerCursor if self._connection_shelf_drag_axis == "y" else Qt.CursorShape.SizeHorCursor)
            return
        for _, _, _, rect in self._connection_delete_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        for _, _, _, axis, rect in self._connection_shelf_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.SizeVerCursor if axis == "y" else Qt.CursorShape.SizeHorCursor)
                return
        if self._connection_hover is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            return
        if self._resize_drag_active or self._socket_drag_active:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
        if self._align_drag_active:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            return
        if self._drag_active or self._nested_drag_active:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        for _, rect in self._resize_left_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        for _, rect in self._resize_right_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
        for _, rect in self._true_socket_hit_areas:
            if rect.contains(point):
                if self._has_output_connection(_, "true", 0):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)
                return
        for _, rect in self._false_socket_hit_areas:
            if rect.contains(point):
                if self._has_output_connection(_, "false", 0):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)
                return
        for idx, socket_index, rect in self._next_socket_hit_areas:
            if rect.contains(point):
                if self._has_output_connection(idx, "next", socket_index):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)
                return
        for idx, socket_index, rect in self._input_socket_hit_areas:
            if rect.contains(point):
                if self._has_input_connection(idx, socket_index):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self.setCursor(Qt.CursorShape.CrossCursor)
                return
        for _, _, rect in self._gate_condition_drag_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.OpenHandCursor)
                return
        for _, _, rect in self._gate_condition_delete_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        for rect in self._delete_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        for rect in self._move_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.OpenHandCursor)
                return
        for idx, rect in enumerate(self._hit_areas):
            if rect.contains(point):
                block = self._blocks[idx] if 0 <= idx < len(self._blocks) else {}
                if isinstance(block, dict) and self._block_width_ratio(block) < 0.999:
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    return
                break
        self.unsetCursor()

    @staticmethod
    def _title_for_block(block: dict[str, object]) -> str:
        block_type = str(block.get("type", "")).strip().lower()
        if block_type == "start":
            return tr("Start")
        if block_type == "end":
            return tr("End")
        if block_type == "trigger":
            return tr("Trigger")
        if block_type == "condition":
            return tr("Condition")
        if block_type == "gate":
            return tr("Gate")
        if block_type == "delay":
            return tr("Delay")
        if block_type == "action":
            return tr("Action")
        return tr("Block")

    def _input_target_for_point(self, point: QPointF, source_index: int) -> tuple[int, int]:
        best_index = -1
        best_socket_index = -1
        best_distance = 999999.0
        for idx, socket_index, rect in self._input_socket_hit_areas:
            if idx == source_index:
                continue
            if not rect.adjusted(-5.0, -5.0, 5.0, 5.0).contains(point):
                continue
            center = rect.center()
            distance = ((center.x() - point.x()) ** 2 + (center.y() - point.y()) ** 2) ** 0.5
            if distance < best_distance:
                best_distance = distance
                best_index = idx
                best_socket_index = socket_index
        return best_index, best_socket_index

    def _branch_socket_center(
        self,
        source_index: int,
        branch: str,
        preferred_x: float | None = None,
        socket_index: int | None = None,
    ) -> QPointF | None:
        if branch == "next":
            hit_areas = self._next_socket_hit_areas
            centers = [(current_socket, rect.center()) for idx, current_socket, rect in hit_areas if idx == source_index]
        else:
            hit_areas = self._true_socket_hit_areas if branch == "true" else self._false_socket_hit_areas
            centers = [(0, rect.center()) for idx, rect in hit_areas if idx == source_index]
        if not centers:
            return None
        if socket_index is not None:
            safe_index = max(0, int(socket_index))
            for current_socket, center in centers:
                if current_socket == safe_index:
                    return center
        if preferred_x is None:
            return centers[0][1]
        return min((center for _, center in centers), key=lambda center: abs(center.x() - preferred_x))

    def _input_socket_center(
        self,
        target_index: int,
        preferred_x: float | None = None,
        socket_index: int | None = None,
    ) -> QPointF | None:
        centers = [
            (current_socket, rect.center())
            for idx, current_socket, rect in self._input_socket_hit_areas
            if idx == target_index
        ]
        if not centers:
            return None
        if socket_index is not None:
            safe_index = max(0, int(socket_index))
            for current_socket, center in centers:
                if current_socket == safe_index:
                    return center
        if preferred_x is None:
            return centers[0][1]
        return min((center for _, center in centers), key=lambda center: abs(center.x() - preferred_x))

    def _socket_count(self, block: dict[str, object], kind: str) -> int:
        block_type = str(block.get("type", "")).strip().lower()
        if kind == "input":
            if block_type == "start":
                return 0
            raw = int(block.get("input_count", 1) or 1)
            return max(1, min(raw, 6))
        if kind == "next":
            if block_type in {"end", "condition", "gate"}:
                return 0
            raw = int(block.get("output_count", 1) or 1)
            return max(1, min(raw, 6))
        return 1

    def _socket_ratios(self, block: dict[str, object], kind: str) -> list[float]:
        count = self._socket_count(block, kind)
        if count <= 0:
            return []
        if kind in {"true", "false"}:
            return [self._socket_ratio(block, kind)]
        key_map = {
            "input": "ui_input_xs",
            "next": "ui_output_xs",
        }
        array_key = key_map.get(kind, "")
        if array_key:
            raw = block.get(array_key, [])
            if isinstance(raw, list):
                values = [self._clamp(float(item), 0.08, 0.92) for item in raw if isinstance(item, (int, float))]
                if len(values) >= count:
                    return values[:count]
        anchor = self._socket_ratio(block, kind)
        if count == 1:
            return [anchor]
        span = min(0.72, 0.16 * float(count - 1))
        start = anchor - (span / 2.0)
        end = anchor + (span / 2.0)
        low = 0.08
        high = 0.92
        if start < low:
            shift = low - start
            start += shift
            end += shift
        if end > high:
            shift = end - high
            start -= shift
            end -= shift
        start = self._clamp(start, low, high)
        end = self._clamp(end, low, high)
        if count == 2:
            return [start, end]
        step = (end - start) / float(count - 1)
        return [self._clamp(start + (step * index), low, high) for index in range(count)]

    def _socket_centers(self, rect: QRectF, block: dict[str, object], kind: str) -> list[QPointF]:
        y = rect.top() + 6.0 if kind == "input" else rect.bottom() - 10.0
        return [QPointF(rect.left() + (rect.width() * ratio), y) for ratio in self._socket_ratios(block, kind)]

    def _socket_center(self, rect: QRectF, block: dict[str, object], kind: str) -> QPointF:
        centers = self._socket_centers(rect, block, kind)
        if centers:
            return centers[0]
        # Fallback for legacy callers.
        x = rect.left() + (rect.width() * self._socket_ratio(block, kind))
        y = rect.top() + 6.0 if kind == "input" else rect.bottom() - 10.0
        return QPointF(x, y)

    def _block_id_to_index(self) -> dict[str, int]:
        mapping: dict[str, int] = {}
        for idx, block in enumerate(self._blocks):
            if not isinstance(block, dict):
                continue
            block_id = str(block.get("_id", "")).strip()
            if block_id:
                mapping[block_id] = idx
        return mapping

    @staticmethod
    def _branch_target_ids_from_block(block: dict[str, object], branch: str) -> list[str]:
        list_key = "next_target_ids" if branch == "next" else "true_target_ids" if branch == "true" else "false_target_ids"
        single_key = "next_target_id" if branch == "next" else "true_target_id" if branch == "true" else "false_target_id"
        ids: list[str] = []
        raw_list = block.get(list_key, [])
        if isinstance(raw_list, list):
            for value in raw_list:
                target_id = str(value).strip()
                if target_id and target_id not in ids:
                    ids.append(target_id)
        raw_single = str(block.get(single_key, "")).strip()
        if raw_single and raw_single not in ids:
            ids.append(raw_single)
        return ids

    def _resolved_branch_target_indices(self, source_index: int, branch: str) -> list[int]:
        if source_index < 0 or source_index >= len(self._blocks):
            return []
        block = self._blocks[source_index]
        if not isinstance(block, dict):
            return []
        id_to_index = self._block_id_to_index()
        indices: list[int] = []
        for target_id in self._branch_target_ids_from_block(block, branch):
            target_idx = id_to_index.get(target_id, -1)
            if target_idx >= 0 and target_idx != source_index and target_idx not in indices:
                indices.append(target_idx)
        return indices

    def _connectivity_state(self) -> dict[int, bool]:
        id_to_index = self._block_id_to_index()
        incoming: dict[int, int] = {idx: 0 for idx in range(len(self._blocks))}
        out_next: dict[int, int] = {idx: 0 for idx in range(len(self._blocks))}
        out_true_false: dict[int, int] = {idx: 0 for idx in range(len(self._blocks))}
        for idx, block in enumerate(self._blocks):
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "")).strip().lower()
            if block_type in {"condition", "gate"}:
                for branch in ("true", "false"):
                    for target_id in self._branch_target_ids_from_block(block, branch):
                        target_idx = id_to_index.get(target_id, -1)
                        if target_idx < 0 or target_idx == idx:
                            continue
                        incoming[target_idx] = incoming.get(target_idx, 0) + 1
                        out_true_false[idx] = out_true_false.get(idx, 0) + 1
            elif block_type != "end":
                for target_id in self._branch_target_ids_from_block(block, "next"):
                    target_idx = id_to_index.get(target_id, -1)
                    if target_idx < 0 or target_idx == idx:
                        continue
                    incoming[target_idx] = incoming.get(target_idx, 0) + 1
                    out_next[idx] = out_next.get(idx, 0) + 1

        invalid: dict[int, bool] = {}
        for idx, block in enumerate(self._blocks):
            if not isinstance(block, dict):
                invalid[idx] = False
                continue
            block_type = str(block.get("type", "")).strip().lower()
            if block_type == "start":
                invalid[idx] = out_next.get(idx, 0) <= 0
            elif block_type == "end":
                invalid[idx] = incoming.get(idx, 0) <= 0
            elif block_type in {"condition", "gate"}:
                invalid[idx] = incoming.get(idx, 0) <= 0 or out_true_false.get(idx, 0) <= 0
            else:
                invalid[idx] = incoming.get(idx, 0) <= 0 or out_next.get(idx, 0) <= 0
        return invalid

    def _draw_top_level_connections(self, painter: QPainter) -> None:
        self._connection_hit_areas = []
        self._connection_delete_hit_areas = []
        self._connection_shelf_hit_areas = []
        connection_midpoints: dict[tuple[int, str, int], QPointF] = {}
        for idx, block in enumerate(self._blocks):
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "")).strip().lower()
            if block_type in {"condition", "gate"}:
                for branch, color in (
                    ("true", QColor(34, 211, 238)),
                    ("false", QColor(245, 158, 11)),
                ):
                    for target_idx in self._resolved_branch_target_indices(idx, branch):
                        target_socket_index = self._connection_target_socket_index(idx, branch, target_idx)
                        source = self._branch_socket_center(idx, branch)
                        if source is None:
                            continue
                        target = self._input_socket_center(target_idx, source.x(), socket_index=target_socket_index)
                        if target is None:
                            continue
                        key = (idx, branch, target_idx)
                        runtime_branch_active = self._runtime_active_branch == (idx, branch)
                        hovered = self._connection_hover == key
                        if runtime_branch_active:
                            pen_color = color.lighter(145)
                            line_width = 3.0
                        else:
                            pen_color = color.lighter(125) if hovered else color
                            line_width = 2.6 if hovered else 1.6
                        painter.setPen(QPen(pen_color, line_width))
                        segments = self._connection_segments(idx, branch, target_idx, source, target)
                        if not segments:
                            continue
                        for segment in segments:
                            painter.drawLine(segment)
                            self._connection_hit_areas.append((idx, branch, target_idx, segment))
                        for segment in segments:
                            if segment.length() < 6.0:
                                continue
                            if abs(segment.p1().y() - segment.p2().y()) <= 1.0:
                                left_x = min(segment.p1().x(), segment.p2().x())
                                right_x = max(segment.p1().x(), segment.p2().x())
                                shelf_rect = QRectF(left_x, segment.p1().y() - 8.0, max(1.0, right_x - left_x), 16.0)
                                self._connection_shelf_hit_areas.append((idx, branch, target_idx, "y", shelf_rect))
                            elif abs(segment.p1().x() - segment.p2().x()) <= 1.0:
                                top_y = min(segment.p1().y(), segment.p2().y())
                                bottom_y = max(segment.p1().y(), segment.p2().y())
                                shelf_rect = QRectF(segment.p1().x() - 8.0, top_y, 16.0, max(1.0, bottom_y - top_y))
                                self._connection_shelf_hit_areas.append((idx, branch, target_idx, "x", shelf_rect))
                        connection_midpoints[key] = self._polyline_midpoint(segments)
                        self._draw_connection_arrow(painter, segments[-1], color)
            elif block_type != "end":
                color = QColor(102, 178, 232)
                for target_idx in self._resolved_branch_target_indices(idx, "next"):
                    target_socket_index = self._connection_target_socket_index(idx, "next", target_idx)
                    source_socket_index = self._connection_source_socket_index_for_target(idx, "next", target_idx)
                    source_probe = self._branch_socket_center(idx, "next")
                    if source_probe is None:
                        continue
                    target = self._input_socket_center(target_idx, source_probe.x(), socket_index=target_socket_index)
                    if target is None:
                        continue
                    source = self._branch_socket_center(idx, "next", target.x(), socket_index=source_socket_index)
                    if source is None:
                        continue
                    key = (idx, "next", target_idx)
                    runtime_branch_active = self._runtime_active_branch == (idx, "next")
                    hovered = self._connection_hover == key
                    if runtime_branch_active:
                        pen_color = color.lighter(145)
                        line_width = 2.8
                    else:
                        pen_color = color.lighter(130) if hovered else color
                        line_width = 2.2 if hovered else 1.2
                    painter.setPen(QPen(pen_color, line_width))
                    segments = self._connection_segments(idx, "next", target_idx, source, target)
                    if not segments:
                        continue
                    for segment in segments:
                        painter.drawLine(segment)
                        self._connection_hit_areas.append((idx, "next", target_idx, segment))
                    for segment in segments:
                        if segment.length() < 6.0:
                            continue
                        if abs(segment.p1().y() - segment.p2().y()) <= 1.0:
                            left_x = min(segment.p1().x(), segment.p2().x())
                            right_x = max(segment.p1().x(), segment.p2().x())
                            shelf_rect = QRectF(left_x, segment.p1().y() - 8.0, max(1.0, right_x - left_x), 16.0)
                            self._connection_shelf_hit_areas.append((idx, "next", target_idx, "y", shelf_rect))
                        elif abs(segment.p1().x() - segment.p2().x()) <= 1.0:
                            top_y = min(segment.p1().y(), segment.p2().y())
                            bottom_y = max(segment.p1().y(), segment.p2().y())
                            shelf_rect = QRectF(segment.p1().x() - 8.0, top_y, 16.0, max(1.0, bottom_y - top_y))
                            self._connection_shelf_hit_areas.append((idx, "next", target_idx, "x", shelf_rect))
                    connection_midpoints[key] = self._polyline_midpoint(segments)
                    self._draw_connection_arrow(painter, segments[-1], color)

        if self._connection_hover is not None:
            source_index, branch, target_index = self._connection_hover
            key = (source_index, branch, target_index)
            center = connection_midpoints.get(key)
            if center is not None:
                delete_rect = QRectF(center.x() - 9.0, center.y() - 9.0, 18.0, 18.0)
                self._connection_delete_hit_areas.append((source_index, branch, target_index, delete_rect))
                painter.setPen(QPen(QColor(48, 83, 118), 1.2))
                painter.setBrush(QColor(9, 24, 45, 230))
                painter.drawRoundedRect(delete_rect, 6.0, 6.0)
                painter.setPen(QColor(227, 240, 252))
                painter.drawText(delete_rect, Qt.AlignmentFlag.AlignCenter, "✕")

    @staticmethod
    def _draw_input_marker(painter: QPainter, center: QPointF) -> None:
        # Input marker: ring + horizontal bar (visually different from output marker).
        rect = QRectF(center.x() - 6.0, center.y() - 6.0, 12.0, 12.0)
        painter.setPen(QPen(QColor(82, 111, 141), 1.2))
        painter.setBrush(QColor(6, 20, 38, 235))
        painter.drawEllipse(rect)
        painter.setPen(QPen(QColor(139, 192, 255), 1.1))
        painter.drawLine(QPointF(center.x() - 3.2, center.y()), QPointF(center.x() + 3.2, center.y()))

    @staticmethod
    def _draw_output_marker(painter: QPainter, center: QPointF) -> None:
        # Output marker: diamond shape.
        diamond = QPolygonF(
            [
                QPointF(center.x(), center.y() - 6.2),
                QPointF(center.x() + 6.2, center.y()),
                QPointF(center.x(), center.y() + 6.2),
                QPointF(center.x() - 6.2, center.y()),
            ]
        )
        painter.setPen(QPen(QColor(96, 165, 250), 1.2))
        painter.setBrush(QColor(13, 49, 89, 235))
        painter.drawPolygon(diamond)

    @staticmethod
    def _orthogonal_segments(source: QPointF, target: QPointF) -> list[QLineF]:
        if abs(source.y() - target.y()) <= 1.0:
            return [QLineF(source, target)]
        mid_y = (source.y() + target.y()) / 2.0
        p1 = QPointF(source.x(), mid_y)
        p2 = QPointF(target.x(), mid_y)
        return [QLineF(source, p1), QLineF(p1, p2), QLineF(p2, target)]

    def _connection_segments(
        self,
        source_index: int,
        branch: str,
        target_index: int,
        source: QPointF,
        target: QPointF,
    ) -> list[QLineF]:
        if abs(source.y() - target.y()) <= 1.0 and abs(source.x() - target.x()) <= 1.0:
            return []
        if abs(source.y() - target.y()) <= 1.0:
            return [QLineF(source, target)]
        x_ratio, y_ratio = self._connection_shelf_values(source_index, branch, target_index)
        low = min(source.y(), target.y())
        high = max(source.y(), target.y())
        low_x = min(source.x(), target.x())
        high_x = max(source.x(), target.x())
        span_y = max(1.0, high - low)
        span_x = max(1.0, high_x - low_x)

        if span_y <= 16.0:
            bend_y = (source.y() + target.y()) / 2.0
        else:
            bend_y = self._clamp(low + (span_y * y_ratio), low + 8.0, high - 8.0)

        if span_x <= 4.0:
            bend_x = (source.x() + target.x()) / 2.0
        else:
            # Allow shelves to move outside the direct source-target corridor.
            bend_x = self._clamp(low_x + (span_x * x_ratio), low_x - (span_x * 2.0), high_x + (span_x * 2.0))

        # Keep the final segment vertical into the input socket, so the arrow
        # points to the input naturally instead of sideways.
        approach = 8.0 if target.y() >= bend_y else -8.0
        pre_target_y = target.y() - approach
        if abs(pre_target_y - bend_y) < 2.0:
            pre_target_y = (bend_y + target.y()) / 2.0

        points = [
            source,
            QPointF(source.x(), bend_y),
            QPointF(bend_x, bend_y),
            QPointF(bend_x, pre_target_y),
            QPointF(target.x(), pre_target_y),
            target,
        ]
        segments: list[QLineF] = []
        for idx in range(len(points) - 1):
            line = QLineF(points[idx], points[idx + 1])
            if line.length() > 0.5:
                segments.append(line)
        return segments

    def _block_id_for_index(self, index: int) -> str:
        if index < 0 or index >= len(self._blocks):
            return ""
        block = self._blocks[index]
        if not isinstance(block, dict):
            return ""
        return str(block.get("_id", "")).strip()

    def _connection_shelf_key(self, branch: str, target_id: str) -> str:
        return f"{str(branch or '').strip().lower()}:{str(target_id or '').strip()}"

    def _connection_shelf_values(self, source_index: int, branch: str, target_index: int) -> tuple[float, float]:
        if source_index < 0 or source_index >= len(self._blocks):
            return 0.5, 0.5
        source_block = self._blocks[source_index]
        if not isinstance(source_block, dict):
            return 0.5, 0.5
        target_id = self._block_id_for_index(target_index)
        if not target_id:
            return 0.5, 0.5
        raw = source_block.get("ui_edge_bends", {})
        if not isinstance(raw, dict):
            return 0.5, 0.5
        key = self._connection_shelf_key(branch, target_id)
        value = raw.get(key, 0.5)
        if isinstance(value, dict):
            x_ratio = self._clamp(float(value.get("x", 0.5) or 0.5), -2.0, 3.0)
            y_ratio = self._clamp(float(value.get("y", 0.5) or 0.5), 0.1, 0.9)
            return x_ratio, y_ratio
        legacy = self._clamp(float(value or 0.5), 0.1, 0.9)
        return 0.5, legacy

    def _set_connection_shelf_position(
        self,
        source_index: int,
        branch: str,
        target_index: int,
        point: QPointF,
        axis: str,
    ) -> bool:
        source_socket_index = self._connection_source_socket_index_for_target(source_index, branch, target_index)
        target_socket_index = self._connection_target_socket_index(source_index, branch, target_index)
        source = self._branch_socket_center(source_index, branch, socket_index=source_socket_index)
        target = self._input_socket_center(target_index, socket_index=target_socket_index)
        if source is None or target is None:
            return False
        axis_key = "x" if str(axis).strip().lower() == "x" else "y"
        if axis_key == "x" and abs(source.x() - target.x()) <= 1.0:
            return False
        if axis_key == "y" and abs(source.y() - target.y()) <= 1.0:
            return False
        if axis_key == "x":
            low = min(source.x(), target.x())
            high = max(source.x(), target.x())
            span = max(1.0, high - low)
            ratio = self._clamp((float(point.x()) - low) / span, -2.0, 3.0)
        else:
            low = min(source.y(), target.y())
            high = max(source.y(), target.y())
            span = max(1.0, high - low)
            ratio = self._clamp((float(point.y()) - low) / span, 0.1, 0.9)

        if source_index < 0 or source_index >= len(self._blocks):
            return False
        source_block = self._blocks[source_index]
        if not isinstance(source_block, dict):
            return False
        target_id = self._block_id_for_index(target_index)
        if not target_id:
            return False
        key = self._connection_shelf_key(branch, target_id)
        bends = source_block.get("ui_edge_bends", {})
        if not isinstance(bends, dict):
            bends = {}
        current_x, current_y = self._connection_shelf_values(source_index, branch, target_index)
        current = current_x if axis_key == "x" else current_y
        if abs(current - ratio) <= 1e-4:
            return False
        if axis_key == "x":
            bends[key] = {"x": ratio, "y": current_y}
        else:
            bends[key] = {"x": current_x, "y": ratio}
        source_block["ui_edge_bends"] = bends
        return True

    def _connection_target_socket_index(self, source_index: int, branch: str, target_index: int) -> int:
        if source_index < 0 or source_index >= len(self._blocks):
            return 0
        source_block = self._blocks[source_index]
        if not isinstance(source_block, dict):
            return 0
        target_id = self._block_id_for_index(target_index)
        if not target_id:
            return 0
        mapping = source_block.get("ui_edge_target_ports", {})
        if not isinstance(mapping, dict):
            return 0
        key = self._connection_shelf_key(branch, target_id)
        return max(0, int(mapping.get(key, 0) or 0))

    def _connection_source_socket_index_for_target(self, source_index: int, branch: str, target_index: int) -> int:
        if source_index < 0 or source_index >= len(self._blocks):
            return 0
        source_block = self._blocks[source_index]
        if not isinstance(source_block, dict):
            return 0
        target_id = self._block_id_for_index(target_index)
        if not target_id:
            return 0
        mapping = source_block.get("ui_edge_source_ports", {})
        if not isinstance(mapping, dict):
            return 0
        key = self._connection_shelf_key(branch, target_id)
        return max(0, int(mapping.get(key, 0) or 0))

    def _has_output_connection(self, source_index: int, branch: str, socket_index: int) -> bool:
        if source_index < 0 or source_index >= len(self._blocks):
            return False
        source_block = self._blocks[source_index]
        if not isinstance(source_block, dict):
            return False
        target_indices = self._resolved_branch_target_indices(source_index, branch)
        if not target_indices:
            return False
        for target_index in target_indices:
            mapped_socket = self._connection_source_socket_index_for_target(source_index, branch, target_index)
            if mapped_socket == max(0, int(socket_index)):
                return True
        # Backward-compatible fallback for old flows without stored per-edge source socket mapping.
        return max(0, int(socket_index)) == 0

    def _has_input_connection(self, target_index: int, socket_index: int) -> bool:
        if target_index < 0 or target_index >= len(self._blocks):
            return False
        safe_socket = max(0, int(socket_index))
        for source_index, source_block in enumerate(self._blocks):
            if not isinstance(source_block, dict):
                continue
            for branch in ("next", "true", "false"):
                for linked_target_index in self._resolved_branch_target_indices(source_index, branch):
                    if linked_target_index != target_index:
                        continue
                    mapped_target_socket = self._connection_target_socket_index(source_index, branch, target_index)
                    if mapped_target_socket == safe_socket:
                        return True
        return False

    @staticmethod
    def _polyline_midpoint(segments: list[QLineF]) -> QPointF:
        if not segments:
            return QPointF()
        mid = segments[len(segments) // 2]
        return QPointF((mid.p1().x() + mid.p2().x()) / 2.0, (mid.p1().y() + mid.p2().y()) / 2.0)

    @staticmethod
    def _draw_connection_arrow(painter: QPainter, last_segment: QLineF, color: QColor) -> None:
        target = last_segment.p2()
        dx = last_segment.p2().x() - last_segment.p1().x()
        dy = last_segment.p2().y() - last_segment.p1().y()
        if abs(dx) >= abs(dy):
            if dx >= 0:
                arrow = QPolygonF([QPointF(target.x(), target.y()), QPointF(target.x() - 7.0, target.y() - 4.0), QPointF(target.x() - 7.0, target.y() + 4.0)])
            else:
                arrow = QPolygonF([QPointF(target.x(), target.y()), QPointF(target.x() + 7.0, target.y() - 4.0), QPointF(target.x() + 7.0, target.y() + 4.0)])
        else:
            if dy >= 0:
                arrow = QPolygonF([QPointF(target.x(), target.y()), QPointF(target.x() - 4.0, target.y() - 7.0), QPointF(target.x() + 4.0, target.y() - 7.0)])
            else:
                arrow = QPolygonF([QPointF(target.x(), target.y()), QPointF(target.x() - 4.0, target.y() + 7.0), QPointF(target.x() + 4.0, target.y() + 7.0)])
        painter.setBrush(color)
        painter.drawPolygon(arrow)

    @staticmethod
    def _distance_to_line(point: QPointF, line: QLineF) -> float:
        x0, y0 = point.x(), point.y()
        x1, y1 = line.p1().x(), line.p1().y()
        x2, y2 = line.p2().x(), line.p2().y()
        dx = x2 - x1
        dy = y2 - y1
        length_sq = (dx * dx) + (dy * dy)
        if length_sq <= 1e-6:
            return ((x0 - x1) ** 2 + (y0 - y1) ** 2) ** 0.5
        t = ((x0 - x1) * dx + (y0 - y1) * dy) / length_sq
        t = max(0.0, min(1.0, t))
        proj_x = x1 + (t * dx)
        proj_y = y1 + (t * dy)
        return ((x0 - proj_x) ** 2 + (y0 - proj_y) ** 2) ** 0.5

    def _connection_at_point(self, point: QPointF) -> tuple[int, str, int] | None:
        best: tuple[int, str, int] | None = None
        best_distance = 99999.0
        for source_index, branch, target_index, line in self._connection_hit_areas:
            distance = self._distance_to_line(point, line)
            if distance <= 8.0 and distance < best_distance:
                best_distance = distance
                best = (source_index, branch, target_index)
        return best

    def _advance_dash_phase(self) -> None:
        self._dash_phase -= 1.0
        if self._dash_phase < -1000.0:
            self._dash_phase = 0.0
        self.update()

    def _ensure_dash_animation(self, enabled: bool) -> None:
        if enabled:
            if not self._dash_timer.isActive():
                self._dash_phase = 0.0
                self._dash_timer.start()
        elif self._dash_timer.isActive():
            self._dash_timer.stop()

    def _update_external_drag_target(self, point: QPointF, mime: QMimeData) -> None:
        block_type = bytes(mime.data(BLOCK_MIME)).decode("utf-8").strip().lower()
        self._drag_hover_block_index = -1
        self._drag_nested_gate_target = None
        if block_type in {"condition", "gate"}:
            nested_target = self._nested_gate_drop_target_for_point(point)
            if nested_target is not None:
                self._drag_nested_gate_target = nested_target
            else:
                gate_index = self._gate_drop_target_for_palette(point)
                if gate_index >= 0:
                    self._drag_hover_block_index = gate_index
        self._ensure_dash_animation(self._drag_hover_block_index >= 0 or self._drag_nested_gate_target is not None)
        self.update()


class RuleListRowWidget(QWidget):
    selected = Signal(str)
    toggled = Signal(str, bool)

    def __init__(self, rule_id: str, name: str, enabled: bool, parent=None) -> None:
        super().__init__(parent)
        self._rule_id = rule_id
        self.setObjectName("AutomationRuleRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        self.enabled_check = QCheckBox()
        self.enabled_check.setObjectName("AutomationRuleEnabled")
        self.enabled_check.setChecked(enabled)
        self.enabled_check.setCursor(Qt.CursorShape.PointingHandCursor)

        self.name_label = QLabel(name)
        self.name_label.setObjectName("AutomationRuleName")
        self.name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout.addWidget(self.enabled_check)
        layout.addWidget(self.name_label, 1)

        self.enabled_check.toggled.connect(lambda checked: self.toggled.emit(self._rule_id, checked))

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", bool(selected))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_name(self, name: str) -> None:
        self.name_label.setText(name)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.selected.emit(self._rule_id)
        super().mousePressEvent(event)


class AutomationTab(QWidget):
    rules_changed = Signal()
    manual_run_requested = Signal(str)
    simulate_requested = Signal(str)
    logs_clear_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rules: list[AutomationRule] = []
        self._device_options: list[tuple[str, str]] = []
        self._measurement_keys: list[str] = []
        self._measurement_keys_by_device: dict[str, list[str]] = {}
        self._inverter_setting_options: list[dict[str, object]] = []
        self._action_inverter_rows: list[dict[str, object]] = []
        self._active_rule_id: str | None = None
        self._runtime_rule_id: str | None = None
        self._runtime_node_id: str | None = None
        self._runtime_branch: str | None = None
        self._syncing = False
        self._selected_nested_condition: tuple[int, int] | None = None
        self._rule_row_widgets: dict[str, RuleListRowWidget] = {}
        self._palette_tile_by_type: dict[str, PaletteTileButton] = {}
        self._global_rule_cooldown_sec = 60

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        section = QFrame()
        section.setObjectName("ChartSection")
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(20, 20, 20, 20)
        section_layout.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel(tr("Automations"))
        title.setObjectName("ChartSectionTitle")
        subtitle = QLabel(tr("Build flows with drag and drop, activate drafts, and run Tuya scenarios."))
        subtitle.setObjectName("SidebarMeta")
        title_row.addWidget(title)
        title_row.addStretch(1)

        body = QGridLayout()
        body.setHorizontalSpacing(12)
        body.setVerticalSpacing(12)

        self.rule_list = QListWidget()
        self.rule_list.setObjectName("AutomationList")
        self.rule_list.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self.rule_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        self.canvas = FlowCanvas()
        self.canvas.setObjectName("AutomationList")
        self.canvas.hide()
        self.diagram_view = FlowDiagramView()
        self.diagram_view.setObjectName("AutomationDiagram")
        self.diagram_scroll = QScrollArea()
        self.diagram_scroll.setObjectName("AutomationList")
        self.diagram_scroll.setWidgetResizable(True)
        self.diagram_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.diagram_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.diagram_scroll.setWidget(self.diagram_view)
        self.diagram_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)

        self.block_editor = QStackedWidget()
        self.block_editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.block_editor.addWidget(self._build_empty_editor())
        self.trigger_editor = self._build_trigger_editor()
        self.condition_editor = self._build_condition_editor()
        self.gate_editor = self._build_gate_editor()
        self.delay_editor = self._build_delay_editor()
        self.action_editor = self._build_action_editor()
        self.block_editor.addWidget(self.trigger_editor)
        self.block_editor.addWidget(self.condition_editor)
        self.block_editor.addWidget(self.gate_editor)
        self.block_editor.addWidget(self.delay_editor)
        self.block_editor.addWidget(self.action_editor)
        self.block_editor.currentChanged.connect(lambda *_args: self._update_block_editor_height())

        self.logs_view = QTextBrowser()
        self.logs_view.setObjectName("AutomationText")
        self.logs_view.setMaximumHeight(160)
        self.analytics_label = QLabel(tr("No executions yet."))
        self.analytics_label.setObjectName("SidebarMeta")
        self.analytics_label.setWordWrap(True)

        left = QVBoxLayout()
        rules_header = QHBoxLayout()
        rules_header.setContentsMargins(0, 0, 0, 0)
        rules_header.setSpacing(8)
        rules_header.addWidget(QLabel(tr("Rules")))
        self.add_rule_btn = QPushButton("+")
        self.add_rule_btn.setObjectName("AutomationHeaderPlusButton")
        self.add_rule_btn.setToolTip(tr("New Rule"))
        self.add_rule_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_rule_btn.setFixedSize(24, 24)
        rules_header.addWidget(self.add_rule_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.import_rule_btn = self._header_action_button("⤒", tr("Import JSON"), self._import_rule_json)
        self.export_rule_btn = self._header_action_button("⤓", tr("Export JSON"), self._export_rule_json)
        self.duplicate_rule_btn = self._header_action_button("⧉", tr("Duplicate"), self._duplicate_rule)
        self.manual_run_btn = self._header_action_button("▶", tr("Run"), self._emit_manual_run)
        self.simulate_btn = self._header_action_button("◉", tr("Simulate"), self._emit_simulate)
        self.delete_rule_btn = self._header_action_button("✕", tr("Delete"), self._delete_rule)
        rules_header.addWidget(self.import_rule_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        rules_header.addWidget(self.export_rule_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        rules_header.addWidget(self.duplicate_rule_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        rules_header.addWidget(self.manual_run_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        rules_header.addWidget(self.simulate_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        rules_header.addWidget(self.delete_rule_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        rules_header.addStretch(1)
        left.addLayout(rules_header)
        left.addWidget(self.rule_list, 1)
        left_analytics = QWidget()
        left_analytics_layout = QVBoxLayout(left_analytics)
        left_analytics_layout.setContentsMargins(0, 0, 0, 0)
        left_analytics_layout.setSpacing(12)
        left_analytics_layout.addWidget(QLabel(tr("Execution Logs")))
        left_analytics_layout.addWidget(self.logs_view)
        analytics_row = QHBoxLayout()
        analytics_row.setContentsMargins(0, 0, 0, 0)
        analytics_row.setSpacing(8)
        analytics_row.addWidget(self.analytics_label, 1)
        self.clear_logs_link = QLabel(f'<a href="clear">{tr("Clear")}</a>')
        self.clear_logs_link.setObjectName("AutomationInlineLink")
        self.clear_logs_link.setTextFormat(Qt.TextFormat.RichText)
        self.clear_logs_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.clear_logs_link.setOpenExternalLinks(False)
        self.clear_logs_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_logs_link.linkActivated.connect(lambda _href: self.logs_clear_requested.emit())
        analytics_row.addWidget(self.clear_logs_link, 0, Qt.AlignmentFlag.AlignRight)
        left_analytics_layout.addLayout(analytics_row)
        left.addWidget(left_analytics, 0)

        middle = QVBoxLayout()
        flow_header = QHBoxLayout()
        flow_header.setContentsMargins(0, 0, 0, 0)
        flow_header.setSpacing(8)
        flow_header.addWidget(QLabel(tr("Flow Canvas (drag to reorder)")))
        flow_header.addStretch(1)
        middle.addLayout(flow_header)
        self.palette_tiles = QWidget()
        self.palette_tiles.setObjectName("AutomationPaletteTiles")
        palette_tiles_layout = QHBoxLayout(self.palette_tiles)
        palette_tiles_layout.setContentsMargins(0, 0, 0, 0)
        palette_tiles_layout.setSpacing(8)
        tile_specs = [
            ("trigger", "◎", tr("Trigger")),
            ("condition", "◇", tr("Condition")),
            ("gate", "⊞", tr("Gate")),
            ("delay", "⏱", tr("Delay")),
            ("action", "⚙", tr("Action")),
        ]
        for block_type, icon, title_text in tile_specs:
            tile = PaletteTileButton(
                block_type=block_type,
                icon=icon,
                title=title_text,
                tooltip="",
            )
            tile.block_requested.connect(self._add_block)
            self._palette_tile_by_type[block_type] = tile
            palette_tiles_layout.addWidget(tile)
        palette_tiles_layout.addStretch(1)
        middle.addWidget(self.palette_tiles)
        middle.addWidget(self.diagram_scroll, 1)

        right_fields_panel = QWidget()
        right_fields_panel.setObjectName("AutomationRightPanel")
        right_fields = QVBoxLayout(right_fields_panel)
        right_fields.setContentsMargins(14, 14, 14, 14)
        right_fields.setSpacing(16)
        right_fields.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.selected_block_title = QLabel("")
        self.selected_block_title.setObjectName("ChartSectionTitle")
        self.selected_block_title.setWordWrap(True)
        right_fields.addWidget(self.selected_block_title)
        self.io_controls_widget = self._build_io_controls()
        right_fields.addWidget(self.io_controls_widget)
        right_fields.addWidget(self.block_editor)

        right_scroll = QScrollArea()
        right_scroll.setObjectName("AutomationRightScroll")
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        right_scroll.setWidget(right_fields_panel)

        right_column = QVBoxLayout()
        right_column.setContentsMargins(0, 0, 0, 0)
        right_column.setSpacing(12)
        right_column.addWidget(QLabel(tr("Settings")))
        right_column.addWidget(right_scroll, 1)

        body.addLayout(left, 0, 0)
        body.addLayout(middle, 0, 1)
        body.addLayout(right_column, 0, 2)
        body.setColumnStretch(0, 2)
        body.setColumnStretch(1, 3)
        body.setColumnStretch(2, 3)

        section_layout.addLayout(title_row)
        section_layout.addWidget(subtitle)
        section_layout.addLayout(body)
        root.addWidget(section)

        self.add_rule_btn.clicked.connect(self._add_rule)
        self.rule_list.currentRowChanged.connect(self._switch_rule)
        self.canvas.block_added.connect(self._add_block)
        self.canvas.currentRowChanged.connect(self._show_selected_block_editor)
        self.canvas.currentRowChanged.connect(self._sync_diagram_selection)
        self.canvas.model().rowsMoved.connect(lambda *_args: self._save_current_rule())
        self.canvas.model().rowsMoved.connect(lambda *_args: self._sync_diagram_from_canvas())
        self.canvas.model().rowsInserted.connect(lambda *_args: self._sync_diagram_from_canvas())
        self.canvas.model().rowsRemoved.connect(lambda *_args: self._sync_diagram_from_canvas())
        self.canvas.model().dataChanged.connect(lambda *_args: self._sync_diagram_from_canvas())
        self.diagram_view.block_added.connect(self._add_block)
        self.diagram_view.palette_block_dropped_to_gate.connect(self._add_palette_block_into_gate)
        self.diagram_view.palette_block_dropped_to_nested_gate.connect(self._add_palette_block_into_nested_gate)
        self.diagram_view.block_selected.connect(self._select_block_from_diagram)
        self.diagram_view.block_delete_requested.connect(self._remove_block_by_index)
        self.diagram_view.blocks_reordered.connect(self._apply_diagram_reorder)
        self.diagram_view.condition_dropped_to_gate.connect(self._move_condition_into_gate_from_diagram)
        self.diagram_view.condition_dropped_to_nested_gate.connect(self._move_condition_into_nested_gate_from_diagram)
        self.diagram_view.gate_condition_selected.connect(self._select_nested_condition_from_diagram)
        self.diagram_view.gate_condition_reordered.connect(self._reorder_gate_child_in_diagram)
        self.diagram_view.gate_condition_extract_requested.connect(self._extract_condition_from_gate_in_diagram)
        self.diagram_view.gate_condition_delete_requested.connect(self._remove_condition_from_gate_in_diagram)
        self.diagram_view.gate_condition_dropped_to_nested_gate.connect(self._move_gate_child_into_nested_gate_in_diagram)
        self.diagram_view.gate_condition_dropped_to_parent_gate.connect(self._move_gate_child_into_parent_gate_in_diagram)
        self.diagram_view.branch_connected.connect(self._set_branch_connection_from_diagram)
        self.diagram_view.branch_deleted.connect(self._clear_branch_connection_from_diagram)

        self._connect_block_editors()
        self._add_rule()
        self._update_palette_block_availability()
        self._update_block_editor_height()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_diagram_geometry()

    def set_devices(self, devices: list[TuyaDevice] | list[tuple[str, str]]) -> None:
        options: list[tuple[str, str]] = []
        for item in devices:
            if isinstance(item, tuple) and len(item) >= 2:
                key = str(item[0]).strip()
                title = str(item[1]).strip() or key
                if key:
                    options.append((key, title))
                continue
            if isinstance(item, TuyaDevice):
                key = item.device_id.strip()
                if key:
                    options.append((key, item.name or item.device_name or key))
        self._device_options = options
        for combo in (self.trigger_device, self.condition_device, self.action_device):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for key, title in options:
                combo.addItem(title, key)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        self._refresh_trigger_metric_options()
        self._refresh_condition_metric_options()
        self._update_action_editor_mode()

    def set_measurement_keys(self, keys: list[str]) -> None:
        cleaned = sorted({str(item).strip() for item in keys if str(item).strip()})
        self._measurement_keys = cleaned
        self._measurement_keys_by_device = {"": cleaned}
        for combo in (self.trigger_metric, self.condition_metric):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            for key in cleaned:
                combo.addItem(key, key)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def set_measurements_by_device(self, keys_by_device: dict[str, Iterable[str]]) -> None:
        normalized: dict[str, list[str]] = {}
        global_keys: set[str] = set()
        for device_id, keys in keys_by_device.items():
            clean_device_id = str(device_id or "").strip()
            clean_keys = sorted({str(item).strip() for item in keys if str(item).strip()})
            normalized[clean_device_id] = clean_keys
            global_keys.update(clean_keys)
        normalized.setdefault("", sorted(global_keys))
        self._measurement_keys_by_device = normalized
        self._measurement_keys = normalized.get("", [])
        self._refresh_trigger_metric_options()
        self._refresh_condition_metric_options()

    def set_inverter_control_fields(self, fields: list[dict[str, object]]) -> None:
        normalized: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for raw in fields:
            if not isinstance(raw, dict):
                continue
            field_id = str(raw.get("field_id", "")).strip()
            if not field_id or field_id in seen_ids:
                continue
            seen_ids.add(field_id)
            label = str(raw.get("label", "")).strip() or field_id
            unit = str(raw.get("unit", "")).strip()
            options_raw = raw.get("options", [])
            options: list[tuple[str, str]] = []
            if isinstance(options_raw, list):
                for item in options_raw:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        options.append((str(item[0]).strip(), str(item[1]).strip()))
            normalized.append(
                {
                    "field_id": field_id,
                    "label": label,
                    "unit": unit,
                    "options": options,
                }
            )
        self._inverter_setting_options = sorted(
            normalized,
            key=lambda item: str(item.get("label", "")).lower(),
        )
        if self._action_is_inverter_mode():
            self._refresh_inverter_action_field_combos()
            if not self._action_inverter_rows and self._inverter_setting_options:
                self._add_inverter_action_row()
        self._update_action_editor_mode()

    def _action_is_inverter_mode(self) -> bool:
        return str(self.action_device.currentData() or "").strip() == INVERTER_DEVICE_ID

    def _update_action_editor_mode(self) -> None:
        inverter_mode = self._action_is_inverter_mode()
        if hasattr(self, "_action_form"):
            self._action_form.setRowVisible(self.action_state, not inverter_mode)
            self._action_form.setRowVisible(self.action_inverter_editor, inverter_mode)
        if inverter_mode:
            if hasattr(self, "action_inverter_hint") and isinstance(self.action_inverter_hint, QLabel):
                if self._inverter_setting_options:
                    self.action_inverter_hint.setText(
                        tr("Add inverter setting changes. Each field can be selected once.")
                    )
                else:
                    self.action_inverter_hint.setText(tr("No available inverter setting fields."))
            if not self._action_inverter_rows and self._inverter_setting_options:
                self._add_inverter_action_row()
            self._refresh_inverter_action_field_combos()
        elif hasattr(self, "action_inverter_hint") and isinstance(self.action_inverter_hint, QLabel):
            self.action_inverter_hint.setText(
                tr("Add inverter setting changes. Each field can be selected once.")
            )
        self._update_block_editor_height()
        self._save_block_editor()

    def _inverter_field_by_id(self, field_id: str) -> dict[str, object] | None:
        normalized = str(field_id).strip()
        for item in self._inverter_setting_options:
            if str(item.get("field_id", "")).strip() == normalized:
                return item
        return None

    def _inverter_field_text(self, field_id: str) -> str:
        data = self._inverter_field_by_id(field_id)
        if data is None:
            return field_id
        label = str(data.get("label", "")).strip() or field_id
        return label

    def _selected_inverter_field_ids(self) -> set[str]:
        selected: set[str] = set()
        for row in self._action_inverter_rows:
            combo = row.get("field_combo")
            if isinstance(combo, QComboBox):
                field_id = str(combo.currentData() or "").strip()
                if field_id:
                    selected.add(field_id)
        return selected

    def _available_inverter_field_ids_for_row(self, current_field_id: str) -> list[str]:
        used_elsewhere = self._selected_inverter_field_ids()
        if current_field_id in used_elsewhere:
            used_elsewhere.remove(current_field_id)
        available: list[str] = []
        for item in self._inverter_setting_options:
            field_id = str(item.get("field_id", "")).strip()
            if field_id and field_id not in used_elsewhere:
                available.append(field_id)
        return available

    def _refresh_inverter_action_field_combos(self) -> None:
        for row in self._action_inverter_rows:
            combo = row.get("field_combo")
            if not isinstance(combo, QComboBox):
                continue
            current_field_id = str(combo.currentData() or "").strip()
            available = self._available_inverter_field_ids_for_row(current_field_id)
            combo.blockSignals(True)
            combo.clear()
            for field_id in available:
                combo.addItem(self._inverter_field_text(field_id), field_id)
            if available:
                target_field_id = current_field_id if current_field_id in available else available[0]
                combo.setCurrentIndex(combo.findData(target_field_id))
            combo.blockSignals(False)
            self._ensure_combo_popup_width(combo)
            self._refresh_inverter_action_row_input(row)
        self._refresh_inverter_action_row_controls()

    def _refresh_inverter_action_row_controls(self) -> None:
        can_add_more = bool(self._inverter_setting_options) and (
            len(self._selected_inverter_field_ids()) < len(self._inverter_setting_options)
        )
        last_index = len(self._action_inverter_rows) - 1
        for index, row in enumerate(self._action_inverter_rows):
            add_btn = row.get("add_btn")
            if isinstance(add_btn, QPushButton):
                show_add = index == last_index and can_add_more
                # Keep width stable across rows so both editor fields stay equal.
                add_btn.setVisible(True)
                add_btn.setEnabled(show_add)

    def _refresh_inverter_action_row_input(self, row: dict[str, object]) -> None:
        field_combo = row.get("field_combo")
        value_stack = row.get("value_stack")
        value_combo = row.get("value_combo")
        value_edit = row.get("value_edit")
        if not isinstance(field_combo, QComboBox):
            return
        if not isinstance(value_stack, QStackedWidget):
            return
        if not isinstance(value_combo, QComboBox):
            return
        if not isinstance(value_edit, QLineEdit):
            return
        field_id = str(field_combo.currentData() or "").strip()
        field_data = self._inverter_field_by_id(field_id) or {}
        options = field_data.get("options", [])
        value_combo.blockSignals(True)
        value_combo.clear()
        if isinstance(options, list) and options:
            for option_value, option_label in options:
                value_combo.addItem(option_label or option_value, option_value)
            current_value = str(row.get("value", "")).strip()
            idx = value_combo.findData(current_value)
            value_combo.setCurrentIndex(idx if idx >= 0 else 0)
            row["value"] = str(value_combo.currentData() or "")
            value_stack.setCurrentIndex(0)
            self._ensure_combo_popup_width(value_combo)
        else:
            current_text = str(row.get("value", "")).strip()
            value_edit.blockSignals(True)
            value_edit.setText(current_text)
            value_edit.blockSignals(False)
            value_stack.setCurrentIndex(1)
        value_combo.blockSignals(False)
        self._save_block_editor()

    def _add_inverter_action_row(self, _checked: bool = False, *, field_id: str = "", value: object = "") -> None:
        if not self._inverter_setting_options:
            return
        available = self._available_inverter_field_ids_for_row("")
        if not available and not field_id:
            return
        selected_field_id = str(field_id).strip()
        if not selected_field_id:
            selected_field_id = available[0] if available else ""
        if not selected_field_id:
            return

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)

        field_combo = QComboBox()
        field_combo.setMinimumWidth(0)
        field_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        field_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        field_combo.setMinimumContentsLength(1)
        value_stack = QStackedWidget()
        value_stack.setMinimumWidth(0)
        value_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        value_combo = QComboBox()
        value_combo.setMinimumWidth(0)
        value_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        value_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        value_combo.setMinimumContentsLength(1)
        value_edit = QLineEdit()
        value_edit.setMinimumWidth(0)
        value_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        value_edit.setPlaceholderText(tr("Value"))
        controls_wrap = QWidget()
        controls_wrap.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        controls_wrap.setFixedWidth(44)
        controls_layout = QHBoxLayout(controls_wrap)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        remove_btn = QPushButton("✕")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.setFixedSize(18, 18)
        remove_btn.setFlat(True)
        remove_btn.setStyleSheet(
            "QPushButton {"
            "background: transparent;"
            "border: none;"
            "color: #22d3ee;"
            "font-size: 18px;"
            "font-weight: 800;"
            "padding: 0;"
            "}"
            "QPushButton:hover { color: #67e8f9; }"
            "QPushButton:pressed { color: #06b6d4; }"
            "QPushButton:disabled { color: rgba(34, 211, 238, 0.4); }"
        )
        add_btn = QPushButton("+")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setFixedSize(18, 18)
        add_btn.setFlat(True)
        add_btn.setStyleSheet(
            "QPushButton {"
            "background: transparent;"
            "border: none;"
            "color: #22d3ee;"
            "font-size: 18px;"
            "font-weight: 800;"
            "padding: 0;"
            "}"
            "QPushButton:hover { color: #67e8f9; }"
            "QPushButton:pressed { color: #06b6d4; }"
            "QPushButton:disabled { color: rgba(34, 211, 238, 0.4); }"
        )

        value_stack.addWidget(value_combo)
        value_stack.addWidget(value_edit)
        controls_layout.addWidget(remove_btn)
        controls_layout.addWidget(add_btn)
        row_layout.addWidget(field_combo, 1)
        row_layout.addWidget(value_stack, 1)
        row_layout.addWidget(controls_wrap, 0)

        row_data: dict[str, object] = {
            "widget": row_widget,
            "field_combo": field_combo,
            "value_stack": value_stack,
            "value_combo": value_combo,
            "value_edit": value_edit,
            "remove_btn": remove_btn,
            "add_btn": add_btn,
            "value": str(value).strip(),
        }
        self._action_inverter_rows.append(row_data)
        self.action_inverter_rows_layout.addWidget(row_widget)

        field_combo.currentIndexChanged.connect(lambda *_args, row=row_data: self._on_inverter_action_row_changed(row))
        value_combo.currentIndexChanged.connect(lambda *_args, row=row_data: self._on_inverter_action_row_value_changed(row))
        value_edit.textChanged.connect(lambda *_args, row=row_data: self._on_inverter_action_row_value_changed(row))
        remove_btn.clicked.connect(lambda _checked=False, row=row_data: self._remove_inverter_action_row(row))
        add_btn.clicked.connect(self._add_inverter_action_row)

        self._refresh_inverter_action_field_combos()
        target_index = field_combo.findData(selected_field_id)
        if target_index >= 0:
            field_combo.setCurrentIndex(target_index)
        self._refresh_inverter_action_row_input(row_data)
        self._update_block_editor_height()
        self._save_block_editor()

    def _remove_inverter_action_row(self, row: dict[str, object]) -> None:
        if row not in self._action_inverter_rows:
            return
        self._action_inverter_rows.remove(row)
        widget = row.get("widget")
        if isinstance(widget, QWidget):
            self.action_inverter_rows_layout.removeWidget(widget)
            widget.deleteLater()
        self._refresh_inverter_action_field_combos()
        self._update_block_editor_height()
        self._save_block_editor()

    def _on_inverter_action_row_changed(self, row: dict[str, object]) -> None:
        self._refresh_inverter_action_field_combos()
        self._refresh_inverter_action_row_input(row)
        self._save_block_editor()

    def _on_inverter_action_row_value_changed(self, row: dict[str, object]) -> None:
        value_stack = row.get("value_stack")
        value_combo = row.get("value_combo")
        value_edit = row.get("value_edit")
        if isinstance(value_stack, QStackedWidget) and isinstance(value_combo, QComboBox) and value_stack.currentIndex() == 0:
            row["value"] = str(value_combo.currentData() or "")
        elif isinstance(value_edit, QLineEdit):
            row["value"] = value_edit.text().strip()
        self._save_block_editor()

    def _clear_inverter_action_rows(self) -> None:
        for row in list(self._action_inverter_rows):
            widget = row.get("widget")
            if isinstance(widget, QWidget):
                self.action_inverter_rows_layout.removeWidget(widget)
                widget.deleteLater()
        self._action_inverter_rows.clear()
        self._refresh_inverter_action_field_combos()
        self._update_block_editor_height()

    def _load_inverter_action_rows(self, changes: list[dict[str, object]]) -> None:
        self._clear_inverter_action_rows()
        seen: set[str] = set()
        for change in changes:
            if not isinstance(change, dict):
                continue
            field_id = str(change.get("field_id", "")).strip()
            if not field_id or field_id in seen:
                continue
            seen.add(field_id)
            self._add_inverter_action_row(field_id=field_id, value=change.get("value", ""))
        if not self._action_inverter_rows and self._inverter_setting_options:
            self._add_inverter_action_row()

    def _collect_inverter_action_changes(self) -> list[dict[str, object]]:
        changes: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in self._action_inverter_rows:
            field_combo = row.get("field_combo")
            if not isinstance(field_combo, QComboBox):
                continue
            field_id = str(field_combo.currentData() or "").strip()
            if not field_id or field_id in seen:
                continue
            seen.add(field_id)
            value_stack = row.get("value_stack")
            value_combo = row.get("value_combo")
            value_edit = row.get("value_edit")
            if isinstance(value_stack, QStackedWidget) and isinstance(value_combo, QComboBox) and value_stack.currentIndex() == 0:
                value = str(value_combo.currentData() or "")
            elif isinstance(value_edit, QLineEdit):
                value = value_edit.text().strip()
            else:
                value = str(row.get("value", "")).strip()
            changes.append(
                {
                    "field_id": field_id,
                    "value": value,
                }
            )
        return changes

    def set_rules(self, rules: list[AutomationRule]) -> None:
        self._rules = [replace(item) for item in rules]
        for rule in self._rules:
            rule.cooldown_sec = int(self._global_rule_cooldown_sec)
        self._refresh_rule_list()

    def set_global_cooldown_sec(self, seconds: int) -> None:
        normalized = max(0, int(seconds))
        if self._global_rule_cooldown_sec == normalized:
            return
        self._global_rule_cooldown_sec = normalized
        for rule in self._rules:
            rule.cooldown_sec = normalized
        self.rules_changed.emit()

    def rules(self) -> list[AutomationRule]:
        return [replace(item) for item in self._rules]

    def set_logs(self, lines: list[str]) -> None:
        self.logs_view.setPlainText("\n".join(lines[-120:]))

    def set_analytics(self, logs_by_rule_id: dict[str, dict[str, int]]) -> None:
        selected = self._current_rule()
        if selected is None:
            self.analytics_label.setText(tr("No selected rule."))
            return
        bucket = logs_by_rule_id.get(selected.rule_id, {"ok": 0, "error": 0, "info": 0})
        self.analytics_label.setText(
            tr("Success: {ok}  Error: {error}  Info: {info}").format(
                ok=bucket.get("ok", 0),
                error=bucket.get("error", 0),
                info=bucket.get("info", 0),
            )
        )

    def set_runtime_active_node(self, rule_id: str, node_id: str, branch: str = "") -> None:
        self._runtime_rule_id = str(rule_id or "").strip() or None
        self._runtime_node_id = str(node_id or "").strip() or None
        self._runtime_branch = str(branch or "").strip().lower() or None
        self._apply_runtime_highlight()

    def _build_empty_editor(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty_editor_hint = QLabel(tr("Select a block to edit."))
        self.empty_editor_hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.empty_editor_hint.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.empty_editor_hint, 0, Qt.AlignmentFlag.AlignTop)
        return box

    def _build_io_controls(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        self._io_form = form
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.io_input_count = QSpinBox()
        self.io_input_count.setRange(1, 6)
        self.io_output_count = QSpinBox()
        self.io_output_count.setRange(1, 6)
        form.addRow(tr("Inputs"), self.io_input_count)
        form.addRow(tr("Outputs"), self.io_output_count)
        self._expand_fields(self.io_input_count, self.io_output_count)
        box.setVisible(False)
        return box

    def _supports_io_counts(self, kind: str) -> tuple[bool, bool]:
        normalized = str(kind or "").strip().lower()
        supports_input = normalized != "start"
        supports_output = normalized not in {"end", "condition", "gate"}
        return supports_input, supports_output

    def _load_io_controls_for_block(self, block: dict[str, object]) -> None:
        kind = str(block.get("type", "")).strip().lower()
        supports_input, supports_output = self._supports_io_counts(kind)
        show_controls = supports_input or supports_output
        self.io_controls_widget.setVisible(show_controls)
        if not show_controls:
            return
        self._syncing = True
        if supports_input:
            self.io_input_count.setValue(max(1, min(6, int(block.get("input_count", 1) or 1))))
        if supports_output:
            self.io_output_count.setValue(max(1, min(6, int(block.get("output_count", 1) or 1))))
        self._set_form_row_visible(self._io_form, self.io_input_count, supports_input)
        self._set_form_row_visible(self._io_form, self.io_output_count, supports_output)
        self._syncing = False

    def _apply_io_controls_to_block(self, block: dict[str, object]) -> None:
        kind = str(block.get("type", "")).strip().lower()
        supports_input, supports_output = self._supports_io_counts(kind)
        if supports_input:
            block["input_count"] = max(1, min(6, int(self.io_input_count.value())))
        else:
            block.pop("input_count", None)
        if supports_output:
            block["output_count"] = max(1, min(6, int(self.io_output_count.value())))
        else:
            block.pop("output_count", None)

    def _build_trigger_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        self._trigger_form = form
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.trigger_mode = QComboBox()
        self.trigger_mode.addItem(tr("Measurement"), "measurement")
        self.trigger_mode.addItem(tr("Schedule"), "schedule")
        self.trigger_mode.addItem(tr("Snapshot update"), "snapshot_update")
        self.trigger_mode.addItem(tr("Manual"), "manual")
        self.trigger_device = QComboBox()
        self.trigger_metric = QComboBox()
        self.trigger_operator = QComboBox()
        for op in (">=", ">", "<=", "<", "==", "!="):
            self.trigger_operator.addItem(op, op)
        self.trigger_value = QDoubleSpinBox()
        self.trigger_value.setRange(-1_000_000.0, 1_000_000.0)
        self.trigger_value.setDecimals(3)
        self.trigger_schedule = QSpinBox()
        self.trigger_schedule.setRange(1, 1440)
        self.trigger_schedule.setSuffix(" min")
        self.trigger_snapshot_hint = QLabel(tr("Fires when a new data snapshot is received."))
        self.trigger_snapshot_hint.setObjectName("SidebarMeta")
        self.trigger_snapshot_hint.setWordWrap(True)
        form.addRow(tr("Type"), self.trigger_mode)
        form.addRow(tr("Device"), self.trigger_device)
        form.addRow(tr("Metric"), self.trigger_metric)
        form.addRow(tr("Operator"), self.trigger_operator)
        form.addRow(tr("Value"), self.trigger_value)
        form.addRow(tr("Schedule"), self.trigger_schedule)
        form.addRow(self.trigger_snapshot_hint)
        self._expand_fields(
            self.trigger_mode,
            self.trigger_device,
            self.trigger_metric,
            self.trigger_operator,
            self.trigger_value,
            self.trigger_schedule,
        )
        self._update_trigger_editor_mode()
        return box

    def _build_condition_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.condition_device = QComboBox()
        self.condition_metric = QComboBox()
        self.condition_operator = QComboBox()
        for op in (">=", ">", "<=", "<", "==", "!="):
            self.condition_operator.addItem(op, op)
        self.condition_value = QDoubleSpinBox()
        self.condition_value.setRange(-1_000_000.0, 1_000_000.0)
        self.condition_value.setDecimals(3)
        form.addRow(tr("Device"), self.condition_device)
        form.addRow(tr("Metric"), self.condition_metric)
        form.addRow(tr("Operator"), self.condition_operator)
        form.addRow(tr("Value"), self.condition_value)
        self._expand_fields(
            self.condition_device,
            self.condition_metric,
            self.condition_operator,
            self.condition_value,
        )
        return box

    def _build_gate_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.gate_mode = QComboBox()
        self.gate_mode.addItem(tr("All conditions"), "and")
        self.gate_mode.addItem(tr("Any condition"), "or")
        self.gate_hint = QLabel(tr("Drag condition or logical blocks into the logical container on canvas."))
        self.gate_hint.setObjectName("SidebarMeta")
        self.gate_hint.setWordWrap(True)
        form.addRow(tr("Type"), self.gate_mode)
        form.addRow(self.gate_hint)
        self._expand_fields(self.gate_mode)
        return box

    def _build_delay_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.delay_seconds = QDoubleSpinBox()
        self.delay_seconds.setRange(0.0, 3600.0)
        self.delay_seconds.setDecimals(1)
        self.delay_seconds.setSuffix(" sec")
        form.addRow(tr("Delay"), self.delay_seconds)
        self._expand_fields(self.delay_seconds)
        return box

    def _build_action_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self._action_form = form
        self.action_device = QComboBox()
        self.action_state = QComboBox()
        self.action_state.addItem(tr("Turn ON"), True)
        self.action_state.addItem(tr("Turn OFF"), False)
        self.action_inverter_editor = self._build_action_inverter_editor()
        self.action_inverter_editor.setVisible(False)
        self.action_retries = QSpinBox()
        self.action_retries.setRange(0, 5)
        self.action_retry_delay = QDoubleSpinBox()
        self.action_retry_delay.setRange(0.0, 60.0)
        self.action_retry_delay.setDecimals(1)
        self.action_retry_delay.setSuffix(" sec")
        form.addRow(tr("Device"), self.action_device)
        form.addRow(tr("Action"), self.action_state)
        form.addRow(self.action_inverter_editor)
        form.addRow(tr("Retries"), self.action_retries)
        form.addRow(tr("Retry delay"), self.action_retry_delay)
        self._expand_fields(
            self.action_device,
            self.action_state,
            self.action_retries,
            self.action_retry_delay,
        )
        self._update_action_editor_mode()
        return box

    def _build_action_inverter_editor(self) -> QWidget:
        panel = QWidget()
        root = QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.action_inverter_hint = QLabel(tr("Add inverter setting changes. Each field can be selected once."))
        self.action_inverter_hint.setObjectName("SidebarMeta")
        self.action_inverter_hint.setWordWrap(True)
        root.addWidget(self.action_inverter_hint)

        rows_holder = QWidget()
        rows_layout = QVBoxLayout(rows_holder)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(8)
        self.action_inverter_rows_layout = rows_layout
        root.addWidget(rows_holder)
        return panel

    def _connect_block_editors(self) -> None:
        for widget in (
            self.trigger_mode,
            self.trigger_device,
            self.trigger_metric,
            self.trigger_operator,
            self.trigger_value,
            self.trigger_schedule,
            self.condition_device,
            self.condition_metric,
            self.condition_operator,
            self.condition_value,
            self.gate_mode,
            self.delay_seconds,
            self.action_device,
            self.action_state,
            self.action_retries,
            self.action_retry_delay,
            self.io_input_count,
            self.io_output_count,
        ):
            if isinstance(widget, (QComboBox, QSpinBox, QDoubleSpinBox)):
                if isinstance(widget, QComboBox):
                    widget.currentIndexChanged.connect(lambda *_args: self._save_block_editor())
                else:
                    widget.valueChanged.connect(lambda *_args: self._save_block_editor())
        self.trigger_device.currentIndexChanged.connect(lambda *_args: self._refresh_trigger_metric_options())
        self.condition_device.currentIndexChanged.connect(lambda *_args: self._refresh_condition_metric_options())
        self.trigger_metric.currentIndexChanged.connect(lambda *_args: self._update_trigger_value_unit_suffix())
        self.condition_metric.currentIndexChanged.connect(lambda *_args: self._update_condition_value_unit_suffix())
        self.action_device.currentIndexChanged.connect(lambda *_args: self._update_action_editor_mode())
        self.trigger_mode.currentIndexChanged.connect(lambda *_args: self._update_trigger_editor_mode())

    def _add_rule(self) -> None:
        rule = AutomationRule(
            rule_id=uuid.uuid4().hex,
            name=tr("Automation {index}").format(index=len(self._rules) + 1),
            enabled=True,
            active=False,
            flow_blocks=[
                {"type": "start", "output_count": 1},
                {"type": "trigger", "input_count": 1, "output_count": 1},
                {"type": "action", "input_count": 1, "output_count": 1},
                {"type": "end", "input_count": 1},
            ],
        )
        self._rules.append(rule)
        # Do not rebuild the whole rule list on every autosave - it resets
        # canvas selection to the first block ("Start") and steals focus.
        self._update_rule_row_selection_state()
        self.rules_changed.emit()

    def _header_action_button(self, symbol: str, tooltip: str, handler) -> QPushButton:
        button = QPushButton(symbol)
        button.setObjectName("AutomationHeaderIconButton")
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(24, 24)
        button.clicked.connect(handler)
        return button

    def _delete_rule(self, rule_id: str | None = None) -> None:
        row = self._row_for_rule_id(rule_id) if rule_id else self.rule_list.currentRow()
        if row < 0 or row >= len(self._rules):
            return
        rule_name = str(self._rules[row].name or "").strip() or tr("Rule")
        if not self._confirm_delete_rule(rule_name):
            return
        self._rules.pop(row)
        if not self._rules:
            self._add_rule()
            return
        self._refresh_rule_list(select_row=max(0, row - 1))
        self.rules_changed.emit()

    def _confirm_delete_rule(self, rule_name: str) -> bool:
        return ask_compact_confirmation(
            self,
            title=tr("Delete rule"),
            text=tr('Are you sure you want to delete rule "{name}"?').format(name=rule_name),
            accept_text=tr("Delete"),
            reject_text=tr("Cancel"),
            destructive=True,
        )

    def _duplicate_rule(self, rule_id: str | None = None) -> None:
        rule = self._rule_by_id(rule_id) if rule_id else self._current_rule()
        if rule is None:
            return
        clone = AutomationRule.from_dict(rule.to_dict())
        clone.rule_id = uuid.uuid4().hex
        clone.name = tr("{name} Copy").format(name=rule.name)
        clone.active = False
        clone.draft_version += 1
        self._rules.append(clone)
        self._refresh_rule_list(select_rule_id=clone.rule_id)
        self.rules_changed.emit()

    def _emit_manual_run(self, rule_id: str | None = None) -> None:
        rule = self._rule_by_id(rule_id) if rule_id else self._current_rule()
        if rule is None:
            return
        self.manual_run_requested.emit(rule.rule_id)

    def _emit_simulate(self, rule_id: str | None = None) -> None:
        rule = self._rule_by_id(rule_id) if rule_id else self._current_rule()
        if rule is None:
            return
        self.simulate_requested.emit(rule.rule_id)

    def _export_rule_json(self, rule_id: str | None = None) -> None:
        rule = self._rule_by_id(rule_id) if rule_id else self._current_rule()
        if rule is None:
            return
        safe_name = re.sub(r"[^\w\-]+", "_", str(rule.name or "").strip()) or "rule"
        default_name = f"{safe_name}.json"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Export rule to JSON"),
            default_name,
            tr("JSON Files (*.json)"),
        )
        if not file_path:
            return
        payload = rule.to_dict()
        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self,
                tr("Export error"),
                tr("Failed to export rule JSON:\n{error}").format(error=str(exc)),
            )

    def _import_rule_json(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Import rule from JSON"),
            "",
            tr("JSON Files (*.json)"),
        )
        if not file_path:
            return
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self,
                tr("Import error"),
                tr("Failed to read JSON file:\n{error}").format(error=str(exc)),
            )
            return

        imported: list[AutomationRule] = []
        try:
            if isinstance(payload, dict) and isinstance(payload.get("rules"), list):
                imported = deserialize_rules(payload.get("rules"))
            elif isinstance(payload, list):
                imported = deserialize_rules(payload)
            elif isinstance(payload, dict):
                imported = [AutomationRule.from_dict(payload)]
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self,
                tr("Import error"),
                tr("Invalid automation rule format:\n{error}").format(error=str(exc)),
            )
            return

        if not imported:
            QMessageBox.information(self, tr("Import"), tr("No rules found in JSON file."))
            return

        existing_ids = {rule.rule_id for rule in self._rules}
        imported_rules: list[AutomationRule] = []
        for rule in imported:
            rule_data = rule.to_dict()
            incoming = AutomationRule.from_dict(rule_data)
            if not str(incoming.rule_id).strip() or incoming.rule_id in existing_ids:
                incoming.rule_id = uuid.uuid4().hex
            existing_ids.add(incoming.rule_id)
            incoming.active = False
            imported_rules.append(incoming)

        self._rules.extend(imported_rules)
        self._refresh_rule_list(select_rule_id=imported_rules[0].rule_id)
        self.rules_changed.emit()

    def _toggle_rule_enabled(self, rule_id: str, enabled: bool) -> None:
        rule = self._rule_by_id(rule_id)
        if rule is None:
            return
        self._save_current_rule()
        rule.enabled = bool(enabled)
        rule.active = bool(enabled)
        if rule.active:
            rule.version = max(rule.version, rule.draft_version)
        rule.updated_at = _now_label()
        self._refresh_rule_list(select_rule_id=rule.rule_id)
        self.rules_changed.emit()

    def _add_block(self, block_type: str) -> None:
        normalized_type = str(block_type).strip().lower()
        if normalized_type == "end" and self._has_end_block_in_canvas():
            self._update_palette_block_availability()
            return

        block: dict[str, object]
        if normalized_type == "start":
            block = {"type": "start", "output_count": 1}
        elif normalized_type == "trigger":
            block = {"type": "trigger", "trigger_type": "measurement", "operator": ">=", "value": 0.0, "input_count": 1, "output_count": 1}
        elif normalized_type == "condition":
            block = {"type": "condition", "operator": ">=", "value": 0.0, "input_count": 1}
        elif normalized_type == "gate":
            block = {"type": "gate", "mode": "and", "input_count": 1}
        elif normalized_type == "delay":
            block = {"type": "delay", "seconds": 1.0, "input_count": 1, "output_count": 1}
        elif normalized_type == "end":
            block = {"type": "end", "input_count": 1}
        else:
            block = {"type": "action", "action_type": "power", "value": True, "retries": 0, "retry_delay_sec": 1.0, "input_count": 1, "output_count": 1}
        self._insert_block_after_selection(block)
        self._save_current_rule()

    def _end_block_row(self) -> int:
        for idx in range(self.canvas.count()):
            item = self.canvas.item(idx)
            if item is None:
                continue
            block = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(block, dict) and str(block.get("type", "")).strip().lower() == "end":
                return idx
        return -1

    def _insert_block_after_selection(self, block: dict[str, object]) -> None:
        self._normalize_block_ports(block)
        self._ensure_block_id(block)
        selected_row = self.diagram_view.selected_index()
        if selected_row < 0:
            selected_row = self.canvas.currentRow()
        end_row = self._end_block_row()
        insert_row = self.canvas.count() if end_row < 0 else end_row
        if 0 <= selected_row < self.canvas.count():
            selected_item = self.canvas.item(selected_row)
            selected_block = selected_item.data(Qt.ItemDataRole.UserRole) if selected_item is not None else None
            selected_type = str(selected_block.get("type", "")).strip().lower() if isinstance(selected_block, dict) else ""
            if selected_type == "end":
                insert_row = selected_row
            else:
                insert_row = selected_row + 1
                if end_row >= 0:
                    insert_row = min(insert_row, end_row)
        insert_row = max(0, min(insert_row, self.canvas.count()))
        item = QListWidgetItem(self._block_label(block))
        item.setData(Qt.ItemDataRole.UserRole, dict(block))
        self.canvas.insertItem(insert_row, item)
        self.canvas.setCurrentRow(insert_row)
        self._sync_diagram_from_canvas()

    def _has_end_block_in_canvas(self) -> bool:
        for idx in range(self.canvas.count()):
            item = self.canvas.item(idx)
            if item is None:
                continue
            block = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")).strip().lower() == "end":
                return True
        return False

    def _update_palette_block_availability(self) -> None:
        end_tile = self._palette_tile_by_type.get("end")
        if end_tile is None:
            return
        can_add_end = not self._has_end_block_in_canvas()
        end_tile.setEnabled(can_add_end)
        end_tile.setCursor(Qt.CursorShape.OpenHandCursor if can_add_end else Qt.CursorShape.ForbiddenCursor)

    def _try_add_palette_block_to_active_target(self, block_type: str) -> bool:
        normalized_type = str(block_type).strip().lower()
        if normalized_type not in {"condition", "gate"}:
            return False

        # Keep palette click adding to top-level by default.
        # Nested insertion is allowed only when a nested gate row is actively selected.
        if self._selected_nested_condition is not None:
            gate_row, child_row = self._selected_nested_condition
            if 0 <= gate_row < self.canvas.count():
                gate_item = self.canvas.item(gate_row)
                gate_block = gate_item.data(Qt.ItemDataRole.UserRole) if gate_item is not None else None
                if (
                    isinstance(gate_block, dict)
                    and str(gate_block.get("type", "")).strip().lower() == "gate"
                ):
                    child_path = self._gate_child_path_from_flat_index(gate_block, child_row)
                    if child_path is not None:
                        selected_child = self._gate_child_at_path(gate_block, child_path)
                        if (
                            isinstance(selected_child, dict)
                            and str(selected_child.get("type", "")).strip().lower() == "gate"
                        ):
                            self._add_palette_block_into_nested_gate(normalized_type, gate_row, child_row)
                            return True
        return False

    def _remove_selected_block(self) -> None:
        row = self.diagram_view.selected_index()
        if row < 0:
            row = self.canvas.currentRow()
        self._remove_block_by_index(row)

    @staticmethod
    def _normalize_block_ports(block: dict[str, object]) -> None:
        def _normalize_ratio_array(key: str, count: int) -> None:
            raw = block.get(key, [])
            if not isinstance(raw, list):
                block.pop(key, None)
                return
            values = []
            for item in raw:
                if isinstance(item, (int, float)):
                    values.append(max(0.08, min(0.92, float(item))))
            if not values:
                block.pop(key, None)
                return
            values = sorted(values)[: max(1, count)]
            block[key] = values

        kind = str(block.get("type", "")).strip().lower()
        if kind == "start":
            block.pop("input_count", None)
            block["output_count"] = max(1, min(6, int(block.get("output_count", 1) or 1)))
            _normalize_ratio_array("ui_output_xs", int(block["output_count"]))
            block.pop("ui_input_xs", None)
            return
        if kind == "end":
            block["input_count"] = max(1, min(6, int(block.get("input_count", 1) or 1)))
            block.pop("output_count", None)
            _normalize_ratio_array("ui_input_xs", int(block["input_count"]))
            block.pop("ui_output_xs", None)
            return
        if kind in {"condition", "gate"}:
            block["input_count"] = max(1, min(6, int(block.get("input_count", 1) or 1)))
            block.pop("output_count", None)
            _normalize_ratio_array("ui_input_xs", int(block["input_count"]))
            block.pop("ui_output_xs", None)
            return
        block["input_count"] = max(1, min(6, int(block.get("input_count", 1) or 1)))
        block["output_count"] = max(1, min(6, int(block.get("output_count", 1) or 1)))
        _normalize_ratio_array("ui_input_xs", int(block["input_count"]))
        _normalize_ratio_array("ui_output_xs", int(block["output_count"]))

    def _append_block_item(self, block: dict[str, object]) -> None:
        self._normalize_block_ports(block)
        self._ensure_block_id(block)
        label = self._block_label(block)
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, block)
        self.canvas.addItem(item)
        self.canvas.setCurrentItem(item)
        self._sync_diagram_from_canvas()

    def _block_label(self, block: dict[str, object]) -> str:
        kind = str(block.get("type", "")).strip().lower()
        if kind == "start":
            return tr("Start")
        if kind == "end":
            return tr("End")
        if kind == "trigger":
            trigger_type = str(block.get("trigger_type", "measurement"))
            metric_key = str(block.get("metric_key", "")).strip()
            metric = self._metric_display_text(
                metric_key,
                include_code=bool(str(block.get("device_id", "")).strip()),
            ) or metric_key or "metric"
            operator = str(block.get("operator", ">="))
            value = float(block.get("value", 0.0) or 0.0)
            if trigger_type == "schedule":
                return tr("Trigger: Every {minutes} min").format(
                    minutes=int(block.get("schedule_every_minutes", 15) or 15)
                )
            if trigger_type == "snapshot_update":
                return tr("Trigger: Snapshot update")
            if trigger_type == "manual":
                return tr("Trigger: Manual")
            return tr("Trigger: {metric} {operator} {value}").format(
                metric=metric,
                operator=operator,
                value=f"{value:g}",
            )
        if kind == "condition":
            metric_key = str(block.get("metric_key", "")).strip()
            metric = self._metric_display_text(
                metric_key,
                include_code=bool(str(block.get("device_id", "")).strip()),
            ) or metric_key or "metric"
            operator = str(block.get("operator", ">="))
            value = float(block.get("value", 0.0) or 0.0)
            return tr("Condition: {metric} {operator} {value}").format(
                metric=metric,
                operator=operator,
                value=f"{value:g}",
            )
        if kind == "gate":
            mode = str(block.get("mode", "and")).strip().lower() or "and"
            mode_label = tr("All conditions") if mode == "and" else tr("Any condition")
            cond_count = len(self._gate_children(block))
            return tr("Gate: {mode} ({count})").format(mode=mode_label, count=cond_count)
        if kind == "delay":
            sec = float(block.get("seconds", 0.0) or 0.0)
            return tr("Delay: {seconds} sec").format(seconds=f"{sec:g}")
        if kind == "action":
            action_type = str(block.get("action_type", "power")).strip().lower() or "power"
            device_id = str(block.get("device_id", "")).strip()
            device_name = self._device_display_text(device_id)
            if action_type == "inverter_settings":
                changes = block.get("changes", block.get("value", []))
                count = len(changes) if isinstance(changes, list) else 0
                return tr("Action: {device} - Inverter settings ({count})").format(
                    device=device_name,
                    count=count,
                )
            state = tr("On") if bool(block.get("value", False)) else tr("Off")
            return tr("Action: {device} - {state}").format(device=device_name, state=state)
        return tr("Block")

    def _switch_rule(self, row: int) -> None:
        if self._syncing:
            return
        if row < 0 or row >= len(self._rules):
            self._active_rule_id = None
            self._update_rule_row_selection_state()
            self._apply_runtime_highlight()
            return
        rule = self._rules[row]
        self._active_rule_id = rule.rule_id
        self._update_rule_row_selection_state()
        self._populate_rule_details(rule)
        self._apply_runtime_highlight()

    def _refresh_rule_list(self, *, select_row: int | None = None, select_rule_id: str | None = None) -> None:
        self._syncing = True
        self._rule_row_widgets.clear()
        self.rule_list.clear()
        selected_rule: AutomationRule | None = None
        for rule in self._rules:
            item = QListWidgetItem("")
            item.setData(Qt.ItemDataRole.UserRole, rule.rule_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            state_label = tr("ACTIVE") if rule.active else tr("DRAFT")
            item.setToolTip(tr("State: {state}").format(state=state_label))
            self.rule_list.addItem(item)
            row_widget = RuleListRowWidget(rule.rule_id, rule.name, rule.enabled)
            row_widget.selected.connect(self._select_rule_by_id)
            row_widget.toggled.connect(self._toggle_rule_enabled)
            self._rule_row_widgets[rule.rule_id] = row_widget
            self.rule_list.setItemWidget(item, row_widget)
            item.setSizeHint(row_widget.sizeHint())
        target_row = 0
        if select_rule_id:
            for idx in range(self.rule_list.count()):
                item = self.rule_list.item(idx)
                if item and item.data(Qt.ItemDataRole.UserRole) == select_rule_id:
                    target_row = idx
                    break
        elif self._active_rule_id:
            for idx in range(self.rule_list.count()):
                item = self.rule_list.item(idx)
                if item and item.data(Qt.ItemDataRole.UserRole) == self._active_rule_id:
                    target_row = idx
                    break
        elif select_row is not None:
            target_row = max(0, min(select_row, self.rule_list.count() - 1))
        if self.rule_list.count() > 0:
            self.rule_list.setCurrentRow(target_row)
            current_item = self.rule_list.item(target_row)
            if current_item is not None:
                self._active_rule_id = str(current_item.data(Qt.ItemDataRole.UserRole) or "")
            selected_rule = self._current_rule()
        self._update_rule_row_selection_state()
        self._syncing = False
        # During initial/refresh selection currentRowChanged is ignored because
        # _syncing is True, so we must populate the selected rule explicitly.
        if selected_rule is not None:
            self._populate_rule_details(selected_rule)
        self._apply_runtime_highlight()

    def _populate_rule_details(self, rule: AutomationRule) -> None:
        self._syncing = True
        self._selected_nested_condition = None
        previous_selected_block_id = ""
        previous_selected_row = self.canvas.currentRow()
        if 0 <= previous_selected_row < self.canvas.count():
            previous_item = self.canvas.item(previous_selected_row)
            if previous_item is not None:
                previous_block = previous_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(previous_block, dict):
                    previous_selected_block_id = str(previous_block.get("_id", "")).strip()
        self.canvas.clear()
        flow_blocks = list(rule.flow_blocks)
        if not flow_blocks:
            flow_blocks = flow_blocks_from_graph(rule.flow_graph)
        normalized_blocks: list[dict[str, object]] = []
        start_added = False
        for block in flow_blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", "")).strip().lower()
            if block_type == "start":
                if start_added:
                    continue
                normalized_blocks.append(dict(block))
                start_added = True
                continue
            normalized_blocks.append(dict(block))
        if not start_added:
            normalized_blocks.insert(0, {"type": "start", "output_count": 1})
        elif normalized_blocks and str(normalized_blocks[0].get("type", "")).strip().lower() != "start":
            start_block = next(
                (item for item in normalized_blocks if str(item.get("type", "")).strip().lower() == "start"),
                {"type": "start", "output_count": 1},
            )
            normalized_blocks = [dict(start_block), *[item for item in normalized_blocks if str(item.get("type", "")).strip().lower() != "start"]]
        end_block = next(
            (dict(item) for item in normalized_blocks if str(item.get("type", "")).strip().lower() == "end"),
            None,
        )
        if end_block is not None:
            normalized_blocks = [item for item in normalized_blocks if str(item.get("type", "")).strip().lower() != "end"]
            normalized_blocks.append(end_block)
        else:
            normalized_blocks.append({"type": "end", "input_count": 1})
        flow_blocks = normalized_blocks
        for block in flow_blocks:
            self._append_block_item(dict(block))
        if self.canvas.count() > 0:
            restored_row = -1
            if previous_selected_block_id:
                for row in range(self.canvas.count()):
                    item = self.canvas.item(row)
                    if item is None:
                        continue
                    block = item.data(Qt.ItemDataRole.UserRole)
                    if not isinstance(block, dict):
                        continue
                    if str(block.get("_id", "")).strip() == previous_selected_block_id:
                        restored_row = row
                        break
            if restored_row < 0:
                restored_row = 0
            self.canvas.setCurrentRow(restored_row)
        else:
            self._set_empty_editor_message(tr("Select a block to edit."))
        self._sync_diagram_from_canvas()
        self._syncing = False
        self._apply_runtime_highlight()

    def _current_rule(self) -> AutomationRule | None:
        row = self.rule_list.currentRow()
        if row < 0 or row >= len(self._rules):
            return None
        return self._rules[row]

    def _rule_by_id(self, rule_id: str | None) -> AutomationRule | None:
        if not rule_id:
            return None
        normalized = str(rule_id).strip()
        for rule in self._rules:
            if rule.rule_id == normalized:
                return rule
        return None

    def _row_for_rule_id(self, rule_id: str | None) -> int:
        if not rule_id:
            return -1
        normalized = str(rule_id).strip()
        for idx, rule in enumerate(self._rules):
            if rule.rule_id == normalized:
                return idx
        return -1

    def _row_for_node_id(self, node_id: str | None) -> int:
        normalized = str(node_id or "").strip()
        if not normalized:
            return -1
        for row in range(self.canvas.count()):
            item = self.canvas.item(row)
            if item is None:
                continue
            block = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(block, dict):
                continue
            if str(block.get("_id", "")).strip() == normalized:
                return row
        return -1

    def _apply_runtime_highlight(self) -> None:
        active_rule_id = str(self._active_rule_id or "").strip()
        runtime_rule_id = str(self._runtime_rule_id or "").strip()
        if not active_rule_id or active_rule_id != runtime_rule_id:
            self.diagram_view.set_runtime_active_index(-1)
            self.diagram_view.set_runtime_active_branch(-1, "")
            return
        row = self._row_for_node_id(self._runtime_node_id)
        self.diagram_view.set_runtime_active_index(row)
        self.diagram_view.set_runtime_active_branch(row, str(self._runtime_branch or ""))

    def _select_rule_by_id(self, rule_id: str) -> None:
        row = self._row_for_rule_id(rule_id)
        if row < 0:
            return
        # Clicking the already-selected row does not emit currentRowChanged.
        # Keep visual selection and editor state in sync explicitly.
        if self.rule_list.currentRow() == row:
            self._active_rule_id = self._rules[row].rule_id
            self._update_rule_row_selection_state()
            self._populate_rule_details(self._rules[row])
            return
        self.rule_list.setCurrentRow(row)

    def _update_rule_row_selection_state(self) -> None:
        current_rule_id = str(self._active_rule_id or "").strip()
        if not current_rule_id:
            current_row = self.rule_list.currentRow()
            if 0 <= current_row < len(self._rules):
                current_rule_id = self._rules[current_row].rule_id
        for rule_id, row_widget in self._rule_row_widgets.items():
            row_widget.set_selected(rule_id == current_rule_id)

    def _show_selected_block_editor(self, row: int) -> None:
        self._selected_nested_condition = None
        if row < 0 or row >= self.canvas.count():
            self.selected_block_title.setText("")
            self.io_controls_widget.setVisible(False)
            self._set_empty_editor_message(tr("Select a block to edit."))
            self.block_editor.setCurrentIndex(0)
            return
        item = self.canvas.item(row)
        block = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(block, dict):
            self.selected_block_title.setText("")
            self.io_controls_widget.setVisible(False)
            self._set_empty_editor_message(tr("Select a block to edit."))
            self.block_editor.setCurrentIndex(0)
            return
        kind = str(block.get("type", "")).strip().lower()
        self.selected_block_title.setText(self._block_title_text(kind))
        self._load_io_controls_for_block(block)
        self._syncing = True
        if kind == "trigger":
            self.block_editor.setCurrentIndex(1)
            self._load_trigger_block(block)
        elif kind == "condition":
            self.block_editor.setCurrentIndex(2)
            self._load_condition_block(block)
        elif kind == "gate":
            self.block_editor.setCurrentIndex(3)
            self._load_gate_block(block)
        elif kind == "delay":
            self.block_editor.setCurrentIndex(4)
            self.delay_seconds.setValue(float(block.get("seconds", 0.0) or 0.0))
        elif kind == "action":
            self.block_editor.setCurrentIndex(5)
            self._load_action_block(block)
        else:
            self._set_empty_editor_message(tr("No editable fields for this block."))
            self.block_editor.setCurrentIndex(0)
        self._syncing = False

    def _block_title_text(self, kind: str) -> str:
        normalized = str(kind or "").strip().lower()
        if normalized == "start":
            return tr("Start")
        if normalized == "end":
            return tr("End")
        if normalized == "trigger":
            return tr("Trigger")
        if normalized == "condition":
            return tr("Condition")
        if normalized == "gate":
            return tr("Gate")
        if normalized == "delay":
            return tr("Delay")
        if normalized == "action":
            return tr("Action")
        return tr("Block")

    def _update_block_editor_height(self) -> None:
        current = self.block_editor.currentWidget()
        if current is None:
            return
        target = max(44, current.sizeHint().height())
        self.block_editor.setMinimumHeight(target)
        self.block_editor.setMaximumHeight(16_777_215)
        self.block_editor.updateGeometry()

    def _load_trigger_block(self, block: dict[str, object]) -> None:
        idx = self.trigger_mode.findData(str(block.get("trigger_type", "measurement")))
        self.trigger_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self._set_combo_by_data(self.trigger_device, block.get("device_id", ""))
        self._refresh_trigger_metric_options()
        self._set_combo_by_data(self.trigger_metric, block.get("metric_key", ""))
        self._set_combo_by_data(self.trigger_operator, block.get("operator", ">="))
        self._update_trigger_value_unit_suffix()
        self.trigger_value.setValue(float(block.get("value", 0.0) or 0.0))
        self.trigger_schedule.setValue(max(1, int(block.get("schedule_every_minutes", 15) or 15)))
        self._update_trigger_editor_mode()

    def _load_condition_block(self, block: dict[str, object]) -> None:
        self._set_combo_by_data(self.condition_device, block.get("device_id", ""))
        self._refresh_condition_metric_options()
        self._set_combo_by_data(self.condition_metric, block.get("metric_key", ""))
        self._set_combo_by_data(self.condition_operator, block.get("operator", ">="))
        self._update_condition_value_unit_suffix()
        self.condition_value.setValue(float(block.get("value", 0.0) or 0.0))

    def _load_action_block(self, block: dict[str, object]) -> None:
        self._set_combo_by_data(self.action_device, block.get("device_id", ""))
        action_type = str(block.get("action_type", "power")).strip().lower() or "power"
        if action_type == "inverter_settings":
            raw_changes = block.get("changes", block.get("value", []))
            parsed_changes = raw_changes if isinstance(raw_changes, list) else []
            self._load_inverter_action_rows(
                [item for item in parsed_changes if isinstance(item, dict)]
            )
        else:
            self._set_combo_by_data(self.action_state, bool(block.get("value", False)))
        self.action_retries.setValue(max(0, int(block.get("retries", 0) or 0)))
        self.action_retry_delay.setValue(float(block.get("retry_delay_sec", 1.0) or 1.0))
        self._update_action_editor_mode()

    def _load_gate_block(self, block: dict[str, object]) -> None:
        mode = str(block.get("mode", "and")).strip().lower() or "and"
        self._set_combo_by_data(self.gate_mode, mode)

    @staticmethod
    def _ensure_block_id(block: dict[str, object]) -> str:
        block_id = str(block.get("_id", "")).strip()
        if block_id:
            return block_id
        new_id = uuid.uuid4().hex
        block["_id"] = new_id
        return new_id

    @staticmethod
    def _gate_children(block: dict[str, object]) -> list[dict[str, object]]:
        raw = block.get("conditions", [])
        if not isinstance(raw, list):
            return []
        normalized: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            child_type = str(item.get("type", "")).strip().lower()
            if child_type not in {"condition", "gate"}:
                continue
            normalized.append(dict(item))
        return normalized

    def _gate_child_at(self, gate_block: dict[str, object], child_index: int) -> dict[str, object] | None:
        path = self._gate_child_path_from_flat_index(gate_block, child_index)
        if path is None:
            return None
        return self._gate_child_at_path(gate_block, path)

    def _set_gate_child_at(self, gate_block: dict[str, object], child_index: int, child_block: dict[str, object]) -> bool:
        path = self._gate_child_path_from_flat_index(gate_block, child_index)
        if path is None:
            return False
        return self._set_gate_child_at_path(gate_block, path, child_block)

    @classmethod
    def _gate_child_paths(cls, gate_block: dict[str, object], prefix: tuple[int, ...] = ()) -> list[tuple[int, ...]]:
        paths: list[tuple[int, ...]] = []
        children = cls._gate_children(gate_block)
        for idx, child in enumerate(children):
            path = prefix + (idx,)
            paths.append(path)
            if str(child.get("type", "")).strip().lower() == "gate":
                paths.extend(cls._gate_child_paths(child, path))
        return paths

    @classmethod
    def _gate_child_path_from_flat_index(cls, gate_block: dict[str, object], child_index: int) -> tuple[int, ...] | None:
        paths = cls._gate_child_paths(gate_block)
        if child_index < 0 or child_index >= len(paths):
            return None
        return paths[child_index]

    @classmethod
    def _gate_child_at_path(cls, gate_block: dict[str, object], path: tuple[int, ...]) -> dict[str, object] | None:
        current = dict(gate_block)
        for depth, idx in enumerate(path):
            children = cls._gate_children(current)
            if idx < 0 or idx >= len(children):
                return None
            child = dict(children[idx])
            if depth == len(path) - 1:
                return child
            if str(child.get("type", "")).strip().lower() != "gate":
                return None
            current = child
        return None

    @classmethod
    def _set_gate_child_at_path(cls, gate_block: dict[str, object], path: tuple[int, ...], child_block: dict[str, object]) -> bool:
        if not path:
            return False
        children = cls._gate_children(gate_block)
        index = path[0]
        if index < 0 or index >= len(children):
            return False
        if len(path) == 1:
            children[index] = dict(child_block)
            gate_block["conditions"] = children
            return True
        parent_child = dict(children[index])
        if str(parent_child.get("type", "")).strip().lower() != "gate":
            return False
        if not cls._set_gate_child_at_path(parent_child, path[1:], child_block):
            return False
        children[index] = parent_child
        gate_block["conditions"] = children
        return True

    @classmethod
    def _pop_gate_child_at_path(cls, gate_block: dict[str, object], path: tuple[int, ...]) -> dict[str, object] | None:
        if not path:
            return None
        children = cls._gate_children(gate_block)
        index = path[0]
        if index < 0 or index >= len(children):
            return None
        if len(path) == 1:
            child = dict(children.pop(index))
            gate_block["conditions"] = children
            return child
        parent_child = dict(children[index])
        if str(parent_child.get("type", "")).strip().lower() != "gate":
            return None
        removed = cls._pop_gate_child_at_path(parent_child, path[1:])
        if removed is None:
            return None
        children[index] = parent_child
        gate_block["conditions"] = children
        return removed

    @classmethod
    def _append_child_to_gate_path(cls, gate_block: dict[str, object], gate_path: tuple[int, ...], child_block: dict[str, object]) -> bool:
        target_gate = cls._gate_child_at_path(gate_block, gate_path)
        if not isinstance(target_gate, dict) or str(target_gate.get("type", "")).strip().lower() != "gate":
            return False
        nested_children = cls._gate_children(target_gate)
        nested_children.append(dict(child_block))
        target_gate["conditions"] = nested_children
        return cls._set_gate_child_at_path(gate_block, gate_path, target_gate)

    @classmethod
    def _insert_child_into_gate_path(
        cls,
        gate_block: dict[str, object],
        gate_path: tuple[int, ...],
        insert_index: int,
        child_block: dict[str, object],
    ) -> bool:
        if gate_path:
            target_gate = cls._gate_child_at_path(gate_block, gate_path)
            if not isinstance(target_gate, dict) or str(target_gate.get("type", "")).strip().lower() != "gate":
                return False
            children = cls._gate_children(target_gate)
            bounded_index = max(0, min(insert_index, len(children)))
            children.insert(bounded_index, dict(child_block))
            target_gate["conditions"] = children
            return cls._set_gate_child_at_path(gate_block, gate_path, target_gate)
        children = cls._gate_children(gate_block)
        bounded_index = max(0, min(insert_index, len(children)))
        children.insert(bounded_index, dict(child_block))
        gate_block["conditions"] = children
        return True

    def _save_block_editor(self) -> None:
        if self._syncing:
            return
        if self._selected_nested_condition is not None:
            gate_row, child_row = self._selected_nested_condition
            if gate_row < 0 or gate_row >= self.canvas.count():
                return
            gate_item = self.canvas.item(gate_row)
            if gate_item is None:
                return
            gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
                return
            child_block = self._gate_child_at(gate_block, child_row)
            if not isinstance(child_block, dict):
                return
            child_type = str(child_block.get("type", "")).strip().lower()
            if child_type == "condition":
                child_block["device_id"] = self.condition_device.currentData() or ""
                child_block["metric_key"] = self.condition_metric.currentData() or ""
                child_block["operator"] = self.condition_operator.currentData() or ">="
                child_block["value"] = float(self.condition_value.value())
                child_block.pop("true_target_id", None)
                child_block.pop("false_target_id", None)
                child_block.pop("next_target_id", None)
                child_block.pop("true_target_ids", None)
                child_block.pop("false_target_ids", None)
                child_block.pop("next_target_ids", None)
            elif child_type == "gate":
                child_block["mode"] = str(self.gate_mode.currentData() or "and")
                child_block.pop("true_target_id", None)
                child_block.pop("false_target_id", None)
                child_block.pop("next_target_id", None)
                child_block.pop("true_target_ids", None)
                child_block.pop("false_target_ids", None)
                child_block.pop("next_target_ids", None)
            else:
                return
            if not self._set_gate_child_at(gate_block, child_row, child_block):
                return
            gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
            gate_item.setText(self._block_label(gate_block))
            self.canvas.setCurrentRow(gate_row)
            self._sync_diagram_from_canvas()
            self._save_current_rule()
            return
        row = self.canvas.currentRow()
        if row < 0 or row >= self.canvas.count():
            return
        item = self.canvas.item(row)
        block = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(block, dict):
            return
        kind = str(block.get("type", "")).strip().lower()
        if kind == "trigger":
            block["trigger_type"] = self.trigger_mode.currentData()
            block["device_id"] = self.trigger_device.currentData() or ""
            block["metric_key"] = self.trigger_metric.currentData() or ""
            block["operator"] = self.trigger_operator.currentData() or ">="
            block["value"] = float(self.trigger_value.value())
            block["schedule_every_minutes"] = int(self.trigger_schedule.value())
        elif kind == "condition":
            block["device_id"] = self.condition_device.currentData() or ""
            block["metric_key"] = self.condition_metric.currentData() or ""
            block["operator"] = self.condition_operator.currentData() or ">="
            block["value"] = float(self.condition_value.value())
            block.pop("next_target_id", None)
            block.pop("next_target_ids", None)
        elif kind == "gate":
            block["mode"] = str(self.gate_mode.currentData() or "and")
            block.pop("next_target_id", None)
            block.pop("next_target_ids", None)
        elif kind == "delay":
            block["seconds"] = float(self.delay_seconds.value())
        elif kind == "action":
            block["device_id"] = self.action_device.currentData() or ""
            if str(block.get("device_id", "")).strip() == INVERTER_DEVICE_ID:
                block["action_type"] = "inverter_settings"
                block["changes"] = self._collect_inverter_action_changes()
                block.pop("value", None)
            else:
                block["action_type"] = "power"
                block["value"] = bool(self.action_state.currentData())
                block.pop("changes", None)
            block["retries"] = int(self.action_retries.value())
            block["retry_delay_sec"] = float(self.action_retry_delay.value())
        self._apply_io_controls_to_block(block)
        item.setData(Qt.ItemDataRole.UserRole, block)
        item.setText(self._block_label(block))
        self._sync_diagram_from_canvas()
        self._save_current_rule()


    def _save_current_rule(self) -> None:
        if self._syncing:
            return
        rule = self._current_rule()
        if rule is None:
            return
        name = rule.name
        enabled = rule.enabled
        blocks: list[dict[str, object]] = []
        for idx in range(self.canvas.count()):
            item = self.canvas.item(idx)
            if item is None:
                continue
            block = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(block, dict):
                self._normalize_block_ports(block)
                self._ensure_block_id(block)
                if str(block.get("type", "")).strip().lower() not in {"condition", "gate"}:
                    block.pop("true_target_id", None)
                    block.pop("false_target_id", None)
                    block.pop("true_target_ids", None)
                    block.pop("false_target_ids", None)
                if str(block.get("type", "")).strip().lower() == "end":
                    block.pop("next_target_id", None)
                    block.pop("next_target_ids", None)
                item.setData(Qt.ItemDataRole.UserRole, block)
                blocks.append(dict(block))
        trigger = next((item for item in blocks if str(item.get("type", "")).strip().lower() == "trigger"), None)
        conditions = [
            item
            for item in blocks
            if str(item.get("type", "")).strip().lower() == "condition"
        ]
        for block in blocks:
            if str(block.get("type", "")).strip().lower() != "gate":
                continue
            conditions.extend(self._collect_conditions_from_gate(block))
        actions = [
            item
            for item in blocks
            if str(item.get("type", "")).strip().lower() == "action"
        ]
        delays = [
            float(item.get("seconds", 0.0) or 0.0)
            for item in blocks
            if str(item.get("type", "")).strip().lower() == "delay"
        ]

        rule.name = name
        rule.enabled = enabled
        gate_modes = self._collect_gate_modes(blocks)
        if "or" in gate_modes:
            rule.conditions_logic = "any"
        elif "and" in gate_modes:
            rule.conditions_logic = "all"
        else:
            rule.conditions_logic = "all"
        rule.cooldown_sec = int(self._global_rule_cooldown_sec)
        rule.flow_blocks = blocks
        expanded_for_graph = self._expand_blocks_for_graph(blocks)
        rule.flow_graph = build_flow_graph_from_blocks(expanded_for_graph)
        rule.delay_before_actions_sec = float(sum(delays))
        rule.draft_version = max(1, int(rule.draft_version) + 1)
        rule.updated_at = _now_label()

        if isinstance(trigger, dict):
            rule.trigger_type = str(trigger.get("trigger_type", "measurement"))
            rule.trigger_device_id = str(trigger.get("device_id", ""))
            rule.trigger_metric_key = str(trigger.get("metric_key", ""))
            rule.trigger_operator = str(trigger.get("operator", ">="))
            rule.trigger_value = float(trigger.get("value", 0.0) or 0.0)
            rule.schedule_every_minutes = max(1, int(trigger.get("schedule_every_minutes", 15) or 15))

        parsed_conditions: list[AutomationCondition] = []
        for block in conditions:
            parsed_conditions.append(
                AutomationCondition(
                    metric_key=str(block.get("metric_key", "")),
                    operator=str(block.get("operator", ">=")),
                    value=float(block.get("value", 0.0) or 0.0),
                    device_id=str(block.get("device_id", "")),
                )
            )
        rule.conditions = parsed_conditions

        parsed_actions: list[AutomationAction] = []
        for block in actions:
            action_type = str(block.get("action_type", "power")) or "power"
            raw_value: object
            if action_type.strip().lower() == "inverter_settings":
                raw_changes = block.get("changes", [])
                raw_value = [dict(item) for item in raw_changes] if isinstance(raw_changes, list) else []
            else:
                raw_value = bool(block.get("value", False))
            parsed_actions.append(
                AutomationAction(
                    action_type=action_type,
                    device_id=str(block.get("device_id", "")),
                    value=raw_value,
                    retries=max(0, int(block.get("retries", 0) or 0)),
                    retry_delay_sec=max(0.0, float(block.get("retry_delay_sec", 1.0) or 1.0)),
                )
            )
        rule.actions = parsed_actions

        # Keep current canvas focus/selection while autosaving.
        # Full rule-list refresh repopulates canvas and jumps to row 0 ("Start").
        self._update_rule_row_selection_state()
        self.rules_changed.emit()

    def _set_empty_editor_message(self, text: str) -> None:
        if hasattr(self, "empty_editor_hint") and isinstance(self.empty_editor_hint, QLabel):
            self.empty_editor_hint.setText(str(text))

    @staticmethod
    def _expand_gate_children_for_graph(gate_block: dict[str, object]) -> list[dict[str, object]]:
        expanded: list[dict[str, object]] = []
        for child in AutomationTab._gate_children(gate_block):
            child_type = str(child.get("type", "")).strip().lower()
            if child_type == "gate":
                expanded.extend(AutomationTab._expand_gate_children_for_graph(child))
                gate_only = dict(child)
                gate_only.pop("conditions", None)
                expanded.append(gate_only)
            elif child_type == "condition":
                expanded.append(dict(child))
        return expanded

    @staticmethod
    def _expand_blocks_for_graph(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
        expanded: list[dict[str, object]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", "")).strip().lower()
            if kind != "gate":
                expanded.append(dict(block))
                continue
            expanded.extend(AutomationTab._expand_gate_children_for_graph(block))
            gate_only = dict(block)
            gate_only.pop("conditions", None)
            expanded.append(gate_only)
        return expanded

    @staticmethod
    def _collect_conditions_from_gate(gate_block: dict[str, object]) -> list[dict[str, object]]:
        collected: list[dict[str, object]] = []
        for child in AutomationTab._gate_children(gate_block):
            child_type = str(child.get("type", "")).strip().lower()
            if child_type == "condition":
                collected.append(dict(child))
            elif child_type == "gate":
                collected.extend(AutomationTab._collect_conditions_from_gate(child))
        return collected

    @staticmethod
    def _collect_gate_modes(blocks: list[dict[str, object]]) -> list[str]:
        modes: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")).strip().lower() == "gate":
                modes.append(str(block.get("mode", "")).strip().lower())
                nested = AutomationTab._gate_children(block)
                if nested:
                    modes.extend(AutomationTab._collect_gate_modes(nested))
        return modes

    @staticmethod
    def _set_combo_by_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
            return
        if combo.count() > 0:
            combo.setCurrentIndex(0)

    @staticmethod
    def _expand_fields(*widgets: QWidget) -> None:
        for widget in widgets:
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @staticmethod
    def _ensure_combo_popup_width(combo: QComboBox) -> None:
        if combo.count() <= 0:
            return
        view = combo.view()
        if view is None:
            return
        metrics = QFontMetrics(combo.font())
        longest = 0
        for index in range(combo.count()):
            text = combo.itemText(index)
            if text:
                longest = max(longest, metrics.horizontalAdvance(text))
        padding = combo.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent) + 40
        view.setMinimumWidth(max(combo.width(), longest + padding))

    def _refresh_trigger_metric_options(self) -> None:
        self._refresh_metric_combo_for(self.trigger_metric, self.trigger_device)
        self._update_trigger_value_unit_suffix()

    def _refresh_condition_metric_options(self) -> None:
        self._refresh_metric_combo_for(self.condition_metric, self.condition_device)
        self._update_condition_value_unit_suffix()

    def _refresh_metric_combo_for(self, metric_combo: QComboBox, device_combo: QComboBox) -> None:
        device_id = str(device_combo.currentData() or "").strip()
        keys = self._measurement_keys_by_device.get(device_id)
        if keys is None:
            keys = self._measurement_keys_by_device.get("", self._measurement_keys)
        current = metric_combo.currentData()
        metric_combo.blockSignals(True)
        metric_combo.clear()
        for key in keys or []:
            metric_combo.addItem(
                self._metric_display_text(key, include_code=bool(device_id)),
                key,
            )
        if metric_combo.count() == 0 and self._measurement_keys:
            for key in self._measurement_keys:
                metric_combo.addItem(self._metric_display_text(key, include_code=False), key)
        index = metric_combo.findData(current)
        metric_combo.setCurrentIndex(index if index >= 0 and metric_combo.count() > 0 else 0)
        metric_combo.blockSignals(False)
        self._ensure_combo_popup_width(metric_combo)

    def _metric_display_text(self, metric_key: str, *, include_code: bool = False) -> str:
        key = str(metric_key or "").strip().lower()
        if not key:
            return ""
        predefined_labels: dict[str, str] = {
            "battery_active_power": tr("Battery power"),
            "grid_active_power": tr("Grid power"),
            "load_active_power": tr("Load power"),
            "pv_output_power": tr("PV output power"),
            "battery_soc": tr("Battery state of charge (SOC)"),
            "battery_current": tr("Battery current"),
            "battery_direction": tr("Battery direction"),
            "battery_power": tr("Battery power"),
            "battery_to_home_power": tr("Battery → Home power"),
            "battery_voltage": tr("Battery voltage"),
            "bt_battery_capacity": tr("Battery capacity"),
            "grid_charge_current": tr("Grid charge current"),
            "grid_direction": tr("Grid direction"),
            "grid_frequency": tr("Grid frequency"),
            "grid_power": tr("Grid power"),
            "grid_to_battery_power": tr("Grid → Battery power"),
            "grid_to_home_power": tr("Grid → Home power"),
            "grid_voltage": tr("Grid voltage"),
            "load_current": tr("Load current"),
            "load_direction": tr("Load direction"),
            "load_frequency": tr("Load frequency"),
            "load_power": tr("Load power"),
            "load_voltage": tr("Load voltage"),
            "pv_charge_current": tr("PV charge current"),
            "pv_current": tr("PV current"),
            "pv_direction": tr("PV direction"),
            "pv_power": tr("PV power"),
            "pv_to_battery_power": tr("PV → Battery power"),
            "pv_to_home_current": tr("PV → Home current"),
            "pv_to_home_power": tr("PV → Home power"),
            "pv_voltage": tr("PV voltage"),
            "cur_power": tr("Current power"),
            "cur_current": tr("Current"),
            "energy": tr("Energy"),
            "power": tr("Power"),
            "voltage": tr("Voltage"),
            "frequency": tr("Frequency"),
            "current": tr("Current"),
            "switch": tr("Switch"),
            "switch_1": tr("Switch 1"),
            "switch_2": tr("Switch 2"),
            "countdown_1": tr("Countdown 1"),
            "countdown_2": tr("Countdown 2"),
            "add_ele": tr("Additional energy"),
            "door_time": tr("Door open time"),
            "closed_opened": tr("Door state"),
            "doorcontact_state": tr("Door contact state"),
        }
        label = predefined_labels.get(key, "")
        if not label:
            token_map = {
                "battery": tr("Battery"),
                "grid": tr("Grid"),
                "load": tr("Load"),
                "pv": tr("PV"),
                "active": tr("Active"),
                "output": tr("Output"),
                "power": tr("Power"),
                "soc": tr("SOC"),
                "current": tr("Current"),
                "voltage": tr("Voltage"),
                "frequency": tr("Frequency"),
                "energy": tr("Energy"),
                "cur": tr("Current"),
                "switch": tr("Switch"),
                "countdown": tr("Countdown"),
                "door": tr("Door"),
                "time": tr("Time"),
                "opened": tr("Opened"),
                "closed": tr("Closed"),
                "state": tr("State"),
                "contact": tr("Contact"),
                "add": tr("Additional"),
                "ele": tr("Energy"),
            }
            words = []
            for chunk in key.split("_"):
                if chunk.isdigit():
                    words.append(chunk)
                else:
                    words.append(token_map.get(chunk, chunk))
            label = " ".join(words).strip()
        text = label or key
        if include_code and text:
            if text.strip().lower() == key:
                return key
            return f"{text} ({key})"
        return text

    def _device_display_text(self, device_id: str) -> str:
        normalized = str(device_id or "").strip()
        if not normalized:
            return tr("Device")
        if normalized == INVERTER_DEVICE_ID:
            return tr("Inverter")
        for key, title in self._device_options:
            if str(key).strip() == normalized:
                return str(title).strip() or normalized
        return normalized

    @staticmethod
    def _set_form_row_visible(form: QFormLayout, field: QWidget, visible: bool) -> None:
        label = form.labelForField(field)
        if label is not None:
            label.setVisible(visible)
        field.setVisible(visible)

    def _update_trigger_editor_mode(self) -> None:
        form = getattr(self, "_trigger_form", None)
        if form is None:
            return
        trigger_type = str(self.trigger_mode.currentData() or "measurement").strip().lower()
        is_measurement = trigger_type == "measurement"
        is_schedule = trigger_type == "schedule"
        is_snapshot = trigger_type == "snapshot_update"
        self._set_form_row_visible(form, self.trigger_device, is_measurement)
        self._set_form_row_visible(form, self.trigger_metric, is_measurement)
        self._set_form_row_visible(form, self.trigger_operator, is_measurement)
        self._set_form_row_visible(form, self.trigger_value, is_measurement)
        self._set_form_row_visible(form, self.trigger_schedule, is_schedule)
        self.trigger_snapshot_hint.setVisible(is_snapshot)

    def _update_trigger_value_unit_suffix(self) -> None:
        metric_key = str(self.trigger_metric.currentData() or "").strip().lower()
        self.trigger_value.setSuffix(self._metric_value_suffix(metric_key))

    def _update_condition_value_unit_suffix(self) -> None:
        metric_key = str(self.condition_metric.currentData() or "").strip().lower()
        self.condition_value.setSuffix(self._metric_value_suffix(metric_key))

    def _metric_value_suffix(self, metric_key: str) -> str:
        key = str(metric_key or "").strip().lower()
        if not key:
            return ""
        unit_map: dict[str, str] = {
            "battery_soc": tr("%"),
            "bt_battery_capacity": tr("Ah"),
            "battery_voltage": tr("V"),
            "grid_voltage": tr("V"),
            "load_voltage": tr("V"),
            "pv_voltage": tr("V"),
            "battery_current": tr("A"),
            "grid_charge_current": tr("A"),
            "load_current": tr("A"),
            "pv_charge_current": tr("A"),
            "pv_current": tr("A"),
            "pv_to_home_current": tr("A"),
            "grid_frequency": tr("Hz"),
            "load_frequency": tr("Hz"),
            "battery_active_power": tr("W"),
            "battery_power": tr("W"),
            "battery_to_home_power": tr("W"),
            "grid_active_power": tr("W"),
            "grid_power": tr("W"),
            "grid_to_battery_power": tr("W"),
            "grid_to_home_power": tr("W"),
            "load_active_power": tr("W"),
            "load_power": tr("W"),
            "pv_output_power": tr("W"),
            "pv_power": tr("W"),
            "pv_to_battery_power": tr("W"),
            "pv_to_home_power": tr("W"),
            "cur_power": tr("W"),
            "power": tr("W"),
            "voltage": tr("V"),
            "current": tr("A"),
            "cur_current": tr("A"),
            "frequency": tr("Hz"),
            "energy": tr("kWh"),
            "add_ele": tr("kWh"),
        }
        unit = unit_map.get(key, "")
        if not unit:
            return ""
        return f" {unit}"

    def _sync_diagram_from_canvas(self) -> None:
        blocks: list[dict[str, object]] = []
        labels: list[str] = []
        for idx in range(self.canvas.count()):
            item = self.canvas.item(idx)
            if item is None:
                continue
            block = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(block, dict):
                continue
            blocks.append(dict(block))
            labels.append(self._block_subtitle(block))
        self.diagram_view.set_blocks(blocks, labels)
        self._sync_diagram_geometry()
        selected_row = self.canvas.currentRow()
        self.diagram_view.set_selected_index(selected_row)
        if self._selected_nested_condition is not None:
            gate_row, condition_row = self._selected_nested_condition
            if gate_row == selected_row:
                self.diagram_view.set_selected_gate_condition(gate_row, condition_row)
        self._apply_runtime_highlight()
        self._update_palette_block_availability()

    def _block_subtitle(self, block: dict[str, object]) -> str:
        kind = str(block.get("type", "")).strip().lower()
        if kind in {"start", "end"}:
            return ""
        if kind == "trigger":
            trigger_type = str(block.get("trigger_type", "measurement"))
            if trigger_type == "schedule":
                return tr("Every {minutes} min").format(
                    minutes=int(block.get("schedule_every_minutes", 15) or 15)
                )
            if trigger_type == "snapshot_update":
                return tr("Snapshot update")
            if trigger_type == "manual":
                return tr("Manual")
            metric_key = str(block.get("metric_key", "")).strip()
            metric = self._metric_display_text(
                metric_key,
                include_code=bool(str(block.get("device_id", "")).strip()),
            ) or metric_key or "metric"
            operator = str(block.get("operator", ">="))
            value = float(block.get("value", 0.0) or 0.0)
            return f"{metric} {operator} {value:g}"
        if kind == "condition":
            metric_key = str(block.get("metric_key", "")).strip()
            metric = self._metric_display_text(
                metric_key,
                include_code=bool(str(block.get("device_id", "")).strip()),
            ) or metric_key or "metric"
            operator = str(block.get("operator", ">="))
            value = float(block.get("value", 0.0) or 0.0)
            return f"{metric} {operator} {value:g}"
        if kind == "gate":
            mode = str(block.get("mode", "and")).strip().lower() or "and"
            mode_label = tr("All conditions") if mode == "and" else tr("Any condition")
            cond_count = len(self._gate_children(block))
            return f"{mode_label} ({cond_count})"
        if kind == "delay":
            sec = float(block.get("seconds", 0.0) or 0.0)
            return tr("{seconds} sec").format(seconds=f"{sec:g}")
        if kind == "action":
            action_type = str(block.get("action_type", "power")).strip().lower() or "power"
            device_id = str(block.get("device_id", "")).strip()
            device_name = self._device_display_text(device_id)
            if action_type == "inverter_settings":
                changes = block.get("changes", block.get("value", []))
                count = len(changes) if isinstance(changes, list) else 0
                return tr("{device} - Inverter settings ({count})").format(device=device_name, count=count)
            state = tr("On") if bool(block.get("value", False)) else tr("Off")
            return tr("{device} - {state}").format(device=device_name, state=state)
        return ""

    def _sync_diagram_selection(self, row: int) -> None:
        self.diagram_view.set_selected_index(row)
        if self._selected_nested_condition is not None:
            gate_row, condition_row = self._selected_nested_condition
            if gate_row == row:
                self.diagram_view.set_selected_gate_condition(gate_row, condition_row)
                self._apply_runtime_highlight()
                return
        self.diagram_view.set_selected_gate_condition(-1, -1)
        self._apply_runtime_highlight()

    def _select_block_from_diagram(self, row: int) -> None:
        if 0 <= row < self.canvas.count():
            self._selected_nested_condition = None
            self.diagram_view.set_selected_gate_condition(-1, -1)
            if self.canvas.currentRow() == row:
                self._show_selected_block_editor(row)
                self._sync_diagram_selection(row)
                return
            self.canvas.setCurrentRow(row)
            return
        self._selected_nested_condition = None
        self.canvas.setCurrentRow(-1)
        self.diagram_view.set_selected_index(-1)
        self.diagram_view.set_selected_gate_condition(-1, -1)
        self.selected_block_title.setText("")
        self.io_controls_widget.setVisible(False)
        self._set_empty_editor_message(tr("Select a block to edit."))
        self.block_editor.setCurrentIndex(0)
        self._apply_runtime_highlight()

    def _select_nested_condition_from_diagram(self, gate_row: int, condition_row: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        child = self._gate_child_at(gate_block, condition_row)
        if not isinstance(child, dict):
            return
        if self.canvas.currentRow() != gate_row:
            self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = (gate_row, condition_row)
        self.diagram_view.set_selected_gate_condition(gate_row, condition_row)
        self.io_controls_widget.setVisible(False)
        self._syncing = True
        child_type = str(child.get("type", "")).strip().lower()
        self.selected_block_title.setText(self._block_title_text(child_type))
        if child_type == "condition":
            self.block_editor.setCurrentIndex(2)
            self._load_condition_block(child)
        elif child_type == "gate":
            self.block_editor.setCurrentIndex(3)
            self._load_gate_block(child)
        self._syncing = False

    def _apply_diagram_reorder(self, blocks: list[dict[str, object]]) -> None:
        if self._syncing:
            return
        normalized_blocks = [dict(item) for item in blocks if isinstance(item, dict)]
        start_block = None
        end_block = None
        rest_blocks: list[dict[str, object]] = []
        for block in normalized_blocks:
            block_type = str(block.get("type", "")).strip().lower()
            if start_block is None and block_type == "start":
                start_block = block
                continue
            if end_block is None and block_type == "end":
                end_block = block
                continue
            rest_blocks.append(block)
        if start_block is None:
            start_block = {"type": "start", "output_count": 1}
        normalized_blocks = [start_block, *rest_blocks]
        if end_block is not None:
            normalized_blocks.append(end_block)
        self._syncing = True
        selected_row = self.diagram_view.selected_index()
        self.canvas.clear()
        for block in normalized_blocks:
            self._normalize_block_ports(block)
            item = QListWidgetItem(self._block_label(block))
            item.setData(Qt.ItemDataRole.UserRole, dict(block))
            self.canvas.addItem(item)
        if self.canvas.count() > 0:
            row = min(max(0, selected_row), self.canvas.count() - 1)
            self.canvas.setCurrentRow(row)
        self._syncing = False
        self._save_current_rule()

    def _remove_block_by_index(self, row: int) -> None:
        if row < 0 or row >= self.canvas.count():
            return
        item = self.canvas.item(row)
        if item is not None:
            block = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(block, dict) and str(block.get("type", "")).strip().lower() == "start":
                return
            block_name = self._block_title_text(str(block.get("type", "")).strip().lower()) if isinstance(block, dict) else tr("Block")
            if not ask_compact_confirmation(
                self,
                title=tr("Delete block"),
                text=tr('Are you sure you want to delete block "{name}"?').format(name=block_name),
                accept_text=tr("Delete"),
                reject_text=tr("Cancel"),
                destructive=True,
            ):
                return
        self._selected_nested_condition = None
        self.canvas.takeItem(row)
        if self.canvas.count() > 0:
            self.canvas.setCurrentRow(max(0, row - 1))
        else:
            self.block_editor.setCurrentIndex(0)
        self._save_current_rule()

    @staticmethod
    def _branch_keys(branch: str) -> tuple[str, str]:
        if branch == "next":
            return ("next_target_id", "next_target_ids")
        if branch == "true":
            return ("true_target_id", "true_target_ids")
        return ("false_target_id", "false_target_ids")

    def _get_branch_targets(self, block: dict[str, object], branch: str) -> list[str]:
        single_key, list_key = self._branch_keys(branch)
        targets: list[str] = []
        raw_list = block.get(list_key, [])
        if isinstance(raw_list, list):
            for value in raw_list:
                target_id = str(value).strip()
                if target_id and target_id not in targets:
                    targets.append(target_id)
        raw_single = str(block.get(single_key, "")).strip()
        if raw_single and raw_single not in targets:
            targets.append(raw_single)
        return targets

    def _set_branch_targets(self, block: dict[str, object], branch: str, targets: list[str]) -> None:
        single_key, list_key = self._branch_keys(branch)
        normalized = [str(value).strip() for value in targets if str(value).strip()]
        deduped: list[str] = []
        for value in normalized:
            if value not in deduped:
                deduped.append(value)
        if not deduped:
            block.pop(single_key, None)
            block.pop(list_key, None)
            return
        block[list_key] = deduped
        block[single_key] = deduped[0]

    def _target_has_opposite_conditional_branch(
        self,
        target_id: str,
        branch: str,
        source_row: int,
        target_socket_index: int,
    ) -> bool:
        opposite_branch = "false" if branch == "true" else "true"
        key = f"{opposite_branch}:{target_id}"
        for row in range(self.canvas.count()):
            item = self.canvas.item(row)
            if item is None:
                continue
            block = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(block, dict):
                continue
            source_type = str(block.get("type", "")).strip().lower()
            if source_type not in {"condition", "gate"}:
                continue
            if row == source_row:
                continue
            if target_id not in self._get_branch_targets(block, opposite_branch):
                continue
            target_ports = block.get("ui_edge_target_ports", {})
            if not isinstance(target_ports, dict):
                return True
            other_target_socket_index = max(0, int(target_ports.get(key, 0) or 0))
            if other_target_socket_index == max(0, int(target_socket_index)):
                return True
        return False

    def _set_branch_connection_from_diagram(
        self,
        source_row: int,
        branch: str,
        target_row: int,
        target_socket_index: int = 0,
        source_socket_index: int = 0,
    ) -> None:
        if source_row < 0 or target_row < 0:
            return
        if source_row >= self.canvas.count() or target_row >= self.canvas.count() or source_row == target_row:
            return
        source_item = self.canvas.item(source_row)
        target_item = self.canvas.item(target_row)
        if source_item is None or target_item is None:
            return
        source_block = source_item.data(Qt.ItemDataRole.UserRole)
        target_block = target_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(source_block, dict) or not isinstance(target_block, dict):
            return
        source_type = str(source_block.get("type", "")).strip().lower()
        if source_type == "end":
            return
        if branch not in {"true", "false", "next"}:
            return
        if source_type in {"condition", "gate"}:
            if branch not in {"true", "false"}:
                return
        else:
            if branch != "next":
                return
        target_id = self._ensure_block_id(target_block)
        self._ensure_block_id(source_block)
        if branch in {"true", "false"}:
            opposite_branch = "false" if branch == "true" else "true"
            if target_id in self._get_branch_targets(source_block, opposite_branch):
                key = f"{opposite_branch}:{target_id}"
                target_ports = source_block.get("ui_edge_target_ports", {})
                if not isinstance(target_ports, dict):
                    return
                other_target_socket_index = max(0, int(target_ports.get(key, 0) or 0))
                if other_target_socket_index == max(0, int(target_socket_index)):
                    return
            if self._target_has_opposite_conditional_branch(target_id, branch, source_row, target_socket_index):
                return
        targets = self._get_branch_targets(source_block, branch)
        if target_id not in targets:
            targets.append(target_id)
        self._set_branch_targets(source_block, branch, targets)
        edge_key = f"{str(branch).strip().lower()}:{target_id}"
        target_ports = source_block.get("ui_edge_target_ports", {})
        if not isinstance(target_ports, dict):
            target_ports = {}
        target_ports[edge_key] = max(0, int(target_socket_index))
        source_block["ui_edge_target_ports"] = target_ports
        source_ports = source_block.get("ui_edge_source_ports", {})
        if not isinstance(source_ports, dict):
            source_ports = {}
        source_ports[edge_key] = max(0, int(source_socket_index))
        source_block["ui_edge_source_ports"] = source_ports
        source_item.setData(Qt.ItemDataRole.UserRole, source_block)
        target_item.setData(Qt.ItemDataRole.UserRole, target_block)
        source_item.setText(self._block_label(source_block))
        self.canvas.setCurrentRow(source_row)
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _clear_branch_connection_from_diagram(self, source_row: int, branch: str, target_row: int) -> None:
        if source_row < 0 or source_row >= self.canvas.count():
            return
        source_item = self.canvas.item(source_row)
        if source_item is None:
            return
        source_block = source_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(source_block, dict):
            return
        source_type = str(source_block.get("type", "")).strip().lower()
        if source_type == "end":
            return
        if branch not in {"true", "false", "next"}:
            return
        if source_type in {"condition", "gate"} and branch not in {"true", "false"}:
            return
        if source_type not in {"condition", "gate"} and branch != "next":
            return
        target_id = ""
        if 0 <= target_row < self.canvas.count():
            target_item = self.canvas.item(target_row)
            if target_item is not None:
                target_block = target_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(target_block, dict):
                    target_id = self._ensure_block_id(target_block)
                    target_item.setData(Qt.ItemDataRole.UserRole, target_block)
        targets = self._get_branch_targets(source_block, branch)
        if not targets:
            return
        if target_id:
            targets = [value for value in targets if value != target_id]
        else:
            targets = []
        self._set_branch_targets(source_block, branch, targets)
        edge_target_ports = source_block.get("ui_edge_target_ports", {})
        if isinstance(edge_target_ports, dict):
            if target_id:
                edge_key = f"{str(branch).strip().lower()}:{target_id}"
                edge_target_ports.pop(edge_key, None)
            else:
                keys_to_remove = [key for key in edge_target_ports if str(key).startswith(f"{branch}:")]
                for key in keys_to_remove:
                    edge_target_ports.pop(key, None)
            source_block["ui_edge_target_ports"] = edge_target_ports
        edge_source_ports = source_block.get("ui_edge_source_ports", {})
        if isinstance(edge_source_ports, dict):
            if target_id:
                edge_key = f"{str(branch).strip().lower()}:{target_id}"
                edge_source_ports.pop(edge_key, None)
            else:
                keys_to_remove = [key for key in edge_source_ports if str(key).startswith(f"{branch}:")]
                for key in keys_to_remove:
                    edge_source_ports.pop(key, None)
            source_block["ui_edge_source_ports"] = edge_source_ports
        source_item.setData(Qt.ItemDataRole.UserRole, source_block)
        source_item.setText(self._block_label(source_block))
        self.canvas.setCurrentRow(source_row)
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _move_condition_into_gate_from_diagram(self, source_row: int, gate_row: int) -> None:
        if source_row < 0 or gate_row < 0 or source_row >= self.canvas.count() or gate_row >= self.canvas.count():
            return
        if source_row == gate_row:
            return
        source_item = self.canvas.item(source_row)
        gate_item = self.canvas.item(gate_row)
        if source_item is None or gate_item is None:
            return
        source_block = source_item.data(Qt.ItemDataRole.UserRole)
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(source_block, dict) or not isinstance(gate_block, dict):
            return
        source_type = str(source_block.get("type", "")).strip().lower()
        if source_type not in {"condition", "gate"}:
            return
        if str(gate_block.get("type", "")).strip().lower() != "gate":
            return

        child_block = dict(source_block)
        child_block["type"] = source_type
        normalized = self._gate_children(gate_block)
        normalized.append(child_block)
        gate_block["conditions"] = normalized
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))

        self.canvas.takeItem(source_row)
        updated_gate_row = gate_row - 1 if source_row < gate_row else gate_row
        if 0 <= updated_gate_row < self.canvas.count():
            self.canvas.setCurrentRow(updated_gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _move_condition_into_nested_gate_from_diagram(self, source_row: int, gate_row: int, child_row: int) -> None:
        if source_row < 0 or gate_row < 0 or source_row >= self.canvas.count() or gate_row >= self.canvas.count():
            return
        if source_row == gate_row:
            return
        source_item = self.canvas.item(source_row)
        gate_item = self.canvas.item(gate_row)
        if source_item is None or gate_item is None:
            return
        source_block = source_item.data(Qt.ItemDataRole.UserRole)
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(source_block, dict) or not isinstance(gate_block, dict):
            return
        source_type = str(source_block.get("type", "")).strip().lower()
        if source_type not in {"condition", "gate"}:
            return
        if str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        target_path = self._gate_child_path_from_flat_index(gate_block, child_row)
        if target_path is None:
            return
        target_child = self._gate_child_at_path(gate_block, target_path)
        if not isinstance(target_child, dict) or str(target_child.get("type", "")).strip().lower() != "gate":
            return
        if not self._append_child_to_gate_path(gate_block, target_path, dict(source_block)):
            return
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))

        self.canvas.takeItem(source_row)
        updated_gate_row = gate_row - 1 if source_row < gate_row else gate_row
        if 0 <= updated_gate_row < self.canvas.count():
            self.canvas.setCurrentRow(updated_gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _move_gate_child_into_nested_gate_in_diagram(self, gate_row: int, source_child_row: int, target_gate_child_row: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        source_path = self._gate_child_path_from_flat_index(gate_block, source_child_row)
        target_path = self._gate_child_path_from_flat_index(gate_block, target_gate_child_row)
        if source_path is None or target_path is None or source_path == target_path:
            return
        source_child = self._gate_child_at_path(gate_block, source_path)
        if not isinstance(source_child, dict):
            return
        source_type = str(source_child.get("type", "")).strip().lower()
        if source_type not in {"condition", "gate"}:
            return
        target_child = self._gate_child_at_path(gate_block, target_path)
        if not isinstance(target_child, dict) or str(target_child.get("type", "")).strip().lower() != "gate":
            return
        if source_type == "gate" and len(target_path) >= len(source_path) and target_path[: len(source_path)] == source_path:
            return

        removed = self._pop_gate_child_at_path(gate_block, source_path)
        if removed is None:
            return
        adjusted_target_path = target_path
        if len(source_path) == len(target_path) and source_path[:-1] == target_path[:-1] and source_path[-1] < target_path[-1]:
            adjusted_target_path = target_path[:-1] + (target_path[-1] - 1,)
        if not self._append_child_to_gate_path(gate_block, adjusted_target_path, removed):
            return
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _move_gate_child_into_parent_gate_in_diagram(self, gate_row: int, source_child_row: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        source_path = self._gate_child_path_from_flat_index(gate_block, source_child_row)
        if source_path is None:
            return
        # Already at parent level; no move needed.
        if len(source_path) <= 1:
            return
        moved_child = self._pop_gate_child_at_path(gate_block, source_path)
        if not isinstance(moved_child, dict):
            return
        parent_children = self._gate_children(gate_block)
        parent_children.append(moved_child)
        gate_block["conditions"] = parent_children
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _reorder_gate_child_in_diagram(self, gate_row: int, source_child_row: int, target_child_row: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        if source_child_row < 0 or target_child_row < 0 or source_child_row == target_child_row:
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        source_path = self._gate_child_path_from_flat_index(gate_block, source_child_row)
        target_path = self._gate_child_path_from_flat_index(gate_block, target_child_row)
        if source_path is None or target_path is None or source_path == target_path:
            return
        source_child = self._gate_child_at_path(gate_block, source_path)
        if not isinstance(source_child, dict):
            return
        source_type = str(source_child.get("type", "")).strip().lower()
        if source_type == "gate" and len(target_path) >= len(source_path) and target_path[: len(source_path)] == source_path:
            return

        moved_child = self._pop_gate_child_at_path(gate_block, source_path)
        if not isinstance(moved_child, dict):
            return

        target_parent_path = target_path[:-1]
        target_index = target_path[-1]
        if (
            len(source_path) == len(target_path)
            and source_path[:-1] == target_parent_path
            and source_path[-1] < target_index
        ):
            target_index -= 1

        if not self._insert_child_into_gate_path(gate_block, target_parent_path, target_index, moved_child):
            return

        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _remove_condition_from_gate_in_diagram(self, gate_row: int, condition_row: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        target_path = self._gate_child_path_from_flat_index(gate_block, condition_row)
        if target_path is None:
            return
        removed = self._pop_gate_child_at_path(gate_block, target_path)
        if removed is None:
            return
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _move_condition_within_gate_in_diagram(self, gate_row: int, condition_row: int, delta: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        source_path = self._gate_child_path_from_flat_index(gate_block, condition_row)
        if source_path is None:
            return
        if not source_path:
            return
        parent_path = source_path[:-1]
        source_idx = source_path[-1]
        if parent_path:
            parent_gate = self._gate_child_at_path(gate_block, parent_path)
            if not isinstance(parent_gate, dict) or str(parent_gate.get("type", "")).strip().lower() != "gate":
                return
        else:
            parent_gate = gate_block
        siblings = self._gate_children(parent_gate)
        if source_idx < 0 or source_idx >= len(siblings):
            return
        target_idx = source_idx + delta
        if target_idx < 0 or target_idx >= len(siblings):
            return
        siblings[source_idx], siblings[target_idx] = siblings[target_idx], siblings[source_idx]
        if parent_path:
            parent_gate["conditions"] = siblings
            if not self._set_gate_child_at_path(gate_block, parent_path, parent_gate):
                return
        else:
            gate_block["conditions"] = siblings
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        paths = self._gate_child_paths(gate_block)
        new_path = parent_path + (target_idx,)
        new_flat = next((idx for idx, path in enumerate(paths) if path == new_path), -1)
        self._selected_nested_condition = (gate_row, new_flat) if new_flat >= 0 else None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _add_palette_block_into_gate(self, block_type: str, gate_row: int) -> None:
        normalized_type = str(block_type).strip().lower()
        if normalized_type not in {"condition", "gate"}:
            self._add_block(block_type)
            return
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return

        if normalized_type == "gate":
            new_child: dict[str, object] = {"type": "gate", "mode": "and", "conditions": []}
        else:
            new_child = {"type": "condition", "operator": ">=", "value": 0.0}
        normalized = self._gate_children(gate_block)
        normalized.append(new_child)
        gate_block["conditions"] = normalized
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _add_palette_block_into_nested_gate(self, block_type: str, gate_row: int, child_row: int) -> None:
        normalized_type = str(block_type).strip().lower()
        if normalized_type not in {"condition", "gate"}:
            self._add_block(block_type)
            return
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        target_path = self._gate_child_path_from_flat_index(gate_block, child_row)
        if target_path is None:
            return
        target_child = self._gate_child_at_path(gate_block, target_path)
        if not isinstance(target_child, dict) or str(target_child.get("type", "")).strip().lower() != "gate":
            return
        if normalized_type == "gate":
            new_child: dict[str, object] = {"type": "gate", "mode": "and", "conditions": []}
        else:
            new_child = {"type": "condition", "operator": ">=", "value": 0.0}
        if not self._append_child_to_gate_path(gate_block, target_path, new_child):
            return
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _extract_condition_from_gate_in_diagram(self, gate_row: int, condition_row: int, insert_index: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        target_path = self._gate_child_path_from_flat_index(gate_block, condition_row)
        if target_path is None:
            return
        condition = self._pop_gate_child_at_path(gate_block, target_path)
        if not isinstance(condition, dict):
            return
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))

        target = max(0, min(insert_index, self.canvas.count()))
        item = QListWidgetItem(self._block_label(condition))
        item.setData(Qt.ItemDataRole.UserRole, condition)
        self.canvas.insertItem(target, item)
        self.canvas.setCurrentRow(target)
        self._selected_nested_condition = None
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _sync_diagram_geometry(self) -> None:
        target = max(220, int(self.diagram_view.content_height()))
        self.diagram_view.setMinimumHeight(target)
        self.diagram_view.updateGeometry()


def _now_label() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def analytics_from_logs(log_lines: list[str]) -> dict[str, dict[str, int]]:
    entries = []
    current_rule_id = ""
    rule_header_pattern = re.compile(r"\(([0-9a-fA-F]{8,64})\)")
    for line in log_lines:
        text = str(line).strip()
        if not text:
            continue
        level = "ok" if "[OK]" in text else "error" if "[ERROR]" in text else "info"
        rule_id = current_rule_id
        if "rule=" in text:
            tail = text.split("rule=", 1)[1]
            rule_id = tail.split(" ", 1)[0].strip()
            current_rule_id = rule_id or current_rule_id
        else:
            match = rule_header_pattern.search(text)
            if match is not None:
                rule_id = match.group(1).strip()
                current_rule_id = rule_id or current_rule_id
        entries.append({"rule_id": rule_id, "level": level})

    materialized = []
    from app.services.automations import AutomationLogEntry

    for entry in entries:
        materialized.append(
            AutomationLogEntry(
                timestamp="",
                level=entry["level"],
                rule_id=entry["rule_id"],
                rule_name="",
                message="",
            )
        )
    return build_rule_analytics(materialized)
