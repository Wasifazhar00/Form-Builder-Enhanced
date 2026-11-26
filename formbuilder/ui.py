from __future__ import annotations

import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import fitz
import pandas as pd
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from formbuilder.data import load_master_data, record_from_row
from formbuilder.detector import detect_fields
from formbuilder.pdf_engine import generate_filled_pdf
from formbuilder.template import make_field, normalize_field, save_template, sha256_bytes

PREVIEW_DPI = 144


class FieldOverlayItem(QGraphicsRectItem):
    """Movable field box with a lower-right resize handle."""

    HANDLE_SIZE = 9.0

    def __init__(self, field: dict[str, Any], on_change):
        x0, y0, x1, y1 = field["rect"]
        super().__init__(QRectF(0, 0, x1 - x0, y1 - y0))
        self.field_id = field["id"]
        self.label = field.get("label", "Field")
        self.on_change = on_change
        self._resizing = False
        self._resize_start = QPointF()
        self.setPos(QPointF(x0, y0))
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.setToolTip(f"{self.label} · drag to move · bottom-right handle to resize")

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: ANN001
        selected = self.isSelected()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#C62828" if selected else "#1565C0"), 1.3))
        painter.setBrush(QColor(21, 101, 192, 28))
        painter.drawRect(self.rect())
        painter.setBrush(QColor("#C62828" if selected else "#1565C0"))
        painter.drawRect(
            QRectF(
                self.rect().right() - self.HANDLE_SIZE,
                self.rect().bottom() - self.HANDLE_SIZE,
                self.HANDLE_SIZE,
                self.HANDLE_SIZE,
            )
        )
        painter.setPen(QPen(QColor("#17324D")))
        painter.drawText(
            self.rect().adjusted(3, 2, -self.HANDLE_SIZE - 2, -2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            self.label,
        )

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        p = event.pos()
        r = self.rect()
        handle = QRectF(
            r.right() - self.HANDLE_SIZE - 2,
            r.bottom() - self.HANDLE_SIZE - 2,
            self.HANDLE_SIZE + 4,
            self.HANDLE_SIZE + 4,
        )
        if event.button() == Qt.MouseButton.LeftButton and handle.contains(p):
            self._resizing = True
            self._resize_start = p
            self.setSelected(True)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._resizing:
            p = event.pos()
            rect = self.rect()
            scene_rect = self.scene().sceneRect() if self.scene() else QRectF()
            max_width = max(8.0, scene_rect.right() - self.pos().x())
            max_height = max(8.0, scene_rect.bottom() - self.pos().y())
            width = min(max(8.0, p.x()), max_width)
            height = min(max(8.0, p.y()), max_height)
            self.prepareGeometryChange()
            self.setRect(QRectF(rect.left(), rect.top(), width, height))
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        was_resizing = self._resizing
        self._resizing = False
        super().mouseReleaseEvent(event)
        if was_resizing:
            self._notify_geometry()

    def itemChange(self, change, value):  # noqa: ANN001
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            scene_rect = self.scene().sceneRect()
            new_x = min(max(0.0, float(value.x())), max(0.0, scene_rect.width() - self.rect().width()))
            new_y = min(max(0.0, float(value.y())), max(0.0, scene_rect.height() - self.rect().height()))
            return QPointF(new_x, new_y)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._notify_geometry()
        return result

    def _notify_geometry(self) -> None:
        if self.on_change:
            bounds = self.sceneBoundingRect()
            self.on_change(
                self.field_id,
                [float(bounds.x()), float(bounds.y()), float(bounds.right()), float(bounds.bottom())],
            )


class FormCanvas(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.graphics_scene = QGraphicsScene(self)
        self.setScene(self.graphics_scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setBackgroundBrush(QColor("#E8EAED"))
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.page_width = 0.0
        self.page_height = 0.0
        self.field_items: dict[str, FieldOverlayItem] = {}

    def show_page(self, page: fitz.Page, fields: list[dict[str, Any]], on_change) -> None:
        self.graphics_scene.clear()
        self.field_items.clear()
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
        ).copy()
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(2.0)
        background: QGraphicsPixmapItem = self.graphics_scene.addPixmap(pixmap)
        background.setZValue(-100)
        self.page_width = float(page.rect.width)
        self.page_height = float(page.rect.height)
        self.graphics_scene.setSceneRect(QRectF(0, 0, self.page_width, self.page_height))
        for field in fields:
            if int(field.get("page", 0)) != page.number:
                continue
            item = FieldOverlayItem(field, on_change)
            self.graphics_scene.addItem(item)
            self.field_items[field["id"]] = item
        self.fitInView(self.graphics_scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def select_field(self, field_id: str | None) -> None:
        for key, item in self.field_items.items():
            item.setSelected(key == field_id)
            if key == field_id:
                self.centerOn(item)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        if not self.graphics_scene.sceneRect().isEmpty():
            self.fitInView(self.graphics_scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)


class PdfPreviewDialog(QDialog):
    def __init__(self, pdf_bytes: bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filled PDF preview · Print at 100% / Actual Size")
        self.resize(900, 900)
        self.document = fitz.open(stream=pdf_bytes, filetype="pdf")
        self.page_selector = QComboBox()
        for index in range(self.document.page_count):
            self.page_selector.addItem(f"Page {index + 1}", index)
        self.page_selector.currentIndexChanged.connect(self.render_page)
        self.image_label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background:#e8eaed;")
        self.image_label.setMinimumSize(600, 700)
        layout = QVBoxLayout(self)
        warning = QLabel(
            "PRINT WARNING: Choose Actual Size / 100%. Disable Fit to Page, Shrink, or Scale to Fit."
        )
        warning.setStyleSheet("font-weight:700;color:#9b1c1c;padding:8px;")
        layout.addWidget(warning)
        layout.addWidget(self.page_selector)
        layout.addWidget(self.image_label, 1)
        self.render_page(0)

    def render_page(self, index: int) -> None:
        if not self.document.page_count:
            return
        page = self.document[min(max(index, 0), self.document.page_count - 1)]
        pix = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
        image = QImage(
            pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
        ).copy()
        pixmap = QPixmap.fromImage(image).scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(pixmap)

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self.document.close()
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bank Form Automation · Local Only")
        self.resize(1500, 950)
        self.source_path: Path | None = None
        self.source_bytes: bytes | None = None
        self.document: fitz.Document | None = None
        self.fields: list[dict[str, Any]] = []
        self.data: pd.DataFrame | None = None
        self.data_path: Path | None = None
        self.template_path: Path | None = None
        self.templates: list[Path] = []
        self.current_field_id: str | None = None
        self.last_output: Path | None = None

        self._build_ui()
        self._set_status("Ready · all form processing stays on this computer.")
        self._refresh_template_list()

    # ---------- UI construction ----------
    def _build_ui(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        actions = [
            ("Open Form", self.open_source),
            ("Load Excel/CSV", self.load_master),
            ("Auto-detect", self.auto_detect),
            ("Add Text", lambda: self.add_field("text")),
            ("Add Checkbox", lambda: self.add_field("checkbox")),
            ("Add Signature", lambda: self.add_field("signature")),
            ("Save Template", self.save_current_template),
            ("Generate Selected Row", self.generate_one),
            ("Batch ZIP", self.generate_batch_zip),
        ]
        for label, callback in actions:
            action = toolbar.addAction(label)
            action.triggered.connect(callback)

        self.page_selector = QComboBox()
        self.page_selector.setMinimumWidth(100)
        self.page_selector.currentIndexChanged.connect(self.change_page)
        toolbar.addWidget(QLabel("Page "))
        toolbar.addWidget(self.page_selector)
        self.row_selector = QComboBox()
        self.row_selector.setMinimumWidth(230)
        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Customer row "))
        toolbar.addWidget(self.row_selector)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        tabs = QTabWidget()
        fields_page = QWidget()
        fields_layout = QVBoxLayout(fields_page)
        self.field_list = QListWidget()
        self.field_list.currentItemChanged.connect(self.select_field_from_list)
        fields_layout.addWidget(self.field_list)
        remove_button = QPushButton("Remove selected field")
        remove_button.clicked.connect(self.remove_selected_field)
        fields_layout.addWidget(remove_button)
        tabs.addTab(fields_page, "Fields")

        templates_page = QWidget()
        templates_layout = QVBoxLayout(templates_page)
        self.template_list = QListWidget()
        self.template_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.template_list.itemDoubleClicked.connect(self.edit_selected_template)
        templates_layout.addWidget(self.template_list, 1)
        template_buttons = QHBoxLayout()
        add_template_button = QPushButton("Add JSON…")
        add_template_button.clicked.connect(self.add_template_to_list)
        remove_template_button = QPushButton("Remove")
        remove_template_button.clicked.connect(self.remove_selected_templates)
        template_buttons.addWidget(add_template_button)
        template_buttons.addWidget(remove_template_button)
        templates_layout.addLayout(template_buttons)
        tabs.addTab(templates_page, "Templates")
        left_layout.addWidget(tabs)
        left.setMinimumWidth(230)
        splitter.addWidget(left)

        self.canvas = FormCanvas()
        self.canvas.graphics_scene.selectionChanged.connect(self._canvas_selection_changed)
        splitter.addWidget(self.canvas)

        properties = QWidget()
        properties_layout = QVBoxLayout(properties)
        properties_title = QLabel("Selected field")
        properties_title.setStyleSheet("font-size:16px;font-weight:700;")
        properties_layout.addWidget(properties_title)
        form = QFormLayout()
        self.label_edit = QLineEdit()
        self.data_key_combo = QComboBox()
        self.data_key_combo.setEditable(True)
        self.type_combo = QComboBox()
        self.type_combo.addItem("Text", "text")
        self.type_combo.addItem("Checkbox", "checkbox")
        self.type_combo.addItem("Signature image", "signature")
        self.font_spin = QDoubleSpinBox()
        self.font_spin.setRange(4.0, 48.0)
        self.font_spin.setSingleStep(0.5)
        self.font_spin.setValue(10.0)
        self.min_font_spin = QDoubleSpinBox()
        self.min_font_spin.setRange(3.0, 24.0)
        self.min_font_spin.setValue(5.0)
        self.align_combo = QComboBox()
        self.align_combo.addItems(["left", "center", "right"])
        self.format_combo = QComboBox()
        self.format_combo.addItem("No formatting", "none")
        self.format_combo.addItem("Date", "date")
        self.format_combo.addItem("Currency", "currency")
        self.format_combo.addItem("UPPERCASE", "upper")
        self.format_combo.addItem("lowercase", "lower")
        self.date_format_edit = QLineEdit("%d-%m-%Y")
        self.currency_prefix_edit = QLineEdit("Rs. ")
        self.currency_decimals_spin = QSpinBox()
        self.currency_decimals_spin.setRange(0, 4)
        self.currency_decimals_spin.setValue(2)
        self.wrap_check = QCheckBox("Allow wrapping onto multiple lines")
        self.signature_path_edit = QLineEdit()
        signature_button = QPushButton("Browse…")
        signature_button.clicked.connect(self.choose_signature)
        signature_row = QWidget()
        signature_layout = QHBoxLayout(signature_row)
        signature_layout.setContentsMargins(0, 0, 0, 0)
        signature_layout.addWidget(self.signature_path_edit, 1)
        signature_layout.addWidget(signature_button)
        self.confidence_label = QLabel("—")
        self.confidence_label.setWordWrap(True)
        form.addRow("Label", self.label_edit)
        form.addRow("Master-data column", self.data_key_combo)
        form.addRow("Field type", self.type_combo)
        form.addRow("Font size", self.font_spin)
        form.addRow("Minimum size", self.min_font_spin)
        form.addRow("Alignment", self.align_combo)
        form.addRow("Value format", self.format_combo)
        form.addRow("Date format", self.date_format_edit)
        form.addRow("Currency prefix", self.currency_prefix_edit)
        form.addRow("Currency decimals", self.currency_decimals_spin)
        form.addRow("Text fit", self.wrap_check)
        form.addRow("Signature path", signature_row)
        form.addRow("Detection confidence", self.confidence_label)
        properties_layout.addLayout(form)
        properties_layout.addStretch(1)
        self.apply_button = QPushButton("Apply field properties")
        self.apply_button.clicked.connect(self.apply_field_properties)
        properties_layout.addWidget(self.apply_button)
        properties.setMinimumWidth(320)
        splitter.addWidget(properties)
        splitter.setSizes([250, 900, 350])

        self.status_label = QLabel()
        self.statusBar().addWidget(self.status_label, 1)
        self._set_property_controls_enabled(False)

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def _set_property_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.label_edit, self.data_key_combo, self.type_combo, self.font_spin,
            self.min_font_spin, self.align_combo, self.format_combo, self.date_format_edit,
            self.currency_prefix_edit, self.currency_decimals_spin, self.wrap_check, self.signature_path_edit,
            self.apply_button,
        ):
            widget.setEnabled(enabled)

    # ---------- source form ----------
    def open_source(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open original PDF or image", "",
            "Forms (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)",
        )
        if not path:
            return
        try:
            self._set_source(path)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open form", str(exc))

    def _set_source(self, path: str | Path) -> None:
        source = Path(path).expanduser().resolve()
        from formbuilder.template import source_as_pdf_bytes

        payload = source_as_pdf_bytes(source)
        if self.document:
            self.document.close()
        self.source_path = source
        self.source_bytes = payload
        self.document = fitz.open(stream=payload, filetype="pdf")
        self.fields = []
        self.template_path = None
        self.current_field_id = None
        self.page_selector.blockSignals(True)
        self.page_selector.clear()
        for number in range(self.document.page_count):
            self.page_selector.addItem(f"{number + 1} / {self.document.page_count}", number)
        self.page_selector.blockSignals(False)
        self.page_selector.setCurrentIndex(0)
        self._refresh_fields()
        self._show_current_page()
        self._set_status(
            f"Loaded {source.name} · {self.document.page_count} page(s) · source file is read-only."
        )

    def _show_current_page(self) -> None:
        if not self.document or self.document.page_count == 0:
            return
        page_index = self.page_selector.currentData()
        if page_index is None:
            page_index = 0
        self.canvas.show_page(self.document[int(page_index)], self.fields, self._field_geometry_changed)
        self.canvas.select_field(self.current_field_id)

    def change_page(self, index: int) -> None:
        if index >= 0 and self.document:
            self._show_current_page()

    # ---------- master data ----------
    def load_master(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load local master data", "", "Excel/CSV (*.xlsx *.xlsm *.csv)"
        )
        if not path:
            return
        try:
            self.data = load_master_data(path)
            self.data_path = Path(path)
            self.row_selector.clear()
            for index, row in self.data.iterrows():
                first = str(row.iloc[0]) if len(row) else ""
                self.row_selector.addItem(f"{index + 1}: {first[:45]}", int(index))
            self.data_key_combo.clear()
            self.data_key_combo.addItems([str(column) for column in self.data.columns])
            if self.data_key_combo.isEditable():
                self.data_key_combo.setCurrentText("")
            self._set_status(
                f"Loaded {len(self.data)} row(s), {len(self.data.columns)} column(s) from {Path(path).name}."
            )
        except Exception as exc:
            QMessageBox.critical(self, "Master data error", str(exc))

    def _current_record(self) -> dict[str, Any]:
        if self.data is None:
            raise ValueError("Load an Excel/CSV master-data file first.")
        index = self.row_selector.currentData()
        if index is None:
            raise ValueError("Select a customer row first.")
        return record_from_row(self.data, int(index))

    # ---------- field editor ----------
    def add_field(self, field_type: str) -> None:
        if not self.document:
            QMessageBox.information(self, "Open a form first", "Choose a PDF or image before adding fields.")
            return
        page_index = int(self.page_selector.currentData() or 0)
        page = self.document[page_index]
        x0 = min(55.0, max(5.0, page.rect.width * 0.1))
        y0 = min(75.0, max(5.0, page.rect.height * 0.1))
        width, height = (16.0, 16.0) if field_type == "checkbox" else ((170.0, 36.0) if field_type == "signature" else (180.0, 22.0))
        field = make_field(
            page=page_index,
            rect=[x0, y0, min(page.rect.width - 5, x0 + width), min(page.rect.height - 5, y0 + height)],
            label=f"New {field_type}",
            data_key=str(self.data.columns[0]) if self.data is not None and len(self.data.columns) else "",
            field_type=field_type,
        )
        self.fields.append(field)
        self.current_field_id = field["id"]
        self._refresh_fields()
        self._show_current_page()
        self._select_field_in_list(field["id"])

    def auto_detect(self) -> None:
        if not self.source_path:
            QMessageBox.information(self, "Open a form first", "Choose a PDF or image before auto-detection.")
            return
        try:
            proposed, messages = detect_fields(self.source_path)
            def overlaps(left: list[float], right: list[float]) -> bool:
                x0 = max(left[0], right[0])
                y0 = max(left[1], right[1])
                x1 = min(left[2], right[2])
                y1 = min(left[3], right[3])
                intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
                smaller = min(
                    (left[2] - left[0]) * (left[3] - left[1]),
                    (right[2] - right[0]) * (right[3] - right[1]),
                )
                return smaller > 0 and intersection / smaller > 0.45

            for candidate in proposed:
                if not any(
                    int(existing["page"]) == int(candidate["page"])
                    and overlaps(existing["rect"], candidate["rect"])
                    for existing in self.fields
                ):
                    self.fields.append(candidate)
            self._refresh_fields()
            self._show_current_page()
            QMessageBox.information(self, "Detection review", "\n".join(messages))
        except Exception as exc:
            QMessageBox.critical(self, "Auto-detection failed", str(exc))

    def remove_selected_field(self) -> None:
        field = self._get_current_field()
        if not field:
            return
        self.fields = [item for item in self.fields if item["id"] != field["id"]]
        self.current_field_id = None
        self._refresh_fields()
        self._show_current_page()
        self._set_property_controls_enabled(False)

    def _refresh_fields(self) -> None:
        self.field_list.blockSignals(True)
        self.field_list.clear()
        for field in self.fields:
            confidence = float(field.get("confidence", 1.0))
            key = field.get("data_key") or "unmapped"
            item = QListWidgetItem(
                f"{field.get('label', 'Field')}  →  {key}  [{confidence:.0%}]"
            )
            item.setData(Qt.ItemDataRole.UserRole, field["id"])
            self.field_list.addItem(item)
        self.field_list.blockSignals(False)
        if self.current_field_id:
            self._select_field_in_list(self.current_field_id)

    def _select_field_in_list(self, field_id: str) -> None:
        for index in range(self.field_list.count()):
            item = self.field_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == field_id:
                self.field_list.setCurrentItem(item)
                return

    def _get_current_field(self) -> dict[str, Any] | None:
        return next((field for field in self.fields if field["id"] == self.current_field_id), None)

    def select_field_from_list(self, current, previous) -> None:  # noqa: ANN001
        if current is None:
            return
        field_id = current.data(Qt.ItemDataRole.UserRole)
        self.current_field_id = field_id
        field = self._get_current_field()
        if field is None:
            return
        page_index = int(field["page"])
        if self.page_selector.currentData() != page_index:
            self.page_selector.setCurrentIndex(page_index)
        self.canvas.select_field(field_id)
        self._load_property_controls(field)

    def _canvas_selection_changed(self) -> None:
        selected = self.canvas.graphics_scene.selectedItems()
        if not selected:
            return
        item = selected[0]
        if not isinstance(item, FieldOverlayItem):
            return
        self.current_field_id = item.field_id
        self._select_field_in_list(item.field_id)
        field = self._get_current_field()
        if field:
            self._load_property_controls(field)

    def _field_geometry_changed(self, field_id: str, rect: list[float]) -> None:
        field = next((item for item in self.fields if item["id"] == field_id), None)
        if field is not None:
            field["rect"] = rect

    def _load_property_controls(self, field: dict[str, Any]) -> None:
        self._set_property_controls_enabled(True)
        self.label_edit.setText(str(field.get("label", "")))
        self.data_key_combo.setCurrentText(str(field.get("data_key", "")))
        index = self.type_combo.findData(field.get("type", "text"))
        self.type_combo.setCurrentIndex(max(index, 0))
        self.font_spin.setValue(float(field.get("font_size", 10.0)))
        self.min_font_spin.setValue(float(field.get("min_font_size", 5.0)))
        self.align_combo.setCurrentText(str(field.get("align", "left")))
        format_index = self.format_combo.findData(field.get("format", "none"))
        self.format_combo.setCurrentIndex(max(format_index, 0))
        self.date_format_edit.setText(str(field.get("date_format", "%d-%m-%Y")))
        self.currency_prefix_edit.setText(str(field.get("currency_prefix", "Rs. ")))
        self.currency_decimals_spin.setValue(int(field.get("currency_decimals", 2)))
        self.wrap_check.setChecked(bool(field.get("wrap", False)))
        self.signature_path_edit.setText(str(field.get("signature_path", "")))
        self.confidence_label.setText(
            f"{float(field.get('confidence', 1.0)):.0%} · {field.get('detection_source', 'manual')}"
        )

    def apply_field_properties(self) -> None:
        field = self._get_current_field()
        if not field:
            return
        field.update(
            {
                "label": self.label_edit.text().strip() or "Field",
                "data_key": self.data_key_combo.currentText().strip(),
                "type": self.type_combo.currentData(),
                "font_size": self.font_spin.value(),
                "min_font_size": min(self.min_font_spin.value(), self.font_spin.value()),
                "align": self.align_combo.currentText(),
                "format": self.format_combo.currentData(),
                "date_format": self.date_format_edit.text().strip() or "%d-%m-%Y",
                "currency_prefix": self.currency_prefix_edit.text(),
                "currency_decimals": self.currency_decimals_spin.value(),
                "wrap": self.wrap_check.isChecked(),
                "signature_path": self.signature_path_edit.text().strip(),
            }
        )
        self._refresh_fields()
        self._show_current_page()
        self._select_field_in_list(field["id"])

    def choose_signature(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select local signature image", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp)"
        )
        if path:
            self.signature_path_edit.setText(path)

    # ---------- templates ----------
    def save_current_template(self) -> None:
        if not self.source_path:
            QMessageBox.information(self, "Open a form first", "Choose a PDF or image before saving a template.")
            return
        default_dir = str(Path.cwd() / "templates")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save editable template", default_dir, "Form template (*.json)"
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            save_template(path, self.source_path, self.fields)
            self.template_path = Path(path)
            self._add_template_path(Path(path))
            self._set_status(f"Template saved: {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Could not save template", str(exc))

    def _add_template_path(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved not in self.templates:
            self.templates.append(resolved)
        self._refresh_template_list()
        for index in range(self.template_list.count()):
            item = self.template_list.item(index)
            if Path(item.data(Qt.ItemDataRole.UserRole)).resolve() == resolved:
                item.setSelected(True)

    def _refresh_template_list(self) -> None:
        self.template_list.clear()
        for path in self.templates:
            item = QListWidgetItem(path.name)
            item.setToolTip(str(path))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.template_list.addItem(item)

    def add_template_to_list(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add saved form templates", "", "Templates (*.json)"
        )
        for path in paths:
            self._add_template_path(Path(path))

    def edit_selected_template(self, item: QListWidgetItem) -> None:
        try:
            path = Path(item.data(Qt.ItemDataRole.UserRole))
            template = self._load_template_json(path)
            self._set_source(template["source_path"])
            self.fields = template["fields"]
            self.template_path = path.resolve()
            self._refresh_fields()
            self._show_current_page()
            self._set_status(f"Editing template: {path.name}")
        except Exception as exc:
            QMessageBox.critical(self, "Could not open template", str(exc))

    def remove_selected_templates(self) -> None:
        chosen = {Path(item.data(Qt.ItemDataRole.UserRole)).resolve() for item in self.template_list.selectedItems()}
        self.templates = [path for path in self.templates if path.resolve() not in chosen]
        self._refresh_template_list()

    def _load_template_json(self, path: Path) -> dict[str, Any]:
        template = json.loads(path.read_text(encoding="utf-8"))
        if template.get("template_version") != 1:
            raise ValueError("Unsupported template version.")
        source = Path(template.get("source_path", ""))
        if not source.is_absolute():
            source = path.resolve().parent / source
        if not source.is_file():
            relink, _ = QFileDialog.getOpenFileName(
                self, "Locate the original form for this template", "",
                "Forms (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)",
            )
            if not relink:
                raise FileNotFoundError("Original source form was not linked.")
            source = Path(relink).resolve()
        actual_hash = sha256_bytes(source.read_bytes())
        if template.get("source_sha256") != actual_hash:
            answer = QMessageBox.warning(
                self, "Source fingerprint mismatch",
                "This source file differs from the one used when the template was saved. "
                "Continue only after confirming it is the correct original form.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                raise ValueError("Template loading was cancelled because the source fingerprint differs.")
            template["source_sha256"] = actual_hash
        template["source_path"] = str(source)
        template["fields"] = [normalize_field(field, i) for i, field in enumerate(template.get("fields", []))]
        return template

    def _selected_template_paths(self) -> list[Path]:
        selected = [Path(item.data(Qt.ItemDataRole.UserRole)) for item in self.template_list.selectedItems()]
        if selected:
            return selected
        if self.template_path and self.template_path.is_file():
            return [self.template_path]
        raise ValueError("Select one or more saved templates, or save the current template first.")

    # ---------- generation ----------
    def generate_one(self) -> None:
        try:
            record = self._current_record()
            templates = self._selected_template_paths()
            output_dir = QFileDialog.getExistingDirectory(self, "Choose output folder")
            if not output_dir:
                return
            first_output: Path | None = None
            for template_path in templates:
                template = self._load_template_json(template_path)
                base = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(template["source_path"]).stem).strip("_") or "form"
                row_index = int(self.row_selector.currentData()) + 1
                customer_name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(next(iter(record.values()), "customer"))).strip("_")
                template_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", template_path.stem).strip("_") or "template"
                filename = f"{base}_{template_tag}_{customer_name or 'customer'}_{row_index}.pdf"
                target = Path(output_dir) / filename
                generate_filled_pdf(template["source_path"], template["fields"], record, target)
                if first_output is None:
                    first_output = target
            self.last_output = first_output
            if first_output:
                self._preview_file(first_output)
            self._set_status(f"Generated {len(templates)} flattened PDF(s). Print at 100% / Actual Size.")
        except Exception as exc:
            QMessageBox.critical(self, "PDF generation failed", str(exc))

    def generate_batch_zip(self) -> None:
        if self.data is None:
            QMessageBox.information(self, "Load data first", "Load your local Excel/CSV master-data sheet.")
            return
        try:
            templates = self._selected_template_paths()
            zip_path, _ = QFileDialog.getSaveFileName(
                self, "Export all customer PDFs as ZIP", "bank_forms_batch.zip", "ZIP archive (*.zip)"
            )
            if not zip_path:
                return
            if not zip_path.lower().endswith(".zip"):
                zip_path += ".zip"
            total = len(self.data) * len(templates)
            progress = QProgressDialog("Generating customer PDFs…", "Cancel", 0, total, self)
            progress.setWindowTitle("Batch export")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            completed = 0
            with tempfile.TemporaryDirectory(prefix="bank-form-batch-") as temp_dir:
                temp_root = Path(temp_dir)
                generated_files: list[Path] = []
                for template_index, template_path in enumerate(templates, start=1):
                    template = self._load_template_json(template_path)
                    source_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(template["source_path"]).stem).strip("_") or "form"
                    template_tag = re.sub(r"[^A-Za-z0-9_-]+", "_", template_path.stem).strip("_") or f"template_{template_index}"
                    for row_index in range(len(self.data)):
                        if progress.wasCanceled():
                            return
                        record = record_from_row(self.data, row_index)
                        customer = re.sub(
                            r"[^A-Za-z0-9_-]+", "_",
                            str(next(iter(record.values()), "customer")),
                        ).strip("_") or "customer"
                        output = temp_root / f"{source_stem}_{template_tag}_{customer}_{row_index + 1}.pdf"
                        generate_filled_pdf(template["source_path"], template["fields"], record, output)
                        generated_files.append(output)
                        completed += 1
                        progress.setValue(completed)
                        progress.setLabelText(f"Generated {completed} of {total}: {output.name}")
                        from PySide6.QtWidgets import QApplication
                        QApplication.processEvents()
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for generated in generated_files:
                        archive.write(generated, generated.name)
            progress.setValue(total)
            QMessageBox.information(
                self, "Batch complete",
                f"Created {completed} PDF(s) in:\n{zip_path}\n\nEach PDF keeps its original page size. Print at 100% / Actual Size.",
            )
            self._set_status(f"Batch ZIP created: {Path(zip_path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Batch export failed", str(exc))

    def _preview_file(self, path: Path) -> None:
        try:
            payload = path.read_bytes()
            dialog = PdfPreviewDialog(payload, self)
            dialog.exec()
        except Exception as exc:
            QMessageBox.warning(self, "Preview unavailable", f"PDF was generated, but preview failed: {exc}")

    def closeEvent(self, event) -> None:  # noqa: ANN001
        if self.document:
            self.document.close()
        super().closeEvent(event)
