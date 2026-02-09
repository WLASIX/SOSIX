"""
Configuration loader module.
Loads all settings from config.json and environment variables.
Supports .env file for local development.
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional

# Import dotenv to load .env files
try:
    from dotenv import load_dotenv
except ImportError:
    # Fallback if python-dotenv not installed
    def load_dotenv(*args, **kwargs):
        pass


class ConfigLoader:
    """Loads and manages configuration from config.json and environment"""

    def __init__(self, config_file: str = "config.json"):
        # Load .env file if it exists (for development only)
        env_file = Path(".env")
        if env_file.exists():
            load_dotenv(env_file)
        
        self.config_file = Path(config_file)
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Загрузить конфигурацию из config.json"""
        if not self.config_file.exists():
            raise FileNotFoundError(f"❌ Файл конфигурации не найден: {self.config_file}")
        
        with open(self.config_file, 'r') as f:
            return json.load(f)

    def get_nvidia_api_config(self) -> Dict[str, Any]:
        """Получить конфигурацию NVIDIA API"""
        import os
        
        api_config = self.config.get("nvidia_api", {})
        # Load API key from environment variable or config.json
        api_key_config = api_config.get("api_key", {})
        source = api_key_config.get("source", "env")

        if source == "env":
            env_var = api_key_config.get("env_var", "NVIDIA_API_KEY")
            api_key = os.getenv(env_var)
            if not api_key:
                raise ValueError(f"❌ API ключ не найден в переменной окружения {env_var}. "
                                f"Установите переменную окружения:\n"
                                f"  export {env_var}='your_api_key_here'  (Linux/Mac)\n"
                                f"  set {env_var}=your_api_key_here      (Windows cmd)\n"
                                f"  ${env_var}='your_api_key_here'       (PowerShell)")
        elif source == "direct":
            # Backwards compatibility: try to use direct value if specified in config
            api_key = api_key_config.get("value")
            if not api_key or str(api_key).startswith("YOUR_"):
                raise ValueError(f"❌ API ключ не найден в config.json или переменной окружения. "
                                f"Установите переменную окружения NVIDIA_API_KEY или обновите config.json.")
        else:
            raise ValueError(f"❌ Неизвестный источник API ключа: {source}. Используйте 'env' или 'direct'.")
        
        return {
            "endpoint": api_config.get("endpoint"),
            "model": api_config.get("model"),
            "api_key": api_key,
            "generation_params": api_config.get("generation_params", {}),
            "stream": api_config.get("stream", False),
            "enable_reasoning": api_config.get("enable_reasoning", False)
        }

    def get_browser_config(self) -> Dict[str, Any]:
        """Получить конфигурацию браузера"""
        return self.config.get("browser", {})

    def get_agent_config(self) -> Dict[str, Any]:
        """Получить конфигурацию агента"""
        return self.config.get("agent", {})

    def get_logging_config(self) -> Dict[str, Any]:
        """Получить конфигурацию логирования"""
        return self.config.get("logging", {"level": "INFO"})

    def get_all_config(self) -> Dict[str, Any]:
        """Получить всю конфигурацию"""
        return self.config.copy()


# Global config instance
config = ConfigLoader()
