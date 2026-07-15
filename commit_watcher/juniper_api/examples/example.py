"""Example usage of the juniper_api library."""

import logging

from juniper_api import JuniperDevice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("demo")


def main() -> None:
    with JuniperDevice(
        host="192.0.2.1",
        user="admin",
        passwd="secret",
        logger=logger,
    ) as dev:
        # --- show command in different formats -----------------------------
        print(dev.show("show version", fmt="text"))

        version_xml = dev.show("show version", fmt="xml")
        hostnames = dev.filter(version_xml, "//host-name/text()")
        print("host-name:", hostnames)

        version_json = dev.show("show version", fmt="json")
        print("json keys:", list(version_json.keys()))

        # --- RE / FPC shell -------------------------------------------------
        print(dev.show_shell("ls -l /var/tmp", target="re"))
        print(dev.show_shell("show version", target="fpc", fpc=0))

        # --- configuration with dry-run (commit check) ---------------------
        diff = dev.edit_config(
            payload="set system host-name LAB-MX",
            fmt="set",
            mode="exclusive",
            dry_run=True,
        )
        print("would change:\n", diff)

        # --- real commit, ignoring warnings --------------------------------
        # edit_config returns the *before-commit* diff (what was committed).
        before = dev.edit_config(
            payload="set system host-name LAB-MX",
            fmt="set",
            mode="private",
            ignore_warning=True,
            comment="set hostname via juniper_api",
        )
        print("diff before commit:\n", before)

        # --- diff after commit (what the latest commit changed) ------------
        print("diff after commit:\n", dev.show_diff(mode="committed"))

        # --- diff before commit (uncommitted candidate vs running) ---------
        # (meaningful when changes are loaded but not yet committed)
        print("pending candidate diff:\n", dev.show_diff(mode="candidate"))

        # --- upload + upgrade + reboot -------------------------------------
        # dev.upload(
        #     "/images/junos-install.tgz",
        #     remote_path="/var/tmp",
        #     method="scp",
        #     md5="d41d8cd98f00b204e9800998ecf8427e",  # verified on the device
        #     copy_to_backup=True,                       # master RE -> backup RE
        # )
        # dev.upgrade("/images/junos-install.tgz", validate=True, reboot=True)
        #
        # dev.reboot(target="re", routing_engine="master")  # connected RE only
        # dev.reboot(target="re", routing_engine="backup")  # other RE only
        # dev.reboot(target="re", routing_engine="both")    # both REs
        # dev.reboot(target="fpc", fpc=2)                    # restart FPC 2


if __name__ == "__main__":
    main()
