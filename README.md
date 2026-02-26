# SiliconRefineryChat

Standalone macOS desktop app for fully local Apple Foundation Models chat, built with BeeWare Toga + Briefcase.

This repository is synchronized automatically from the parent project:

- Parent repo: [adpena/silicon-refinery](https://github.com/adpena/silicon-refinery)
- Source path: `examples/toga_local_chat_app`
- Automation: release pipeline in parent repo (`scripts/publish_chat_repo.sh`)

## Features

- Realtime streaming responses
- SQLite-backed multi-chat persistence
- Codex-style stacked chat tabs
- Slash commands (`/help`, `/new`, `/clear`, `/export`)
- Steering interjection reruns (interrupt + inject + regenerate)
- Nonblocking inference pipeline with UI-safe updates

## Install via Homebrew

```bash
brew tap adpena/silicon-refinery https://github.com/adpena/homebrew-silicon-refinery
brew install silicon-refinery-chat
silicon-refinery-chat
```

```text
Actual output excerpt from local run:

silicon-refinery-chat 0.0.214 installed=0.0.214
silicon-refinery 0.0.214 installed=0.0.214
```

This installs `SiliconRefineryChat.app` and a `silicon-refinery-chat` launcher command.

## Local development

```bash
uv sync
uv run briefcase dev
```

## Build/package macOS app

```bash
uv run briefcase create macOS
uv run briefcase build macOS
# Local-only build
uv run briefcase package macOS --adhoc-sign

# Redistributable build (Developer ID + notarization)
export CHAT_SIGN_IDENTITY="${CHAT_SIGN_IDENTITY:?Set your Developer ID identity string first}"
export APPLE_NOTARY_PROFILE="${APPLE_NOTARY_PROFILE:?Set your stored notary profile name first}"
uv run briefcase package macOS --identity "$CHAT_SIGN_IDENTITY" --no-notarize
APPLE_NOTARY_PROFILE="$APPLE_NOTARY_PROFILE" \
  ./scripts/notarize_macos_artifact.sh --artifact "$(ls -t dist/SiliconRefineryChat-*.dmg | head -n 1)" --app-name "SiliconRefineryChat.app"
```

Packaged artifacts are published under `artifacts/` in this repo and attached to GitHub Releases.

Ad-hoc builds are for local testing only. Public distribution should always use Developer ID signing + notarization to avoid Gatekeeper malware verification warnings during install.
The parent publish pipeline now blocks release uploads for untrusted artifacts by default (ad-hoc, unstapled, or non-notarized), unless `--allow-untrusted-release` is explicitly provided.

## Versioning

`silicon-refinery-chat` stays version-locked with `silicon-refinery` and uses thousandth-place increments (`0.0.213` -> `0.0.214`).

## License

MIT. See [LICENSE](LICENSE).
