"""
Test doubles for the external CLI tools the calibration loop shells out to
(edmFileUtil, xrdfs) so ngt_calibration_loop's processing logic can be
exercised without EOS access, CMSSW, or any real subprocess execution.
Job-script launches (cmsDriver.py-generated bash scripts) are mocked
separately -- see conftest.py's `job_runner` fixture, which replaces
ngt_calibration_loop.shell.run_job_script directly.
"""

import subprocess


class FakeEOS:
    """In-memory stand-in for the EOS files edmFileUtil/xrdfs would report on.

    Each file is keyed by its EOS path (as it would appear in an `xrdfs ls` listing,
    i.e. without the "root://eoscms.cern.ch/" prefix) and maps to a list of
    (run_number, ls_number, events_in_lumi) rows, mimicking `edmFileUtil --eventsInLumi`
    table output. A file can instead be marked broken, which makes edmFileUtil report
    "ERR" the way it does for a corrupted/inaccessible file.
    """

    def __init__(self):
        self._files = {}  # path -> list[(run, ls, events)] | "ERR"

    def add_file(self, path, run_number, ls_numbers, events_in_lumi=1000):
        self._files[path] = [(run_number, ls, events_in_lumi) for ls in ls_numbers]
        return path

    def add_broken_file(self, path):
        self._files[path] = "ERR"
        return path

    def list_dir(self, directory):
        directory = directory.rstrip("/")
        return sorted(p for p in self._files if p.rsplit("/", 1)[0] == directory)

    def edm_file_util_output(self, path):
        rows = self._files.get(path)
        if rows is None:
            return f"Error: file {path} does not exist\nERR\n"
        if rows == "ERR":
            return f"Error opening file {path}\nERR\n"
        # Column widths mirror real edmFileUtil --eventsInLumi output closely enough
        # for the run/LS-extracting regexes in NGTLoopStep2.py to match.
        lines = [f"{run:>15}{ls:>13}{events:>13}" for run, ls, events in rows]
        return "\n".join(lines) + "\n"


def make_fake_run(fake_eos, prefix="root://eoscms.cern.ch/"):
    """Build a fake `subprocess.run` answering edmFileUtil and `xrdfs ls` calls from
    the given FakeEOS. Raises on any other command, so unmocked calls fail loudly
    instead of silently trying to hit the network."""

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, str) and cmd.startswith("xrdfs"):
            # Real command shape: f"xrdfs {prefix} ls {directory}"
            _xrdfs, _prefix, _ls, directory = cmd.split(maxsplit=3)
            files = fake_eos.list_dir(directory)
            stdout = "\n".join(files) + ("\n" if files else "")
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        if isinstance(cmd, list) and cmd and cmd[0] == "edmFileUtil":
            url = cmd[1]
            path = url[len(prefix):] if url.startswith(prefix) else url
            stdout = fake_eos.edm_file_util_output(path)
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        raise AssertionError(f"Unexpected subprocess.run call in test: {cmd!r}")

    return fake_run
