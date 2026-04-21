# i18n audit report

- Translation keys (`uk`): 203
- Fragment keys (`uk`): 15
- UI strings detected: 213
- Covered by i18n: 49
- Missing in i18n: 164

## Missing strings by file

### app/ui/dessmonitor_dialog.py (26)

- `app/ui/dessmonitor_dialog.py:171` `setToolTip` -> `Open calendar`
- `app/ui/dessmonitor_dialog.py:237` `setText` -> `{expr} ▾`
- `app/ui/dessmonitor_dialog.py:278` `setWindowTitle` -> `Connect to DessMonitor`
- `app/ui/dessmonitor_dialog.py:304` `setPlaceholderText` -> `Optional if your account allows auth without it`
- `app/ui/dessmonitor_dialog.py:318` `QLabel` -> `Sign in to fetch devices automatically.`
- `app/ui/dessmonitor_dialog.py:322` `QPushButton` -> `Load Devices`
- `app/ui/dessmonitor_dialog.py:345` `QLabel` -> `To`
- `app/ui/dessmonitor_dialog.py:373` `setPlaceholderText` -> `Authentication log will appear here.`
- `app/ui/dessmonitor_dialog.py:389` `QLabel` -> `Sync now`
- `app/ui/dessmonitor_dialog.py:391` `QLabel` -> `Pull fresh telemetry from DessMonitor`
- `app/ui/dessmonitor_dialog.py:393` `QLabel` -> `Choose how much history to sync. Shorter periods finish faster.`
- `app/ui/dessmonitor_dialog.py:404` `QLabel` -> `Choose a sync period. Larger periods may take longer to import.`
- `app/ui/dessmonitor_dialog.py:421` `QLabel` -> `Sync period`
- `app/ui/dessmonitor_dialog.py:422` `QLabel` -> `Datasets`
- `app/ui/dessmonitor_dialog.py:423` `QLabel` -> `Selected dates`
- `app/ui/dessmonitor_dialog.py:451` `QCheckBox` -> `DessMonitor`
- `app/ui/dessmonitor_dialog.py:456` `QCheckBox` -> `Weather History`
- `app/ui/dessmonitor_dialog.py:497` `setText` -> `Sync now`
- `app/ui/dessmonitor_dialog.py:783` `setText` -> `Sync data from your saved profile`
- `app/ui/dessmonitor_dialog.py:784` `setText` -> `Choose how much history to sync, then pull the newest telemetry.`
- `app/ui/dessmonitor_dialog.py:785` `setText` -> `Profile: {expr}\nDevice: {expr}`
- `app/ui/dessmonitor_dialog.py:868` `setText` -> `Weather History will become available after saving inverter coordinates in the profile.`
- `app/ui/dessmonitor_dialog.py:1033` `setText` -> `Platform: {expr}\nDevice: {expr}`
- `app/ui/dessmonitor_dialog.py:1087` `setText` -> `Could not load devices. Check credentials and company key.`
- `app/ui/dessmonitor_dialog.py:1092` `setText` -> `Load Devices`
- `app/ui/dessmonitor_dialog.py:1110` `setText` -> `Loaded {expr} device(s). Choose one to import.`

### app/ui/device_profiles_dialog.py (35)

