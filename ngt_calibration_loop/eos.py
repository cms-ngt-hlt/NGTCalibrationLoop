"""EOS file discovery via edmFileUtil / xrdfs.

Ported from NGTLoopStep2.py's edmFileUtilCommand/GetListOfAvailableFiles/
LSavailable methods as stateless functions operating on an explicit path,
rather than instance attributes.
"""

import logging
import re
import subprocess

EOS_PREFIX = "root://eoscms.cern.ch/"


def edm_file_util(filename):
    """Run edmFileUtil on a single EOS file and return the completed subprocess result."""
    cmd = ["edmFileUtil", EOS_PREFIX + filename, "--eventsInLumi"]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)


def list_available_files(path_where_files_appear):
    """List all valid ROOT files at path_where_files_appear on EOS, skipping unavailable ones."""
    logging.info(f"list_available_files: using path {path_where_files_appear}")

    if not path_where_files_appear:
        logging.critical("path_where_files_appear is not set. Cannot list files.")
        return []

    cmd = f"xrdfs {EOS_PREFIX} ls {path_where_files_appear}"
    logging.info(f"Running command: {cmd}")

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)

    logging.info(f"Command stdout:\n{result.stdout}")
    logging.info(f"Command stderr:\n{result.stderr}")

    if result.returncode != 0:
        logging.warning(
            "xrdfs command failed with return code %s\nOutput:\n%s\nError:\n%s",
            result.returncode, result.stdout, result.stderr,
        )

    all_files = result.stdout.strip().splitlines()

    final_list = []
    for file in all_files:
        output = edm_file_util(file)
        if "ERR" in output.stdout:
            logging.warning(f"Following file won't be processed(skipping): {file}")
        else:
            final_list.append(file)
    return final_list


def ls_available(path_where_files_appear):
    """Return the highest lumisection number found across all currently available files."""
    available_files = list_available_files(path_where_files_appear)
    ls_numbers = set()
    for file_path in available_files:
        result = edm_file_util(file_path)
        for match in re.finditer(r"^\s*\d+\s+(\d+)\s+", result.stdout, re.MULTILINE):
            ls_numbers.add(int(match.group(1)))
    return max(ls_numbers, default=0)
