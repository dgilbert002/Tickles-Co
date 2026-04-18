"""Phase-13 smoke tests for the operator CLIs.

We verify that every CLI:

1. Builds its argparse tree without raising (module-level import side effects clean).
2. Exposes the expected subcommands.
3. Returns a non-zero exit code for unknown subcommands (argparse standard).
4. Does not crash when invoked with `--help`.

We deliberately do NOT test DB-backed subcommands here — those land in their
own phase suites (17 for gateway, 21 for validator, 22 for forward-test).
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import List

import pytest

from shared.cli import forward_test_cli, gateway_cli, validator_cli


CLIS = [
    ("gateway_cli", gateway_cli, {"status", "services", "subscribe", "unsubscribe", "replay"}),
    ("validator_cli", validator_cli, {"status", "windows", "pair", "check"}),
    ("forward_test_cli", forward_test_cli, {"status", "start", "stop", "results"}),
]


@pytest.mark.parametrize("name,mod,expected", CLIS)
def test_parser_builds(name: str, mod, expected: set) -> None:
    parser = mod._build()
    actions = [a for a in parser._actions if a.dest == "subcommand"]
    assert actions, f"{name} has no subcommand action"
    choices = set(actions[0].choices.keys())
    assert choices == expected, f"{name} subcommands drifted: {choices} != {expected}"


@pytest.mark.parametrize("name,mod,_expected", CLIS)
def test_help_runs(name: str, mod, _expected: set) -> None:
    parser = mod._build()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("name,mod,_expected", CLIS)
def test_unknown_subcommand_rejected(name: str, mod, _expected: set) -> None:
    parser = mod._build()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["does-not-exist"])
    # argparse emits exit 2 for usage errors
    assert exc.value.code == 2


def _run_cli_subprocess(module: str, argv: List[str]) -> subprocess.CompletedProcess:
    """Invoke the CLI via `python -m` — catches packaging mistakes."""
    return subprocess.run(
        [sys.executable, "-m", module, *argv],
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.mark.parametrize("module", [
    "shared.cli.gateway_cli",
    "shared.cli.validator_cli",
    "shared.cli.forward_test_cli",
])
def test_help_via_python_m(module: str) -> None:
    res = _run_cli_subprocess(module, ["--help"])
    assert res.returncode == 0, res.stderr
    assert "usage:" in res.stdout


@pytest.mark.parametrize("module,sub", [
    ("shared.cli.gateway_cli", "services"),
    ("shared.cli.validator_cli", "windows"),
])
def test_stateless_commands_emit_json(module: str, sub: str) -> None:
    """`services` and `windows` must emit a single parseable JSON line."""
    res = _run_cli_subprocess(module, [sub])
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout.strip())
    assert payload["ok"] is True


@pytest.mark.parametrize("module,sub", [
    ("shared.cli.gateway_cli", "subscribe"),
    ("shared.cli.validator_cli", "pair"),
    ("shared.cli.forward_test_cli", "start"),
])
def test_stub_commands_report_lands_in_phase(module: str, sub: str) -> None:
    """Phase-13 stubs must identify themselves clearly and exit with code 2."""
    args_for_sub = {
        "subscribe": ["--venue", "binance", "--symbol", "BTC/USDT"],
        "pair": ["--trade-id", "1"],
        "start": ["--strategy-id", "1", "--symbol", "BTC/USDT"],
    }
    res = _run_cli_subprocess(module, [sub, *args_for_sub[sub]])
    assert res.returncode == 2, res.stderr
    payload = json.loads(res.stdout.strip())
    assert payload["ok"] is False
    assert payload["status"] == "not_yet_implemented"
    assert isinstance(payload["lands_in_phase"], int)
    assert payload["lands_in_phase"] >= 14
