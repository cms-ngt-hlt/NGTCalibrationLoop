"""ngtParameters.jsn's CMSSW_AREA (config.release_area): every step's jobs cmsenv in its one release area, instead of
each Step 2 job making one in its run's directory, which real scram refuses the second time and two jobs race over."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import omsapi

from ngt_calibration_loop import step2, step3, step4

CALIBRATION = "EcalPedestals"
RUN = 398600
RELEASE = "CMSSW_16_0_7_patch1"


def _job_scripts(fake_eos):
    """Each step's job script for one file of a live run."""
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    omsapi.configure_runs([{
        "run_number": RUN, "fill_type_runtime": "PROTONS", "l1_hlt_mode": "collisions2026", "stable_beam": True,
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end_time": None, "last_lumisection_number": 100,
    }])
    assert step2.find_new_run(CALIBRATION) == RUN
    ctx2 = step2.build_run_context(CALIBRATION, RUN)
    raw = f"{ctx2.path_where_files_appear}/run{RUN}_ls0051.root"
    fake_eos.add_file(raw, run_number=RUN, ls_numbers=[51])
    job2 = step2.prepare_express_job(ctx2, {Path(raw)})

    step2_output = ctx2.working_dir / job2.output_file
    step2_output.write_bytes(b"(FAKE) RECO output\n")
    job3 = step3.prepare_alca_prompt_job(step3.build_run_context(CALIBRATION, str(RUN)), {step2_output})

    alca = job3.job_dir / "PromptCalibProdEcalPedestals.root"
    alca.write_bytes(b"(FAKE) ALCARECO output\n")
    job4 = step4.prepare_harvesting_job(step4.build_run_context(CALIBRATION, str(RUN)), {alca})

    scripts = [ctx2.working_dir / job2.script_name, job3.job_dir / "ALCAOUTPUT.sh", job4.job_dir / "HARVESTING.sh"]
    return [path.read_text(encoding="utf-8") for path in scripts], ctx2.working_dir


def test_every_step_cmsenvs_in_the_one_release_area_it_names(isolated_env, fake_eos):
    area = isolated_env.tmp_path / "cmssw"
    params = json.loads(isolated_env.parameters_path.read_text(encoding="utf-8"))
    isolated_env.parameters_path.write_text(json.dumps(dict(params, CMSSW_AREA=str(area))), encoding="utf-8")

    scripts, _ = _job_scripts(fake_eos)

    for script in scripts:
        assert f"cd {area}/{RELEASE}/src || exit 1\ncmsenv\n" in script
        assert "cmsrel" not in script


def test_without_one_step2_makes_an_area_per_run_as_before(isolated_env, fake_eos):
    (step2_script, step3_script, step4_script), working_dir = _job_scripts(fake_eos)

    assert f"cmsrel {RELEASE}\ncd {RELEASE}/src || exit 1\ncmsenv\n" in step2_script
    assert f"cd {working_dir}/{RELEASE}/src || exit 1\n" in step3_script
    assert f"cd /nfshome0/sakura//{RELEASE}/src || exit 1\n" in step4_script  # the calibration's cmssw_base_path
