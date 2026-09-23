"""
Data layer for Qave Inventory.

Two interchangeable backends share the same operations:
  • LocalBackend  – a JSON file in the app's data folder (no setup needed)
  • SheetsBackend – a shared Google Sheet, accessed with a service account

Every operation re-reads the current data before changing it, so edits made
by other people (or typed straight into the sheet) are never overwritten.

An item is either counted (just a qty) or tracked by serial number: it then
has a list of "units", each with its own serial, status and notes, and its
qty is the number of units.
"""

import csv, getpass, json, os, shutil, sys, uuid
from datetime import datetime

APP_NAME = "QaveInventory"
TIME_FMT = "%Y-%m-%d %H:%M"

INV_HEADERS  = ["id", "name", "qty", "updated_at", "updated_by"]
UNIT_HEADERS = ["id", "item_id", "item", "serial", "status", "notes", "updated_at", "updated_by"]
LOG_HEADERS  = ["timestamp", "user", "action", "item", "change", "qty_after"]

STATUSES = ["working", "broken", "in repairs", "dormant"]


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
            items = [i for i in json.load(f) if i.get("name")]
    except (OSError, ValueError):
        return []
    for i in items:
        i.setdefault("units", [])
    return items

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
    def _insert_units(self, item, units): raise NotImplementedError
    def _update_units(self, item, units): raise NotImplementedError
    def _delete_units(self, units): raise NotImplementedError
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

    def _find_unit(self, it, unit_id):
        u = next((u for u in it["units"] if u["id"] == unit_id), None)
        if u is None:
            raise InventoryError("That unit no longer exists — someone may have removed it.")
        return u

    @staticmethod
    def _check_serial(items, serial, unit_id=None):
        for i in items:
            for u in i["units"]:
                if u["serial"].lower() == serial.lower() and u["id"] != unit_id:
                    raise InventoryError(f'Serial number "{serial}" is already used by {i["name"]}.')

    def add(self, name, qty):
        items = self.fetch()
        if any(i["name"].lower() == name.lower() for i in items):
            raise InventoryError(f'"{name}" already exists.')
        item = self._stamp({"id": str(uuid.uuid4()), "name": name, "qty": qty, "units": []})
        items.append(item)
        self._insert([item])
        self._log("add", name, f"+{qty}", qty)
        return items

    def adjust(self, item_id, delta):
        items = self.fetch()
        it = self._find(items, item_id)
        if it["units"]:
            raise InventoryError(f'"{it["name"]}" is tracked by serial number — '
                                 "add or remove units under Serial numbers instead.")
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
        if it["units"]:
            self._update_units(it, it["units"])
        self._log("rename", f"{old} → {new_name}", "", it["qty"])
        return items

    def remove(self, item_id):
        items = self.fetch()
        it = self._find(items, item_id)
        items.remove(it)
        if it["units"]:
            self._delete_units(it["units"])
        self._delete(it)
        self._log("remove", it["name"], f"-{it['qty']}", 0)
        return items

    # ── serial-numbered units ──

    def add_unit(self, item_id, serial, status="working", notes=""):
        items = self.fetch()
        it = self._find(items, item_id)
        self._check_serial(items, serial)
        unit = self._stamp({"id": str(uuid.uuid4()), "serial": serial,
                            "status": status or "working", "notes": notes})
        it["units"].append(unit)
        it["qty"] = len(it["units"])
        self._insert_units(it, [unit])
        self._update(self._stamp(it))
        self._log("add unit", f'{it["name"]} ({serial})', unit["status"], it["qty"])
        return items

    def update_unit(self, item_id, unit_id, **changes):
        """changes: any of serial=, status=, notes="""
        items = self.fetch()
        it = self._find(items, item_id)
        u = self._find_unit(it, unit_id)
        if "serial" in changes:
            self._check_serial(items, changes["serial"], unit_id)
        diff = []
        for k, v in changes.items():
            if u.get(k, "") != v:
                diff.append(f'{k}: {u.get(k) or "—"} → {v or "—"}')
                u[k] = v
        if diff:
            self._update_units(it, [self._stamp(u)])
            self._update(self._stamp(it))
            self._log("edit unit", f'{it["name"]} ({u["serial"]})', "; ".join(diff), it["qty"])
        return items

    def remove_unit(self, item_id, unit_id):
        items = self.fetch()
        it = self._find(items, item_id)
        u = self._find_unit(it, unit_id)
        it["units"].remove(u)
        it["qty"] = len(it["units"])
        self._delete_units([u])
        self._update(self._stamp(it))
        self._log("remove unit", f'{it["name"]} ({u["serial"]})', "-1", it["qty"])
        return items

    def upload(self, local_items):
        """Copy items (e.g. from local mode) in, skipping names that exist."""
        items = self.fetch()
        names = {i["name"].lower() for i in items}
        new = []
        for i in local_items:
            if i["name"].lower() in names:
                continue
            units = [dict(u) for u in i.get("units", [])]
            new.append(self._stamp({"id": i.get("id") or str(uuid.uuid4()), "name": i["name"],
                                    "qty": len(units) if units else _to_int(i["qty"]),
                                    "units": units}))
        if new:
            items.extend(new)
            self._insert(new)
            for it in new:
                if it["units"]:
                    self._insert_units(it, it["units"])
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
    _insert_units = _update_units = _delete_units = _save

    def _log(self, action, name, change, qty_after):
        new = not os.path.exists(LOCAL_LOG)
        with open(LOCAL_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_HEADERS)
            w.writerow([_now(), self.user, action, name, change, qty_after])


