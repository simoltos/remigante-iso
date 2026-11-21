"""Reinstall into the standard Btrfs layout with the existing home subvolume."""

from __future__ import annotations

import filecmp
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .command import capture_identifier
from .context import InstallContext
from .ui import info

ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
SUBVOLUMES = {"/": "@", "/home": "@home", "/var/log": "@log", "/var/cache/pacman/pkg": "@pkg"}
BASE_SYSTEM = Path("/run/remigante-base/system")
WORKSPACE = ".remigante-reinstall"
PENDING_FACTORY = ".remigante-new-factory"
MAPPER = "omarchy_root"


def _partitions(disk: str) -> list[dict]:
    output = capture_identifier(
        ["lsblk", "--list", "--json", "--paths", "-o",
         "PATH,TYPE,PKNAME,FSTYPE,UUID,PARTUUID,PARTTYPE,MOUNTPOINTS", "--", disk],
        f"partitions of {disk}",
    )
    return [row for row in json.loads(output)["blockdevices"]
            if row["type"] == "part" and row["pkname"] == disk and row["partuuid"]]


def _is_esp(part: dict) -> bool:
    return part["fstype"] == "vfat" and (part["parttype"] or "").lower() == ESP_TYPE


def _available(part: dict) -> bool:
    holders = Path("/sys/class/block") / Path(part["path"]).name / "holders"
    return not any(part["mountpoints"] or []) and not any(holders.iterdir())


def candidates(disk: str) -> list[dict]:
    partitions = _partitions(disk)
    if not Path("/sys/firmware/efi").is_dir() or not any(map(_is_esp, partitions)):
        return []
    return [part for part in partitions if part["fstype"] in {"btrfs", "crypto_LUKS"} and _available(part)]


def _open_root(part: dict, password: bytes, mapper: str, *, readonly: bool) -> str:
    if part["fstype"] == "btrfs":
        return part["path"]
    if not password:
        raise RuntimeError("Enter the existing LUKS passphrase.")
    args = ["cryptsetup", "open", "--type", "luks2", "--key-file", "-"]
    if readonly:
        args.append("--readonly")
    result = subprocess.run(
        [*args, part["path"], mapper], input=password, capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"Could not unlock {part['path']}: {os.fsdecode(result.stderr).strip()}")
    return f"/dev/mapper/{mapper}"


@contextmanager
def _mount(device: str, path: Path, fstype: str, options: str):
    subprocess.run(["mount", "-t", fstype, "-o", options, device, path], check=True)
    try:
        yield path
    finally:
        subprocess.run(["umount", path], check=True)


