import json
import os
import logging
import threading

logger = logging.getLogger(__name__)

_VIXSRC_WARP_EXCLUDES = {
    "vixcloud.cc",
    "*.vixcloud.cc",
    "vixsrc.to",
    "*.vixsrc.to",
    "*.vix-content.net",
}

# Docker keeps its persistent volume at /data.  Native Windows runs should
# keep the same layout inside the EasyProxy checkout instead of writing to
# the drive root (C:\data).
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG_DIR = (
    os.path.join(_PROJECT_DIR, "data") if os.name == "nt" else "/data"
)
_CONFIG_DIR = os.environ.get("CONFIG_DIR") or _DEFAULT_CONFIG_DIR
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")
DEFAULT_RECORDINGS_DIR = os.path.join(_CONFIG_DIR, "recordings")

DEFAULT_CONFIG = {
    "enable_warp": False,
    "warp_license_key": "",
    "warp_exclude_domains": [
        "strem.fun", "*.strem.fun", "torrentio.strem.fun",
        "real-debrid.com", "*.real-debrid.com", "realdebrid.com",
        "*.realdebrid.com", "api.real-debrid.com",
        "premiumize.me", "*.premiumize.me", "www.premiumize.me",
        "alldebrid.com", "*.alldebrid.com", "api.alldebrid.com",
        "debrid-link.com", "*.debrid-link.com", "debridlink.com",
        "*.debridlink.com", "api.debrid-link.com",
        "torbox.app", "*.torbox.app", "api.torbox.app",
        "offcloud.com", "*.offcloud.com", "api.offcloud.com",
        "put.io", "*.put.io", "api.put.io",
    ],
    "warp_exclude_domains_custom": [],
    "global_proxies": [],
    "transport_routes": [],
    "extractor_proxies": {},
    "warp_off_extractors": [],
    "proxy_off_extractors": [],
    "proxy_exclude_domains": [],
    "dvr_enabled": False,
    "recordings_dir": DEFAULT_RECORDINGS_DIR,
    "max_recording_duration": 28800,
    "recordings_retention_days": 7,
    "proxy_test_timeout": 10,
    "proxy_test_concurrency": None,
    "log_level": "WARNING",
}

_lock = threading.Lock()
_config_data = None


def _remove_vixsrc_warp_excludes(config):
    changed = False
    for key in ("warp_exclude_domains", "warp_exclude_domains_custom"):
        values = config.get(key, [])
        if not isinstance(values, list):
            continue
        filtered = [
            value for value in values
            if str(value).strip().lower() not in _VIXSRC_WARP_EXCLUDES
        ]
        if filtered != values:
            config[key] = filtered
            changed = True
    return changed


def _load():
    global _config_data
    os.makedirs(_CONFIG_DIR, exist_ok=True)
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r") as f:
                data = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            # ponytail: merge default list keys to ensure mandatory exclusions are always present
            for list_key in ["warp_exclude_domains", "warp_off_extractors", "proxy_off_extractors"]:
                if list_key in data and list_key in DEFAULT_CONFIG:
                    combined = list(DEFAULT_CONFIG[list_key])
                    for item in data[list_key]:
                        if item not in combined:
                            combined.append(item)
                    merged[list_key] = combined
            config_changed = _remove_vixsrc_warp_excludes(merged)
            _config_data = merged
            if config_changed:
                _save()
            logger.debug("Loaded config from %s", _CONFIG_FILE)
            return
        except Exception as e:
            logger.warning("Failed to load config.json: %s", e)
    _config_data = dict(DEFAULT_CONFIG)
    _remove_vixsrc_warp_excludes(_config_data)
    _save()


def _save():
    if _config_data is None:
        return
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_CONFIG_FILE, "w") as f:
            json.dump(_config_data, f, indent=2)
    except Exception as e:
        logger.error("Failed to save config.json: %s", e)


def get(key, default=None):
    if _config_data is None:
        _load()
    with _lock:
        return _config_data.get(key, default)


def set(key, value):
    if _config_data is None:
        _load()
    with _lock:
        _config_data[key] = value
    _save()


def get_all():
    if _config_data is None:
        _load()
    with _lock:
        return dict(_config_data)


def update(values: dict):
    if _config_data is None:
        _load()
    with _lock:
        _config_data.update(values)
    _save()


def replace_all(data: dict):
    """Replace entire config with new data (merged with defaults)."""
    global _config_data
    if _config_data is None:
        _load()
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    _remove_vixsrc_warp_excludes(merged)
    with _lock:
        _config_data = merged
    _save()

def delete(key):
    if _config_data is None:
        _load()
    with _lock:
        _config_data.pop(key, None)
    _save()


_load()
