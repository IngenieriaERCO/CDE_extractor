"""Contratos de almacenamiento y punto de extensión para SharePoint/Microsoft Graph.

La integración remota no está implementada. Esta capa no solicita credenciales ni abre
conexiones; sus operaciones fallan explícitamente hasta incorporar un adaptador Graph.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


class SharePointNotImplementedError(NotImplementedError):
    """La operación requiere la futura integración con Microsoft Graph."""


@dataclass(frozen=True, slots=True)
class StoredFile:
    name: str
    path: str
    size_bytes: int | None = None


class StorageBackend(ABC):
    """Contrato desacoplado para cargar y consultar documentos remotos."""

    @abstractmethod
    def upload_file(self, local_path: str | Path, remote_path: str) -> StoredFile:
        """Carga un archivo y devuelve sus metadatos remotos."""

    @abstractmethod
    def list_files(self, remote_path: str = "") -> list[StoredFile]:
        """Lista los archivos disponibles bajo una ruta remota."""

    @abstractmethod
    def exists(self, remote_path: str) -> bool:
        """Indica si existe una ruta remota."""


class SharePointStorage(StorageBackend):
    """Implementación pendiente; no reporta cargas simuladas como exitosas."""

    def upload_file(self, local_path: str | Path, remote_path: str) -> StoredFile:
        raise SharePointNotImplementedError(
            "La carga a SharePoint todavía no está implementada; requiere Microsoft Graph."
        )

    def list_files(self, remote_path: str = "") -> list[StoredFile]:
        raise SharePointNotImplementedError(
            "La consulta de SharePoint todavía no está implementada; requiere Microsoft Graph."
        )

    def exists(self, remote_path: str) -> bool:
        raise SharePointNotImplementedError(
            "La consulta de SharePoint todavía no está implementada; requiere Microsoft Graph."
        )
