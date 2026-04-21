from app.services.energy_flow import _derive_power_breakdown_from_last_data


def test_direct_breakdown_does_not_infer_grid_battery_charge_without_ac_charge_current() -> None:
    breakdown = _derive_power_breakdown_from_last_data(
        pv_power=282.0,
        battery_power=409.0,
        battery_voltage=52.1,
        pv_voltage=215.3,
        pv_current=1.31,
        pv_charge_current=5.41,
        grid_charge_current=0.0,
        grid_voltage=240.2,
        load_voltage=239.9,
        load_power=316.0,
        grid_power=443.0,
    )

    assert breakdown is not None
    (
        pv_to_battery_power,
        pv_to_home_power,
        grid_to_battery_power,
        grid_to_home_power,
        _grid_to_home_current,
        battery_to_home_power,
    ) = breakdown

    assert round(pv_to_battery_power or 0.0) == 282
    assert round(pv_to_home_power or 0.0) == 0
    assert round(grid_to_battery_power or 0.0) == 0
    assert round(grid_to_home_power or 0.0) == 316
    assert round(battery_to_home_power or 0.0) == 0


def test_direct_breakdown_keeps_home_balance_when_battery_discharges() -> None:
    breakdown = _derive_power_breakdown_from_last_data(
        pv_power=250.0,
        battery_power=-120.0,
        battery_voltage=50.0,
        pv_voltage=200.0,
        pv_current=1.25,
        pv_charge_current=0.0,
        grid_charge_current=0.0,
        grid_voltage=230.0,
        load_voltage=230.0,
        load_power=400.0,
        grid_power=30.0,
    )

    assert breakdown is not None
    (
        pv_to_battery_power,
        pv_to_home_power,
        grid_to_battery_power,
        grid_to_home_power,
        _grid_to_home_current,
        battery_to_home_power,
    ) = breakdown

    assert round(pv_to_battery_power or 0.0) == 0
    assert round(grid_to_battery_power or 0.0) == 0
    assert round(battery_to_home_power or 0.0) == 120
    assert round((pv_to_home_power or 0.0) + (grid_to_home_power or 0.0) + (battery_to_home_power or 0.0)) == 400
