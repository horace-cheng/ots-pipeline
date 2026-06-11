"""
Tests for shared.db utility functions used by all pipeline jobs.
"""
import os, sys
os.environ.setdefault("ORDER_ID", "test-order-gutenberg")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

# Make `ots-pipeline/` importable so `from shared.db import ...` works
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch
import pytest
from shared.db import update_order_field


class TestUpdateOrderField:
    def test_allows_title_field(self):
        """gt_fetcher updates the order's title to the real Gutenberg
        book title after fetching — this must be in the allow list."""
        mock_db = MagicMock()
        with patch("shared.db.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__.return_value = mock_db
            update_order_field("title", "Pride and Prejudice")

        assert mock_db.execute.called
        sql = str(mock_db.execute.call_args[0][0])
        assert "title" in sql
        assert mock_db.execute.call_args[0][1]["value"] == "Pride and Prejudice"

    def test_allows_standard_fields(self):
        mock_db = MagicMock()
        with patch("shared.db.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__.return_value = mock_db
            for f in (
                "gcs_upload_path", "gcs_output_path",
                "gcs_bilingual_output_path", "gcs_plain_text_output_path",
                "term_dict_id", "status",
            ):
                update_order_field(f, "x")

        assert mock_db.execute.call_count == 6

    def test_rejects_unknown_field(self):
        with pytest.raises(ValueError, match="Field not allowed"):
            update_order_field("price_ntd", 100)

        with pytest.raises(ValueError, match="Field not allowed"):
            update_order_field("user_id", "abc")


# ── temp_blob_exists (storage) ──────────────────────────────────────────────

class TestTempBlobExists:
    def test_returns_true_when_blob_exists(self, monkeypatch):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from shared import storage as storage_mod
        from unittest.mock import MagicMock
        fake_bucket = MagicMock()
        fake_blob = MagicMock()
        fake_blob.exists.return_value = True
        fake_bucket.blob.return_value = fake_blob
        fake_client = MagicMock()
        fake_client.bucket.return_value = fake_bucket
        monkeypatch.setattr(storage_mod, "get_client", lambda: fake_client)
        assert storage_mod.temp_blob_exists("translated/chunk_0.txt") is True
        fake_bucket.blob.assert_called_once()

    def test_returns_false_when_blob_missing(self, monkeypatch):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from shared import storage as storage_mod
        from unittest.mock import MagicMock
        fake_bucket = MagicMock()
        fake_blob = MagicMock()
        fake_blob.exists.return_value = False
        fake_bucket.blob.return_value = fake_blob
        fake_client = MagicMock()
        fake_client.bucket.return_value = fake_bucket
        monkeypatch.setattr(storage_mod, "get_client", lambda: fake_client)
        assert storage_mod.temp_blob_exists("translated/chunk_99.txt") is False
