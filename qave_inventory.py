"""
Qave Inventory — a small desktop inventory tracker.

Data lives either on this computer or in a shared Google Sheet
(set it up under ⚙ Settings; see README.md).

Run from source:
    pip install -r requirements.txt
    python qave_inventory.py

Build a double-clickable app:
    ./build_mac.sh
"""

import sys
from collections import Counter
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QFrame, QDialog, QFormLayout,
    QDialogButtonBox, QFileDialog, QSpinBox, QShortcut, QComboBox
)
from PyQt5.QtCore import Qt, QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPalette, QKeySequence

import inventory_store as store

APP_TITLE  = "Qave Inventory"
REFRESH_MS = 60_000          # pull changes from the sheet every minute

INK, MUTED = "#1A1A18", "#999996"
GREEN, AMBER, RED = "#3B6D11", "#854F0B", "#A32D2D"
ACCENT = "#1D9E75"
UNIT_COLORS = {"working": GREEN, "loaned": "#2F5E9E", "broken": RED,
               "in repairs": AMBER, "dormant": MUTED}

STYLESHEET = """
    QMainWindow, QWidget#central, QDialog { background: #F5F5F2; }
    QLabel { color: #1A1A18; background: transparent; }
    QLineEdit, QSpinBox {
        background: #FFFFFF; color: #1A1A18;
        border: 1.5px solid #CCCCC8; border-radius: 6px;
        padding: 7px 10px; font-size: 14px;
        selection-background-color: #1D9E75;
    }
    QLineEdit:focus, QSpinBox:focus { border: 1.5px solid #1D9E75; }
    QComboBox {
        background: #FFFFFF; border: 1px solid #CCCCC8; border-radius: 6px;
        padding: 3px 10px; font-size: 13px;
    }
    QTableWidget {
        background: #FFFFFF; color: #1A1A18;
        border: 1px solid #E0DDD8; border-radius: 8px;
        gridline-color: #F0EEE9; font-size: 13px;
    }
    QTableWidget::item { padding: 0 12px; border: none; }
    QTableWidget::item:selected { padding: 0 12px; border: none; background: #E1F5EE; color: #1A1A18; }
    QHeaderView::section {
        background: #EEECEA; color: #666663; font-size: 11px;
        padding: 6px 12px; border: none; border-bottom: 1px solid #E0DDD8;
    }
    QPushButton {
        background: #FFFFFF; color: #1A1A18;
        border: 1px solid #CCCCC8; border-radius: 6px;
        padding: 7px 16px; font-size: 13px;
    }
    QPushButton:hover    { background: #EEECEA; }
    QPushButton:pressed  { background: #E0DDD8; }
    QPushButton:disabled { color: #AAAAA6; }
    QPushButton#addBtn {
        background: #1D9E75; color: #FFFFFF; border: 2px solid #1D9E75;
        font-weight: bold; font-size: 14px; padding: 8px 20px;
    }
    QPushButton#addBtn:hover   { background: #17896A; border-color: #17896A; }
    QPushButton#addBtn:pressed { background: #0F6E56; border-color: #0F6E56; }
    QPushButton#addBtn:disabled { background: #8CCDB8; border-color: #8CCDB8; }
    QPushButton#removeBtn { background: #FCEBEB; color: #A32D2D; border: 1px solid #F7C1C1; }
    QPushButton#removeBtn:hover { background: #F7C1C1; }
    QScrollBar:vertical { background: #F5F5F2; width: 8px; border-radius: 4px; }
    QScrollBar::handle:vertical { background: #CCCCC8; border-radius: 4px; min-height: 30px; }
"""


def _small_label(text):
    lbl = QLabel(text)
    lbl.setStyleSheet("color:#666663; font-size:11px;")
    return lbl

def _ago(ts):
    try:
        t = datetime.strptime(ts, store.TIME_FMT)
    except (TypeError, ValueError):
        return ts or ""
    mins = int((datetime.now() - t).total_seconds() // 60)
    if mins < 1:        return "just now"
    if mins < 60:       return f"{mins}m ago"
    if mins < 60 * 24:  return f"{mins // 60}h ago"
    if mins < 60 * 24 * 7: return f"{mins // (60 * 24)}d ago"
    return t.strftime("%b %d")


def _unit_summary(units):
    """e.g. "2 working · 1 in repairs" (known statuses first)."""
    counts = Counter(u["status"] for u in units)
    order = store.STATUSES + sorted(set(counts) - set(store.STATUSES))
    return " · ".join(f"{counts[s]} {s}" for s in order if counts[s])


# ── background work ───────────────────────────────────────────────────────────
# Google Sheets calls take a moment, so they run off the UI thread. The pool has
# a single thread, which keeps operations in the order they were requested.

class _TaskSignals(QObject):
    done   = pyqtSignal(object)
    failed = pyqtSignal(object)

class _Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = _TaskSignals()

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


# ── settings ──────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._new_creds = None

        self.url = QLineEdit(cfg["sheet_url"])
        self.url.setPlaceholderText("https://docs.google.com/spreadsheets/d/…")
        self.creds_lbl = QLabel()
        self.creds_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.creds_lbl.setWordWrap(True)
        pick = QPushButton("Choose key file…")
        pick.clicked.connect(self._pick)
        creds_row = QHBoxLayout()
        creds_row.addWidget(self.creds_lbl, 1)
        creds_row.addWidget(pick)
        self._show_creds(store.CREDS_FILE)

        self.user = QLineEdit(cfg["user_name"])
        self.low = QSpinBox()
        self.low.setRange(0, 9999)
        self.low.setValue(int(cfg["low_stock"]))

        hint = QLabel(
            "Leave the sheet link blank to keep data on this computer only.\n"
            "To use Google Sheets, share the sheet with the service-account "
            "email shown above as an Editor. See README.md for setup.")
        hint.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        hint.setWordWrap(True)

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Google Sheet link", self.url)
        form.addRow("Service account", creds_row)
        form.addRow("", hint)
        form.addRow("Your name", self.user)
        form.addRow("Low-stock alert at", self.low)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(buttons)

    def _show_creds(self, path):
        email = store.service_account_email(path)
        self.creds_lbl.setText(f"✓ {email}" if email else "No key file yet")

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "Service account key", "", "JSON key (*.json)")
        if not path:
            return
        if not store.service_account_email(path):
            QMessageBox.warning(self, "Not a key file",
                                "That file doesn't look like a Google service-account key.")
            return
        self._new_creds = path
        self._show_creds(path)

    def result_config(self, cfg):
        return dict(cfg, sheet_url=self.url.text().strip(),
                    user_name=self.user.text().strip() or store.DEFAULTS["user_name"],
                    low_stock=self.low.value())


