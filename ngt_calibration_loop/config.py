"""Loading of ngtParameters.jsn and per-calibration YAML config.

The same loaders, with the same env var overrides and defaults, as the
_load_ngt_parameters/_load_calibration_config that NGTLoopStep2/3/4.py share
from ngt_loop_config.py -- a copy rather than an import, so the library
doesn't depend on the FSM scripts. The library functions and the Airflow
DAGs share this one.
"""

import json
import os

import yaml


def load_ngt_parameters():
    """Load ngtParameters.jsn, honoring the NGT_PARAMETERS_PATH override used by tests."""
    parameters_path = os.environ.get(
        "NGT_PARAMETERS_PATH", os.path.join(os.getcwd(), "ngtParameters.jsn")
    )
    with open(parameters_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_calibration_config(calibration_name):
    """Load a calibration workflow YAML, honoring the NGT_CALIBRATION_YAML_DIR
    override used by tests."""
    calibration_yaml_dir = os.environ.get(
        "NGT_CALIBRATION_YAML_DIR", os.path.join(os.getcwd(), "calibrationYAML")
    )
    calibration_config_path = os.path.join(
        calibration_yaml_dir, f"{calibration_name}.yaml"
    )
    with open(calibration_config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
