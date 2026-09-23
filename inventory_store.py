"""
Data layer for Qave Inventory.

Two interchangeable backends share the same operations:
  • LocalBackend  – a JSON file in the app's data folder (no setup needed)
  • SheetsBackend – a shared Google Sheet, accessed with a service account

Every operation re-reads the current data before changing it, so edits made
by other people (or typed straight into the sheet) are never overwritten.

Each item has a type, and each type has a tracking mode:
  • count  – just a quantity (e.g. Cables)
  • serial – one "unit" per serial number, each with a status (e.g. Test Bench)
  • batch  – "units" are batches: nickname + serial + quantity (e.g. Disc)
For serial and batch items, the item's qty is derived from its units.
"""

import csv, getpass, json, os, shutil, sys, uuid
from datetime import datetime

APP_NAME = "QaveInventory"
TIME_FMT = "%Y-%m-%d %H:%M"

INV_HEADERS  = ["id", "name", "type", "qty", "updated_at", "updated_by"]
UNIT_HEADERS = ["id", "item_id", "item", "nickname", "serial", "qty", "status", "notes",
                "loaned_to", "updated_at", "updated_by"]
TYPE_HEADERS = ["name", "tracking"]
LOG_HEADERS  = ["timestamp", "user", "action", "item", "change", "qty_after", "note"]

TRACKING = {"count":  "Count only",
            "serial": "Serial number per unit",
            "batch":  "Batches (nickname + serial number + quantity)"}
DEFAULT_TYPES = [{"name": "Test Bench",  "tracking": "serial"},
                 {"name": "TBox",        "tracking": "serial"},
                 {"name": "Instruments", "tracking": "serial"},
                 {"name": "Cables",      "tracking": "count"},
                 {"name": "Disc",        "tracking": "batch"}]
STATUSES  = ["working", "loaned", "broken", "in repairs", "dormant"]
LOANABLE  = ("working", "dormant")       # statuses a unit can be checked out from


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
    """Returns {"items": [...], "types": [...]}."""
    _migrate_legacy_data()
    try:
        with open(CACHE_FILE) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, list):                   # older versions stored just the items
        raw = {"items": raw}
    items = [i for i in raw.get("items", []) if isinstance(i, dict) and i.get("name")]
    for i in items:
        i.setdefault("type", "")
        for u in i.setdefault("units", []):
            for k, v in (("nickname", ""), ("qty", 1), ("status", "working"),
                         ("notes", ""), ("loaned_to", "")):
                u.setdefault(k, v)
    return {"items": items, "types": raw.get("types") or [dict(t) for t in DEFAULT_TYPES]}

def save_cache(data):
    _write_json(CACHE_FILE, data)

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


# ── helpers ───────────────────────────────────────────────────────────────────

def _now():
    return datetime.now().strftime(TIME_FMT)

def _to_int(text):
    try:
        return max(0, int(float(str(text).replace(",", "").strip() or 0)))
    except ValueError:
        return 0

def tracking_of(item, types):
    """"count", "serial" or "batch" for an item, based on its type."""
    name = (item.get("type") or "").lower()
    for t in types:
        if t["name"].lower() == name:
            return t["tracking"]
    return "serial" if item.get("units") else "count"   # untyped items from older versions

def unit_label(item, unit):
    if unit.get("nickname"):
        return f'{item["name"]} ({unit["nickname"]} · {unit["serial"]})'
    return f'{item["name"]} ({unit["serial"]})'


# ── shared operations ─────────────────────────────────────────────────────────

