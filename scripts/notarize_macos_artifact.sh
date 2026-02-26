#!/usr/bin/env bash
set -euo pipefail

ARTIFACT_PATH=""
APP_NAME=""

usage() {
  cat <<USAGE
Usage: scripts/notarize_macos_artifact.sh --artifact <path> [--app-name <Name.app>]

Required authentication (choose one):
  1) APPLE_NOTARY_PROFILE
  2) APPLE_ID + APPLE_TEAM_ID + APPLE_APP_SPECIFIC_PASSWORD
  3) APPLE_NOTARY_KEY_PATH + APPLE_NOTARY_KEY_ID + APPLE_NOTARY_ISSUER
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifact)
      ARTIFACT_PATH="$2"
      shift 2
      ;;
    --app-name)
      APP_NAME="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$ARTIFACT_PATH" ]]; then
  echo "Error: --artifact is required." >&2
  usage
  exit 2
fi

if [[ ! -f "$ARTIFACT_PATH" ]]; then
  echo "Error: artifact not found: $ARTIFACT_PATH" >&2
  exit 1
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command not found: $1" >&2
    exit 2
  fi
}

log() {
  printf "\n[%s] %s\n" "notarize-macos" "$*"
}

require_cmd xcrun
require_cmd codesign
require_cmd spctl

NOTARY_ARGS=()
if [[ -n "${APPLE_NOTARY_PROFILE:-}" ]]; then
  if xcrun notarytool history --keychain-profile "$APPLE_NOTARY_PROFILE" --output-format json >/dev/null 2>&1; then
    NOTARY_ARGS=(--keychain-profile "$APPLE_NOTARY_PROFILE")
  elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]]; then
    log "Keychain profile '${APPLE_NOTARY_PROFILE}' is unavailable; falling back to APPLE_ID credentials."
    NOTARY_ARGS=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD")
  elif [[ -n "${APPLE_NOTARY_KEY_PATH:-}" && -n "${APPLE_NOTARY_KEY_ID:-}" && -n "${APPLE_NOTARY_ISSUER:-}" ]]; then
    log "Keychain profile '${APPLE_NOTARY_PROFILE}' is unavailable; falling back to App Store Connect API key credentials."
    NOTARY_ARGS=(--key "$APPLE_NOTARY_KEY_PATH" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER")
  else
    echo "Error: APPLE_NOTARY_PROFILE is set but not available in this keychain, and no fallback notarization credentials were provided." >&2
    exit 1
  fi
elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]]; then
  NOTARY_ARGS=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD")
elif [[ -n "${APPLE_NOTARY_KEY_PATH:-}" && -n "${APPLE_NOTARY_KEY_ID:-}" && -n "${APPLE_NOTARY_ISSUER:-}" ]]; then
  NOTARY_ARGS=(--key "$APPLE_NOTARY_KEY_PATH" --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER")
else
  echo "Error: no notarization credentials detected." >&2
  echo "Set APPLE_NOTARY_PROFILE, or APPLE_ID/APPLE_TEAM_ID/APPLE_APP_SPECIFIC_PASSWORD," >&2
  echo "or APPLE_NOTARY_KEY_PATH/APPLE_NOTARY_KEY_ID/APPLE_NOTARY_ISSUER." >&2
  exit 1
fi

log "Verifying artifact signature"
codesign --verify --verbose=2 "$ARTIFACT_PATH"

log "Submitting for notarization"
xcrun notarytool submit "$ARTIFACT_PATH" --wait "${NOTARY_ARGS[@]}"

log "Stapling notarization ticket"
xcrun stapler staple "$ARTIFACT_PATH"
xcrun stapler validate "$ARTIFACT_PATH"

log "Gatekeeper assessment"
spctl -a -vv -t open --context context:primary-signature "$ARTIFACT_PATH"

if [[ "$ARTIFACT_PATH" == *.dmg ]]; then
  require_cmd hdiutil
  MOUNT_POINT="$(mktemp -d /tmp/silicon-refinery-chat-notary.XXXXXX)"
  ATTACHED=0
  cleanup() {
    if [[ "$ATTACHED" == "1" ]]; then
      hdiutil detach "$MOUNT_POINT" -quiet || true
    fi
    rm -rf "$MOUNT_POINT"
  }
  trap cleanup EXIT

  log "Mounting DMG for app verification"
  hdiutil attach "$ARTIFACT_PATH" -nobrowse -readonly -mountpoint "$MOUNT_POINT" -quiet
  ATTACHED=1

  APP_PATH=""
  if [[ -n "$APP_NAME" && -d "$MOUNT_POINT/$APP_NAME" ]]; then
    APP_PATH="$MOUNT_POINT/$APP_NAME"
  else
    APP_PATH="$(find "$MOUNT_POINT" -maxdepth 2 -type d -name '*.app' | head -n 1 || true)"
  fi

  if [[ -n "$APP_PATH" && -d "$APP_PATH" ]]; then
    log "Verifying app bundle: $(basename "$APP_PATH")"
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
    codesign_details="$(codesign -d --verbose=4 "$APP_PATH" 2>&1 || true)"
    if ! grep -Eq "runtime|Runtime Version" <<<"$codesign_details"; then
      echo "Error: hardened runtime flag not detected in app signature." >&2
      exit 1
    fi
    spctl -a -vv "$APP_PATH"
  else
    log "No app bundle detected inside DMG; skipping app-level verification"
  fi
fi

log "Notarization + verification completed: $(basename "$ARTIFACT_PATH")"
