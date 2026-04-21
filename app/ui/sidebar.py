from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget


class SidebarWidget(QWidget):
    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ChartSidebar")
        self.setMinimumWidth(230)
        self.setMaximumWidth(280)

        self.helper_label = QLabel("Select series to display")
        self.helper_label.setObjectName("SidebarHelper")
        self.helper_label.setWordWrap(True)

        self.selection_label = QLabel("0 selected")
        self.selection_label.setObjectName("SidebarMeta")

        self.tree = QTreeWidget()
        self.tree.setObjectName("SidebarTree")
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(16)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.itemChanged.connect(self._on_item_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(4)
        layout.addWidget(self.helper_label)
        layout.addWidget(self.selection_label)
        layout.addWidget(self.tree)

    def set_groups(self, grouped_columns: dict[str, list[str]]) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()

        for group_name, columns in grouped_columns.items():
            group_item = QTreeWidgetItem([group_name])
            group_item.setFlags(group_item.flags() & ~Qt.ItemIsUserCheckable)
            self.tree.addTopLevelItem(group_item)

            for index, column in enumerate(columns):
                item = QTreeWidgetItem([column])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if index == 0 else Qt.Unchecked)
                group_item.addChild(item)

            group_item.setExpanded(True)

        self.tree.blockSignals(False)
        self._update_selection_label()

    def selected_columns(self) -> list[str]:
        selected: list[str] = []
        for group_index in range(self.tree.topLevelItemCount()):
            group_item = self.tree.topLevelItem(group_index)
            for child_index in range(group_item.childCount()):
                child = group_item.child(child_index)
                if child.checkState(0) == Qt.Checked:
                    selected.append(child.text(0))
        return selected

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if item.childCount() == 0 and column == 0:
            self._update_selection_label()
            self.selection_changed.emit()

    def _update_selection_label(self) -> None:
        count = len(self.selected_columns())
        label = "series" if count != 1 else "series"
        self.selection_label.setText(f"{count} {label} selected")
