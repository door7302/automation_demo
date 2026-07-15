# juniper-api

High-level NETCONF management library for Juniper devices, built on top of
[Junos PyEZ](https://github.com/Juniper/py-junos-eznc).

It exposes a small, friendly API on top of PyEZ:

- `show()` — run operational commands (text, XML or JSON output).
- `select()` — read clean text values from XML **without writing XPath by hand**.
- `filter()` — apply a raw XPath (XML) or `/`-separated path (JSON) when you need nodes.
- `show_shell()` — run RE or FPC shell commands.
- `edit_config()` — load and commit configuration (with dry-run support).
- `show_diff()` — compare two commit/rollback points.
- `upload()` / `upgrade()` / `reboot()` — file transfer and device maintenance.

## Requirements

- Python 3.8+
- [junos-eznc](https://pypi.org/project/junos-eznc/) (`>=2.7.0`)
- [lxml](https://pypi.org/project/lxml/) (`>=4.6.0`)

## Quick example

See [example.py](./examples/example.py) and [example_filter.py](examples/example_filter.py) for more.

```python
import logging
from juniper_api import JuniperDevice

logging.basicConfig(level=logging.INFO)

with JuniperDevice(
    host="192.0.2.1",
    user="admin",
    passwd="secret",
) as dev:
    # Run a command and get plain text
    print(dev.show("show version"))

    # Request XML and pull clean values without writing XPath
    intf_xml = dev.show("show interfaces terse", fmt="xml")

    # Every physical interface name
    names = dev.select(intf_xml, "physical-interface", "name")
    print("Interfaces:", names)

    # Names of interfaces whose oper-status is "down"
    down = dev.select(
        intf_xml, "physical-interface", "name",
        where={"oper-status": "down"},
    )
    print("Down:", down)

    # A single value
    ver_xml = dev.show("show version", fmt="xml")
    print("host-name:", dev.select(ver_xml, "host-name", first=True))
```