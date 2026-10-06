"""Loading of ngtParameters.jsn and per-calibration YAML config.

Moved verbatim (same env var overrides, same defaults) from the three
duplicated copies that used to live in NGTLoopStep2/3/4.py, so both the
library functions and the Airflow DAGs share one implementation.
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