class Backend:
    kind = ""

    def __init__(self, user):
        self.user = user or getpass.getuser()
        self.types = [dict(t) for t in DEFAULT_TYPES]

    # Subclasses implement these. Operations mutate the list returned by
    # fetch() first, then call the matching primitive to persist the change.
    def fetch(self): raise NotImplementedError
    def _insert(self, items): raise NotImplementedError
    def _update(self, item): raise NotImplementedError
    def _delete(self, item): raise NotImplementedError
    def _insert_units(self, item, units): raise NotImplementedError
    def _update_units(self, item, units): raise NotImplementedError
    def _delete_units(self, units): raise NotImplementedError
    def _insert_type(self, t): raise NotImplementedError
    def _log(self, action, name, change, qty_after, note=""): raise NotImplementedError

    def load(self):
        return self._result(self.fetch())

    def _result(self, items):
        return {"items": items, "types": list(self.types)}

    def _stamp(self, obj):
        obj["updated_at"] = _now()
        obj["updated_by"] = self.user
        return obj

    def _tracking(self, it):
        return tracking_of(it, self.types)

    def _recount(self, it):
        mode = self._tracking(it)
        if mode == "serial":
            it["qty"] = len(it["units"])
        elif mode == "batch":
            it["qty"] = sum(u["qty"] for u in it["units"])

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

    def _check_unit(self, items, it, serial, nickname="", unit_id=None):
        if self._tracking(it) == "batch":       # batches: nickname + serial unique per item
            for u in it["units"]:
                if (u["serial"].lower(), u["nickname"].lower()) == (serial.lower(), nickname.lower()) \
                        and u["id"] != unit_id:
                    raise InventoryError(f'{it["name"]} already has batch "{nickname} · {serial}". '
                                         "Restock that batch instead.")
            return
        for i in items:                         # serial numbers: unique everywhere
            if self._tracking(i) != "serial":
                continue
            for u in i["units"]:
                if u["serial"].lower() == serial.lower() and u["id"] != unit_id:
                    raise InventoryError(f'Serial number "{serial}" is already used by {i["name"]}.')

    # ── types ──

    def add_type(self, name, tracking):
        items = self.fetch()
        if any(t["name"].lower() == name.lower() for t in self.types):
            raise InventoryError(f'Type "{name}" already exists.')
        t = {"name": name, "tracking": tracking}
        self.types.append(t)
        self._insert_type(t)
        self._log("add type", name, TRACKING[tracking], "")
        return self._result(items)

    # ── items ──

    def add(self, name, qty, type_=""):
        items = self.fetch()
        if any(i["name"].lower() == name.lower() for i in items):
            raise InventoryError(f'"{name}" already exists.')
        item = {"id": str(uuid.uuid4()), "name": name, "type": type_, "qty": qty, "units": []}
        self._recount(item)
        items.append(self._stamp(item))
        self._insert([item])
        self._log("add", name, f"+{item['qty']}", item["qty"], type_)
        return self._result(items)

    def adjust(self, item_id, delta, note=""):
        items = self.fetch()
        it = self._find(items, item_id)
        if self._tracking(it) != "count":
            raise InventoryError(f'"{it["name"]}" is tracked by serial number or batch.')
        if it["qty"] + delta < 0:
            raise InventoryError(f'Only {it["qty"]} × "{it["name"]}" left.')
        it["qty"] += delta
        self._update(self._stamp(it))
        self._log("check out" if delta < 0 else "restock", it["name"], f"{delta:+d}", it["qty"], note)
        return self._result(items)

    def edit_item(self, item_id, name, type_):
        items = self.fetch()
        it = self._find(items, item_id)
        if any(i["name"].lower() == name.lower() and i["id"] != item_id for i in items):
            raise InventoryError(f'"{name}" already exists.')
        if it["units"] and tracking_of(dict(it, type=type_), self.types) != self._tracking(it):
            raise InventoryError(
                f'"{it["name"]}" has serial numbers or batches recorded. Remove them before '
                "switching to a type that is tracked differently.")
        diff = []
        if name != it["name"]:
            diff.append(f'{it["name"]} → {name}')
        if type_ != it.get("type", ""):
            diff.append(f'type: {it.get("type") or "—"} → {type_ or "—"}')
        if not diff:
            return self._result(items)
        it["name"], it["type"] = name, type_
        self._recount(it)
        self._update(self._stamp(it))
        if it["units"]:
            self._update_units(it, it["units"])
        self._log("edit", name, "; ".join(diff), it["qty"])
        return self._result(items)

    def remove(self, item_id):
        items = self.fetch()
        it = self._find(items, item_id)
        items.remove(it)
        if it["units"]:
            self._delete_units(it["units"])
        self._delete(it)
        self._log("remove", it["name"], f"-{it['qty']}", 0)
        return self._result(items)

    # ── units (serial numbers and batches) ──

    def add_unit(self, item_id, serial, status="working", notes="", nickname="", qty=1):
        items = self.fetch()
        it = self._find(items, item_id)
        mode = self._tracking(it)
        if mode == "count":
            raise InventoryError(f'"{it["name"]}" is counted, not tracked by serial number. '
                                 "Change its type first.")
        self._check_unit(items, it, serial, nickname)
        batch = mode == "batch"
        unit = self._stamp({"id": str(uuid.uuid4()), "serial": serial,
                            "nickname": nickname if batch else "", "qty": qty if batch else 1,
                            "status": "" if batch else (status or "working"),
                            "notes": notes, "loaned_to": ""})
        it["units"].append(unit)
        self._recount(it)
        self._insert_units(it, [unit])
        self._update(self._stamp(it))
        self._log("add batch" if batch else "add unit", unit_label(it, unit),
                  f"+{unit['qty']}" if batch else unit["status"], it["qty"])
        return self._result(items)

    def update_unit(self, item_id, unit_id, **changes):
        """changes: any of serial=, nickname=, qty=, status=, notes="""
        items = self.fetch()
        it = self._find(items, item_id)
        u = self._find_unit(it, unit_id)
        if "serial" in changes or "nickname" in changes:
            self._check_unit(items, it, changes.get("serial", u["serial"]),
                             changes.get("nickname", u["nickname"]), unit_id)
        if changes.get("status", "loaned") != "loaned":
            changes["loaned_to"] = ""
        diff = []
        for k, v in changes.items():
            if u.get(k, "") != v:
                diff.append(f'{k}: {u.get(k) or "—"} → {v or "—"}')
                u[k] = v
        if diff:
            self._recount(it)
            self._update_units(it, [self._stamp(u)])
            self._update(self._stamp(it))
            self._log("edit unit", unit_label(it, u), "; ".join(diff), it["qty"])
        return self._result(items)

    def remove_unit(self, item_id, unit_id):
        items = self.fetch()
        it = self._find(items, item_id)
        u = self._find_unit(it, unit_id)
        it["units"].remove(u)
        self._recount(it)
        self._delete_units([u])
        self._update(self._stamp(it))
        self._log("remove unit", unit_label(it, u), f"-{u['qty']}", it["qty"])
        return self._result(items)

    def loan_unit(self, item_id, unit_id, loaned_to):
        """Check out one serial-numbered unit: it's marked "loaned"."""
        items = self.fetch()
        it = self._find(items, item_id)
        u = self._find_unit(it, unit_id)
        if u["status"] == "loaned":
            raise InventoryError(f'{u["serial"]} is already on loan ({u["loaned_to"] or "no note"}).')
        u["status"], u["loaned_to"] = "loaned", loaned_to
        self._update_units(it, [self._stamp(u)])
        self._update(self._stamp(it))
        self._log("loan", unit_label(it, u), "loaned", it["qty"], loaned_to)
        return self._result(items)

    def return_unit(self, item_id, unit_id, status="working", note=""):
        items = self.fetch()
        it = self._find(items, item_id)
        u = self._find_unit(it, unit_id)
        if u["status"] != "loaned":
            raise InventoryError(f'{u["serial"]} is not on loan.')
        was = u["loaned_to"]
        u["status"], u["loaned_to"] = status, ""
        self._update_units(it, [self._stamp(u)])
        self._update(self._stamp(it))
        self._log("return", unit_label(it, u), f"returned from {was}" if was else "returned",
                  it["qty"], note)
        return self._result(items)

    def adjust_batch(self, item_id, unit_id, delta, note=""):
        items = self.fetch()
        it = self._find(items, item_id)
        u = self._find_unit(it, unit_id)
        if u["qty"] + delta < 0:
            raise InventoryError(f'Only {u["qty"]} left in {unit_label(it, u)}.')
        u["qty"] += delta
        self._recount(it)
        self._update_units(it, [self._stamp(u)])
        self._update(self._stamp(it))
        self._log("check out" if delta < 0 else "restock", unit_label(it, u),
                  f"{delta:+d}", it["qty"], note)
        return self._result(items)

    def upload(self, local_items, local_types=()):
        """Copy items and types (e.g. from local mode) in, skipping names that exist."""
        items = self.fetch()
        for t in local_types:
            if not any(x["name"].lower() == t["name"].lower() for x in self.types):
                self.types.append(dict(t))
                self._insert_type(t)
        names = {i["name"].lower() for i in items}
        new = []
        for i in local_items:
            if i["name"].lower() in names:
                continue
            it = {"id": i.get("id") or str(uuid.uuid4()), "name": i["name"],
                  "type": i.get("type", ""), "qty": _to_int(i["qty"]),
                  "units": [dict(u) for u in i.get("units", [])]}
            self._recount(it)
            new.append(self._stamp(it))
        if new:
            items.extend(new)
            self._insert(new)
            for it in new:
                if it["units"]:
                    self._insert_units(it, it["units"])
            self._log("import", f"{len(new)} items", "", "")
        return self._result(items)


