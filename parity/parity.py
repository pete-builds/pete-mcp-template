#!/usr/bin/env python3
"""Release parity check for MCP servers built from pete-mcp-template.

Compares the last released image of a server with a candidate image built from
a pull request, the way a shadow deploy would: same inputs, diff the outputs.

Two subcommands:

``baseline``
    Runs on the CI runner with the standard library only. Finds the highest
    semver tag of the image on GHCR, reading EVERY page of the tag list
    (GHCR returns 100 tags a page, and attestation tags push the newest
    release off page one), then confirms that tag with a manifest request.
    Cross-checks the repo's own release tags so that "no image found" can only
    mean "first release" when the repo has never released either.

``compare``
    Runs inside a client container on the same docker network as the two
    servers, never over loopback. Collects ``tools/list`` and the golden calls
    from both, normalizes the volatile fields the golden file names, diffs,
    and fails on any difference not declared in the expected-changes file.

Exit codes: 0 pass (or first release), 1 parity failure, 2 error (the check
could not reach a verdict, which must never read as a pass).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

SKIP_LINE = "parity: no baseline image, first release, skipped"

SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Tool fields compared between baseline and candidate. ``_meta`` is left out on
# purpose: it carries framework bookkeeping (FastMCP tags) that is not part of
# the contract a client sees.
TOOL_FIELDS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")
CALL_FIELDS = ("isError", "content", "structuredContent")
NORMALIZED = "<normalized>"

# Keys that hold a whole answer rather than a volatile field inside one.
# Normalizing any of these would blank the answer and make every call pass.
STRUCTURAL_KEYS = {"content", "structuredContent", "isError", "text", "type", "result"}

# Variables that decide where and how the server listens. The env file goes to
# BOTH containers, so setting one there would hide exactly the regression this
# check exists for (a new default that only binds 127.0.0.1). Compared without
# case, because pydantic-settings reads them without case.
REFUSED_ENV = {
    "MCP_HOST",
    "MCP_PORT",
    "MCP_TRANSPORT",
    "FASTMCP_HOST",
    "FASTMCP_PORT",
    "FASTMCP_TRANSPORT",
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}  # noqa: S104 (refused, not bound)


# --------------------------------------------------------------------------
# Baseline resolution (standard library only)
# --------------------------------------------------------------------------


class RegistryError(Exception):
    """The registry answered in a way that is neither "found" nor "missing"."""


class ImageMissing(Exception):
    """The registry says the image does not exist, or will not show it to us."""


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


HttpFn = Callable[[str, str, dict[str, str]], HttpResponse]


def urllib_http(method: str, url: str, headers: dict[str, str]) -> HttpResponse:
    """Plain urllib transport. Non-2xx statuses are returned, not raised."""
    req = urllib.request.Request(url, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return HttpResponse(
                resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read() or b""
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(f"{method} {url}: {exc}") from exc


def parse_semver(tag: str) -> tuple[int, int, int] | None:
    m = SEMVER.match(tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def highest_semver(tags: Iterable[str], below: str | None = None) -> str | None:
    """Highest X.Y.Z tag, optionally strictly below another version.

    Only full three-part versions count: ``0.6`` and ``latest`` are moving
    aliases, ``sha256-...`` tags are attestations, and pre-releases such as
    ``0.6.0-rc1`` are not releases.
    """
    limit = parse_semver(below) if below else None
    if below and limit is None:
        raise ValueError(f"release version {below!r} is not X.Y.Z")
    best: tuple[tuple[int, int, int], str] | None = None
    for tag in tags:
        v = parse_semver(tag)
        if v is None or (limit is not None and v >= limit):
            continue
        if best is None or v > best[0]:
            best = (v, tag)
    return best[1] if best else None


def split_image(image: str) -> str:
    """``ghcr.io/owner/name`` to ``owner/name``. Only GHCR is supported."""
    image = image.strip()
    prefix = "ghcr.io/"
    if not image.startswith(prefix):
        raise ValueError(f"image must be ghcr.io/<owner>/<name>, got {image!r}")
    repo = image[len(prefix) :]
    if ":" in repo or "@" in repo or repo.count("/") < 1:
        raise ValueError(f"image must be ghcr.io/<owner>/<name> with no tag, got {image!r}")
    return repo.lower()


def ghcr_token(repo: str, http: HttpFn, credential: str | None) -> str:
    """Get a pull token. Tries the credential first, then anonymous.

    GHCR answers DENIED both for an image that does not exist and for a private
    image we cannot read; the two are indistinguishable here, which is why the
    caller cross-checks the repo's own release tags before calling it missing.
    """
    url = "https://ghcr.io/token?" + urllib.parse.urlencode(
        {"scope": f"repository:{repo}:pull", "service": "ghcr.io"}
    )
    attempts: list[dict[str, str]] = []
    if credential:
        basic = base64.b64encode(f"x-access-token:{credential}".encode()).decode()
        attempts.append({"Authorization": f"Basic {basic}"})
    attempts.append({})
    last = ""
    for headers in attempts:
        resp = http("GET", url, headers)
        if resp.status == 200:
            try:
                token = json.loads(resp.body)["token"]
            except (ValueError, KeyError) as exc:
                raise RegistryError(f"token response had no token: {resp.body[:200]!r}") from exc
            if isinstance(token, str) and token:
                return token
            raise RegistryError("token response had an empty token")
        if resp.status in (401, 403):
            last = " ".join(resp.body[:200].decode(errors="replace").split())
            continue
        raise RegistryError(f"token request returned HTTP {resp.status}")
    raise ImageMissing(f"GHCR denied a pull token for {repo}: {last}")


def _next_link(headers: dict[str, str]) -> str | None:
    link = headers.get("link", "")
    m = re.search(r"<([^>]+)>\s*;\s*rel=\"?next\"?", link)
    if not m:
        return None
    return urllib.parse.urljoin("https://ghcr.io/", m.group(1))


def list_all_tags(repo: str, token: str, http: HttpFn, max_pages: int = 500) -> list[str]:
    """Every tag of the image, following ``Link: rel=next`` to the last page."""
    url: str | None = f"https://ghcr.io/v2/{repo}/tags/list?n=100"
    seen: set[str] = set()
    tags: list[str] = []
    pages = 0
    while url:
        if url in seen:
            raise RegistryError(f"tag pagination looped at {url}")
        seen.add(url)
        pages += 1
        if pages > max_pages:
            raise RegistryError(f"more than {max_pages} tag pages, refusing to guess")
        resp = http("GET", url, {"Authorization": f"Bearer {token}"})
        if resp.status == 404:
            raise ImageMissing(f"GHCR has no image {repo} (404 on tags/list)")
        if resp.status in (401, 403):
            raise ImageMissing(f"GHCR denied tags/list for {repo} (HTTP {resp.status})")
        if resp.status != 200:
            raise RegistryError(f"tags/list for {repo} returned HTTP {resp.status}")
        try:
            page = json.loads(resp.body).get("tags") or []
        except ValueError as exc:
            raise RegistryError(f"tags/list for {repo} was not JSON") from exc
        tags.extend(str(t) for t in page)
        url = _next_link(resp.headers)
    print(f"parity: read {len(tags)} tags from {pages} page(s) of ghcr.io/{repo}")
    return tags


MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def manifest_exists(repo: str, tag: str, token: str, http: HttpFn) -> bool:
    resp = http(
        "HEAD",
        f"https://ghcr.io/v2/{repo}/manifests/{tag}",
        {"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT},
    )
    if resp.status == 200:
        return True
    if resp.status == 404:
        return False
    raise RegistryError(f"manifest request for {repo}:{tag} returned HTTP {resp.status}")


@dataclass
class BaselineResult:
    state: str  # "compare" or "first-release"
    tag: str = ""
    message: str = ""


def resolve_baseline(
    image: str,
    git_tags: list[str],
    release_version: str | None,
    http: HttpFn,
    credential: str | None,
) -> BaselineResult:
    """Decide what to compare against. Raises RegistryError when it cannot tell."""
    repo = split_image(image)
    below = release_version or None
    git_release = highest_semver(git_tags, below=below)
    try:
        token = ghcr_token(repo, http, credential)
        tags = list_all_tags(repo, token, http)
    except ImageMissing as exc:
        if git_release:
            raise RegistryError(
                f"{exc}, but this repo has released before (git tag {git_release}). "
                "Check the image input and that the job has packages: read."
            ) from exc
        return BaselineResult("first-release", message=str(exc))

    tag = highest_semver(tags, below=below)
    if tag is None:
        if git_release:
            raise RegistryError(
                f"ghcr.io/{repo} has no X.Y.Z tag"
                + (f" below {below}" if below else "")
                + f", but this repo has released before (git tag {git_release})."
            )
        return BaselineResult("first-release", message=f"ghcr.io/{repo} has no release tags")

    if not manifest_exists(repo, tag, token, http):
        raise RegistryError(f"tags/list names {tag} but its manifest is missing")

    msg = f"baseline is ghcr.io/{repo}:{tag} (manifest confirmed)"
    git_v, tag_v = parse_semver(git_release or ""), parse_semver(tag)
    if git_release and git_v is not None and tag_v is not None and git_v > tag_v:
        msg += f"; note: the highest git release tag is {git_release}"
        print(
            f"::warning::parity: highest git release tag {git_release} has no image "
            f"on GHCR; comparing against {tag}, the newest image that exists"
        )
    return BaselineResult("compare", tag=tag, message=msg)


def cmd_baseline(args: argparse.Namespace) -> int:
    git_tags: list[str] = []
    if args.git_tags_file:
        git_tags = [t for t in Path(args.git_tags_file).read_text().split() if t]
    release = (args.release_version or "").strip() or None
    try:
        result = resolve_baseline(
            args.image, git_tags, release, urllib_http, os.environ.get("GHCR_TOKEN") or None
        )
    except (RegistryError, ValueError) as exc:
        print(f"parity: ERROR resolving baseline: {exc}")
        return EXIT_ERROR
    print(f"parity: {result.message}")
    if args.github_output:
        with open(args.github_output, "a") as fh:
            fh.write(f"state={result.state}\n")
            fh.write(f"tag={result.tag}\n")
    return EXIT_PASS


# --------------------------------------------------------------------------
# Golden and expected files
# --------------------------------------------------------------------------


class ConfigError(Exception):
    """A golden or expected file is malformed."""


@dataclass
class GoldenCall:
    id: str
    tool: str
    arguments: dict[str, Any]
    keys: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)


@dataclass
class Golden:
    keys: list[str]
    patterns: list[str]
    calls: list[GoldenCall]


def format_path(segs: tuple[str, ...]) -> str:
    """Dotted path; a segment holding a dot or a quote is written as a JSON string."""
    return ".".join(json.dumps(s) if (not s or "." in s or '"' in s) else s for s in segs)


def parse_path(text: str) -> tuple[str, ...]:
    """Inverse of format_path: ``tools."a.b".description`` is three segments."""
    text = text.strip()
    segs: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            try:
                seg, i = json.JSONDecoder().raw_decode(text, i)
            except ValueError as exc:
                raise ConfigError(f"bad quoted segment in path {text!r}") from exc
            if not isinstance(seg, str):
                raise ConfigError(f"bad quoted segment in path {text!r}")
        else:
            j = text.find(".", i)
            j = len(text) if j == -1 else j
            seg = text[i:j]
            i = j
        if not seg:
            raise ConfigError(f"empty segment in path {text!r}")
        segs.append(seg)
        if i < len(text):
            if text[i] != "." or i + 1 == len(text):
                raise ConfigError(f"malformed path {text!r}")
            i += 1
    return tuple(segs)


@dataclass
class Expected:
    change: str
    baseline: str
    reason: str
    segs: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        self.segs = parse_path(self.change)
        if len(self.segs) < 2 or self.segs[0] not in ("tools", "calls"):
            raise ConfigError(
                f"change {self.change!r} must name at least one tool or call: "
                "tools.<name>... or calls.<id>..."
            )


def _load_yaml(path: Path) -> Any:
    import yaml  # imported lazily: the baseline subcommand runs without it

    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return list(value)


def load_golden(path: Path) -> Golden:
    if not path.is_file():
        raise ConfigError(f"golden file {path} not found")
    doc = _load_yaml(path) or {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} must be a mapping")
    norm = doc.get("normalize") or {}
    if not isinstance(norm, dict):
        raise ConfigError("normalize must be a mapping")
    keys = _str_list(norm.get("keys"), "normalize.keys")
    patterns = _str_list(norm.get("patterns"), "normalize.patterns")
    calls: list[GoldenCall] = []
    seen: set[str] = set()
    for i, raw in enumerate(doc.get("calls") or []):
        where = f"calls[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping")
        cid, tool = raw.get("id"), raw.get("tool")
        if not isinstance(cid, str) or not cid or not isinstance(tool, str) or not tool:
            raise ConfigError(f"{where} needs a string id and tool")
        if cid in seen:
            raise ConfigError(f"duplicate golden call id {cid!r}")
        seen.add(cid)
        if raw.get("read_only") is not True:
            raise ConfigError(
                f"golden call {cid!r} must say read_only: true. Golden calls run against "
                "the released image and the candidate; never list a call that writes."
            )
        arguments = raw.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ConfigError(f"golden call {cid!r}: arguments must be a mapping")
        call_norm = raw.get("normalize") or {}
        calls.append(
            GoldenCall(
                cid,
                tool,
                arguments,
                _str_list(call_norm.get("keys"), f"{cid}.normalize.keys"),
                _str_list(call_norm.get("patterns"), f"{cid}.normalize.patterns"),
            )
        )
    for p in patterns + [p for c in calls for p in c.patterns]:
        try:
            compiled = re.compile(p)
        except re.error as exc:
            raise ConfigError(f"bad normalize pattern {p!r}: {exc}") from exc
        if compiled.search("") is not None:
            raise ConfigError(
                f"normalize pattern {p!r} matches the empty string, so it can match "
                "anything; anchor it to the volatile text it is meant to hide"
            )
    for k in keys + [k for c in calls for k in c.keys]:
        if k in STRUCTURAL_KEYS:
            raise ConfigError(
                f"normalize key {k!r} holds a whole answer, not a volatile field; "
                "name the field inside it instead"
            )
    return Golden(keys, patterns, calls)


def load_expected(path: Path) -> list[Expected]:
    if not path.is_file():
        print(f"parity: no expected-changes file at {path}; every difference fails")
        return []
    doc = _load_yaml(path) or {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} must be a mapping")
    out: list[Expected] = []
    for i, raw in enumerate(doc.get("changes") or []):
        where = f"{path} changes[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping")
        change, baseline, reason = raw.get("change"), raw.get("baseline"), raw.get("reason")
        if not isinstance(change, str) or not change.strip():
            raise ConfigError(f"{where} needs change: the path of what changed")
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ConfigError(f"{where} needs reason: a plain-English sentence")
        if not isinstance(baseline, str) or parse_semver(baseline) is None:
            raise ConfigError(f"{where} needs baseline: the X.Y.Z release it differs from")
        try:
            out.append(Expected(change.strip(), baseline.strip(), reason.strip()))
        except ConfigError as exc:
            raise ConfigError(f"{where}: {exc}") from exc
    return out


# --------------------------------------------------------------------------
# Normalizing and diffing
# --------------------------------------------------------------------------


def decode_json_strings(value: Any) -> Any:
    """Parse strings that hold JSON objects or arrays, recursively.

    FastMCP wraps a tool's JSON text in ``structuredContent.result`` as a string,
    so without this a volatile field nested in that string could not be named.
    """
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in ("{", "["):
            try:
                return decode_json_strings(json.loads(s))
            except ValueError:
                return value
        return value
    if isinstance(value, list):
        return [decode_json_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: decode_json_strings(v) for k, v in value.items()}
    return value


def normalize(value: Any, keys: set[str], patterns: list[re.Pattern[str]]) -> Any:
    if isinstance(value, dict):
        return {
            k: (NORMALIZED if k in keys else normalize(v, keys, patterns)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalize(v, keys, patterns) for v in value]
    if isinstance(value, str):
        for p in patterns:
            value = p.sub(NORMALIZED, value)
        return value
    return value


@dataclass
class Difference:
    segs: tuple[str, ...]
    kind: str  # added, removed, changed
    baseline: Any = None
    candidate: Any = None

    @property
    def path(self) -> str:
        return format_path(self.segs)


def deep_diff(base: Any, cand: Any, path: tuple[str, ...]) -> list[Difference]:
    if isinstance(base, dict) and isinstance(cand, dict):
        out: list[Difference] = []
        for k in sorted(set(base) | set(cand), key=str):
            sub = (*path, str(k))
            if k not in cand:
                out.append(Difference(sub, "removed", base[k], None))
            elif k not in base:
                out.append(Difference(sub, "added", None, cand[k]))
            else:
                out.extend(deep_diff(base[k], cand[k], sub))
        return out
    if isinstance(base, list) and isinstance(cand, list):
        out = []
        for i in range(max(len(base), len(cand))):
            sub = (*path, str(i))
            if i >= len(cand):
                out.append(Difference(sub, "removed", base[i], None))
            elif i >= len(base):
                out.append(Difference(sub, "added", None, cand[i]))
            else:
                out.extend(deep_diff(base[i], cand[i], sub))
        return out
    if base != cand or type(base) is not type(cand):
        return [Difference(path, "changed", base, cand)]
    return []


def covers(change: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """True when ``path`` is ``change`` or under it, compared segment by segment."""
    return path[: len(change)] == change


@dataclass
class Verdict:
    code: int
    line: str
    summary: str
    warnings: list[str] = field(default_factory=list)


def tool_record(dumped: dict[str, Any]) -> dict[str, Any]:
    """The parts of one tools/list entry that are compared."""
    return {k: dumped[k] for k in TOOL_FIELDS if k in dumped}


def call_record(dumped: dict[str, Any]) -> dict[str, Any]:
    """The parts of one tools/call result that are compared."""
    return {k: dumped[k] for k in CALL_FIELDS if k in dumped}


# Protocol scaffolding present in every answer; it never counts as content.
SCAFFOLD_KEYS = {"isError", "type", "mimeType", "annotations", "_meta"}


def surviving_leaves(raw: Any, norm: Any) -> int:
    """Count content leaves normalization left untouched.

    A leaf counts when it is a non-empty string, or a number, that is identical
    before and after normalization. Booleans and protocol scaffolding (``type``,
    ``isError`` and the like) are present in every answer and never count.
    """
    if isinstance(raw, dict):
        if not isinstance(norm, dict):
            return 0
        return sum(
            surviving_leaves(v, norm.get(k))
            for k, v in raw.items()
            if k not in SCAFFOLD_KEYS and k in norm
        )
    if isinstance(raw, list):
        if not isinstance(norm, list) or len(norm) != len(raw):
            return 0
        return sum(surviving_leaves(a, b) for a, b in zip(raw, norm, strict=True))
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, str):
        return 1 if raw.strip() and norm == raw else 0
    if isinstance(raw, int | float):
        return 1 if type(norm) is type(raw) and norm == raw else 0
    return 0


def normalize_calls(calls: dict[str, Any], golden: Golden | None) -> dict[str, Any]:
    """Apply the golden file's global and per-call normalization to each answer."""
    if golden is None:
        return dict(calls)
    by_id = {c.id: c for c in golden.calls}
    out: dict[str, Any] = {}
    for cid, record in calls.items():
        call = by_id.get(cid)
        keys = set(golden.keys) | set(call.keys if call else [])
        pats = [re.compile(p) for p in golden.patterns + (call.patterns if call else [])]
        out[cid] = normalize(record, keys, pats)
    return out


