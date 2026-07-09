**DISCLAIMER:** The LumaCore Manager functionality in this application is provided for educational purposes only. The author assumes no responsibility for any issues, damages, or consequences resulting from the use of the LumaCore component or its associated DLLs.
You can find LumaCore source code and documentation [here](https://github.com/Midrags/SFF/tree/main/LumaCore).

# DepotManager

A modular graphical desktop application for downloading Steam depots via multiple manifest sources with [DepotDownloaderMod](https://github.com/SteamAutoCracks/DepotDownloaderMod).

Supports both **API-based fetching**, **offline local archive loading**, and **LumaCore DLL injection management**.

## Project Structure

The source code has been refactored from a monolithic script into a clean, modular Python package:

```txt
Source/
├── DepotManager/              # Modular application (current)
│   ├── __init__.py
│   ├── main.py                # Entry point & logging setup
│   ├── gui.py                 # Tkinter user interface
│   ├── api_client.py          # Async HTTP client (aiohttp)
│   ├── downloader.py          # Download orchestration & subprocess management
│   ├── parser.py              # Lua key extraction & manifest scanning
│   ├── config.py              # Settings, constants & path resolution
│   ├── steam_path.py          # Steam path discovery & library management
│   ├── steam_process.py       # Process management (kill/restart)
│   ├── lumacore_setup.py      # LumaCore installation & update logic
│   ├── lumacore_games.py      # LumaCore game management & ACF handling
│   ├── lumacore_gui.py        # LumaCore Manager UI
│   └── icon.ico
│
├── DepotManager_Monolitic/    # Legacy single-file version
│   └── main.py
│
├── DepotDownloaderMod/        # External download engine (bundled)
│   └── ...
│
└── requirements.txt
```

## Release Bundle Structure

Official releases are distributed as a bundle with two physically separated components:

```txt
DepotManager_vX.X.X/
├── README.txt
├── DepotManager/
│   └── DepotManager.exe       # GUI application (PyInstaller standalone)
└── DepotDownloaderMod/
    └── DepotDownloaderMod.exe # Download engine + runtime DLLs
```

Do not move files out of their folders. DepotManager.exe locates DepotDownloaderMod.exe using a relative path (`../DepotDownloaderMod/DepotDownloaderMod.exe`). Keep the folder structure intact.

## Requirements & External Tools

Before running DepotManager, make sure the release bundle structure shown above is preserved.

- **DepotDownloaderMod** — The underlying download engine.
  Repository: [SteamAutoCracks/DepotDownloaderMod](https://github.com/SteamAutoCracks/DepotDownloaderMod)
- **For API mode**: An API key for at least one supported source:
  - Morrenus's API (HubcapManifest)
  - Ryuu's API

  You can store keys for both and switch between them inside the app.
- **For Local Archive mode**: No API key required. A ZIP file containing `.lua` and `.manifest` files is sufficient.

## Installation

1. Extract the release bundle (`DepotManager_vX.X.X/`) anywhere on your PC.
2. Ensure `DepotManager/` and `DepotDownloaderMod/` remain side-by-side.
3. Run `DepotManager/DepotManager.exe` — no installation required.

## Usage

### Method A: Download via API (requires API key)

1. **Configure your API Key**
   - Select your preferred source from the Source dropdown (Morrenus's API or Ryuu's API).
   - Paste the corresponding API key in the API Key field.
   - Click **Save Key** to persist it in `settings.json`.
2. **Fetch depots for a game**
   - Enter a valid Steam AppID in the Enter AppID field.
   - Click **Fetch Manifest** — the app will contact the selected source and display all available depots.
3. **Select depots and download**
   - Click the checkbox column on each row to select depots, or use Select All / Deselect All.
   - Click **START DOWNLOAD** to begin. Output from DepotDownloaderMod will appear in real time.
   - Click **STOP DOWNLOAD** at any time to cancel all running downloads.

### Method B: Download via Local Archive (offline, no API key)

1. **Load a local archive**
   - Click **Load Archive...** and select a ZIP file containing `.lua` and `.manifest` files.
   - The AppID is automatically detected from the archive name or `.lua` file contents.
2. **Select depots and download**
   - The depot list will populate automatically. Check the depots you want.
   - Click **START DOWNLOAD** to begin downloading.

### Method C: LumaCore Management (Proxy DLL Injection)

The LumaCore Manager tab allows you to manage the LumaCore DLL injector for Steam.

1. **Setup and Installation**
   - The app automatically detects your Steam installation path.
   - Use the **Install / Update** button to download and inject the LumaCore DLLs into your Steam folder. This will automatically close Steam during the process.
   - Use the **Restart Steam** button to quickly relaunch Steam after installation or updates.
2. **Managed Games**
   - The app maintains a list of games injected with LumaCore.
   - Use **Add Game** to inject a new game (this handles Lua installation and ACF creation).
   - Use **Remove Selected** to clean up injected files.
   - The **Restore ACF Backups** button allows you to recover `.acf` files from the automatic backups created during LumaCore full removal.

## Configuration (`settings.json`)

The following settings are stored in `settings.json`, located inside the `DepotManager/` folder. The file is created automatically the first time you save your API Key:

| Key | Default |
|---|---|
| `steam_path` | (Auto-detected) |
| `lumacore_installed_version` | ? |
| `latest_version` | ? |
| `last_check` | ? |
| `api_key_morrenus` | (empty) |
| `api_key_ryuu` | (empty) |
| `selected_source` | morrenus |
| `api_base_url_morrenus` | https://hubcapmanifest.com/api/v1 |
| `api_base_url_ryuu` | https://generator.ryuu.lol/secure_download |
| `exe_name` | ../DepotDownloaderMod/DepotDownloaderMod.exe |
| `max_concurrent_downloads` | 1 |
| `request_timeout` | 30 |

You do not need to edit this file manually. All values work out of the box — the only required change is saving your API Key(s) through the application interface. You can store keys for both sources and switch between them at any time using the Source dropdown.

## Logging

A `depot_manager.log` file is created in the `DepotManager/` working directory and contains detailed debug and error information.

## Notes

- A temporary `keys.txt` file is created during downloads and automatically deleted when the application closes.
- Manifest files are copied temporarily to the working directory and deleted after each download completes.
- **ATTENTION** - When updating to a new version, it is advisable to delete old `settings.json` to get a clean, up-to-date configuration. Before doing so, note down your API keys from the API Key field, then re-enter them after the first launch.

## Running from Source (Python)

If you want to run the modular application with Python instead of the compiled executable:

```bash
cd Source
pip install -r requirements.txt
python -m DepotManager.main
```

Or use the launcher script:

```bash
python run_depot_manager.py
```
