from __future__ import annotations

import json

from pybamm_w10.output import append_json_line, build_output_manifest


def test_solver_attempt_audit_is_transaction_protected(workspace_tmp) -> None:
    path = workspace_tmp / "solver_attempts.jsonl"
    append_json_line(path, {
        "audit_version": "solver-attempt-v1",
        "cycle": 1,
        "attempt_count": 2,
        "solver_profile": "conservative_cv_transition",
        "initial_failure_code": "IDA_ERR_FAIL",
        "final_status": "COMPLETED",
    })

    manifest = build_output_manifest(workspace_tmp, 1, 1, None)

    assert "solver_attempts.jsonl" in manifest.append_files
    assert manifest.append_files["solver_attempts.jsonl"].data_rows == 1
    assert json.loads(path.read_text(encoding="utf-8"))["attempt_count"] == 2
