from __future__ import annotations

from dataclasses import replace
import uuid
from typing import Iterable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QPainter, QPen, QPolygonF
from PySide6.QtCore import QMimeData
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QScrollArea,
    QSizePolicy,
)

from app.services.automations import (
    AutomationAction,
    AutomationCondition,
    AutomationRule,
    build_flow_graph_from_blocks,
    build_rule_analytics,
    flow_blocks_from_graph,
)
from app.services.i18n import tr
from app.services.tuya_api import TuyaDevice

BLOCK_MIME = "application/x-energyflow-automation-block"


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
    blocks_reordered = Signal(list)
    block_delete_requested = Signal(int)
    condition_dropped_to_gate = Signal(int, int)
    gate_condition_delete_requested = Signal(int, int)
    gate_condition_move_requested = Signal(int, int, int)
    gate_condition_selected = Signal(int, int)
    gate_condition_extract_requested = Signal(int, int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._blocks: list[dict[str, object]] = []
        self._labels: list[str] = []
        self._selected_index = -1
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
        self._content_h = 220
        self._gate_condition_delete_hit_areas: list[tuple[int, int, QRectF]] = []
        self._gate_condition_up_hit_areas: list[tuple[int, int, QRectF]] = []
        self._gate_condition_down_hit_areas: list[tuple[int, int, QRectF]] = []
        self._gate_condition_row_hit_areas: list[tuple[int, int, QRectF]] = []
        self._gate_condition_drag_hit_areas: list[tuple[int, int, QRectF]] = []
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setMinimumHeight(220)

    def set_blocks(self, blocks: list[dict[str, object]], labels: list[str]) -> None:
        self._blocks = [dict(item) for item in blocks]
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
                gate_index = self._gate_drop_target_for_palette(event.position())
                if gate_index >= 0:
                    self.block_selected.emit(gate_index)
                    self.palette_block_dropped_to_gate.emit(block_type, gate_index)
                else:
                    self.block_added.emit(block_type)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        point = event.position()
        for gate_index, condition_index, rect in self._gate_condition_drag_hit_areas:
            if rect.contains(point):
                self._nested_drag_active = True
                self._nested_drag_gate_index = gate_index
                self._nested_drag_condition_index = condition_index
                self._nested_drag_insert_index = -1
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
        for gate_index, condition_index, rect in self._gate_condition_up_hit_areas:
            if rect.contains(point):
                self._drag_active = False
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self._selected_gate_condition = (gate_index, condition_index)
                self.block_selected.emit(gate_index)
                self.gate_condition_move_requested.emit(gate_index, condition_index, -1)
                self.update()
                return
        for gate_index, condition_index, rect in self._gate_condition_down_hit_areas:
            if rect.contains(point):
                self._drag_active = False
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self._selected_gate_condition = (gate_index, condition_index)
                self.block_selected.emit(gate_index)
                self.gate_condition_move_requested.emit(gate_index, condition_index, 1)
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
                self._update_hover_cursor(point)
                self.block_selected.emit(idx)
                return
        self._update_hover_cursor(point)
        self._drag_active = False
        self._nested_drag_active = False
        self._selected_gate_condition = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        point = event.position()
        self._update_hover_cursor(point)
        if self._nested_drag_active and self._nested_drag_gate_index >= 0 and self._nested_drag_condition_index >= 0:
            source_gate_rect = self._hit_areas[self._nested_drag_gate_index] if 0 <= self._nested_drag_gate_index < len(self._hit_areas) else QRectF()
            if source_gate_rect.contains(point):
                self._nested_drag_insert_index = -1
            else:
                self._nested_drag_insert_index = self._top_level_insert_index_for_point(point.y())
            self.update()
            super().mouseMoveEvent(event)
            return
        if not self._drag_active or self._drag_origin_index < 0:
            super().mouseMoveEvent(event)
            return
        self._drag_gate_target_index = self._gate_drop_target_at(point, self._drag_origin_index)
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
        if (
            self._nested_drag_active
            and self._nested_drag_gate_index >= 0
            and self._nested_drag_condition_index >= 0
        ):
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
            self._update_hover_cursor(drop_point)
            self.update()
            super().mouseReleaseEvent(event)
            return
        if (
            self._drag_active
            and self._drag_origin_index >= 0
            and self._drag_insert_index >= 0
            and 0 <= self._drag_origin_index < len(self._blocks)
        ):
            drop_gate_index = self._drag_gate_target_index
            source_block = self._blocks[self._drag_origin_index]
            source_type = str(source_block.get("type", "")).strip().lower()
            if drop_gate_index >= 0 and source_type == "condition":
                self.block_selected.emit(drop_gate_index)
                self.condition_dropped_to_gate.emit(self._drag_origin_index, drop_gate_index)
                self._drag_origin_index = -1
                self._drag_insert_index = -1
                self._drag_gate_target_index = -1
                self._drag_active = False
                self._update_hover_cursor(drop_point)
                self.update()
                super().mouseReleaseEvent(event)
                return
            block = self._blocks.pop(self._drag_origin_index)
            label = self._labels.pop(self._drag_origin_index)
            target_index = self._drag_insert_index
            if target_index > self._drag_origin_index:
                target_index -= 1
            target_index = min(max(0, target_index), len(self._blocks))
            self._blocks.insert(target_index, block)
            self._labels.insert(target_index, label)
            self._selected_index = target_index
            if target_index != self._drag_origin_index:
                self.blocks_reordered.emit([dict(item) for item in self._blocks])
        self._drag_origin_index = -1
        self._drag_insert_index = -1
        self._drag_gate_target_index = -1
        self._drag_active = False
        self._nested_drag_active = False
        self._nested_drag_gate_index = -1
        self._nested_drag_condition_index = -1
        self._nested_drag_insert_index = -1
        self._update_hover_cursor(drop_point)
        self.update()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.unsetCursor()
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
        gap = 22.0
        width = max(120.0, self.width() - margin_x * 2.0)
        self._hit_areas = []
        self._delete_hit_areas = []
        self._move_hit_areas = []
        self._gate_condition_delete_hit_areas = []
        self._gate_condition_up_hit_areas = []
        self._gate_condition_down_hit_areas = []
        self._gate_condition_row_hit_areas = []
        self._gate_condition_drag_hit_areas = []
        current_top = top

        for idx, block in enumerate(self._blocks):
            block_height = float(self._block_height(block))
            rect = QRectF(margin_x, current_top, width, block_height)
            self._hit_areas.append(rect)
            move_rect = QRectF(rect.right() - 54.0, rect.top() + 6.0, 20.0, 20.0)
            delete_rect = QRectF(rect.right() - 28.0, rect.top() + 6.0, 20.0, 20.0)
            self._move_hit_areas.append(move_rect)
            self._delete_hit_areas.append(delete_rect)

            gate_nested_selected = (
                self._selected_gate_condition is not None
                and self._selected_gate_condition[0] == idx
            )
            selected = idx == self._selected_index and not gate_nested_selected
            border = QColor(34, 211, 238) if selected else QColor(45, 71, 103)
            background = QColor(12, 28, 52, 220 if selected else 180)
            if self._drag_active and idx == self._drag_origin_index:
                border = QColor(103, 232, 249)
            if self._drag_active and idx == self._drag_gate_target_index:
                border = QColor(45, 212, 191)
                background = QColor(10, 42, 60, 230)
            painter.setPen(QPen(border, 1.6))
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 12.0, 12.0)

            painter.setPen(QPen(QColor(48, 83, 118), 1.2))
            painter.setBrush(QColor(9, 24, 45, 230))
            painter.drawRoundedRect(move_rect, 6.0, 6.0)
            painter.setPen(QColor(227, 240, 252))
            painter.drawText(move_rect, Qt.AlignmentFlag.AlignCenter, "≡")

            painter.setPen(QPen(QColor(48, 83, 118), 1.2))
            painter.setBrush(QColor(9, 24, 45, 230))
            painter.drawRoundedRect(delete_rect, 6.0, 6.0)
            painter.setPen(QColor(227, 240, 252))
            painter.drawText(delete_rect, Qt.AlignmentFlag.AlignCenter, "-")

            if str(block.get("type", "")).strip().lower() == "gate":
                title_rect = QRectF(rect.left() + 12.0, rect.top() + 8.0, rect.width() - 76.0, 18.0)
                mode_rect = QRectF(rect.left() + 12.0, rect.top() + 27.0, rect.width() - 76.0, 16.0)
                painter.setPen(QColor(236, 246, 255))
                painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title_for_block(block))
                painter.setPen(QColor(152, 176, 201))
                subtitle = self._labels[idx] if idx < len(self._labels) else ""
                painter.drawText(mode_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)

                conditions = self._gate_conditions(block)
                row_top = rect.top() + 48.0
                for condition_index, condition in enumerate(conditions):
                    cond_rect = QRectF(rect.left() + 12.0, row_top, rect.width() - 24.0, 24.0)
                    condition_selected = self._selected_gate_condition == (idx, condition_index)
                    cond_border = QColor(34, 211, 238) if condition_selected else QColor(48, 83, 118)
                    cond_bg = QColor(10, 84, 122, 236) if condition_selected else QColor(8, 60, 92, 220)
                    painter.setPen(QPen(cond_border, 1.2 if condition_selected else 1.0))
                    painter.setBrush(cond_bg)
                    painter.drawRoundedRect(cond_rect, 9.0, 9.0)
                    drag_rect_nested = QRectF(cond_rect.right() - 92.0, cond_rect.top() + 3.0, 18.0, 18.0)
                    up_rect = QRectF(cond_rect.right() - 70.0, cond_rect.top() + 3.0, 18.0, 18.0)
                    down_rect = QRectF(cond_rect.right() - 48.0, cond_rect.top() + 3.0, 18.0, 18.0)
                    delete_rect_nested = QRectF(cond_rect.right() - 26.0, cond_rect.top() + 3.0, 18.0, 18.0)
                    self._gate_condition_row_hit_areas.append((idx, condition_index, cond_rect))
                    self._gate_condition_drag_hit_areas.append((idx, condition_index, drag_rect_nested))
                    self._gate_condition_up_hit_areas.append((idx, condition_index, up_rect))
                    self._gate_condition_down_hit_areas.append((idx, condition_index, down_rect))
                    self._gate_condition_delete_hit_areas.append((idx, condition_index, delete_rect_nested))
                    action_border = QColor(34, 211, 238) if condition_selected else QColor(48, 83, 118)
                    action_bg = QColor(8, 38, 66, 238) if condition_selected else QColor(9, 24, 45, 230)
                    painter.setPen(QPen(action_border, 1.0))
                    painter.setBrush(action_bg)
                    painter.drawRoundedRect(drag_rect_nested, 5.0, 5.0)
                    painter.drawRoundedRect(up_rect, 5.0, 5.0)
                    painter.drawRoundedRect(down_rect, 5.0, 5.0)
                    painter.drawRoundedRect(delete_rect_nested, 5.0, 5.0)
                    painter.setPen(QColor(227, 240, 252))
                    painter.drawText(drag_rect_nested, Qt.AlignmentFlag.AlignCenter, "≡")
                    painter.drawText(up_rect, Qt.AlignmentFlag.AlignCenter, "↑")
                    painter.drawText(down_rect, Qt.AlignmentFlag.AlignCenter, "↓")
                    painter.drawText(delete_rect_nested, Qt.AlignmentFlag.AlignCenter, "-")
                    painter.setPen(QColor(227, 240, 252))
                    painter.drawText(
                        cond_rect.adjusted(10.0, 0.0, -96.0, 0.0),
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        self._gate_condition_text(condition),
                    )
                    row_top += 30.0
            else:
                title_rect = rect.adjusted(12, 8, -64, -26)
                subtitle_rect = rect.adjusted(12, 28, -64, -8)
                painter.setPen(QColor(236, 246, 255))
                painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title_for_block(block))
                painter.setPen(QColor(152, 176, 201))
                subtitle = self._labels[idx] if idx < len(self._labels) else ""
                painter.drawText(subtitle_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)

            if idx < len(self._blocks) - 1:
                line_x = rect.center().x()
                line_top = rect.bottom() + 4.0
                line_bottom = rect.bottom() + gap - 6.0
                painter.setPen(QPen(QColor(102, 178, 232), 1.4))
                painter.drawLine(QPointF(line_x, line_top), QPointF(line_x, line_bottom))
                arrow = QPolygonF(
                    [
                        QPointF(line_x, line_bottom + 5.0),
                        QPointF(line_x - 5.0, line_bottom - 3.0),
                        QPointF(line_x + 5.0, line_bottom - 3.0),
                    ]
                )
                painter.setBrush(QColor(102, 178, 232))
                painter.drawPolygon(arrow)
            current_top = rect.bottom() + gap

        if self._drag_active and self._drag_origin_index >= 0 and self._drag_insert_index >= 0:
            line_y = self._insertion_line_y(self._drag_insert_index)
            painter.setPen(QPen(QColor(34, 211, 238), 3.0))
            painter.drawLine(QPointF(margin_x + 8.0, line_y), QPointF(margin_x + width - 8.0, line_y))
        if self._nested_drag_active and self._nested_drag_insert_index >= 0:
            line_y = self._insertion_line_y(self._nested_drag_insert_index)
            painter.setPen(QPen(QColor(45, 212, 191), 3.0))
            painter.drawLine(QPointF(margin_x + 8.0, line_y), QPointF(margin_x + width - 8.0, line_y))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(640, 220)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 220)

    def _content_height(self) -> int:
        if not self._blocks:
            return 220
        gap = 22
        top = 14
        bottom = 16
        heights = [self._block_height(block) for block in self._blocks]
        return top + sum(heights) + (max(0, len(heights) - 1) * gap) + bottom

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
        if not isinstance(source_block, dict) or str(source_block.get("type", "")).strip().lower() != "condition":
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

    def _block_height(self, block: dict[str, object]) -> int:
        block_type = str(block.get("type", "")).strip().lower()
        if block_type != "gate":
            return 58
        conditions = self._gate_conditions(block)
        if not conditions:
            return 58
        return 58 + (len(conditions) * 30) + 8

    @staticmethod
    def _gate_conditions(block: dict[str, object]) -> list[dict[str, object]]:
        raw = block.get("conditions", [])
        if not isinstance(raw, list):
            return []
        result: list[dict[str, object]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")).strip().lower() != "condition":
                continue
            result.append(item)
        return result

    @staticmethod
    def _gate_condition_text(block: dict[str, object]) -> str:
        metric = str(block.get("metric_key", "")).strip() or "metric"
        operator = str(block.get("operator", ">=")).strip() or ">="
        value = float(block.get("value", 0.0) or 0.0)
        return tr("Condition: {metric} {operator} {value}").format(
            metric=metric,
            operator=operator,
            value=f"{value:g}",
        )

    def content_height(self) -> int:
        return self._content_h

    def _update_hover_cursor(self, point: QPointF) -> None:
        if self._drag_active or self._nested_drag_active:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        for _, _, rect in self._gate_condition_drag_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.OpenHandCursor)
                return
        for _, _, rect in self._gate_condition_delete_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        for _, _, rect in self._gate_condition_up_hit_areas:
            if rect.contains(point):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
        for _, _, rect in self._gate_condition_down_hit_areas:
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
        self.unsetCursor()

    @staticmethod
    def _title_for_block(block: dict[str, object]) -> str:
        block_type = str(block.get("type", "")).strip().lower()
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


