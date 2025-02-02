#!/usr/bin/env python3
import argparse
import sys
from types import SimpleNamespace

from middlewared.plugins.system.dmi import DMIDecode
from middlewared.plugins.truenas import get_chassis_hardware
from middlewared.plugins.tunables import zfs_parameter_value
from middlewared.utils.db import query_table, update_table

KiB = 1024 ** 1
MiB = 1024 ** 2
GiB = 1024 ** 3

MIN_ZFS_RESERVED_MEM = 1 * GiB

zfs_parameters = {}


def zfs_parameter(tunable_name):
    def decorator(func):
        zfs_parameters[tunable_name] = func
        return func

    return decorator


@zfs_parameter("zfs_dirty_data_max_max")
def guess_vfs_zfs_dirty_data_max_max(context):
    if context.hardware.startswith("M"):
        return 12 * GiB
    else:
        return None


@zfs_parameter("l2arc_noprefetch")
def guess_vfs_zfs_l2arc_noprefetch(context):
    return 0


@zfs_parameter("l2arc_write_max")
def guess_vfs_zfs_l2arc_write_max(context):
    return 10000000


@zfs_parameter("l2arc_write_boost")
def guess_vfs_zfs_l2arc_write_boost(context):
    return 40000000


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-unknown", action="store_true")
    args = parser.parse_args()

    dmi = DMIDecode().info()
    chassis_hardware = get_chassis_hardware(dmi).removeprefix("TRUENAS-").split("-")[0]

    if args.skip_unknown and chassis_hardware == "UNKNOWN":
        sys.exit(0)

    context = SimpleNamespace(hardware=chassis_hardware)

    recommendations = {}
    for knob, func in zfs_parameters.items():
        retval = func(context)
        if retval is None:
            continue

        recommendations[knob] = str(retval)

    overwrite = False
    changed_values = False
    qs = {i["var"]: i for i in query_table("system_tunable", prefix="tun_")}
    for var, value in recommendations.items():
        if tunable := qs.get(var, {}):
            if not overwrite:
                # Already exists and we're honoring the user setting. Move along.
                continue
            elif tunable["value"] == value:
                # We bail out here because if we set a value to what the database
                # already has we'll set changed_values = True which will
                # cause the system to be rebooted.
                continue

        comment = "Generated by autotune"
        if id_ := tunable.pop("id", None):
            update_table("UPDATE system_tunable SET tun_value = ?, tun_comment = ? WHERE id = ?", (value, comment, id_))
        else:
            orig_value = zfs_parameter_value(var)
            update_table(
                "INSERT INTO system_tunable (tun_type, tun_var, tun_value, tun_orig_value, tun_comment, tun_enabled)"
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("ZFS", var, value, orig_value, comment, 1)
            )

        # If we got this far, that means the database save went through just
        # fine at least once.
        changed_values = True

    for tunable in qs.values():
        if tunable["comment"] == "Generated by autotune":
            if tunable["var"] in ["zfs_arc_max", "zfs_vdev_async_read_max_active", "zfs_vdev_sync_read_max_active",
                                  "zfs_vdev_async_write_max_active", "zfs_vdev_sync_write_max_active",
                                  "zfs_vdev_sync_write_max_active"]:
                update_table("DELETE FROM system_tunable WHERE id = ?", (tunable["id"],))
                changed_values = True

    if changed_values:
        sys.exit(2)
