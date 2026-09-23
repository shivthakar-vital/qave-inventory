# Install Qave Inventory on your Mac

Setup takes about 2 minutes. You'll need a Mac with Apple Silicon (M1 or newer), which is
any Mac from late 2020 onwards. To check, open the  Apple menu → **About This Mac** and
look for **Chip: Apple M…**.

## Step 1: Install the app

1. Open **Terminal**: press **⌘ Space**, type `Terminal`, and press **Return**.
2. Copy this line, paste it into Terminal, and press **Return**:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/shivthakar-vital/qave-inventory/main/install.sh | bash
   ```

3. When it says **✓ Installed**, Qave Inventory opens automatically. You can close Terminal.

From now on, open it like any other app: press **⌘ Space**, type `Qave`, and press
**Return**. You'll also find it in Launchpad and your Applications folder.

> **To update** to a newer version later, run the same line again.

## Step 2: Connect to the shared inventory

When the app first opens, it may say it can't connect to Google Sheets. That's expected
until you finish this step.

1. Ask **Shiv** for two things:
   - the **Google Sheet link**
   - the **key file**, a small `.json` file
2. In Qave Inventory, click **⚙ Settings**.
3. Paste the link into **Google Sheet link**.
4. Click **Choose key file…** and pick the `.json` file Shiv sent you.
5. Check that **Your name** is right. It's shown next to every change you make.
6. Click **Save**.

The inventory loads, and the bottom-right corner shows **● Google Sheets · synced**.

> 🔒 Keep the key file private. Don't post it in public channels or email it outside the
> company. Anyone who has it can edit the inventory.

---

## Other ways to install

<details>
<summary><b>Download it instead of using Terminal</b></summary>

1. Go to the [latest release](https://github.com/shivthakar-vital/qave-inventory/releases/latest)
   and download **Qave-Inventory-mac.zip**.
2. Double-click the zip, then drag **Qave Inventory** into your **Applications** folder.
3. Open it. macOS warns that it *"can't be opened"* or that *Apple could not verify it*,
   because the app isn't registered with Apple. Click **Done**.
4. Open **System Settings → Privacy & Security**, scroll down to
   *"Qave Inventory was blocked…"*, click **Open Anyway**, and enter your Mac password.

You only need to do this once. The Terminal method above skips this warning.

</details>

<details>
<summary><b>Intel Mac</b></summary>

The ready-made app only runs on Apple Silicon. On an Intel Mac, build it yourself with
Python 3: download the code, then run `./build_mac.sh --install` in Terminal from that folder.

</details>

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Couldn't connect to Google Sheets" | Check your internet connection. Then open ⚙ Settings and check the link and key file. |
| "No permission to open the Google Sheet" | Tell Shiv. The sheet needs to be shared with the key's service account. |
| App won't open after downloading the zip | Follow step 4 of *Download it instead of using Terminal* above. |
| Want to uninstall | Drag **Qave Inventory** from Applications to the Trash. To also remove its settings, delete `~/Library/Application Support/QaveInventory`. |

---

### For Shiv: publishing a new version

1. In `qave_inventory.py`, bump the version near the top:

   ```python
   APP_VERSION = "1.0.3"
   ```

2. Commit that change, push it to `main`, then push a matching tag:

   ```bash
   git tag v1.0.3
   ```

   ```bash
   git push origin v1.0.3
   ```

GitHub builds and publishes the app in about 3 minutes. If the tag doesn't match
`APP_VERSION`, the build stops with an error and nothing is published. Everyone gets the
update the next time they run the install line, and the version shows in the bottom-right
corner of the app.