class AutomationTab(QWidget):
    rules_changed = Signal()
    manual_run_requested = Signal(str)
    simulate_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rules: list[AutomationRule] = []
        self._device_options: list[tuple[str, str]] = []
        self._measurement_keys: list[str] = []
        self._measurement_keys_by_device: dict[str, list[str]] = {}
        self._active_rule_id: str | None = None
        self._syncing = False
        self._selected_nested_condition: tuple[int, int] | None = None

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

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.add_rule_btn = QPushButton(tr("New Rule"))
        self.delete_rule_btn = QPushButton(tr("Delete"))
        self.duplicate_rule_btn = QPushButton(tr("Duplicate"))
        self.activate_btn = QPushButton(tr("Activate"))
        self.manual_run_btn = QPushButton(tr("Run"))
        self.simulate_btn = QPushButton(tr("Simulate"))
        for button in (
            self.add_rule_btn,
            self.delete_rule_btn,
            self.duplicate_rule_btn,
            self.activate_btn,
            self.manual_run_btn,
            self.simulate_btn,
        ):
            button.setObjectName("AutomationActionButton")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            toolbar.addWidget(button)
        toolbar.addStretch(1)

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

        self.palette = BlockPalette()
        self.palette.setObjectName("AutomationList")
        self.palette.addItem(self._palette_item(tr("Trigger"), "trigger"))
        self.palette.addItem(self._palette_item(tr("Condition"), "condition"))
        self.palette.addItem(self._palette_item(tr("Gate"), "gate"))
        self.palette.addItem(self._palette_item(tr("Delay"), "delay"))
        self.palette.addItem(self._palette_item(tr("Action"), "action"))

        self.canvas = FlowCanvas()
        self.canvas.setObjectName("AutomationList")
        self.canvas.hide()
        self.diagram_view = FlowDiagramView()
        self.diagram_view.setObjectName("AutomationDiagram")
        self.diagram_scroll = QScrollArea()
        self.diagram_scroll.setObjectName("AutomationList")
        self.diagram_scroll.setWidgetResizable(True)
        self.diagram_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.diagram_scroll.setWidget(self.diagram_view)
        self.diagram_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)

        self.rule_cooldown = QSpinBox()
        self.rule_cooldown.setRange(0, 3600)
        self.rule_cooldown.setSuffix(" sec")

        self.block_editor = QStackedWidget()
        self.block_editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
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

        meta_panel = QWidget()
        meta_form = QVBoxLayout(meta_panel)
        meta_form.setContentsMargins(0, 0, 0, 0)
        meta_form.setSpacing(12)
        meta_form.addWidget(QLabel(tr("Cooldown")))
        meta_form.addWidget(self.rule_cooldown)
        self._expand_fields(self.rule_cooldown)

        left = QVBoxLayout()
        left.addWidget(QLabel(tr("Rules")))
        left.addWidget(self.rule_list, 1)
        left_analytics = QWidget()
        left_analytics_layout = QVBoxLayout(left_analytics)
        left_analytics_layout.setContentsMargins(0, 0, 0, 0)
        left_analytics_layout.setSpacing(12)
        left_analytics_layout.addWidget(QLabel(tr("Execution Logs")))
        left_analytics_layout.addWidget(self.logs_view)
        left_analytics_layout.addWidget(self.analytics_label)
        left.addWidget(left_analytics, 0)

        middle = QVBoxLayout()
        middle.addWidget(QLabel(tr("Palette")))
        middle.addWidget(self.palette)
        flow_header = QHBoxLayout()
        flow_header.setContentsMargins(0, 0, 0, 0)
        flow_header.setSpacing(8)
        flow_header.addWidget(QLabel(tr("Flow Canvas (drag to reorder)")))
        flow_header.addStretch(1)
        middle.addLayout(flow_header)
        middle.addWidget(self.diagram_scroll, 1)

        right_fields_panel = QWidget()
        right_fields_panel.setObjectName("AutomationRightPanel")
        right_fields = QVBoxLayout(right_fields_panel)
        right_fields.setContentsMargins(14, 14, 14, 14)
        right_fields.setSpacing(16)
        right_fields.setAlignment(Qt.AlignmentFlag.AlignTop)
        right_fields.addWidget(meta_panel)
        right_fields.addWidget(QLabel(tr("Selected Block")))
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
        section_layout.addLayout(toolbar)
        section_layout.addLayout(body)
        root.addWidget(section)

        self.add_rule_btn.clicked.connect(self._add_rule)
        self.delete_rule_btn.clicked.connect(self._delete_rule)
        self.duplicate_rule_btn.clicked.connect(self._duplicate_rule)
        self.activate_btn.clicked.connect(self._activate_current_rule)
        self.manual_run_btn.clicked.connect(self._emit_manual_run)
        self.simulate_btn.clicked.connect(self._emit_simulate)
        self.rule_list.currentRowChanged.connect(self._switch_rule)
        self.rule_list.itemChanged.connect(self._on_rule_item_changed)
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
        self.diagram_view.block_selected.connect(self._select_block_from_diagram)
        self.diagram_view.block_delete_requested.connect(self._remove_block_by_index)
        self.diagram_view.blocks_reordered.connect(self._apply_diagram_reorder)
        self.diagram_view.condition_dropped_to_gate.connect(self._move_condition_into_gate_from_diagram)
        self.diagram_view.gate_condition_selected.connect(self._select_nested_condition_from_diagram)
        self.diagram_view.gate_condition_extract_requested.connect(self._extract_condition_from_gate_in_diagram)
        self.diagram_view.gate_condition_delete_requested.connect(self._remove_condition_from_gate_in_diagram)
        self.diagram_view.gate_condition_move_requested.connect(self._move_condition_within_gate_in_diagram)

        self.rule_cooldown.valueChanged.connect(lambda *_args: self._save_current_rule())

        self._connect_block_editors()
        self._add_rule()
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

    def set_rules(self, rules: list[AutomationRule]) -> None:
        self._rules = [replace(item) for item in rules]
        self._refresh_rule_list()

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

    def _palette_item(self, title: str, block_type: str) -> QListWidgetItem:
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole, block_type)
        return item

    def _build_empty_editor(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        label = QLabel(tr("Select a block to edit."))
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)
        return box

    def _build_trigger_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.trigger_mode = QComboBox()
        self.trigger_mode.addItem(tr("Measurement"), "measurement")
        self.trigger_mode.addItem(tr("Schedule"), "schedule")
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
        form.addRow(tr("Type"), self.trigger_mode)
        form.addRow(tr("Device"), self.trigger_device)
        form.addRow(tr("Metric"), self.trigger_metric)
        form.addRow(tr("Operator"), self.trigger_operator)
        form.addRow(tr("Value"), self.trigger_value)
        form.addRow(tr("Schedule"), self.trigger_schedule)
        self._expand_fields(
            self.trigger_mode,
            self.trigger_device,
            self.trigger_metric,
            self.trigger_operator,
            self.trigger_value,
            self.trigger_schedule,
        )
        return box

    def _build_condition_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
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
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.gate_mode = QComboBox()
        self.gate_mode.addItem(tr("All conditions"), "and")
        self.gate_mode.addItem(tr("Any condition"), "or")
        self.gate_hint = QLabel(tr("Drag condition blocks into the logical container on canvas."))
        self.gate_hint.setObjectName("SidebarMeta")
        self.gate_hint.setWordWrap(True)
        form.addRow(tr("Type"), self.gate_mode)
        form.addRow(self.gate_hint)
        self._expand_fields(self.gate_mode)
        return box

    def _build_delay_editor(self) -> QWidget:
        box = QWidget()
        form = QFormLayout(box)
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
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.action_device = QComboBox()
        self.action_state = QComboBox()
        self.action_state.addItem(tr("Turn ON"), True)
        self.action_state.addItem(tr("Turn OFF"), False)
        self.action_retries = QSpinBox()
        self.action_retries.setRange(0, 5)
        self.action_retry_delay = QDoubleSpinBox()
        self.action_retry_delay.setRange(0.0, 60.0)
        self.action_retry_delay.setDecimals(1)
        self.action_retry_delay.setSuffix(" sec")
        form.addRow(tr("Device"), self.action_device)
        form.addRow(tr("State"), self.action_state)
        form.addRow(tr("Retries"), self.action_retries)
        form.addRow(tr("Retry delay"), self.action_retry_delay)
        self._expand_fields(
            self.action_device,
            self.action_state,
            self.action_retries,
            self.action_retry_delay,
        )
        return box

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

    def _add_rule(self) -> None:
        rule = AutomationRule(
            rule_id=uuid.uuid4().hex,
            name=tr("Automation {index}").format(index=len(self._rules) + 1),
            enabled=True,
            active=False,
            flow_blocks=[{"type": "trigger"}, {"type": "action"}],
        )
        self._rules.append(rule)
        self._refresh_rule_list(select_rule_id=rule.rule_id)
        self.rules_changed.emit()

    def _delete_rule(self) -> None:
        row = self.rule_list.currentRow()
        if row < 0 or row >= len(self._rules):
            return
        self._rules.pop(row)
        if not self._rules:
            self._add_rule()
            return
        self._refresh_rule_list(select_row=max(0, row - 1))
        self.rules_changed.emit()

    def _duplicate_rule(self) -> None:
        rule = self._current_rule()
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

    def _emit_manual_run(self) -> None:
        rule = self._current_rule()
        if rule is None:
            return
        self.manual_run_requested.emit(rule.rule_id)

    def _emit_simulate(self) -> None:
        rule = self._current_rule()
        if rule is None:
            return
        self.simulate_requested.emit(rule.rule_id)

    def _activate_current_rule(self) -> None:
        rule = self._current_rule()
        if rule is None:
            return
        self._save_current_rule()
        for item in self._rules:
            if item.rule_id == rule.rule_id:
                item.active = True
                item.version = max(item.version, item.draft_version)
                item.updated_at = _now_label()
                break
        self._refresh_rule_list(select_rule_id=rule.rule_id)
        self.rules_changed.emit()

    def _add_block(self, block_type: str) -> None:
        block: dict[str, object]
        if block_type == "trigger":
            block = {"type": "trigger", "trigger_type": "measurement", "operator": ">=", "value": 0.0}
        elif block_type == "condition":
            block = {"type": "condition", "operator": ">=", "value": 0.0}
        elif block_type == "gate":
            block = {"type": "gate", "mode": "and"}
        elif block_type == "delay":
            block = {"type": "delay", "seconds": 1.0}
        else:
            block = {"type": "action", "action_type": "power", "value": True, "retries": 0, "retry_delay_sec": 1.0}
        self._append_block_item(block)
        self._save_current_rule()

    def _remove_selected_block(self) -> None:
        row = self.diagram_view.selected_index()
        if row < 0:
            row = self.canvas.currentRow()
        self._remove_block_by_index(row)

    def _append_block_item(self, block: dict[str, object]) -> None:
        label = self._block_label(block)
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, block)
        self.canvas.addItem(item)
        self.canvas.setCurrentItem(item)
        self._sync_diagram_from_canvas()

    def _block_label(self, block: dict[str, object]) -> str:
        kind = str(block.get("type", "")).strip().lower()
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
            cond_count = len(block.get("conditions", [])) if isinstance(block.get("conditions", []), list) else 0
            return tr("Gate: {mode} ({count})").format(mode=mode_label, count=cond_count)
        if kind == "delay":
            sec = float(block.get("seconds", 0.0) or 0.0)
            return tr("Delay: {seconds} sec").format(seconds=f"{sec:g}")
        if kind == "action":
            state = tr("ON") if bool(block.get("value", False)) else tr("OFF")
            return tr("Action: Power {state}").format(state=state)
        return tr("Block")

    def _switch_rule(self, row: int) -> None:
        if self._syncing:
            return
        if row < 0 or row >= len(self._rules):
            return
        rule = self._rules[row]
        self._active_rule_id = rule.rule_id
        self._populate_rule_details(rule)

    def _on_rule_item_changed(self, item: QListWidgetItem) -> None:
        if self._syncing:
            return
        row = self.rule_list.row(item)
        if row < 0 or row >= len(self._rules):
            return
        rule = self._rules[row]
        new_name = item.text().strip() or rule.name
        item.setText(new_name)
        rule.name = new_name
        rule.enabled = item.checkState() == Qt.CheckState.Checked
        rule.updated_at = _now_label()
        rule.draft_version = max(1, int(rule.draft_version) + 1)
        self.rules_changed.emit()

    def _refresh_rule_list(self, *, select_row: int | None = None, select_rule_id: str | None = None) -> None:
        self._syncing = True
        self.rule_list.clear()
        for rule in self._rules:
            item = QListWidgetItem(rule.name)
            item.setData(Qt.ItemDataRole.UserRole, rule.rule_id)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsEditable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            state_label = tr("ACTIVE") if rule.active else tr("DRAFT")
            item.setToolTip(tr("State: {state}").format(state=state_label))
            self.rule_list.addItem(item)
        target_row = 0
        if select_rule_id:
            for idx in range(self.rule_list.count()):
                item = self.rule_list.item(idx)
                if item and item.data(Qt.ItemDataRole.UserRole) == select_rule_id:
                    target_row = idx
                    break
        elif select_row is not None:
            target_row = max(0, min(select_row, self.rule_list.count() - 1))
        if self.rule_list.count() > 0:
            self.rule_list.setCurrentRow(target_row)
        self._syncing = False

    def _populate_rule_details(self, rule: AutomationRule) -> None:
        self._syncing = True
        self._selected_nested_condition = None
        self.rule_cooldown.setValue(rule.cooldown_sec)
        self.canvas.clear()
        flow_blocks = list(rule.flow_blocks)
        if not flow_blocks:
            flow_blocks = flow_blocks_from_graph(rule.flow_graph)
        for block in (flow_blocks or [{"type": "trigger"}, {"type": "action"}]):
            if isinstance(block, dict):
                self._append_block_item(dict(block))
        if self.canvas.count() > 0:
            self.canvas.setCurrentRow(0)
        self._sync_diagram_from_canvas()
        self._syncing = False

    def _current_rule(self) -> AutomationRule | None:
        row = self.rule_list.currentRow()
        if row < 0 or row >= len(self._rules):
            return None
        return self._rules[row]

    def _show_selected_block_editor(self, row: int) -> None:
        self._selected_nested_condition = None
        if row < 0 or row >= self.canvas.count():
            self.block_editor.setCurrentIndex(0)
            return
        item = self.canvas.item(row)
        block = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(block, dict):
            self.block_editor.setCurrentIndex(0)
            return
        kind = str(block.get("type", "")).strip().lower()
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
            self.block_editor.setCurrentIndex(0)
        self._syncing = False

    def _update_block_editor_height(self) -> None:
        current = self.block_editor.currentWidget()
        if current is None:
            return
        target = max(44, current.sizeHint().height())
        self.block_editor.setFixedHeight(target)

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

    def _load_condition_block(self, block: dict[str, object]) -> None:
        self._set_combo_by_data(self.condition_device, block.get("device_id", ""))
        self._refresh_condition_metric_options()
        self._set_combo_by_data(self.condition_metric, block.get("metric_key", ""))
        self._set_combo_by_data(self.condition_operator, block.get("operator", ">="))
        self._update_condition_value_unit_suffix()
        self.condition_value.setValue(float(block.get("value", 0.0) or 0.0))

    def _load_action_block(self, block: dict[str, object]) -> None:
        self._set_combo_by_data(self.action_device, block.get("device_id", ""))
        self._set_combo_by_data(self.action_state, bool(block.get("value", False)))
        self.action_retries.setValue(max(0, int(block.get("retries", 0) or 0)))
        self.action_retry_delay.setValue(float(block.get("retry_delay_sec", 1.0) or 1.0))

    def _load_gate_block(self, block: dict[str, object]) -> None:
        mode = str(block.get("mode", "and")).strip().lower() or "and"
        self._set_combo_by_data(self.gate_mode, mode)

    def _save_block_editor(self) -> None:
        if self._syncing:
            return
        if self._selected_nested_condition is not None:
            gate_row, condition_row = self._selected_nested_condition
            if gate_row < 0 or gate_row >= self.canvas.count():
                return
            gate_item = self.canvas.item(gate_row)
            if gate_item is None:
                return
            gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
                return
            conditions = gate_block.get("conditions", [])
            if not isinstance(conditions, list):
                return
            normalized: list[dict[str, object]] = [
                dict(item)
                for item in conditions
                if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "condition"
            ]
            if condition_row < 0 or condition_row >= len(normalized):
                return
            block = normalized[condition_row]
            block["device_id"] = self.condition_device.currentData() or ""
            block["metric_key"] = self.condition_metric.currentData() or ""
            block["operator"] = self.condition_operator.currentData() or ">="
            block["value"] = float(self.condition_value.value())
            normalized[condition_row] = block
            gate_block["conditions"] = normalized
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
        elif kind == "gate":
            block["mode"] = str(self.gate_mode.currentData() or "and")
        elif kind == "delay":
            block["seconds"] = float(self.delay_seconds.value())
        elif kind == "action":
            block["device_id"] = self.action_device.currentData() or ""
            block["action_type"] = "power"
            block["value"] = bool(self.action_state.currentData())
            block["retries"] = int(self.action_retries.value())
            block["retry_delay_sec"] = float(self.action_retry_delay.value())
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
        item = self.rule_list.currentItem()
        if item is not None:
            name = item.text().strip() or rule.name
            enabled = item.checkState() == Qt.CheckState.Checked
        else:
            name = rule.name
            enabled = rule.enabled
        blocks: list[dict[str, object]] = []
        for idx in range(self.canvas.count()):
            block = self.canvas.item(idx).data(Qt.ItemDataRole.UserRole)
            if isinstance(block, dict):
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
            embedded = block.get("conditions", [])
            if not isinstance(embedded, list):
                continue
            for embedded_item in embedded:
                if isinstance(embedded_item, dict) and str(embedded_item.get("type", "")).strip().lower() == "condition":
                    conditions.append(dict(embedded_item))
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
        gate_modes = [
            str(item.get("mode", "")).strip().lower()
            for item in blocks
            if str(item.get("type", "")).strip().lower() == "gate"
        ]
        if "or" in gate_modes:
            rule.conditions_logic = "any"
        elif "and" in gate_modes:
            rule.conditions_logic = "all"
        else:
            rule.conditions_logic = "all"
        rule.cooldown_sec = int(self.rule_cooldown.value())
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
            parsed_actions.append(
                AutomationAction(
                    action_type=str(block.get("action_type", "power")) or "power",
                    device_id=str(block.get("device_id", "")),
                    value=bool(block.get("value", False)),
                    retries=max(0, int(block.get("retries", 0) or 0)),
                    retry_delay_sec=max(0.0, float(block.get("retry_delay_sec", 1.0) or 1.0)),
                )
            )
        rule.actions = parsed_actions

        self._refresh_rule_list(select_rule_id=rule.rule_id)
        self.rules_changed.emit()

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
            embedded = block.get("conditions", [])
            if isinstance(embedded, list):
                for embedded_item in embedded:
                    if not isinstance(embedded_item, dict):
                        continue
                    if str(embedded_item.get("type", "")).strip().lower() != "condition":
                        continue
                    expanded.append(dict(embedded_item))
            gate_only = dict(block)
            gate_only.pop("conditions", None)
            expanded.append(gate_only)
        return expanded

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
            labels.append(str(item.text()))
        self.diagram_view.set_blocks(blocks, labels)
        self._sync_diagram_geometry()
        selected_row = self.canvas.currentRow()
        self.diagram_view.set_selected_index(selected_row)
        if self._selected_nested_condition is not None:
            gate_row, condition_row = self._selected_nested_condition
            if gate_row == selected_row:
                self.diagram_view.set_selected_gate_condition(gate_row, condition_row)

    def _sync_diagram_selection(self, row: int) -> None:
        self.diagram_view.set_selected_index(row)
        if self._selected_nested_condition is not None:
            gate_row, condition_row = self._selected_nested_condition
            if gate_row == row:
                self.diagram_view.set_selected_gate_condition(gate_row, condition_row)
                return
        self.diagram_view.set_selected_gate_condition(-1, -1)

    def _select_block_from_diagram(self, row: int) -> None:
        if 0 <= row < self.canvas.count():
            self._selected_nested_condition = None
            self.diagram_view.set_selected_gate_condition(-1, -1)
            if self.canvas.currentRow() == row:
                self._show_selected_block_editor(row)
                self._sync_diagram_selection(row)
                return
            self.canvas.setCurrentRow(row)

    def _select_nested_condition_from_diagram(self, gate_row: int, condition_row: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        conditions = gate_block.get("conditions", [])
        if not isinstance(conditions, list):
            return
        normalized: list[dict[str, object]] = [
            dict(item)
            for item in conditions
            if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "condition"
        ]
        if condition_row < 0 or condition_row >= len(normalized):
            return
        if self.canvas.currentRow() != gate_row:
            self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = (gate_row, condition_row)
        self.diagram_view.set_selected_gate_condition(gate_row, condition_row)
        self._syncing = True
        self.block_editor.setCurrentIndex(2)
        self._load_condition_block(normalized[condition_row])
        self._syncing = False

    def _apply_diagram_reorder(self, blocks: list[dict[str, object]]) -> None:
        if self._syncing:
            return
        self._syncing = True
        selected_row = self.diagram_view.selected_index()
        self.canvas.clear()
        for block in blocks:
            if not isinstance(block, dict):
                continue
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
        self._selected_nested_condition = None
        self.canvas.takeItem(row)
        if self.canvas.count() > 0:
            self.canvas.setCurrentRow(max(0, row - 1))
        else:
            self.block_editor.setCurrentIndex(0)
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
        if str(source_block.get("type", "")).strip().lower() != "condition":
            return
        if str(gate_block.get("type", "")).strip().lower() != "gate":
            return

        condition = dict(source_block)
        condition["type"] = "condition"
        conditions = gate_block.get("conditions", [])
        normalized: list[dict[str, object]] = []
        if isinstance(conditions, list):
            for item in conditions:
                if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "condition":
                    normalized.append(dict(item))

        normalized.append(condition)
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

    def _remove_condition_from_gate_in_diagram(self, gate_row: int, condition_row: int) -> None:
        if gate_row < 0 or gate_row >= self.canvas.count():
            return
        gate_item = self.canvas.item(gate_row)
        if gate_item is None:
            return
        gate_block = gate_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(gate_block, dict) or str(gate_block.get("type", "")).strip().lower() != "gate":
            return
        conditions = gate_block.get("conditions", [])
        if not isinstance(conditions, list):
            return
        normalized: list[dict[str, object]] = [
            dict(item)
            for item in conditions
            if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "condition"
        ]
        if condition_row < 0 or condition_row >= len(normalized):
            return
        normalized.pop(condition_row)
        gate_block["conditions"] = normalized
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
        conditions = gate_block.get("conditions", [])
        if not isinstance(conditions, list):
            return
        normalized: list[dict[str, object]] = [
            dict(item)
            for item in conditions
            if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "condition"
        ]
        if condition_row < 0 or condition_row >= len(normalized):
            return
        target = condition_row + delta
        if target < 0 or target >= len(normalized):
            return
        normalized[condition_row], normalized[target] = normalized[target], normalized[condition_row]
        gate_block["conditions"] = normalized
        gate_item.setData(Qt.ItemDataRole.UserRole, gate_block)
        gate_item.setText(self._block_label(gate_block))
        self.canvas.setCurrentRow(gate_row)
        self._selected_nested_condition = (gate_row, target)
        self._sync_diagram_from_canvas()
        self._save_current_rule()

    def _add_palette_block_into_gate(self, block_type: str, gate_row: int) -> None:
        if str(block_type).strip().lower() != "condition":
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

        new_condition: dict[str, object] = {"type": "condition", "operator": ">=", "value": 0.0}
        conditions = gate_block.get("conditions", [])
        normalized: list[dict[str, object]] = []
        if isinstance(conditions, list):
            for item in conditions:
                if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "condition":
                    normalized.append(dict(item))
        normalized.append(new_condition)
        gate_block["conditions"] = normalized
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
        conditions = gate_block.get("conditions", [])
        if not isinstance(conditions, list):
            return
        normalized: list[dict[str, object]] = [
            dict(item)
            for item in conditions
            if isinstance(item, dict) and str(item.get("type", "")).strip().lower() == "condition"
        ]
        if condition_row < 0 or condition_row >= len(normalized):
            return
        condition = dict(normalized.pop(condition_row))
        gate_block["conditions"] = normalized
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
        return


def _now_label() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def analytics_from_logs(log_lines: list[str]) -> dict[str, dict[str, int]]:
    entries = []
    for line in log_lines:
        text = str(line).strip()
        if not text:
            continue
        level = "ok" if "[OK]" in text else "error" if "[ERROR]" in text else "info"
        rule_id = ""
        if "rule=" in text:
            tail = text.split("rule=", 1)[1]
            rule_id = tail.split(" ", 1)[0].strip()
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
