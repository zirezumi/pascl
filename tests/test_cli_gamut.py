"""``pascl gamut`` and ``pascl palette check`` from the command line: the calibration state,
the seeds, a measurement and the loop against a simulated host, and the palette report."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from pascl.cli import main
from test_gamut_runtime import DARK_EMPTY, SimulatedHome

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo_home.yaml"
BINDING = ROOT / "examples" / "demo_binding.yaml"
TRIANGLE = [[0.153185, 0.047547], [0.691493, 0.308293], [0.169986, 0.699992]]
HULL = tuple(tuple(v) for v in TRIANGLE)


def test_plan_lists_the_far_points(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gamut", "plan"]) == 0
    out = capsys.readouterr().out
    assert "18 far points" in out
    assert out.startswith(" 1  far  0.95 0.04")


def test_status_and_pick_on_an_unmeasured_home(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gamut", "status", "--model", str(DEMO)]) == 1
    captured = capsys.readouterr()
    assert captured.out.count("unmeasured") == 3
    assert "3 unmeasured" in captured.err
    assert main(["gamut", "pick", "--model", str(DEMO)]) == 0
    assert capsys.readouterr().out.strip() == "unmeasured den den_pendant_1"


def test_status_reads_measured_and_rebound(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    raw = yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    fixtures = raw["home_model"]["rooms"]["den"]["fixtures"]
    for fid in ("den_pendant_1", "den_pendant_2", "den_strip"):
        fixtures[fid]["gamut"] = {"vertices": TRIANGLE, "bound_to": f"dev-{fid}"}
    model = tmp_path / "home.yaml"
    model.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert main(["gamut", "status", "--model", str(model)]) == 0
    assert "3 measured, 0 inherited, 0 unmeasured, 0 rebound" in capsys.readouterr().err
    ids = tmp_path / "ids.tsv"
    # the ids file may carry more columns (model id, firmware), as `pascl gamut ids` prints
    ids.write_text(
        "den_strip\tdev-replaced\tLST002\t1.116.3\nden_pendant_1\tdev-den_pendant_1\n",
        encoding="utf-8",
    )
    assert main(["gamut", "status", "--model", str(model), "--device-ids", str(ids)]) == 1
    assert "rebound" in capsys.readouterr().out
    lit = tmp_path / "lit.txt"
    lit.write_text("den_strip\n", encoding="utf-8")
    assert (
        main(["gamut", "pick", "--model", str(model), "--device-ids", str(ids), "--lit", str(lit)])
        == 1
    )
    assert "nothing to measure now" in capsys.readouterr().err
    assert main(["gamut", "pick", "--model", str(model), "--device-ids", str(ids)]) == 0
    assert capsys.readouterr().out.strip() == "rebound den den_strip"


def test_inherit_seeds_same_label_fixtures(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    raw = yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    fixtures = raw["home_model"]["rooms"]["den"]["fixtures"]
    fixtures["den_pendant_1"]["gamut"] = {"vertices": TRIANGLE, "bound_to": "dev-1"}
    model = tmp_path / "home.yaml"
    model.write_text(yaml.safe_dump(raw), encoding="utf-8")
    out = tmp_path / "seeded.yaml"
    assert main(["gamut", "inherit", "--model", str(model), "--write", str(out)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("seed     den                  den_pendant_2")
    assert "from den_pendant_1" in captured.out and "1 seed(s)" in captured.err
    seeded = yaml.safe_load(out.read_text(encoding="utf-8"))
    g = seeded["home_model"]["rooms"]["den"]["fixtures"]["den_pendant_2"]["gamut"]
    assert g["inherited_from"] == "den_pendant_1" and g["vertices"] == TRIANGLE
    assert main(["gamut", "status", "--model", str(out)]) == 1
    captured = capsys.readouterr()
    assert "inherited  den                  den_pendant_2" in captured.out
    assert "1 measured, 1 inherited, 1 unmeasured, 0 rebound" in captured.err
    # nothing more to seed: no write
    out2 = tmp_path / "again.yaml"
    assert main(["gamut", "inherit", "--model", str(out), "--write", str(out2)]) == 0
    assert not out2.exists()


def test_palette_check_reports_unreachable_entries(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert main(["palette", "check", "--model", str(DEMO)]) == 0
    assert "0 unreachable" in capsys.readouterr().err
    raw = yaml.safe_load(DEMO.read_text(encoding="utf-8"))
    fixtures = raw["home_model"]["rooms"]["den"]["fixtures"]
    fixtures["den_pendant_1"]["gamut"] = {"vertices": TRIANGLE}
    fixtures["den_pendant_2"]["gamut"] = {"vertices": TRIANGLE}
    model = tmp_path / "home.yaml"
    model.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert main(["palette", "check", "--model", str(model)]) == 1
    captured = capsys.readouterr()
    assert "dusk" in captured.out and "bulbs[0]" in captured.out
    assert "2 fixture(s): den_pendant_1, den_pendant_2" in captured.out
    assert "unreachable palette colour(s) across 2 fixture(s)" in captured.err


class FastClock:
    """A system clock stand-in that moves a tenth of a second per look, so the timing rules
    play out without waiting."""

    def __init__(self) -> None:
        self._t = 0.0

    def now(self) -> datetime:
        return datetime(2026, 9, 17, 14, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        self._t += 0.1
        return self._t


@pytest.fixture
def simulated(monkeypatch: pytest.MonkeyPatch) -> SimulatedHome:
    """Route the CLI's Home Assistant link to a simulated host with two Hue-class pendants,
    on a clock that does not wait."""
    from pascl.shell import gamut_measure, gamut_runtime, ha_mqtt

    home = SimulatedHome({"den_pendant_1": HULL, "den_pendant_2": HULL}, DARK_EMPTY)
    monkeypatch.setattr(ha_mqtt, "HAMqttLink", lambda url, token: home)
    monkeypatch.setattr(gamut_measure, "SystemClock", FastClock)
    monkeypatch.setattr(gamut_runtime, "SystemClock", FastClock)
    monkeypatch.setenv("HA_URL", "http://ha.test:8123")
    monkeypatch.setenv("HA_TOKEN", "t")
    return home


def test_ids_lists_devices_from_the_coordinator(
    capsys: pytest.CaptureFixture[str], simulated: SimulatedHome
) -> None:
    code = main(["gamut", "ids", "--model", str(DEMO), "--binding", str(BINDING)])
    captured = capsys.readouterr()
    assert code == 1  # the strip is not on the simulated coordinator
    assert "den_pendant_1\t0x0000000000000001\tLCA001\t1.116.3" in captured.out
    assert "den_strip is not listed" in captured.err and "2 devices, 1 not listed" in captured.err


def test_measure_binds_confirms_and_writes(
    capsys: pytest.CaptureFixture[str], simulated: SimulatedHome, tmp_path: Path
) -> None:
    out = tmp_path / "home.yaml"
    code = main(
        [
            "gamut",
            "measure",
            "--model",
            str(DEMO),
            "--binding",
            str(BINDING),
            "--fixture",
            "den_pendant_1",
            "--write",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "device 0x0000000000000001 firmware 1.116.3" in captured.err
    assert "clip_rule closest" in captured.out and "fits [closest=" in captured.out
    g = yaml.safe_load(out.read_text(encoding="utf-8"))["home_model"]["rooms"]["den"]["fixtures"][
        "den_pendant_1"
    ]["gamut"]
    assert g["bound_to"] == "0x0000000000000001" and g["firmware"] == "1.116.3"
    assert g["clip_rule"] == "closest" and g["inherited_from"] is None
    # seed the other pendant from it, then measure that one: the seed is confirmed
    seeded = tmp_path / "seeded.yaml"
    assert main(["gamut", "inherit", "--model", str(out), "--write", str(seeded)]) == 0
    capsys.readouterr()
    code = main(
        [
            "gamut",
            "measure",
            "--model",
            str(seeded),
            "--binding",
            str(BINDING),
            "--fixture",
            "den_pendant_2",
            "--write",
            str(seeded),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0 and "seed from den_pendant_1 confirmed" in captured.err
    g2 = yaml.safe_load(seeded.read_text(encoding="utf-8"))["home_model"]["rooms"]["den"][
        "fixtures"
    ]["den_pendant_2"]["gamut"]
    assert g2["inherited_from"] is None and g2["bound_to"] == "0x0000000000000002"


def test_auto_once_measures_and_seeds(
    capsys: pytest.CaptureFixture[str], simulated: SimulatedHome, tmp_path: Path
) -> None:
    out = tmp_path / "home.yaml"
    code = main(
        [
            "gamut",
            "auto",
            "--model",
            str(DEMO),
            "--binding",
            str(BINDING),
            "--write",
            str(out),
            "--once",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "unmeasured den den_pendant_1: polygon" in captured.err
    assert "written; 1 seed(s); 2 left" in captured.err
    fixtures = yaml.safe_load(out.read_text(encoding="utf-8"))["home_model"]["rooms"]["den"][
        "fixtures"
    ]
    assert fixtures["den_pendant_2"]["gamut"]["inherited_from"] == "den_pendant_1"
