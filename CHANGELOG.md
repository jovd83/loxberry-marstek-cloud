# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - Unreleased

### Added

### Changed

### Fixed

### Removed

## [1.0.1] - 2026-05-30

### Added
- GitHub Actions CI pipeline (`validate.yml`) — runs Python syntax checks and build verification on every push and pull request.
- Principal-audit reports added to `Technical_Reviews/`.

### Changed
- README badges updated to standard set: Actions / GitHub Release / License.
- `<script>` block in settings template moved to end of form for cleaner HTML structure.
- Legacy `jovd83-*.zip` exclusion removed from `build_zip.py`.

### Fixed
- MQTT broker connection now retried up to 3 times with 5-second backoff on startup, preventing a permanent crash when the broker is not yet ready at boot.
- Replaced module-level `RUNNING` flag with `threading.Event`; daemon now responds to SIGTERM immediately rather than waiting up to 1 second.
- Version string shown in the plugin UI was `0.1.2`; corrected to track `plugin.cfg` version.
- User-Agent header was hardcoded as `/0.1`; now uses the `PLUGIN_VERSION` constant.
- `system()` call for daemon restart in CGI now double-quotes paths, preventing breakage on paths with spaces.

## [1.0.0] - 2026-03-01

### Added
- Automatic plugin updates via LoxBerry auto-update mechanism (`AUTOMATIC_UPDATES=true` in `plugin.cfg`).

### Changed
- Plugin icons redesigned to solid blue rounded-rectangle app-icon style.

### Fixed
- Plugin icons regenerated with correct alpha channel transparency.

## [0.1.2] - 2026-02-01

### Fixed
- ZIP archive now sets explicit Unix file permissions (0755 for executables, 0644 for data files) to ensure correct permissions after LoxBerry installation.
- Removed hardcoded `/opt/loxberry` path literals from daemon and CGI scripts to pass the LoxBerry installer linter check.

## [0.1.0] - 2026-01-01

### Added
- Initial release: Marstek Cloud to MQTT bridge for LoxBerry.
- Polls `eu.hamedata.com` (and other regional `*.hamedata.com` endpoints) at a configurable interval and republishes device datapoints as retained MQTT messages.
- Auto-discovers LoxBerry built-in MQTT broker credentials.
- Auto-registers topic subscription with the LoxBerry MQTT Gateway.
- Supports 8 configurable datapoints per device plus 3 diagnostic metrics.
- Token caching with automatic re-authentication on expiry.
- Exponential backoff retry on API failures.
- API base URL allow-list to prevent credential exfiltration via CSRF.
- Config file stored at mode 0600; credentials never logged in plaintext.
- MQTT dry-run mode for smoke testing.
- Optional raw-JSON topic publishing.
- LoxBerry web UI for all settings with daemon status display.

[1.0.1]: https://github.com/jovd83/loxberry-marstek-cloud/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/jovd83/loxberry-marstek-cloud/compare/v0.1.2...v1.0.0
[0.1.2]: https://github.com/jovd83/loxberry-marstek-cloud/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/jovd83/loxberry-marstek-cloud/releases/tag/v0.1.0
