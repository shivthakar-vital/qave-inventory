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
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QInputDialog, QFrame, QDialog, QFormLayout,
    QDialogButtonBox, QFileDialog, QSpinBox, QShortcut
)
from PyQt5.QtCore import Qt, QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QPalette, QKeySequence

import inventory_store as store

APP_TITLE  = "Qave Inventory"
REFRESH_MS = 60_000          # pull changes from the sheet every minute

INK, MUTED = "#1A1A18", "#999996"
GREEN, AMBER, RED = "#3B6D11", "#854F0B", "#A32D2D"
ACCENT = "#1D9E75"

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


# ── main window ───────────────────────────────────────────────────────────────

class InventoryApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(900, 620)
        self.setMinimumSize(700, 480)
        self.setStyleSheet(STYLESHEET)

        self.cfg = store.load_config()
        self.items = store.load_cache()     # shown right away, refreshed once connected
        self.backend = None
        self.last_sync = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self._tasks = set()

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
        self.name_input.setPlaceholderText("e.g. Ethernet cables, Test bench, Box of gloves…")
        self.name_input.setMinimumWidth(260)
        self.name_input.returnPressed.connect(self._add_item)

        self.qty_input = QSpinBox()
        self.qty_input.setRange(1, 99999)
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
        top.addWidget(_small_label("Quantity"), 0, 1)
        top.addWidget(self.name_input, 1, 0)
        top.addWidget(self.qty_input, 1, 1)
        top.addWidget(self.add_btn, 1, 2)
        top.setColumnMinimumWidth(3, 20)
        top.addWidget(self.sync_btn, 1, 4)
        top.addWidget(settings_btn, 1, 5)
        top.setColumnStretch(0, 1)
        root.addLayout(top)

        # ── stat cards ────────────────────────────────────────────────────────
        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        self.stat_labels = {}
        for key, title, color in [("total", "Total items", INK), ("units", "Units in stock", INK),
                                  ("low", "Low stock", AMBER), ("out", "Out of stock", RED)]:
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
        self.search.setPlaceholderText("Search items…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_table)
        root.addWidget(self.search)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Item", "Qty", "Status", "Last change"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 80), (2, 130), (3, 170)):
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.ClickFocus)
        self.table.doubleClicked.connect(self._checkout)
        for key in (Qt.Key_Delete, Qt.Key_Backspace):
            QShortcut(QKeySequence(key), self.table, self._remove, context=Qt.WidgetShortcut)
        root.addWidget(self.table)

        # ── action bar ────────────────────────────────────────────────────────
        act_row = QHBoxLayout()
        act_row.setSpacing(8)
        self.btn_checkout = QPushButton("− Check out")
        self.btn_restock  = QPushButton("+ Restock")
        self.btn_rename   = QPushButton("Rename")
        self.btn_remove   = QPushButton("Remove")
        self.btn_remove.setObjectName("removeBtn")
        self.btn_checkout.setToolTip("Tip: double-click a row to check it out")
        self.btn_checkout.clicked.connect(self._checkout)
        self.btn_restock.clicked.connect(self._restock)
        self.btn_rename.clicked.connect(self._rename)
        self.btn_remove.clicked.connect(self._remove)
        for b in (self.btn_checkout, self.btn_restock, self.btn_rename):
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
                              self.btn_rename, self.btn_remove, self.sync_btn]

    # ── helpers ───────────────────────────────────────────────────────────────

    def _level(self, qty):
        """0 = out, 1 = low, 2 = ok (also the sort order)."""
        if qty == 0: return 0
        if qty <= self.cfg["low_stock"]: return 1
        return 2

    def _status(self, qty):
        return [("Out of stock", RED),
                (f"Low  (≤{self.cfg['low_stock']})", AMBER),
                ("In stock", GREEN)][self._level(qty)]

    def _selected_id(self):
        row = self.table.currentRow()
        return self.table.item(row, 0).data(Qt.UserRole) if row >= 0 and self.table.item(row, 0) else None

    def _selected_item(self):
        item_id = self._selected_id()
        it = next((i for i in self.items if i["id"] == item_id), None)
        if it is None:
            QMessageBox.information(self, "No selection", "Select an item from the list first.")
        return it

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
            self._set_items(self.backend.fetch())
            return
        self._set_conn("connecting")
        self._run(lambda: store.SheetsBackend(cfg["sheet_url"], store.CREDS_FILE, cfg["user_name"]),
                  self._on_connected, self._on_connect_failed,
                  busy_msg="Connecting to Google Sheets…")

    def _on_connected(self, backend):
        self.backend = backend
        self._run(backend.fetch, self._on_first_fetch, busy_msg="Loading…")

    def _on_connect_failed(self, exc):
        msg = store.describe_error(exc)
        self._set_conn("offline", msg)
        self._set_status("Not connected to Google Sheets. Click ↻ Sync to retry.")
        QMessageBox.warning(self, "Couldn't connect to Google Sheets",
                            msg + "\n\nShowing the last synced data. Editing is paused until "
                                  "the connection works.")

    def _on_first_fetch(self, items):
        local = [i for i in self.items if i.get("name")]
        if not items and local:
            reply = QMessageBox.question(
                self, "Empty sheet",
                f"The Google Sheet has no items yet.\n\nUpload the {len(local)} item(s) "
                "currently on this computer to the sheet?",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._run(lambda: self.backend.upload(local),
                          lambda items: self._set_items(items, f"Uploaded {len(local)} item(s)."),
                          busy_msg="Uploading…")
                return
        self._set_items(items, "Loaded from Google Sheets.")

    def _sync(self, quiet):
        if self.backend is None:
            if not quiet and self.cfg["sheet_url"]:
                self._connect()
            return
        if self.backend.kind == "local":
            self._set_items(self.backend.fetch(), None if quiet else "Reloaded.")
            return
        if quiet and self._tasks:
            return      # something is already running; skip this tick
        self._run(self.backend.fetch,
                  lambda items: self._set_items(items, None if quiet else "Synced."),
                  on_error=(lambda exc: self._set_conn("offline", store.describe_error(exc))) if quiet
                           else (lambda exc: self._on_error(exc, note="")),
                  busy_msg=None if quiet else "Syncing…")

    def _set_items(self, items, msg=None):
        self.items = items
        store.save_cache(items)
        if self.backend and self.backend.kind == "sheets":
            self.last_sync = datetime.now()
            self._set_conn("sheets")
        self._refresh_table()
        if msg:
            self._set_status(msg(items) if callable(msg) else msg)

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

    def _refresh_table(self):
        keep = self._selected_id()
        query = self.search.text().strip().lower()
        shown = sorted((i for i in self.items if query in i["name"].lower()),
                       key=lambda x: (self._level(x["qty"]), x["name"].lower()))

        self.table.setRowCount(len(shown))
        for r, it in enumerate(shown):
            lbl, color = self._status(it["qty"])

            name_item = QTableWidgetItem(it["name"])
            name_item.setData(Qt.UserRole, it["id"])
            name_item.setFont(QFont("Helvetica", 13))

            qty_item = QTableWidgetItem(str(it["qty"]))
            qty_item.setTextAlignment(Qt.AlignCenter)
            qty_item.setFont(QFont("Helvetica", 13, QFont.Bold))
            qty_item.setForeground(QColor(color))

            status_item = QTableWidgetItem(lbl)
            status_item.setTextAlignment(Qt.AlignCenter)
            status_item.setFont(QFont("Helvetica", 12))
            status_item.setForeground(QColor(color))

            when = _ago(it.get("updated_at", ""))
            who = it.get("updated_by", "")
            change_item = QTableWidgetItem(f"{when} · {who}" if when and who else when or who)
            change_item.setFont(QFont("Helvetica", 11))
            change_item.setForeground(QColor(MUTED))
            change_item.setToolTip(it.get("updated_at", ""))

            for c, cell in enumerate((name_item, qty_item, status_item, change_item)):
                self.table.setItem(r, c, cell)
            if it["id"] == keep:
                self.table.selectRow(r)

        self.stat_labels["total"].setText(str(len(self.items)))
        self.stat_labels["units"].setText(str(sum(i["qty"] for i in self.items)))
        self.stat_labels["low"].setText(str(sum(1 for i in self.items if self._level(i["qty"]) == 1)))
        self.stat_labels["out"].setText(str(sum(1 for i in self.items if i["qty"] == 0)))

    # ── actions ───────────────────────────────────────────────────────────────

    def _do(self, fn, msg):
        """Apply a change through the backend in the background."""
        if self.backend is None:
            QMessageBox.information(self, "Not connected",
                                    "Not connected to the Google Sheet, so changes can't be saved.\n\n"
                                    "Click ↻ Sync to retry, or check ⚙ Settings.")
            return False
        self._run(fn, lambda items: self._set_items(items, msg), busy_msg="Saving…")
        return True

    def _qty_of(self, items, item_id):
        return next((i["qty"] for i in items if i["id"] == item_id), 0)

    def _add_item(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Please enter an item name.")
            return
        qty = self.qty_input.value()
        if any(i["name"].lower() == name.lower() for i in self.items):
            QMessageBox.warning(self, "Duplicate", f'"{name}" already exists.')
            return
        if self._do(lambda: self.backend.add(name, qty), f'Added "{name}" ×{qty}.'):
            self.name_input.clear()
            self.qty_input.setValue(1)
            self.name_input.setFocus()

    def _checkout(self):
        it = self._selected_item()
        if not it: return
        if it["qty"] == 0:
            QMessageBox.information(self, "Out of stock", f'"{it["name"]}" has no stock left.')
            return
        n, ok = QInputDialog.getInt(self, "Check out", f'How many "{it["name"]}"?',
                                    value=1, min=1, max=it["qty"])
        if not ok: return
        item_id, name = it["id"], it["name"]
        self._do(lambda: self.backend.adjust(item_id, -n),
                 lambda items: f'Checked out {n}× {name} — {self._qty_of(items, item_id)} left.')

    def _restock(self):
        it = self._selected_item()
        if not it: return
        n, ok = QInputDialog.getInt(self, "Restock", f'Units to add to "{it["name"]}":',
                                    value=1, min=1, max=99999)
        if not ok: return
        item_id, name = it["id"], it["name"]
        self._do(lambda: self.backend.adjust(item_id, n),
                 lambda items: f'Restocked "{name}" +{n} → {self._qty_of(items, item_id)} total.')

    def _rename(self):
        it = self._selected_item()
        if not it: return
        new, ok = QInputDialog.getText(self, "Rename", f'New name for "{it["name"]}":', text=it["name"])
        new = new.strip()
        if not ok or not new or new == it["name"]: return
        item_id, old = it["id"], it["name"]
        self._do(lambda: self.backend.rename(item_id, new), f'Renamed "{old}" → "{new}".')

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
