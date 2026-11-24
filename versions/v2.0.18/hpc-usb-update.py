#!/usr/bin/python3
"""Install an HMI release from a USB drive, after confirmation on the panel.

A drive holding updates has a copy of the hpc-releases repository at its root,
as hpc-releases/ (the checkout copied as-is) or, from older drives, releases/:
CURRENT names a folder in versions/. A drive can also hold the setup kit
(HPC_SETUP/) and logs (HPC_LOGS/); none of them interact.

The release CURRENT names is offered whenever it isn't the installed one, older
or newer, so a drive can also roll a panel back. Every breaking change between
the two releases (BREAKING_CHANGES.json in HPC_Firmware) is shown as a warning
first, and a change only a full USB kit can make is refused. The list comes from
the release's manifest, the installed release's manifest and the copy the USB kit
installed, so a rollback to a release published before a change still sees it.

stock-hmi.py installs this as /usr/local/sbin/hpc-usb-update, run as root by:
  --scan DEVICE  a drive was plugged in (udev): copy its release here and verify it
  --apply        the panel asked to install: swap the release in, watching the app
  --boot         every boot, before the app: finish whatever a power cut interrupted

The panel reads STATUS_FILE and creates INSTALL_REQUEST. Only the app layer is
replaced here; a release that needs more says so in requires_setup, and is then
refused with a message to use a full USB kit (stock-hmi.py).

A release that ships its own updater (the manifest's updater component) installs
it in place of this one, with the app, and it is rolled back with the app.
"""

from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import subprocess
import sys
import time

APP_USER = "nwhs"
APP_SERVICE = "hmi.service"
APP_DIR = Path(f"/home/{APP_USER}/hpc-hmi/bin")
PREVIOUS_APP_DIR = APP_DIR.with_name("bin.previous")
SERVICE_FILE = Path("/etc/systemd/system/hmi.service")
POLKIT_RULES_FILE = Path("/etc/polkit-1/rules.d/40-hpc-hmi-networkmanager.rules")
RELEASE_INFO_FILE = Path("/etc/hpc-release")
# This script, as stock-hmi.py installs it; a release's updater component replaces it
UPDATER_FILE = Path("/usr/local/sbin/hpc-usb-update")
# Installed by stock-hmi.py from the kit's BREAKING_CHANGES.json
BREAKING_CHANGES_FILE = Path("/usr/local/share/hpc-hmi/breaking-changes.json")

# Root-only: hmi.service is installed from here, and runs what it says.
UPDATE_DIR = Path("/var/lib/hpc-hmi/update")
STAGE_DIR = UPDATE_DIR / "stage"
PREVIOUS_SYSTEM_DIR = UPDATE_DIR / "previous-system"
# Exists from before the app is stopped until the new one has proved itself.
APPLYING_MARKER = UPDATE_DIR / "applying"
LOCK_FILE = UPDATE_DIR / "lock"

# Not /run/hpc, which goes away whenever hmi.service stops. Writable by the app,
# which is why nothing in here is trusted beyond "the panel asked to install".
RUN_DIR = Path("/run/hpc-update")
STATUS_FILE = RUN_DIR / "status.json"
INSTALL_REQUEST = RUN_DIR / "install"
MOUNT_DIR = RUN_DIR / "mount"

# The app mounts a FAT drive automount_grace_ms (3 s by default) after seeing it.
# Linux refuses a read-only and a read-write mount of the same drive at once, so
# read the release through the app's mount. While the app is running, wait for
# that mount, and only mount here if it never comes (log drives disabled, or the
# app can't mount) or the app isn't running. hpc-usb-update@.service is ordered
# after hmi.service, so at boot this wait starts once the app has started.
# The app takes priority: it detaches a read-only mount made here when it wants
# the drive for logging, and the scan then carries on through the app's mount.
APP_MOUNT_WAIT_SECONDS = 60
APP_RUNNING_STATES = ("active", "activating", "reloading")
# How long the new app must stay up without systemd restarting it.
HEALTH_CHECK_SECONDS = 30
DEBS_TARGET = "bookworm-arm64"
# Where a drive's releases are, in order: a copy of the hpc-releases checkout, then the folder older drives used
RELEASE_FOLDERS = ("hpc-releases", "releases")
RELEASE_NAME_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-RC(\d+))?$")


