"""Position-protection algorithms, registered via the ``@protector`` decorator."""

from agent.protection import trailing_stop  # noqa: F401 - registers ``trailing_stop``
from agent.protection.base import (
    PROTECTORS,
    ProtectionContext,
    Protector,
    available_protectors,
    protector,
)

__all__ = [
    "PROTECTORS",
    "ProtectionContext",
    "Protector",
    "available_protectors",
    "protector",
]
