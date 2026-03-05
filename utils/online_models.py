"""在线模型配置管理（持久化到项目根目录 .env）。"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def _read_env_map() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_PATH.exists():
        return env

    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        value = v.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def _write_env_map(env: dict[str, str]):
    lines = [f"{k}={v}" for k, v in sorted(env.items(), key=lambda kv: kv[0])]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_profiles() -> tuple[list[dict], str]:
    env = _read_env_map()
    profiles: list[dict] = []

    count = int(env.get("ONLINE_MODEL_PROFILE_COUNT", "0") or "0")
    for idx in range(1, count + 1):
        prefix = f"ONLINE_MODEL_PROFILE_{idx}_"
        name = env.get(prefix + "NAME", "").strip()
        base_url = env.get(prefix + "BASE_URL", "").strip()
        api_key = env.get(prefix + "API_KEY", "").strip()
        default_model = env.get(prefix + "DEFAULT_MODEL", "").strip()
        models_json = env.get(prefix + "MODEL_LIST_JSON", "[]").strip()
        try:
            models = json.loads(models_json) if models_json else []
            if not isinstance(models, list):
                models = []
        except Exception:
            models = []

        if not name:
            continue
        profiles.append(
            {
                "name": name,
                "base_url": base_url,
                "api_key": api_key,
                "default_model": default_model,
                "models": [str(x) for x in models if str(x).strip()],
            }
        )

    if not profiles:
        # 无配置时初始化默认配置组
        base_url = "https://api.siliconflow.cn/v1"
        api_key = ""
        model = "Pro/moonshotai/Kimi-K2.5"
        profiles = [
            {
                "name": "default",
                "base_url": base_url,
                "api_key": api_key,
                "default_model": model,
                "models": [model] if model else [],
            }
        ]

    active = env.get("ONLINE_MODEL_ACTIVE_PROFILE", "").strip()
    if not active or active not in {p["name"] for p in profiles}:
        active = profiles[0]["name"]
    return profiles, active


def save_profiles(profiles: list[dict], active_profile: str | None = None):
    env = _read_env_map()

    # 清理旧键
    to_delete = [k for k in env if k.startswith("ONLINE_MODEL_PROFILE_") or k == "ONLINE_MODEL_ACTIVE_PROFILE"]
    for k in to_delete:
        env.pop(k, None)

    clean_profiles: list[dict] = []
    seen_names: set[str] = set()
    for p in profiles:
        name = str(p.get("name", "")).strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        base_url = str(p.get("base_url", "")).strip()
        api_key = str(p.get("api_key", "")).strip()
        default_model = str(p.get("default_model", "")).strip()
        models = p.get("models", [])
        if not isinstance(models, list):
            models = []
        models = [str(x).strip() for x in models if str(x).strip()]
        if default_model and default_model not in models:
            models.insert(0, default_model)
        if not default_model and models:
            default_model = models[0]

        clean_profiles.append(
            {
                "name": name,
                "base_url": base_url,
                "api_key": api_key,
                "default_model": default_model,
                "models": models,
            }
        )

    if not clean_profiles:
        clean_profiles = [
            {
                "name": "default",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "",
                "default_model": "",
                "models": [],
            }
        ]

    env["ONLINE_MODEL_PROFILE_COUNT"] = str(len(clean_profiles))
    for idx, p in enumerate(clean_profiles, start=1):
        prefix = f"ONLINE_MODEL_PROFILE_{idx}_"
        env[prefix + "NAME"] = p["name"]
        env[prefix + "BASE_URL"] = p["base_url"]
        env[prefix + "API_KEY"] = p["api_key"]
        env[prefix + "DEFAULT_MODEL"] = p["default_model"]
        env[prefix + "MODEL_LIST_JSON"] = json.dumps(p["models"], ensure_ascii=False)

    names = {p["name"] for p in clean_profiles}
    chosen = active_profile if active_profile in names else clean_profiles[0]["name"]
    env["ONLINE_MODEL_ACTIVE_PROFILE"] = chosen

    # 清理已废弃的重复配置键
    env.pop("SILICONFLOW_BASE_URL", None)
    env.pop("SILICONFLOW_API_KEY", None)
    env.pop("SILICONFLOW_MODEL", None)

    _write_env_map(env)


def upsert_profile(profiles: list[dict], profile: dict) -> list[dict]:
    name = str(profile.get("name", "")).strip()
    if not name:
        return profiles

    updated = False
    result: list[dict] = []
    for p in profiles:
        if p.get("name") == name:
            result.append(profile)
            updated = True
        else:
            result.append(p)

    if not updated:
        result.append(profile)
    return result


def delete_profile(profiles: list[dict], name: str) -> list[dict]:
    name = (name or "").strip()
    result = [p for p in profiles if p.get("name") != name]
    return result
