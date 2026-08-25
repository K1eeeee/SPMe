from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is required")
@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("3c_cc", "Step 1 - 3C CC charge to 4.0 V"),
        ("step6_udds", "Step 6 - UDDS"),
    ],
)
def test_terminal_monitor_reports_exact_step_once(
    workspace_tmp: Path,
    stage: str,
    expected: str,
) -> None:
    run_dir = workspace_tmp / f"monitor-{stage}"
    run_dir.mkdir()
    (run_dir / "failures").mkdir()
    (run_dir / "run_config.json").write_text(
        json.dumps({"protocol": {"max_aging_cycles": 350}}),
        encoding="utf-8",
    )
    (run_dir / "run_progress.json").write_text(
        json.dumps(
            {
                "phase": "STEP6_UDDS" if stage == "step6_udds" else "STANDARD_CHARGE",
                "stage": stage,
                "completed_cycles": 4,
                "current_cycle": 5,
                "business_status": "RUNNING",
                "updated_at_utc": "2026-08-18T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    script = Path(__file__).parents[1] / "scripts" / "watch_pybamm_w10.ps1"

    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-OutputDir",
            str(run_dir),
            "-ProcessId",
            "2147483646",
            "-Once",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "cycle=5/350 completed=4" in completed.stdout
    assert expected in completed.stdout
    assert "process=STOPPED pid=2147483646" in completed.stdout
    assert "failure_files=0" in completed.stdout
