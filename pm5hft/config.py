"""配置加载：yaml 文件 + 环境变量覆盖（pydantic-settings）。

层级：代码默认值 < config/*.yaml < 环境变量。
所有敏感项只允许来自环境变量。

frozen（PyInstaller exe）模式：config/artifacts/.env 都放在 exe 同目录，
ROOT 指向 exe 所在目录；开发模式 ROOT = 项目根目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if getattr(sys, "frozen", False):  # PyInstaller 打包后
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def default_db_url() -> str:
    """默认 SQLite 路径：frozen 模式放 exe 同目录 data/，开发模式保持 ./data。"""
    if getattr(sys, "frozen", False):
        p = (ROOT / "data" / "pm5hft.db").resolve().as_posix()
        # Unix 绝对路径需 4 斜杠（sqlite+aiosqlite:////home/...），Windows 盘符 3 斜杠（///C:/...）
        prefix = "sqlite+aiosqlite:///" + ("/" if p.startswith("/") else "")
        return prefix + p
    return "sqlite+aiosqlite:///./data/pm5hft.db"


def resolve_run_path(path: str) -> str:
    """相对路径解析到 ROOT（frozen=exe 目录 / 开发=项目根）；绝对路径原样返回。"""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(ROOT / p)


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config/{name} must contain a mapping at top level")
    return data


class AssetConfig(BaseModel):
    enabled: bool = True
    asset: str
    series_slug: str
    tf_label: str
    duration_s: int
    slug_template: str
    twap_symbol: str
    twap_lookback_default: int
    binance_symbol: str
    rtds_binance_symbol: str
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"


class AssetsConfig(BaseModel):
    assets: dict[str, AssetConfig]


class RiskConfig(BaseModel):
    account_equity: str = "auto"
    max_initial_exposure: float = 50.0
    max_unhedged_exposure_pct: float = 0.01
    max_complete_set_exposure_pct: float = 0.05
    max_market_exposure_pct: float = 0.10
    max_window_notional: float = 200.0
    max_daily_loss_pct: float = 0.010
    max_hourly_loss_pct: float = 0.005
    # 每资产并发敞口（占权益比）：组合未对冲总上限 = max(max_unhedged_exposure_pct,
    # unhedged_exposure_per_asset_pct × 启用资产数)。随资产数自动扩展。
    unhedged_exposure_per_asset_pct: float = 0.005
    max_consecutive_losses: int = 6
    cooloff_windows: int = 12
    max_holding_time_s: int = 330
    max_drawdown_pct: float = 0.03
    paper_starting_equity: float = 10000.0
    breakers: dict[str, Any] = Field(default_factory=dict)


class LiveConfig(BaseModel):
    allow_live: bool = False


class Settings(BaseSettings):
    """进程级设置：env 覆盖 yaml。"""

    model_config = SettingsConfigDict(
        env_prefix="PM5HFT_",
        env_file=str(ROOT / ".env"),  # frozen 模式 = exe 同目录 .env；不存在则忽略
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mode: str = "paper"  # paper | live
    live: bool = False
    db_url: str = Field(default_factory=default_db_url)
    log_level: str = "INFO"
    assets_list: str = "btc"  # PM5HFT_ASSETS env（逗号分隔）

    @property
    def assets(self) -> list[str]:
        return [a.strip().lower() for a in self.assets_list.split(",") if a.strip()]


class Config:
    """聚合配置。"""

    def __init__(self) -> None:
        self.settings = Settings()
        self.assets_cfg = AssetsConfig(assets=_load_yaml("assets.yaml")["assets"])
        self.strategy: dict[str, Any] = _load_yaml("strategy.yaml")
        self.risk = RiskConfig(**_load_yaml("risk.yaml"))
        self.execution: dict[str, Any] = _load_yaml("execution.yaml")
        self.probability: dict[str, Any] = _load_yaml("probability.yaml")
        self.live = LiveConfig(**_load_yaml("live.yaml"))
        self._validate()

    def _validate(self) -> None:
        unknown = [a for a in self.settings.assets if a not in self.assets_cfg.assets]
        if unknown:
            raise ValueError(f"unknown assets in PM5HFT_ASSETS: {unknown}")
        if self.settings.mode == "live":
            if not self.settings.live:
                raise ValueError("mode=live requires PM5HFT_LIVE=true")
            if not self.live.allow_live:
                raise ValueError("LIVE 需要人工开启 config/live.yaml 的 allow_live: true")
            if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
                raise ValueError("LIVE 需要 POLYMARKET_PRIVATE_KEY 环境变量")

    # ── 便捷访问 ──────────────────────────────────────────────
    def asset(self, name: str) -> AssetConfig:
        return self.assets_cfg.assets[name]

    def enabled_assets(self) -> dict[str, AssetConfig]:
        return {k: v for k, v in self.assets_cfg.assets.items() if v.enabled}

    def s(self, path: str, default: Any = None) -> Any:
        """strategy.yaml 点路径取值。"""
        node: Any = self.strategy
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def e(self, path: str, default: Any = None) -> Any:
        """execution.yaml 点路径取值。"""
        node: Any = self.execution
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def live_risk(self) -> dict[str, Any]:
        """实盘小资金风控档（config/live_risk.yaml）；缺失时返回空（用默认限额）。"""
        try:
            return _load_yaml("live_risk.yaml")
        except FileNotFoundError:
            return {}

    def p(self, path: str, default: Any = None) -> Any:
        """probability.yaml 点路径取值。"""
        node: Any = self.probability
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def sanitized_summary(self) -> dict[str, Any]:
        """脱敏配置快照（日志用）。"""
        return {
            "mode": self.settings.mode,
            "db_url_scheme": self.settings.db_url.split("://")[0],
            "assets": self.settings.assets,
            "live_allowed": self.live.allow_live,
            "risk": self.risk.model_dump(),
            "execution": self.execution,
            "strategy": self.strategy,
        }
