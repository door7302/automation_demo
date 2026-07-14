"""High-level wrapper around Junos PyEZ to manage Juniper devices over NETCONF."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, List, Optional, Tuple, Union

from lxml import etree

from jnpr.junos import Device
from jnpr.junos.exception import (
    ConnectError as PyEZConnectError,
    RpcError,
    CommitError,
    ConfigLoadError,
    LockError,
    UnlockError,
)
from jnpr.junos.utils.config import Config
from jnpr.junos.utils.fs import FS
from jnpr.junos.utils.ftp import FTP
from jnpr.junos.utils.scp import SCP
from jnpr.junos.utils.start_shell import StartShell
from jnpr.junos.utils.sw import SW

from .exceptions import (
    ConfigError,
    ConnectionError,
    ShellError,
    ShowCommandError,
    TransferError,
    UpgradeError,
)

# Output formats supported by :meth:`JuniperDevice.show`.
_TEXT_FORMATS = ("text", "txt")
_XML_FORMAT = "xml"
_JSON_FORMAT = "json"

# Configuration edit modes supported by :meth:`JuniperDevice.edit_config`.
_EDIT_MODES = ("exclusive", "private")


class JuniperDevice:
    """Manage a Juniper device remotely over NETCONF using Junos PyEZ.

    The class exposes a small, high-level API on top of PyEZ:

    * :meth:`open` / :meth:`close` - manage the NETCONF session.
    * :meth:`show` - run an operational command (text, XML or JSON output).
    * :meth:`filter` - filter previously retrieved XML/JSON output via XPath.
    * :meth:`select` - read clean text values from XML without writing XPath.
    * :meth:`show_shell` - run an RE or FPC shell command.
    * :meth:`edit_config` - load and commit configuration (with dry-run support).
    * :meth:`show_diff` - compare two commit/rollback points.
    * :meth:`upload` - copy a file to the device via SCP or FTP.
    * :meth:`upgrade` - install a software package.
    * :meth:`reboot` - reboot a routing engine or an FPC.

    It can be used as a context manager::

        with JuniperDevice("10.0.0.1", user="admin", passwd="secret",
                           logger=my_logger) as dev:
            print(dev.show("show version"))
    """

    def __init__(
        self,
        host: str,
        user: Optional[str] = None,
        passwd: Optional[str] = None,
        port: int = 830,
        logger: Optional[logging.Logger] = None,
        ssh_private_key_file: Optional[str] = None,
        timeout: int = 30,
        gather_facts: bool = True,
        **device_kwargs: Any,
    ) -> None:
        """Initialise the device wrapper (does not connect yet).

        :param host: Hostname or IP address of the target device.
        :param user: Login user-name. Defaults to ``$USER`` when omitted.
        :param passwd: Login password. Omit when relying on SSH keys.
        :param port: NETCONF TCP port (default ``830``).
        :param logger: A :class:`logging.Logger` used for state/error logging.
            When omitted a module logger with a :class:`~logging.NullHandler`
            is used so the library stays silent by default.
        :param ssh_private_key_file: Optional path to an SSH private key.
        :param timeout: Default RPC timeout (seconds).
        :param gather_facts: Whether PyEZ should gather facts on :meth:`open`.
        :param device_kwargs: Any extra keyword arguments forwarded to the
            underlying :class:`jnpr.junos.Device`.
        """
        self.host = host
        self.log = logger or self._default_logger()

        self._timeout = timeout
        self._dev = Device(
            host=host,
            user=user,
            passwd=passwd,
            port=port,
            ssh_private_key_file=ssh_private_key_file,
            gather_facts=gather_facts,
            **device_kwargs,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _default_logger() -> logging.Logger:
        logger = logging.getLogger("juniper_api")
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
        return logger

    @property
    def dev(self) -> Device:
        """The underlying :class:`jnpr.junos.Device` instance."""
        return self._dev

    @property
    def connected(self) -> bool:
        """Whether the NETCONF session is currently open."""
        return bool(self._dev.connected)

    @property
    def facts(self) -> dict:
        """Device facts gathered by PyEZ."""
        return dict(self._dev.facts)

    def _progress(self) -> Callable[[Device, str], None]:
        """Return a PyEZ progress callback that forwards reports to the logger."""

        def _report(_dev: Device, report: str) -> None:
            self.log.info("[%s] %s", self.host, report)

        return _report

    @staticmethod
    def _element_text(element: Any) -> str:
        """Extract the text payload from an lxml element returned by an RPC."""
        if element is None:
            return ""
        if isinstance(element, str):
            return element
        if hasattr(element, "text") and element.text is not None:
            return element.text
        # Fall back to serialising the whole element.
        return etree.tostring(element, encoding="unicode")

    @staticmethod
    def _normalize(value: str) -> str:
        """Collapse internal whitespace and strip ends (XPath ``normalize-space``)."""
        return " ".join(value.split())

    @classmethod
    def _node_text(cls, node: Any) -> str:
        """Return the whitespace-normalized text of an XPath result item.

        Handles both string results (from ``text()``/attribute XPaths, which
        lxml returns as ``str`` subclasses) and element results (whose text
        content is extracted).
        """
        if isinstance(node, str):
            return cls._normalize(node)
        text = getattr(node, "text", None)
        return cls._normalize(text) if text is not None else ""

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #
    def open(self) -> "JuniperDevice":
        """Open the NETCONF session.

        :raises ConnectionError: when the connection cannot be established.
        """
        try:
            self.log.info("Connecting to %s ...", self.host)
            self._dev.open()
            self._dev.timeout = self._timeout
            self.log.info("Connected to %s", self.host)
            return self
        except PyEZConnectError as err:
            self.log.error("Failed to connect to %s: %s", self.host, err)
            raise ConnectionError(str(err)) from err

    def close(self) -> None:
        """Close the NETCONF session (no-op when already closed)."""
        try:
            if self._dev.connected:
                self._dev.close()
                self.log.info("Disconnected from %s", self.host)
        except Exception as err:  # pragma: no cover - best-effort close
            self.log.warning("Error while closing session to %s: %s", self.host, err)

    def __enter__(self) -> "JuniperDevice":
        return self.open()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Operational ("show") commands
    # ------------------------------------------------------------------ #
    def show(
        self,
        command: str,
        fmt: str = "text",
    ) -> Union[str, dict, etree._Element]:
        """Run an operational command and return its output.

        :param command: A Junos operational command, e.g. ``"show version"``.
        :param fmt: Desired output format:

            * ``"text"`` / ``"txt"`` -> returns a ``str``.
            * ``"xml"`` -> returns an :class:`lxml.etree._Element`.
            * ``"json"`` -> returns a ``dict``.

        :returns: The command output in the requested format.
        :raises ShowCommandError: when the command fails.
        """
        fmt = fmt.lower()
        self.log.debug("show(%r, fmt=%s) on %s", command, fmt, self.host)
        try:
            if fmt in _TEXT_FORMATS:
                resp = self._dev.rpc.cli(command, format="text")
                return self._element_text(resp)
            if fmt == _XML_FORMAT:
                return self._dev.rpc.cli(command, format="xml")
            if fmt == _JSON_FORMAT:
                return self._dev.rpc.cli(command, format="json")
            raise ValueError(
                "Unsupported format %r (use 'text', 'xml' or 'json')" % fmt
            )
        except ValueError:
            raise
        except RpcError as err:
            self.log.error("show command failed on %s: %s", self.host, err)
            raise ShowCommandError(str(err)) from err

    # ------------------------------------------------------------------ #
    # Output filtering
    # ------------------------------------------------------------------ #
    def filter(
        self,
        output: Union[etree._Element, dict, list],
        path: str,
        *,
        text: bool = False,
        first: bool = False,
    ) -> Union[List[Any], Any]:
        """Filter previously retrieved output by an XPath / path expression.

        * For **XML** output (an lxml element returned by ``show(..., fmt="xml")``)
          a standard XPath expression is applied and the matching node list is
          returned.
        * For **JSON** output (a ``dict``/``list`` returned by
          ``show(..., fmt="json")``) a simple ``/``-separated path is supported,
          where each component is a mapping key or a list index, e.g.
          ``"software-information/0/host-name"``.

        :param output: The output returned by :meth:`show`.
        :param path: XPath (for XML) or ``/``-separated path (for JSON).
        :param text: When ``True`` (XML only), return whitespace-normalized text
            strings instead of nodes. This lets you drop the trailing ``text()``
            from the XPath and the manual ``str(...).strip()`` afterwards: both
            ``filter(xml, "//name/text()", text=True)`` and
            ``filter(xml, "//name", text=True)`` yield clean strings.
        :param first: When ``True``, return only the first match (or ``None``
            when there is none) instead of a list. Handy for single-value
            lookups such as a host-name or Junos version.
        :returns: The list of matching nodes/strings (XML) or the addressed
            value (JSON). With ``first=True`` a single item (or ``None``).
        :raises ShowCommandError: when the path cannot be evaluated.
        """
        if isinstance(output, (etree._Element, etree._ElementTree)):
            try:
                result = output.xpath(path)
            except etree.XPathError as err:
                raise ShowCommandError("Invalid XPath %r: %s" % (path, err)) from err

            if text:
                result = [self._node_text(node) for node in result]

            if first:
                return result[0] if result else None
            return result

        if isinstance(output, (dict, list)):
            return self._filter_json(output, path)

        raise ShowCommandError(
            "filter() expects XML (lxml element) or JSON (dict/list), got %s"
            % type(output).__name__
        )

    def select(
        self,
        output: Union[etree._Element, etree._ElementTree],
        element: str,
        field: Optional[str] = None,
        where: Optional[dict] = None,
        first: bool = False,
    ) -> Union[List[str], Optional[str]]:
        """Select clean text values from XML without writing XPath by hand.

        This is a convenience layer over :meth:`filter` that builds the XPath
        for you, including the ``normalize-space(...)`` whitespace handling in
        match predicates, so callers never have to write ``text()``,
        ``normalize-space()`` or ``.strip()`` themselves.

        Examples::

            # Every physical interface name
            dev.select(xml, "physical-interface", "name")

            # Names of interfaces whose oper-status is "down"
            dev.select(xml, "physical-interface", "name",
                       where={"oper-status": "down"})

            # A single host-name value
            dev.select(ver_xml, "host-name", first=True)

            # BGP peers that are NOT Established
            dev.select(bgp_xml, "bgp-peer", "peer-address",
                       where={"peer-state": ("!startswith", "Established")})

        :param output: XML output from ``show(..., fmt="xml")``.
        :param element: Tag of the (possibly repeating) element to match, e.g.
            ``"physical-interface"``. Matched anywhere in the tree (``//``).
        :param field: Optional child element whose text is returned. When
            omitted, the text of ``element`` itself is returned.
        :param where: Optional mapping of child element -> condition used to
            filter ``element``. Each value is either a plain string (equality)
            or a ``(operator, value)`` tuple. Supported operators:
            ``"="``, ``"!="``, ``"startswith"``, ``"!startswith"``,
            ``"contains"`` and ``"!contains"``. All conditions are AND-ed and
            compared using ``normalize-space()`` so surrounding whitespace in
            the XML is ignored.
        :param first: When ``True`` return the first value (or ``None``)
            instead of a list.
        :returns: A list of whitespace-normalized strings, or a single string /
            ``None`` when ``first=True``.
        :raises ShowCommandError: when the generated XPath cannot be evaluated.
        """
        xpath = "//%s%s" % (element, self._build_predicate(where))
        if field:
            xpath += "/%s" % field
        return self.filter(output, xpath, text=True, first=first)

    @classmethod
    def _build_predicate(cls, where: Optional[dict]) -> str:
        """Build an XPath predicate (``[...]``) from a ``where`` mapping."""
        if not where:
            return ""

        clauses: List[str] = []
        for field, condition in where.items():
            if isinstance(condition, tuple):
                operator, value = condition
            else:
                operator, value = "=", condition

            target = "normalize-space(%s)" % field
            literal = cls._xpath_literal(value)

            if operator == "=":
                clauses.append("%s=%s" % (target, literal))
            elif operator == "!=":
                clauses.append("%s!=%s" % (target, literal))
            elif operator == "startswith":
                clauses.append("starts-with(%s,%s)" % (target, literal))
            elif operator == "!startswith":
                clauses.append("not(starts-with(%s,%s))" % (target, literal))
            elif operator == "contains":
                clauses.append("contains(%s,%s)" % (target, literal))
            elif operator == "!contains":
                clauses.append("not(contains(%s,%s))" % (target, literal))
            else:
                raise ValueError(
                    "Unsupported where operator %r for field %r" % (operator, field)
                )

        return "[%s]" % " and ".join(clauses)

    @staticmethod
    def _xpath_literal(value: Any) -> str:
        """Return *value* as a safe XPath 1.0 string literal.

        XPath 1.0 has no escape character, so a value containing both single
        and double quotes is emitted using ``concat()``.
        """
        text = str(value)
        if "'" not in text:
            return "'%s'" % text
        if '"' not in text:
            return '"%s"' % text
        # Contains both quote types: split on ' and re-join via concat().
        parts = text.split("'")
        pieces = []
        for index, part in enumerate(parts):
            if index:
                pieces.append('"\'"')  # a literal single quote
            if part:
                pieces.append("'%s'" % part)
        return "concat(%s)" % ", ".join(pieces)

    @staticmethod
    def _filter_json(data: Union[dict, list], path: str) -> Any:
        """Navigate a JSON structure using a ``/``-separated path."""
        current: Any = data
        for raw in path.strip("/").split("/"):
            if raw == "":
                continue
            if isinstance(current, list):
                try:
                    current = current[int(raw)]
                except (ValueError, IndexError) as err:
                    raise ShowCommandError(
                        "Invalid JSON list index %r in path %r" % (raw, path)
                    ) from err
            elif isinstance(current, dict):
                if raw not in current:
                    raise ShowCommandError(
                        "Key %r not found in JSON output (path %r)" % (raw, path)
                    )
                current = current[raw]
            else:
                raise ShowCommandError(
                    "Cannot descend into %r at path component %r"
                    % (type(current).__name__, raw)
                )
        return current

    # ------------------------------------------------------------------ #
    # RE / FPC shell commands
    # ------------------------------------------------------------------ #
    def show_shell(
        self,
        command: str,
        target: str = "re",
        fpc: int = 0,
        timeout: int = 30,
    ) -> str:
        """Run a shell command on the Routing Engine or an FPC.

        :param command: The shell command to execute.
        :param target: ``"re"`` to run on the Routing Engine shell, or
            ``"fpc"`` to run on an FPC (line card) shell.
        :param fpc: FPC slot number (used only when ``target == "fpc"``).
        :param timeout: Seconds to wait for the command to complete.
        :returns: The command output as text.
        :raises ShellError: when the shell command fails.
        """
        target = target.lower()
        if target == "fpc":
            shell_cmd = 'cprod -A fpc%d -c "%s"' % (fpc, command)
        elif target == "re":
            shell_cmd = command
        else:
            raise ValueError("target must be 're' or 'fpc', got %r" % target)

        self.log.debug("show_shell(%r) on %s", shell_cmd, self.host)
        try:
            with StartShell(self._dev, timeout=timeout) as shell:
                ok, output = shell.run(shell_cmd, timeout=timeout)
            if not ok:
                self.log.warning(
                    "Shell command returned non-zero on %s: %s", self.host, shell_cmd
                )
            return output
        except Exception as err:
            self.log.error("Shell command failed on %s: %s", self.host, err)
            raise ShellError(str(err)) from err

    # ------------------------------------------------------------------ #
    # Configuration editing
    # ------------------------------------------------------------------ #
    def edit_config(
        self,
        payload: Optional[str] = None,
        fmt: Optional[str] = None,
        mode: str = "exclusive",
        dry_run: bool = False,
        ignore_warning: Union[bool, str, List[str]] = False,
        merge: bool = False,
        overwrite: bool = False,
        comment: Optional[str] = None,
        confirm: Optional[int] = None,
        path: Optional[str] = None,
        url: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Optional[str]:
        """Load and (optionally) commit a configuration change.

        :param payload: Configuration content as a string. The format is
            auto-detected when ``fmt`` is omitted; provide ``fmt`` to be explicit.
        :param fmt: Payload format: ``"set"``, ``"text"``, ``"xml"`` or ``"json"``.
        :param mode: Configuration mode, ``"exclusive"`` (default) or ``"private"``.
        :param dry_run: When ``True`` the change is loaded and validated with a
            commit-check (equivalent to ``commit check``) **without** committing;
            the candidate is rolled back afterwards. The diff is still returned.
        :param ignore_warning: Passed to PyEZ ``load``/``commit`` to ignore
            warnings (``True``, a string, or a list of strings).
        :param merge: Use a merge load action instead of replace.
        :param overwrite: Completely overwrite the existing configuration.
        :param comment: Optional commit log comment.
        :param confirm: Activate ``commit confirmed`` with this timeout (minutes).
        :param path: Load configuration from a local file path instead of ``payload``.
        :param url: Load configuration from a URL (local path, FTP or HTTP).
        :param timeout: Optional commit timeout (seconds).
        :returns: The configuration diff (``str``) or ``None`` when there is no change.
        :raises ConfigError: when loading, validating or committing fails.
        """
        if mode not in _EDIT_MODES:
            raise ValueError("mode must be one of %s, got %r" % (_EDIT_MODES, mode))
        if payload is None and path is None and url is None:
            raise ValueError("provide one of 'payload', 'path' or 'url'")

        load_kwargs: dict = {}
        if fmt:
            load_kwargs["format"] = fmt
        if merge:
            load_kwargs["merge"] = True
        if overwrite:
            load_kwargs["overwrite"] = True
        if ignore_warning:
            load_kwargs["ignore_warning"] = ignore_warning

        self.log.info(
            "edit_config on %s (mode=%s, dry_run=%s)", self.host, mode, dry_run
        )
        try:
            with Config(self._dev, mode=mode) as cu:
                if path is not None:
                    cu.load(path=path, **load_kwargs)
                elif url is not None:
                    cu.load(url=url, **load_kwargs)
                else:
                    cu.load(payload, **load_kwargs)

                diff = cu.diff()

                if dry_run:
                    self.log.info("Dry-run: performing commit check on %s", self.host)
                    cu.commit_check(timeout=timeout) if timeout else cu.commit_check()
                    cu.rollback(0)
                    self.log.info("Dry-run validation succeeded on %s", self.host)
                    return diff

                if diff is None:
                    self.log.info("No configuration change to commit on %s", self.host)
                    return None

                commit_kwargs: dict = {}
                if comment:
                    commit_kwargs["comment"] = comment
                if ignore_warning:
                    commit_kwargs["ignore_warning"] = ignore_warning
                if confirm:
                    commit_kwargs["confirm"] = confirm
                if timeout:
                    commit_kwargs["timeout"] = timeout

                cu.commit(**commit_kwargs)
                self.log.info("Committed configuration on %s", self.host)
                return diff
        except (ConfigLoadError, CommitError, LockError, UnlockError, RpcError) as err:
            self.log.error("edit_config failed on %s: %s", self.host, err)
            raise ConfigError(str(err)) from err

    # ------------------------------------------------------------------ #
    # Configuration diff (before-commit candidate / after-commit rollbacks)
    # ------------------------------------------------------------------ #
    def show_diff(
        self,
        mode: str = "committed",
        from_rollback: int = 1,
        to_rollback: int = 0,
    ) -> str:
        """Show a configuration diff, either before or after a commit.

        :param mode:
            * ``"candidate"`` -- **before commit**: diff of the uncommitted
              candidate configuration against the running configuration
              (equivalent to ``show | compare``). Use this to review pending
              changes that have been loaded but not yet committed.
            * ``"committed"`` (default) -- **after commit**: diff between two
              commit/rollback points. By default this compares the previous
              commit (rollback ``1``) with the running configuration
              (rollback ``0``), i.e. it shows what the latest commit changed.
        :param from_rollback: Older rollback id ``[0-49]`` (default ``1``).
            Used only when ``mode == "committed"``.
        :param to_rollback: Newer rollback id ``[0-49]`` (default ``0`` = running).
            Used only when ``mode == "committed"``.
        :returns: The diff in patch/text format (empty string when identical).
        :raises ShowCommandError: when the comparison fails.
        """
        mode = mode.lower()
        if mode == "candidate":
            self.log.debug("show_diff(candidate vs running) on %s", self.host)
            try:
                diff = Config(self._dev).diff()
                return diff or ""
            except RpcError as err:
                self.log.error("show_diff failed on %s: %s", self.host, err)
                raise ShowCommandError(str(err)) from err

        if mode == "committed":
            cmd = "show system rollback compare %d %d" % (from_rollback, to_rollback)
            self.log.debug("show_diff via %r on %s", cmd, self.host)
            try:
                resp = self._dev.rpc.cli(cmd, format="text")
                return self._element_text(resp)
            except RpcError as err:
                self.log.error("show_diff failed on %s: %s", self.host, err)
                raise ShowCommandError(str(err)) from err

        raise ValueError(
            "mode must be 'candidate' or 'committed', got %r" % mode
        )

    # ------------------------------------------------------------------ #
    # File upload (SCP / FTP)
    # ------------------------------------------------------------------ #
    def upload(
        self,
        local_file: str,
        remote_path: str = "/var/tmp",
        method: str = "scp",
        progress: bool = True,
        md5: Optional[str] = None,
        force: bool = False,
        copy_to_backup: bool = False,
        backup_re: Optional[str] = None,
        checksum_timeout: int = 300,
        scp_socket_timeout: float = 600.0,
    ) -> bool:
        """Upload a local file to the device via SCP or FTP.

        Before transferring, the destination is inspected so an unnecessary
        upload is skipped:

        * If the file is **not** present remotely, it is uploaded.
        * If the file **is** present remotely and ``md5`` is provided, the
          remote MD5 (``file checksum md5 <path>``) is compared with ``md5``:
          a match skips the upload, a mismatch re-uploads the file.
        * If the file **is** present remotely and no ``md5`` is provided, the
          upload is skipped.
        * ``force=True`` always uploads, bypassing the checks above.

        :param local_file: Path to the local file (e.g. a software image).
        :param remote_path: Destination directory on the device.
        :param method: ``"scp"`` (default) or ``"ftp"``.
        :param progress: When ``True`` progress is reported through the logger.
        :param md5: Expected MD5 checksum of the file. When provided, the MD5 of
            the uploaded file is computed on the device and compared; a mismatch
            raises :class:`TransferError`.
        :param force: When ``True`` the file is always uploaded, skipping the
            remote existence/MD5 pre-check.
        :param copy_to_backup: When ``True``, after a successful upload to the
            master RE the file is copied from the master RE to the backup RE
            (using the Junos ``file copy`` command).
        :param backup_re: Name of the backup RE for the copy (e.g. ``"re1"``).
            When omitted it is derived from device facts (the RE that is not the
            current master).
        :param checksum_timeout: Seconds to wait for remote checksum computation.
        :param scp_socket_timeout: Per-channel socket timeout (seconds) for the
            SCP transfer. The scp client default (10s) is too low for large
            images and can abort the transfer with a socket timeout while
            waiting on the SSH send window; raise this for big files.
        :returns: ``True`` on success.
        :raises TransferError: when the transfer or checksum verification fails.
        """
        method = method.lower()
        cb = self._progress() if progress else None
        remote_file = remote_path.rstrip("/") + "/" + os.path.basename(local_file)

        if not force and not self._needs_upload(remote_file, md5, checksum_timeout):
            self.log.info(
                "Skipping upload of %s; already present on %s",
                local_file,
                self.host,
            )
            return True

        self.log.info(
            "Uploading %s -> %s:%s via %s", local_file, self.host, remote_path, method
        )
        try:
            if method == "scp":
                with SCP(
                    self._dev, progress=cb, socket_timeout=scp_socket_timeout
                ) as scp:
                    scp.put(local_file, remote_path)
            elif method == "ftp":
                with FTP(self._dev) as ftp:
                    if not ftp.put(local_file, remote_path):
                        raise TransferError("FTP put of %s failed" % local_file)
            else:
                raise ValueError("method must be 'scp' or 'ftp', got %r" % method)
            self.log.info("Upload of %s completed", local_file)
        except ValueError:
            raise
        except Exception as err:
            self.log.error("Upload failed on %s: %s", self.host, err)
            raise TransferError(str(err)) from err

        # Verify MD5 checksum of the uploaded file on the device.
        if md5 is not None:
            self._verify_md5(remote_file, md5, checksum_timeout)

        # Copy from master RE to backup RE.
        if copy_to_backup:
            self._copy_to_backup_re(remote_file, backup_re)

        return True

    def _needs_upload(
        self, remote_file: str, expected_md5: Optional[str], timeout: int
    ) -> bool:
        """Decide whether ``remote_file`` must be (re-)uploaded.

        :returns: ``True`` when the file is missing remotely, or present with an
            MD5 that differs from ``expected_md5``. ``False`` when the file is
            already present and either matches ``expected_md5`` or no expected
            MD5 was supplied.
        """
        try:
            stat = FS(self._dev).stat(remote_file)
        except RpcError as err:
            self.log.warning(
                "Unable to stat %s on %s (%s); uploading", remote_file, self.host, err
            )
            return True

        if not stat:
            self.log.info("%s not present on %s; uploading", remote_file, self.host)
            return True

        if not expected_md5:
            self.log.info(
                "%s already present on %s and no MD5 to verify; skipping upload",
                remote_file,
                self.host,
            )
            return False

        try:
            actual = SW(self._dev).remote_checksum(
                remote_file, timeout=timeout, algorithm="md5"
            )
        except RpcError as err:
            self.log.warning(
                "Unable to compute MD5 of %s on %s (%s); uploading",
                remote_file,
                self.host,
                err,
            )
            return True

        if actual is None:
            self.log.info("%s not present on %s; uploading", remote_file, self.host)
            return True

        if actual.lower() == expected_md5.lower():
            self.log.info(
                "%s already present on %s with matching MD5; skipping upload",
                remote_file,
                self.host,
            )
            return False

        self.log.info(
            "%s present on %s but MD5 mismatch (remote %s != expected %s); re-uploading",
            remote_file,
            self.host,
            actual,
            expected_md5,
        )
        return True

    def _verify_md5(self, remote_file: str, expected_md5: str, timeout: int) -> None:
        """Compute the remote MD5 and compare against ``expected_md5``."""
        self.log.info("Verifying MD5 of %s on %s", remote_file, self.host)
        try:
            actual = SW(self._dev).remote_checksum(
                remote_file, timeout=timeout, algorithm="md5"
            )
        except RpcError as err:
            raise TransferError(
                "Unable to compute MD5 of %s: %s" % (remote_file, err)
            ) from err
        if actual is None:
            raise TransferError("Uploaded file %s not found on device" % remote_file)
        if actual.lower() != expected_md5.lower():
            self.log.error(
                "MD5 mismatch for %s: expected %s, got %s",
                remote_file,
                expected_md5,
                actual,
            )
            raise TransferError(
                "MD5 mismatch for %s (expected %s, got %s)"
                % (remote_file, expected_md5, actual)
            )
        self.log.info("MD5 verified for %s (%s)", remote_file, actual)

    def _backup_re_name(self) -> str:
        """Derive the backup RE name (the RE that is not the current master)."""
        master = str(self._dev.facts.get("master") or "RE0").upper()
        return "re1" if master == "RE0" else "re0"

    def _copy_to_backup_re(
        self, remote_file: str, backup_re: Optional[str]
    ) -> None:
        """Copy ``remote_file`` from the master RE to the backup RE."""
        if not self._dev.facts.get("2RE"):
            self.log.warning(
                "%s is not a dual-RE system; skipping copy to backup RE", self.host
            )
            return
        target_re = (backup_re or self._backup_re_name()).lower()
        dest = "%s:%s" % (target_re, remote_file)
        self.log.info("Copying %s to backup RE (%s)", remote_file, dest)
        try:
            ok = FS(self._dev).cp(remote_file, dest)
        except RpcError as err:
            raise TransferError(
                "Copy of %s to %s failed: %s" % (remote_file, dest, err)
            ) from err
        if ok is False:
            raise TransferError("Copy of %s to %s failed" % (remote_file, dest))
        self.log.info("Copied %s to backup RE (%s)", remote_file, target_re)


    # ------------------------------------------------------------------ #
    # Software upgrade
    # ------------------------------------------------------------------ #
    def upgrade(
        self,
        package: str,
        remote_path: str = "/var/tmp",
        validate: bool = True,
        no_copy: bool = False,
        reboot: bool = False,
        issu: bool = False,
        nssu: bool = False,
        vmhost: Optional[bool] = None,
        timeout: int = 1800,
        progress: bool = True,
        **install_kwargs: Any,
    ) -> Tuple[bool, str]:
        """Install a Junos software package.

        Copies (unless ``no_copy``/URL), optionally validates, and installs the
        package. Call with ``reboot=True`` to reboot automatically on success.

        :param package: Local path to the image, or a URL reachable by the device.
        :param remote_path: Remote directory used for the image copy.
        :param validate: Validate the package against the running config.
        :param no_copy: Assume the package already exists on the device.
        :param reboot: Reboot the device after a successful install.
        :param issu: Perform a unified in-service software upgrade.
        :param nssu: Perform a nonstop software upgrade.
        :param vmhost: Perform a vmhost software upgrade (``request vmhost software
            add``) instead of a classical Junos install. When ``None`` (default)
            this is auto-detected from the package name: a ``"vmhost"`` substring
            enables it. Pass ``True``/``False`` to force the behaviour explicitly.
        :param timeout: RPC timeout for the install operation (seconds).
        :param progress: When ``True`` progress is reported through the logger.
        :param install_kwargs: Extra keyword arguments forwarded to ``SW.install``.
        :returns: ``(status, message)`` where ``status`` is ``True`` on success.
        :raises UpgradeError: when the installation fails.
        """
        sw = SW(self._dev)
        # Collect the device-side progress reports so the detailed install
        # output (validation, disk-space checks, ...) can be surfaced to the
        # caller when the install fails instead of the generic RPC message.
        reports: list = []

        def _collecting_progress(_dev: Device, report: str) -> None:
            self.log.info("[%s] %s", self.host, report)
            reports.append(report)

        cb = _collecting_progress if progress else None
        if vmhost is None:
            vmhost = "vmhost" in os.path.basename(package).lower()
        self.log.info(
            "Starting software install of %s on %s (vmhost=%s)",
            package,
            self.host,
            vmhost,
        )

        def _with_detail(summary: str, err: Optional[BaseException] = None) -> str:
            lines: List[str] = [str(r) for r in reports if r and str(r).strip()]
            # The real failure reason (e.g. "Not enough free space in VirtFS")
            # usually lives in the RPC reply itself, not the progress callback.
            rsp = getattr(err, "rsp", None)
            if rsp is not None:
                try:
                    rsp_text = (
                        rsp
                        if isinstance(rsp, str)
                        else "".join(rsp.itertext())
                    )
                except Exception:  # noqa: BLE001
                    rsp_text = ""
                for line in rsp_text.splitlines():
                    if line.strip() and line.strip() not in lines:
                        lines.append(line.strip())
            detail = "\n".join(lines)
            return "%s\n\nDevice output:\n%s" % (summary, detail) if detail else summary

        try:
            status, message = sw.install(
                package=package,
                remote_path=remote_path,
                validate=validate,
                no_copy=no_copy,
                issu=issu,
                nssu=nssu,
                vmhost=vmhost,
                timeout=timeout,
                progress=cb,
                **install_kwargs,
            )
        except RpcError as err:
            self.log.error("Upgrade failed on %s: %s", self.host, err)
            raise UpgradeError(_with_detail(str(err), err)) from err

        if not status:
            self.log.error("Upgrade failed on %s: %s", self.host, message)
            raise UpgradeError(_with_detail(message or "software install failed"))

        self.log.info("Software install succeeded on %s: %s", self.host, message)
        if reboot:
            self.log.info("Rebooting %s to complete upgrade", self.host)
            if vmhost:
                sw.reboot(vmhost=True)
            else:
                self.reboot()
        return status, message

    # ------------------------------------------------------------------ #
    # Reboot (RE / FPC)
    # ------------------------------------------------------------------ #
    def reboot(
        self,
        target: str = "re",
        routing_engine: str = "both",
        fpc: int = 0,
        in_min: int = 0,
        at: Optional[str] = None,
        vmhost: bool = False,
    ) -> str:
        """Reboot a Routing Engine or restart an FPC.

        :param target: ``"re"`` to reboot the Routing Engine(s), or ``"fpc"`` to
            restart a specific FPC (line card).
        :param routing_engine: Which RE(s) to reboot when ``target == "re"``:

            * ``"master"`` -- reboot only the master (the connected RE).
            * ``"backup"`` -- reboot only the backup (the other RE).
            * ``"both"`` (default) -- reboot both REs.
        :param fpc: FPC slot number (used only when ``target == "fpc"``).
        :param in_min: Delay before the RE reboot (minutes).
        :param at: Absolute date/time for the RE reboot (Junos CLI syntax).
        :param vmhost: When ``True`` (and ``target == "re"``) perform a vmhost
            reboot (``request vmhost reboot``) instead of a plain RE reboot;
            required after a vmhost software install.
        :returns: The reboot/restart status message.
        :raises UpgradeError: when the operation fails.
        """
        target = target.lower()
        try:
            if target == "fpc":
                cmd = "request chassis fpc slot %d restart" % fpc
                self.log.info("Restarting FPC %d on %s", fpc, self.host)
                resp = self._dev.rpc.cli(cmd, format="text")
                return self._element_text(resp)
            if target == "re":
                re_sel = routing_engine.lower()
                if re_sel == "master":
                    all_re, other_re = False, False
                elif re_sel == "backup":
                    all_re, other_re = False, True
                elif re_sel == "both":
                    all_re, other_re = True, False
                else:
                    raise ValueError(
                        "routing_engine must be 'master', 'backup' or 'both', "
                        "got %r" % routing_engine
                    )
                self.log.info(
                    "Rebooting %s routing engine(s) on %s", re_sel, self.host
                )
                sw = SW(self._dev)
                return sw.reboot(
                    in_min=in_min, at=at, all_re=all_re, other_re=other_re,
                    vmhost=vmhost,
                )
            raise ValueError("target must be 're' or 'fpc', got %r" % target)
        except ValueError:
            raise
        except (RpcError, Exception) as err:
            self.log.error("Reboot failed on %s: %s", self.host, err)
            raise UpgradeError(str(err)) from err
