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
            return json.loads(value)

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


config_manager = ConfigManager()