class UpdateError(Exception):
    """A release can't be used. The message is shown on the panel, so keep it short."""


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    logging.info("Running: %s", " ".join(command))
    return subprocess.run(command, check=check, text=True, capture_output=True)


def write_status(state: str, version: str = "", message: str = "", offer: dict | None = None) -> None:
    """Tell the panel. States: preparing, ready, blocked, installing, installed, failed.

    A ready status also carries the offer: installed (release name, or ""), direction (upgrade,
    downgrade or unknown) and warnings (texts to show before installing)."""
    logging.info("Status %s %s %s %s", state, version, message, offer or "")
    try:
        app_gid = pwd.getpwnam(APP_USER).pw_gid
    except KeyError:
        app_gid = -1
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(RUN_DIR, 0, app_gid)
    RUN_DIR.chmod(0o775)
    temporary = STATUS_FILE.with_name(f".{STATUS_FILE.name}.tmp")
    temporary.write_text(
        json.dumps({"state": state, "version": version, "message": message, **(offer or {})}), encoding="utf-8"
    )
    temporary.chmod(0o644)
    os.replace(temporary, STATUS_FILE)


def calculate_sha256(filename: Path) -> str:
    digest = hashlib.sha256()
    with filename.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assets_sha256(assets_dir: Path) -> str:
    """Same as HPC_Firmware tools/create_release.sh: a SHA-256 of the sorted sha256sum list."""
    listing = hashlib.sha256()
    root = assets_dir.parent
    for path in sorted((p for p in assets_dir.rglob("*") if p.is_file()),
                       key=lambda p: p.relative_to(root).as_posix().encode()):
        listing.update(f"{calculate_sha256(path)}  {path.relative_to(root).as_posix()}\n".encode())
    return listing.hexdigest()


def release_sort_key(name: str) -> tuple[int, int, int, float]:
    """Order release names. A release candidate comes before its final release."""
    match = RELEASE_NAME_PATTERN.match(name)
    if not match:
        raise UpdateError(f"Bad release name: {name}"[:60])
    major, minor, patch, candidate = match.groups()
    return int(major), int(minor), int(patch), float(candidate) if candidate else float("inf")


