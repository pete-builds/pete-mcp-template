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
