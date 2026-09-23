# Qave Inventory

A small desktop app for tracking stock: add items, check them out, restock, and see
what's running low. Data can live on your computer or in a **shared Google Sheet**,
so several people can use the app at once and anyone can view the data in the browser.

![icon](assets/icon.png)

## Features

- Add, check out, restock, rename, and remove items (double-click a row to check out)
- **Optional serial numbers**: track equipment unit by unit (e.g. `HT-12005`), each with
  a status (**working**, **broken**, **in repairs**, **dormant**) and notes. Select an item
  and click **Serial numbers…**
- Low-stock and out-of-stock highlighting, with an adjustable alert level
- Search box, plus a "Last change" column showing who changed each item and when
- **Google Sheets sync**: the sheet acts as the database. The app pulls changes every
  minute (or when you click **↻ Sync**), and every change is written straight to the sheet
- A **Log** tab in the sheet records every change: time, person, action, and amount
- Works offline in local mode. In Sheets mode it keeps showing the last synced data if
  the connection drops

## Install the app (macOS)

```bash
./build_mac.sh --install
```

This builds `Qave Inventory.app` and copies it to `/Applications`. After that, open it
from Spotlight or Launchpad like any other app. (Drop `--install` to only build into `dist/`.)

> **Sharing the app with coworkers:** send them the zipped `.app`. It isn't signed by
> Apple, so the first time they must **right-click → Open → Open**.

**Windows:** run `build_windows.bat` on a Windows PC with Python 3. The app ends up in
`dist\Qave Inventory\Qave Inventory.exe`.

**Run from source (for development):**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python qave_inventory.py
```

## Connect a Google Sheet

The app signs in to Google with a *service account*: a robot Google account that you
share the sheet with. This is a one-time setup, about 10 minutes.

1. **Create a Google Cloud project.** Go to <https://console.cloud.google.com/>, then
   project picker → **New project** (for example, `qave-inventory`).
2. **Enable the API.** Go to **APIs & Services → Library**, search for
   **Google Sheets API**, and click **Enable**.
3. **Create the service account.** Go to **APIs & Services → Credentials →
   Create credentials → Service account**. Give it a name (for example,
   `inventory-bot`) and click **Done**. Skip the optional role steps.
4. **Download a key.** Click the new service account → **Keys → Add key →
   Create new key → JSON**. A `.json` file downloads. Keep it private: anyone with the
   file can edit sheets shared with the account.
5. **Create the sheet** at <https://sheets.new> (or use an existing one).
6. **Share the sheet** with the service account's email, which looks like
   `inventory-bot@your-project.iam.gserviceaccount.com`, as an **Editor**.
7. **Configure the app.** In the app, click **⚙ Settings**, paste the sheet link, click
   **Choose key file…**, pick the JSON file, then click **Save**.

The app creates **Inventory**, **Units**, and **Log** tabs. If you were already using the
app locally and the sheet is empty, it offers to upload your existing items.

Each teammate installs the app and repeats step 7 with the same link and key file.

> If sharing with the service account fails ("can't share outside your organization"),
> your Google Workspace admin may need to allow it, or create the service account in the
> company's Google Cloud organization.

## Serial numbers

Most items are simply counted. For equipment where each unit matters, select the item
and click **Serial numbers…** (or double-click it once it has serials) to add units. Each
unit has a serial number, a status, and optional notes. Change a status right from the
dropdown in the list, and it saves immediately.

Once an item has serial numbers, its quantity is the number of units. Its colour in the
main list reflects how many units work: green if all do, amber if some don't, and red if
none do. Search matches serial numbers too.

### Sheet format

**Inventory** tab: one row per item

| id | name | qty | updated_at | updated_by |
|----|------|-----|------------|------------|

**Units** tab: one row per serial-numbered unit

| id | item_id | item | serial | status | notes | updated_at | updated_by |
|----|---------|------|--------|--------|-------|------------|------------|

You can edit the sheet by hand. Change a `qty` or add a row with just a `name` and a
`qty`, and the app picks it up on the next sync (it fills in the `id` itself). On the
Units tab, a row with just `item` (the item's name), `serial`, and `status` works too.
Columns are matched by header name, so you can reorder them or add your own columns next to them.

## Where data is stored

Settings, the local data file, and the key are kept in
`~/Library/Application Support/QaveInventory/` on macOS or `%APPDATA%\QaveInventory\`
on Windows, never in this repo. `.gitignore` excludes data files and keys, so they
can't be committed by accident.

## Project layout

| File | What it is |
|------|------------|
| `qave_inventory.py` | The app window (PyQt5) |
| `inventory_store.py` | Data layer: local JSON and Google Sheets backends |
| `build_mac.sh` / `build_windows.bat` | Build the standalone app with PyInstaller |
| `scripts/make_icon.py` | Regenerates `assets/icon.png` and `icon.icns` |