def _subvolume(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and path.stat().st_ino == 256


def _subvolume_id(path: Path) -> int:
    return int(capture_identifier(
        ["btrfs", "inspect-internal", "rootid", str(path)], f"the subvolume ID of {path}"
    ))


def _matches(source: str, uuid: str, partuuid: str = "") -> bool:
    return bool(uuid and source.casefold() == f"UUID={uuid}".casefold()) or bool(
        partuuid and source.casefold() == f"PARTUUID={partuuid}".casefold()
    )


def _inspect(top: Path, fs_uuid: str, partitions: list[dict], username: str) -> dict:
    for name in (WORKSPACE, PENDING_FACTORY):
        if (top / name).exists() or (top / name).is_symlink():
            raise RuntimeError(f"A previous reinstall left {name} on this filesystem.")
    if not all(_subvolume(top / name) for name in SUBVOLUMES.values()):
        raise RuntimeError("This disk does not have the standard Remigante Btrfs layout.")
    etc = top / "@" / "etc"
    if etc.is_symlink() or any((etc / name).is_symlink() for name in ("fstab", "passwd")):
        raise RuntimeError("Could not verify the existing system's mount and account configuration.")

    mounts = {}
    for line in (etc / "fstab").read_text().splitlines():
        fields = line.partition("#")[0].split()
        if len(fields) < 4:
            continue
        source, target, fstype, options, *_ = fields
        if target in mounts:
            raise RuntimeError(f"The existing mount point {target} is ambiguous.")
        if target in SUBVOLUMES or target in {"/boot", "/efi"}:
            mounts[target] = source, fstype, options.split(",")
        elif target.startswith("/home/"):
            raise RuntimeError("There are separate mounts inside /home.")
    for target, name in SUBVOLUMES.items():
        source, fstype, options = mounts.get(target, ("", "", []))
        if fstype != "btrfs" or not _matches(source, fs_uuid) or not {
            f"subvol={name}", f"subvol=/{name}",
        }.intersection(options):
            raise RuntimeError(f"{target} does not use the expected Btrfs subvolume {name}.")

    efi = [
        (target, part) for target in ("/boot", "/efi") for part in partitions
        if _is_esp(part) and _available(part) and target in mounts
        and mounts[target][1] == "vfat" and _matches(mounts[target][0], part["uuid"], part["partuuid"])
    ]
    if len(efi) != 1:
        raise RuntimeError("Could not identify the existing system's EFI partition on this disk.")
    esp_mount, esp = efi[0]

    for line in (etc / "passwd").read_text().splitlines():
        account = line.split(":")
        if len(account) == 7 and account[0] == username and account[5] == f"/home/{username}":
            uid, gid = (int(number) for number in account[2:4])
            home = top / "@home" / username
            if uid >= 1000 and gid >= 1000 and not home.is_symlink() and home.is_dir() and home.stat().st_uid == uid:
                break
    else:
        raise RuntimeError(f"Choose the username that already owns /home/{username} on this disk.")

    return {"esp_device": esp["path"], "esp_partuuid": esp["partuuid"],
            "esp_mount": esp_mount, "uid": uid, "gid": gid,
            "home_id": _subvolume_id(top / "@home")}


def _probe(disk: str, part: dict, device: str, username: str) -> dict:
    fs_uuid = capture_identifier(["blkid", "-p", "-s", "UUID", "-o", "value", device], "the Btrfs UUID")
    with tempfile.TemporaryDirectory(prefix="remigante-preserve-", dir="/run") as directory:
        path = Path(directory)
        with _mount(device, path, "btrfs", "ro,rescue=nologreplay,subvolid=5"):
            details = capture_identifier(
                ["env", "LC_ALL=C", "btrfs", "filesystem", "show", "--raw", str(path)],
                "the Btrfs layout",
            )
            if not any(line.split()[:3] == ["Total", "devices", "1"] for line in details.splitlines()):
                raise RuntimeError("Reinstalling needs a Btrfs filesystem on a single partition.")
            plan = _inspect(path, fs_uuid, _partitions(disk), username)
        with _mount(plan["esp_device"], path, "vfat", "ro"):
            arch = "aa64" if platform.machine() == "aarch64" else "x64"
            loader = path / f"EFI/limine/limine_{arch}.efi"
            fallback = path / f"EFI/BOOT/BOOT{arch.upper()}.EFI"
            if not loader.is_file() or not (path / "limine.conf").is_file():
                raise RuntimeError("The selected EFI partition does not hold the existing Limine installation.")
            plan["enable_fallback"] = not fallback.exists() or filecmp.cmp(loader, fallback, shallow=False)
    return {"disk": disk, "root_device": part["path"], "root_partuuid": part["partuuid"],
            "luks_uuid": part["uuid"] if part["fstype"] == "crypto_LUKS" else None,
            "fs_uuid": fs_uuid, **plan}


def _selected_partition(disk: str, device: str) -> dict:
    for part in candidates(disk):
        if part["path"] == device:
            return part
    raise RuntimeError(f"{device} is not an available Btrfs or LUKS partition on {disk}.")


def probe(disk: str, device: str, username: str, password: bytes) -> dict:
    part = _selected_partition(disk, device)
    mapper = f"remigante-probe-{os.getpid()}"
    root_device = _open_root(part, password, mapper, readonly=True)
    try:
        return _probe(disk, part, root_device, username)
    finally:
        if root_device != part["path"]:
            subprocess.run(["cryptsetup", "close", mapper], check=True)


class Reinstall:
    """Stage a new root, keeping the original @ and @home separate from setup."""

    def __init__(self, ctx: InstallContext):
        self.ctx = ctx
        self.top = ctx.state_dir / "reinstall-top"
        self.workspace = self.top / WORKSPACE
        self.root = self.workspace / "root"
        # Btrfs can rename a read-only subvolume within the same directory.
        self.factory = self.top / PENDING_FACTORY
        self.esp_backup = self.workspace / "efi-backup"
        self.resources = ExitStack()
        self.owns_workspace = False
        self.backed_up_esp = False
        self.opened_mapper = False
        self.mounted_target = False
        self.activated = False

    def prepare(self) -> None:
        ctx = self.ctx
        expected = ctx.omarchy_install.get("preserve_home")
        disk_config = ctx.user_configuration.get("disk_config") or {}
        if not isinstance(expected, dict) or ctx.defer_provisioning or disk_config.get("config_type") != "pre_mounted_config":
            raise RuntimeError("Invalid configuration for reinstalling with the existing home.")
        if subprocess.run(["mountpoint", "-q", ctx.target], check=False).returncode == 0:
            raise RuntimeError(f"Installation target {ctx.target} is already mounted.")
        subprocess.run(
            ["findmnt", "-rn", "-t", "btrfs", "--mountpoint", BASE_SYSTEM],
            stdout=subprocess.DEVNULL, check=True,
        )

        part = _selected_partition(expected["disk"], expected["root_device"])
        if ctx.encrypt != (part["fstype"] == "crypto_LUKS"):
            raise RuntimeError("The existing disk's encryption does not match the confirmed reinstall.")
        password = ctx.user_credentials.get("encryption_password", "").encode()
        device = _open_root(part, password, MAPPER, readonly=False)
        self.opened_mapper = device != part["path"]
        self.plan = _probe(expected["disk"], part, device, ctx.username)
        if self.plan != expected:
            raise RuntimeError("The existing installation changed after confirmation. Select it again.")

        ctx.omarchy_install["boot"] = {
            "esp_mount": self.plan["esp_mount"], "enable_fallback": self.plan["enable_fallback"],
        }
        ctx.omarchy_install["storage"] = {
            "root_device": part["path"], "root_mapper": device, "luks_uuid": self.plan["luks_uuid"],
            "esp_device": self.plan["esp_device"], "kernel": ctx.user_configuration["kernels"][0],
        }
        self.esp = ctx.target / self.plan["esp_mount"].lstrip("/")
        self.top.mkdir(parents=True, exist_ok=True)
        self.resources.enter_context(_mount(device, self.top, "btrfs", "noatime,compress=zstd,subvolid=5"))
        factory = self.top / "@factory"
        if (factory.exists() or factory.is_symlink()) and not _subvolume(factory):
            raise RuntimeError("The existing @factory path is not a Btrfs subvolume.")
        self.workspace.mkdir(mode=0o700)
        self.owns_workspace = True

        info("› copying the installation image into a new Btrfs subvolume")
        subprocess.run(["btrfs", "subvolume", "create", self.root], check=True)
        subprocess.run(["cp", "-a", "--reflink=auto", "--sparse=always", f"{BASE_SYSTEM}/.", self.root], check=True)
        self.esp_backup.mkdir()
        with tempfile.TemporaryDirectory(prefix="remigante-efi-", dir="/run") as directory:
            with _mount(self.plan["esp_device"], Path(directory), "vfat", "ro") as old_esp:
                subprocess.run(["rsync", "-rt", f"{old_esp}/", self.esp_backup], check=True)
        subprocess.run(["sync", "-f", self.workspace], check=True)

        ctx.target.mkdir(parents=True, exist_ok=True)
        options = "noatime,compress=zstd,subvol="
        subprocess.run(["mount", "-t", "btrfs", "-o", options + str(self.root.relative_to(self.top)),
                        device, ctx.target], check=True)
        self.mounted_target = True
        # useradd and package hooks can populate /home during setup. Keep
        # that temporary skeleton out of the root and its factory snapshot.
        subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0755", "tmpfs", ctx.target / "home"], check=True)
        for dest in ("/var/log", "/var/cache/pacman/pkg"):
            path = ctx.target / dest.lstrip("/")
            path.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", "-t", "btrfs", "-o", options + SUBVOLUMES[dest],
                            device, path], check=True)
        self.esp.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", "-t", "vfat", "-o", "umask=0077", self.plan["esp_device"], self.esp], check=True)
        self.backed_up_esp = True

    def restore_account_ids(self) -> None:
        target = str(self.ctx.target)
        username = self.ctx.username
        uid = str(self.plan["uid"])
        gid = str(self.plan["gid"])
        subprocess.run(["arch-chroot", target, "groupmod", "-g", gid, "--", username], check=True)
        subprocess.run(["arch-chroot", target, "usermod", "-u", uid, "-g", gid, "--", username], check=True)

    def activate(self) -> None:
        home = self.ctx.target / "home"
        device = self.ctx.omarchy_install["storage"]["root_mapper"]
        subprocess.run(["umount", home], check=True)
        subprocess.run(["mount", "-t", "btrfs", "-o", "noatime,compress=zstd,subvol=@home", device, home], check=True)
        if _subvolume_id(home) != self.plan["home_id"]:
            raise RuntimeError("The mounted home no longer matches the confirmed installation.")

        old_root = self.top / "@"
        old_factory = self.top / "@factory"
        exchange = ["mv", "--exchange", "--no-copy", "-T", "--"]
        new_id = _subvolume_id(self.root)
        new_factory_id = _subvolume_id(self.factory)
        subprocess.run(["sync", "-f", self.esp], check=True)
        subprocess.run(["sync", "-f", self.top], check=True)
        had_factory = old_factory.exists()
        try:
            if had_factory:
                subprocess.run([*exchange, self.factory, old_factory], check=True)
            else:
                self.factory.rename(old_factory)
            subprocess.run([*exchange, self.root, old_root], check=True)
        finally:
            self.activated = _subvolume_id(old_root) == new_id
            if not self.activated and old_factory.exists() and _subvolume_id(old_factory) == new_factory_id:
                if had_factory:
                    subprocess.run([*exchange, self.factory, old_factory], check=True)
                else:
                    old_factory.rename(self.factory)
        subprocess.run(["sync", "-f", self.top], check=True)

    def cleanup(self) -> None:
        try:
            if self.backed_up_esp and not self.activated:
                info("› restoring the existing EFI files after the failed reinstall")
                subprocess.run(["rsync", "-rt", "--ignore-times", "--delete", f"{self.esp_backup}/", self.esp], check=True)
                subprocess.run(["sync", "-f", self.esp], check=True)
            if self.mounted_target and not self.activated:
                subprocess.run(["omarchy-release-install-target", "--keep-mappers", self.ctx.target], check=True)
            if self.owns_workspace:
                for subvolume in (self.factory, self.root):
                    if subvolume.exists():
                        subprocess.run(["btrfs", "subvolume", "delete", "--recursive", subvolume], check=True)
                if self.esp_backup.exists():
                    shutil.rmtree(self.esp_backup)
                self.workspace.rmdir()
        finally:
            self.resources.close()
            # The normal dashboard release unmounts the finished target.
            if self.opened_mapper and not self.activated:
                subprocess.run(["cryptsetup", "close", MAPPER], check=True)


def main() -> int:
    try:
        action, disk, *args = sys.argv[1:]
        if action == "candidates":
            for part in candidates(disk):
                print(part["path"], part["fstype"], sep="\t")
        elif action == "probe":
            device, username = args
            print(json.dumps(probe(disk, device, username, sys.stdin.buffer.read())))
        else:
            raise ValueError("Expected candidates or probe.")
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
