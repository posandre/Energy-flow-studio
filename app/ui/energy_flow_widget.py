from __future__ import annotations

import math

import shiboken6

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF, QPixmap
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QVBoxLayout,
    QWidget,
)

from app.services.energy_flow import EnergyFlowSnapshot
from app.services.i18n import tr
from app.services.logging_utils import get_logger
from app.services.weather_api import WeatherLiveSnapshot

UI_FONT_STACK = ["SF Pro Text", "Inter", "Segoe UI", "sans-serif"]
LOGGER = get_logger(__name__)

class EnergyFlowCanvas(QGraphicsView):
    pv_clicked = Signal()
    grid_clicked = Signal()
    battery_clicked = Signal()
    home_clicked = Signal()
    inverter_clicked = Signal()
    weather_metric_clicked = Signal(str)
    PV_COLOR = QColor("#f5cf55")
    BATTERY_COLOR = QColor("#3b82f6")
    GRID_COLOR = QColor("#22c55e")
    LOAD_COLOR = QColor("#f5cf55")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._snapshot: EnergyFlowSnapshot | None = None
        self._weather_snapshot: WeatherLiveSnapshot | None = None
        self._hovered_tag: str | None = None
        self._status_text = ""
        self._device_title = tr("Inverter")
        self._device_pn = ""
        self._device_sn = ""
        self._battery_capability_ah = 0.0
        self._active_tuya_icons: list[tuple[str, QPixmap]] = []
        self._active_tuya_icons_updated_at = ""
        self._hover_overlay_items: list[QGraphicsItem] = []
        self._hover_target_rects: dict[str, QRectF] = {}
        self._hover_target_order: list[str] = []
        self._animation_phase = 0.0
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(250)
        self._animation_timer.timeout.connect(self._advance_animation)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setFrameShape(self.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("background: transparent;")
        self.setMinimumHeight(540)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        # Animate only when there is active power flow to avoid unnecessary redraw cost.
        self._sync_animation_timer()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_view_fit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        try:
            hovered_tag = self._tag_at_view_pos(event.position().toPoint())
            if hovered_tag != self._hovered_tag:
                self._log_debug(
                    "EnergyFlow hover changed | "
                    f"from={self._hovered_tag!r} to={hovered_tag!r} | "
                    f"pos=({event.position().x():.1f},{event.position().y():.1f})"
                )
                self._hovered_tag = hovered_tag
                cursor = (
                    Qt.CursorShape.PointingHandCursor
                    if self._hovered_tag is not None
                    else Qt.CursorShape.ArrowCursor
                )
                self.viewport().setCursor(cursor)
                self.setCursor(cursor)
                self._update_hover_overlay()
        except Exception as exc:
            self._log_debug(f"mouseMoveEvent failed: {exc}")
        finally:
            super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hovered_tag is not None:
            self._log_debug(f"EnergyFlow hover leave | tag={self._hovered_tag!r}")
            self._hovered_tag = None
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._update_hover_overlay()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        try:
            tag = self._tag_at_view_pos(event.position().toPoint())
            if tag == "pv_node":
                QTimer.singleShot(0, self.pv_clicked.emit)
                event.accept()
                return
            if tag == "grid_node":
                QTimer.singleShot(0, self.grid_clicked.emit)
                event.accept()
                return
            if tag == "battery_node":
                QTimer.singleShot(0, self.battery_clicked.emit)
                event.accept()
                return
            if tag == "home_node":
                QTimer.singleShot(0, self.home_clicked.emit)
                event.accept()
                return
            if tag == "inverter_node":
                QTimer.singleShot(0, self.inverter_clicked.emit)
                event.accept()
                return
            if tag and tag.startswith("weather_"):
                metric_key = tag.removeprefix("weather_")
                QTimer.singleShot(0, lambda key=metric_key: self.weather_metric_clicked.emit(key))
                event.accept()
                return
        except Exception:
            event.accept()
            return
        super().mousePressEvent(event)

    def _tag_at_view_pos(self, point) -> str | None:
        try:
            scene_point = self.mapToScene(point)
            for tag in reversed(self._hover_target_order):
                rect = self._hover_target_rects.get(tag)
                if rect is not None and rect.contains(scene_point):
                    return tag
        except Exception as exc:
            self._log_debug(f"Hit-test failed: {exc}")
        return None

    def render_snapshot(
        self,
        snapshot: EnergyFlowSnapshot | None,
        weather_snapshot: WeatherLiveSnapshot | None,
        status_text: str,
    ) -> None:
        self._snapshot = snapshot
        self._weather_snapshot = weather_snapshot
        self._status_text = status_text
        self._sync_animation_timer()
        self._render_scene()

    def set_battery_capability_ah(self, value: float) -> None:
        try:
            capability = float(value)
        except (TypeError, ValueError):
            capability = 0.0
        self._battery_capability_ah = max(0.0, capability)
        self._render_scene()

    def set_active_tuya_icons(self, icons: list[tuple[str, QPixmap]]) -> None:
        prepared: list[tuple[str, QPixmap]] = []
        for item in icons:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            name, pixmap = item
            if not isinstance(pixmap, QPixmap) or pixmap.isNull():
                continue
            prepared.append((str(name or "").strip() or tr("Device"), pixmap))
        self._active_tuya_icons = prepared
        self._render_scene()

    def set_active_tuya_icons_updated_at(self, text: str) -> None:
        self._active_tuya_icons_updated_at = str(text or "").strip()
        self._render_scene()

    def set_device_title(self, title: str) -> None:
        self._device_title = title.strip() or tr("Inverter")
        self._render_scene()

    def set_device_meta(self, title: str, pn: str, sn: str) -> None:
        self._device_title = title.strip() or tr("Inverter")
        self._device_pn = str(pn).strip()
        self._device_sn = str(sn).strip()
        self._render_scene()

    def _advance_animation(self) -> None:
        self._animation_phase = (self._animation_phase + 0.65) % 1000.0
        if self._snapshot is not None and self._has_active_flow(self._snapshot):
            self._render_scene()
            return
        self._sync_animation_timer()

    def _sync_animation_timer(self) -> None:
        should_run = self._snapshot is not None and self._has_active_flow(self._snapshot)
        if should_run:
            if not self._animation_timer.isActive():
                self._animation_timer.start()
            return
        if self._animation_timer.isActive():
            self._animation_timer.stop()

    def _render_scene(self) -> None:
        self._scene.clear()
        self._hover_overlay_items.clear()
        self._hover_target_rects.clear()
        self._hover_target_order.clear()
        self._scene.setSceneRect(0, 0, 1000, 700)
        snapshot = self._snapshot
        status_text = self._status_text
        self._log_debug(
            "EnergyFlow canvas render start | "
            f"weather_present={self._weather_snapshot is not None} | "
            f"status='{status_text}' | scene_rect={self._scene.sceneRect().getRect()}"
        )

        pv_pos = QPointF(180, 145)
        grid_pos = QPointF(820, 145)
        device_pos = QPointF(500, 360)
        battery_pos = QPointF(180, 585)
        load_pos = QPointF(820, 585)

        self._draw_glow(device_pos, QColor(56, 189, 248, 52), 168)
        self._draw_glow(pv_pos, QColor(56, 189, 248, 20), 126)
        self._draw_glow(grid_pos, QColor(148, 163, 184, 18), 126)
        self._draw_glow(battery_pos, QColor(56, 189, 248, 24), 126)
        self._draw_glow(load_pos, QColor(56, 189, 248, 24), 126)

        self._draw_branch(
            kind="top_left",
            start=pv_pos,
            end=device_pos,
            value=self._format_branch_value("pv", snapshot),
            active=self._has_flow_value(
                snapshot.pv_power if snapshot else None,
                snapshot.pv_direction if snapshot else None,
                snapshot.pv_voltage if snapshot else None,
            ),
            animated=self._is_active(snapshot.pv_power if snapshot else None, snapshot.pv_direction if snapshot else None),
            flow_sign=self._flow_sign("pv", snapshot.pv_power if snapshot else None, snapshot.pv_direction if snapshot else None),
            color=self.PV_COLOR,
        )
        self._draw_branch(
            kind="top_right",
            start=grid_pos,
            end=device_pos,
            value=self._format_branch_value("grid", snapshot),
            active=self._has_flow_value(
                snapshot.grid_power if snapshot else None,
                snapshot.grid_direction if snapshot else None,
                snapshot.grid_voltage if snapshot else None,
            ),
            animated=self._is_active(snapshot.grid_power if snapshot else None, snapshot.grid_direction if snapshot else None),
            flow_sign=self._grid_flow_sign(snapshot),
            color=self.GRID_COLOR,
        )
        battery_layers = self._battery_flow_layers(snapshot)
        self._draw_branch(
            kind="bottom_left",
            start=battery_pos,
            end=device_pos,
            value=self._format_branch_value("battery", snapshot),
            active=self._has_flow_value(
                snapshot.battery_power if snapshot else None,
                snapshot.battery_direction if snapshot else None,
                snapshot.battery_voltage if snapshot else None,
            ),
            animated=self._is_active(snapshot.battery_power if snapshot else None, snapshot.battery_direction if snapshot else None),
            flow_sign=self._flow_sign("battery", snapshot.battery_power if snapshot else None, snapshot.battery_direction if snapshot else None),
            color=self.BATTERY_COLOR,
            flow_layers=battery_layers,
        )
        home_layers = self._home_flow_layers(snapshot)
        self._draw_branch(
            kind="bottom_right",
            start=load_pos,
            end=device_pos,
            value=self._format_branch_value("home", snapshot),
            active=self._has_flow_value(
                snapshot.load_power if snapshot else None,
                snapshot.load_direction if snapshot else None,
                snapshot.load_voltage if snapshot else None,
            ),
            animated=self._is_active(snapshot.load_power if snapshot else None, snapshot.load_direction if snapshot else None),
            flow_sign=self._flow_sign("load", snapshot.load_power if snapshot else None, snapshot.load_direction if snapshot else None),
            color=self.LOAD_COLOR,
            flow_layers=home_layers,
        )

        self._draw_node(pv_pos, tr("PV"), "pv", self.PV_COLOR, faded=False, title_position="above")
        self._add_click_target(QRectF(pv_pos.x() - 42, pv_pos.y() - 56, 84, 84), "pv_node")
        self._draw_node(grid_pos, tr("Grid"), "grid", self.GRID_COLOR, faded=False, title_position="above")
        self._add_click_target(QRectF(grid_pos.x() - 42, grid_pos.y() - 56, 84, 84), "grid_node")
        self._draw_node(
            device_pos,
            "",
            "inv",
            QColor("#67e8f9"),
            title_position="below",
        )
        self._add_click_target(QRectF(device_pos.x() - 46, device_pos.y() - 56, 92, 92), "inverter_node")
        self._draw_device_meta(device_pos, snapshot)
        self._draw_node(battery_pos, tr("Battery"), "battery", self.BATTERY_COLOR, title_position="below")
        self._add_click_target(QRectF(battery_pos.x() - 42, battery_pos.y() - 56, 84, 84), "battery_node")
        self._draw_node(load_pos, tr("Home"), "home", QColor("#38d6ff"), title_position="below")
        self._add_click_target(QRectF(load_pos.x() - 42, load_pos.y() - 56, 84, 84), "home_node")
        self._draw_info_cards(snapshot, battery_pos)
        self._draw_active_tuya_icons_panel(QRectF(896, 344, 196, 132))
        try:
            self._draw_weather_card(QRectF(286, 92, 428, 138), self._weather_snapshot)
            self._log_debug(
                "EnergyFlow weather card rendered | "
                f"weather_present={self._weather_snapshot is not None} | items={len(self._scene.items())}"
            )
        except Exception as exc:
            self._log_debug(f"EnergyFlow weather card render failed: {exc}")
            raise
        self._sync_hover_from_cursor(reason="render")
        self._update_hover_overlay()
        self._apply_view_fit()
        self._log_debug(
            "EnergyFlow scene render complete | "
            f"hover_tag={self._hovered_tag!r} | hover_targets={len(self._hover_target_rects)}"
        )

    def _apply_view_fit(self) -> None:
        scene_rect = self._scene.sceneRect()
        if scene_rect.isNull() or scene_rect.isEmpty():
            return
        self.resetTransform()
        self.fitInView(scene_rect, Qt.AspectRatioMode.KeepAspectRatio)
        transform = self.transform()
        if transform.m11() > 1.0 or transform.m22() > 1.0:
            # Keep a stable 1:1 baseline and avoid visual "zoom-in" when viewport grows.
            self.resetTransform()
            self.centerOn(scene_rect.center())

    def _sync_hover_from_cursor(self, *, reason: str) -> None:
        viewport = self.viewport()
        local_pos = viewport.mapFromGlobal(QCursor.pos())
        if not viewport.rect().contains(local_pos):
            hovered_tag = None
        else:
            hovered_tag = self._tag_at_view_pos(local_pos)
        if hovered_tag == self._hovered_tag:
            return
        self._log_debug(
            "EnergyFlow hover sync from cursor | "
            f"reason={reason} from={self._hovered_tag!r} to={hovered_tag!r} "
            f"pos=({local_pos.x()},{local_pos.y()})"
        )
        self._hovered_tag = hovered_tag
        cursor = (
            Qt.CursorShape.PointingHandCursor
            if self._hovered_tag is not None
            else Qt.CursorShape.ArrowCursor
        )
        viewport.setCursor(cursor)
        self.setCursor(cursor)

    def _draw_info_cards(self, snapshot: EnergyFlowSnapshot | None, battery_pos: QPointF) -> None:
        left_card_x = -93
        right_card_x = 896
        card_y_offset = 0
        pv_card_rect = QRectF(left_card_x, 78 + card_y_offset, 194, 96)
        self._draw_info_card(
            pv_card_rect,
            tr("PV"),
            [
                (tr("Voltage"), self._format_voltage(snapshot.pv_voltage if snapshot else None)),
                (tr("Power"), self._format_power(snapshot.pv_power if snapshot else None)),
            ],
            self.PV_COLOR,
        )
        grid_card_rect = QRectF(right_card_x, 78 + card_y_offset, 196, 96)
        self._draw_info_card(grid_card_rect, tr("Grid"), self._grid_info_rows(snapshot), self.GRID_COLOR)
        battery_card_rect = QRectF(left_card_x, 492 + card_y_offset, 194, 112)
        self._draw_info_card(
            battery_card_rect,
            tr("Battery"),
            [
                (tr("Voltage"), self._format_voltage(snapshot.battery_voltage if snapshot else None)),
                self._battery_eta_row(snapshot),
            ],
            self.BATTERY_COLOR,
        )
        self._draw_battery_soc_indicator(
            QRectF(battery_pos.x() + 81.5, battery_pos.y() - 60, 9.5, 84),
            snapshot.battery_soc if snapshot else None,
            charging=self._is_battery_charging(snapshot) if snapshot else False,
        )
        self._draw_battery_soc_badge(
            QPointF(battery_pos.x() + 86.25, battery_pos.y() + 50),
            snapshot.battery_soc if snapshot else None,
        )
        home_card_rect = QRectF(right_card_x, 488 + card_y_offset, 196, 112)
        self._draw_info_card(
            home_card_rect,
            tr("Home"),
            [
                (tr("Voltage"), self._format_voltage(snapshot.load_voltage if snapshot else None)),
                (tr("Frequency"), self._format_frequency(snapshot.load_frequency if snapshot else None)),
            ],
            QColor("#38d6ff"),
        )

    def _draw_info_card(
        self,
        rect: QRectF,
        title: str,
        rows: list[tuple[str, str] | tuple[str, str, QColor] | tuple[str, str, QColor, QColor]],
        accent: QColor,
    ) -> None:
        center = rect.center()
        self._draw_glow(QPointF(center.x(), center.y()), QColor(accent.red(), accent.green(), accent.blue(), 20), 138)

        outer = QGraphicsRectItem(rect)
        outer.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 120), 1.2))
        outer.setBrush(QColor(8, 16, 30, 220))
        self._scene.addItem(outer)

        inner = QGraphicsRectItem(rect.adjusted(5, 5, -5, -5))
        inner.setPen(QPen(QColor(31, 54, 80, 180), 1.0))
        inner.setBrush(QColor(12, 28, 48, 208))
        self._scene.addItem(inner)

        self._add_text(
            QPointF(rect.left() + 14, rect.top() + 10),
            title,
            size=12,
            weight=500,
            color=QColor("#e6fbff"),
        )

        row_y = rect.top() + 34
        for row in rows:
            if len(row) == 4:
                label, value, label_color, value_color = row
            elif len(row) == 3:
                label, value, value_color = row
                label_color = QColor("#8fb0cb")
            else:
                label, value = row
                label_color = QColor("#8fb0cb")
                value_color = QColor("#f8fafc")
            self._add_text(
                QPointF(rect.left() + 14, row_y),
                label,
                size=12,
                weight=400,
                color=label_color,
            )
            self._add_text(
                QPointF(rect.right() - 14, row_y),
                value,
                size=12,
                weight=500,
                color=value_color,
                anchor="right",
            )
            row_y += 22

    def _draw_weather_card(self, rect: QRectF, weather: WeatherLiveSnapshot | None) -> None:
        self._log_debug(
            "EnergyFlow weather card draw | "
            f"rect={rect.getRect()} | weather_present={weather is not None}"
        )
        metrics = [
            ("temp", tr("Temp"), self._format_temperature(weather.temperature_2m if weather else None), QColor("#fb7185")),
            ("cloud", tr("Cloud"), self._format_percentage(weather.cloud_cover if weather else None), QColor("#93c5fd")),
            ("rain", tr("Rain"), self._format_precipitation(weather.precipitation if weather else None), QColor("#38bdf8")),
            ("wind", tr("Wind"), self._format_wind(weather.wind_speed_10m if weather else None), QColor("#22c55e")),
            ("sun", tr("Sun"), self._format_radiation(weather.shortwave_radiation if weather else None), QColor("#f5cf55")),
        ]

        tile_width = 74.0
        tile_height = 88.0
        gap = 9.0
        total_width = len(metrics) * tile_width + (len(metrics) - 1) * gap
        start_x = rect.left() + (rect.width() - total_width) / 2
        tile_y = rect.top() + 16
        for index, (metric_key, label, value, color) in enumerate(metrics):
            tile_rect = QRectF(start_x + index * (tile_width + gap), tile_y, tile_width, tile_height)
            tile = QGraphicsRectItem(tile_rect)
            tile.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 120), 1.2))
            tile_gradient = QLinearGradient(tile_rect.topLeft(), tile_rect.bottomRight())
            tile_gradient.setColorAt(0.0, QColor(8, 18, 34, 216))
            tile_gradient.setColorAt(1.0, QColor(10, 22, 40, 188))
            tile.setBrush(QBrush(tile_gradient))
            tile.setZValue(60)
            self._scene.addItem(tile)
            self._draw_weather_metric_icon(metric_key, tile_rect, color)
            self._add_click_target(tile_rect, f"weather_{metric_key}", z_value=150)

            value_item = self._add_text(
                QPointF(tile_rect.left() + tile_rect.width() / 2, tile_rect.top() + 64),
                value,
                size=13,
                weight=700,
                color=QColor("#f8fafc"),
                anchor="center",
            )
            value_item.setZValue(65)
        self._log_debug("EnergyFlow weather card tiles created: 5")

    def _draw_active_tuya_icons_panel(self, rect: QRectF) -> None:
        devices = self._active_tuya_icons[:3]
        if not devices:
            return

        icon_size = 34.0
        row_min_height = 40.0
        gap = 5.0
        text_font = QFont(UI_FONT_STACK[0], 10)
        for family in UI_FONT_STACK:
            text_font.setFamilies([family])
            break
        text_font.setWeight(self._font_weight(500))
        text_metrics = QFontMetrics(text_font)
        line_height = max(11, text_metrics.height())

        text_left = rect.left() + 3 + icon_size + 8
        text_width = max(40.0, rect.right() - 6 - text_left)

        def wrap_text(value: str) -> list[str]:
            source = " ".join(str(value).split())
            if not source:
                return [""]

            words = source.split(" ")
            lines: list[str] = []
            current = ""
            for word in words:
                candidate = word if not current else f"{current} {word}"
                if text_metrics.horizontalAdvance(candidate) <= text_width:
                    current = candidate
                    continue
                if current:
                    lines.append(current)
                    current = ""

                if text_metrics.horizontalAdvance(word) <= text_width:
                    current = word
                    continue

                part = ""
                for ch in word:
                    cand = f"{part}{ch}"
                    if part and text_metrics.horizontalAdvance(cand) > text_width:
                        lines.append(part)
                        part = ch
                    else:
                        part = cand
                current = part

            if current:
                lines.append(current)
            return lines or [source]

        rows: list[tuple[str, QPixmap, list[str], float]] = []
        total_height = 0.0
        for name, pixmap in devices:
            wrapped_lines = wrap_text(name)
            text_height = len(wrapped_lines) * line_height
            row_height = max(row_min_height, text_height + 8.0, icon_size + 6.0)
            rows.append((name, pixmap, wrapped_lines, row_height))
            total_height += row_height
        total_height += max(0, len(rows) - 1) * gap

        start_y = rect.top() + max(0.0, (rect.height() - total_height) / 2)
        y = start_y
        for _, pixmap, wrapped_lines, row_height in rows:
            row_rect = QRectF(rect.left(), y, rect.width(), row_height)
            row_bg = QGraphicsRectItem(row_rect)
            row_bg.setPen(QPen(QColor(14, 165, 233, 110), 1.0))
            row_bg.setBrush(QColor(6, 28, 54, 150))
            self._scene.addItem(row_bg)

            icon_y = y + (row_height - icon_size) / 2
            icon_frame_rect = QRectF(rect.left() + 3, icon_y, icon_size, icon_size)
            icon_rect = QRectF(icon_frame_rect.left(), icon_frame_rect.top(), icon_size, icon_size)
            icon_border = QGraphicsRectItem(icon_frame_rect)
            icon_border.setPen(QPen(QColor(226, 232, 240, 190), 1.0))
            icon_border.setBrush(QColor(0, 0, 0, 0))
            self._scene.addItem(icon_border)
            icon_pixmap = pixmap.scaled(
                int(icon_rect.width()),
                int(icon_rect.height()),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_item = self._scene.addPixmap(icon_pixmap)
            icon_item.setPos(
                icon_rect.center().x() - icon_pixmap.width() / 2,
                icon_rect.center().y() - icon_pixmap.height() / 2,
            )
            icon_item.setOpacity(0.95)

            text_block_height = len(wrapped_lines) * line_height
            text_y = y + (row_height - text_block_height) / 2
            for line in wrapped_lines:
                self._add_text(
                    QPointF(icon_frame_rect.right() + 8, text_y),
                    line,
                    size=10,
                    weight=500,
                    color=QColor("#c9def7"),
                    anchor="left",
                )
                text_y += line_height
            y += row_height + gap

    def _battery_eta_row(
        self,
        snapshot: EnergyFlowSnapshot | None,
    ) -> tuple[str, str] | tuple[str, str, QColor, QColor]:
        capability_ah = self._battery_capability_ah
        if capability_ah <= 0.0 or snapshot is None:
            return (tr("ETA"), "--")

        current = snapshot.battery_current
        soc = snapshot.battery_soc
        if current is None or soc is None:
            return (tr("ETA"), "--")

        current_a = abs(float(current))
        soc_pct = max(0.0, min(100.0, float(soc)))
        if current_a < 0.05:
            return (tr("ETA"), tr("Idle"))

        # Determine direction from trusted flow model first, then use current only as rate.
        if self._is_battery_charging(snapshot):
            remaining_ah = capability_ah * (100.0 - soc_pct) / 100.0
            eta_hours = remaining_ah / current_a if current_a > 0 else None
            return (tr("To full"), self._format_duration_hours(eta_hours), QColor("#8fb0cb"), QColor("#22c55e"))

        if self._is_battery_discharging(snapshot):
            remaining_ah = capability_ah * soc_pct / 100.0
            eta_hours = remaining_ah / current_a if current_a > 0 else None
            return (tr("To empty"), self._format_duration_hours(eta_hours), QColor("#8fb0cb"), QColor("#ef4444"))

        return (tr("ETA"), tr("Idle"))

    def _format_duration_hours(self, value: float | None) -> str:
        if value is None or not math.isfinite(value):
            return "--"
        minutes_total = max(0, int(round(value * 60)))
        hours, minutes = divmod(minutes_total, 60)
        if hours >= 100:
            return "99 h+"
        if hours <= 0:
            return f"{minutes} {tr('m')}"
        if minutes == 0:
            return f"{hours} {tr('h')}"
        return f"{hours} {tr('h')} {minutes} {tr('m')}"

    def _draw_weather_metric_icon(self, metric_key: str, tile_rect: QRectF, color: QColor) -> None:
        center_x = tile_rect.center().x()
        icon_top = tile_rect.top() + 12
        line_pen = QPen(color, 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        glow_pen = QPen(QColor(color.red(), color.green(), color.blue(), 100), 5.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        def add_path(path: QPainterPath) -> None:
            glow = QGraphicsPathItem(path)
            glow.setPen(glow_pen)
            glow.setZValue(62)
            self._scene.addItem(glow)
            item = QGraphicsPathItem(path)
            item.setPen(line_pen)
            item.setZValue(63)
            self._scene.addItem(item)

        if metric_key == "temp":
            stem_x = center_x
            bulb_left = center_x - 5
            bulb = QGraphicsEllipseItem(bulb_left, icon_top + 18, 10, 10)
            bulb.setPen(glow_pen)
            bulb.setZValue(62)
            self._scene.addItem(bulb)
            bulb_inner = QGraphicsEllipseItem(bulb_left, icon_top + 18, 10, 10)
            bulb_inner.setPen(line_pen)
            bulb_inner.setZValue(63)
            self._scene.addItem(bulb_inner)
            stem = QPainterPath(QPointF(stem_x, icon_top))
            stem.lineTo(stem_x, icon_top + 24)
            stem.moveTo(stem_x - 4, icon_top + 4)
            stem.lineTo(stem_x + 4, icon_top + 4)
            add_path(stem)
            return

        if metric_key == "cloud":
            icon_left = center_x - 17
            path = QPainterPath()
            path.moveTo(icon_left, icon_top + 20)
            path.cubicTo(icon_left + 3, icon_top + 12, icon_left + 10, icon_top + 11, icon_left + 14, icon_top + 15)
            path.cubicTo(icon_left + 15, icon_top + 8, icon_left + 24, icon_top + 8, icon_left + 27, icon_top + 15)
            path.cubicTo(icon_left + 32, icon_top + 15, icon_left + 35, icon_top + 18, icon_left + 35, icon_top + 20)
            path.lineTo(icon_left, icon_top + 20)
            add_path(path)
            return

        if metric_key == "rain":
            icon_left = center_x - 17
            cloud = QPainterPath()
            cloud.moveTo(icon_left, icon_top + 14)
            cloud.cubicTo(icon_left + 3, icon_top + 8, icon_left + 9, icon_top + 8, icon_left + 13, icon_top + 12)
            cloud.cubicTo(icon_left + 14, icon_top + 4, icon_left + 23, icon_top + 4, icon_left + 26, icon_top + 12)
            cloud.cubicTo(icon_left + 31, icon_top + 12, icon_left + 34, icon_top + 14, icon_left + 34, icon_top + 16)
            cloud.lineTo(icon_left, icon_top + 16)
            add_path(cloud)
            for drop_x in (icon_left + 9, icon_left + 18, icon_left + 27):
                drop = QPainterPath(QPointF(drop_x, icon_top + 22))
                drop.lineTo(drop_x - 3, icon_top + 30)
                add_path(drop)
            return

        if metric_key == "wind":
            icon_left = center_x - 16
            for row, width in ((0, 24), (8, 31), (16, 21)):
                path = QPainterPath(QPointF(icon_left, icon_top + row + 6))
                path.cubicTo(icon_left + width * 0.35, icon_top + row + 1, icon_left + width * 0.75, icon_top + row + 8, icon_left + width, icon_top + row + 3)
                add_path(path)
            return

        if metric_key == "sun":
            sun_left = center_x - 6
            sun = QGraphicsEllipseItem(sun_left, icon_top + 6, 12, 12)
            sun.setPen(glow_pen)
            sun.setZValue(62)
            self._scene.addItem(sun)
            sun_inner = QGraphicsEllipseItem(sun_left, icon_top + 6, 12, 12)
            sun_inner.setPen(line_pen)
            sun_inner.setZValue(63)
            self._scene.addItem(sun_inner)
            for dx, dy in ((0, -8), (0, 18), (-8, 0), (18, 0), (-6, -6), (16, -6), (-6, 16), (16, 16)):
                ray = QPainterPath(QPointF(center_x, icon_top + 12))
                ray.lineTo(center_x + dx, icon_top + 12 + dy)
                add_path(ray)

    def _grid_info_rows(
        self,
        snapshot: EnergyFlowSnapshot | None,
    ) -> list[tuple[str, str] | tuple[str, str, QColor, QColor]]:
        grid_voltage = snapshot.grid_voltage if snapshot else None
        grid_frequency = snapshot.grid_frequency if snapshot else None
        is_offline = grid_voltage is None or abs(float(grid_voltage)) < 1.0
        if is_offline:
            return [(tr("Status"), tr("Offline"), QColor("#8fb0cb"), QColor("#4f647c"))]
        return [
            (tr("Voltage"), self._format_voltage(grid_voltage)),
            (tr("Frequency"), self._format_frequency(grid_frequency)),
        ]

    def _draw_battery_soc_indicator(self, rect: QRectF, value: float | None, *, charging: bool = False) -> None:
        outer = QGraphicsRectItem(rect)
        outer.setPen(QPen(QColor("#35506d"), 1.2))
        outer.setBrush(QColor(12, 22, 38, 220))
        self._scene.addItem(outer)

        inner_rect = rect.adjusted(3, 3, -3, -3)
        track = QGraphicsRectItem(inner_rect)
        track.setPen(QPen(Qt.PenStyle.NoPen))
        track.setBrush(QColor(18, 36, 58, 220))
        self._scene.addItem(track)

        if value is None:
            return

        fill_ratio = max(0.0, min(100.0, float(value))) / 100.0
        fill_height = inner_rect.height() * fill_ratio
        fill_rect = QRectF(inner_rect.left(), inner_rect.bottom() - fill_height, inner_rect.width(), fill_height)
        fill = QGraphicsRectItem(fill_rect)
        fill.setPen(QPen(Qt.PenStyle.NoPen))
        fill.setBrush(self._battery_soc_color(value))
        self._scene.addItem(fill)

        if not charging or fill_height < 10.0:
            pulse = 0.0
        else:
            cycle = (math.sin(self._animation_phase * 0.12) + 1.0) / 2.0
            pulse = (math.sin(self._animation_phase * 0.7) + 1.0) / 2.0

            outline = QGraphicsRectItem(rect.adjusted(-1.0, -1.0, 1.0, 1.0))
            outline.setPen(QPen(QColor(125, 250, 255, 120 + int(90 * pulse)), 1.8))
            outline.setBrush(Qt.BrushStyle.NoBrush)
            self._scene.addItem(outline)

            accent = self._battery_soc_color(value).lighter(150)
            column_height = max(26.0, min(inner_rect.height() * 0.62, 54.0))
            travel = max(0.0, inner_rect.height() + column_height)
            column_top = inner_rect.bottom() - column_height - travel * cycle
            moving_column = QRectF(
                inner_rect.left() + 0.8,
                column_top,
                max(1.0, inner_rect.width() - 1.6),
                column_height,
            ).intersected(fill_rect.adjusted(0.0, 0.0, 0.0, 0.0))
            if not moving_column.isEmpty():
                column = QGraphicsRectItem(moving_column)
                column.setPen(QPen(Qt.PenStyle.NoPen))
                gradient = QLinearGradient(
                    moving_column.center().x(),
                    moving_column.bottom(),
                    moving_column.center().x(),
                    moving_column.top(),
                )
                gradient.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
                gradient.setColorAt(0.28, QColor(accent.red(), accent.green(), accent.blue(), 145))
                gradient.setColorAt(0.68, QColor(accent.red(), accent.green(), accent.blue(), 235))
                gradient.setColorAt(1.0, QColor(235, 250, 255, 120))
                column.setBrush(QBrush(gradient))
                self._scene.addItem(column)

                core_rect = moving_column.adjusted(2.0, 4.0, -2.0, -4.0)
                if not core_rect.isEmpty():
                    core = QGraphicsRectItem(core_rect)
                    core.setPen(QPen(Qt.PenStyle.NoPen))
                    core_gradient = QLinearGradient(
                        core_rect.center().x(),
                        core_rect.bottom(),
                        core_rect.center().x(),
                        core_rect.top(),
                    )
                    core_gradient.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
                    core_gradient.setColorAt(0.35, QColor(accent.red(), accent.green(), accent.blue(), 185))
                    core_gradient.setColorAt(1.0, QColor(245, 252, 255, 180))
                    core.setBrush(QBrush(core_gradient))
                    self._scene.addItem(core)

    def _draw_battery_soc_badge(self, center: QPointF, value: float | None) -> None:
        if value is None:
            return

        percent_text = self._format_percent(value)
        accent_color = self._battery_soc_color(value)
        shadow = self._add_text(
            QPointF(center.x() + 0.8, center.y() - 3.0 + 0.9),
            percent_text,
            size=17,
            weight=400,
            color=QColor(3, 10, 18, 220),
            anchor="center",
        )
        glow_outer = self._add_text(
            QPointF(center.x(), center.y() - 3.0),
            percent_text,
            size=17,
            weight=400,
            color=QColor(accent_color.red(), accent_color.green(), accent_color.blue(), 72),
            anchor="center",
        )
        glow_inner = self._add_text(
            QPointF(center.x(), center.y() - 3.0),
            percent_text,
            size=17,
            weight=400,
            color=QColor(accent_color.red(), accent_color.green(), accent_color.blue(), 118),
            anchor="center",
        )
        label = self._add_text(
            QPointF(center.x(), center.y() - 3.0),
            percent_text,
            size=17,
            weight=400,
            color=accent_color,
            anchor="center",
        )
        shadow.setZValue(18)
        glow_outer.setZValue(19)
        glow_inner.setZValue(20)
        label.setZValue(21)

    def _battery_soc_color(self, value: float | None) -> QColor:
        if value is None:
            return QColor("#f8fafc")

        normalized = max(0.0, min(100.0, float(value)))
        if normalized >= 60.0:
            return QColor("#22c55e")
        if normalized >= 30.0:
            return QColor("#facc15")
        return QColor("#ef4444")

    def _draw_device_meta(self, center: QPointF, snapshot: EnergyFlowSnapshot | None) -> None:
        meta_rect = QRectF(center.x() - 166, center.y() + 88, 332, 104)
        outer = QGraphicsRectItem(meta_rect)
        outer.setPen(QPen(QColor(103, 232, 249, 110), 1.2))
        outer.setBrush(QColor(8, 16, 30, 205))
        self._scene.addItem(outer)

        inner = QGraphicsRectItem(meta_rect.adjusted(5, 5, -5, -5))
        inner.setPen(QPen(QColor("#1f3650"), 1.0))
        inner.setBrush(QColor(12, 28, 48, 196))
        self._scene.addItem(inner)

        self._add_text(
            QPointF(center.x(), meta_rect.top() + 22),
            self._device_title,
            size=18,
            weight=500,
            color=QColor("#f8fafc"),
            anchor="center",
        )

        pn = self._format_identifier(self._device_pn)
        sn = self._format_identifier(self._device_sn)
        self._add_text(
            QPointF(center.x(), meta_rect.top() + 50),
            f"{tr('PN')} {pn}",
            size=12,
            weight=400,
            color=QColor("#c6f7ff"),
            anchor="center",
        )
        self._add_text(
            QPointF(center.x(), meta_rect.top() + 72),
            f"{tr('SN')} {sn}",
            size=12,
            weight=400,
            color=QColor("#c6f7ff"),
            anchor="center",
        )

    def _draw_glow(self, center: QPointF, color: QColor, size: float) -> None:
        glow = QGraphicsRectItem(center.x() - size / 2, center.y() - size / 2, size, size)
        gradient = QLinearGradient(center.x(), center.y() - size / 2, center.x(), center.y() + size / 2)
        gradient.setColorAt(0.0, color)
        gradient.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        glow.setBrush(QBrush(gradient))
        glow.setPen(QPen(Qt.PenStyle.NoPen))
        self._scene.addItem(glow)

    def _draw_status_badge(self, center: QPointF, text: str) -> None:
        badge_width = 290
        badge_height = 46
        badge_rect = QRectF(
            center.x() - badge_width / 2,
            center.y() - badge_height / 2,
            badge_width,
            badge_height,
        )

        outer = QGraphicsRectItem(badge_rect)
        outer.setPen(QPen(QColor("#3cc8f4"), 1.2))
        outer.setBrush(QColor(8, 16, 30, 228))
        self._scene.addItem(outer)

        inner_rect = badge_rect.adjusted(4, 4, -4, -4)
        inner = QGraphicsRectItem(inner_rect)
        inner.setPen(QPen(QColor("#1f3650"), 1.0))
        inner.setBrush(QColor(12, 26, 44, 220))
        self._scene.addItem(inner)

        prefix = text
        timer = ""
        next_refresh_prefix = tr("Next refresh")
        if text.startswith(next_refresh_prefix + " ") or text.startswith("Next refresh "):
            prefix = tr("NEXT REFRESH")
            timer = text
            if timer.startswith(next_refresh_prefix + " "):
                timer = timer.removeprefix(next_refresh_prefix + " ").strip()
            else:
                timer = timer.removeprefix("Next refresh ").strip()

        if timer:
            self._add_text(
                QPointF(center.x() - 62, center.y()),
                prefix,
                size=11,
                weight=700,
                color=QColor("#89a5c7"),
                anchor="center",
            )
            self._add_text(
                QPointF(center.x() + 74, center.y()),
                timer,
                size=18,
                weight=800,
                color=QColor("#67e8f9"),
                anchor="center",
            )
        else:
            self._add_text(
                center,
                text,
                size=13,
                weight=700,
                color=QColor("#d9f7ff"),
                anchor="center",
            )

    def _draw_branch(
        self,
        *,
        kind: str,
        start: QPointF,
        end: QPointF,
        value: str,
        active: bool,
        animated: bool,
        flow_sign: int,
        color: QColor,
        flow_layers: list[tuple[QColor, int]] | None = None,
    ) -> None:
        path = QPainterPath()
        text_pos = QPointF()
        upper_lane = end.y() - 32
        lower_lane = end.y() + 32
        left_entry_x = end.x() - 88
        right_entry_x = end.x() + 88
        if kind == "top_left":
            path.moveTo(start.x(), start.y() + 42)
            path.lineTo(start.x(), upper_lane - 28)
            path.quadTo(start.x(), upper_lane, start.x() + 28, upper_lane)
            path.lineTo(left_entry_x, upper_lane)
            text_pos = QPointF((start.x() + left_entry_x) / 2, upper_lane - 54)
        elif kind == "top_right":
            path.moveTo(start.x(), start.y() + 42)
            path.lineTo(start.x(), upper_lane - 28)
            path.quadTo(start.x(), upper_lane, start.x() - 28, upper_lane)
            path.lineTo(right_entry_x, upper_lane)
            text_pos = QPointF((start.x() + right_entry_x) / 2, upper_lane - 54)
        elif kind == "bottom_left":
            path.moveTo(start.x(), start.y() - 42)
            path.lineTo(start.x(), lower_lane + 28)
            path.quadTo(start.x(), lower_lane, start.x() + 28, lower_lane)
            path.lineTo(left_entry_x, lower_lane)
            text_pos = QPointF((start.x() + left_entry_x) / 2, lower_lane + 28)
        else:
            path.moveTo(start.x(), start.y() - 42)
            path.lineTo(start.x(), lower_lane + 28)
            path.quadTo(start.x(), lower_lane, start.x() - 28, lower_lane)
            path.lineTo(right_entry_x, lower_lane)
            text_pos = QPointF((start.x() + right_entry_x) / 2, lower_lane + 28)

        layers = flow_layers or []
        accent_color = color
        value_color = color if active else QColor("#d8dee6")
        if active and layers:
            if len(layers) == 1:
                accent_color = layers[0][0]
                value_color = layers[0][0]
            else:
                accent_color = QColor("#42566f")
                value_color = QColor("#f8fafc")

        pipe = QGraphicsPathItem(path)
        pipe.setPen(QPen(QColor("#1f2a38"), 24, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        self._scene.addItem(pipe)

        accent_pen = QPen(accent_color if active else QColor("#16202d"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        highlight = QGraphicsPathItem(path)
        highlight.setPen(accent_pen)
        self._scene.addItem(highlight)

        if animated:
            self._draw_flow_layers(path, layers or [(color, flow_sign)])

        self._add_text(
            text_pos,
            value,
            size=24,
            weight=700,
            color=value_color,
            anchor="center",
        )

    def _draw_flow_layers(self, path: QPainterPath, layers: list[tuple[QColor, int]]) -> None:
        if not layers:
            return

        offsets = self._lane_offsets(len(layers))
        for (layer_color, layer_sign), offset in zip(layers, offsets):
            layer_pen = QPen(
                layer_color,
                5.0 if len(layers) > 1 else 10.0,
                Qt.PenStyle.CustomDashLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            layer_pen.setDashPattern([1.2, 2.2])
            layer_pen.setDashOffset(self._animation_phase * float(layer_sign))
            flow = QGraphicsPathItem(path)
            flow.setPen(layer_pen)
            if abs(offset) > 0.01:
                flow.setPos(offset, 0)
            self._scene.addItem(flow)

    def _lane_offsets(self, count: int) -> list[float]:
        if count <= 1:
            return [0.0]
        if count == 2:
            return [-4.0, 4.0]
        if count == 3:
            return [-6.0, 0.0, 6.0]
        step = 4.0
        center = (count - 1) / 2.0
        return [(index - center) * step for index in range(count)]

    def _home_flow_layers(self, snapshot: EnergyFlowSnapshot | None) -> list[tuple[QColor, int]] | None:
        if snapshot is None:
            return None

        layers: list[tuple[QColor, int]] = []
        if self._is_positive_power(snapshot.pv_to_home_power):
            layers.append((self.PV_COLOR, 1))
        if self._is_positive_power(snapshot.grid_to_home_power):
            layers.append((self.GRID_COLOR, 1))
        if self._is_battery_discharging(snapshot):
            layers.append((self.BATTERY_COLOR, 1))
        return layers or None

    def _battery_flow_layers(self, snapshot: EnergyFlowSnapshot | None) -> list[tuple[QColor, int]] | None:
        if snapshot is None:
            return None

        if self._is_battery_charging(snapshot):
            layers: list[tuple[QColor, int]] = []
            if self._is_positive_power(snapshot.pv_to_battery_power):
                layers.append((self.PV_COLOR, 1))
            if self._is_positive_power(snapshot.grid_to_battery_power):
                layers.append((self.GRID_COLOR, 1))
            return layers or [(self.BATTERY_COLOR, 1)]

        if self._is_battery_discharging(snapshot):
            return [(self.BATTERY_COLOR, -1)]
        return None

    def _draw_node(
        self,
        center: QPointF,
        title: str,
        icon_kind: str,
        accent: QColor,
        *,
        faded: bool = False,
        title_position: str = "below",
    ) -> None:
        self._draw_neon_pedestal(center, accent, faded=faded)
        self._draw_icon_art(center, icon_kind, accent, faded=faded)
        if title:
            if title_position == "above":
                label_y = center.y() - 96
            else:
                label_y = center.y() + 104
                if center.y() < 300:
                    label_y = center.y() + 72
                elif center.y() > 500:
                    label_y = center.y() + 88

            self._add_text(
                QPointF(center.x(), label_y),
                title,
                size=18,
                weight=500,
                color=QColor("#f8fafc" if not faded else "#a6b0bd"),
                anchor="center",
            )

    def _add_click_target(self, rect: QRectF, tag: str, *, z_value: float = 100) -> None:
        self._hover_target_rects[tag] = QRectF(rect)
        if tag in self._hover_target_order:
            self._hover_target_order.remove(tag)
        self._hover_target_order.append(tag)

    def _draw_click_target_hover(self, rect: QRectF, tag: str) -> list[QGraphicsItem]:
        color = self._hover_color_for_tag(tag)
        if tag.startswith("weather_"):
            # Keep weather hover glow strictly inside the tile bounds.
            outer = QGraphicsRectItem(rect.adjusted(1.0, 1.0, -1.0, -1.0))
            outer.setPen(QPen(Qt.PenStyle.NoPen))
            outer.setBrush(QColor(color.red(), color.green(), color.blue(), 24))
            outer.setZValue(95)
            self._scene.addItem(outer)

            inner = QGraphicsRectItem(rect.adjusted(5.0, 5.0, -5.0, -5.0))
            inner.setPen(QPen(Qt.PenStyle.NoPen))
            inner.setBrush(QColor(color.red(), color.green(), color.blue(), 12))
            inner.setZValue(96)
            self._scene.addItem(inner)
            return [outer, inner]

        center = rect.center()
        base_diameter = max(rect.width(), rect.height())
        halo_diameter = base_diameter + 24.0
        inner_glow_diameter = base_diameter + 10.0

        halo = QGraphicsEllipseItem(
            center.x() - halo_diameter / 2,
            center.y() - halo_diameter / 2,
            halo_diameter,
            halo_diameter,
        )
        halo.setPen(QPen(Qt.PenStyle.NoPen))
        halo.setBrush(QColor(color.red(), color.green(), color.blue(), 22))
        halo.setZValue(95)
        self._scene.addItem(halo)

        inner_glow = QGraphicsEllipseItem(
            center.x() - inner_glow_diameter / 2,
            center.y() - inner_glow_diameter / 2,
            inner_glow_diameter,
            inner_glow_diameter,
        )
        inner_glow.setPen(QPen(Qt.PenStyle.NoPen))
        inner_glow.setBrush(QColor(color.red(), color.green(), color.blue(), 12))
        inner_glow.setZValue(96)
        self._scene.addItem(inner_glow)
        return [halo, inner_glow]

    def _clear_hover_overlay(self) -> None:
        for item in self._hover_overlay_items:
            try:
                if shiboken6.isValid(item):
                    self._scene.removeItem(item)
            except Exception:
                LOGGER.exception("Failed to remove hover overlay item from EnergyFlow scene")
        self._hover_overlay_items.clear()

    def _hover_rect_for_tag(self, tag: str) -> QRectF | None:
        rect = self._hover_target_rects.get(tag)
        return QRectF(rect) if rect is not None else None

    def _update_hover_overlay(self) -> None:
        self._clear_hover_overlay()
        if not self._hovered_tag:
            return
        rect = self._hover_rect_for_tag(self._hovered_tag)
        if rect is None:
            self._sync_hover_from_cursor(reason="missing_rect")
            if not self._hovered_tag:
                return
            rect = self._hover_rect_for_tag(self._hovered_tag)
        if rect is None:
            self._log_debug(
                "EnergyFlow hover missing target rect | "
                f"tag={self._hovered_tag!r} | known_targets={len(self._hover_target_rects)}"
            )
            return
        self._log_debug(
            "EnergyFlow hover draw overlay | "
            f"tag={self._hovered_tag!r} | rect={rect.getRect()}"
        )
        self._hover_overlay_items.extend(self._draw_click_target_hover(rect, self._hovered_tag))

    def _hover_color_for_tag(self, tag: str) -> QColor:
        if tag == "weather_temp":
            return QColor("#fb7185")
        if tag == "weather_cloud":
            return QColor("#93c5fd")
        if tag == "weather_rain":
            return QColor("#38bdf8")
        if tag == "weather_wind":
            return QColor("#22c55e")
        if tag == "weather_sun":
            return QColor("#f5cf55")
        if tag == "pv_node":
            return self.PV_COLOR
        if tag == "grid_node":
            return self.GRID_COLOR
        if tag == "battery_node":
            return self.BATTERY_COLOR
        if tag == "home_node":
            return QColor("#38d6ff")
        return QColor("#67e8f9")

    def _draw_neon_pedestal(self, center: QPointF, accent: QColor, *, faded: bool) -> None:
        base = QPolygonF(
            [
                QPointF(center.x(), center.y() + 58),
                QPointF(center.x() + 68, center.y() + 18),
                QPointF(center.x(), center.y() - 20),
                QPointF(center.x() - 68, center.y() + 18),
            ]
        )
        base_item = QGraphicsPolygonItem(base)
        base_item.setPen(QPen(QColor(255, 255, 255, 28 if not faded else 10), 1.4))
        base_gradient = QLinearGradient(center.x(), center.y() - 18, center.x(), center.y() + 58)
        base_gradient.setColorAt(0.0, QColor(255, 255, 255, 90 if not faded else 16))
        base_gradient.setColorAt(1.0, QColor(120, 132, 150, 32 if not faded else 10))
        base_item.setBrush(QBrush(base_gradient))
        self._scene.addItem(base_item)

        inner = QPolygonF(
            [
                QPointF(center.x(), center.y() + 34),
                QPointF(center.x() + 42, center.y() + 10),
                QPointF(center.x(), center.y() - 12),
                QPointF(center.x() - 42, center.y() + 10),
            ]
        )
        inner_item = QGraphicsPolygonItem(inner)
        inner_item.setPen(QPen(Qt.PenStyle.NoPen))
        inner_item.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), 64 if not faded else 18)))
        self._scene.addItem(inner_item)

    def _draw_icon_art(self, center: QPointF, icon_kind: str, accent: QColor, *, faded: bool) -> None:
        opacity = 0.42 if faded else 1.0
        line_color = QColor("#dffcff" if not faded else "#5f7188")
        line_pen = QPen(line_color, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        glow_pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 110 if not faded else 24), 6.0)

        def add_rect(x: float, y: float, w: float, h: float, fill: QColor) -> None:
            item = QGraphicsRectItem(x, y, w, h)
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setBrush(QBrush(fill))
            item.setOpacity(opacity)
            self._scene.addItem(item)

        if icon_kind == "pv":
            panel = QGraphicsRectItem(center.x() - 28, center.y() - 44, 56, 30)
            panel.setPen(glow_pen)
            panel.setBrush(QBrush(QColor(105, 227, 255, 36)))
            panel.setOpacity(opacity)
            self._scene.addItem(panel)
            panel.setPen(line_pen)
            for offset in (-12, 0, 12):
                grid = QGraphicsPathItem()
                path = QPainterPath(QPointF(center.x() + offset, center.y() - 44))
                path.lineTo(center.x() + offset, center.y() - 14)
                grid.setPath(path)
                grid.setPen(line_pen)
                grid.setOpacity(opacity)
                self._scene.addItem(grid)
            for offset in (-34, -24, -14, -4):
                grid = QGraphicsPathItem()
                path = QPainterPath(QPointF(center.x() + offset, center.y() - 29))
                path.lineTo(center.x() + offset + 56, center.y() - 29)
                grid.setPath(path)
                grid.setPen(line_pen)
                grid.setOpacity(opacity)
                self._scene.addItem(grid)
            stem = QGraphicsPathItem()
            path = QPainterPath(QPointF(center.x(), center.y() - 14))
            path.lineTo(center.x(), center.y() + 4)
            path.moveTo(center.x() - 10, center.y() + 8)
            path.lineTo(center.x() + 10, center.y() + 8)
            stem.setPath(path)
            stem.setPen(line_pen)
            stem.setOpacity(opacity)
            self._scene.addItem(stem)
            return

        if icon_kind == "battery":
            add_rect(center.x() - 24, center.y() - 42, 48, 58, QColor(56, 214, 255, 36))
            body = QGraphicsRectItem(center.x() - 24, center.y() - 42, 48, 58)
            body.setPen(line_pen)
            body.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            body.setOpacity(opacity)
            self._scene.addItem(body)
            cap = QGraphicsRectItem(center.x() - 8, center.y() - 50, 16, 8)
            cap.setPen(line_pen)
            cap.setBrush(QBrush(QColor(56, 214, 255, 60 if not faded else 18)))
            cap.setOpacity(opacity)
            self._scene.addItem(cap)
            for y in (-30, -16, -2, 12):
                line = QGraphicsPathItem()
                path = QPainterPath(QPointF(center.x() - 18, center.y() + y))
                path.lineTo(center.x() + 18, center.y() + y)
                line.setPath(path)
                line.setPen(QPen(QColor(90, 228, 255, 200 if not faded else 70), 2))
                line.setOpacity(opacity)
                self._scene.addItem(line)
            bolt = QGraphicsPathItem()
            path = QPainterPath(QPointF(center.x() + 4, center.y() - 26))
            path.lineTo(center.x() - 3, center.y() - 2)
            path.lineTo(center.x() + 6, center.y() - 2)
            path.lineTo(center.x() - 2, center.y() + 18)
            bolt.setPath(path)
            bolt.setPen(QPen(QColor("#f8fafc"), 3))
            bolt.setOpacity(opacity)
            self._scene.addItem(bolt)
            return

        if icon_kind == "inv":
            add_rect(center.x() - 28, center.y() - 44, 56, 60, QColor(103, 232, 249, 40))
            body = QGraphicsRectItem(center.x() - 28, center.y() - 44, 56, 60)
            body.setPen(line_pen)
            body.setBrush(QBrush(QColor(103, 232, 249, 22)))
            body.setOpacity(opacity)
            self._scene.addItem(body)
            leg_left = QGraphicsPathItem()
            path = QPainterPath(QPointF(center.x() - 14, center.y() + 16))
            path.lineTo(center.x() - 14, center.y() + 22)
            path.moveTo(center.x() + 14, center.y() + 16)
            path.lineTo(center.x() + 14, center.y() + 22)
            leg_left.setPath(path)
            leg_left.setPen(line_pen)
            leg_left.setOpacity(opacity)
            self._scene.addItem(leg_left)
            wave = QGraphicsPathItem()
            path = QPainterPath(QPointF(center.x() - 18, center.y() - 12))
            path.lineTo(center.x() - 4, center.y() - 12)
            path.moveTo(center.x() + 4, center.y() - 6)
            path.cubicTo(center.x() + 10, center.y() - 16, center.x() + 14, center.y() - 2, center.x() + 20, center.y() - 10)
            wave.setPath(path)
            wave.setPen(line_pen)
            wave.setOpacity(opacity)
            self._scene.addItem(wave)
            slash = QGraphicsPathItem()
            path = QPainterPath(QPointF(center.x() - 2, center.y() - 20))
            path.lineTo(center.x() - 12, center.y() + 2)
            slash.setPath(path)
            slash.setPen(line_pen)
            slash.setOpacity(opacity)
            self._scene.addItem(slash)
            return

        if icon_kind == "home":
            roof = QGraphicsPathItem()
            path = QPainterPath(QPointF(center.x() - 28, center.y() - 8))
            path.lineTo(center.x(), center.y() - 36)
            path.lineTo(center.x() + 28, center.y() - 8)
            roof.setPath(path)
            roof.setPen(line_pen)
            roof.setOpacity(opacity)
            self._scene.addItem(roof)
            house = QGraphicsRectItem(center.x() - 20, center.y() - 8, 40, 30)
            house.setPen(line_pen)
            house.setBrush(QBrush(QColor(56, 214, 255, 26)))
            house.setOpacity(opacity)
            self._scene.addItem(house)
            door = QGraphicsRectItem(center.x() - 5, center.y() + 4, 10, 18)
            door.setPen(line_pen)
            door.setBrush(QBrush(QColor("#f8fafc")))
            door.setOpacity(opacity)
            self._scene.addItem(door)
            for x in (-13, 7):
                win = QGraphicsRectItem(center.x() + x, center.y(), 6, 6)
                win.setPen(QPen(Qt.PenStyle.NoPen))
                win.setBrush(QBrush(QColor("#ffe36b")))
                win.setOpacity(opacity)
                self._scene.addItem(win)
            return

        tower = QGraphicsPathItem()
        path = QPainterPath(QPointF(center.x(), center.y() - 48))
        path.lineTo(center.x() - 18, center.y() - 26)
        path.lineTo(center.x() - 10, center.y() - 26)
        path.lineTo(center.x() - 4, center.y() + 16)
        path.lineTo(center.x() + 4, center.y() + 16)
        path.lineTo(center.x() + 10, center.y() - 26)
        path.lineTo(center.x() + 18, center.y() - 26)
        path.closeSubpath()
        tower.setPath(path)
        tower.setPen(line_pen)
        tower.setOpacity(opacity)
        self._scene.addItem(tower)
        cross = QGraphicsPathItem()
        path = QPainterPath(QPointF(center.x() - 22, center.y() - 28))
        path.lineTo(center.x() + 22, center.y() - 28)
        path.moveTo(center.x() - 18, center.y() - 10)
        path.lineTo(center.x() + 18, center.y() - 10)
        path.moveTo(center.x() - 10, center.y() - 42)
        path.lineTo(center.x() + 10, center.y() - 42)
        cross.setPath(path)
        cross.setPen(line_pen)
        cross.setOpacity(opacity)
        self._scene.addItem(cross)

    def _add_text(
        self,
        pos: QPointF,
        text: str,
        *,
        size: int,
        weight: int,
        color: QColor,
        anchor: str = "left",
    ) -> QGraphicsSimpleTextItem:
        item = QGraphicsSimpleTextItem(text)
        font = QFont()
        font.setFamilies(UI_FONT_STACK)
        font.setPointSize(size)
        font.setWeight(self._font_weight(weight))
        item.setFont(font)
        item.setBrush(QBrush(color))
        rect = item.boundingRect()
        if anchor == "center":
            item.setPos(pos.x() - rect.width() / 2, pos.y() - rect.height() / 2)
        elif anchor == "right":
            item.setPos(pos.x() - rect.width(), pos.y())
        else:
            item.setPos(pos)
        self._scene.addItem(item)
        return item

    def _log_debug(self, message: str) -> None:
        _ = message

    def _format_power(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{int(round(value))} {tr('W')}"

    def _format_temperature(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{int(round(value))} {tr('C')}"

    def _format_percentage(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{int(round(value))}%"

    def _format_precipitation(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{value:.1f} {tr('mm')}"

    def _format_wind(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{value:.1f} {tr('m/s')}"

    def _format_radiation(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{int(round(value))} {tr('W/m2')}"

    def _format_weather_time(self, value: str) -> str:
        text = str(value).strip()
        if len(text) >= 16:
            return text[11:16]
        return "--:--"

    def _format_branch_value(self, branch: str, snapshot: EnergyFlowSnapshot | None) -> str:
        if snapshot is None:
            return "--"
        if branch == "pv":
            return self._format_power(snapshot.pv_power)
        if branch == "grid":
            return self._format_power(snapshot.grid_power)
        if branch == "home":
            return self._format_power(snapshot.load_power)
        if branch == "battery":
            power = snapshot.battery_power
            if power is None:
                return "--"
            return self._format_power(abs(power))
        return "--"

    def _format_voltage(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{value:.1f} {tr('V')}"

    def _format_frequency(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{value:.2f} {tr('Hz')}"

    def _format_percent(self, value: float | None) -> str:
        if value is None:
            return "--"
        return f"{int(round(value))}%"

    def _format_identifier(self, value: str | None) -> str:
        text = str(value or "").strip()
        return text or "--"

    def _is_active(self, value: float | None, direction: int | None) -> bool:
        if value is not None:
            return abs(value) >= 1.0
        if direction is not None:
            return direction != 0
        return False

    def _has_flow_value(self, value: float | None, direction: int | None, voltage: float | None) -> bool:
        if value is not None:
            return abs(float(value)) >= 1.0
        value_is_zeroish = value is None or abs(float(value)) < 1.0
        voltage_is_zeroish = voltage is not None and abs(float(voltage)) < 1.0
        if value_is_zeroish and voltage_is_zeroish:
            return False
        return value is not None or direction is not None

    def _has_active_flow(self, snapshot: EnergyFlowSnapshot) -> bool:
        return bool(self._home_flow_layers(snapshot) or self._battery_flow_layers(snapshot)) or any(
            self._is_active(value, direction)
            for value, direction in (
                (snapshot.pv_power, snapshot.pv_direction),
                (snapshot.grid_power, snapshot.grid_direction),
                (snapshot.battery_power, snapshot.battery_direction),
                (snapshot.load_power, snapshot.load_direction),
            )
        )

    def _flow_sign(self, branch: str, value: float | None, direction: int | None) -> int:
        if branch == "pv":
            return -1
        if branch == "load":
            return 1
        if value is not None:
            if branch == "battery":
                # Battery uses signed power:
                # negative = Battery -> Inverter, positive = Inverter -> Battery
                return 1 if value > 0 else -1
        if direction is not None:
            if branch == "battery":
                return 1 if direction <= 0 else -1
            return 1 if direction > 0 else -1
        return 1

    def _grid_flow_sign(self, snapshot: EnergyFlowSnapshot | None) -> int:
        if snapshot is None:
            return -1
        if self._is_positive_power(snapshot.grid_to_home_power) or self._is_positive_power(snapshot.grid_to_battery_power):
            return -1
        if snapshot.grid_power is not None:
            return -1 if snapshot.grid_power >= 0 else 1
        if snapshot.grid_direction is not None:
            return -1 if snapshot.grid_direction > 0 else 1
        return -1

    def _is_positive_power(self, value: float | None) -> bool:
        return value is not None and value >= 1.0

    def _is_battery_charging(self, snapshot: EnergyFlowSnapshot) -> bool:
        if snapshot.battery_power is not None:
            return snapshot.battery_power > 0
        if snapshot.battery_direction is not None:
            return snapshot.battery_direction <= 0
        return self._is_positive_power(snapshot.pv_to_battery_power) or self._is_positive_power(snapshot.grid_to_battery_power)

    def _is_battery_discharging(self, snapshot: EnergyFlowSnapshot) -> bool:
        if snapshot.battery_power is not None:
            return snapshot.battery_power < 0
        if snapshot.battery_direction is not None:
            return snapshot.battery_direction > 0
        return False

    def _font_weight(self, weight: int) -> QFont.Weight:
        if weight >= 700:
            return QFont.Weight.Bold
        if weight >= 600:
            return QFont.Weight.DemiBold
        if weight >= 500:
            return QFont.Weight.Medium
        return QFont.Weight.Normal


class EnergyFlowWidget(QWidget):
    pv_clicked = Signal()
    grid_clicked = Signal()
    battery_clicked = Signal()
    home_clicked = Signal()
    inverter_clicked = Signal()
    weather_metric_clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._snapshot: EnergyFlowSnapshot | None = None
        self._weather_snapshot: WeatherLiveSnapshot | None = None
        self._status_text = tr("Choose a device profile and refresh to load current energy flow.")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.canvas = EnergyFlowCanvas()
        self.canvas.pv_clicked.connect(self.pv_clicked.emit)
        self.canvas.grid_clicked.connect(self.grid_clicked.emit)
        self.canvas.battery_clicked.connect(self.battery_clicked.emit)
        self.canvas.home_clicked.connect(self.home_clicked.emit)
        self.canvas.inverter_clicked.connect(self.inverter_clicked.emit)
        self.canvas.weather_metric_clicked.connect(self.weather_metric_clicked.emit)
        root.addWidget(self.canvas, 1)

        self._render()

    def set_snapshot(
        self,
        snapshot: EnergyFlowSnapshot,
        weather_snapshot: WeatherLiveSnapshot | None = None,
    ) -> None:
        if self._snapshot == snapshot and self._weather_snapshot == weather_snapshot and not self._status_text:
            return
        self._snapshot = snapshot
        self._weather_snapshot = weather_snapshot
        self._status_text = ""
        self._render()

    def set_status_text(self, text: str) -> None:
        if self._status_text == text:
            return
        self._status_text = text
        self._render()

    def set_weather_snapshot(self, weather_snapshot: WeatherLiveSnapshot | None) -> None:
        if self._weather_snapshot == weather_snapshot:
            return
        self._weather_snapshot = weather_snapshot
        self._render()

    def set_device_title(self, text: str) -> None:
        self.canvas.set_device_title(text)

    def set_device_meta(self, title: str, pn: str, sn: str) -> None:
        self.canvas.set_device_meta(title, pn, sn)

    def set_battery_capability_ah(self, value: float) -> None:
        self.canvas.set_battery_capability_ah(value)

    def set_active_tuya_icons(self, icons: list[tuple[str, QPixmap]]) -> None:
        self.canvas.set_active_tuya_icons(icons)

    def set_active_tuya_icons_updated_at(self, text: str) -> None:
        self.canvas.set_active_tuya_icons_updated_at(text)

    def _render(self) -> None:
        self.canvas.render_snapshot(self._snapshot, self._weather_snapshot, self._status_text)