def call_errored(record: Any) -> bool:
    return isinstance(record, dict) and (record.get("isError") is True or "protocolError" in record)


def normalize_lines(golden: Golden | None) -> list[str]:
    if golden is None:
        return []
    lines = [
        "Normalized before the diff: keys "
        + (", ".join(f"`{k}`" for k in golden.keys) or "none")
        + "; patterns "
        + (", ".join(f"`{p}`" for p in golden.patterns) or "none")
        + "."
    ]
    for c in golden.calls:
        if c.keys or c.patterns:
            lines.append(
                f"Call `{c.id}` also normalizes keys "
                + (", ".join(f"`{k}`" for k in c.keys) or "none")
                + "; patterns "
                + (", ".join(f"`{p}`" for p in c.patterns) or "none")
                + "."
            )
    return [*lines, ""]


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
    text = text.replace("\n", "\\n").replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def judge(
    base: dict[str, Any],
    cand: dict[str, Any],
    expected: list[Expected],
    baseline_label: str,
    golden: Golden | None = None,
) -> Verdict:
    """Turn two snapshots into a verdict. Unreachable servers always fail."""
    lines = [f"## MCP release parity against `{baseline_label}`", ""]
    unreachable = [
        (role, snap)
        for role, snap in (("baseline", base), ("candidate", cand))
        if not snap.get("reachable")
    ]
    if unreachable:
        no_network = [r for r, s in unreachable if s.get("stage", "connect") == "connect"]
        for role, snap in unreachable:
            lines.append(f"- **{role}**: {snap.get('error')}")
        lines.append("")
        if no_network:
            lines.append(
                "A server that is healthy inside its own container but refuses other "
                "containers is usually bound to 127.0.0.1 instead of 0.0.0.0."
            )
            what = " and ".join(no_network) + " unreachable over the docker network"
        else:
            what = " and ".join(r for r, _ in unreachable) + " failed after connecting"
        return Verdict(
            EXIT_FAIL,
            f"parity: FAIL {what} (baseline {baseline_label})",
            "\n".join(lines) + "\n",
        )

    empty = [role for role, snap in (("baseline", base), ("candidate", cand)) if not snap["tools"]]
    if empty:
        lines.append(
            f"- **{' and '.join(empty)}** listed 0 tools. Two empty lists are equal, which "
            "proves nothing; usually the server failed to register its tools or the "
            "client was not authorized to see them."
        )
        return Verdict(
            EXIT_ERROR,
            f"parity: ERROR {' and '.join(empty)} listed 0 tools, nothing to compare "
            f"(baseline {baseline_label})",
            "\n".join(lines) + "\n",
        )

    base_calls = normalize_calls(base["calls"], golden)
    cand_calls = normalize_calls(cand["calls"], golden)
    erased = [
        f"golden call {cid}"
        for cid in sorted(base["calls"])
        if surviving_leaves(base["calls"][cid], base_calls[cid]) == 0
    ] + [
        f"tool {name}"
        for name in sorted(base["tools"])
        if surviving_leaves(base["tools"][name], base["tools"][name]) == 0
    ]
    if erased:
        lines += [
            f"- **{what}**: after normalization the baseline's answer has no non-empty "
            "text or number left that normalization did not touch, so any change to it "
            "would pass."
            for what in erased
        ]
        lines += ["", *normalize_lines(golden)]
        more = f" (and {len(erased) - 1} more)" if len(erased) > 1 else ""
        return Verdict(
            EXIT_ERROR,
            f"parity: ERROR {erased[0]}: normalization leaves nothing to compare{more}",
            "\n".join(lines) + "\n",
        )

    warnings = [
        f"golden call `{cid}` returned an error on the baseline {baseline_label}, so it "
        "proves nothing about behavior; fix its arguments or remove it"
        for cid, rec in sorted(base["calls"].items())
        if call_errored(rec)
    ]
    lines += [f"> Warning: {w}." for w in warnings]
    if warnings:
        lines.append("")
    lines += normalize_lines(golden)

    diffs = deep_diff(base["tools"], cand["tools"], ("tools",)) + deep_diff(
        base_calls, cand_calls, ("calls",)
    )
    active = [e for e in expected if parse_semver(e.baseline) == parse_semver(baseline_label)]
    stale = [e for e in expected if e not in active]
    declared: list[tuple[Difference, Expected]] = []
    undeclared: list[Difference] = []
    for d in diffs:
        match = next((e for e in active if covers(e.segs, d.segs)), None)
        if match:
            declared.append((d, match))
        else:
            undeclared.append(d)
    used = {id(e) for _, e in declared}
    unused = [e for e in active if id(e) not in used]

    n_tools, n_calls = len(cand["tools"]), len(cand["calls"])
    lines.append(
        f"Compared {len(base['tools'])} baseline and {n_tools} candidate tools, "
        f"and {n_calls} golden call(s)."
    )
    lines.append("")
    if undeclared:
        lines += [
            f"### {len(undeclared)} undeclared difference(s): these fail the check",
            "",
            "| path | change | baseline | candidate |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| `{d.path}` | {d.kind} | {_short(d.baseline)} | {_short(d.candidate)} |"
            for d in undeclared
        ]
        lines += [
            "",
            "If a change is intended, declare it in the expected-changes file with the "
            f"path, `baseline: {baseline_label}` and a plain-English reason.",
            "",
        ]
    if declared:
        lines += [
            f"### {len(declared)} declared difference(s): allowed",
            "",
            "| path | change | reason |",
            "|---|---|---|",
        ]
        lines += [f"| `{d.path}` | {d.kind} | {_short(e.reason, 300)} |" for d, e in declared]
        lines.append("")
    if unused:
        lines.append("Declared for this baseline but not seen (consider removing):")
        lines += [f"- `{e.change}`: {e.reason}" for e in unused]
        lines.append("")
    if stale:
        lines.append("Ignored, written against another baseline (safe to delete):")
        lines += [f"- `{e.change}` (baseline {e.baseline})" for e in stale]
        lines.append("")
    if not diffs:
        lines.append("No differences.")

    if undeclared:
        line = (
            f"parity: FAIL {len(undeclared)} undeclared difference(s) against baseline "
            f"{baseline_label}"
        )
        code = EXIT_FAIL
    else:
        line = (
            f"parity: PASS against baseline {baseline_label} ({n_tools} tools, "
            f"{n_calls} golden calls, {len(declared)} declared differences)"
        )
        code = EXIT_PASS
    return Verdict(code, line, "\n".join(lines) + "\n", warnings)


