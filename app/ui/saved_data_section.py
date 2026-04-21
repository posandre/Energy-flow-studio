from __future__ import annotations

from numbers import Real

import pandas as pd
from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QComboBox,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.storage import SavedImportRecord, list_saved_imports, load_import_dataframe
from app.services.i18n import tr


class SortableTableItem(QTableWidgetItem):
    def __lt__(self, other) -> bool:
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left < right
        return str(self.text()) < str(other.text())


class PreviewTableWidget(QTableWidget):
    """Preview table that should never force parent window width expansion."""

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, 0)


class SavedDataSection(QWidget):
    open_import_requested = Signal(int)
    update_requested = Signal()
    import_excel_requested = Signal()
    export_excel_requested = Signal(int)
    clear_database_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("SavedDataSection")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

        title = QLabel(tr("Data"))
        title.setObjectName("SavedDataTitle")

        subtitle = QLabel(
            tr("Browse the saved dataset, inspect its structure, preview rows, and reopen or export it.")
        )
        subtitle.setWordWrap(True)

        self.dataset_status = QLabel(tr("Current saved dataset: not loaded"))
        self.dataset_status.setObjectName("SavedDataStatus")
        self.dataset_selector_label = QLabel(tr("Dataset"))
        self.dataset_selector = QComboBox()
        self.dataset_selector.setObjectName("SavedDataDatasetCombo")
        self.dataset_selector.setFixedWidth(170)
        self.dataset_selector.setMinimumHeight(40)
        self.dataset_selector.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.dataset_selector.currentIndexChanged.connect(self._change_dataset_selection)

        self.refresh_button = QPushButton(tr("Refresh list"))
        self.refresh_button.setObjectName("SecondaryActionButton")
        self.refresh_button.clicked.connect(self.reload_records)

        self.clear_database_button = QPushButton(tr("Clear database"))
        self.clear_database_button.setObjectName("SecondaryActionButton")
        self.clear_database_button.clicked.connect(self._emit_clear_database_requested)

        self.update_button = QPushButton(tr("Update from API"))
        self.update_button.setObjectName("PrimaryActionButton")
        self.update_button.clicked.connect(self._emit_update_requested)

        self.import_excel_button = QPushButton(tr("Import Excel"))
        self.import_excel_button.setObjectName("SecondaryActionButton")
        self.import_excel_button.clicked.connect(self._emit_import_excel_requested)
        self.export_excel_button = QPushButton(tr("Export Excel"))
        self.export_excel_button.setObjectName("SecondaryActionButton")
        self.export_excel_button.clicked.connect(self.export_selected)

        header_row = QHBoxLayout()
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(self.import_excel_button)
        header_row.addWidget(self.export_excel_button)
        header_row.addWidget(self.refresh_button)
        header_row.addWidget(self.clear_database_button)
        header_row.addWidget(self.update_button)

        self.preview_table = PreviewTableWidget(0, 0)
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setSortingEnabled(False)
        self.preview_table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        self.preview_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.preview_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.preview_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.preview_table.setWordWrap(False)
        self.preview_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.preview_table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.preview_table.setMinimumSize(0, 0)
        self.preview_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.preview_table.horizontalHeader().setMinimumSectionSize(24)
        self.preview_table.horizontalHeader().setDefaultSectionSize(128)
        self.preview_table.horizontalHeader().sectionClicked.connect(self._handle_header_sort)

        self.page_size_label = QLabel(tr("Rows per page"))
        self.page_size_combo = QComboBox()
        self.page_size_combo.addItem("100", 100)
        self.page_size_combo.addItem("200", 200)
        self.page_size_combo.addItem("300", 300)
        self.page_size_combo.addItem("500", 500)
        self.page_size_combo.addItem(tr("All"), 0)
        self.page_size_combo.currentIndexChanged.connect(self._change_page_size)
        self.page_size_combo.setObjectName("SavedDataPageSizeCombo")
        self.page_size_combo.setMinimumWidth(160)
        self.page_size_combo.setMinimumHeight(32)
        self.page_size_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

        self.first_page_button = QPushButton(tr("First"))
        self.first_page_button.setObjectName("SecondaryActionButton")
        self.first_page_button.clicked.connect(self._go_to_first_page)
        self.prev_page_button = QPushButton(tr("Previous"))
        self.prev_page_button.setObjectName("SecondaryActionButton")
        self.prev_page_button.clicked.connect(self._go_to_previous_page)
        self.page_info_label = QLabel(tr("Page 0 of 0"))
        self.next_page_button = QPushButton(tr("Next"))
        self.next_page_button.setObjectName("SecondaryActionButton")
        self.next_page_button.clicked.connect(self._go_to_next_page)
        self.last_page_button = QPushButton(tr("Last"))
        self.last_page_button.setObjectName("SecondaryActionButton")
        self.last_page_button.clicked.connect(self._go_to_last_page)
        for button in (
            self.refresh_button,
            self.clear_database_button,
            self.update_button,
            self.import_excel_button,
            self.export_excel_button,
            self.first_page_button,
            self.prev_page_button,
            self.next_page_button,
            self.last_page_button,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        pagination_row = QHBoxLayout()
        pagination_row.addWidget(self.dataset_selector_label)
        pagination_row.addWidget(self.dataset_selector)
        pagination_row.addSpacing(16)
        pagination_row.addWidget(self.page_size_label)
        pagination_row.addWidget(self.page_size_combo)
        pagination_row.addStretch()
        pagination_row.addWidget(self.first_page_button)
        pagination_row.addWidget(self.prev_page_button)
        pagination_row.addWidget(self.page_info_label)
        pagination_row.addWidget(self.next_page_button)
        pagination_row.addWidget(self.last_page_button)

        content_panel = QWidget()
        content_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        content_layout = QVBoxLayout(content_panel)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(18)
        content_layout.addWidget(self.dataset_status)
        content_layout.addLayout(pagination_row)
        content_layout.addWidget(self.preview_table, stretch=1)

        section_card = QFrame()
        section_card.setObjectName("SavedDataFrame")
        section_card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        section_layout = QVBoxLayout(section_card)
        section_layout.setContentsMargins(20, 20, 20, 20)
        section_layout.setSpacing(16)
        section_layout.addLayout(header_row)
        section_layout.addWidget(subtitle)
        section_layout.addWidget(content_panel, stretch=1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(section_card, stretch=1)

        self._records: list[SavedImportRecord] = []
        self._current_import_id: int | None = None
        self._current_metadata: dict[str, object] = {}
        self._current_dataframe = pd.DataFrame()
        self._current_page = 0
        self._sort_column_name: str | None = None
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._refresh_spinner_phase = 0
        self._refresh_spinner_timer = QTimer(self)
        self._refresh_spinner_timer.setInterval(120)
        self._refresh_spinner_timer.timeout.connect(self._tick_refresh_spinner)
        self.reload_records()

    def reload_records(self) -> None:
        self._set_refresh_loading(True)
        QTimer.singleShot(0, self._reload_records_impl)

    def _reload_records_impl(self) -> None:
        previous_import_id = self._current_import_id
        self._records = list_saved_imports()
        self._current_import_id = previous_import_id if any(
            record.import_id == previous_import_id for record in self._records
        ) else (self._records[0].import_id if self._records else None)
        self._reload_dataset_selector()
        self._show_current_record()
        self._set_refresh_loading(False)

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
        self.refresh_button.setText(tr("Refresh list"))

    def _tick_refresh_spinner(self) -> None:
        frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        self._refresh_spinner_phase = (self._refresh_spinner_phase + 1) % len(frames)
        self.refresh_button.setText(f"{tr('Refreshing')} {frames[self._refresh_spinner_phase]}")

    def _dataset_option_label(self, record: SavedImportRecord) -> str:
        metadata = record.metadata or {}
        provider = str(metadata.get("provider", "")).strip().lower()
        if provider == "dessmonitor":
            return tr("DessMonitor")
        if provider == "weather":
            series_type = str(metadata.get("series_type", "history")).strip().title() or tr("History")
            return f"{tr('Weather')} {tr(series_type)}"
        if provider == "pv_forecast":
            return tr("PV Forecast")
        if provider == "excel":
            return tr("Excel")
        return record.source_label

    def _reload_dataset_selector(self) -> None:
        self.dataset_selector.blockSignals(True)
        self.dataset_selector.clear()
        for record in self._records:
            self.dataset_selector.addItem(self._dataset_option_label(record), record.import_id)
        selected_index = self.dataset_selector.findData(self._current_import_id)
        if selected_index >= 0:
            self.dataset_selector.setCurrentIndex(selected_index)
        self.dataset_selector.setEnabled(bool(self._records))
        self.dataset_selector.blockSignals(False)

    def _show_current_record(self) -> None:
        import_id = self._current_import_id
        if import_id is None:
            self.dataset_status.setText(tr("Current saved dataset: not loaded"))
            self._current_metadata = {}
            self._current_dataframe = pd.DataFrame()
            self._current_page = 0
            self.preview_table.setRowCount(0)
            self.preview_table.setColumnCount(0)
            self._update_action_buttons()
            self._update_pagination_controls()
            return

        record = next((item for item in self._records if item.import_id == import_id), None)
        if record is None:
            return

        dataframe = load_import_dataframe(import_id)
        self._current_metadata = dict(record.metadata)
        self._current_dataframe = dataframe
        self._current_page = 0
        note = str(record.metadata.get("device_label") or record.metadata.get("file_name") or "-")
        self.dataset_status.setText(f"{tr('Current saved dataset')}: {note} ({len(dataframe.index)} {tr('rows')})")
        self._update_action_buttons()
        self._refresh_current_page()

    def _refresh_current_page(self) -> None:
        dataframe = self._sorted_dataframe()
        if dataframe.empty:
            self.preview_table.clear()
            self.preview_table.setRowCount(0)
            self.preview_table.setColumnCount(0)
            self._update_pagination_controls()
            return

        page_size = self._page_size()
        if page_size == 0:
            max_page = 0
            page_slice = dataframe
        else:
            max_page = max(0, (len(dataframe.index) - 1) // page_size)
            self._current_page = min(self._current_page, max_page)
            start = self._current_page * page_size
            end = start + page_size
            page_slice = dataframe.iloc[start:end]

        self._current_page = min(self._current_page, max_page)
        self._populate_preview(page_slice)
        self._update_pagination_controls()

    def _populate_preview(self, dataframe: pd.DataFrame) -> None:
        self.preview_table.clear()
        self.preview_table.setColumnCount(len(dataframe.columns))
        self.preview_table.setHorizontalHeaderLabels([str(column) for column in dataframe.columns])
        self.preview_table.setRowCount(len(dataframe.index))

        for row_index, (_, row) in enumerate(dataframe.iterrows()):
            for col_index, value in enumerate(row.tolist()):
                display = self._format_preview_value(value)
                item = SortableTableItem(display)
                if pd.notna(value):
                    if isinstance(value, (int, float)):
                        item.setData(Qt.ItemDataRole.UserRole, float(value))
                    else:
                        item.setData(Qt.ItemDataRole.UserRole, str(value))
                self.preview_table.setItem(row_index, col_index, item)

        self.preview_table.horizontalHeader().setStretchLastSection(False)
        for col_index in range(self.preview_table.columnCount()):
            self.preview_table.resizeColumnToContents(col_index)
            measured_width = self.preview_table.columnWidth(col_index)
            column_name = str(dataframe.columns[col_index]).strip().lower()
            if "timestamp" in column_name or "time" in column_name or "date" in column_name:
                min_width = 170
                max_width = 220
            elif column_name.endswith("_code") or column_name == "code":
                min_width = 90
                max_width = 130
            else:
                min_width = 84
                max_width = 180
            self.preview_table.setColumnWidth(col_index, min(max(min_width, measured_width + 10), max_width))
        self._update_header_sort_indicator()

    @staticmethod
    def _format_preview_value(value) -> str:
        if pd.isna(value):
            return ""
        if isinstance(value, Real) and not isinstance(value, bool):
            formatted = f"{float(value):.3f}".rstrip("0").rstrip(".")
            return formatted or "0"
        return str(value)

    def _update_pagination_controls(self) -> None:
        total_rows = len(self._current_dataframe.index)
        page_size = self._page_size()
        total_pages = 1 if total_rows and page_size == 0 else (max(1, (total_rows + page_size - 1) // page_size) if total_rows else 0)
        current_page_number = self._current_page + 1 if total_pages else 0
        if total_rows:
            if page_size == 0:
                start = 1
                end = total_rows
            else:
                start = self._current_page * page_size + 1
                end = min(total_rows, (self._current_page + 1) * page_size)
            self.page_info_label.setText(
                f"{tr('Page')} {current_page_number} {tr('of')} {total_pages}  |  "
                f"{tr('Rows')} {start}-{end} {tr('of')} {total_rows}"
            )
        else:
            self.page_info_label.setText(tr("Page 0 of 0"))

        has_previous = total_pages > 1 and self._current_page > 0
        has_next = total_pages > 1 and self._current_page < total_pages - 1
        self.first_page_button.setEnabled(has_previous)
        self.prev_page_button.setEnabled(has_previous)
        self.next_page_button.setEnabled(has_next)
        self.last_page_button.setEnabled(has_next)

    def _page_size(self) -> int:
        return int(self.page_size_combo.currentData() or 0)

    def _change_page_size(self, index: int) -> None:
        del index
        self._current_page = 0
        self._refresh_current_page()

    def _change_dataset_selection(self, index: int) -> None:
        if index < 0:
            return
        import_id = self.dataset_selector.itemData(index)
        if import_id is None:
            return
        self._current_import_id = int(import_id)
        self._show_current_record()

    def _go_to_first_page(self) -> None:
        self._current_page = 0
        self._refresh_current_page()

    def _go_to_previous_page(self) -> None:
        if self._current_page == 0:
            return
        self._current_page -= 1
        self._refresh_current_page()

    def _go_to_next_page(self) -> None:
        if self._current_dataframe.empty:
            return
        page_size = self._page_size()
        if page_size == 0:
            return
        total_pages = (len(self._current_dataframe.index) + page_size - 1) // page_size
        if self._current_page >= total_pages - 1:
            return
        self._current_page += 1
        self._refresh_current_page()

    def _go_to_last_page(self) -> None:
        if self._current_dataframe.empty:
            return
        page_size = self._page_size()
        if page_size == 0:
            self._current_page = 0
            self._refresh_current_page()
            return
        total_pages = (len(self._current_dataframe.index) + page_size - 1) // page_size
        self._current_page = max(0, total_pages - 1)
        self._refresh_current_page()

    def _sorted_dataframe(self) -> pd.DataFrame:
        dataframe = self._current_dataframe
        if dataframe.empty or self._sort_column_name not in dataframe.columns:
            return dataframe

        ascending = self._sort_order == Qt.SortOrder.AscendingOrder
        sorted_frame = dataframe.copy()
        series = sorted_frame[self._sort_column_name]
        if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_datetime64_any_dtype(series):
            sorted_frame = sorted_frame.sort_values(
                by=self._sort_column_name,
                ascending=ascending,
                kind="mergesort",
                na_position="last",
            )
        else:
            sorted_frame = sorted_frame.assign(
                __sort_key=series.astype(str).str.lower()
            ).sort_values(
                by="__sort_key",
                ascending=ascending,
                kind="mergesort",
                na_position="last",
            ).drop(columns=["__sort_key"])
        return sorted_frame.reset_index(drop=True)

    def _handle_header_sort(self, section: int) -> None:
        if section < 0 or section >= len(self._current_dataframe.columns):
            return
        column_name = str(self._current_dataframe.columns[section])
        if self._sort_column_name == column_name:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_column_name = column_name
            self._sort_order = Qt.SortOrder.AscendingOrder
        self._current_page = 0
        self._refresh_current_page()

    def _update_header_sort_indicator(self) -> None:
        header = self.preview_table.horizontalHeader()
        header.setSortIndicatorShown(False)
        if self._sort_column_name not in self._current_dataframe.columns:
            return
        section = list(self._current_dataframe.columns).index(self._sort_column_name)
        header.setSortIndicator(section, self._sort_order)
        header.setSortIndicatorShown(True)

    def open_selected(self) -> None:
        import_id = self._current_import_id
        if import_id is None:
            return
        self.open_import_requested.emit(import_id)

    def export_selected(self) -> None:
        import_id = self._current_import_id
        if import_id is None:
            return
        self.export_excel_requested.emit(import_id)

    def _update_action_buttons(self) -> None:
        self.update_button.setVisible(True)

    def set_activity_dataset(self, label: str, metadata: dict[str, object] | None = None) -> None:
        self._current_metadata = dict(metadata or self._current_metadata)
        self.dataset_status.setText(f"{tr('Current saved dataset')}: {label}")
        self._update_action_buttons()

    def append_activity(self, message: str) -> None:
        del message

    def clear_activity(self) -> None:
        return

    def _emit_update_requested(self, checked: bool = False) -> None:
        del checked
        self.update_requested.emit()

    def _emit_import_excel_requested(self, checked: bool = False) -> None:
        del checked
        self.import_excel_requested.emit()

    def _emit_clear_database_requested(self, checked: bool = False) -> None:
        del checked
        self.clear_database_requested.emit()
