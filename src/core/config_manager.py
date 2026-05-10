

from pathlib import Path
import shutil
from typing import Dict, Any, Optional
import logging
import yaml
from llm import config as llm_config

logger = logging.getLogger(__name__)


class ConfigManager:

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or llm_config.CONFIG_PATH
        self._cached_data = None

    def load(self, force_reload: bool = False) -> Dict[str, Any]:
        """Load configuration from file, using cache if available.

        Args:
            force_reload: If True, bypass cache and reload from disk

        Returns:
            Configuration dictionary
        """
        if not force_reload and self._cached_data is not None:
            return self._cached_data

        if not self.config_path.exists():
            self._cached_data = llm_config.generate_config_template()
            return self._cached_data

        try:
            with open(self.config_path, 'r', encoding='utf-8-sig') as f:
                self._cached_data = yaml.safe_load(f) or {}

            # Migrate legacy provider IDs to current names.
            old_provider = self._cached_data.get('LAST_PROVIDER')
            if old_provider in ('vmcode_proxy', 'vmcode_free', 'vmcode'):
                logger.info("Migrating provider name '%s' -> 'bone'", old_provider)
                self._cached_data['LAST_PROVIDER'] = 'bone'
                self.save(self._cached_data, create_backup=True)
            elif old_provider == 'codex_plan':
                logger.info("Migrating provider name '%s' -> 'codex'", old_provider)
                self._cached_data['LAST_PROVIDER'] = 'codex'
                self.save(self._cached_data, create_backup=True)

            return self._cached_data
        except yaml.YAMLError as e:
            logger.error(f"Failed to parse config file {self.config_path}: {e}")
            logger.warning("Using default configuration template")
            self._cached_data = llm_config.generate_config_template()
            return self._cached_data

    def save(self, config_data: Dict[str, Any], create_backup: bool = False):
        if create_backup and self.config_path.exists():
            backup_path = self.config_path.with_suffix('.backup')
            shutil.copy2(self.config_path, backup_path)

        with open(self.config_path, 'w', encoding='utf-8-sig') as f:
            yaml.dump(config_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        self._cached_data = config_data
        # Note: Config is read from disk on reload. Call reload_config() after changes.

    def update_field(self, key: str, value: Any, create_backup: bool = False) -> Optional[Path]:
        """Update a single configuration field.

        Args:
            key: Configuration key to update
            value: New value for the key
            create_backup: If True, create a backup before saving

        Returns:
            Backup path if backup was created, None otherwise
        """
        config_data = self.load(force_reload=True)
        config_data[key] = value

        backup_path = None
        if create_backup and self.config_path.exists():
            backup_path = self.config_path.with_suffix('.backup')

        self.save(config_data, create_backup=create_backup)
        return backup_path

    def set_provider(self, provider_name: str) -> Optional[Path]:
        return self.update_field('LAST_PROVIDER', provider_name)

    def _extract_model_pricing(self, model_name: str, config: Dict[str, Any] | None = None) -> Dict[str, float]:
        """Extract pricing for a model from MODEL_PRICES.

        Delegates to llm_config.get_model_cost() — single source of truth.
        """
        cost_in, cost_out = llm_config.get_model_cost(model_name, config=config)
        return {'in': cost_in, 'out': cost_out}

    def get_usage_costs(self, provider: str = None, model: str = None) -> Dict[str, float]:
        """Get usage costs for a specific model.

        Args:
            provider: Provider name (e.g., 'openrouter', 'glm', 'openai').
                     If None, uses the last provider from config.
            model: Model name (e.g., 'minimax/minimax-m2.5', 'GLM-4.7').
                   If None, uses the current model from the provider.

        Returns:
            Dict with 'in' and 'out' cost values per 1M tokens
        """
        config_data = self.load()

        if provider is None:
            provider = config_data.get('LAST_PROVIDER', 'glm')

        # Get model name from config if not provided
        if model is None:
            provider_model_map = {
                'bone': 'BONE_PROXY_MODEL',
                'codex': 'CODEX_PLAN_MODEL',
                'openrouter': 'OPENROUTER_MODEL',
                'glm': 'GLM_MODEL',
                'glm_plan': 'GLM_PLAN_MODEL',
                'openai': 'OPENAI_MODEL',
                'gemini': 'GEMINI_MODEL',
                'minimax': 'MINIMAX_MODEL',
                'minimax_plan': 'MINIMAX_PLAN_MODEL',
                'anthropic': 'ANTHROPIC_MODEL',
                'kimi': 'KIMI_MODEL',
                'deepseek': 'DEEPSEEK_MODEL'
            }
            model_key = provider_model_map.get(provider.lower())
            if model_key:
                model = config_data.get(model_key, '')

        return self._extract_model_pricing(model, config=config_data)

    def set_model(self, provider_name: str, model: str) -> Optional[Path]:
        """Set model for a specific provider.

        Args:
            provider_name: Provider name (e.g., 'openrouter', 'glm', 'local', 'openai')
            model: Model name/path to set

        Returns:
            Backup path if backup was created, None otherwise
        """
        # Map provider names to their config keys
        provider_keys = {
            'local': 'LLM_MODEL',
            'bone': 'BONE_PROXY_MODEL',
            'codex': 'CODEX_PLAN_MODEL',
            'openrouter': 'OPENROUTER_MODEL',
            'glm': 'GLM_MODEL',
            'glm_plan': 'GLM_PLAN_MODEL',
            'openai': 'OPENAI_MODEL',
            'gemini': 'GEMINI_MODEL',
            'minimax': 'MINIMAX_MODEL',
            'minimax_plan': 'MINIMAX_PLAN_MODEL',
            'anthropic': 'ANTHROPIC_MODEL',
            'kimi': 'KIMI_MODEL',
            'deepseek': 'DEEPSEEK_MODEL',
        }

        if provider_name not in provider_keys:
            raise ValueError(f"Unknown provider: {provider_name}")

        key = provider_keys[provider_name]
        return self.update_field(key, model)

    def set_api_key(self, provider_name: str, api_key: str) -> Optional[Path]:
        """Set API key for a specific provider.

        Args:
            provider_name: Provider name (e.g., 'openrouter', 'glm', 'openai')
            api_key: API key to set

        Returns:
            Backup path if backup was created, None otherwise
        """
        # Map provider names to their config keys
        provider_keys = {
            'openrouter': 'OPENROUTER_API_KEY',
            'bone': 'BONE_PROXY_API_KEY',
            'codex': 'CODEX_PLAN_API_KEY',
            'glm': 'GLM_API_KEY',
            'glm_plan': 'GLM_PLAN_API_KEY',
            'openai': 'OPENAI_API_KEY',
            'gemini': 'GEMINI_API_KEY',
            'minimax': 'MINIMAX_API_KEY',
            'minimax_plan': 'MINIMAX_PLAN_API_KEY',
            'anthropic': 'ANTHROPIC_API_KEY',
            'kimi': 'KIMI_API_KEY',
            'deepseek': 'DEEPSEEK_API_KEY',
        }

        if provider_name not in provider_keys:
            raise ValueError(f"Unknown provider: {provider_name}")

        key = provider_keys[provider_name]
        return self.update_field(key, api_key)

    def get_model_price(self, model_name: str) -> Dict[str, float]:
        """Get pricing for a specific model.

        Args:
            model_name: Model name (e.g., 'minimax/minimax-m2.5', 'GLM-4.7')

        Returns:
            Dict with 'in' and 'out' cost values per 1M tokens
        """
        config_data = self.load()
        return self._extract_model_pricing(model_name, config=config_data)

    def set_model_price(self, model_name: str, cost_in: float, cost_out: float) -> Optional[Path]:
        """Set pricing for a specific model.

        Args:
            model_name: Model name (e.g., 'minimax/minimax-m2.5', 'GLM-4.7')
            cost_in: Cost per 1M input tokens
            cost_out: Cost per 1M output tokens

        Returns:
            Backup path if backup was created, None otherwise
        """
        config_data = self.load(force_reload=True)

        if 'MODEL_PRICES' not in config_data:
            config_data['MODEL_PRICES'] = {}

        config_data['MODEL_PRICES'][model_name] = {
            'cost_in': cost_in,
            'cost_out': cost_out
        }

        return self.save(config_data, create_backup=False)

