from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.components.chart import apply_energyflow_chart_style
from app.services.i18n import tr, tr_fragment
from app.services.pv_forecast import PvForecastResult
from app.ui.design_system import apply_tab_widget_interaction, compose_styles, tab_widget_qss
from app.ui.graph_view import GraphView


@dataclass(slots=True)
class _MetricCard:
    root: QFrame
    title: QLabel
    value: QLabel
    meta: QLabel


class _SeriesToggle(QCheckBox):
    def __init__(self, text: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("SeriesToggle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(SERIES_TOGGLE_STYLE_TEMPLATE.format(color=color))


class ForecastTab(QWidget):
    """Forecast workspace with overview metrics and Plotly-backed charts."""
    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result: PvForecastResult | None = None
        self._tariff_cost_visible = False
        self._refresh_spinner_phase = 0
        self._refresh_spinner_timer = QTimer(self)
        self._refresh_spinner_timer.setInterval(120)
        self._refresh_spinner_timer.timeout.connect(self._tick_refresh_spinner)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        shell = QWidget()
        shell.setObjectName("ChartSection")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(24, 24, 24, 24)
        shell_layout.setSpacing(16)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)

        header_text = QVBoxLayout()
        header_text.setContentsMargins(0, 0, 0, 0)
        header_text.setSpacing(6)
        title = QLabel(tr("PV Forecast"))
        title.setObjectName("ChartSectionTitle")
        subtitle = QLabel(tr("Short-term solar generation forecast calibrated by local DessMonitor history and weather inputs."))
        subtitle.setWordWrap(True)
        subtitle.setObjectName("ForecastHeaderSubtitle")
        subtitle.setContentsMargins(0, 1, 0, 3)
        header_text.addWidget(title)
        header_text.addWidget(subtitle)
        header.addLayout(header_text, 1)

        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setObjectName("EnergyFlowRefreshButton")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        header.addWidget(self.refresh_button, alignment=Qt.AlignmentFlag.AlignTop)
        shell_layout.addLayout(header)

        self.status_label = QLabel(tr("Open the Forecast tab to build the first prediction."))
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("ForecastHeaderStatus")
        self.status_label.setContentsMargins(0, 2, 0, 6)
        shell_layout.addWidget(self.status_label)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("ForecastTabs")
        apply_tab_widget_interaction(self.tabs)
        shell_layout.addWidget(self.tabs, 1)

        self.overview_page = QWidget()
        overview_layout = QVBoxLayout(self.overview_page)
        overview_layout.setContentsMargins(8, 8, 8, 10)
        overview_layout.setSpacing(10)

        cards_row = QGridLayout()
        cards_row.setContentsMargins(2, 2, 2, 2)
        cards_row.setHorizontalSpacing(12)
        cards_row.setVerticalSpacing(12)
        self.today_card = self._create_metric_card(tr("Today forecast"))
        self.tomorrow_card = self._create_metric_card(tr("Tomorrow forecast"))
        self.peak_card = self._create_metric_card(tr("Peak window"))
        self.confidence_card = self._create_metric_card(tr("Confidence"))
        self.day_cost_card = self._create_metric_card(tr("Day import cost"))
        self.night_cost_card = self._create_metric_card(tr("Night import cost"))
        for index, card in enumerate((self.today_card, self.tomorrow_card, self.peak_card, self.confidence_card)):
            cards_row.addWidget(card.root, 0, index)
        cards_row.addWidget(self.day_cost_card.root, 1, 0, 1, 2)
        cards_row.addWidget(self.night_cost_card.root, 1, 2, 1, 2)
        self.day_cost_card.root.setVisible(False)
        self.night_cost_card.root.setVisible(False)
        cards_shell = QWidget()
        cards_shell.setLayout(cards_row)
        cards_shell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        overview_layout.addWidget(cards_shell, 0, Qt.AlignmentFlag.AlignTop)

        self.model_card = QFrame()
        self.model_card.setObjectName("ForecastPanel")
        self.model_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        model_layout = QVBoxLayout(self.model_card)
        model_layout.setContentsMargins(16, 14, 16, 14)
        model_layout.setSpacing(8)
        self.model_title = QLabel(tr("Forecast model is waiting for data."))
        self.model_title.setObjectName("ForecastPanelTitle")
        self.model_meta = QLabel("")
        self.model_meta.setWordWrap(True)
        self.model_meta.setObjectName("SidebarMeta")
        model_layout.addWidget(self.model_title)
        model_layout.addWidget(self.model_meta)

        self.driver_grid = QGridLayout()
        self.driver_grid.setHorizontalSpacing(10)
        self.driver_grid.setVerticalSpacing(8)
        self.driver_cards = [
            self._create_driver_card(tr("Solar radiation")),
            self._create_driver_card(tr("Cloud cover")),
            self._create_driver_card(tr("Temperature")),
            self._create_driver_card(tr("Wind")),
        ]
        for index, card in enumerate(self.driver_cards):
            self.driver_grid.addWidget(card.root, 0, index)
        model_layout.addLayout(self.driver_grid)
        overview_layout.addWidget(self.model_card, 0, Qt.AlignmentFlag.AlignTop)
        overview_layout.addStretch(1)

        self.hourly_page = QWidget()
        hourly_layout = QVBoxLayout(self.hourly_page)
        hourly_layout.setContentsMargins(8, 8, 8, 10)
        hourly_layout.setSpacing(14)
        self.hourly_summary = QLabel(tr("Hourly prediction for the next 48 hours."))
        self.hourly_summary.setObjectName("SidebarMeta")
        self.hourly_summary.setWordWrap(True)
        self.hourly_toggle_row = self._build_toggle_row()
        self.hourly_checks = {
            "p10": _SeriesToggle(tr("P10"), "#22c55e"),
            "p50": _SeriesToggle(tr("P50"), "#22c55e"),
            "p90": _SeriesToggle(tr("P90"), "#22c55e"),
        }
        self.hourly_checks["p10"].setChecked(True)
        self.hourly_checks["p50"].setChecked(True)
        self.hourly_checks["p90"].setChecked(True)
        self._fill_toggle_row(self.hourly_toggle_row, list(self.hourly_checks.values()))
        for checkbox in self.hourly_checks.values():
            checkbox.toggled.connect(self._refresh_hourly_graph)
        self.hourly_graph = GraphView()
        hourly_layout.addWidget(self.hourly_summary)
        hourly_layout.addWidget(self.hourly_toggle_row)
        hourly_layout.addWidget(self.hourly_graph, 1)

        self.accuracy_page = QWidget()
        accuracy_layout = QVBoxLayout(self.accuracy_page)
        accuracy_layout.setContentsMargins(8, 8, 8, 10)
        accuracy_layout.setSpacing(14)
        accuracy_cards = QGridLayout()
        accuracy_cards.setContentsMargins(2, 2, 2, 2)
        accuracy_cards.setHorizontalSpacing(12)
        accuracy_cards.setVerticalSpacing(12)
        self.actual_today_card = self._create_metric_card(tr("Actual today"))
        self.forecast_now_card = self._create_metric_card(tr("Forecast to now"))
        self.delta_card = self._create_metric_card(tr("Delta"))
        for index, card in enumerate((self.actual_today_card, self.forecast_now_card, self.delta_card)):
            accuracy_cards.addWidget(card.root, 0, index)
        accuracy_cards_shell = QWidget()
        accuracy_cards_shell.setLayout(accuracy_cards)
        accuracy_cards_shell.setSizePolicy(
            accuracy_cards_shell.sizePolicy().horizontalPolicy(),
            accuracy_cards_shell.sizePolicy().Policy.Fixed,
        )
        self.accuracy_toggle_row = self._build_toggle_row()
        self.accuracy_checks = {
            "forecast": _SeriesToggle(tr("Forecast"), "#38bdf8"),
            "actual": _SeriesToggle(tr("Actual"), "#f5cf55"),
        }
        self.accuracy_checks["forecast"].setChecked(True)
        self.accuracy_checks["actual"].setChecked(True)
        self._fill_toggle_row(self.accuracy_toggle_row, list(self.accuracy_checks.values()))
        for checkbox in self.accuracy_checks.values():
            checkbox.toggled.connect(self._refresh_accuracy_graph)
        self.accuracy_graph = GraphView()
        accuracy_layout.addWidget(accuracy_cards_shell, 0, Qt.AlignmentFlag.AlignTop)
        accuracy_layout.addWidget(self.accuracy_toggle_row)
        accuracy_layout.addWidget(self.accuracy_graph, 1)

        self.tabs.addTab(self.overview_page, tr("Overview"))
        self.tabs.addTab(self.hourly_page, tr("Hourly"))
        self.tabs.addTab(self.accuracy_page, tr("Accuracy"))
        root.addWidget(shell, 1)

        self.setStyleSheet(compose_styles(FORECAST_TABS_STYLE, FORECAST_TAB_STYLE))
        self.set_unavailable(tr("Choose an active profile and sync DessMonitor data to generate a forecast."))

    def _create_metric_card(self, title: str) -> _MetricCard:
        frame = QFrame()
        frame.setObjectName("ForecastCard")
        frame.setMinimumHeight(96)
        frame.setMaximumHeight(180)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("ForecastCardTitle")
        value_label = QLabel("--")
        value_label.setObjectName("ForecastCardValue")
        meta_label = QLabel("")
        meta_label.setWordWrap(True)
        meta_label.setObjectName("ForecastCardMeta")
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addStretch(1)
        layout.addWidget(meta_label)
        return _MetricCard(frame, title_label, value_label, meta_label)

    def _create_driver_card(self, title: str) -> _MetricCard:
        frame = QFrame()
        frame.setObjectName("ForecastDriverCard")
        frame.setMinimumHeight(82)
        frame.setMaximumHeight(132)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        title_label = QLabel(title)
        title_label.setObjectName("ForecastDriverTitle")
        value_label = QLabel("--")
        value_label.setObjectName("ForecastDriverValue")
        meta_label = QLabel("")
        meta_label.setWordWrap(True)
        meta_label.setObjectName("ForecastDriverMeta")
        layout.addWidget(title_label)
        layout.addWidget(value_label)
        layout.addWidget(meta_label)
        return _MetricCard(frame, title_label, value_label, meta_label)

    def set_loading(self, message: str = "Building forecast...") -> None:
        self._set_status_message(message, error=False)
        self._set_refresh_loading(True)

    def set_unavailable(self, message: str) -> None:
        self._result = None
        self._set_status_message(message, error=False)
        self._set_refresh_loading(False)
        self.hourly_graph.set_placeholder(message)
        self.accuracy_graph.set_placeholder(message)

    def set_error(self, message: str) -> None:
        self._result = None
        self._set_status_message(message, error=True)
        self._set_refresh_loading(False)
        self.hourly_graph.set_placeholder(message)
        self.accuracy_graph.set_placeholder(message)

    def set_tariff_cost_estimate(self, day_cost_uah: float, night_cost_uah: float, *, enabled: bool) -> None:
        self._tariff_cost_visible = bool(enabled)
        self.day_cost_card.root.setVisible(self._tariff_cost_visible)
        self.night_cost_card.root.setVisible(self._tariff_cost_visible)
        if not self._tariff_cost_visible:
            return
        self._set_card(
            self.day_cost_card,
            f"{float(day_cost_uah):.2f} {tr('UAH')}",
            tr("Estimated from latest daily grid-import profile."),
        )
        self._set_card(
            self.night_cost_card,
            f"{float(night_cost_uah):.2f} {tr('UAH')}",
            tr("Estimated from latest daily grid-import profile."),
        )

    def set_result(self, result: PvForecastResult) -> None:
        self._result = result
        self._set_refresh_loading(False)
        self._set_status_message(
            (
                f"{tr_fragment(result.model_name)} • "
                f"{result.training_days} {tr('training day(s)')} • "
                f"{tr_fragment(result.confidence_label)}"
            )
            ,
            error=False,
        )

        progress_meta = tr("Expected output for the current day.")
        progress_ratio = self._today_progress_ratio(result)
        if progress_ratio is not None:
            progress_meta = f"{tr('Progress vs forecast')}: {int(round(progress_ratio * 100))}%"
        self._set_card(
            self.today_card,
            self._format_energy(result.today_energy_kwh),
            progress_meta,
        )
        self._set_card(
            self.tomorrow_card,
            self._format_energy(result.tomorrow_energy_kwh),
            tr("Forecasted daily energy for tomorrow."),
        )
        self._set_card(
            self.peak_card,
            self._format_power(result.peak_power_kw),
            result.peak_power_at or tr("No daylight peak detected"),
        )
        reliability = self._reliability_from_compare(result.compare_frame)
        confidence_meta = f"{tr_fragment(result.confidence_label)} • {tr('Reliability')}: {reliability}"
        self._set_card(
            self.confidence_card,
            f"{int(round(result.confidence_score * 100))}%",
            confidence_meta,
        )
        self._set_card(
            self.actual_today_card,
            self._format_energy(result.actual_today_energy_kwh),
            tr("Measured generation so far today."),
        )
        self._set_card(
            self.forecast_now_card,
            self._format_energy(result.forecast_today_to_now_kwh),
            tr("Forecast integrated up to the latest actual hour."),
        )
        self._set_card(
            self.delta_card,
            self._format_delta(result.delta_today_kwh),
            tr("Actual minus forecast-to-now."),
        )

        self.model_title.setText(tr_fragment(result.model_name))
        notes = " • ".join(tr_fragment(note) for note in result.notes)
        self.model_meta.setText(notes)

        driver_values = [
            (self.driver_cards[0], self._format_radiation(result.driver_snapshot.get("shortwave_radiation")), self._next_hour_meta(result.forecast_frame)),
            (self.driver_cards[1], self._format_percent(result.driver_snapshot.get("cloud_cover")), tr("Cloud attenuation")),
            (self.driver_cards[2], self._format_temperature(result.driver_snapshot.get("temperature_2m")), tr("Module heat loss proxy")),
            (self.driver_cards[3], self._format_wind(result.driver_snapshot.get("wind_speed_10m")), tr("Cooling effect")),
        ]
        for card, value, meta in driver_values:
            card.value.setText(value)
            card.meta.setText(meta)

        self.hourly_summary.setText(
            (
                f"{tr('Next 48 hours')} • {tr('PV source')}: {result.source_pv_column} • "
                f"{tr('Weather-matched rows')}: {result.matched_history_rows}"
            )
        )
        self._refresh_hourly_graph()
        self._refresh_accuracy_graph()

    def _set_status_message(self, message: str, *, error: bool) -> None:
        self.status_label.setText(message)
        if error:
            self.status_label.setStyleSheet(
                "color: #fecaca; background: rgba(127, 29, 29, 0.35); "
                "border: 1px solid #ef4444; border-radius: 10px; padding: 8px;"
            )
            return
        self.status_label.setStyleSheet("")

    def _set_refresh_loading(self, loading: bool) -> None:
        if loading:
            self._refresh_spinner_phase = 0
            self.refresh_button.setEnabled(False)
            self.refresh_button.setText(f"{tr('Refreshing')} ⠋")
            self._refresh_spinner_timer.start()
            return
        self._refresh_spinner_timer.stop()
        self._refresh_spinner_phase = 0
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText(tr("Refresh"))

    def _tick_refresh_spinner(self) -> None:
        frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        self._refresh_spinner_phase = (self._refresh_spinner_phase + 1) % len(frames)
        self.refresh_button.setText(f"{tr('Refreshing')} {frames[self._refresh_spinner_phase]}")

    def _set_card(self, card: _MetricCard, value: str, meta: str) -> None:
        card.value.setText(value)
        card.meta.setText(meta)

    def _build_hourly_figure(self, dataframe: pd.DataFrame, compare_frame: pd.DataFrame):
        predicted = pd.to_numeric(dataframe["predicted_pv_power_kw"], errors="coerce").fillna(0.0)
        lower, upper = self._forecast_band(predicted, compare_frame)
        figure = go.Figure()
        show_p10 = self.hourly_checks.get("p10").isChecked()
        show_p50 = self.hourly_checks.get("p50").isChecked()
        show_p90 = self.hourly_checks.get("p90").isChecked()

        if show_p10:
            figure.add_trace(
                go.Scatter(
                    x=dataframe["Timestamp"],
                    y=lower,
                    mode="lines",
                    name=tr("P10"),
                    line={"color": "rgba(34,197,94,0.55)", "width": 1.6},
                    hovertemplate=f"{tr('P10')}: " + "%{y:.2~f} kW<extra></extra>",
                    showlegend=False,
                )
            )
        if show_p90:
            figure.add_trace(
                go.Scatter(
                    x=dataframe["Timestamp"],
                    y=upper,
                    mode="lines",
                    name=tr("P90"),
                    line={"color": "rgba(34,197,94,0.55)", "width": 1.6},
                    hovertemplate=f"{tr('P90')}: " + "%{y:.2~f} kW<extra></extra>",
                    fill="tonexty" if show_p10 else None,
                    fillcolor="rgba(34,197,94,0.16)" if show_p10 else None,
                    showlegend=False,
                )
            )
        if show_p50:
            figure.add_trace(
                go.Scatter(
                    x=dataframe["Timestamp"],
                    y=predicted,
                    mode="lines",
                    name=tr("P50"),
                    line={"color": "#22c55e", "width": 3},
                    hovertemplate=f"{tr('P50')}: " + "%{y:.2~f} kW<extra></extra>",
                    showlegend=False,
                )
            )

        if not figure.data:
            figure.add_trace(
                go.Scatter(
                    x=dataframe["Timestamp"],
                    y=[0.0] * len(dataframe.index),
                    mode="lines",
                    line={"color": "rgba(0,0,0,0)", "width": 1},
                    hoverinfo="skip",
                    showlegend=False,
                )
            )
        apply_energyflow_chart_style(
            figure,
            y_title=tr("PV power (kW)"),
            x_title=tr("Time"),
            margin={"l": 42, "r": 24, "t": 28, "b": 48},
            showlegend=False,
        )
        return figure

    def _build_accuracy_figure(self, dataframe: pd.DataFrame, forecast_frame: pd.DataFrame):
        figure = go.Figure()
        if dataframe.empty and not forecast_frame.empty and "Timestamp" in forecast_frame.columns:
            fallback = forecast_frame[["Timestamp", "predicted_pv_power_kw"]].copy()
            fallback["Timestamp"] = pd.to_datetime(fallback["Timestamp"], errors="coerce")
            fallback["predicted_pv_power_kw"] = pd.to_numeric(
                fallback["predicted_pv_power_kw"],
                errors="coerce",
            )
            fallback.dropna(subset=["Timestamp", "predicted_pv_power_kw"], inplace=True)
            dataframe = fallback.reset_index(drop=True)
        if not dataframe.empty:
            if self.accuracy_checks.get("forecast").isChecked():
                figure.add_trace(
                    go.Scatter(
                        x=dataframe["Timestamp"],
                        y=dataframe["predicted_pv_power_kw"],
                        mode="lines",
                        name=tr("Forecast"),
                        line={"color": "#38bdf8", "width": 3},
                        connectgaps=True,
                        showlegend=False,
                    )
                )
            if (
                self.accuracy_checks.get("actual").isChecked()
                and "actual_pv_power_kw" in dataframe.columns
                and dataframe["actual_pv_power_kw"].notna().any()
            ):
                figure.add_trace(
                    go.Scatter(
                        x=dataframe["Timestamp"],
                        y=dataframe["actual_pv_power_kw"],
                        mode="lines",
                        name=tr("Actual"),
                        line={"color": "#f5cf55", "width": 3},
                        connectgaps=True,
                        showlegend=False,
                    )
                )
        if not figure.data:
            figure.add_trace(
                go.Scatter(
                    x=[],
                    y=[],
                    mode="lines",
                    line={"color": "rgba(0,0,0,0)", "width": 1},
                    hoverinfo="skip",
                    showlegend=False,
                )
            )
        apply_energyflow_chart_style(
            figure,
            y_title=tr("PV power (kW)"),
            x_title=tr("Today"),
            margin={"l": 42, "r": 24, "t": 28, "b": 48},
            showlegend=False,
        )
        return figure

    def _build_toggle_row(self) -> QScrollArea:
        toggle_wrap = QScrollArea()
        toggle_wrap.setWidgetResizable(True)
        toggle_wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        toggle_wrap.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        toggle_wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        toggle_wrap.setFixedHeight(42)
        toggle_wrap.setStyleSheet("background: transparent; border: none;")
        content = QWidget()
        layout = QHBoxLayout(content)
        layout.setContentsMargins(2, 6, 2, 8)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        toggle_wrap.setWidget(content)
        return toggle_wrap

    def _fill_toggle_row(self, toggle_wrap: QScrollArea, checkboxes: list[QCheckBox]) -> None:
        container = toggle_wrap.widget()
        row_layout = container.layout() if container is not None else None
        if row_layout is None:
            return
        while row_layout.count():
            item = row_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for checkbox in checkboxes:
            row_layout.addWidget(checkbox)

    def _refresh_hourly_graph(self) -> None:
        if self._result is None:
            return
        self.hourly_graph.set_figure(self._build_hourly_figure(self._result.forecast_frame, self._result.compare_frame))

    def _refresh_accuracy_graph(self) -> None:
        if self._result is None:
            return
        self.accuracy_graph.set_figure(
            self._build_accuracy_figure(self._result.compare_frame, self._result.forecast_frame)
        )

    def _format_energy(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        return f"{float(value):.1f} {tr('kWh')}"

    def _format_power(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        if float(value) >= 10:
            return f"{float(value):.1f} kW"
        return f"{float(value):.2f} kW"

    def _format_delta(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        return f"{float(value):+.1f} {tr('kWh')}"

    def _format_radiation(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        return f"{int(round(float(value)))} {tr('W/m2')}"

    def _format_percent(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        return f"{int(round(float(value)))}%"

    def _format_temperature(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        return f"{float(value):.1f} C"

    def _format_wind(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        return f"{float(value):.1f} m/s"

    def _next_hour_meta(self, forecast_frame: pd.DataFrame) -> str:
        if forecast_frame.empty or "Timestamp" not in forecast_frame.columns:
            return tr("Next forecast hour")
        timestamps = pd.to_datetime(forecast_frame["Timestamp"], errors="coerce").dropna()
        if timestamps.empty:
            return tr("Next forecast hour")
        now_local = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None)
        next_hour = now_local.ceil("h")
        future = timestamps[timestamps >= next_hour]
        target = future.iloc[0] if not future.empty else timestamps.iloc[-1]
        return f"{tr('Forecast for')} {target.strftime('%H:%M')}"

    def _today_progress_ratio(self, result: PvForecastResult) -> float | None:
        if (
            result.actual_today_energy_kwh is None
            or pd.isna(result.actual_today_energy_kwh)
            or result.today_energy_kwh is None
            or pd.isna(result.today_energy_kwh)
            or float(result.today_energy_kwh) <= 0
        ):
            return None
        ratio = float(result.actual_today_energy_kwh) / float(result.today_energy_kwh)
        return max(0.0, min(ratio, 1.99))

    def _reliability_from_compare(self, dataframe: pd.DataFrame) -> str:
        """Classify forecast reliability from MAPE over known actual points."""
        if dataframe.empty or "actual_pv_power_kw" not in dataframe.columns:
            return tr("Unknown")
        valid = dataframe[
            dataframe["actual_pv_power_kw"].notna()
            & dataframe["predicted_pv_power_kw"].notna()
            & (pd.to_numeric(dataframe["predicted_pv_power_kw"], errors="coerce") >= 0.0)
        ].copy()
        if valid.empty:
            return tr("Unknown")
        predicted = pd.to_numeric(valid["predicted_pv_power_kw"], errors="coerce").fillna(0.0)
        actual = pd.to_numeric(valid["actual_pv_power_kw"], errors="coerce").fillna(0.0)
        # Floor denominator to avoid unstable percentages around zero generation.
        denom = actual.abs().clip(lower=0.05)
        mape = float(((actual - predicted).abs() / denom).mean())
        if mape <= 0.18:
            return tr("High")
        if mape <= 0.35:
            return tr("Medium")
        return tr("Low")

    def _forecast_band(self, predicted: pd.Series, compare_frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """Build lower/upper uncertainty band around prediction curve."""
        if compare_frame.empty or "actual_pv_power_kw" not in compare_frame.columns:
            spread = max(float(predicted.max()) * 0.2, 0.05)
            lower = (predicted - spread).clip(lower=0.0)
            upper = (predicted + spread).clip(lower=0.0)
            return lower, upper
        valid = compare_frame[
            compare_frame["actual_pv_power_kw"].notna()
            & compare_frame["predicted_pv_power_kw"].notna()
        ].copy()
        if valid.empty:
            spread = max(float(predicted.max()) * 0.2, 0.05)
            lower = (predicted - spread).clip(lower=0.0)
            upper = (predicted + spread).clip(lower=0.0)
            return lower, upper
        residual = (
            pd.to_numeric(valid["actual_pv_power_kw"], errors="coerce").fillna(0.0)
            - pd.to_numeric(valid["predicted_pv_power_kw"], errors="coerce").fillna(0.0)
        )
        # Quantile residual band is robust to outliers compared with min/max.
        q10 = float(residual.quantile(0.10))
        q90 = float(residual.quantile(0.90))
        if q90 < q10:
            q10, q90 = q90, q10
        lower = (predicted + q10).clip(lower=0.0)
        upper = (predicted + q90).clip(lower=0.0)
        return lower, upper
SERIES_TOGGLE_STYLE_TEMPLATE = """
QCheckBox {{
    color: {color};
    spacing: 6px;
    padding: 4px 0;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}}
"""

FORECAST_TAB_STYLE = """
QFrame#ForecastCard,
QFrame#ForecastDriverCard,
QFrame#ForecastPanel {
    background: rgba(9, 18, 34, 0.82);
    border: 1px solid #22304a;
    border-radius: 16px;
}
QLabel#ForecastCardTitle,
QLabel#ForecastDriverTitle,
QLabel#ForecastPanelTitle {
    color: #9fb0c5;
    font-size: 12px;
    font-weight: 600;
}
QLabel#ForecastCardValue,
QLabel#ForecastDriverValue {
    color: #f8fafc;
    font-size: 20px;
    font-weight: 700;
}
QLabel#ForecastCardMeta,
QLabel#ForecastDriverMeta {
    color: #8fb0cb;
    font-size: 12px;
}
QLabel#ForecastHeaderSubtitle {
    color: #8fa7c2;
    font-size: 15px;
    font-weight: 500;
}
QLabel#ForecastHeaderStatus {
    color: #8fb0cb;
    font-size: 14px;
    font-weight: 500;
}
"""
FORECAST_TABS_STYLE = tab_widget_qss(
    "QTabWidget#ForecastTabs",
    pane_margin_top=8,
    tab_radius=8,
)
