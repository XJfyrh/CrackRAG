# Contributing

Issues and pull requests are welcome for the documented financial-question scope, reproducibility, source validation, and demo usability. For a bug, include the version or commit, steps to reproduce, expected and actual behavior, and whether the run used mock or real mode. Remove access tokens, request traces, private ledgers, and third-party full PDFs before posting.

Work from `main` on a topic branch. Keep `v0.1.0` as an immutable release reference. Explain the user-visible behavior and add a focused regression check when changing answer validation, billing, or recovery. Before a pull request, run the relevant checks from [CI](.github/workflows/ci.yml); for a frontend change, at least run `npm --prefix web run build`, `npm --prefix web run build:demo`, and the mock browser flow if Docker is available. Real provider calls are never required for a public contribution.

For security findings, use the private route in [SECURITY.md](SECURITY.md) rather than a public issue.
