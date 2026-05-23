#!/usr/bin/env python3
"""
Sub Asset Widget — offline-first desktop subscription tracker.

Track monthly burn rate, billing dates, and renewal alerts on your desktop.
All data is stored locally in SQLite (sub_asset.db).
"""

from __future__ import annotations

import sys
from datetime import date

from PyQt6.QtCore import QEvent, QObject, Qt, QPoint, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QCloseEvent,
    QContextMenuEvent,
    QFont,
    QMouseEvent,
    QPainter,
    QPen,
    QColor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import database as db
import autostart

MIN_WIDGET_WIDTH = 260
MIN_WIDGET_HEIGHT = 220
MAX_WIDGET_WIDTH = 500
MAX_WIDGET_HEIGHT = 720
RESIZE_MARGIN = 14

OPACITY_PRESETS: tuple[tuple[str, float], ...] = (
    ("20%", 0.2),
    ("50%", 0.5),
    ("80%", 0.8),
    ("100% (Opaque)", 1.0),
)


def opacity_to_alpha(opacity: float) -> int:
    clamped = max(0.2, min(1.0, opacity))
    return int(clamped * 255)


def format_currency(amount: float, currency: str = db.DEFAULT_CURRENCY) -> str:
    if currency == "USD":
        return f"${amount:,.2f}"
    return f"{amount:,.2f} {currency}"


def format_subscription_line(sub: dict) -> str:
    dday = sub.get("dday")
    if isinstance(dday, int) and dday != 9999:
        dday_text = db.format_dday(dday)
    else:
        dday_text = "?"
    return f"{sub['name']} - {format_currency(sub['cost'], sub.get('currency', 'USD'))} ({dday_text})"


def billing_label_style(dday: int) -> str:
    if dday <= 0:
        return "alert_danger"
    if dday <= db.URGENT_BILLING_DAYS:
        return "alert_warn"
    return "billing"