# ── Google Sheets ─────────────────────────────────────────────────────────────

class SheetsBackend(Backend):
    """Uses an "Inventory" tab (one row per item), a "Units" tab (one row per
    serial-numbered unit) and a "Log" tab (history).

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
        self.units, self._unit_cols = self._tab("Units", UNIT_HEADERS)
        self.log, self._log_cols = self._tab("Log", LOG_HEADERS)
        self._rows, self._unit_rows = {}, {}

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
        inv_vals, unit_vals = (
            r.get("values", []) for r in self.book.values_batch_get(
                [f"'{self.inv.title}'", f"'{self.units.title}'"])["valueRanges"])
        c, uc = self._cols, self._unit_cols
        items, self._rows, self._unit_rows = [], {}, {}
        fixes, unit_fixes = [], []

        def cells(row, cols):
            return {h: (row[i - 1].strip() if i <= len(row) else "") for h, i in cols.items()}

        for r, row in enumerate(inv_vals[1:], start=2):
            cell = cells(row, c)
            if not cell["name"]:
                continue
            item_id = cell["id"]
            if not item_id or item_id in self._rows:     # typed by hand or copy-pasted
                item_id = str(uuid.uuid4())
                fixes.append({"range": rowcol_to_a1(r, c["id"]), "values": [[item_id]]})
            items.append({"id": item_id, "name": cell["name"], "qty": _to_int(cell["qty"]),
                          "updated_at": cell["updated_at"], "updated_by": cell["updated_by"],
                          "units": []})
            self._rows[item_id] = r

        by_id = {i["id"]: i for i in items}
        by_name = {i["name"].lower(): i for i in items}
        for r, row in enumerate(unit_vals[1:], start=2):
            cell = cells(row, uc)
            it = by_id.get(cell["item_id"]) or by_name.get(cell["item"].lower())
            if not cell["serial"] or it is None:
                continue
            unit_id = cell["id"]
            if not unit_id or unit_id in self._unit_rows:
                unit_id = str(uuid.uuid4())
                unit_fixes.append({"range": rowcol_to_a1(r, uc["id"]), "values": [[unit_id]]})
            if cell["item_id"] != it["id"]:              # linked by name when typed by hand
                unit_fixes.append({"range": rowcol_to_a1(r, uc["item_id"]), "values": [[it["id"]]]})
            it["units"].append({"id": unit_id, "serial": cell["serial"],
                                "status": cell["status"].lower() or "working", "notes": cell["notes"],
                                "updated_at": cell["updated_at"], "updated_by": cell["updated_by"]})
            self._unit_rows[unit_id] = r

        for it in items:                                 # serialized qty = number of units
            if it["units"] and it["qty"] != len(it["units"]):
                it["qty"] = len(it["units"])
                fixes.append({"range": rowcol_to_a1(self._rows[it["id"]], c["qty"]),
                              "values": [[it["qty"]]]})
        if fixes:
            self.inv.batch_update(fixes, value_input_option="RAW")
        if unit_fixes:
            self.units.batch_update(unit_fixes, value_input_option="RAW")
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

    def _unit_values(self, item, unit):
        return dict(unit, item_id=item["id"], item=item["name"])

    def _insert_units(self, item, units):
        rows = [self._as_row(self._unit_cols, self._unit_values(item, u)) for u in units]
        self.units.append_rows(rows, value_input_option="RAW", table_range="A1")

    def _update_units(self, item, units):
        from gspread.utils import rowcol_to_a1
        data = []
        for u in units:
            r, vals = self._unit_rows[u["id"]], self._unit_values(item, u)
            data += [{"range": rowcol_to_a1(r, self._unit_cols[h]), "values": [[vals.get(h, "")]]}
                     for h in ("item", "serial", "status", "notes", "updated_at", "updated_by")]
        self.units.batch_update(data, value_input_option="RAW")

    def _delete_units(self, units):
        for r in sorted((self._unit_rows[u["id"]] for u in units), reverse=True):
            self.units.delete_rows(r)       # bottom-up so row numbers stay valid

    def _log(self, action, name, change, qty_after):
        row = self._as_row(self._log_cols, {
            "timestamp": _now(), "user": self.user, "action": action,
            "item": name, "change": change, "qty_after": qty_after})
        try:
            self.log.append_row(row, value_input_option="RAW", table_range="A1")
        except Exception as exc:      # the change itself already succeeded
            print("Could not write to Log tab:", exc, file=sys.stderr)
