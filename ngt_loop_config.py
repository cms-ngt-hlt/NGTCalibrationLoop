#!/usr/bin/env python
# coding: utf-8
"""
Config and logging setup shared by NGTLoopStep2/3/4.py -- loading
ngtParameters.jsn and a calibration workflow YAML, and wiring up each step's
split-by-level log files.
"""

import json
import logging
import os
import sys
from pathlib import Path

import yaml


def _load_ngt_parameters():
    """Load ngtParameters.jsn, honoring the NGT_PARAMETERS_PATH override used by tests."""
    parameters_path = os.environ.get(
        "NGT_PARAMETERS_PATH", os.path.join(os.getcwd(), "ngtParameters.jsn")
    )
    with open(parameters_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_calibration_config(calibration_name):
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


def _setup_logging(step_name, calibration_name):
    """Set up the split-by-level log files for a run of the given step.

    Honors the LOG_BASE_PATH override in ngtParameters.jsn so tests can
    redirect logging away from /tmp/ngt.
    """
    config = _load_ngt_parameters()
    log_base_path = config.get("LOG_BASE_PATH", "/tmp/ngt")

    # Ensure the log directory exists.
    log_dir = Path(log_base_path) / calibration_name
    log_dir.mkdir(parents=True, exist_ok=True)

    # Get the main logger
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # Capture everything at logger level

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. ALL MESSAGES - Complete history
    all_handler = logging.FileHandler(log_dir / f"{step_name}_ALL.log")
    all_handler.setLevel(logging.DEBUG)
    all_handler.setFormatter(formatter)
    logger.addHandler(all_handler)

    # 2. INFO ONLY
    info_handler = logging.FileHandler(log_dir / f"{step_name}_INFO.log")
    info_handler.setLevel(logging.INFO)
    info_handler.addFilter(lambda record: record.levelno == logging.INFO)  # ONLY info
    info_handler.setFormatter(formatter)
    logger.addHandler(info_handler)

    # 3. WARNING ONLY
    warning_handler = logging.FileHandler(log_dir / f"{step_name}_WARNING.log")
    warning_handler.setLevel(logging.WARNING)
    warning_handler.addFilter(
        lambda record: record.levelno == logging.WARNING
    )  # ONLY warnings
    warning_handler.setFormatter(formatter)
    logger.addHandler(warning_handler)

    # 4. ERROR ONLY
    error_handler = logging.FileHandler(log_dir / f"{step_name}_ERROR.log")
    error_handler.setLevel(logging.ERROR)
    error_handler.addFilter(lambda record: record.levelno == logging.ERROR)  # ONLY errors
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    # 5. CRITICAL ONLY
    critical_handler = logging.FileHandler(log_dir / f"{step_name}_CRITICAL.log")
    critical_handler.setLevel(logging.CRITICAL)
    critical_handler.addFilter(
        lambda record: record.levelno == logging.CRITICAL
    )  # ONLY critical
    critical_handler.setFormatter(formatter)
    logger.addHandler(critical_handler)

    # 6. Screen output (stderr) - warnings and above
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    # Optional: Add a simple startup message to verify logging is working
    logging.info("Logging initialized - writing to split log files")
    logging.warning("Warning-level logging active")