# ── item types ────────────────────────────────────────────────────────────────

NEW_TYPE = "+ New type…"

def _form_dialog(dlg, rows, ok_text="OK"):
    """Lay out a simple dialog: form rows, then OK / Cancel."""
    form = QFormLayout()
    form.setSpacing(10)
    for label, widget in rows:
        form.addRow(label, widget)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText(ok_text)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    root = QVBoxLayout(dlg)
    root.addLayout(form)
    root.addWidget(buttons)

def _hint(text):
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{MUTED}; font-size:11px;")
    lbl.setWordWrap(True)
    return lbl


class NewTypeDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("New type")
        self.setMinimumWidth(480)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Power supply")
        self.tracking = QComboBox()
        for key, label in store.TRACKING.items():
            self.tracking.addItem(label, key)
        _form_dialog(self, [
            ("Type name", self.name),
            ("Tracking", self.tracking),
            ("", _hint("Serial number per unit: each unit has its own serial number and status, "
                       "and is loaned out when checked out (e.g. test benches).\n"
                       "Batches: groups of identical parts, each with a nickname, serial number "
                       "and quantity (e.g. discs).\n"
                       "Count only: just a quantity (e.g. cables).")),
        ], "Add type")

    def accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Missing name", "Please enter a type name.")
            return
        super().accept()

    def values(self):
        return self.name.text().strip(), self.tracking.currentData()


class TypeCombo(QComboBox):
    """Item-type dropdown that ends with “+ New type…”, which creates a type on the spot."""

    def __init__(self, win, current=""):
        super().__init__()
        self.win = win
        self.populate(current)
        self.activated.connect(self._on_activated)

    def populate(self, current=None):
        current = self.type_name() if current is None else current
        self.blockSignals(True)
        self.clear()
        self.addItem("No type", "")
        for t in self.win.types:
            self.addItem(t["name"], t["name"])
        if current and self.findData(current) < 0:       # e.g. type deleted from the sheet
            self.addItem(current, current)
        self.addItem(NEW_TYPE, NEW_TYPE)
        self.setCurrentIndex(max(0, self.findData(current)))
        self._last = current
        self.blockSignals(False)
        self.currentIndexChanged.emit(self.currentIndex())

    def type_name(self):
        data = self.currentData()
        return self._last if data == NEW_TYPE else (data or "")

    def _on_activated(self, index):
        if self.itemData(index) == NEW_TYPE:
            self.populate(self.win._create_type(self) or self._last)
        else:
            self._last = self.itemData(index) or ""


class ItemDialog(QDialog):
    """Edit an item's name and type."""

    def __init__(self, win, it):
        super().__init__(win)
        self.setWindowTitle(f'Edit — {it["name"]}')
        self.setMinimumWidth(420)
        self.name = QLineEdit(it["name"])
        self.type = TypeCombo(win, it.get("type", ""))
        _form_dialog(self, [("Name", self.name), ("Type", self.type)], "Save")

    def accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Missing name", "Please enter a name.")
            return
        super().accept()

    def values(self):
        return self.name.text().strip(), self.type.type_name()


# ── serial numbers & batches ──────────────────────────────────────────────────