class SubscriptionManageDialog(QDialog):
    """Add, edit, and remove subscriptions."""

    data_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage Subscriptions")
        self.setMinimumSize(480, 560)
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self._auto_start_updating = False
        self._build_ui()
        self._refresh_list()
        self._load_auto_start_checkbox()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.accept()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        preset_group = QGroupBox("Quick Presets")
        preset_row = QHBoxLayout(preset_group)
        for name in db.QUICK_PRESET_NAMES:
            btn = QPushButton(name)
            btn.clicked.connect(lambda _checked=False, n=name: self._apply_preset(n))
            preset_row.addWidget(btn)
        layout.addWidget(preset_group)

        form_group = QGroupBox("Subscription Details")
        form = QFormLayout(form_group)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. Netflix")
        self.cost_input = QDoubleSpinBox()
        self.cost_input.setRange(0, 10_000)
        self.cost_input.setDecimals(2)
        self.cost_input.setPrefix("$ ")
        self.cycle_input = QComboBox()
        self.cycle_input.addItem("Monthly", "monthly")
        self.cycle_input.addItem("Yearly", "yearly")
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDate(date.today())
        self.date_input.setDisplayFormat("yyyy-MM-dd")
        form.addRow("Name", self.name_input)
        form.addRow("Cost ($)", self.cost_input)
        form.addRow("Billing Cycle", self.cycle_input)
        form.addRow("Next Billing Date (YYYY-MM-DD)", self.date_input)
        layout.addWidget(form_group)

        add_btn = QPushButton("Add Subscription")
        add_btn.clicked.connect(self._add_subscription)
        layout.addWidget(add_btn)

        list_group = QGroupBox("Your Subscriptions")
        list_layout = QVBoxLayout(list_group)
        self.sub_list = QListWidget()
        del_btn = QPushButton("Delete Selected")
        del_btn.clicked.connect(self._delete_subscription)
        list_layout.addWidget(self.sub_list)
        list_layout.addWidget(del_btn)
        layout.addWidget(list_group)

        system_group = QGroupBox("System Settings")
        system_layout = QVBoxLayout(system_group)
        self.auto_start_checkbox = QCheckBox("Launch automatically on system startup")
        self.auto_start_checkbox.setEnabled(autostart.is_supported_os())
        self.auto_start_checkbox.stateChanged.connect(self._on_auto_start_changed)
        system_layout.addWidget(self.auto_start_checkbox)
        if not autostart.is_supported_os():
            hint = QLabel("Auto-start is not supported on this operating system.")
            hint.setStyleSheet("color: #888888; font-size: 11px;")
            system_layout.addWidget(hint)
        layout.addWidget(system_group)

        buttons = QDialogButtonBox()
        close_btn = buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        close_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_preset(self, preset_name: str) -> None:
        preset = db.get_preset(preset_name)
        if preset is None:
            return
        self.name_input.setText(preset["name"])
        self.cost_input.setValue(float(preset["cost"]))
        cycle_idx = self.cycle_input.findData(preset["billing_cycle"])
        if cycle_idx >= 0:
            self.cycle_input.setCurrentIndex(cycle_idx)
        self.date_input.setFocus()

    def _refresh_list(self) -> None:
        self.sub_list.clear()
        for sub in db.sorted_subscriptions_with_dday():
            dday = sub["dday"]
            dday_text = db.format_dday(dday) if dday != 9999 else "?"
            text = (
                f"{sub['name']}  ·  {format_currency(sub['cost'], sub.get('currency', 'USD'))}"
                f"  ·  {sub['next_billing_date']} ({dday_text})"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, sub["id"])
            self.sub_list.addItem(item)

    def _load_auto_start_checkbox(self) -> None:
        self._auto_start_updating = True
        db_enabled = db.get_setting("auto_start", "0") == "1"
        os_registered = autostart.is_auto_start_registered()
        checked = db_enabled or os_registered
        if db_enabled != os_registered:
            checked = os_registered
            db.set_setting("auto_start", "1" if os_registered else "0")
        self.auto_start_checkbox.setChecked(checked)
        self._auto_start_updating = False

    def _on_auto_start_changed(self, state: int) -> None:
        if self._auto_start_updating:
            return

        enabled = state == int(Qt.CheckState.Checked.value)
        ok, message = autostart.set_auto_start(enabled)
        if ok:
            db.set_setting("auto_start", "1" if enabled else "0")
            QMessageBox.information(self, "Auto-Start", message)
        else:
            self._auto_start_updating = True
            self.auto_start_checkbox.setChecked(not enabled)
            self._auto_start_updating = False
            QMessageBox.warning(self, "Auto-Start Error", message)

    def _add_subscription(self) -> None:
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Input Error", "Please enter a subscription name.")
            return
        ok = db.add_subscription(
            name=name,
            cost=float(self.cost_input.value()),
            currency=db.DEFAULT_CURRENCY,
            billing_cycle=str(self.cycle_input.currentData()),
            next_billing_date=self.date_input.date().toString("yyyy-MM-dd"),
        )
        if ok:
            self._refresh_list()
            self.data_changed.emit()
            QMessageBox.information(self, "Added", f"'{name}' has been added.")
        else:
            QMessageBox.warning(self, "Error", "Failed to add subscription.")

    def _delete_subscription(self) -> None:
        item = self.sub_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Notice", "Please select a subscription to delete.")
            return
        sub_id = int(item.data(Qt.ItemDataRole.UserRole))
        if db.delete_subscription(sub_id):
            self._refresh_list()
            self.data_changed.emit()
        else:
            QMessageBox.warning(self, "Error", "Failed to delete subscription.")


class PanelWidget(QWidget):
    """Semi-transparent panel background — child labels stay fully opaque."""

    CORNER_RADIUS = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self._background_alpha = 255
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

    def set_background_alpha(self, alpha: int) -> None:
        self._background_alpha = max(51, min(255, alpha))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        fill = QColor(30, 30, 30, self._background_alpha)
        border = QColor(255, 255, 255, 18)
        painter.setBrush(fill)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, self.CORNER_RADIUS, self.CORNER_RADIUS)
        painter.end()
        super().paintEvent(event)