- `app/ui/device_profiles_dialog.py:191` `setWindowTitle` -> `Choose Location`
- `app/ui/device_profiles_dialog.py:241` `QLabel` -> `Search for a place or click directly on the map to set the inverter location.`
- `app/ui/device_profiles_dialog.py:253` `QLabel` -> `Map view is unavailable in this environment. Please enter location manually.`
- `app/ui/device_profiles_dialog.py:263` `QLabel` -> `Tip: click a point on the map or search by city, village, or full address.`
- `app/ui/device_profiles_dialog.py:271` `setText` -> `Use location`
- `app/ui/device_profiles_dialog.py:556` `QCheckBox` -> `Run every {expr} minutes`
- `app/ui/device_profiles_dialog.py:593` `setPlaceholderText` -> `30`
- `app/ui/device_profiles_dialog.py:597` `setPlaceholderText` -> `0`
- `app/ui/device_profiles_dialog.py:612` `setPlaceholderText` -> `City, region or coordinates`
- `app/ui/device_profiles_dialog.py:615` `QLabel` -> `Required for weather-based generation forecast.`
- `app/ui/device_profiles_dialog.py:638` `QLabel` -> `All available import fields will be imported automatically.`
- `app/ui/device_profiles_dialog.py:818` `QLabel` -> `Run Check to load the device list. All available import fields are added automatically.`
- `app/ui/device_profiles_dialog.py:1251` `setText` -> `Manual location from map`
- `app/ui/device_profiles_dialog.py:1261` `setText` -> `Manual location`
- `app/ui/device_profiles_dialog.py:1263` `setText` -> `Required for weather-based generation forecast. You can type a place or choose it on the map.`
- `app/ui/device_profiles_dialog.py:1265` `setText` -> `Required for weather-based generation forecast.`
- `app/ui/device_profiles_dialog.py:1353` `setPlaceholderText` -> `Latitude, longitude`
- `app/ui/device_profiles_dialog.py:1354` `setText` -> `Manual location`
- `app/ui/device_profiles_dialog.py:1358` `setText` -> `Required for weather-based generation forecast. You can type coordinates manually or choose a point on the map.`
- `app/ui/device_profiles_dialog.py:1360` `setText` -> `Required for weather-based generation forecast.`
- `app/ui/device_profiles_dialog.py:1369` `setPlaceholderText` -> `Run Check to load location from DessMonitor`
- `app/ui/device_profiles_dialog.py:1371` `setText` -> `Loaded from DessMonitor`
- `app/ui/device_profiles_dialog.py:1373` `setText` -> `Location loaded automatically from DessMonitor.`
- `app/ui/device_profiles_dialog.py:1375` `setText` -> `Location loaded automatically from DessMonitor.`
- `app/ui/device_profiles_dialog.py:1377` `setText` -> `Waiting for auto location`
- `app/ui/device_profiles_dialog.py:1378` `setText` -> `Required. If DessMonitor does not return it, switch to manual mode.`
- `app/ui/device_profiles_dialog.py:1423` `setText` -> `All available import fields will be imported automatically ({expr} loaded).`
- `app/ui/device_profiles_dialog.py:1427` `setText` -> `All available import fields will be imported automatically.`
- `app/ui/device_profiles_dialog.py:1959` `QLabel` -> `{expr}: {expr}`
- `app/ui/device_profiles_dialog.py:1961` `QLabel` -> `{expr}: {expr}`
- `app/ui/device_profiles_dialog.py:1963` `QLabel` -> `{expr}: {expr}`
- `app/ui/device_profiles_dialog.py:1986` `setWindowTitle` -> `Inverter Fields`
- `app/ui/device_profiles_dialog.py:2040` `QPushButton` -> `Import Excel`
- `app/ui/device_profiles_dialog.py:2043` `QPushButton` -> `Export Excel`
- `app/ui/device_profiles_dialog.py:2053` `setText` -> `Apply`

### app/ui/forecast_tab.py (3)

- `app/ui/forecast_tab.py:339` `setText` -> `{expr} • {expr}: {expr} • {expr}: {expr}`
- `app/ui/forecast_tab.py:362` `setText` -> `{expr} ⠋`
- `app/ui/forecast_tab.py:373` `setText` -> `{expr} {expr}`

### app/ui/inverter_analysis_dialog.py (10)

- `app/ui/inverter_analysis_dialog.py:147` `setWindowTitle` -> `Inverter Analysis - {expr}`
- `app/ui/inverter_analysis_dialog.py:916` `set_placeholder` -> `Enable at least one power series to inspect inverter behavior.`
- `app/ui/inverter_analysis_dialog.py:920` `set_placeholder` -> `No power data for the selected period.`
- `app/ui/inverter_analysis_dialog.py:945` `set_placeholder` -> `Enable at least one energy series to compare daily totals.`
- `app/ui/inverter_analysis_dialog.py:950` `set_placeholder` -> `Not enough saved history was found to calculate energy totals for this period.`
- `app/ui/inverter_analysis_dialog.py:998` `set_placeholder` -> `Enable battery power or SOC to explore storage behavior.`
- `app/ui/inverter_analysis_dialog.py:1002` `set_placeholder` -> `No battery data for the selected period.`
- `app/ui/inverter_analysis_dialog.py:1188` `setText` -> `{expr}%`
- `app/ui/inverter_analysis_dialog.py:1189` `setText` -> `{expr}%`
- `app/ui/inverter_analysis_dialog.py:1190` `setText` -> `{expr}%`

### app/ui/inverter_settings_tab.py (13)

