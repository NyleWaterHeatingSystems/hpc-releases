# HPC Releases

This repository is the archive of HPC firmware releases and release candidates.
Each release consists of three binaries (HMI, control, and iot) plus the HMI assets.
The binaries are kept here uncompressed, so a checkout can be copied onto an HMI as-is.

Releases are created by `tools/create_release.sh` in the
[HPC_Firmware](https://github.com/NyleWaterHeatingSystems/HPC_Firmware) repository; nothing here builds anything.

## Directory Structure

```
hpc-releases/
├── CURRENT                       # Text file naming the release in use, e.g. v2.0.8
├── README.md                     # This document
└── versions/                     # All releases, including release candidates
    ├── v2.0.8/
    │   ├── manifest.json         # Checksums, source commit and version metadata
    │   ├── notes.txt             # Release notes
    │   ├── hmi_v2.0.8.bin        # Linux HMI binary (aarch64)
    │   ├── iot_v2.0.8.bin        # Embedded IOT chip binary
    │   ├── control_v2.0.8.bin    # Embedded control chip binary
    │   ├── assets/               # HMI assets (audio, fonts, images)
    │   └── debs/bookworm-arm64/  # Optional: app dependency packages for that Debian release/architecture
    ├── v2.1.0-RC1/               # Release candidates are suffixed with '-RC[Candidate Index]'
    └ ...
```

A release folder is never changed after it is published. A fix gets a new version.

## manifest.json

| Field | Meaning |
|---|---|
| `release_version` | Release name (matches its folder and `CURRENT`), e.g. `v2.0.8` or `v2.1.0-RC1` |
| `release_datetime` | UTC time the release was created |
| `source_repo`, `source_commit`, `source_branch`, `source_tag` | Exactly what the binaries were built from. `source_tag` is a tag in HPC_Firmware |
| `source_provenance` | Only on releases made before source tracking (v2.0.10 and earlier): how the source commit was inferred |
| `requires_setup` | Setup the release needs, or `null`. Any value makes USB updates refuse it and ask for a full kit |
| `breaking_changes` | From v2.0.12-RC2: all of HPC_Firmware's `BREAKING_CHANGES.json` up to this release. A USB update warns about each change between the installed release and this one, either way, and refuses a `full_kit` one |
| `debs` | Bundled packages (target, folder, and each file's package, version, architecture and SHA-256), or `null` |
| `components` | File name and SHA-256 of each binary, plus a combined SHA-256 of the `assets/` folder |

Releases up to v2.0.8 have no assets hash, and their source commit was inferred after the fact.
Each manifest's `source_provenance` field and the release's notes say how confident that match is.
v2.0.9 and v2.0.10 were rebuilt from their tags on 2026-11-04, so their source is exact, but they are not the original binaries.

## Creating a Release

In HPC_Firmware:

1. Bump `VERSION`, commit, and push to `main`. Add an entry to `BREAKING_CHANGES.json` for anything that breaks
   across releases, in the commit that makes the change; the script checks the file and copies it into the manifest.
2. Build the HMI (aarch64), control, and iot from that commit. For the HMI, run
   `LinuxHMI/HPC_LinuxGUI/scripts/build-cross.sh`.
3. Run `tools/create_release.sh` for a final release, or `tools/create_release.sh <RC-Number>` for a release candidate.
   Add `--requires-setup <ver>` if the release needs a newer setup; USB updates then refuse it and ask for a full kit.
   Add `--debs <dir>` to bundle the app's dependency `.deb` files (see below).
   The script expects this repository checked out next to HPC_Firmware; otherwise set `HPC_RELEASES_DIR` or pass `--releases-dir`.

The script refuses to run if:

- the working tree has uncommitted or untracked changes
- the release tag already exists, locally or on origin
- a final release is not on `main` in sync with `origin/main` (release candidates may come from any branch)
- `VERSION` does not match the requested version, or a binary is older than the current commit

It writes `versions/<release>/`, drafts `notes.txt` from the commits since the previous tag, and tags the source commit
in HPC_Firmware. Then:

4. Complete `notes.txt` (see below).
5. Push the tag from HPC_Firmware: `git push origin <release>`.
6. Commit and push the new folder here.

## Choosing the Release in Use

`CURRENT` is a one-line text file holding the name of a folder in `versions/`, e.g. `v2.0.8`.
Whatever installs a release reads it and uses `versions/<that name>/`.
To promote a release candidate or roll back, change it and commit:

```
echo v2.0.8 > CURRENT
```

`create_release.sh` offers to do this after creating a release.

`CURRENT` is a plain file rather than a symlink because this repository travels on USB sticks, and FAT/exFAT
cannot store symlinks. For the same reason, do not rely on file permissions surviving a copy: the installer must
mark the HMI binary executable itself.

## Bundled Dependencies

A release can carry the `.deb` packages the app needs that are not already in the golden image, so it installs
offline from USB. Install them on the HMI with:

```
sudo apt install ./versions/<release>/debs/bookworm-arm64/*.deb
```

- Bundle only app dependencies (e.g. `libpaho-mqtt1.3`). System packages and configuration belong to `hmi_provisioning/` in HPC_Firmware.
- Bundle runtime packages, not `-dev` packages.
- Include the full set the app needs beyond the golden image; `apt` resolves dependencies among the local files.
- Packages are tied to the Debian release and architecture in the folder name. A release for a new Debian release
  needs a new golden image or setup (`requires_setup`).

## Release Notes

Release notes should include:

- The date of the release
- A summary of features and fixes made since the last release (changelog)
- The supported units of that release