class UnitDialog(QDialog):
    """Add or edit one serial-numbered unit, or one batch."""

    def __init__(self, parent, title, mode, unit=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        self.mode = mode
        unit = unit or {}

        self.serial = QLineEdit(unit.get("serial", ""))
        self.notes = QLineEdit(unit.get("notes", ""))
        self.notes.setPlaceholderText("Optional")
        if mode == "batch":
            self.nickname = QLineEdit(unit.get("nickname", ""))
            self.nickname.setPlaceholderText("e.g. IM3.3")
            self.serial.setPlaceholderText("e.g. 1000233-01 A")
            self.qty = QSpinBox()
            self.qty.setRange(0, 999999)
            self.qty.setValue(unit.get("qty", 1))
            rows = [("Nickname", self.nickname), ("Serial number", self.serial),
                    ("Quantity", self.qty), ("Notes", self.notes)]
        else:
            self.serial.setPlaceholderText("e.g. HT-12005")
            self.status = QComboBox()
            self.status.addItems(store.STATUSES)
            current = unit.get("status", "working")
            if current not in store.STATUSES:
                self.status.addItem(current)
            self.status.setCurrentText(current)
            rows = [("Serial number", self.serial), ("Status", self.status), ("Notes", self.notes)]
        _form_dialog(self, rows)

    def accept(self):
        if not self.serial.text().strip():
            QMessageBox.warning(self, "Missing serial", "Please enter a serial number.")
            return
        super().accept()

    def values(self):
        v = dict(serial=self.serial.text().strip(), notes=self.notes.text().strip())
        if self.mode == "batch":
            v.update(nickname=self.nickname.text().strip(), qty=self.qty.value())
        else:
            v.update(status=self.status.currentText())
        return v


class UnitsDialog(QDialog):
    """Lists an item's serial-numbered units (status changes save immediately) or batches."""

    def __init__(self, win, item_id):
        super().__init__(win)
        self.win, self.item_id = win, item_id
        self._shown = self._mode = None
        self.setMinimumSize(800, 440)

        self.title = QLabel()
        self.title.setStyleSheet("font-size:16px; font-weight:bold;")
        self.summary = QLabel()
        self.summary.setStyleSheet(f"color:{MUTED}; font-size:12px;")

        self.table = QTableWidget(0, 0)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.doubleClicked.connect(self._edit)

        self.add_btn = QPushButton()
        self.add_btn.setObjectName("addBtn")
        edit = QPushButton("Edit…")
        remove = QPushButton("Remove")
        remove.setObjectName("removeBtn")
        close = QPushButton("Close")
        self.add_btn.clicked.connect(self._add)
        edit.clicked.connect(self._edit)
        remove.clicked.connect(self._remove)
        close.clicked.connect(self.accept)
        btns = QHBoxLayout()
        for b in (self.add_btn, edit, remove):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(close)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)
        root.addWidget(self.title)
        root.addWidget(self.summary)
        root.addWidget(self.table)
        root.addLayout(btns)
        self.refresh()

    def _item(self):
        return next((i for i in self.win.items if i["id"] == self.item_id), None)

    def _selected_unit(self):
        it, row = self._item(), self.table.currentRow()
        cell = self.table.item(row, 0) if row >= 0 else None
        u = next((u for u in it["units"] if cell and u["id"] == cell.data(Qt.UserRole)), None) if it else None
        if u is None:
            QMessageBox.information(self, "No selection", "Select a row first.")
        return u

    def _setup_columns(self, mode):
        self._mode = mode
        if mode == "batch":
            headers, widths, stretch = ["Nickname", "Serial number", "Qty", "Notes", "Last change"], \
                                       {0: 120, 1: 170, 2: 70, 4: 175}, 3
            self.add_btn.setText("+ Add batch")
        else:
            headers, widths, stretch = ["Serial number", "Status", "Notes", "Last change"], \
                                       {0: 150, 1: 170, 3: 175}, 2
            self.add_btn.setText("+ Add unit")
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(stretch, QHeaderView.Stretch)
        for col, width in widths.items():
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)

    def refresh(self):
        it = self._item()
        if it is None:
            self.reject()
            return
        mode = self.win._mode(it)
        snapshot = (it["name"], mode, repr(it["units"]))
        if snapshot == self._shown:
            return              # unchanged: don't rebuild (keeps open dropdowns alive)
        self._shown = snapshot
        row = self.table.currentRow()
        keep = self.table.item(row, 0).data(Qt.UserRole) if row >= 0 and self.table.item(row, 0) else None
        if mode != self._mode:
            self._setup_columns(mode)

        batch = mode == "batch"
        self.setWindowTitle(f'{"Batches" if batch else "Serial numbers"} — {it["name"]}')
        self.title.setText(it["name"] + (f'  ·  {it["type"]}' if it.get("type") else ""))
        if batch:
            summary = (f'{it["qty"]} total in {len(it["units"])} batch(es)' if it["units"] else
                       "No batches yet. Click “+ Add batch” to add one.")
        else:
            summary = (_unit_summary(it["units"]) if it["units"] else
                       "No serial numbers yet. Click “+ Add unit” to add one.")
        self.summary.setText(summary)

        key = (lambda u: (u["nickname"].lower(), u["serial"].lower())) if batch else \
              (lambda u: u["serial"].lower())
        units = sorted(it["units"], key=key)
        self.table.setRowCount(len(units))
        for r, u in enumerate(units):
            cells = []
            if batch:
                cells.append(QTableWidgetItem(u["nickname"]))
                cells.append(QTableWidgetItem(u["serial"]))
                qty = QTableWidgetItem(str(u["qty"]))
                qty.setTextAlignment(Qt.AlignCenter)
                qty.setForeground(QColor(RED if u["qty"] == 0 else INK))
                cells.append(qty)
                notes_text = u.get("notes", "")
            else:
                cells.append(QTableWidgetItem(u["serial"]))
                cells.append(None)            # status dropdown goes here
                notes_text = " · ".join(filter(None, [
                    f'On loan → {u["loaned_to"]}' if u["status"] == "loaned" and u.get("loaned_to") else "",
                    u.get("notes", "")]))
            notes = QTableWidgetItem(notes_text)
            notes.setToolTip(notes_text)
            if not batch and u["status"] == "loaned":
                notes.setForeground(QColor(UNIT_COLORS["loaned"]))
            cells.append(notes)
            when, who = _ago(u.get("updated_at", "")), u.get("updated_by", "")
            change = QTableWidgetItem(f"{when} · {who}" if when and who else when or who)
            change.setForeground(QColor(MUTED))
            change.setFont(QFont("Helvetica", 11))
            cells.append(change)

            cells[0].setData(Qt.UserRole, u["id"])
            cells[0].setFont(QFont("Helvetica", 13, QFont.Bold))
            for c, cell in enumerate(cells):
                if cell is not None:
                    self.table.setItem(r, c, cell)
            if not batch:
                self.table.setCellWidget(r, 1, self._status_combo(u))
            if u["id"] == keep:
                self.table.selectRow(r)

    def _status_combo(self, u):
        combo = QComboBox()
        combo.addItems(store.STATUSES)
        if u["status"] not in store.STATUSES:
            combo.addItem(u["status"])
        combo.setCurrentText(u["status"])
        combo.setStyleSheet(f"QComboBox {{ color:{UNIT_COLORS.get(u['status'], INK)}; "
                            "font-weight:bold; margin: 5px 8px; }")
        combo.currentTextChanged.connect(
            lambda status, uid=u["id"], sn=u["serial"]: self._set_status(uid, sn, status))
        return combo

    def _set_status(self, unit_id, serial, status):
        self.win._do(lambda: self.win.backend.update_unit(self.item_id, unit_id, status=status),
                     f"{serial} → {status}.")

    def _add(self):
        it = self._item()
        if not it: return
        batch = self._mode == "batch"
        dlg = UnitDialog(self, f'{"Add batch" if batch else "Add unit"} — {it["name"]}', self._mode)
        if dlg.exec_() != QDialog.Accepted: return
        v = dlg.values()
        self.win._do(lambda: self.win.backend.add_unit(self.item_id, **v),
                     f'Added {v["serial"]} to {it["name"]}.')

    def _edit(self):
        u = self._selected_unit()
        if not u: return
        dlg = UnitDialog(self, f'Edit — {u["serial"]}', self._mode, u)
        if dlg.exec_() != QDialog.Accepted: return
        unit_id, v = u["id"], dlg.values()
        self.win._do(lambda: self.win.backend.update_unit(self.item_id, unit_id, **v),
                     f'Updated {v["serial"]}.')

    def _remove(self):
        u = self._selected_unit()
        if not u: return
        what = f'batch {u["nickname"]} · {u["serial"]}' if self._mode == "batch" else f'unit {u["serial"]}'
        reply = QMessageBox.question(self, "Remove", f"Remove {what}?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes: return
        unit_id = u["id"]
        self.win._do(lambda: self.win.backend.remove_unit(self.item_id, unit_id), f"Removed {what}.")


# ── check out / return ────────────────────────────────────────────────────────

class MoveDialog(QDialog):
    """Check out, or restock / return, adapting to how the item is tracked.

    serial: pick a serial number → note where it's going → marked "loaned"
            (returning: pick a loaned unit → choose its condition)
    batch:  pick a batch → quantity → optional note
    count:  quantity → optional note
    """

    def __init__(self, parent, it, mode, out):
        super().__init__(parent)
        self.mode, self.out = mode, out
        self.unit = self.qty = self.status = None
        rows = []

        if mode == "serial":
            self.setWindowTitle(f'{"Check out" if out else "Return"} — {it["name"]}')
            self.unit = QComboBox()
            for u in sorted(self.choices(it, mode, out), key=lambda u: u["serial"].lower()):
                label = (f'{u["serial"]}  ({u["status"]})' if out else
                         f'{u["serial"]}  (at {u["loaned_to"] or "unknown"})')
                self.unit.addItem(label, u["id"])
            rows.append(("Serial number", self.unit))
            if not out:
                self.status = QComboBox()
                self.status.addItems([s for s in store.STATUSES if s != "loaned"])
                rows.append(("Condition", self.status))
        else:
            self.setWindowTitle(f'{"Check out" if out else "Restock"} — {it["name"]}')
            if mode == "batch":
                self.unit = QComboBox()
                for u in sorted(self.choices(it, mode, out),
                                key=lambda u: (u["nickname"].lower(), u["serial"].lower())):
                    self.unit.addItem(f'{u["nickname"]} · {u["serial"]}  ({u["qty"]} left)', u["id"])
                    self.unit.setItemData(self.unit.count() - 1, u["qty"], Qt.UserRole + 1)
                rows.append(("Batch", self.unit))
            self.qty = QSpinBox()
            self.qty.setRange(1, 999999)
            rows.append(("Quantity", self.qty))
            if out:
                if self.unit:
                    self.unit.currentIndexChanged.connect(self._limit_qty)
                    self._limit_qty()
                else:
                    self.qty.setMaximum(it["qty"])

        self.note = QLineEdit()
        if out and mode == "serial":
            self.note.setPlaceholderText("Required, e.g. Lab 3 — Priya, or Customer ACME")
            rows.append(("Where is it going?", self.note))
        else:
            self.note.setPlaceholderText("Optional" + (", e.g. where it's going" if out else ""))
            rows.append(("Where is it going?" if out else "Note", self.note))

        ok = {("serial", True): "Loan out", ("serial", False): "Return"}.get(
            (mode, out), "Check out" if out else "Restock")
        _form_dialog(self, rows, ok)
        self.setMinimumWidth(460)

    @staticmethod
    def choices(it, mode, out):
        if mode == "serial":
            return [u for u in it["units"]
                    if (u["status"] in store.LOANABLE if out else u["status"] == "loaned")]
        if mode == "batch":
            return [u for u in it["units"] if u["qty"] > 0] if out else list(it["units"])
        return []

    def _limit_qty(self):
        self.qty.setMaximum(max(1, self.unit.currentData(Qt.UserRole + 1) or 1))

    def accept(self):
        if self.mode == "serial" and self.out and not self.note.text().strip():
            QMessageBox.warning(self, "Where is it going?", "Please add a note of where it's going.")
            return
        super().accept()

    def values(self):
        return dict(unit_id=self.unit.currentData() if self.unit else None,
                    qty=self.qty.value() if self.qty else 1,
                    status=self.status.currentText() if self.status else None,
                    note=self.note.text().strip())


# ── main window ───────────────────────────────────────────────────────────────

class InventoryApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(980, 640)
        self.setMinimumSize(760, 480)
        self.setStyleSheet(STYLESHEET)

        self.cfg = store.load_config()
        cached = store.load_cache()         # shown right away, refreshed once connected
        self.items, self.types = cached["items"], cached["types"]
        self.backend = None
        self.last_sync = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._tasks = set()
        self._units_dlg = None

        self._build_ui()
        self._refresh_table()

        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self._sync(quiet=True))
        self.timer.start(REFRESH_MS)
        QTimer.singleShot(0, self._connect)

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(12)

        # ── add-item row + toolbar ────────────────────────────────────────────
        top = QGridLayout()
        top.setHorizontalSpacing(10)
        top.setVerticalSpacing(4)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. HT Test bench, CC Discs, Ethernet cables…")
        self.name_input.setMinimumWidth(240)
        self.name_input.returnPressed.connect(self._add_item)

        self.type_input = TypeCombo(self)
        self.type_input.setMinimumWidth(150)
        self.type_input.currentIndexChanged.connect(self._on_add_type_changed)

        self.qty_input = QSpinBox()
        self.qty_input.setRange(0, 99999)
        self.qty_input.setValue(1)
        self.qty_input.setFixedWidth(90)
        self.qty_input.lineEdit().returnPressed.connect(self._add_item)

        self.add_btn = QPushButton("+ Add item")
        self.add_btn.setObjectName("addBtn")
        self.add_btn.setFixedHeight(38)
        self.add_btn.clicked.connect(self._add_item)

        self.sync_btn = QPushButton("↻ Sync")
        self.sync_btn.setToolTip("Pull the latest data from the Google Sheet")
        self.sync_btn.clicked.connect(lambda: self._sync(quiet=False))
        settings_btn = QPushButton("⚙ Settings")
        settings_btn.clicked.connect(self._open_settings)

        top.addWidget(_small_label("Item name"), 0, 0)
        top.addWidget(_small_label("Type"), 0, 1)
        top.addWidget(_small_label("Quantity"), 0, 2)
        top.addWidget(self.name_input, 1, 0)
        top.addWidget(self.type_input, 1, 1)
        top.addWidget(self.qty_input, 1, 2)
        top.addWidget(self.add_btn, 1, 3)
        top.setColumnMinimumWidth(4, 20)
        top.addWidget(self.sync_btn, 1, 5)
        top.addWidget(settings_btn, 1, 6)
        top.setColumnStretch(0, 1)
        root.addLayout(top)

        # ── stat cards ────────────────────────────────────────────────────────
        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        self.stat_labels = {}
        for key, title, color in [("total", "Total items", INK), ("units", "Units in stock", INK),
                                  ("low", "Low stock / issues", AMBER), ("out", "Out of stock", RED)]:
            card = QFrame()
            card.setStyleSheet("QFrame { background: #EEECEA; border-radius: 8px; }")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 10, 14, 10)
            cl.setSpacing(2)
            tl = QLabel(title)
            tl.setStyleSheet("color:#888885; font-size:10px;")
            vl = QLabel("0")
            vl.setStyleSheet(f"color:{color}; font-size:22px; font-weight:bold;")
            cl.addWidget(tl)
            cl.addWidget(vl)
            self.stat_labels[key] = vl
            stat_row.addWidget(card)
        root.addLayout(stat_row)

        # ── search + table ────────────────────────────────────────────────────
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search items, types, serial numbers, nicknames…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_table)
        root.addWidget(self.search)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Item", "Type", "Qty", "Status", "Last change"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 120), (2, 60), (4, 175)):
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setMinimumSectionSize(60)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setFocusPolicy(Qt.ClickFocus)
        self.table.doubleClicked.connect(self._checkout)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        for key in (Qt.Key_Delete, Qt.Key_Backspace):
            QShortcut(QKeySequence(key), self.table, self._remove, context=Qt.WidgetShortcut)
        root.addWidget(self.table)

        # ── action bar ────────────────────────────────────────────────────────
        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        self.btn_checkout = QPushButton("− Check out")
        self.btn_restock  = QPushButton("+ Restock")
        self.btn_edit     = QPushButton("Edit…")
        self.btn_units    = QPushButton("Serial numbers…")
        self.btn_remove   = QPushButton("Remove")
        self.btn_remove.setObjectName("removeBtn")
        self.btn_checkout.setToolTip("Tip: double-click a row to check it out")
        self.btn_edit.setToolTip("Change the item's name or type")
        self.btn_checkout.clicked.connect(self._checkout)
        self.btn_restock.clicked.connect(self._restock)
        self.btn_edit.clicked.connect(self._edit)
        self.btn_units.clicked.connect(self._open_units)
        self.btn_remove.clicked.connect(self._remove)
        for b in (self.btn_checkout, self.btn_restock, self.btn_edit, self.btn_units):
            act_row.addWidget(b)
        act_row.addStretch()
        act_row.addWidget(self.btn_remove)
        root.addLayout(act_row)

        # ── status bar ────────────────────────────────────────────────────────
        status_row = QHBoxLayout()
        self.status_lbl = QLabel("Ready.")
        self.status_lbl.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        self.conn_lbl = QLabel()
        self.conn_lbl.setStyleSheet("font-size:11px;")
        status_row.addWidget(self.status_lbl, 1)
        status_row.addWidget(self.conn_lbl)
        root.addLayout(status_row)

        self._edit_buttons = [self.add_btn, self.btn_checkout, self.btn_restock,
                              self.btn_edit, self.btn_remove, self.sync_btn]
        self._update_buttons()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _mode(self, it):
        return store.tracking_of(it, self.types)

    def _level(self, it):
        """0 = out / none working, 1 = low / some broken, 2 = ok (also the sort order)."""
        if self._mode(it) == "serial":
            statuses = [u["status"] for u in it["units"]]
            if "working" not in statuses: return 0
            return 1 if any(s in ("broken", "in repairs") for s in statuses) else 2
        if it["qty"] == 0: return 0
        if it["qty"] <= self.cfg["low_stock"]: return 1
        return 2

    def _status(self, it):
        mode, level = self._mode(it), self._level(it)
        color = [RED, AMBER, GREEN][level]
        if mode == "serial":
            return (_unit_summary(it["units"]) if it["units"] else "No serial numbers yet"), color
        text = ["Out of stock", f"Low  (≤{self.cfg['low_stock']})", "In stock"][level]
        if mode == "batch":
            n = len(it["units"])
            text = f'{text} · {n} batch{"es" if n != 1 else ""}' if n else "No batches yet"
        return text, color

    def _selected_id(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.UserRole) if row >= 0 and self.table.item(row, 0) else None

    def _selected_item(self, quiet=False):
        item_id = self._selected_id()
        it = next((i for i in self.items if i["id"] == item_id), None)
        if it is None and not quiet:
            QMessageBox.information(self, "No selection", "Select an item from the list first.")
        return it

    def _update_buttons(self):
        it = self._selected_item(quiet=True)
        mode = self._mode(it) if it else "count"
        self.btn_restock.setText("↩ Return" if mode == "serial" else "+ Restock")
        self.btn_units.setText("Batches…" if mode == "batch" else "Serial numbers…")
        self.btn_units.setEnabled(mode != "count")
        self.btn_units.setToolTip("" if mode != "count" else
                                  "This item is counted. Give it a type that tracks serial "
                                  "numbers or batches (Edit…) to use this.")

    def _on_add_type_changed(self, *_):
        mode = store.tracking_of({"type": self.type_input.type_name()}, self.types)
        self.qty_input.setEnabled(mode == "count")
        self.qty_input.setToolTip("" if mode == "count" else
                                  "Quantity comes from the serial numbers / batches you add next.")

    def _set_status(self, msg):
        self.status_lbl.setText(msg)

    def _set_conn(self, state, detail=""):
        text, color = {
            "local":      ("● Saved on this computer", MUTED),
            "connecting": ("● Connecting to Google Sheets…", AMBER),
            "sheets":     (f"● Google Sheets · synced {self.last_sync:%H:%M}" if self.last_sync
                           else "● Google Sheets", GREEN),
            "offline":    ("● Offline — showing last synced data", RED),
        }[state]
        self.conn_lbl.setText(text)
        self.conn_lbl.setStyleSheet(f"color:{color}; font-size:11px;")
        self.conn_lbl.setToolTip(detail)

    # ── background runner ─────────────────────────────────────────────────────

    def _run(self, fn, on_done, on_error=None, busy_msg=None):
        """Run fn() on the worker thread. With busy_msg, lock the buttons meanwhile."""
        busy = busy_msg is not None
        if busy:
            self._set_status(busy_msg)
            for b in self._edit_buttons:
                b.setEnabled(False)
        task = _Task(fn)

        def finish():
            self._tasks.discard(task)
            if busy:
                for b in self._edit_buttons:
                    b.setEnabled(True)

        def done(result):
            finish()
            on_done(result)

        def failed(exc):
            finish()
            (on_error or self._on_error)(exc)

        task.signals.done.connect(done)
        task.signals.failed.connect(failed)
        self._tasks.add(task)
        self.pool.start(task)

    def _on_error(self, exc, note="Your change was not saved."):
        msg = store.describe_error(exc)
        if self._units_dlg:              # undo e.g. a status dropdown that didn't save
            self._units_dlg._shown = None
            self._units_dlg.refresh()
        if isinstance(exc, store.InventoryError):
            self._set_status(msg)
            QMessageBox.warning(self, "Can't do that", msg)
            return
        if self.backend and self.backend.kind == "sheets":
            self._set_conn("offline", msg)
        self._set_status(msg.splitlines()[0])
        QMessageBox.warning(self, "Problem", f"{msg}\n\n{note}".strip())

    # ── connecting & syncing ──────────────────────────────────────────────────

    def _connect(self):
        self.backend = None
        cfg = dict(self.cfg)
        if not cfg["sheet_url"]:
            self.backend = store.LocalBackend(cfg["user_name"])
            self._set_conn("local")
            self._set_items(self.backend.load())
            return
        self._set_conn("connecting")
        self._run(lambda: store.SheetsBackend(cfg["sheet_url"], store.CREDS_FILE, cfg["user_name"]),
                  self._on_connected, self._on_connect_failed,
                  busy_msg="Connecting to Google Sheets…")

    def _on_connected(self, backend):
        self.backend = backend
        self._run(backend.load, self._on_first_fetch, busy_msg="Loading…")

    def _on_connect_failed(self, exc):
        msg = store.describe_error(exc)
        self._set_conn("offline", msg)
        self._set_status("Not connected to Google Sheets. Click ↻ Sync to retry.")
        QMessageBox.warning(self, "Couldn't connect to Google Sheets",
                            msg + "\n\nShowing the last synced data. Editing is paused until "
                                  "the connection works.")

    def _on_first_fetch(self, data):
        local, local_types = [i for i in self.items if i.get("name")], list(self.types)
        if not data["items"] and local:
            reply = QMessageBox.question(
                self, "Empty sheet",
                f"The Google Sheet has no items yet.\n\nUpload the {len(local)} item(s) "
                "currently on this computer to the sheet?",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._run(lambda: self.backend.upload(local, local_types),
                          lambda d: self._set_items(d, f"Uploaded {len(local)} item(s)."),
                          busy_msg="Uploading…")
                return
        self._set_items(data, "Loaded from Google Sheets.")

    def _sync(self, quiet):
        if self.backend is None:
            if not quiet and self.cfg["sheet_url"]:
                self._connect()
            return
        if self.backend.kind == "local":
            self._set_items(self.backend.load(), None if quiet else "Reloaded.")
            return
        if quiet and self._tasks:
            return      # something is already running; skip this tick
        self._run(self.backend.load,
                  lambda d: self._set_items(d, None if quiet else "Synced."),
                  on_error=(lambda exc: self._set_conn("offline", store.describe_error(exc))) if quiet
                           else (lambda exc: self._on_error(exc, note="")),
                  busy_msg=None if quiet else "Syncing…")

    def _set_items(self, data, msg=None):
        self.items, self.types = data["items"], data["types"]
        store.save_cache(data)
        if self.backend and self.backend.kind == "sheets":
            self.last_sync = datetime.now()
            self._set_conn("sheets")
        self.type_input.populate()
        self._refresh_table()
        self._update_buttons()
        if self._units_dlg:
            self._units_dlg.refresh()
        if msg:
            self._set_status(msg(data) if callable(msg) else msg)

    def _open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        if dlg.exec_() != QDialog.Accepted:
            return
        if dlg._new_creds:
            store.install_credentials(dlg._new_creds)
        self.cfg = dlg.result_config(self.cfg)
        store.save_config(self.cfg)
        self._connect()

    # ── refresh ───────────────────────────────────────────────────────────────

    def _matches(self, it, query):
        return (query in it["name"].lower() or query in it.get("type", "").lower()
                or any(query in u["serial"].lower() or query in u["nickname"].lower()
                       for u in it["units"]))

    def _refresh_table(self):
        keep = self._selected_id()
        query = self.search.text().strip().lower()
        shown = sorted((i for i in self.items if self._matches(i, query)),
                       key=lambda x: (self._level(x), x["name"].lower()))

        self.table.setRowCount(len(shown))
        for r, it in enumerate(shown):
            lbl, color = self._status(it)
            mode = self._mode(it)

            name_item = QTableWidgetItem(it["name"])
            name_item.setData(Qt.UserRole, it["id"])
            name_item.setFont(QFont("Helvetica", 13))
            if it["units"]:
                if mode == "batch":
                    lines = [f'{u["nickname"]} · {u["serial"]} — {u["qty"]}' for u in it["units"]]
                else:
                    lines = [f'{u["serial"]} — {u["status"]}' +
                             (f' ({u["loaned_to"]})' if u["status"] == "loaned" and u.get("loaned_to") else "")
                             for u in it["units"]]
                name_item.setToolTip("\n".join(sorted(lines, key=str.lower)))

            type_item = QTableWidgetItem(it.get("type", ""))
            type_item.setFont(QFont("Helvetica", 12))
            type_item.setForeground(QColor("#666663"))

            qty_item = QTableWidgetItem(str(it["qty"]))
            qty_item.setTextAlignment(Qt.AlignCenter)
            qty_item.setFont(QFont("Helvetica", 13, QFont.Bold))
            qty_item.setForeground(QColor(color))

            status_item = QTableWidgetItem(lbl)
            status_item.setTextAlignment(Qt.AlignCenter)
            status_item.setFont(QFont("Helvetica", 12))
            status_item.setForeground(QColor(color))
            status_item.setToolTip(lbl)

            when = _ago(it.get("updated_at", ""))
            who = it.get("updated_by", "")
            change_item = QTableWidgetItem(f"{when} · {who}" if when and who else when or who)
            change_item.setFont(QFont("Helvetica", 11))
            change_item.setForeground(QColor(MUTED))
            change_item.setToolTip(it.get("updated_at", ""))

            for c, cell in enumerate((name_item, type_item, qty_item, status_item, change_item)):
                self.table.setItem(r, c, cell)
            if it["id"] == keep:
                self.table.selectRow(r)

        self.stat_labels["total"].setText(str(len(self.items)))
        self.stat_labels["units"].setText(str(sum(i["qty"] for i in self.items)))
        self.stat_labels["low"].setText(str(sum(1 for i in self.items if self._level(i) == 1)))
        self.stat_labels["out"].setText(str(sum(1 for i in self.items if self._level(i) == 0)))

    # ── actions ───────────────────────────────────────────────────────────────

    def _do(self, fn, msg, then=None):
        """Apply a change through the backend in the background."""
        if self.backend is None:
            QMessageBox.information(self, "Not connected",
                                    "Not connected to the Google Sheet, so changes can't be saved.\n\n"
                                    "Click ↻ Sync to retry, or check ⚙ Settings.")
            return False

        def done(data):
            self._set_items(data, msg)
            if then:
                then(data)
        self._run(fn, done, busy_msg="Saving…")
        return True

    def _create_type(self, combo):
        """Called by a TypeCombo's “+ New type…”. Returns the new type's name, or None."""
        dlg = NewTypeDialog(combo.window())
        if dlg.exec_() != QDialog.Accepted:
            return None
        name, tracking = dlg.values()
        existing = next((t for t in self.types if t["name"].lower() == name.lower()), None)
        if existing:
            return existing["name"]
        if not self._do(lambda: self.backend.add_type(name, tracking), f'Added type "{name}".'):
            return None
        self.types.append({"name": name, "tracking": tracking})   # show it now; confirmed on save
        return name

    def _open_units_for(self, item_id):
        self._units_dlg = UnitsDialog(self, item_id)
        self._units_dlg.exec_()
        self._units_dlg = None

    def _add_item(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Please enter an item name.")
            return
        type_ = self.type_input.type_name()
        mode = store.tracking_of({"type": type_}, self.types)
        qty = self.qty_input.value() if mode == "count" else 0
        if any(i["name"].lower() == name.lower() for i in self.items):
            QMessageBox.warning(self, "Duplicate", f'"{name}" already exists.')
            return

        def open_new(data):                 # serial / batch items: add units right away
            it = next((i for i in data["items"] if i["name"] == name), None)
            if it and mode != "count":
                self._open_units_for(it["id"])

        msg = f'Added "{name}" ×{qty}.' if mode == "count" else f'Added "{name}".'
        if self._do(lambda: self.backend.add(name, qty, type_), msg, then=open_new):
            self.name_input.clear()
            self.qty_input.setValue(1)
            self.name_input.setFocus()

    def _open_units(self):
        it = self._selected_item()
        if it:
            self._open_units_for(it["id"])

    def _unit(self, it, unit_id):
        return next((u for u in it["units"] if u["id"] == unit_id), {})

    def _checkout(self):
        it = self._selected_item()
        if not it: return
        mode, item_id, name = self._mode(it), it["id"], it["name"]
        if mode != "count" and not MoveDialog.choices(it, mode, True):
            if not it["units"]:
                what = "batches" if mode == "batch" else "serial numbers"
                QMessageBox.information(self, "Nothing to check out",
                                        f'"{name}" has no {what} recorded yet. Add them first.')
                return self._open_units_for(item_id)
            QMessageBox.information(self, "Nothing available",
                                    f'No "{name}" available to check out.' +
                                    ("\n\nEvery unit is loaned, broken, or in repairs."
                                     if mode == "serial" else ""))
            return
        if mode == "count" and it["qty"] == 0:
            QMessageBox.information(self, "Out of stock", f'"{name}" has no stock left.')
            return
        dlg = MoveDialog(self, it, mode, out=True)
        if dlg.exec_() != QDialog.Accepted: return
        v = dlg.values()
        unit = self._unit(it, v["unit_id"])
        if mode == "serial":
            self._do(lambda: self.backend.loan_unit(item_id, v["unit_id"], v["note"]),
                     f'{unit.get("serial")} loaned → {v["note"]}.')
        elif mode == "batch":
            self._do(lambda: self.backend.adjust_batch(item_id, v["unit_id"], -v["qty"], v["note"]),
                     f'Checked out {v["qty"]}× {unit.get("nickname")} · {unit.get("serial")}.')
        else:
            self._do(lambda: self.backend.adjust(item_id, -v["qty"], v["note"]),
                     lambda d: f'Checked out {v["qty"]}× {name} — {self._qty_of(d, item_id)} left.')

    def _restock(self):
        it = self._selected_item()
        if not it: return
        mode, item_id, name = self._mode(it), it["id"], it["name"]
        if mode == "serial" and not MoveDialog.choices(it, mode, False):
            QMessageBox.information(self, "Nothing on loan",
                                    f'No "{name}" units are on loan.\n\n'
                                    "To add new units, use Serial numbers….")
            return
        if mode == "batch" and not it["units"]:
            QMessageBox.information(self, "No batches", f'"{name}" has no batches yet. Add one first.')
            return self._open_units_for(item_id)
        dlg = MoveDialog(self, it, mode, out=False)
        if dlg.exec_() != QDialog.Accepted: return
        v = dlg.values()
        unit = self._unit(it, v["unit_id"])
        if mode == "serial":
            self._do(lambda: self.backend.return_unit(item_id, v["unit_id"], v["status"], v["note"]),
                     f'{unit.get("serial")} returned ({v["status"]}).')
        elif mode == "batch":
            self._do(lambda: self.backend.adjust_batch(item_id, v["unit_id"], v["qty"], v["note"]),
                     f'Restocked {unit.get("nickname")} · {unit.get("serial")} +{v["qty"]}.')
        else:
            self._do(lambda: self.backend.adjust(item_id, v["qty"], v["note"]),
                     lambda d: f'Restocked "{name}" +{v["qty"]} → {self._qty_of(d, item_id)} total.')

    def _qty_of(self, data, item_id):
        return next((i["qty"] for i in data["items"] if i["id"] == item_id), 0)

    def _edit(self):
        it = self._selected_item()
        if not it: return
        dlg = ItemDialog(self, it)
        if dlg.exec_() != QDialog.Accepted: return
        name, type_ = dlg.values()
        old_mode = self._mode(it)
        new_mode = store.tracking_of(dict(it, type=type_), self.types)
        if new_mode != old_mode:
            if it["units"]:
                QMessageBox.warning(self, "Can't change type",
                                    f'"{it["name"]}" has serial numbers or batches recorded. Remove '
                                    "them first, or pick a type that is tracked the same way.")
                return
            if old_mode == "count" and it["qty"] > 0:
                what = "batches" if new_mode == "batch" else "serial numbers"
                reply = QMessageBox.question(
                    self, "Change type",
                    f'"{it["name"]}" currently has a quantity of {it["qty"]}.\n\n'
                    f"As a {type_}, its quantity will come from the {what} you add. Continue?",
                    QMessageBox.Yes | QMessageBox.No)
                if reply != QMessageBox.Yes: return
        item_id, open_units = it["id"], new_mode != old_mode and new_mode != "count"

        def then(data):
            new_id = store.item_key(name)          # ids follow the name
            self._select(new_id)
            if open_units:
                self._open_units_for(new_id)
        self._do(lambda: self.backend.edit_item(item_id, name, type_), f'Saved "{name}".', then=then)

    def _select(self, item_id):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == item_id:
                self.table.selectRow(r)
                return

    def _remove(self):
        it = self._selected_item()
        if not it: return
        reply = QMessageBox.question(self, "Remove", f'Remove "{it["name"]}" from inventory?',
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes: return
        item_id, name = it["id"], it["name"]
        self._do(lambda: self.backend.remove(item_id), f'Removed "{name}".')

    def closeEvent(self, event):
        self.pool.waitForDone(5000)     # let an in-flight save finish
        super().closeEvent(event)


def _light_palette():
    """Keep dialogs readable even when macOS is in dark mode."""
    pal = QPalette()
    for role, color in [(QPalette.Window, "#F5F5F2"), (QPalette.WindowText, INK),
                        (QPalette.Base, "#FFFFFF"), (QPalette.AlternateBase, "#F5F5F2"),
                        (QPalette.Text, INK), (QPalette.Button, "#FFFFFF"),
                        (QPalette.ButtonText, INK), (QPalette.ToolTipBase, "#FFFFFF"),
                        (QPalette.ToolTipText, INK), (QPalette.Highlight, ACCENT),
                        (QPalette.HighlightedText, "#FFFFFF"), (QPalette.PlaceholderText, MUTED)]:
        pal.setColor(role, QColor(color))
    return pal


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")   # consistent look on all platforms including macOS
    app.setPalette(_light_palette())
    window = InventoryApp()
    window.show()
    sys.exit(app.exec_())