- `app/ui/inverter_settings_tab.py:72` `QLabel` -> `Inverter Settings`
- `app/ui/inverter_settings_tab.py:78` `QPushButton` -> `Expand all`
- `app/ui/inverter_settings_tab.py:81` `QPushButton` -> `Collapse all`
- `app/ui/inverter_settings_tab.py:92` `QLabel` -> `Choose an active DessMonitor profile to load inverter settings.`
- `app/ui/inverter_settings_tab.py:97` `QLabel` -> `Ready to load.`
- `app/ui/inverter_settings_tab.py:102` `setPlaceholderText` -> `Search by parameter name, value, or category`
- `app/ui/inverter_settings_tab.py:151` `setText` -> `Choose an active DessMonitor profile to load inverter settings.`
- `app/ui/inverter_settings_tab.py:212` `setText` -> `Refreshing ⠋`
- `app/ui/inverter_settings_tab.py:223` `setText` -> `Refreshing {expr}`
- `app/ui/inverter_settings_tab.py:701` `setWindowTitle` -> `Edit parameter`
- `app/ui/inverter_settings_tab.py:749` `setPlaceholderText` -> `Enter value`
- `app/ui/inverter_settings_tab.py:762` `QLabel` -> `Saving is temporarily disabled at this stage.`
- `app/ui/inverter_settings_tab.py:803` `setText` -> `Value will be saved to the local cache.`

### app/ui/main_window.py (48)

- `app/ui/main_window.py:676` `QLabel` -> `Active Profile`
- `app/ui/main_window.py:679` `QLabel` -> `Profile`
- `app/ui/main_window.py:868` `QLabel` -> `Step 1 of 5`
- `app/ui/main_window.py:870` `QLabel` -> `Preparing import`
- `app/ui/main_window.py:872` `QLabel` -> `Connecting to the selected source.`
- `app/ui/main_window.py:885` `QPushButton` -> `View log`
- `app/ui/main_window.py:969` `setText` -> `Step 1 of 5`
- `app/ui/main_window.py:971` `setText` -> `Connecting to the selected source.`
- `app/ui/main_window.py:977` `setText` -> `View log`
- `app/ui/main_window.py:995` `setText` -> `Import stopped`
- `app/ui/main_window.py:996` `setText` -> `Import failed`
- `app/ui/main_window.py:1001` `setText` -> `Hide log`
- `app/ui/main_window.py:1391` `showMessage` -> `Stopping background requests before closing...`
- `app/ui/main_window.py:2018` `setText` -> `Refreshing ⠋`
- `app/ui/main_window.py:2031` `setText` -> `Refreshing {expr}`
- `app/ui/main_window.py:2060` `setText` -> `Refresh {expr}:{expr}`
- `app/ui/main_window.py:2127` `QLabel` -> `● {expr}`
- `app/ui/main_window.py:2154` `QLabel` -> `{expr}: {expr}\n{expr}: {expr}`
- `app/ui/main_window.py:2162` `QLabel` -> `{expr}: {expr}`
- `app/ui/main_window.py:2328` `QLabel` -> `{expr}: {expr}`
- `app/ui/main_window.py:2853` `QLabel` -> `Tuya Devices`
- `app/ui/main_window.py:3625` `showMessage` -> `Active API device: {expr}`
- `app/ui/main_window.py:4121` `showMessage` -> `PV forecast updated.`
- `app/ui/main_window.py:4132` `showMessage` -> `PV forecast loaded from cache (live API unavailable).`
- `app/ui/main_window.py:4135` `showMessage` -> `PV forecast unavailable.`
- `app/ui/main_window.py:4361` `showMessage` -> `EnergyFlow refresh is already in progress.`
- `app/ui/main_window.py:4376` `showMessage` -> `Refreshing current EnergyFlow snapshot...`
- `app/ui/main_window.py:4708` `setText` -> `Live snapshot unavailable.`
- `app/ui/main_window.py:4723` `showMessage` -> `Profile is active. EnergyFlow timed out; background refresh will continue.`
- `app/ui/main_window.py:5199` `showMessage` -> `Could not refresh current value before edit: {expr}`
- `app/ui/main_window.py:5235` `showMessage` -> `Cached local value for {expr}.`
- `app/ui/main_window.py:5336` `showMessage` -> `Inverter settings refresh failed: {expr}`
- `app/ui/main_window.py:5426` `setText` -> `Refreshing ⠋`
- `app/ui/main_window.py:5451` `setText` -> `Refreshing {expr}`
- `app/ui/main_window.py:5539` `setText` -> `{expr} {expr}`
- `app/ui/main_window.py:5641` `showMessage` -> `Updating today actual PV values for forecast...`
- `app/ui/main_window.py:6123` `showMessage` -> `Auto-sync started for {expr}`
- `app/ui/main_window.py:6137` `showMessage` -> `Auto-sync skipped while another import is running.`
- `app/ui/main_window.py:6208` `showMessage` -> `Auto-sync failed.`
- `app/ui/main_window.py:6240` `showMessage` -> `Auto-sync completed at {expr}`
- `app/ui/main_window.py:6246` `showMessage` -> `Auto-sync failed.`
- `app/ui/main_window.py:6336` `showMessage` -> `Failed to load data.`
- `app/ui/main_window.py:6437` `showMessage` -> `Saved dataset exported to {expr}`
- `app/ui/main_window.py:6449` `showMessage` -> `The local saved-data database was cleared.`
- `app/ui/main_window.py:7330` `set_placeholder` -> `Select at least one series to display the chart.`
- `app/ui/main_window.py:7341` `set_placeholder` -> `No rows match the selected date range.`
- `app/ui/main_window.py:7370` `showMessage` -> `Chart exported to {expr}`
- `app/ui/main_window.py:7389` `showMessage` -> `Exported: {expr}`

