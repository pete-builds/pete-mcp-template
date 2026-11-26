# pete-mcp-template

Copier template that scaffolds a new [MCP](https://modelcontextprotocol.io)
server on the [pete-mcp-core](https://github.com/pete-builds/pete-mcp-core)
substrate — pyproject, CI, Dockerfile, healthcheck, structured logging, and
a working tool stub, in one command.

## What you get

A ready-to-run repo with:

- `src/<your_package>/` — server, settings, healthcheck, all wired to
  `pete-mcp-core`. One example tool (`hello`) that FastMCP registers on boot.
- `pyproject.toml`: hatchling build, `pete-mcp-core` dep (a pinned commit
  tarball, see below), `fastmcp>=4.1.6,<5`, ruff + pytest + mypy dev extras,
  console script entry point.
- `Dockerfile` — multi-stage, non-root, digest-independent base, `HEALTHCHECK`
  wired to `pete-mcp-healthcheck`.
- `docker-compose.yml` — one-liner local run.
- `.github/workflows/ci.yml`: matrix over the Python versions you allow, with
  every action pinned by full commit SHA.
- `.github/workflows/parity.yml` and `parity/`: the release parity check,
  described below.
- `.github/dependabot.yml`: pip, Actions and Docker updates; the Docker block
  ignores Python minor and major moves so the image stays on 3.13.
- `README.md`, `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `.env.example`
  — populated from your answers.

Everything passes `pytest` and `ruff` immediately after generation.

## Usage

```bash
pipx install copier
copier copy gh:pete-builds/pete-mcp-template my-new-server
cd my-new-server
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Answer the prompts (project name, port, default transport, etc.) and you have
a running server on the same pattern as `mcp-searxng`, `mcp-threatintel`, and
`mcp-spotify`.

## How generated projects get pete-mcp-core

`pete-mcp-core` is **not on PyPI**, and staying off it is the standing decision
(`pete-builds/mcp-unifi`, `docs/decisions/0002-shared-substrate-stays-off-pypi.md`).
Generated projects depend on it the same way `mcp-searxng`, `mcp-threatintel`,
`mcp-threads`, `mcp-spotify` and `mcp-fleaflicker` do: a PEP 508 direct reference
to an immutable commit tarball, plus `allow-direct-references = true` under
`[tool.hatch.metadata]`:

```
pete-mcp-core @ https://github.com/pete-builds/pete-mcp-core/archive/<sha>.tar.gz
```

The commit is the `pete_mcp_core_sha` question in `copier.yml` (default
`18f2261...`, the commit all five consumers pin). To move every newly generated
project to a newer core, change that one line. Dependabot cannot see through a
tarball pin, so existing projects bump their own SHA by hand.

## Release parity check

Every generated server ships `.github/workflows/parity.yml`, which calls
`.github/workflows/mcp-parity.yml` in this repo, pinned to the template commit
the server was generated from (`copier update` moves the pin).

**What it proves.** On every pull request, and before a release when the
release workflow calls it, CI starts two containers: the newest `X.Y.Z` release
of the server from GHCR and a candidate built from the change. A third
container talks to both over a docker network, the way a LAN client does. It
compares the tool list, each tool's title, full description, input and output
schema and annotations, and the answers to the read-only golden calls in
`parity/golden.yaml`. Any difference fails the check. This catches what a green
test suite and a green healthcheck both miss: a renamed tool parameter, a
description the framework truncated, an argument the server now accepts and
ignores, and a server that only listens on `127.0.0.1` inside its container.
The job always ends with one line starting `parity:`; a first release with no
image yet says `parity: no baseline image, first release, skipped`.

**What it does not prove.** It replays a fixed golden set, not live traffic.
Both containers run on the dummy values in `parity/parity.env`, so a server
whose answers depend on a live web service (search, weather, a vendor API)
gets schema and transport parity only, until recorded upstream fixtures exist.
It says nothing about tools the golden set never calls, or about load,
memory or timing.

**Declaring an intended change.** When a change is meant to alter what clients
see, the failing run's summary lists each difference by path, for example
`tools.hello.inputSchema.properties.who`. Add an entry to
`parity/expected.yaml`:

```yaml
changes:
  - change: tools.hello.inputSchema.properties
    baseline: 0.3.0
    reason: Renamed the hello tool's argument from name to who, to match the other tools.
```

An entry covers its path and everything under it, and applies only while the
baseline is the release it names, so it stops applying after the next release
and cannot hide a later change. Reviewers read this file instead of the diff.

**How the pinned client is found.** A reusable workflow's `github` context
describes the caller, so it cannot tell which commit of itself is running.
`mcp-parity.yml` reads `job.workflow_repository` and `job.workflow_sha`, which
name the workflow that defines the job, checks out `parity/` at exactly that
commit, and confirms `HEAD` before running anything. It refuses to run if they
are empty, rather than falling back to `main`.

The template's own CI proves the check can fail: `parity-controls` renders a
sample server and runs parity against itself (must pass) and against mutants
that must each fail, and `parity-e2e` runs the reusable workflow against a real
GHCR release.

## Updating a generated project

The point of copier over a plain cookiecutter is that generated projects can
pull in later template improvements:

```bash
copier update
```

Run inside a generated project. It re-applies the template, honoring the
answers you gave first time and merging any template changes (new CI job,
Dockerfile tweak, README section) into your working tree with conflict
markers if you've edited the same lines.

## What's deliberately out of scope

- Multi-arch Docker build, cosign signing, SBOM, GHCR push, `.dxt` bundle —
  those live in `mcp-unifi`'s `release.yml` and are a follow-up template
  question (`enable_release_pipeline`) once we've validated the base template.
- Per-domain tool scaffolds (which SDK client, which schema).

## License

MIT.
