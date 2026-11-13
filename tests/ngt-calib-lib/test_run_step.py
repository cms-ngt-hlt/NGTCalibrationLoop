"""Tests for ngt_calibration_loop.run_step: one step of the per-file pipeline as one call (prepare, run the job
script, finalize), the result each step leaves in the run's working directory for the next, "nothing to do", and the
command line that runs a step outside Airflow. Job launches are recorded by the job_runner fixture and EOS is
fake_eos (see conftest.py), so nothing runs cmsRun.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import omsapi
import pytest

from ngt_calibration_loop import run_step, step2

CALIBRATION = "EcalPedestals"
RUN = 398601


def _seed_run():
    start_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs(
        [
            {
                "run_number": RUN,
                "fill_type_runtime": "PROTONS",
                "l1_hlt_mode": "collisions2026",
                "stable_beam": True,
                "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_time": None,
                "last_lumisection_number": 100,
            }
        ]
    )
    assert step2.find_new_run(CALIBRATION) == RUN
    return step2.build_run_context(CALIBRATION, RUN)


def _input_file(ctx, fake_eos):
    path = f"{ctx.path_where_files_appear}/run{RUN}_ls0051.root"
    fake_eos.add_file(path, run_number=RUN, ls_numbers=[51])
    return path


def _alca_output(job_dir):
    """A finished Step 3 job, as Step 4's scan finds it: its witness file and its ALCARECO output."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "ecalPedsStep3_job.txt").touch()
    output = job_dir / "PromptCalibProdEcalPedestals.root"
    output.write_bytes(b"(FAKE) ALCARECO output\n")
    return output


def test_one_file_through_every_step_each_taking_the_previous_ones_result(isolated_env, fake_eos, job_runner):
    ctx = _seed_run()
    file_path = _input_file(ctx, fake_eos)

    result2 = run_step.step2_express(CALIBRATION, RUN, file_path)
    assert run_step.read_result(ctx.working_dir, "step2_express", file_path) == result2
    Path(result2["step2_output"]).write_bytes(b"(FAKE) RECO output\n")  # what the job itself would write

    result3 = run_step.step3_alca(CALIBRATION, RUN, file_path)
    alca_output = _alca_output(result3["step3_job_dir"])

    result4 = run_step.step4_harvest(CALIBRATION, RUN, file_path)
    assert result4 == {"harvested_files": [str(alca_output)]}
    run_step.step4_upload(CALIBRATION, RUN, file_path)

    scripts = [call["script_name"] for call in job_runner]
    assert scripts[0].startswith("cmsDriver_") and scripts[1:] == ["ALCAOUTPUT.sh", "HARVESTING.sh", "UPLOAD.sh"]
    assert job_runner[1]["cwd"] == result3["step3_job_dir"]
    assert (ctx.working_dir / step2.PROCESSED_LOG_NAME).exists()


def test_step2_has_nothing_to_do_for_a_file_already_processed(isolated_env, fake_eos, job_runner):
    ctx = _seed_run()
    file_path = _input_file(ctx, fake_eos)
    (ctx.working_dir / step2.PROCESSED_LOG_NAME).write_text(str(Path(file_path)) + "\n", encoding="utf-8")

    with pytest.raises(run_step.NothingToDo, match="already processed"):
        run_step.step2_express(CALIBRATION, RUN, file_path)
    assert len(job_runner) == 0


def test_step3_has_nothing_to_do_without_a_step2_result_for_its_file(isolated_env, job_runner):
    _seed_run()
    with pytest.raises(run_step.NothingToDo, match="Step 2 recorded no output"):
        run_step.step3_alca(CALIBRATION, RUN, "/eos/never/processed.root")
    assert len(job_runner) == 0


def test_step4_upload_uploads_what_its_harvest_harvested_not_a_recomputed_set(isolated_env, job_runner):
    """Found live: the job directory is a hash of the harvested set, which grows as Step 3 delivers more ALCARECO
    files. An upload that recomputed it after another file arrived uploaded from a directory whose harvest hadn't
    run."""
    ctx = _seed_run()
    _alca_output(ctx.working_dir / "alcaPromptJob_aaaaaaaaaa")
    run_step.step4_harvest(CALIBRATION, RUN, "file-a")
    harvest_dir = job_runner[-1]["cwd"]

    _alca_output(ctx.working_dir / "alcaPromptJob_bbbbbbbbbb")  # Step 3 delivers another one before the upload
    run_step.step4_upload(CALIBRATION, RUN, "file-a")

    assert job_runner[-1]["script_name"] == "UPLOAD.sh"
    assert job_runner[-1]["cwd"] == harvest_dir


def test_a_harvest_with_nothing_new_leaves_its_upload_nothing_to_do(isolated_env, job_runner):
    """A step clears its earlier result before it starts, so a later step never reads one this pass didn't
    produce: here, re-uploading an old payload."""
    ctx = _seed_run()
    _alca_output(ctx.working_dir / "alcaPromptJob_aaaaaaaaaa")
    run_step.step4_harvest(CALIBRATION, RUN, "file-a")
    run_step.step4_upload(CALIBRATION, RUN, "file-a")
    launched = len(job_runner)

    with pytest.raises(run_step.NothingToDo, match="Nothing new"):
        run_step.step4_harvest(CALIBRATION, RUN, "file-a")
    with pytest.raises(run_step.NothingToDo, match="Nothing harvested"):
        run_step.step4_upload(CALIBRATION, RUN, "file-a")
    assert len(job_runner) == launched


def test_the_command_line_prints_the_result_or_exits_99_with_nothing_to_do(isolated_env, fake_eos, job_runner, capsys):
    ctx = _seed_run()
    file_path = _input_file(ctx, fake_eos)
    argv = ["step2_express", "--calibration", CALIBRATION, "--run", str(RUN), "--file", file_path]

    assert run_step.main(argv) == 0
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(printed) == run_step.read_result(ctx.working_dir, "step2_express", file_path)

    assert run_step.main(argv) == run_step.EXIT_NOTHING_TO_DO  # it's processed now
    assert "already processed" in capsys.readouterr().err