class DesktopWidget(QWidget):
    """Minimal desktop widget for subscription asset tracking."""

    REFRESH_MS = 60_000

    def __init__(self) -> None:
        super().__init__()
        self._drag_pos: QPoint | None = None
        self._resize_origin: QPoint | None = None
        self._resize_start_size: QSize | None = None
        self._position_locked = db.get_setting_bool("position_locked", False)
        self._panel: PanelWidget | None = None
        self._init_window()
        self._build_ui()
        self._apply_visual_settings()
        self._restore_window_state()
        self.refresh_display()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_display)
        self._timer.start(self.REFRESH_MS)

    def _label_stylesheet(self) -> str:
        return """
            QLabel {
                color: #b0b0b0;
                background: transparent;
            }
            QLabel#title {
                font-size: 13px;
                font-weight: 600;
                color: #f0f0f0;
                letter-spacing: 0.5px;
                background: transparent;
            }
            QLabel#spend {
                font-size: 15px;
                font-weight: 700;
                color: #ffffff;
                background: transparent;
            }
            QLabel#section {
                font-size: 11px;
                font-weight: 600;
                color: #707070;
                margin-top: 2px;
                background: transparent;
            }
            QLabel#billing {
                font-size: 11px;
                color: #c8c8c8;
                background: transparent;
            }
            QLabel#alert_warn {
                font-size: 11px;
                font-weight: 600;
                color: #ff9800;
                background: transparent;
            }
            QLabel#alert_danger {
                font-size: 11px;
                font-weight: 600;
                color: #ef5350;
                background: transparent;
            }
            QLabel#footer {
                font-size: 9px;
                color: #505050;
                background: transparent;
            }
        """

    def _current_background_opacity(self) -> float:
        return db.get_setting_float("window_opacity", 1.0)

    def _apply_visual_settings(self) -> None:
        opacity = self._current_background_opacity()
        alpha = opacity_to_alpha(opacity)
        db.set_setting("panel_alpha", str(alpha))
        if self._panel is not None:
            self._panel.set_background_alpha(alpha)
            self._panel.setStyleSheet(self._label_stylesheet())

    def _restore_window_state(self) -> None:
        width = db.get_setting_int("window_width", 300)
        height = db.get_setting_int("window_height", 380)
        width = max(MIN_WIDGET_WIDTH, min(MAX_WIDGET_WIDTH, width))
        height = max(MIN_WIDGET_HEIGHT, min(MAX_WIDGET_HEIGHT, height))
        self.resize(width, height)

        x_raw = db.get_setting("window_x")
        y_raw = db.get_setting("window_y")
        if x_raw != "" and y_raw != "":
            try:
                self.move(int(x_raw), int(y_raw))
                return
            except (TypeError, ValueError):
                x_raw = ""
                y_raw = ""

        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(100, 100)
            return
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - self.width()) // 2,
            geo.y() + (geo.height() - self.height()) // 2,
        )

    def save_widget_state(self) -> None:
        db.save_widget_geometry(self.x(), self.y(), self.width(), self.height())
        db.set_setting("window_opacity", str(self._current_background_opacity()))
        db.set_setting("position_locked", str(self._position_locked).lower())

    def show_and_focus(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _init_window(self) -> None:
        if sys.platform == "darwin":
            flags = (
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowStaysOnTopHint
            )
        elif sys.platform == "win32":
            flags = (
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnBottomHint
                | Qt.WindowType.Tool
                | Qt.WindowType.SubWindow
            )
        else:
            flags = (
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowStaysOnTopHint
            )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setMinimumSize(MIN_WIDGET_WIDTH, MIN_WIDGET_HEIGHT)
        self.setMaximumSize(MAX_WIDGET_WIDTH, MAX_WIDGET_HEIGHT)

    def _in_resize_zone(self, pos: QPoint) -> bool:
        return (
            pos.x() >= self.width() - RESIZE_MARGIN
            and pos.y() >= self.height() - RESIZE_MARGIN
        )

    def _register_context_menu(self, widget: QWidget) -> None:
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(self._on_child_context_menu)
        widget.installEventFilter(self)

    def _on_child_context_menu(self, pos: QPoint) -> None:
        sender = self.sender()
        if isinstance(sender, QWidget):
            global_pos = sender.mapToGlobal(pos)
            local_pos = self.mapFromGlobal(global_pos)
            self._show_context_menu(local_pos)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[name-defined]
        if event.type() == QEvent.Type.MouseButtonPress:
            mouse_event = event
            if isinstance(mouse_event, QMouseEvent):
                if mouse_event.button() == Qt.MouseButton.RightButton:
                    if isinstance(watched, QWidget):
                        global_pos = mouse_event.globalPosition().toPoint()
                        local_pos = self.mapFromGlobal(global_pos)
                        self._show_context_menu(local_pos)
                        return True
        return super().eventFilter(watched, event)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)

        panel = PanelWidget()
        self._panel = panel
        panel.setStyleSheet(self._label_stylesheet())

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 16, 18, 14)
        panel_layout.setSpacing(4)
        panel_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = QLabel("SUBSCRIPTIONS")
        title.setObjectName("title")
        panel_layout.addWidget(title)

        self.spend_label = QLabel()
        self.spend_label.setObjectName("spend")
        self.spend_label.setWordWrap(True)
        panel_layout.addWidget(self.spend_label)

        panel_layout.addSpacing(6)

        billing_header = QLabel("All Subscriptions")
        billing_header.setObjectName("section")
        panel_layout.addWidget(billing_header)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._scroll_area.setStyleSheet(
            """
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: rgba(40, 40, 40, 120);
                width: 6px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: rgba(120, 120, 120, 180);
                border-radius: 3px;
                min-height: 20px;
            }
            """
        )

        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet("background: transparent;")
        self.billing_container = QVBoxLayout(self._scroll_content)
        self.billing_container.setSpacing(2)
        self.billing_container.setContentsMargins(0, 0, 4, 0)
        self.billing_container.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll_area.setWidget(self._scroll_content)
        panel_layout.addWidget(self._scroll_area, stretch=1)

        panel_layout.addSpacing(4)

        alert_header = QLabel("Renewal Alerts")
        alert_header.setObjectName("section")
        panel_layout.addWidget(alert_header)

        alert_wrapper = QWidget()
        self.alert_container = QVBoxLayout(alert_wrapper)
        self.alert_container.setSpacing(1)
        self.alert_container.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.alert_container.setContentsMargins(0, 0, 0, 0)
        panel_layout.addWidget(alert_wrapper)

        panel_layout.addSpacing(2)

        self.footer_label = QLabel("Right-click to manage")
        self.footer_label.setObjectName("footer")
        self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addWidget(self.footer_label)

        outer.addWidget(panel)

        self._register_context_menu(self)
        self._register_context_menu(panel)
        self._register_context_menu(self._scroll_area)
        self._register_context_menu(self._scroll_content)
        for child in panel.findChildren(QLabel):
            self._register_context_menu(child)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.save_widget_state()
        event.accept()

    def contextMenuEvent(self, event: QContextMenuEvent | None) -> None:
        if event is not None:
            self._show_context_menu(event.pos())
            event.accept()

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _update_footer(self) -> None:
        lock_text = "Locked" if self._position_locked else "Drag to move · resize corner"
        opacity_pct = int(self._current_background_opacity() * 100)
        self.footer_label.setText(f"Right-click · {lock_text} · BG {opacity_pct}%")

    def refresh_display(self) -> None:
        try:
            total_usd = db.calculate_monthly_total_usd()
            other = {k: v for k, v in db.calculate_monthly_totals().items() if k != "USD"}

            spend_text = f"Total Monthly Burn Rate: {format_currency(total_usd)}"
            if other:
                extras = "  ".join(format_currency(v, k) for k, v in sorted(other.items()))
                spend_text += f"\n    {extras}"
            self.spend_label.setText(spend_text)

            self._clear_layout(self.billing_container)
            all_subs = db.sorted_subscriptions_with_dday()
            if all_subs:
                for sub in all_subs:
                    dday = int(sub["dday"])
                    lbl = QLabel(format_subscription_line(sub))
                    lbl.setObjectName(billing_label_style(dday))
                    lbl.setWordWrap(True)
                    lbl.setAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                    )
                    self.billing_container.addWidget(lbl)
                    self._register_context_menu(lbl)
            else:
                lbl = QLabel("  No subscriptions yet")
                lbl.setObjectName("billing")
                self.billing_container.addWidget(lbl)
                self._register_context_menu(lbl)

            self.billing_container.addStretch(1)

            self._clear_layout(self.alert_container)
            urgent = db.urgent_billings()
            if urgent:
                for sub in urgent:
                    dday = sub["dday"]
                    dday_text = db.format_dday(dday)
                    obj_name = "alert_danger" if dday <= 0 else "alert_warn"
                    text = f"Warning: {sub['name']} Renewing Soon! ({dday_text})"
                    lbl = QLabel(f"⚠️ {text}")
                    lbl.setObjectName(obj_name)
                    lbl.setWordWrap(True)
                    self.alert_container.addWidget(lbl)
                    self._register_context_menu(lbl)
            else:
                lbl = QLabel("  No upcoming renewals")
                lbl.setObjectName("billing")
                self.alert_container.addWidget(lbl)
                self._register_context_menu(lbl)

            self._update_footer()
        except Exception:
            self.spend_label.setText("Failed to load data")
            self._clear_layout(self.billing_container)
            self._clear_layout(self.alert_container)

    def _set_opacity(self, opacity: float) -> None:
        opacity = max(0.2, min(1.0, opacity))
        db.set_setting("window_opacity", str(opacity))
        db.set_setting("panel_alpha", str(opacity_to_alpha(opacity)))
        self._apply_visual_settings()
        self._update_footer()

    def _toggle_position_lock(self) -> None:
        self._position_locked = not self._position_locked
        db.set_setting("position_locked", str(self._position_locked).lower())
        if self._position_locked:
            self.save_widget_state()
        self._update_footer()

    def _open_manage_dialog(self) -> None:
        dialog = SubscriptionManageDialog(parent=self)
        dialog.data_changed.connect(self.refresh_display)
        dialog.finished.connect(lambda _result: self._on_manage_dialog_closed())
        dialog.exec()

    def _on_manage_dialog_closed(self) -> None:
        self.refresh_display()
        self.show_and_focus()

    def _quit_application(self) -> None:
        self.save_widget_state()
        QApplication.instance().quit()

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: rgba(30, 30, 30, 240);
                color: #eeeeee;
                border: 1px solid rgba(255, 255, 255, 30);
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 24px;
            }
            QMenu::item:selected {
                background-color: rgba(80, 80, 80, 200);
            }
            """
        )
        menu.addAction("Manage Subscriptions", self._open_manage_dialog)

        if self._position_locked:
            menu.addAction("Unlock Widget Position", self._toggle_position_lock)
        else:
            menu.addAction("Lock Widget Position", self._toggle_position_lock)

        opacity_menu = menu.addMenu("Opacity Settings")
        current_opacity = self._current_background_opacity()
        for label, value in OPACITY_PRESETS:
            action = opacity_menu.addAction(
                label,
                lambda _checked=False, v=value: self._set_opacity(v),
            )
            if abs(current_opacity - value) < 0.01:
                action.setCheckable(True)
                action.setChecked(True)

        menu.addSeparator()
        menu.addAction("Exit Program", self._quit_application)
        menu.exec(self.mapToGlobal(pos))

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return

        if event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.pos())
            event.accept()
            return

        if self._position_locked:
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            if self._in_resize_zone(event.pos()):
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_start_size = self.size()
                event.accept()
                return
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return

        if self._position_locked:
            event.accept()
            return

        if (
            self._resize_origin is not None
            and self._resize_start_size is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            delta = event.globalPosition().toPoint() - self._resize_origin
            new_w = max(MIN_WIDGET_WIDTH, min(MAX_WIDGET_WIDTH, self._resize_start_size.width() + delta.x()))
            new_h = max(MIN_WIDGET_HEIGHT, min(MAX_WIDGET_HEIGHT, self._resize_start_size.height() + delta.y()))
            self.resize(new_w, new_h)
            event.accept()
            return

        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return

        if self._in_resize_zone(event.pos()):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if self._resize_origin is not None:
            self.save_widget_state()
        elif self._drag_pos is not None and not self._position_locked:
            self.save_widget_state()

        self._drag_pos = None
        self._resize_origin = None
        self._resize_start_size = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

        if event:
            event.accept()


def main() -> int:
    db.init_db()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Sub Asset Widget")
    app.setFont(QFont("Segoe UI", 10))

    widget = DesktopWidget()
    app.aboutToQuit.connect(widget.save_widget_state)
    widget.show_and_focus()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
