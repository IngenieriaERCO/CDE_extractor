"""Protocolo del trabajador (docs/protocolo_trabajador.md): mensajes, versión y errores.

Una línea JSON por mensaje. Claves y valores enumerados en inglés; mensajes en español. Solo usa
la biblioteca estándar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PROTOCOL_VERSION = "1.3"

# «texts» desde la 1.3: textos de una zona y escala propuesta (zone_texts, scale_text).
COMMANDS = frozenset(
    {"load", "preview", "texts", "export", "cancel", "status", "close"}
)
# Se atienden siempre, aunque otro pedido esté en curso (los lee el hilo de stdin).
IMMEDIATE_COMMANDS = frozenset({"cancel", "status", "close"})

# Códigos de error del pedido (sección 8 del contrato).
PROTOCOL_VERSION_ERROR = "PROTOCOL_VERSION"
BAD_REQUEST = "BAD_REQUEST"
UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
BUSY = "BUSY"
NO_SESSION = "NO_SESSION"
DXF_NOT_FOUND = "DXF_NOT_FOUND"
DXF_INVALID = "DXF_INVALID"
DXF_TOO_LARGE = "DXF_TOO_LARGE"
DXF_CHANGED = "DXF_CHANGED"
BLOCK_NOT_FOUND = "BLOCK_NOT_FOUND"
REFERENCE_NOT_FOUND = "REFERENCE_NOT_FOUND"
NOT_EXPORTABLE = "NOT_EXPORTABLE"
OUTPUT_EXISTS = "OUTPUT_EXISTS"
OUTPUT_INVALID = "OUTPUT_INVALID"
CANCELLED = "CANCELLED"
INTERNAL = "INTERNAL"

ERROR_CODES = frozenset(
    {
        PROTOCOL_VERSION_ERROR,
        BAD_REQUEST,
        UNKNOWN_COMMAND,
        BUSY,
        NO_SESSION,
        DXF_NOT_FOUND,
        DXF_INVALID,
        DXF_TOO_LARGE,
        DXF_CHANGED,
        BLOCK_NOT_FOUND,
        REFERENCE_NOT_FOUND,
        NOT_EXPORTABLE,
        OUTPUT_EXISTS,
        OUTPUT_INVALID,
        CANCELLED,
        INTERNAL,
    }
)


@dataclass
class WorkerError(Exception):
    """Error que termina un pedido: se envía como mensaje ``error`` (nunca con traceback)."""

    code: str
    message: str
    advice: str | None = None
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.code not in ERROR_CODES:
            raise ValueError(f"Código de error desconocido: {self.code}")

    def payload(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "advice": self.advice,
            "details": self.details,
        }


@dataclass(frozen=True)
class Request:
    id: str
    command: str
    data: dict


def encode(message: dict) -> bytes:
    """Una línea JSON en UTF-8 (sin escapar acentos), terminada en ``\\n``."""
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def message(type_: str, request_id: str | None, **fields) -> dict:
    return {"protocol": PROTOCOL_VERSION, "id": request_id, "type": type_, **fields}


def result(request: Request, data: dict) -> dict:
    return message("result", request.id, command=request.command, data=data)


def error(request_id: str | None, failure: WorkerError, command: str | None = None) -> dict:
    fields = {"error": failure.payload()}
    if command is not None:
        fields["command"] = command
    return message("error", request_id, **fields)


def progress(request_id: str, data: dict) -> dict:
    return message("progress", request_id, data=data)


def event(name: str, data: dict) -> dict:
    """Aviso que no responde a ningún pedido (``id`` null; desde la 1.2): la preparación de la
    vista previa en segundo plano (``preview_progress``, ``preview_ready``,
    ``preview_failed``)."""
    return message("event", None, event=name, data=data)


def parse_request(line: bytes) -> Request:
    """Pedido de una línea de stdin; lanza ``WorkerError`` si no cumple el formato.

    Admite una BOM de UTF-8 al principio (la pone PowerShell al escribir en una tubería)."""
    line = line.removeprefix(b"\xef\xbb\xbf")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as failure:
        raise WorkerError(BAD_REQUEST, "El pedido no está en UTF-8.") from failure
    try:
        data = json.loads(text)
    except json.JSONDecodeError as failure:
        raise WorkerError(
            BAD_REQUEST,
            f"El pedido no es un JSON válido (columna {failure.colno}): {failure.msg}.",
        ) from failure
    if not isinstance(data, dict):
        raise WorkerError(BAD_REQUEST, "El pedido debe ser un objeto JSON {…}.")
    request_id = data.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise WorkerError(BAD_REQUEST, "Falta «id» (texto no vacío) en el pedido.")
    version = data.get("protocol")
    if not isinstance(version, str) or not version:
        raise WorkerError(BAD_REQUEST, "Falta «protocol» en el pedido.", details={"id": request_id})
    if version.split(".", 1)[0] != PROTOCOL_VERSION.split(".", 1)[0]:
        raise WorkerError(
            PROTOCOL_VERSION_ERROR,
            f"Versión del protocolo {version} no compatible con {PROTOCOL_VERSION}.",
            "Actualice el Componedor o el exportador para que hablen la misma versión mayor.",
            {"id": request_id, "requested": version, "supported": PROTOCOL_VERSION},
        )
    command = data.get("command")
    if not isinstance(command, str) or command not in COMMANDS:
        raise WorkerError(
            UNKNOWN_COMMAND,
            f"Orden desconocida: {command!r}.",
            "Órdenes válidas: " + ", ".join(sorted(COMMANDS)) + ".",
            {"id": request_id},
        )
    payload = data.get("data", {})
    if not isinstance(payload, dict):
        raise WorkerError(
            BAD_REQUEST, "«data» debe ser un objeto JSON {…}.", details={"id": request_id}
        )
    return Request(request_id, command, payload)