### app/ui/pv_history_dialog.py (16)

- `app/ui/pv_history_dialog.py:239` `setText` -> `{expr} ▾`
- `app/ui/pv_history_dialog.py:343` `QLabel` -> `Select year`
- `app/ui/pv_history_dialog.py:426` `setWindowTitle` -> `{expr} History - {expr}`
- `app/ui/pv_history_dialog.py:750` `QLabel` -> `{expr}: --`
- `app/ui/pv_history_dialog.py:906` `setText` -> `\n            <div style="line-height:1.25;">\n                <div style="font-size:24px; font-weight:700; color:#f8fafc;">\n                    {expr}: {expr}\n                </div>\n                {expr}\n            </div>\n            `
- `app/ui/pv_history_dialog.py:1073` `set_placeholder` -> `Forecast is unavailable for this future day.`
- `app/ui/pv_history_dialog.py:1080` `set_placeholder` -> `Forecast source is unavailable for this future day.`
- `app/ui/pv_history_dialog.py:1084` `set_placeholder` -> `No {expr} samples for the selected day.`
- `app/ui/pv_history_dialog.py:1133` `set_placeholder` -> `No {expr} {expr} for the selected month.`
- `app/ui/pv_history_dialog.py:1212` `set_placeholder` -> `No {expr} {expr} for the selected year.`
- `app/ui/pv_history_dialog.py:1291` `set_placeholder` -> `No {expr} history available.`
- `app/ui/pv_history_dialog.py:1711` `set_placeholder` -> `Forecast is unavailable for this future day.`
- `app/ui/pv_history_dialog.py:1750` `set_placeholder` -> `No {expr} samples for the selected month.`
- `app/ui/pv_history_dialog.py:1778` `set_placeholder` -> `No {expr} samples for the selected year.`
- `app/ui/pv_history_dialog.py:1792` `set_placeholder` -> `No {expr} history available.`
- `app/ui/pv_history_dialog.py:1812` `set_placeholder` -> `No {expr} history available.`

### app/ui/saved_data_section.py (5)

- `app/ui/saved_data_section.py:230` `setText` -> `{expr} ⠋`
- `app/ui/saved_data_section.py:241` `setText` -> `{expr} {expr}`
- `app/ui/saved_data_section.py:290` `setText` -> `{expr}: {expr} ({expr} {expr})`
- `app/ui/saved_data_section.py:373` `setText` -> `{expr} {expr} {expr} {expr}  |  {expr} {expr}-{expr} {expr} {expr}`
- `app/ui/saved_data_section.py:506` `setText` -> `{expr}: {expr}`

### app/ui/settings_dialog.py (5)

- `app/ui/settings_dialog.py:45` `setWindowTitle` -> `Settings`
- `app/ui/settings_dialog.py:52` `QLabel` -> `Configure how DessMonitor imports work. By default the app imports all available parameter keys for the selected device.`
- `app/ui/settings_dialog.py:58` `QCheckBox` -> `Import all available DessMonitor parameter keys`
- `app/ui/settings_dialog.py:63` `setPlaceholderText` -> `Enter one parameter key per line, for example:\nPV_OUTPUT_POWER\nGRID_ACTIVE_POWER`
- `app/ui/settings_dialog.py:69` `QLabel` -> `These custom keys are used only when 'Import all available...' is turned off.`

### app/ui/sidebar.py (3)

- `app/ui/sidebar.py:16` `QLabel` -> `Select series to display`
- `app/ui/sidebar.py:20` `QLabel` -> `0 selected`
- `app/ui/sidebar.py:77` `setText` -> `{expr} {expr} selected`
