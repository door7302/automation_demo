"""Example: read a 'show' command's output without writing XPath by hand.

The workflow is:
  1. Run a show command requesting XML output  -> dev.show(cmd, fmt="xml")
  2. Pick values out of it with dev.select(...)  -> no XPath, no text(),
     no normalize-space() and no .strip() needed.

`dev.select(xml, element, field=..., where=..., first=...)` builds the XPath
for you and returns clean, whitespace-normalized strings. Use the lower-level
`dev.filter(xml, xpath)` only when you need raw nodes or a custom XPath.
"""

import logging

from juniper_api import JuniperDevice

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("filter-demo")


def main() -> None:
    with JuniperDevice(
        host="192.0.2.1",
        user="admin",
        passwd="secret",
        logger=logger,
    ) as dev:
        # ----------------------------------------------------------------- #
        # 1) "show interfaces terse" -> list every physical interface name
        # ----------------------------------------------------------------- #
        # XML returned looks like:
        #   <interface-information>
        #     <physical-interface>
        #       <name>ge-0/0/0</name>
        #       <admin-status>up</admin-status>
        #       <oper-status>up</oper-status>
        #       ...
        intf_xml = dev.show("show interfaces terse", fmt="xml")

        # Name of every <physical-interface>. No text()/strip() needed.
        names = dev.select(intf_xml, "physical-interface", "name")
        print("Interfaces:", names)

        # ----------------------------------------------------------------- #
        # 2) Only the interfaces whose oper-status is "down"
        # ----------------------------------------------------------------- #
        # `where` builds the normalize-space(...) match predicate for you.
        down = dev.select(
            intf_xml,
            "physical-interface",
            "name",
            where={"oper-status": "down"},
        )
        print("Down interfaces:", down)

        # ----------------------------------------------------------------- #
        # 3) Get whole elements, then read child fields in Python
        #    (use the lower-level filter() when you need raw nodes)
        # ----------------------------------------------------------------- #
        phys = dev.filter(intf_xml, "//physical-interface")
        for intf in phys:
            name = intf.findtext("name", default="").strip()
            oper = intf.findtext("oper-status", default="").strip()
            print(f"{name:<14} oper={oper}")

        # ----------------------------------------------------------------- #
        # 4) "show version" -> pull a single value
        # ----------------------------------------------------------------- #
        ver_xml = dev.show("show version", fmt="xml")
        # first=True returns a single value (or None) instead of a list.
        hostname = dev.select(ver_xml, "host-name", first=True)
        junos = dev.select(ver_xml, "junos-version", first=True)
        print("host-name:", hostname or "?")
        print("junos-version:", junos or "?")

        # ----------------------------------------------------------------- #
        # 5) "show bgp summary" -> peers that are NOT Established
        # ----------------------------------------------------------------- #
        bgp_xml = dev.show("show bgp summary", fmt="xml")
        not_estab = dev.select(
            bgp_xml,
            "bgp-peer",
            "peer-address",
            where={"peer-state": ("!startswith", "Established")},
        )
        print("BGP peers not Established:", not_estab)


if __name__ == "__main__":
    main()
