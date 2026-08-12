# Security Policy

## Token handling

MVSep Client talks to the MVSep API with a personal API token. The token:

- lives **only** in your local config file at `%APPDATA%\MVSepClient\config.json`
- may be auto-imported from `~/.mvsep_cli_config` on first run
- is **never** logged, printed, or written to any file inside this repository
- is **never** sent anywhere except the MVSep API endpoint for the mirror you selected

If you accidentally commit a token, rotate it on the MVSep website immediately — git history never forgets.

## Reporting a vulnerability

This is a personal project. To report a security issue, open a private discussion or contact the maintainer directly. Do **not** open a public issue that includes tokens, API keys, or credentials.

## Supported versions

Only the latest release is supported.
