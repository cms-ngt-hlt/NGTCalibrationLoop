"""Tests for the job_runner fixture's richer queue_failure() (returncode/
match/times) and the inject_fault fixture that applies a YAML-shaped fault
dict to it -- the pytest half of the scenario-player/faults.py FaultSpec
vocabulary shared with scenario-player/scenario_player.py for cmsRun/upload_conditions.py.
"""
import pytest

from ngt_calibration_loop import oms, shell


def test_returncode_builds_a_message_naming_the_code(job_runner):
    job_runner.queue_failure(returncode=139)
    with pytest.raises(shell.JobScriptFailedError, match="139"):
        shell.run_job_script("HARVESTING.sh", "/some/job/dir")


def test_exc_and_returncode_are_mutually_exclusive(job_runner):
    with pytest.raises(ValueError, match="exc OR returncode"):
        job_runner.queue_failure(exc=RuntimeError("x"), returncode=1)


def test_bare_queue_failure_fires_on_the_very_next_call_regardless_of_script(job_runner):
    """Backward-compat lock-in for every existing caller of queue_failure()
    with no arguments."""
    job_runner.queue_failure()
    with pytest.raises(shell.JobScriptFailedError, match="forced test failure"):
        shell.run_job_script("cmsDriver_abc123.sh", "/some/job/dir")

    result = shell.run_job_script("cmsDriver_abc123.sh", "/some/job/dir")  # consumed: next call succeeds
    assert result.returncode == 0


def test_match_substring_only_fires_for_the_named_script(job_runner):
    job_runner.queue_failure(returncode=1, match="UPLOAD.sh")

    result = shell.run_job_script("ALCAOUTPUT.sh", "/job/dir")  # a cmsRun-family call: unaffected
    assert result.returncode == 0

    with pytest.raises(shell.JobScriptFailedError):  # the targeted UPLOAD.sh call: still fails
        shell.run_job_script("UPLOAD.sh", "/job/dir")


def test_match_callable_scopes_by_cwd_too(job_runner):
    job_runner.queue_failure(returncode=1, match=lambda script, cwd: "EcalPedestals" in cwd)

    result = shell.run_job_script("HARVESTING.sh", "/data/SiStripBad/run1")
    assert result.returncode == 0

    with pytest.raises(shell.JobScriptFailedError):
        shell.run_job_script("HARVESTING.sh", "/data/EcalPedestals/run1")


def test_times_self_clears_across_repeated_matching_calls(job_runner):
    job_runner.queue_failure(returncode=1, match="HARVESTING.sh", times=2)

    with pytest.raises(shell.JobScriptFailedError):
        shell.run_job_script("HARVESTING.sh", "/job/dir")
    with pytest.raises(shell.JobScriptFailedError):
        shell.run_job_script("HARVESTING.sh", "/job/dir")

    result = shell.run_job_script("HARVESTING.sh", "/job/dir")  # 3rd: fault exhausted
    assert result.returncode == 0


def test_unlimited_times_never_clears(job_runner):
    job_runner.queue_failure(returncode=1, times=None)
    for _ in range(4):
        with pytest.raises(shell.JobScriptFailedError):
            shell.run_job_script("HARVESTING.sh", "/job/dir")


def test_two_scoped_outcomes_do_not_interfere(job_runner):
    """FIFO-among-matching-entries, mirroring
    scenario-player/faults.py's consume_fault array-order matching."""
    job_runner.queue_failure(returncode=1, match="UPLOAD.sh")
    job_runner.queue_failure(returncode=2, match="ALCAOUTPUT.sh")

    with pytest.raises(shell.JobScriptFailedError, match="2"):
        shell.run_job_script("ALCAOUTPUT.sh", "/job/dir")
    with pytest.raises(shell.JobScriptFailedError, match="1"):
        shell.run_job_script("UPLOAD.sh", "/job/dir")


# --- inject_fault: the YAML-shaped-dict entry point -----------------------------------


def test_inject_fault_cmsrun_scoped_by_calibration_and_step(inject_fault):
    inject_fault(
        {
            "target": "cmsrun",
            "mode": "exit_code",
            "exit_code": 139,
            "calibration": "EcalPedestals",
            "step": "step2",
        }
    )

    result = shell.run_job_script("cmsDriver_abc.sh", "/data/SiStripBad/run398600")  # different calibration
    assert result.returncode == 0

    result = shell.run_job_script(  # same calibration, different step (step3)
        "ALCAOUTPUT.sh", "/data/EcalPedestals/run398600/alcaPromptJob_abc"
    )
    assert result.returncode == 0

    with pytest.raises(shell.JobScriptFailedError, match="139"):  # the exact scoped target
        shell.run_job_script("cmsDriver_abc.sh", "/data/EcalPedestals/run398600")


def test_inject_fault_upload_conditions_scoped_by_calibration(inject_fault):
    inject_fault({"target": "upload_conditions", "mode": "exit_code", "exit_code": 1, "calibration": "BeamSpot"})

    with pytest.raises(shell.JobScriptFailedError):
        shell.run_job_script("UPLOAD.sh", "/data/BeamSpot/run1/harvestJob_abc")

    # cmsRun in the same calibration/run is unaffected -- upload_conditions
    # only ever matches UPLOAD.sh, never a cmsRun-family script.
    result = shell.run_job_script("HARVESTING.sh", "/data/BeamSpot/run1/harvestJob_abc")
    assert result.returncode == 0


def test_inject_fault_run_scoping(inject_fault):
    inject_fault(
        {"target": "cmsrun", "mode": "exit_code", "exit_code": 1, "calibration": "EcalPedestals", "run": 398600}
    )

    result = shell.run_job_script("cmsDriver_x.sh", "/data/EcalPedestals/run500201")  # different run
    assert result.returncode == 0

    with pytest.raises(shell.JobScriptFailedError):
        shell.run_job_script("cmsDriver_x.sh", "/data/EcalPedestals/run398600")


def test_inject_fault_oms_delegates_to_set_failure(inject_fault):
    inject_fault({"target": "oms", "mode": "timeout", "run": 398600})
    is_running, _last_ls = oms.daq_is_running(398600)
    assert is_running is False