def installed_manifest() -> dict:
    """The manifest installed with the running app, or {}."""
    try:
        manifest = json.loads((APP_DIR / "release-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def installed_release() -> str | None:
    """The running app's release, from the manifest installed with it."""
    try:
        name = installed_manifest()["release_version"]
        release_sort_key(name)
    except (KeyError, TypeError, UpdateError):
        return None
    return name


def valid_changes(changes) -> list[dict]:
    """The well-formed entries of a breaking change list; anything else is skipped."""
    valid = []
    for change in changes if isinstance(changes, list) else []:
        try:
            release_sort_key(change["since"])
            if change["kind"] in ("full_kit", "warning") and isinstance(change["text"], str) and change["text"]:
                valid.append(change)
        except (KeyError, TypeError, UpdateError):
            continue
    return valid


def breaking_changes(installed: str, target: str, manifests: list[dict]) -> list[dict]:
    """The breaking changes made between two releases, either way round, oldest first.

    A change since S lies between A and B when min(A, B) < S <= max(A, B). The lists from every
    source are merged, because a release only knows the changes made up to it."""
    changes = []
    for manifest in manifests:
        changes += valid_changes(manifest.get("breaking_changes"))
    try:
        changes += valid_changes(json.loads(BREAKING_CHANGES_FILE.read_text(encoding="utf-8")).get("changes"))
    except (OSError, ValueError, AttributeError):
        pass
    low, high = sorted((release_sort_key(installed), release_sort_key(target)))
    crossed, seen = [], set()
    for change in sorted(changes, key=lambda c: release_sort_key(c["since"])):
        key = (change["since"], change["kind"], change["text"])
        if low < release_sort_key(change["since"]) <= high and key not in seen:
            seen.add(key)
            crossed.append(change)
    return crossed


def release_info() -> dict[str, str]:
    info = {}
    if RELEASE_INFO_FILE.is_file():
        for line in RELEASE_INFO_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                info[key.strip()] = value.strip()
    return info


def update_release_info(values: dict[str, str]) -> None:
    """Set these keys in /etc/hpc-release, keeping every other line."""
    values = {key: re.sub(r"[^A-Za-z0-9._:+-]", "_", value) for key, value in values.items()}
    lines = []
    if RELEASE_INFO_FILE.is_file():
        lines = RELEASE_INFO_FILE.read_text(encoding="utf-8").splitlines()
    remaining = dict(values)
    updated = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        updated.append(f"{key}={remaining.pop(key)}" if key in remaining else line)
    updated.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = RELEASE_INFO_FILE.with_name(f".{RELEASE_INFO_FILE.name}.tmp")
    temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, RELEASE_INFO_FILE)


def check_requires_setup(required: str | None) -> None:
    # Any value blocks: the setup isn't versioned, so this can't tell what a unit has.
    if required:
        raise UpdateError(f"Needs {required}. Use a full USB kit."[:60])


def component_file(release_dir: Path, manifest: dict, component: str) -> tuple[Path, str]:
    try:
        name = manifest["components"][component]["file"]
        expected = manifest["components"][component]["sha256"].lower()
    except (KeyError, TypeError, AttributeError) as error:
        raise UpdateError(f"Release has no {component}. Use a full USB kit.") from error
    if Path(name).name != name:
        raise UpdateError(f"Bad {component} file name")
    return release_dir / name, expected


def find_mount(device: Path) -> Path | None:
    """Where this device is already mounted, read-write or not."""
    wanted = os.stat(device).st_rdev
    wanted_id = f"{os.major(wanted)}:{os.minor(wanted)}"
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields[2] == wanted_id:
            return Path(fields[4].replace("\\040", " "))
    return None


def app_running() -> bool:
    """Whether hmi.service is up, starting, or waiting to be restarted."""
    output = run(["systemctl", "show", APP_SERVICE, "--property=ActiveState"], check=False).stdout
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return values.get("ActiveState", "") in APP_RUNNING_STATES


def wait_for_app_mount(device: Path) -> Path | None:
    """Where the drive is mounted, waiting for the app to mount it while the app is running."""
    deadline = time.monotonic() + APP_MOUNT_WAIT_SECONDS
    while True:
        mount_point = find_mount(device)
        if mount_point is not None:
            return mount_point
        if time.monotonic() >= deadline or not app_running():
            return None
        time.sleep(0.5)


def mount_lost(mount_point: Path) -> bool:
    """Whether our own mount of the drive was detached (by the app) or unmounted."""
    return not os.path.ismount(mount_point)


def mount_drive(device: Path) -> tuple[Path | None, bool]:
    """The drive's mount point, and whether it was mounted here and needs unmounting."""
    mount_point = wait_for_app_mount(device)
    if mount_point is not None:
        return mount_point, False
    MOUNT_DIR.mkdir(parents=True, exist_ok=True)
    result = run(
        ["mount", "-t", "vfat", "-o", "ro,nosuid,nodev,noexec", str(device), str(MOUNT_DIR)],
        check=False,
    )
    if result.returncode != 0:
        logging.info("Cannot mount %s: %s", device, result.stderr.strip())
        return None, False
    return MOUNT_DIR, True


def clear_stage() -> None:
    shutil.rmtree(STAGE_DIR, ignore_errors=True)


def find_releases(drive: Path) -> Path | None:
    """The drive's releases folder (the first of RELEASE_FOLDERS holding a CURRENT), or None."""
    for folder in RELEASE_FOLDERS:
        if (drive / folder / "CURRENT").is_file():
            return drive / folder
    return None


def system_files() -> tuple[tuple[str, Path, int], ...]:
    """(staged name, installed path, mode) of each system file an install replaces. The previous copies are saved
    under the installed names, as older updaters save them, so either can roll back the other's."""
    return (
        ("hmi.service", SERVICE_FILE, 0o644),
        ("hmi-networkmanager.rules", POLKIT_RULES_FILE, 0o644),
        ("hpc-usb-update", UPDATER_FILE, 0o755),
    )


def install_file(source: Path, destination: Path, mode: int) -> None:
    """Copy a file into place atomically, so a power cut leaves the old or the new one, never half of it."""
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    temporary.chmod(mode)
    os.replace(temporary, destination)


def stage_release(drive: Path) -> None:
    """Copy the release CURRENT names to this HMI and verify the copy, if it is an update."""
    releases = find_releases(drive)
    if releases is None:
        logging.info("%s has no %s: not an update drive", drive, " or ".join(f"{f}/CURRENT" for f in RELEASE_FOLDERS))
        return
    current_file = releases / "CURRENT"

    name = current_file.read_text(encoding="utf-8").strip()
    candidate = release_sort_key(name)
    installed = installed_release()
    if installed == name:
        logging.info("%s is already installed", name)
        return

    source = releases / "versions" / name
    manifest_file = source / "manifest.json"
    if not manifest_file.is_file():
        raise UpdateError(f"Release {name} is not on the drive")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("release_version") != name:
        raise UpdateError(f"Release {name} has the wrong manifest")
    check_requires_setup(manifest.get("requires_setup"))
    offer = make_offer(installed, name, candidate, manifest)

    write_status("preparing", name)
    clear_stage()
    app_dir = STAGE_DIR / "bin"
    system_dir = STAGE_DIR / "system"
    app_dir.mkdir(parents=True)
    system_dir.mkdir()

    # Everything is checked from the copy here, so a drive pulled early or a bad
    # copy can't get past this, and the drive isn't needed for the install.
    for component, destination in (
        ("hmi", app_dir / "HPC_LinuxGUI"),
        ("hmi_service", system_dir / "hmi.service"),
        ("hmi_polkit_rules", system_dir / "hmi-networkmanager.rules"),
    ):
        release_file, expected = component_file(source, manifest, component)
        if not release_file.is_file():
            raise UpdateError(f"Release is missing {component}")
        shutil.copyfile(release_file, destination)
        if calculate_sha256(destination) != expected:
            raise UpdateError(f"{component} failed its checksum. Copy the drive again.")

    # Optional: releases before v2.0.18 don't ship one, and then the installed updater stays
    if "updater" in (manifest.get("components") or {}):
        release_file, expected = component_file(source, manifest, "updater")
        if not release_file.is_file():
            raise UpdateError("Release is missing updater")
        shutil.copyfile(release_file, system_dir / "hpc-usb-update")
        if calculate_sha256(system_dir / "hpc-usb-update") != expected:
            raise UpdateError("updater failed its checksum. Copy the drive again.")

    if not (source / "assets").is_dir():
        raise UpdateError("Release is missing assets")
    shutil.copytree(source / "assets", app_dir / "assets", copy_function=shutil.copyfile)
    try:
        expected_assets = manifest["components"]["assets"]["sha256"].lower()
    except (KeyError, TypeError, AttributeError) as error:
        raise UpdateError("Release has no assets checksum. Use a full USB kit.") from error
    if assets_sha256(app_dir / "assets") != expected_assets:
        raise UpdateError("Assets failed their checksum. Copy the drive again.")

    debs = manifest.get("debs")
    if debs:
        if debs.get("target") != DEBS_TARGET or platform.machine() != "aarch64":
            raise UpdateError(f"Release packages are for {debs.get('target')}")
        deb_dir = STAGE_DIR / "debs"
        deb_dir.mkdir()
        for package in debs.get("packages", []):
            deb_name = package["file"]
            if Path(deb_name).name != deb_name:
                raise UpdateError("Bad package file name")
            shutil.copyfile(source / debs["dir"] / deb_name, deb_dir / deb_name)
            if calculate_sha256(deb_dir / deb_name) != package["sha256"].lower():
                raise UpdateError("A package failed its checksum. Copy the drive again.")

    (app_dir / "release-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (STAGE_DIR / "offer.json").write_text(json.dumps(offer), encoding="utf-8")
    # Written last: its presence means the stage is complete.
    (STAGE_DIR / "ready").write_text(name, encoding="utf-8")
    write_status("ready", name, offer=offer)


def make_offer(installed: str | None, name: str, candidate: tuple, manifest: dict) -> dict:
    """What the panel shows before installing; refuses a change only a full USB kit can make."""
    if installed is None:
        return {"installed": "", "direction": "unknown",
                "warnings": ["The installed version is unknown, so changes between versions can't be checked."]}
    changes = breaking_changes(installed, name, [manifest, installed_manifest()])
    for change in changes:
        if change["kind"] == "full_kit":
            raise UpdateError(f"{change['text']} Use a full USB kit.")
    direction = "downgrade" if candidate < release_sort_key(installed) else "upgrade"
    return {"installed": installed, "direction": direction, "warnings": [c["text"] for c in changes]}


def staged_offer() -> dict:
    try:
        offer = json.loads((STAGE_DIR / "offer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return offer if isinstance(offer, dict) else {}


def stage_ready() -> str | None:
    marker = STAGE_DIR / "ready"
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else None


def scan(device: Path) -> int:
    try:
        mount_point, mounted_here = mount_drive(device)
    except OSError as error:
        logging.info("Drive went away: %s", error)
        return 0
    if mount_point is None:
        return 0
    try:
        try:
            stage_release(mount_point)
        except OSError:
            if not (mounted_here and mount_lost(mount_point)):
                raise
        if mounted_here and mount_lost(mount_point):
            # The app took the drive over for logging, which detaches our mount and
            # leaves an empty folder: read the release again through the app's mount.
            mounted_here = False
            try:
                app_mount = wait_for_app_mount(device)
            except OSError:
                app_mount = None
            if app_mount is None:
                raise UpdateError("The drive went away while it was read. Plug it in again.")
            logging.info("Reading %s through the app's mount %s", device, app_mount)
            stage_release(app_mount)
    except (UpdateError, ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        clear_stage()
        message = str(error) if isinstance(error, UpdateError) else "Cannot read the release on this drive."
        logging.error("Update drive refused: %s", error)
        write_status("blocked", message=message)
    finally:
        if mounted_here:
            run(["umount", str(mount_point)], check=False)
    return 0


def app_stays_up() -> bool:
    """Whether the started app runs for HEALTH_CHECK_SECONDS without systemd restarting it."""
    def service_state() -> tuple[str, int]:
        output = run(["systemctl", "show", APP_SERVICE, "--property=ActiveState,NRestarts"]).stdout
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        return values.get("ActiveState", ""), int(values.get("NRestarts") or 0)

    _, restarts = service_state()
    deadline = time.monotonic() + HEALTH_CHECK_SECONDS
    while time.monotonic() < deadline:
        time.sleep(2)
        state, now_restarts = service_state()
        if now_restarts != restarts or state in ("failed", "inactive"):
            logging.error("App not healthy: %s, restarts %d to %d", state, restarts, now_restarts)
            return False
    return True


def restore_previous() -> None:
    """Put the previous app and system files back. Safe to repeat after a power cut."""
    if PREVIOUS_APP_DIR.is_dir():
        shutil.rmtree(APP_DIR, ignore_errors=True)
        PREVIOUS_APP_DIR.rename(APP_DIR)
    for _, destination, mode in system_files():
        saved = PREVIOUS_SYSTEM_DIR / destination.name
        if saved.is_file():
            install_file(saved, destination, mode)


def set_owner(path: Path) -> None:
    run(["chown", "-R", f"{APP_USER}:{APP_USER}", str(path)])


def show_on_console(text: str) -> None:
    """The panel has no app on it while swapping; say something rather than go blank."""
    try:
        Path("/dev/tty1").write_text(f"\033[2J\033[H\n\n  {text}\n", encoding="utf-8")
    except OSError:
        pass


def apply_update() -> int:
    INSTALL_REQUEST.unlink(missing_ok=True)
    name = stage_ready()
    if name is None:
        logging.info("Install requested with nothing staged")
        return 0

    write_status("installing", name)
    deb_dir = STAGE_DIR / "debs"
    deb_files = sorted(deb_dir.glob("*.deb")) if deb_dir.is_dir() else []
    if deb_files:
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        result = subprocess.run(["dpkg", "--install", *map(str, deb_files)], env=env, text=True,
                                capture_output=True, check=False)
        if result.returncode != 0:
            logging.error("dpkg failed: %s", result.stderr.strip())
            clear_stage()
            write_status("failed", name, "Packages failed to install. Nothing was changed.")
            return 1

    previous = installed_release() or "unknown"
    downgrade = staged_offer().get("direction") == "downgrade"
    # Before the marker: once it exists, what's saved here is what a boot restores,
    # so a bin.previous left by the last update must already be gone.
    shutil.rmtree(PREVIOUS_APP_DIR, ignore_errors=True)
    shutil.rmtree(PREVIOUS_SYSTEM_DIR, ignore_errors=True)
    PREVIOUS_SYSTEM_DIR.mkdir(parents=True)
    for _, installed, _ in system_files():
        if installed.is_file():
            shutil.copyfile(installed, PREVIOUS_SYSTEM_DIR / installed.name)

    APPLYING_MARKER.write_text(name, encoding="utf-8")
    run(["systemctl", "stop", APP_SERVICE], check=False)
    show_on_console(f"Installing {name}. Do not power off.")

    # settings.txt is this unit's configuration, so it moves with the app.
    settings = APP_DIR / "settings.txt"
    new_app_dir = STAGE_DIR / "bin"
    if settings.is_file():
        shutil.copy2(settings, new_app_dir / "settings.txt")
    set_owner(new_app_dir)
    (new_app_dir / "HPC_LinuxGUI").chmod(0o755)

    APP_DIR.rename(PREVIOUS_APP_DIR)
    new_app_dir.rename(APP_DIR)
    # The updater is replaced only when the release ships one. This process carries on with the code it started
    # with; the new updater runs from the next scan, or from the next boot if power is cut before the app proves
    # itself, when it restores the previous files (including the previous updater).
    for file_name, destination, mode in system_files():
        staged = STAGE_DIR / "system" / file_name
        if staged.is_file():
            install_file(staged, destination, mode)

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "start", APP_SERVICE], check=False)
    if app_stays_up():
        # The new app has proved itself, so it stays: remove the marker first, so a
        # power cut from here on can't make the next boot roll it back.
        APPLYING_MARKER.unlink(missing_ok=True)
        manifest = json.loads((APP_DIR / "release-manifest.json").read_text(encoding="utf-8"))
        update_release_info({
            "HPC_APP": name,
            "HPC_APP_COMMIT": str(manifest.get("source_commit") or "unknown"),
            "HPC_APP_DATE": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        clear_stage()
        write_status("installed", name, f"{'Went back' if downgrade else 'Updated'} from {previous}.")
        return 0

    run(["systemctl", "stop", APP_SERVICE], check=False)
    restore_previous()
    run(["systemctl", "daemon-reload"])
    clear_stage()
    APPLYING_MARKER.unlink(missing_ok=True)
    write_status("failed", name, f"{name} did not start. Went back to {previous}.")
    run(["systemctl", "start", APP_SERVICE], check=False)
    return 1


def boot() -> int:
    """Runs before hmi.service. A marker here means power was lost during an install."""
    if APPLYING_MARKER.is_file():
        name = APPLYING_MARKER.read_text(encoding="utf-8").strip()
        logging.error("Install of %s was interrupted; going back", name)
        restore_previous()
        run(["systemctl", "daemon-reload"], check=False)
        clear_stage()
        APPLYING_MARKER.unlink(missing_ok=True)
        write_status("failed", name, f"Install of {name} was interrupted. Plug the drive in to retry.")
        return 0
    name = stage_ready()
    if name is None:
        return 0
    if installed_release() == name:
        # Installed, but power was cut before the stage was cleared
        clear_stage()
        return 0
    write_status("ready", name, offer=staged_offer())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Install an HMI release from a USB drive")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--scan", type=Path, metavar="DEVICE", help="check a drive for an update")
    action.add_argument("--apply", action="store_true", help="install what a scan prepared")
    action.add_argument("--boot", action="store_true", help="recover from an interrupted install")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_DIR.chmod(0o700)
    with LOCK_FILE.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.scan:
            return scan(args.scan)
        if args.apply:
            return apply_update()
        return boot()


if __name__ == "__main__":
    raise SystemExit(main())
