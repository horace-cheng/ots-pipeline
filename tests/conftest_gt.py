"""
Conftest for Gutenberg pipeline tests.

Sets the required environment variables BEFORE any module imports
shared.config (which validates them at import time).
"""
import os

os.environ.setdefault("ORDER_ID", "test-order-gutenberg")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")
