"""Unit tests for parity.py: baseline resolution, config parsing, diff and verdict.

The network-facing half (collecting from live servers) is proven by the
mutation controls in .github/workflows/ci.yml, which run real containers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import parity
from parity import HttpResponse

REPO = "pete-builds/mcp-demo"


class FakeGhcr:
    """A GHCR double: token endpoint, paged tags/list, manifest HEAD."""

    def __init__(
        self,
        pages: list[list[str]] | None,
        token_status: int = 200,
        manifest_status: int = 200,
        anonymous_ok: bool = True,
    ) -> None:
        self.pages = pages
        self.token_status = token_status
        self.manifest_status = manifest_status
        self.anonymous_ok = anonymous_ok
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str]) -> HttpResponse:
        self.calls.append((method, url))
        if url.startswith("https://ghcr.io/token"):
            authed = "Authorization" in headers
            if self.token_status != 200 or (not authed and not self.anonymous_ok):
                status = self.token_status if self.token_status != 200 else 403
                return HttpResponse(status, {}, b'{"errors":[{"code":"DENIED"}]}')
            return HttpResponse(200, {}, json.dumps({"token": "t"}).encode())
        if "/tags/list" in url:
            if self.pages is None:
                return HttpResponse(404, {}, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')
            idx = 0
            if "last=" in url:
                idx = int(url.split("last=")[1].split("&")[0].replace("p", ""))
            headers_out = {}
            if idx + 1 < len(self.pages):
                headers_out["link"] = f'</v2/{REPO}/tags/list?last=p{idx + 1}&n=100>; rel="next"'
            return HttpResponse(200, headers_out, json.dumps({"tags": self.pages[idx]}).encode())
        if "/manifests/" in url:
            assert method == "HEAD"
            return HttpResponse(self.manifest_status, {}, b"")
        raise AssertionError(f"unexpected request {method} {url}")


def attestations(n: int) -> list[str]:
    return [f"sha256-{i:064x}" for i in range(n)]


# --- semver -----------------------------------------------------------------


def test_highest_semver_ignores_aliases_attestations_and_prereleases() -> None:
    tags = ["latest", "0.9", "0.10.0", "0.9.9", "0.11.0-rc1", "sha256-abc", "v0.2.0"]
    assert parity.highest_semver(tags) == "0.10.0"


def test_highest_semver_is_numeric_not_lexical() -> None:
    assert parity.highest_semver(["0.9.0", "0.10.0", "0.2.0"]) == "0.10.0"


def test_highest_semver_below_release_version() -> None:
    tags = ["0.5.0", "0.6.0", "0.7.0"]
    assert parity.highest_semver(tags, below="v0.7.0") == "0.6.0"
    assert parity.highest_semver(tags, below="0.5.0") is None


def test_highest_semver_rejects_bad_release_version() -> None:
    with pytest.raises(ValueError):
        parity.highest_semver(["0.1.0"], below="next")


# --- baseline resolution ----------------------------------------------------


def test_newest_release_on_page_two_is_found() -> None:
    # The mcp-autodeploy.sh miss: attestation tags fill page one, the newest
    # release sits on page two. Reading one page would answer 0.26.0.
    page1 = ["0.26.0", "0.26", *attestations(98)]
    page2 = [*attestations(50), "0.29.0", "0.29", "latest"]
    http = FakeGhcr([page1, page2])
    result = parity.resolve_baseline(f"ghcr.io/{REPO}", ["v0.29.0"], None, http, None)
    assert result.state == "compare"
    assert result.tag == "0.29.0"
    assert ("HEAD", f"https://ghcr.io/v2/{REPO}/manifests/0.29.0") in http.calls
    assert sum("/tags/list" in u for _, u in http.calls) == 2


def test_three_pages_are_all_read() -> None:
    http = FakeGhcr([["0.1.0"], attestations(3), ["0.3.0"]])
    result = parity.resolve_baseline(f"ghcr.io/{REPO}", [], None, http, None)
    assert result.tag == "0.3.0"


def test_release_version_excludes_the_tag_being_released() -> None:
    http = FakeGhcr([["0.5.0", "0.6.0", "0.7.0"]])
    result = parity.resolve_baseline(f"ghcr.io/{REPO}", ["v0.7.0"], "v0.7.0", http, None)
    assert result.tag == "0.6.0"


def test_missing_manifest_is_an_error_not_a_skip() -> None:
    http = FakeGhcr([["0.6.0"]], manifest_status=404)
    with pytest.raises(parity.RegistryError, match="manifest"):
        parity.resolve_baseline(f"ghcr.io/{REPO}", ["v0.6.0"], None, http, None)


def test_no_image_and_no_release_tags_is_first_release() -> None:
    http = FakeGhcr(None)
    result = parity.resolve_baseline(f"ghcr.io/{REPO}", ["not-a-release"], None, http, None)
    assert result.state == "first-release"


def test_denied_token_and_no_release_tags_is_first_release() -> None:
    http = FakeGhcr([["0.1.0"]], token_status=403)
    result = parity.resolve_baseline(f"ghcr.io/{REPO}", [], None, http, "ghs_x")
    assert result.state == "first-release"


def test_unreadable_image_after_a_release_is_an_error() -> None:
    # GHCR says DENIED both for "no such image" and "private, no access". If
    # the repo has released before, that must not read as a first release.
    http = FakeGhcr([["0.1.0"]], token_status=403)
    with pytest.raises(parity.RegistryError, match="released before"):
        parity.resolve_baseline(f"ghcr.io/{REPO}", ["v0.4.0"], None, http, "ghs_x")


def test_missing_image_after_a_release_is_an_error() -> None:
    http = FakeGhcr(None)
    with pytest.raises(parity.RegistryError, match="released before"):
        parity.resolve_baseline(f"ghcr.io/{REPO}", ["v0.4.0"], None, http, None)


def test_registry_5xx_is_an_error() -> None:
    http = FakeGhcr([["0.1.0"]], token_status=503)
    with pytest.raises(parity.RegistryError):
        parity.resolve_baseline(f"ghcr.io/{REPO}", [], None, http, None)


def test_credential_falls_back_to_anonymous() -> None:
    class DenyAuthed(FakeGhcr):
        def __call__(self, method: str, url: str, headers: dict[str, str]) -> HttpResponse:
            if url.startswith("https://ghcr.io/token") and "Authorization" in headers:
                self.calls.append((method, url))
                return HttpResponse(403, {}, b"DENIED")
            return super().__call__(method, url, headers)

    http = DenyAuthed([["0.2.0"]])
    result = parity.resolve_baseline(f"ghcr.io/{REPO}", [], None, http, "ghs_x")
    assert result.tag == "0.2.0"


def test_pagination_loop_is_refused() -> None:
    def looping(method: str, url: str, headers: dict[str, str]) -> HttpResponse:
        return HttpResponse(
            200,
            {"link": f'</v2/{REPO}/tags/list?last=x&n=100>; rel="next"'},
            b'{"tags": []}',
        )

    with pytest.raises(parity.RegistryError, match="looped"):
        parity.list_all_tags(REPO, "t", looping)


@pytest.mark.parametrize(
    "image",
    ["docker.io/pete/x", "ghcr.io/pete-builds/x:1.0.0", "ghcr.io/x", "ghcr.io/a/b@sha256:1"],
)
def test_bad_image_names_are_refused(image: str) -> None:
    with pytest.raises(ValueError):
        parity.split_image(image)


# --- golden and expected files ----------------------------------------------


def write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_golden_call_must_be_marked_read_only(tmp_path: Path) -> None:
    p = write(tmp_path, "g.yaml", "calls:\n  - id: a\n    tool: hello\n")
    with pytest.raises(parity.ConfigError, match="read_only"):
        parity.load_golden(p)


def test_golden_duplicate_ids_are_refused(tmp_path: Path) -> None:
    text = (
        "calls:\n"
        "  - {id: a, tool: hello, read_only: true}\n"
        "  - {id: a, tool: hello, read_only: true}\n"
    )
    with pytest.raises(parity.ConfigError, match="duplicate"):
        parity.load_golden(write(tmp_path, "g.yaml", text))


def test_missing_golden_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(parity.ConfigError, match="not found"):
        parity.load_golden(tmp_path / "nope.yaml")


def test_template_starter_files_parse() -> None:
    root = Path(__file__).parent.parent / "template" / "parity"
    golden = parity.load_golden(root / "golden.yaml")
    assert golden.calls == []
    assert parity.load_expected(root / "expected.yaml") == []


@pytest.mark.parametrize(
    "entry, match",
    [
        ("{baseline: 0.1.0, reason: A long enough reason.}", "change"),
        ("{change: tools.x, reason: A long enough reason.}", "baseline"),
        ("{change: tools.x, baseline: latest, reason: A long enough reason.}", "baseline"),
        ("{change: tools.x, baseline: 0.1.0}", "reason"),
        ("{change: tools.x, baseline: 0.1.0, reason: short}", "reason"),
        ("{change: transport, baseline: 0.1.0, reason: A long enough reason.}", "unreachable"),
    ],
)
def test_expected_entries_are_validated(tmp_path: Path, entry: str, match: str) -> None:
    p = write(tmp_path, "e.yaml", f"changes:\n  - {entry}\n")
    with pytest.raises(parity.ConfigError, match=match):
        parity.load_expected(p)


# --- normalize, diff, judge -------------------------------------------------


def snap(tools: dict[str, object], calls: dict[str, object] | None = None) -> dict[str, object]:
    return {"reachable": True, "tools": tools, "calls": calls or {}}


HELLO = {
    "name": "hello",
    "description": "Sample tool. Replace with your own.",
    "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
}


def test_identical_snapshots_pass() -> None:
    v = parity.judge(snap({"hello": HELLO}), snap({"hello": HELLO}), [], "0.1.0")
    assert v.code == parity.EXIT_PASS
    assert v.line.startswith("parity: PASS against baseline 0.1.0")


def test_renamed_parameter_fails() -> None:
    renamed = json.loads(json.dumps(HELLO))
    renamed["inputSchema"]["properties"] = {"who": {"type": "string"}}
    v = parity.judge(snap({"hello": HELLO}), snap({"hello": renamed}), [], "0.1.0")
    assert v.code == parity.EXIT_FAIL
    assert "tools.hello.inputSchema.properties.name" in v.summary
    assert "tools.hello.inputSchema.properties.who" in v.summary


def test_declared_change_passes_and_is_listed() -> None:
    renamed = json.loads(json.dumps(HELLO))
    renamed["inputSchema"]["properties"] = {"who": {"type": "string"}}
    exp = [parity.Expected("tools.hello.inputSchema.properties", "0.1.0", "Renamed name to who.")]
    v = parity.judge(snap({"hello": HELLO}), snap({"hello": renamed}), exp, "0.1.0")
    assert v.code == parity.EXIT_PASS
    assert "2 declared differences" in v.line
    assert "Renamed name to who." in v.summary


def test_declaration_for_another_baseline_does_not_apply() -> None:
    renamed = json.loads(json.dumps(HELLO))
    renamed["description"] = "Sample tool."
    exp = [parity.Expected("tools.hello", "0.0.9", "Written against an older release.")]
    v = parity.judge(snap({"hello": HELLO}), snap({"hello": renamed}), exp, "0.1.0")
    assert v.code == parity.EXIT_FAIL
    assert "another baseline" in v.summary


def test_declaration_is_a_path_prefix_not_a_substring() -> None:
    other = dict(HELLO, description="changed")
    exp = [parity.Expected("tools.hell", "0.1.0", "Should not cover tools.hello.")]
    v = parity.judge(snap({"hello": HELLO}), snap({"hello": other}), exp, "0.1.0")
    assert v.code == parity.EXIT_FAIL


def test_removed_tool_fails() -> None:
    v = parity.judge(snap({"hello": HELLO}), snap({}), [], "0.1.0")
    assert v.code == parity.EXIT_FAIL
    assert "`tools.hello` | removed" in v.summary


def test_unreachable_candidate_fails_and_names_loopback() -> None:
    cand = {"reachable": False, "stage": "connect", "error": "ConnectError", "tools": {}}
    v = parity.judge(snap({"hello": HELLO}), cand, [], "0.1.0")
    assert v.code == parity.EXIT_FAIL
    assert "candidate unreachable over the docker network" in v.line
    assert "127.0.0.1" in v.summary


def test_failure_after_connect_is_not_called_unreachable() -> None:
    cand = {"reachable": False, "stage": "tools/list", "error": "boom", "tools": {}}
    v = parity.judge(snap({"hello": HELLO}), cand, [], "0.1.0")
    assert v.code == parity.EXIT_FAIL
    assert "failed after connecting" in v.line


def test_golden_call_difference_fails() -> None:
    base = snap({"hello": HELLO}, {"hi": {"content": [{"text": {"greeting": "hello, parity"}}]}})
    cand = snap({"hello": HELLO}, {"hi": {"content": [{"text": {"greeting": "hello, world"}}]}})
    v = parity.judge(base, cand, [], "0.1.0")
    assert v.code == parity.EXIT_FAIL
    assert "calls.hi.content.0.text.greeting" in v.summary


def test_normalize_blanks_named_keys_and_patterns() -> None:
    import re

    record = parity.decode_json_strings(
        {"text": '{"at": "2026-11-26T10:00:00Z", "n": 1, "msg": "id 42 at 2026-11-26T10:00:00Z"}'}
    )
    out = parity.normalize(record, {"at"}, [re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:]+Z")])
    assert out == {"text": {"at": "<normalized>", "n": 1, "msg": "id 42 at <normalized>"}}


def test_loopback_urls_are_refused() -> None:
    for url in ("http://127.0.0.1:3800/mcp", "http://localhost:3800/mcp", "http://[::1]:1/mcp"):
        with pytest.raises(parity.ConfigError, match="loopback"):
            parity.check_not_loopback(url)
    parity.check_not_loopback("http://parity-1-candidate:3800/mcp")


def test_warns_only_when_git_is_ahead_of_ghcr(capsys: pytest.CaptureFixture[str]) -> None:
    parity.resolve_baseline(f"ghcr.io/{REPO}", ["v0.29.0"], None, FakeGhcr([["0.30.0"]]), None)
    assert "::warning::" not in capsys.readouterr().out
    parity.resolve_baseline(f"ghcr.io/{REPO}", ["v0.31.0"], None, FakeGhcr([["0.30.0"]]), None)
    assert "::warning::parity: highest git release tag v0.31.0" in capsys.readouterr().out
