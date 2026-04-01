import json
import os
from cryptography.fernet import Fernet
from sqlalchemy import select
from database import async_session
from models import Config, Module


class ConfigManager:
    def __init__(self):
        self._fernet = None

    async def _get_fernet(self) -> Fernet:
        if self._fernet:
            return self._fernet
        key_path = os.path.join(os.path.dirname(os.environ.get("DATABASE_PATH", "/app/data/mediaassistant.db")), ".secret_key")
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            with open(key_path, "wb") as f:
                f.write(key)
        self._fernet = Fernet(key)
        return self._fernet

    async def get(self, key: str, default=None):
        async with async_session() as session:
            result = await session.get(Config, key)
            if not result:
                return default
            value = result.value
            if result.encrypted:
                fernet = await self._get_fernet()
                value = fernet.decrypt(value.encode()).decode()
            try:
                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return value

    async def set(self, key: str, value, encrypted: bool = False):
        serialized = json.dumps(value)
        if encrypted:
            fernet = await self._get_fernet()
            serialized = fernet.encrypt(serialized.encode()).decode()
        async with async_session() as session:
            existing = await session.get(Config, key)
            if existing:
                existing.value = serialized
                existing.encrypted = encrypted
            else:
                session.add(Config(key=key, value=serialized, encrypted=encrypted))
            await session.commit()

    async def is_setup_complete(self) -> bool:
        return await self.get("setup_complete", False)

    async def is_module_enabled(self, name: str) -> bool:
        async with async_session() as session:
            module = await session.get(Module, name)
            return module.enabled if module else False

    async def set_module_enabled(self, name: str, enabled: bool):
        async with async_session() as session:
            module = await session.get(Module, name)
            if module:
                module.enabled = enabled
                await session.commit()

    def get_env(self, key: str, default: str = "") -> str:
        return os.environ.get(key, default)

    async def seed_from_env(self):
        """Write ENV variables into DB if set. Existing DB values are not overwritten."""
        env_map = {
            "AI_BACKEND_URL": ("ai.backend_url", False),
            "AI_MODEL": ("ai.model", False),
            "AI_API_KEY": ("ai.api_key", True),
            "SMTP_SERVER": ("smtp.server", False),
            "SMTP_PORT": ("smtp.port", False),
            "SMTP_SSL": ("smtp.ssl", False),
            "SMTP_USER": ("smtp.user", False),
            "SMTP_PASSWORD": ("smtp.password", True),
            "SMTP_RECIPIENT": ("smtp.recipient", False),
            "GEO_PROVIDER": ("geo.provider", False),
            "GEO_URL": ("geo.url", False),
            "GEO_API_KEY": ("geo.api_key", True),
            "LIBRARY_BASE_PATH": ("library.base_path", False),
            "IMMICH_URL": ("immich.url", False),
            "IMMICH_API_KEY": ("immich.api_key", True),
            "UI_LANGUAGE": ("ui.language", False),
            "UI_THEME": ("ui.theme", False),
            "FILEWATCHER_INTERVAL": ("filewatcher.interval", False),
            "FILEWATCHER_SCHEDULE_MODE": ("filewatcher.schedule_mode", False),
            "OCR_MODE": ("ocr.mode", False),
            "PHASH_THRESHOLD": ("duplikat.phash_threshold", False),
            "SETUP_COMPLETE": ("setup_complete", False),
        }
        for env_key, (config_key, encrypted) in env_map.items():
            env_value = os.environ.get(env_key)
            if env_value is None or env_value == "":
                continue
            # Convert types
            if config_key in ("smtp.port", "filewatcher.interval", "duplikat.phash_threshold"):
                env_value = int(env_value)
            elif config_key in ("smtp.ssl",):
                env_value = env_value.lower() in ("true", "1", "yes")
            elif config_key == "setup_complete":
                env_value = env_value.lower() in ("true", "1", "yes")
                if not env_value:
                    continue
            # Only seed if not already in DB
            existing = await self.get(config_key)
            if existing is None:
                await self.set(config_key, env_value, encrypted=encrypted)


config_manager = ConfigManager()
