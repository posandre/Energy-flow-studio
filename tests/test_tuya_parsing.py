from __future__ import annotations

from app.services.tuya_api import TuyaDevice
from app.ui.main_window import MainWindow


class _TuyaParsingHarness:
    _parse_tuya_measurements = MainWindow._parse_tuya_measurements
    _parse_power_watts = MainWindow._parse_power_watts
    _parse_current_amps = MainWindow._parse_current_amps
    _parse_tuya_status = MainWindow._parse_tuya_status
    _parse_status_power_watts = MainWindow._parse_status_power_watts
    _extract_power_from_text = MainWindow._extract_power_from_text
    _extract_status_power_from_text = MainWindow._extract_status_power_from_text
    _tuya_device_power_watts = MainWindow._tuya_device_power_watts


def test_parse_tuya_measurements_keeps_raw_value_for_math() -> None:
    harness = _TuyaParsingHarness()
    pairs = harness._parse_tuya_measurements("Power: 1470 W, Current: 7 A")
    assert ("Power", "1470 W") in pairs
    assert ("Current", "7 A") in pairs


def test_parse_power_watts_supports_english_and_ukrainian_units() -> None:
    harness = _TuyaParsingHarness()
    assert harness._parse_power_watts("1470 W") == 1470.0
    assert harness._parse_power_watts("1470 Вт") == 1470.0
    assert harness._parse_power_watts("1.47 kW") == 1470.0
    assert harness._parse_power_watts("1,47 кВт") == 1470.0


def test_parse_current_amps_supports_english_and_ukrainian_units() -> None:
    harness = _TuyaParsingHarness()
    assert harness._parse_current_amps("7 A") == 7.0
    assert harness._parse_current_amps("7 А") == 7.0
    assert harness._parse_current_amps("6562 mA") == 6.562
    assert harness._parse_current_amps("6562 мА") == 6.562


def test_tuya_device_power_watts_reads_localized_power_measurement() -> None:
    harness = _TuyaParsingHarness()
    device = TuyaDevice(
        device_id="dev1",
        name="Boiler",
        device_name="Boiler",
        icon_url="",
        online=True,
        status_text="switch_1=True",
        measurements_text="Energy: 0 кВт*год, Current: 7 А, Power: 1470 Вт, Voltage: 223 В",
        power_on=True,
    )
    assert harness._tuya_device_power_watts(device) == 1470.0