# ── local file ────────────────────────────────────────────────────────────────

class LocalBackend(Backend):
    kind = "local"

    def fetch(self):
        self._data = load_cache()
        self.types = self._data["types"]
        return self._data["items"]

    def _save(self, *_):
        save_cache(self._data)

    _insert = _update = _delete = _save
    _insert_units = _update_units = _delete_units = _insert_type = _save

    def _log(self, action, name, change, qty_after, note=""):
        new = not os.path.exists(LOCAL_LOG)
        with open(LOCAL_LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(LOG_HEADERS)
            w.writerow([_now(), self.user, action, name, change, qty_after, note])


# ── Google Sheets ─────────────────────────────────────────────────────────────

class SheetsBackend(Backend):
    """Uses tabs "Inventory" (one row per item), "Units" (one row per serial-
    numbered unit or batch), "Types" (item types) and "Log" (history).

    Columns are found by header name, so they can be reordered in the sheet,
    and rows typed in by hand get their ids filled in on fetch.
    """
    kind = "sheets"

    def __init__(self, sheet_url, creds_path, user):
        super().__init__(user)
        import gspread
        gc = gspread.service_account(filename=creds_path)
        self.book = gc.open_by_url(sheet_url)
        self.inv, self._cols = self._tab("Inventory", INV_HEADERS)
        self.units, self._unit_cols = self._tab("Units", UNIT_HEADERS)
        self.types_ws, self._type_cols = self._tab("Types", TYPE_HEADERS)
        self.log, self._log_cols = self._tab("Log", LOG_HEADERS)
        if len(self.types_ws.get_all_values()) <= 1:          # new sheet: add default types
            self.types_ws.append_rows([self._as_row(self._type_cols, t) for t in DEFAULT_TYPES],
                                      value_input_option="RAW", table_range="A1")
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
        inv_vals, unit_vals, type_vals = (
            r.get("values", []) for r in self.book.values_batch_get(
                [f"'{ws.title}'" for ws in (self.inv, self.units, self.types_ws)])["valueRanges"])
        c, uc = self._cols, self._unit_cols
        items, self._rows, self._unit_rows = [], {}, {}
        fixes, unit_fixes = [], []

        def cells(row, cols):
            return {h: (row[i - 1].strip() if i <= len(row) else "") for h, i in cols.items()}

        self.types = []
        for row in type_vals[1:]:
            cell = cells(row, self._type_cols)
            if cell["name"] and not any(t["name"].lower() == cell["name"].lower() for t in self.types):
                tracking = cell["tracking"].lower()
                self.types.append({"name": cell["name"],
                                   "tracking": tracking if tracking in TRACKING else "count"})

        for r, row in enumerate(inv_vals[1:], start=2):
            cell = cells(row, c)
            if not cell["name"]:
                continue
            item_id = cell["id"]
            if not item_id or item_id in self._rows:     # typed by hand or copy-pasted
                item_id = str(uuid.uuid4())
                fixes.append({"range": rowcol_to_a1(r, c["id"]), "values": [[item_id]]})
            items.append({"id": item_id, "name": cell["name"], "type": cell["type"],
                          "qty": _to_int(cell["qty"]), "units": [],
                          "updated_at": cell["updated_at"], "updated_by": cell["updated_by"]})
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
            it["units"].append({"id": unit_id, "serial": cell["serial"], "nickname": cell["nickname"],
                                "qty": _to_int(cell["qty"]) if cell["qty"] else 1,
                                "status": cell["status"].lower(), "notes": cell["notes"],
                                "loaned_to": cell["loaned_to"],
                                "updated_at": cell["updated_at"], "updated_by": cell["updated_by"]})
            self._unit_rows[unit_id] = r

        for it in items:
            if self._tracking(it) == "serial":
                for u in it["units"]:
                    u["status"] = u["status"] or "working"
            before = it["qty"]
            self._recount(it)                            # keep the sheet's qty column honest
            if it["qty"] != before:
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
            [{"range": rowcol_to_a1(r, self._cols[h]), "values": [[item.get(h, "")]]}
             for h in ("name", "type", "qty", "updated_at", "updated_by")],
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
                     for h in UNIT_HEADERS[2:]]
        self.units.batch_update(data, value_input_option="RAW")

    def _delete_units(self, units):
        for r in sorted((self._unit_rows[u["id"]] for u in units), reverse=True):
            self.units.delete_rows(r)       # bottom-up so row numbers stay valid

    def _insert_type(self, t):
        self.types_ws.append_row(self._as_row(self._type_cols, t),
                                 value_input_option="RAW", table_range="A1")

    def _log(self, action, name, change, qty_after, note=""):
        row = self._as_row(self._log_cols, {
            "timestamp": _now(), "user": self.user, "action": action,
            "item": name, "change": change, "qty_after": qty_after, "note": note})
        try:
            self.log.append_row(row, value_input_option="RAW", table_range="A1")
        except Exception as exc:      # the change itself already succeeded
            print("Could not write to Log tab:", exc, file=sys.stderr)
