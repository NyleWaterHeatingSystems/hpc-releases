#!/bin/bash
set -euo pipefail

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSIONS_DIR="$PROJECT_ROOT/releases/versions"
VERSION_FILE="$PROJECT_ROOT/VERSION"

usage() {
    echo "Usage:"
    echo "  $0                 # use repo-root VERSION file"
    echo "  $0 <RC-Number>     # VERSION as a release candidate, e.g. $0 1 -> vX.Y.Z-RC1"
    echo "  $0 <Major> <Minor> <Patch> [RC-Number]  # must match VERSION"
    exit 1
}

read_version_file() {
    if [[ ! -f "$VERSION_FILE" ]]; then
        echo "Error: VERSION file not found: $VERSION_FILE"
        exit 1
    fi
    local parsed
    parsed="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "$VERSION_FILE" | head -n1 || true)"
    if [[ -z "$parsed" ]]; then
        echo "Error: VERSION file must contain a MAJOR.MINOR.PATCH line: $VERSION_FILE"
        exit 1
    fi
    echo "$parsed"
}

RC_NUM=""
case "$#" in
    0)
        VERSION="$(read_version_file)"
        ;;
    1)
        VERSION="$(read_version_file)"
        RC_NUM="$1"
        ;;
    3|4)
        VERSION="$1.$2.$3"
        if [[ "$#" -eq 4 ]]; then
            RC_NUM="$4"
        fi
        FILE_VERSION="$(read_version_file)"
        if [[ "$VERSION" != "$FILE_VERSION" ]]; then
            echo "Error: requested $VERSION does not match VERSION file ($FILE_VERSION)."
            echo "Bump $VERSION_FILE (the single source of truth) instead of passing a different version."
            exit 1
        fi
        ;;
    *)
        usage
        ;;
esac

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: invalid version '$VERSION'"
    exit 1
fi

VERSION_MAJOR="${VERSION%%.*}"
VERSION_MINOR="${VERSION#*.}"
VERSION_MINOR="${VERSION_MINOR%%.*}"
VERSION_PATCH="${VERSION##*.}"

# --- Validate File+Path Existence ---
# TODO: how to handle files that are not 'dev' suffixed?
BUILD_HMI="$PROJECT_ROOT/LinuxHMI/HPC_LinuxGUI/build-release/Release/bin/HPC_LinuxGUI"
BUILD_HMI_ASSETS="$PROJECT_ROOT/LinuxHMI/HPC_LinuxGUI/build-release/Release/bin/assets"
BUILD_IOT="$PROJECT_ROOT/iot/build/HPC_Firmware_${VERSION_MAJOR}_${VERSION_MINOR}_${VERSION_PATCH}_IOTdev.bin"
BUILD_CONTROL="$PROJECT_ROOT/control/build/HPC_Firmware_${VERSION_MAJOR}_${VERSION_MINOR}_${VERSION_PATCH}_CTLdev.bin"

# Safety Check: Ensure we don't proceed if files are missing
for f in "$BUILD_HMI" "$BUILD_IOT" "$BUILD_CONTROL"; do
    if [[ ! -f "$f" ]]; then
        echo "Error: Missing binary: $f"
				echo "Check that you have built the latest version."
        exit 1
    fi
done

if [[ ! -d "$BUILD_HMI_ASSETS" ]]; then
    echo "Error: Missing assets directory: $BUILD_HMI_ASSETS"
    exit 1
fi

# Logic for RC naming
# If an RC number is provided, it turns "2.3.0" into "v2.3.0-RC1"
RELEASE_NAME="v$VERSION"
if [[ -n "$RC_NUM" ]]; then
    RELEASE_NAME="v$VERSION-RC$RC_NUM"
fi

TARGET_DIR="$VERSIONS_DIR/$RELEASE_NAME"

echo "Creating Version: $RELEASE_NAME"

# 1. Create directory structure
# FIXED: mkdir -p handles parent directories; we don't need to mkdir assets 
# if we use cp -r correctly below.
mkdir -p "$TARGET_DIR"

# 2. Copy binaries
# TODO: retain exact name from build directory?
cp "$BUILD_HMI" "$TARGET_DIR/hmi_v$VERSION.bin"
cp "$BUILD_IOT" "$TARGET_DIR/iot_v$VERSION.bin"
cp "$BUILD_CONTROL" "$TARGET_DIR/control_v$VERSION.bin"

# FIXED: Copy the contents of the assets folder, not the folder itself,
# to avoid v2.3.0/assets/assets/
cp -r "$BUILD_HMI_ASSETS" "$TARGET_DIR/"

# 3. Calculate SHA-256 Checksums
echo "Generating Checksums..."
HMI_HASH=$(sha256sum "$TARGET_DIR/hmi_v$VERSION.bin" | awk '{print $1}')
IOT_HASH=$(sha256sum "$TARGET_DIR/iot_v$VERSION.bin" | awk '{print $1}')
CONTROL_HASH=$(sha256sum "$TARGET_DIR/control_v$VERSION.bin" | awk '{print $1}')
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# 4. Generate manifest.json
cat <<EOF > "$TARGET_DIR/manifest.json"
{
  "release_version": "$RELEASE_NAME",
  "release_datetime": "$TIMESTAMP",
  "components": {
    "hmi": {
      "file": "hmi_v$VERSION.bin",
      "sha256": "$HMI_HASH"
    },
    "iot": {
      "file": "iot_v$VERSION.bin",
      "sha256": "$IOT_HASH"
    },
    "control": {
      "file": "control_v$VERSION.bin",
      "sha256": "$CONTROL_HASH"
    }
  }
}
EOF

echo "Release created successfully at $TARGET_DIR"
ls "$TARGET_DIR"
