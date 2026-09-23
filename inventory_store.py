"""
Data layer for Qave Inventory.

Two interchangeable backends share the same operations:
  • LocalBackend  – a JSON file in the app's data folder (no setup needed)
  • SheetsBackend – a shared Google Sheet, accessed with a service account

Every operation re-reads the current data before changing it, so edits made
by other people (or typed straight into the sheet) are never overwritten.
"""

import csv, getpass, json, os, shutil, sys, uuid
from datetime import datetime

APP_NAME = "QaveInventory"
TIME_FMT = "%Y-%m-%d %H:%M"

INV_HEADERS = ["id", "name", "qty", "updated_at", "updated_by"]
LOG_HEADERS = ["timestamp", "user", "action", "item", "change", "qty_after"]


# ── files & config ────────────────────────────────────────────────────────────

def _data_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path

DATA_DIR    = _data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
CACHE_FILE  = os.path.join(DATA_DIR, "inventory_data.json")
CREDS_FILE  = os.path.join(DATA_DIR, "service_account.json")
LOCAL_LOG   = os.path.join(DATA_DIR, "log.csv")

DEFAULTS = {"sheet_url": "", "user_name": getpass.getuser(), "low_stock": 3}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg

def save_config(cfg):
    _write_json(CONFIG_FILE, cfg)

def load_cache():
    _migrate_legacy_data()
    try:
        with open(CACHE_FILE) as f:
            return [i for i in json.load(f) if i.get("name")]
    except (OSError, ValueError):
        return []

def save_cache(items):
    _write_json(CACHE_FILE, items)

def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def _migrate_legacy_data():
    """Earlier versions kept inventory_data.json in the working directory."""
    if os.path.exists(CACHE_FILE):
        return
    for d in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        old = os.path.join(d, "inventory_data.json")
        if os.path.isfile(old):
            shutil.copy(old, CACHE_FILE)
            return

def service_account_email(path=CREDS_FILE):
    try:
        with open(path) as f:
            return json.load(f).get("client_email", "")
    except (OSError, ValueError, AttributeError):
        return ""

def install_credentials(src):
    shutil.copy(src, CREDS_FILE)
    os.chmod(CREDS_FILE, 0o600)


# ── errors ────────────────────────────────────────────────────────────────────

class InventoryError(Exception):
    """A problem the user can fix (duplicate name, not enough stock, …)."""

def describe_error(exc):
    """Turn any exception from a backend into a message a person can act on."""
    if isinstance(exc, InventoryError):
        return str(exc)
    import gspread, google.auth.exceptions as gauth
    email = service_account_email()
    share = f"\n\nMake sure the sheet is shared with:\n{email}\n(as an Editor)." if email else ""
    if isinstance(exc, FileNotFoundError):
        return "No Google service-account key installed. Add one in ⚙ Settings."
    if isinstance(exc, (gspread.exceptions.SpreadsheetNotFound,
                        gspread.exceptions.NoValidUrlKeyFound)):
        return "Couldn't find that Google Sheet. Check the link in ⚙ Settings." + share
    if isinstance(exc, PermissionError):
        return "No permission to open the Google Sheet." + share
    if isinstance(exc, gspread.exceptions.APIError):
        code = exc.response.status_code
        if code == 403:
            return "No permission to edit the Google Sheet." + share
        if code == 429:
            return "Google Sheets is rate-limiting requests. Wait a minute and try again."
        return f"Google Sheets error ({code}): {exc}"
    if isinstance(exc, gauth.RefreshError):
        return "Google rejected the service-account key. It may have been deleted — add a new one in ⚙ Settings."
    if isinstance(exc, (gauth.TransportError, OSError)):
        return "Couldn't reach Google Sheets. Check your internet connection."
    return f"{type(exc).__name__}: {exc}"


# ── shared operations ─────────────────────────────────────────────────────────

def _now():
    return datetime.now().strftime(TIME_FMT)

def _to_int(text):
    try:
        return max(0, int(float(str(text).replace(",", "").strip() or 0)))
    except ValueError:
        return 0


class Backend:
    kind = ""

    def __init__(self, user):
        self.user = user or getpass.getuser()

    # Subclasses implement these. Operations mutate the list returned by
    # fetch() first, then call the matching primitive to persist the change.
    def fetch(self): raise NotImplementedError
    def _insert(self, items): raise NotImplementedError
    def _update(self, item): raise NotImplementedError
    def _delete(self, item): raise NotImplementedError
    def _log(self, action, name, change, qty_after): raise NotImplementedError

    def _stamp(self, item):
        item["updated_at"] = _now()
        item["updated_by"] = self.user
        return item

    def _find(self, items, item_id):
        it = next((i for i in items if i["id"] == item_id), None)
        if it is None:
            raise InventoryError("That item no longer exists — someone may have removed it.")
        return it

    def add(self, name, qty):
        items = self.fetch()
        if any(i["name"].lower() == name.lower() for i in items):
            raise InventoryError(f'"{name}" already exists.')
        item = self._stamp({"id": str(uuid.uuid4()), "name": name, "qty": qty})
        items.append(item)
        self._insert([item])
        self._log("add", name, f"+{qty}", qty)
        return items

    def adjust(self, item_id, delta):
        items = self.fetch()
        it = self._find(items, item_id)
        if it["qty"] + delta < 0:
            raise InventoryError(f'Only {it["qty"]} × "{it["name"]}" left.')
        it["qty"] += delta
        self._update(self._stamp(it))
        self._log("check out" if delta < 0 else "restock", it["name"], f"{delta:+d}", it["qty"])
        return items

    def rename(self, item_id, new_name):
        items = self.fetch()
        it = self._find(items, item_id)
        if any(i["name"].lower() == new_name.lower() and i["id"] != item_id for i in items):
            raise InventoryError(f'"{new_name}" already exists.')
        old, it["name"] = it["name"], new_name
        self._update(self._stamp(it))
        self._log("rename", f"{old} → {new_name}", "", it["qty"])
        return items

    def remove(self, item_id):
        items = self.fetch()
        it = self._find(items, item_id)
        items.remove(it)
        self._delete(it)
        self._log("remove", it["name"], f"-{it['qty']}", 0)
        return items

    def upload(self, local_items):
        """Copy items (e.g. from local mode) in, skipping names that exist."""
        items = self.fetch()
        names = {i["name"].lower() for i in items}
        new = [self._stamp({"id": i.get("id") or str(uuid.uuid4()),
                            "name": i["name"], "qty": _to_int(i["qty"])})
               for i in local_items if i["name"].lower() not in names]
        if new:
            items.extend(new)
            self._insert(new)
            self._log("import", f"{len(new)} items", "", "")
        return items