# --------------------------------------------------------------------------
# Collecting a snapshot over MCP (needs the pinned mcp client)
# --------------------------------------------------------------------------


def check_not_loopback(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in LOOPBACK_HOSTS or host.startswith("127."):
        raise ConfigError(
            f"{url} is a loopback address. Parity must reach the server over the docker "
            "network, the way a LAN client would."
        )


async def _collect_once(
    url: str,
    bearer: str | None,
    golden: Golden,
    attempt_timeout: float,
    progress: dict[str, str],
) -> dict[str, Any]:
    import anyio
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp_types import PaginatedRequestParams

    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    tools: dict[str, Any] = {}
    calls: dict[str, Any] = {}
    with anyio.fail_after(attempt_timeout):
        async with (
            httpx2.AsyncClient(headers=headers, timeout=30) as http,
            streamable_http_client(url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            progress["stage"] = "tools/list"
            cursor: str | None = None
            while True:
                params = PaginatedRequestParams(cursor=cursor) if cursor else None
                page = await session.list_tools(params=params)
                for tool in page.tools:
                    dumped = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    tools[tool.name] = tool_record(dumped)
                cursor = page.next_cursor
                if not cursor:
                    break
            for call in golden.calls:
                progress["stage"] = f"golden call {call.id}"
                try:
                    result = await session.call_tool(call.tool, call.arguments)
                    dumped = result.model_dump(mode="json", by_alias=True, exclude_none=True)
                    record: Any = call_record(dumped)
                except Exception as exc:  # a protocol error is a result worth diffing
                    record = {"protocolError": str(exc)}
                # Stored raw; judge() normalizes, so it can also check that the
                # normalization left something to compare.
                calls[call.id] = decode_json_strings(record)
    return {"reachable": True, "tools": tools, "calls": calls}


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def collect(
    url: str, bearer: str | None, golden: Golden, wait_seconds: float
) -> dict[str, Any]:
    """Retry until the server answers or the wait runs out. Never raises."""
    import anyio

    deadline = anyio.current_time() + wait_seconds
    attempts = 0
    last = "no attempt made"
    while True:
        attempts += 1
        progress = {"stage": "connect"}
        try:
            return await _collect_once(url, bearer, golden, 30, progress)
        except Exception as exc:
            last = _describe(exc)
        if progress["stage"] != "connect":
            # It answered the MCP handshake, so this is not a network problem and
            # retrying will not help: report where it broke.
            return {
                "reachable": False,
                "stage": progress["stage"],
                "error": f"{url} answered initialize, then failed during "
                f"{progress['stage']}: {last}",
                "tools": {},
                "calls": {},
            }
        if anyio.current_time() >= deadline:
            return {
                "reachable": False,
                "stage": "connect",
                "error": f"{url} did not complete the MCP handshake after {attempts} "
                f"attempts over {wait_seconds:.0f}s; last error: {last}",
                "tools": {},
                "calls": {},
            }
        await anyio.sleep(2)


def cmd_compare(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def finish(v: Verdict) -> int:
        (out / "summary.md").write_text(v.summary)
        (out / "verdict.txt").write_text(v.line + "\n")
        print(v.summary)
        for w in v.warnings:
            print(f"::warning::parity: {w}")
        print(v.line)
        return v.code

    try:
        check_not_loopback(args.baseline_url)
        check_not_loopback(args.candidate_url)
        golden = load_golden(Path(args.golden))
        expected = load_expected(Path(args.expected))
        if parse_semver(args.baseline_label) is None:
            raise ConfigError(f"baseline label {args.baseline_label!r} is not X.Y.Z")
    except ConfigError as exc:
        return finish(
            Verdict(EXIT_ERROR, f"parity: ERROR {exc}", f"## MCP release parity\n\n{exc}\n")
        )

    import anyio

    bearer = os.environ.get("MCP_AUTH_TOKEN") or None

    async def both() -> tuple[dict[str, Any], dict[str, Any]]:
        base = await collect(args.baseline_url, bearer, golden, args.wait)
        cand = await collect(args.candidate_url, bearer, golden, args.wait)
        return base, cand

    base, cand = anyio.run(both)
    (out / "baseline.json").write_text(json.dumps(base, indent=2, sort_keys=True))
    (out / "candidate.json").write_text(json.dumps(cand, indent=2, sort_keys=True))
    return finish(judge(base, cand, expected, args.baseline_label, golden))


# --------------------------------------------------------------------------
# The dummy env file (standard library only; runs on the CI runner)
# --------------------------------------------------------------------------


def parse_env_file(text: str) -> list[tuple[str, str | None]]:
    """Names and values in docker --env-file order. A bare NAME has value None."""
    out: list[tuple[str, str | None]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        out.append((name.strip(), value if sep else None))
    return out


def image_env(doc: Any) -> dict[str, str]:
    """``docker image inspect --format '{{json .Config.Env}}'`` output as a dict."""
    env: dict[str, str] = {}
    for item in doc or []:
        name, _, value = str(item).partition("=")
        env[name.upper()] = value
    return env


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def check_env(text: str, baseline_image_env: dict[str, str]) -> list[str]:
    """Refuse listen settings in the env file; return the variable names it sets.

    The one exception is the line the template writes for a stdio server:
    MCP_TRANSPORT=streamable-http, allowed only while the RELEASED image itself
    defaults to stdio. Once a release serves HTTP by default, the line is refused.
    """
    names: list[str] = []
    for name, value in parse_env_file(text):
        if not ENV_NAME.match(name):
            # Docker strips a leading BOM and may read a name differently from us;
            # a name we cannot read plainly is refused, never waved through.
            raise ConfigError(
                f"the env file has a variable name that is not plain ASCII "
                f"[A-Za-z_][A-Za-z0-9_]*: {name!r}"
            )
        names.append(name)
        upper = name.upper()
        if upper not in REFUSED_ENV:
            continue
        stdio_case = (
            upper == "MCP_TRANSPORT"
            and value is not None
            and value.strip() == "streamable-http"
            and baseline_image_env.get("MCP_TRANSPORT", "").strip() == "stdio"
        )
        if not stdio_case:
            raise ConfigError(
                f"the env file sets {name}. It goes to both containers, so it would "
                "hide a change in where the server listens; remove it (allowed: "
                "MCP_TRANSPORT=streamable-http while the released image defaults to stdio)"
            )
    return names


def cmd_check_env(args: argparse.Namespace) -> int:
    summary = Path(args.summary)
    shown = args.env_file_label or args.env_file or "none"
    try:
        base_env = image_env(json.loads(Path(args.baseline_image_env).read_text()))
        # utf-8-sig drops a byte order mark, as docker's env-file parser does, so a
        # BOM cannot hide MCP_HOST on the first line.
        text = Path(args.env_file).read_text(encoding="utf-8-sig") if args.env_file else ""
        names = check_env(text, base_env)
    except (ConfigError, OSError, ValueError) as exc:
        summary.write_text(f"Env file `{shown}`: refused. {exc}\n\n")
        print(f"parity: ERROR {exc}")
        return EXIT_ERROR
    listed = ", ".join(f"`{n}`" for n in names) or "nothing"
    note = f"Env file `{shown}` sets {listed} (names only, values not shown).\n\n"
    summary.write_text(note)
    print(f"parity: {note.strip()}")
    return EXIT_PASS


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("baseline", help="find the last released image on GHCR")
    b.add_argument("--image", required=True, help="ghcr.io/<owner>/<name>")
    b.add_argument("--release-version", default="", help="only consider tags below this")
    b.add_argument("--git-tags-file", default="", help="the repo's git tags, one per line")
    b.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    b.set_defaults(func=cmd_baseline)

    c = sub.add_parser("compare", help="diff two running servers over the network")
    c.add_argument("--baseline-url", required=True)
    c.add_argument("--candidate-url", required=True)
    c.add_argument("--baseline-label", required=True, help="the baseline's X.Y.Z tag")
    c.add_argument("--golden", required=True)
    c.add_argument("--expected", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--wait", type=float, default=60.0, help="seconds to wait for each server")
    c.set_defaults(func=cmd_compare)

    e = sub.add_parser("check-env", help="refuse listen settings in the dummy env file")
    e.add_argument("--env-file", default="", help="the dummy env file, if any")
    e.add_argument("--env-file-label", default="", help="how to name it in the summary")
    e.add_argument("--baseline-image-env", required=True, help="JSON list of K=V")
    e.add_argument("--summary", required=True, help="markdown file to write")
    e.set_defaults(func=cmd_check_env)

    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
