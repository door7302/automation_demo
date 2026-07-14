"""Exception hierarchy for the juniper_api library."""


class JuniperDeviceError(Exception):
    """Base class for all juniper_api errors."""


class ConnectionError(JuniperDeviceError):
    """Raised when opening/closing the NETCONF session fails."""


class ShowCommandError(JuniperDeviceError):
    """Raised when an operational ("show") command fails."""


class ConfigError(JuniperDeviceError):
    """Raised when loading, validating or committing configuration fails."""


class ShellError(JuniperDeviceError):
    """Raised when an RE/FPC shell command fails."""


class TransferError(JuniperDeviceError):
    """Raised when an SCP/FTP file transfer fails."""


class UpgradeError(JuniperDeviceError):
    """Raised when a software upgrade or reboot fails."""
