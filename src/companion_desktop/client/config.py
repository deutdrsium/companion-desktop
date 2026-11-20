"""Local, git-ignored client configuration.

Lookup order: ``$COMPANION_DESKTOP_CONFIG``, ``<repo>/config/client.json``,
``%LOCALAPPDATA%/companion-desktop/client.json``. ``config/client.example.json`` documents
every field with placeholder values.
"""

import copy
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

DEFAULTS = {
    "title": "桌面陪伴",
    "character": {"name": "小伴", "glyph": "", "avatar_image": "", "voice_font": ""},
    "server": {"base_url": "", "ssh_target": "", "remote_port": 6185, "ssh_options": [],
               "api_path": "", "chat_path": "/api/v1/chat"},
    "account": {"username": "", "session_id": "", "keyring_service": "CompanionDesktop"},
    "subtitle": {"port": 18676, "token_file": ""},
    "instance_mutex": "Local\\CompanionDesktop",
    "helpers": [],
    "rhythm_command": None,
    "voice_key_command": None,
    "data_dir": "",
}


class ConfigError(RuntimeError):
    """Raised with a plain-language reason shown on the launch card."""


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def candidates():
    env = os.environ.get("COMPANION_DESKTOP_CONFIG")
    if env:
        yield Path(env)
    yield REPO_ROOT / "config" / "client.json"
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "companion-desktop" / "client.json"


def load(path=None):
    paths = [Path(path)] if path else list(candidates())
    for p in paths:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ConfigError("本机设置文件读不出来，请检查 JSON 格式。") from error
            cfg = _merge(DEFAULTS, data)
            cfg["_path"] = str(p)
            return cfg
    raise ConfigError("还没有本机设置。请把 config/client.example.json 复制为 config/client.json 并填写。")


def validate(cfg):
    server, account = cfg["server"], cfg["account"]
    if not server.get("api_path"):
        raise ConfigError("本机设置缺少 server.api_path。")
    if not (server.get("base_url") or server.get("ssh_target")):
        raise ConfigError("本机设置缺少服务器地址（server.base_url 或 server.ssh_target）。")
    if not account.get("username") or not account.get("session_id"):
        raise ConfigError("本机设置缺少桌面账号（account.username / account.session_id）。")
    return cfg


def data_dir(cfg):
    d = cfg.get("data_dir") or os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()), "companion-desktop")
    path = Path(d)
    path.mkdir(parents=True, exist_ok=True)
    return path


def api_key(cfg):
    import keyring
    key = keyring.get_password(cfg["account"]["keyring_service"], cfg["account"]["username"])
    if not key:
        raise ConfigError("桌面连接尚未完成设置：凭据管理器里没有连接密钥。")
    return key
