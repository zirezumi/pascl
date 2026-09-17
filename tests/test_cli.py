from pathlib import Path

import pytest

from pascl import __version__
from pascl.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_version_flag_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"pascl {__version__}"


def test_bare_invocation_prints_help_and_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: pascl" in capsys.readouterr().out


def test_binding_ids_lists_the_expansion_and_checks_a_live_list(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    args = [
        "binding-ids",
        "--model",
        str(ROOT / "examples" / "demo_home.yaml"),
        "--binding",
        str(ROOT / "examples" / "demo_binding.yaml"),
    ]
    assert main(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert "input_number.den_pendant_1_brightness_intent_cache" in out
    assert "z2m-1/den_bulbs/set" in out
    assert "timer.den_vacancy" not in out
    # a sensor address may contain a slash (`den_plate/illuminance`); only topics end in /set
    entities = [ln for ln in out if not ln.endswith("/set")]
    assert "den_plate/illuminance" in entities

    live = tmp_path / "live.txt"
    live.write_text("\n".join(e for e in entities if e != "input_boolean.eeptime") + "\n")
    assert main([*args, "--live", str(live), "--no-topics"]) == 1
    captured = capsys.readouterr()
    assert "not live: input_boolean.eeptime -> sleep" in captured.err
    assert "z2m-1/den_bulbs/set" not in captured.out.splitlines()

    live.write_text("\n".join(entities) + "\n")
    assert main([*args, "--live", str(live)]) == 0
    assert "0 not on the host" in capsys.readouterr().err
