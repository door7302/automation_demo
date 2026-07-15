"""High-level NETCONF management library for Juniper devices, built on Junos PyEZ.

Public API:
    from juniper_api import JuniperDevice
"""

from .device import JuniperDevice
from .exceptions import (
    JuniperDeviceError,
    ConnectionError,
    ShowCommandError,
    ConfigError,
    ShellError,
    TransferError,
    UpgradeError,
)

__all__ = [
    "JuniperDevice",
    "JuniperDeviceError",
    "ConnectionError",
    "ShowCommandError",
    "ConfigError",
    "ShellError",
    "TransferError",
    "UpgradeError",
]

__version__ = "1.0.0"
