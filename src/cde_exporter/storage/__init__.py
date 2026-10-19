"""Abstracciones para almacenamiento documental."""

from cde_exporter.storage.sharepoint import (
    SharePointNotImplementedError,
    SharePointStorage,
    StorageBackend,
    StoredFile,
)

__all__ = ["SharePointNotImplementedError", "SharePointStorage", "StorageBackend", "StoredFile"]
