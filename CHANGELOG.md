# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every release ships:

- a matching git tag `vX.Y.Z`,
- a PyPI wheel + sdist at `ontos==X.Y.Z`,
- a GHCR container image at `ghcr.io/enclave-labs-inc/ontos:X.Y.Z`,
- a Helm chart `appVersion` bumped to match under `deploy/helm/ontos/Chart.yaml`.

See [RELEASING.md](RELEASING.md) for how a release is cut.

## [Unreleased]

Nothing yet. New work between releases lands here.

## [0.0.1] - 2026-09-25 - pre-alpha scaffold

Initial repository scaffold — never published; served as the M0 baseline
before the milestone plan started shipping.

- FastMCP server with `search / traverse / explain / provenance / audit_lookup` tools
- Immutable, bitemporal `Fact` model
- Hash-chained in-memory Article-12 audit emitter
- Permission-aware traversal in the NetworkX dev store
- Docker + docker-compose scaffold
