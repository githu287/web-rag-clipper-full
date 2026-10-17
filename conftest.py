"""Tests never load personal .env credentials."""
from backend.core.config import Settings
Settings.model_config["env_file"] = None