# ── local file ────────────────────────────────────────────────────────────────

class LocalBackend(Backend):
    kind = "local"

    def fetch(self):
        self._items = load_cache()
        return self._items

    def _save(self, *_):
        save_cache(self._items)

    _insert = _update = _delete = _save

    def _log(self, action, name, change, qty_after):
        new = not os.path.exists(LOCAL_LOG)
        with open(LOCAL_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_HEADERS)
            w.writerow([_now(), self.user, action, name, change, qty_after])


# ── Google Sheets ─────────────────────────────────────────────────────────────

class SheetsBackend(Backend):
    """Uses an "Inventory" tab (one row per item) and a "Log" tab (history).

    Columns are found by header name, so they can be reordered in the sheet,
    and rows typed in by hand (name + qty only) get an id assigned on fetch.
    """
    kind = "sheets"

    def __init__(self, sheet_url, creds_path, user):
        super().__init__(user)
        import gspread
        gc = gspread.service_account(filename=creds_path)
        self.book = gc.open_by_url(sheet_url)
        self.inv, self._cols = self._tab("Inventory", INV_HEADERS)
        self.log, self._log_cols = self._tab("Log", LOG_HEADERS)
        self._rows = {}

    def _tab(self, title, headers):
        import gspread
        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            tabs = self.book.worksheets()
            if title == "Inventory" and len(tabs) == 1 and not tabs[0].get_all_values():
                ws = tabs[0]                     # reuse a brand-new sheet's blank tab
                ws.update_title(title)
            else:
                ws = self.book.add_worksheet(title, rows=100, cols=len(headers))

        first = [h.strip().lower() for h in ws.row_values(1)]
        if not any(first):
            ws.update([headers], "A1")
            ws.format("1:1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
            first = list(headers)
        else:
            if not set(headers) & set(first):
                raise InventoryError(
                    f'The "{title}" tab\'s first row should be headers: {", ".join(headers)}')
            missing = [h for h in headers if h not in first]
            if missing:
                need = len(first) + len(missing)
                if ws.col_count < need:
                    ws.add_cols(need - ws.col_count)
                start = gspread.utils.rowcol_to_a1(1, len(first) + 1)
                ws.update([missing], start)
                first += missing
        return ws, {h: first.index(h) + 1 for h in headers}

    @staticmethod
    def _as_row(cols, values):
        row = [""] * max(cols.values())
        for h, v in values.items():
            if h in cols:
                row[cols[h] - 1] = v
        return row

    def fetch(self):
        from gspread.utils import rowcol_to_a1
        values, c = self.inv.get_all_values(), self._cols
        items, self._rows, fixes = [], {}, []
        for r, row in enumerate(values[1:], start=2):
            cell = {h: (row[i - 1].strip() if i <= len(row) else "") for h, i in c.items()}
            if not cell["name"]:
                continue
            item_id = cell["id"]
            if not item_id or item_id in self._rows:     # typed by hand or copy-pasted
                item_id = str(uuid.uuid4())
                fixes.append({"range": rowcol_to_a1(r, c["id"]), "values": [[item_id]]})
            items.append({"id": item_id, "name": cell["name"], "qty": _to_int(cell["qty"]),
                          "updated_at": cell["updated_at"], "updated_by": cell["updated_by"]})
            self._rows[item_id] = r
        if fixes:
            self.inv.batch_update(fixes, value_input_option="RAW")
        return items

    def _insert(self, items):
        rows = [self._as_row(self._cols, i) for i in items]
        self.inv.append_rows(rows, value_input_option="RAW", table_range="A1")

    def _update(self, item):
        from gspread.utils import rowcol_to_a1
        r = self._rows[item["id"]]
        self.inv.batch_update(
            [{"range": rowcol_to_a1(r, self._cols[h]), "values": [[item[h]]]}
             for h in ("name", "qty", "updated_at", "updated_by")],
            value_input_option="RAW")

    def _delete(self, item):
        self.inv.delete_rows(self._rows[item["id"]])

    def _log(self, action, name, change, qty_after):
        row = self._as_row(self._log_cols, {
            "timestamp": _now(), "user": self.user, "action": action,
            "item": name, "change": change, "qty_after": qty_after})
        try:
            self.log.append_row(row, value_input_option="RAW", table_range="A1")
        except Exception as exc:      # the change itself already succeeded
            print("Could not write to Log tab:", exc, file=sys.stderr)
