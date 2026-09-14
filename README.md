# Release Management

This directory serves as the archive for HPC firmware releases and release candidates.
Each release consists of three binaries: HMI, control, and iot.

## Directory Structure

```
releases/
├── LATEST                        # Symlink to the latest version (./versions/v2.2.0/)
├── create_release.sh             # Script that bundles current software into a release
├── README.md                     # This document, details the organization and SOO of release versions
└── versions/                     # All previous versions, may include release candidates
    ├── v2.2.0/
    │   ├── manifest.json         # Checksums and version metadata
    │   ├── notes.txt             # Release Notes
    │   ├── hmi_v2.2.0.bin        # Linux HMI binary
    │   ├── iot_v2.2.0.bin        # Embedded IOT chip binary
    │   └── control_v2.0.0.bin    # Embedded control chip binary
    ├── v2.2.0-RC2/               # Release Candidates are suffixed with '-RC[Candidate Index]'
    ├── v2.2.0-RC1/
    ├── v2.1.0/
    ├── v2.1.0-RC1/
    └ ...
```

## Firmware version

The firmware version for HMI, control, and IoT is defined once in the repo-root `VERSION` file.
Bump that file, then reconfigure/rebuild each component so generated `version.h` files and binary names stay in sync.

## Release Sequence of Operations

1. Navigate to the root-directory of the project (`HPC_Firmware/`).
2. Run `releases/create_release.sh`. It reads the version from `VERSION`.
   Examples:
   - `releases/create_release.sh` would generate a release from `VERSION` (e.g. 2.0.5)
   - `releases/create_release.sh 3` would generate a release candidate (e.g. 2.0.5-RC3)
   - `releases/create_release.sh 2 0 5` still works if the numbers match `VERSION`
3. If you wish to update the currently-used release (when performing a rollback or after validation a release candidate) update the LATEST symlink to point to the desired version.
   Example:
   - `ln -sf ./versions/v2.0.0-RC1 ./releases/LATEST`

## Release Notes

Release notes should include the following information:

- The date of the release
- A summary of features and fixes made since the last release (changelog)
- The supported units of that release
