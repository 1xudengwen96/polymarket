"""交易日程测试：每日止盈目标 / 北京时间交易时段 / 进出场价（Dashboard 设置持久化 + 校验）。"""

from __future__ import annotations

import os
import tempfile

import pytest

from pm5hft.dashboard import _Db, get_controls, save_controls

SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_settings (
  setting_key VARCHAR(64) PRIMARY KEY,
  value VARCHAR(128) NOT NULL,
  updated_ts_ms BIGINT NOT NULL
)
"""


@pytest.fixture
def db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = _Db(path)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_get_controls_defaults(db_path: str) -> None:
    controls = get_controls(_Db(db_path))
    assert controls["auto_trading_enabled"] is True
    assert controls["fixed_order_notional"] == "5"
    assert controls["daily_profit_target"] == "0"
    assert controls["tail_entry_price"] == "0.98"
    assert controls["tail_exit_price"] == "0"
    assert controls["trading_hours_enabled"] is False
    assert controls["trading_hours_start_bt"] == 9
    assert controls["trading_hours_end_bt"] == 21


def test_save_controls_roundtrip(db_path: str) -> None:
    controls = save_controls(db_path, {
        "auto_trading_enabled": True,
        "fixed_order_notional": 7.5,
        "daily_profit_target": 12.0,
        "tail_entry_price": 0.80,
        "tail_exit_price": 0.99,
        "trading_hours_enabled": True,
        "trading_hours_start_bt": 22,
        "trading_hours_end_bt": 6,
    })
    assert controls["fixed_order_notional"] == "7.5"
    assert controls["daily_profit_target"] == "12"
    assert controls["tail_entry_price"] == "0.8"
    assert controls["tail_exit_price"] == "0.99"
    assert controls["trading_hours_enabled"] is True
    assert controls["trading_hours_start_bt"] == 22
    assert controls["trading_hours_end_bt"] == 6
    assert get_controls(_Db(db_path)) == controls


def test_save_controls_validation(db_path: str) -> None:
    base = {
        "auto_trading_enabled": True,
        "fixed_order_notional": 5,
        "daily_profit_target": 0,
        "tail_entry_price": 0.98,
        "tail_exit_price": 0,
        "trading_hours_enabled": False,
        "trading_hours_start_bt": 9,
        "trading_hours_end_bt": 21,
    }
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "daily_profit_target": -1})
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "tail_entry_price": 0.49})
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "tail_entry_price": 1.0})
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "tail_exit_price": 1.0})
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "tail_exit_price": 0.98})   # 出场须高于进场
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "trading_hours_start_bt": 24})
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "trading_hours_end_bt": -1})
    with pytest.raises(ValueError):
        save_controls(db_path, {**base, "fixed_order_notional": 101})
    # 跨夜时段（22→6）合法
    ok = save_controls(db_path, {**base, "trading_hours_enabled": True,
                                 "trading_hours_start_bt": 22, "trading_hours_end_bt": 6})
    assert ok["trading_hours_start_bt"] == 22 and ok["trading_hours_end_bt"] == 6
