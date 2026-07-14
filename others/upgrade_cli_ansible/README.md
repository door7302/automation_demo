# Juniper maintenance playbooks

Simple Ansible playbooks built on the **official Juniper collection**
(`juniper.device`, PyEZ / NETCONF) to run common maintenance actions on
Junos and Junos-Evolved devices.

## Layout

```
ansible.cfg
inventory.yml                 # device_name / ansible_host / user / password / model / software
group_vars/junos_devices.yml  # connection + default options
playbooks/                    # one playbook per action
state/<device_name>/*.json    # auto-generated rollback state (git-ignore it)
```

## Install

A modern `ansible-core` (**>= 2.16.6**) and a specific set of Python deps are
required. The `juniper.device.pyez` connection is broken on ansible-core
<= 2.15 (`'Connection' object has no attribute 'nonetype'`,
[ansible/ansible#82954](https://github.com/ansible/ansible/pull/82954)) and only
fixed in 2.16.6 — the steps below use 2.17. The ancient distro Ansible
(2.10) is **not** compatible with the `juniper.device` collection, and
`pyparsing` **must be >= 3**.

```bash
sudo apt-get remove -y ansible || true      # remove distro Ansible 2.10 if present
sudo python3 -m pip install --upgrade pip
sudo python3 -m pip install \
  "ansible-core>=2.17,<2.18" \
  "junos-eznc>=2.7.2" jxmlease xmltodict lxml ncclient "pyparsing>=3" paramiko scp
hash -r
ansible-galaxy collection install -r requirements.yml --upgrade
```

Then edit `inventory.yml` with your real devices. Connection details come from
`group_vars/junos_devices.yml` (it sets `ansible_host: "{{ device_name }}"`, so
`device_name` must be a resolvable hostname/FQDN), with `user` / `password`
supplied per host or via `-e`.

Quick test:

```bash
ansible-playbook playbooks/check_node.yml \
  -e target_hosts=<host> -e user=<user> -e password=<pw>
```

## Guided upgrade

To run the full upgrade sequence (pre-checks → drain → upload → upgrade →
reboot → post-checks/restore) as a single, gated `ansible-playbook` run, use
the umbrella playbook `playbooks/guided_upgrade.yml`. See
[GUIDED_UPGRADE.md](GUIDED_UPGRADE.md) for the details.

## Actions

| Playbook | What it does |
|---|---|
| `check_node.yml` | Wait until the device is reachable over NETCONF (TCP/830 + live RPC), polling every 5s up to 30 min. |
| `check_routing_engines.yml` | Poll `show chassis routing-engine` until every RE is `OK` (every 30s, up to 30 min). |
| `check_fpc_online.yml` | Poll `show chassis fpc` until all present FPCs are `Online` (max 30s); Empty slots ignored. |
| `check_replication_state.yml` | Poll GRES/NSR replication until every protocol from `show task replication` is `Complete` and `show bgp replication` route-sync is `Complete`. |
| `shut_interfaces.yml` | Disable every admin-up interface; saves the list for rollback. |
| `unshut_interfaces.yml` | Re-enable the interfaces saved above. |
| `set_isis_overload.yml` | Save current overload config, then set ISIS overload (+optional flags). |
| `restore_isis_overload.yml` | Restore the overload config saved above. |
| `deactivate_bgp_groups.yml` | Deactivate active BGP groups; saves the list. |
| `activate_bgp_groups.yml` | Re-activate the BGP groups saved above. |
| `deactivate_ri.yml` | Deactivate active routing-instances; saves the list. |
| `activate_ri.yml` | Re-activate the routing-instances saved above. |
| `deactivate_gres_nsr.yml` | Deactivate GRES / NSR / NSB knobs that are present; saves the list. |
| `activate_gres_nsr.yml` | Re-activate the HA knobs saved above. |
| `upload_release.yml` | Upload a release image to `/var/tmp`, idempotent via its `.md5` (skips if already present + matching), 30-min transfer cap. |
| `upgrade_software.yml` | Install a staged image (no reboot). Auto-detects EVO (both REs) vs Junos (`re_target`), and junos vs vmhost from the filename. |
| `reboot.yml` | Reboot the device or a given RE (`re_target: both\|local\|other`), optional `vmhost`, `in_min`/`at`. |
| `snapshot.yml` | Capture `show` command outputs (external command file) into a labelled snapshot. |
| `diff_snapshots.yml` | Diff two snapshots (e.g. before/after) and raise the differences. |

The umbrella playbook `playbooks/guided_upgrade.yml` (see
[GUIDED_UPGRADE.md](GUIDED_UPGRADE.md)) chains these together with interactive
approval gates. State files under `state/<device_name>/` are how each "undo"
playbook knows what to revert. They are merged across runs, so running a
`*deactivate*` / `shut` playbook several times accumulates the full list.

## Examples

```bash
# reachability
ansible-playbook playbooks/check_node.yml
ansible-playbook playbooks/check_node.yml --limit router1

# stateful replication (GRES/NSR) must be Complete
ansible-playbook playbooks/check_replication_state.yml

# shut interfaces but keep two of them up
ansible-playbook playbooks/shut_interfaces.yml \
    -e '{"interface_whitelist": ["ge-0/0/0", "lo0"]}'
ansible-playbook playbooks/unshut_interfaces.yml

# ISIS overload with extra options (all default false)
ansible-playbook playbooks/set_isis_overload.yml \
    -e overload_advertise_high_metrics=true -e overload_internal_prefixes=true
ansible-playbook playbooks/restore_isis_overload.yml

# BGP groups, keeping some active
ansible-playbook playbooks/deactivate_bgp_groups.yml -e '{"bgp_group_whitelist": ["CORE"]}'
ansible-playbook playbooks/activate_bgp_groups.yml

# routing-instances
ansible-playbook playbooks/deactivate_ri.yml -e '{"ri_whitelist": ["mgmt_junos"]}'
ansible-playbook playbooks/activate_ri.yml

# GRES / NSR / NSB
ansible-playbook playbooks/deactivate_gres_nsr.yml
ansible-playbook playbooks/activate_gres_nsr.yml
```

## Troubleshooting

- **`'Connection' object has no attribute 'nonetype'`** — the
  `juniper.device.pyez` connection running on **ansible-core <= 2.15**. It's an
  ansible-core bug fixed only in **2.16.6**
  ([ansible/ansible#82954](https://github.com/ansible/ansible/pull/82954)).
  Install ansible-core 2.17 (as above) and don't run against the old distro
  Ansible (2.10).
- **`'JuniperJunosModule' object has no attribute '_pyez_conn'`** — the effective
  connection resolved to `netconf`/`local` instead of pyez. Fixed by the explicit
  `connection: juniper.device.pyez` on every play.
- **`Invalid callback for stdout specified: yaml`** — `ansible.cfg` sets
  `stdout_callback = yaml` (`community.general`). If that collection isn't
  installed, either install it or run with `-e ANSIBLE_STDOUT_CALLBACK=default`.
