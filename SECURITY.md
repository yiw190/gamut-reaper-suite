# Security

Hibiki gives an AI agent control of REAPER, including `run_lua` (any Lua inside REAPER), and runs two local
services: the MCP front on `127.0.0.1:8787` and the agent gateway on `127.0.0.1:47892`. Both need a bearer token.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/yiw190/hibiki/security/advisories/new). Include the
version (`hibiki doctor` prints it), your OS and the steps to reproduce.

You'll get a reply within a week. Fixes ship in the next release, and the release notes credit you unless you'd
rather not be named.

## Supported versions

Only the latest release, including release candidates, gets security fixes.
