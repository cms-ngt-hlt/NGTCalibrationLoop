"""OMS REST API queries used to latch onto and monitor LHC runs.

Ported from NGTLoopStep2.py's NewRunAvailable/DAQIsRunning/LastLSRunNumber
methods as stateless functions. Uses the same `omsapi` import as before, so
tests still substitute tests/stubs/omsapi and production still uses the real
oms-api-client copied in next to these scripts (see README.md).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from omsapi import OMSAPI

OMS_BASE_URL = "https://cmsoms.cms/agg/api"


@dataclass
class LatchedRun:
    run_number: int
    run_start_time: datetime
    last_ls: Optional[int]
    current_run_str: str
    path_where_files_appear: str


def last_ls_run_number(run_number):
    """Query OMS for the last lumisection number of a given run.

    Returns 0 if the OMS query fails.
    """
    omsapi = OMSAPI(OMS_BASE_URL, "v1", cert_verify=False)
    q = omsapi.query("runs")
    q.filter("run_number", run_number)
    try:
        response = q.data().json()
    except Exception as e:
        logging.warning(f"OMS query for run {run_number} failed: {e}. Returning LS 0.")
        return 0
    if not response.get("data"):
        logging.warning(f"OMS returned no data for run {run_number}. Returning LS 0.")
        return 0
    run_info = response["data"][0]["attributes"]
    last_ls = run_info.get("last_lumisection_number")

    if last_ls is None:
        logging.warning(
            f"OMS returned null for last_lumisection_numer in run {run_number}. Returning LS = 0."
        )
        return 0

    return int(last_ls)


def daq_is_running(run_number):
    """Query OMS to determine whether the given run is still active.

    Returns a (is_running, last_ls) tuple. is_running is False (rather than
    raising) if the run can't be found or OMS is unreachable.
    """
    logging.info("Checking our run status via OMS...")
    omsapi = OMSAPI(OMS_BASE_URL, "v1", cert_verify=False)

    logging.info(f"Checking status of *our* latched run: {run_number}")

    try:
        q_our_run = omsapi.query("runs")
        q_our_run.filter("run_number", run_number)
        response_our_run = q_our_run.data().json()
    except Exception as e:
        logging.error(f"Error querying OMS API: {e}")
        return False, None

    if "data" not in response_our_run or not response_our_run["data"]:
        logging.warning(f"Could not find info for *our* run {run_number}. Assuming it ended.")
        return False, None

    our_run_info = response_our_run["data"][0]["attributes"]
    last_ls = our_run_info.get("last_lumisection_number")
    is_running = our_run_info.get("end_time") is None

    logging.info(f"Our run {run_number}: Last LS is {last_ls}. Running: {is_running}")

    return is_running, last_ls


def find_new_run(calib_config, data_base_path, calibration_name, max_latch_time_hours, min_ls_to_process):
    """Search OMS for a new PROTONS/collisions run to latch onto.

    Iterates over the 50 most recent runs matching the configured fill type and
    L1/HLT mode, selecting the earliest eligible run that is either currently
    active or recently ended with enough lumisections and no existing working
    directory. Returns a LatchedRun, or None if nothing eligible was found.
    """
    logging.info("Looking for the most recent PROTONS run...")
    omsapi = OMSAPI(OMS_BASE_URL, "v1", cert_verify=False)
    q = omsapi.query("runs")

    oms_args = calib_config["step_2_config"]
    fill_type = oms_args["filltype"]
    l1_hlt_mode = oms_args["l1hltmode"]

    q.filter("fill_type_runtime", fill_type)
    q.filter("l1_hlt_mode", l1_hlt_mode)
    q.filter("stable_beam", True)

    q.sort("run_number", asc=False).paginate(page=1, per_page=50)
    query_response = q.data()

    try:
        query_response.raise_for_status()
        response = query_response.json()
    except requests.exceptions.JSONDecodeError as e:
        logging.error(f"Failed to fetch JSON from OMS API: {e}")
        logging.error(f"Status Code: {query_response.status_code}")
        logging.error(f"Query response text: {query_response.text}")
        return None

    if "data" not in response or not response["data"]:
        logging.info("No PROTONS *collisions* runs found in OMS. Waiting.")
        return None

    now_utc = datetime.now(timezone.utc)
    found = None
    for candidate_run in reversed(response["data"]):
        run_info = candidate_run["attributes"]
        run_number = run_info.get("run_number")
        is_running = run_info.get("end_time") is None
        last_ls = run_info.get("last_lumisection_number")
        run_start_time = datetime.fromisoformat(run_info.get("start_time").replace("Z", "+00:00"))
        delta = now_utc - run_start_time
        is_recent_run = int(delta.total_seconds()) < int(max_latch_time_hours * 60 * 60)
        run_dir = Path(data_base_path) / calibration_name / f"run{run_number}"
        run_dir_missing = not run_dir.exists()

        if is_running and is_recent_run and run_dir_missing:
            logging.info(f"Found running run {run_number}, and no runDir for it {run_dir}")
            found = (run_number, run_start_time, last_ls, is_running)
            break

        if last_ls is None:
            continue

        run_ready = not is_running and last_ls >= min_ls_to_process
        run_to_be_processed = is_recent_run and run_dir_missing
        if run_ready and run_to_be_processed:
            logging.info(f"Found ended run {run_number}, and no runDir for it {run_dir}")
            found = (run_number, run_start_time, last_ls, is_running)
            break

    if found is None:
        logging.info("No new run available: no running recent run with missing runDir found.")
        return None

    run_number, run_start_time, last_ls, is_running = found

    if not is_running and (last_ls is None or last_ls < min_ls_to_process):
        logging.warning(f"Found ended run {run_number}, but it's too short ({last_ls} LS). Skipping.")
        return None

    run_str = str(run_number)
    current_run_str = f"{run_str[:3]}/{run_str[3:]}" if len(run_str) == 6 else run_str

    file_in_path = calib_config.get("file_in_path")
    path_where_files_appear = file_in_path + current_run_str + "/00000"

    logging.info(f"LATCHED run: {current_run_str}, last LS: {last_ls}")

    return LatchedRun(
        run_number=run_number,
        run_start_time=run_start_time,
        last_ls=last_ls,
        current_run_str=current_run_str,
        path_where_files_appear=path_where_files_appear,
    )
