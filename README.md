# EnergyFlow Studio

> A modern desktop app for syncing DessMonitor telemetry, visualizing real-time energy flow, and exploring historical analytics in a PySide6 interface.

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-Unspecified-lightgrey?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active%20Development-0ea5e9?style=for-the-badge)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-111827?style=for-the-badge)

## 📚 Table of Contents

- [📸 Preview](#-preview)
- [✨ Features](#-features)
- [🛠 Tech Stack](#-tech-stack)
- [🚀 Installation](#-installation)
- [📁 Project Structure](#-project-structure)
- [🧩 Architecture](#-architecture)
- [🎨 Styling System](#-styling-system)
- [▶️ Usage](#-usage)
- [🧪 Development](#-development)
- [🤝 Contributing](#-contributing)
- [🔮 Future Improvements](#-future-improvements)
- [📄 License](#-license)

## 📸 Preview

![App Screenshot](docs/images/screenshot.png)

## ✨ Features

- Sync telemetry directly from DessMonitor using profile-based credentials.
- Manage multiple inverter/device profiles and switch active profile quickly.
- Render EnergyFlow dashboard data (PV, Grid, Battery, Home) with status widgets.
- Open detailed history dialogs for PV, Grid, Battery, Home, and weather metrics.
- Run inverter analysis with period-based navigation (day / month / year / total).
- Use interactive Plotly charts hosted in `QWebEngineView`.
- Import/export inverter field metadata via Excel (`.xlsx`).
- Load local datasets and keep a saved data workflow inside the app.
- Optional Tuya integration paths for smart-device monitoring/control.
- Build distributable desktop app via PyInstaller spec.

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| UI Framework | PySide6 |
| Data Processing | pandas |
| HTTP/API | requests |
| Charting | Plotly |
| Chart Host | Qt WebEngine (`QWebEngineView`) |
| Excel I/O | openpyxl |
| Packaging | PyInstaller |
| Testing | pytest |

## 🚀 Installation

```bash
git clone <your-repo-url>
cd <your-repo-folder>
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
python -m app.main
```

Alternative (installed console script):

```bash
energyflow-studio
```

## 📁 Project Structure

```text
.
├── app/
│   ├── main.py
│   ├── assets/                # Icons and static UI assets
│   ├── components/            # Reusable chart/filter helpers
│   ├── services/              # API clients, storage, processing, settings
│   ├── ui/                    # MainWindow, dialogs, tabs, graph host, design system
│   └── utils/                 # Shared utility helpers
├── tests/                     # Unit/integration tests (GUI tests are marker-based)
├── EnergyFlow Studio.spec     # PyInstaller build spec
├── pyproject.toml             # Project metadata and dependencies
└── Makefile                   # Common dev commands (test/build)
```

### Folder roles

- `app/ui`: all visual layers and dialog flows (including modal/backdrop handling).
- `app/services`: data ingestion, API communication, profile/settings persistence.
- `app/components`: reusable pieces used by UI modules.
- `app/assets`: icons used by app shell and controls.

## 🧩 Architecture

The app follows a practical layered desktop architecture:

```text
MainWindow / Dialogs / Tabs
            │
            ▼
     UI orchestration layer
            │
            ▼
 Services (API, storage, parsing, normalization)
            │
            ▼
      pandas DataFrames
            │
            ▼
  Plotly figures -> GraphView (QWebEngine)
```

Styling is centralized through shared QSS helpers and composed per dialog/tab.

## 🎨 Styling System

The project uses a structured QSS approach with reusable helpers from `app/ui/design_system.py`.

### Core principles

- Component/dialog styles are composed using `compose_styles(...)`.
- Base dialog skin is provided by `dialog_surface_qss("#DialogObjectName")`.
- Shared controls (buttons, checkboxes) are provided as reusable blocks.
- Selector scoping is used to avoid style leakage between dialogs.

### Selector patterns used in the codebase

- `#ObjectName` for dialog/widget scope:
  - `#DeviceProfilesDialog`, `#DessMonitorDialog`, `#InverterEditDialog`
- Type + object selectors:
  - `QFrame#DeviceProfileListCard`
- Dynamic properties for UI state:
  - `QFrame#DeviceProfileListCard[selected="true"]`
  - `QPushButton[selectedYear="true"]`

### Typical style composition pattern

```python
styles = [
    dialog_surface_qss("#DeviceProfilesDialog"),
    DEVICE_LIST_STYLE,
    NEON_CLOSE_BUTTON_STYLE,
    NEON_HEADER_BAR_STYLE,
]
self.setStyleSheet("\n\n".join(styles))
```

### Adding new styles safely

1. Add/extend reusable style blocks in `design_system.py` when shared.
2. Keep dialog-specific overrides near the component if truly local.
3. Prefer objectName + scoped selectors over global widget selectors.
4. If stateful visuals are needed, use dynamic properties and repolish only affected widgets.

## ▶️ Usage

Typical user flow:

1. Launch app.
2. Open **Choose Device** and select/create an active profile.
3. Run **DessMonitor Sync** to load or refresh telemetry.
4. Explore EnergyFlow dashboard cards, history dialogs, inverter analysis, and forecast tabs
5. Use saved data section for reopening historical imports.

## 🧪 Development

Run tests:

```bash
make test
```

Run GUI-marked tests in headless mode:

```bash
make test-gui
```

Build distributable app:

```bash
make build
```

### Developer notes

- Default test config excludes GUI marker unless explicitly requested.
- Plotly rendering is hosted via `QWebEngineView`; chart issues should be debugged in `app/ui/graph_view.py` and related dialog lifecycle handlers.
- For style changes, prefer updating reusable QSS blocks first, then local overrides.

## 🤝 Contributing

Contributions are welcome.

1. Create a feature branch.
2. Keep PRs focused (UI, services, styling, or infra).
3. Preserve behavior unless a change is explicitly intended.
4. Follow existing code patterns: typed Python signatures, reusable style helpers, and dialog lifecycle management
5. Run tests before opening a PR.

Recommended commit style:

- `feat(ui): add ...`
- `fix(dialog): resolve ...`
- `refactor(styles): consolidate ...`
- `test(services): cover ...`

## 🔮 Future Improvements

- Centralized theming tokens for color/radius/spacing.
- Expanded automated GUI regression coverage.
- Improved plugin-like extension points for new telemetry providers.
- Richer chart export/reporting workflows.
- Optional localization/i18n pass for all user-facing strings.

## 📄 License

No repository-level license file is currently present.

If you plan to open-source this project, add a `LICENSE` file (for example MIT/Apache-2.0) and update the badge above accordingly.
