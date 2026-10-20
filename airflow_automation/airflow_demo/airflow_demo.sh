#!/bin/bash
# Self-contained "live" runner for the Airflow-based NGT calibration loop
# against the same fake OMS/EOS/CMSSW toolchain scenario-player/sim_env.sh's `sim_setup`
# provides (scenario-player/bin/, tests/stubs/omsapi, scenario-player/seed.py) -- so the real Airflow
# scheduler drives real DAG runs, real generated job scripts, and real output
# files flowing Step2 -> Step3 -> Step4 to a fake condDB upload, entirely
# offline. Needs a Linux host (Airflow has no Windows support; WSL works fine
# on Windows).
#
# Manages ONE Airflow instance per checkout (own AIRFLOW_HOME, metadata DB, port and tmux
# sessions -- see airflow_env.sh, so several checkouts can run side by side) running
# airflow_automation/airflow_dags/ngt_dags_watch.py (4 DAGs -- all-static-asset,
# AssetWatcher-driven, zero cron; see its module docstring), which the
# seed-run/add-ls/end-run/scenario_player.py-driven fake run feeds.
#
# This file is the Airflow-*specific* engine adapter: everything about
# actually simulating a live run (seed-run/add-ls/end-run/list-runs below)
# just forwards to scenario-player/seed.py, which knows nothing about Airflow -- see
# scenario-player/sim_env.sh's module docstring for why that split exists.
#
# For a guided, pause-and-confirm walkthrough instead of firing everything at
# once, use watch_interactive_demo.sh (ngt_dags_watch.py -- see
# watch_asset_scheduling_design.md). For an unattended, timed sequence of runs/
# lumisections (e.g. for a scripted test run against whatever engine/design
# is currently `start`ed/unpaused), use scenario-player/scenario_player.py -- see
# scenario-player/scenarios/.
#
# Quick start:
#   ./airflow_automation/airflow_demo/airflow_demo.sh setup
#   ./airflow_automation/airflow_demo/airflow_demo.sh start
#   ./airflow_automation/airflow_demo/airflow_demo.sh unpause-watch
#   ./airflow_automation/airflow_demo/airflow_demo.sh seed-run EcalPedestals 398600 --ls 51 52
#   ./airflow_automation/airflow_demo/airflow_demo.sh ui            # prints the webserver URL
#   ./airflow_automation/airflow_demo/airflow_demo.sh add-ls EcalPedestals 398600 53
#   ./airflow_automation/airflow_demo/airflow_demo.sh end-run 398600
#   ./airflow_automation/airflow_demo/airflow_demo.sh status
#   ./airflow_automation/airflow_demo/airflow_demo.sh stop
#
# $NGT_DEV_HOME (default ./demo-env, a gitignored directory inside this repo)
# holds the fake data/EOS/CMSSW scratch state, same default as scenario-player/seed.py
# uses directly -- delete it any time to reset the demo data. The Airflow
# instance itself (metadata DB, AIRFLOW_HOME) is separate/persistent -- `setup` creates and
# migrates this checkout's own database; see airflow_env.sh and README.md's Airflow setup
# section for the one-time Postgres role it needs. Every default is overridable via
# local_airflow_env.sh (see local_airflow_env.sh.example).

set -euo pipefail

# airflow_env.sh sources sim_env.sh itself, and both pick up the gitignored local_*_env.sh
# overrides -- so sourcing it here (not just in activate_venv) makes every per-checkout value
# (ports, tmux prefix, venv path, ...) available to usage and to commands that never activate.
# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/airflow_env.sh"
CALIBRATIONS=(SiStripBad EcalPedestals BeamSpot)

# tmux session names, per checkout (see airflow_env.sh's NGT_AIRFLOW_TMUX_PREFIX).
SESSION_SCHEDULER="${NGT_AIRFLOW_TMUX_PREFIX}Scheduler"
SESSION_DAG_PROCESSOR="${NGT_AIRFLOW_TMUX_PREFIX}DagProcessor"
SESSION_API_SERVER="${NGT_AIRFLOW_TMUX_PREFIX}ApiServer"
SESSION_TRIGGERER="${NGT_AIRFLOW_TMUX_PREFIX}Triggerer"

usage() {
  cat <<USAGE
Usage: $0 <command> [args]

Setup:
  setup                                  Create/refresh \$NGT_DEV_HOME scratch data
                                          (fake EOS/CMSSW/OMS state; safe to re-run)

Running Airflow (this checkout's own instance -- UI on :$NGT_AIRFLOW_PORT, tmux sessions ${NGT_AIRFLOW_TMUX_PREFIX}*):
  start                                   Launch scheduler + dag-processor + API server + triggerer
  stop                                    Kill all four tmux sessions
  status                                  List this checkout's running Airflow tmux sessions
  ui                                      Print the Airflow UI URL
  logs <scheduler|dag-processor|api|triggerer>   Tail -f that process's tmux console log

DAG control:
  unpause-watch                           Unpause ngt_dags_watch.py's 4 DAGs (starts the AssetWatchers)
  pause-watch                             Pause them again (stops the AssetWatchers)

Resetting state (see scenario-player/sim_env.sh's setup/reset for the simulator's OWN state --
\$NGT_DEV_HOME -- which none of these touch, except reset-scenario):
  reset-watch                             Clear DAG-run history for ngt_dags_watch.py's 4 DAGs
                                          (also drops the ngt://runs / ngt://files/* Assets +
                                          their AssetStateStore rows)
  reset-airflow                           Full reset: drop + recreate the whole metadata DB
                                          (DAG-run history, variables, connections --
                                          everything) and re-migrate. Back to freshly-installed.
  reset-scenario                          Wipe + recreate \$NGT_DEV_HOME (scenario-player/sim_env.sh's own
                                          reset+setup), stopping/restarting Airflow around it if
                                          running -- calling sim_env.sh's reset directly while
                                          Airflow's processes are live can leave in-flight
                                          deferred work stuck; this is the safe way to do it from
                                          this adapter. Also clears ngt_dags_watch.py's
                                          AssetStateStore watermarks (seen_runs / emitted_run<N>),
                                          which would otherwise describe fake run/file state this
                                          just wiped -- without touching the Assets themselves or
                                          any DAG-run history. Pair with reset-watch/
                                          -airflow if you want DAG-run history cleared too.

Driving a fake run (edits \$NGT_DEV_HOME/oms_runs.json + drops fake RAW files,
identical to any other engine adapter -- delegates straight to scenario-player/seed.py):
  seed-run <calibration> <run> [--ls N ...] [--minutes-ago M]
  add-ls <calibration> <run> <ls>
  end-run <run>
  list-runs

Injecting failures (writes \$NGT_DEV_HOME/faults/<target>.json, polled by
tests/stubs/omsapi / scenario-player/bin/cmsRun / scenario-player/bin/uploadConditions.py -- see
scenario-player/faults.py and scenario-player/scenarios/fault_injection_demo.yaml
for the equivalent scenario-YAML \`faults:\` syntax):
  arm-fault --target <oms|cmsrun|upload_conditions> --mode <mode> [options...]
            (oms: --mode connection_error|timeout|http_error|malformed_json
                  [--status N] [--run N] [--times N|unlimited]
             cmsrun/upload_conditions: --mode exit_code --exit-code N
                  --calibration <calibration> [--run N] [--step step2|step3|step4]
                  [--times N|unlimited])
  list-faults [--target <oms|cmsrun|upload_conditions>]
  clear-faults [--target <oms|cmsrun|upload_conditions>]

calibration is one of: ${CALIBRATIONS[*]}
USAGE
}

activate_venv() {
  if [ ! -f "$NGT_AIRFLOW_VENV/bin/activate" ]; then
    echo "No Airflow venv at $NGT_AIRFLOW_VENV -- see README.md's Airflow setup section" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$NGT_AIRFLOW_VENV/bin/activate"
}

ensure_airflow_db() {
  # Creates this checkout's metadata database if it doesn't exist yet. Only for the database
  # airflow_env.sh itself derived (NGT_AIRFLOW_DB_*): if AIRFLOW__DATABASE__SQL_ALCHEMY_CONN was
  # pointed somewhere else, that database is the operator's to provision. The `airflow_ngt`
  # role was created with CREATEDB (README.md's Airflow setup section), so no sudo is needed.
  if [ "$AIRFLOW__DATABASE__SQL_ALCHEMY_CONN" != "$NGT_AIRFLOW_DB_CONN" ]; then
    echo "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN is overridden -- not creating a database; make sure it exists."
    return
  fi
  local exists
  exists="$(PGPASSWORD="$NGT_AIRFLOW_DB_PASSWORD" psql -h "$NGT_AIRFLOW_DB_HOST" -p "$NGT_AIRFLOW_DB_PORT" \
    -U "$NGT_AIRFLOW_DB_USER" -d postgres -Atc \
    "SELECT 1 FROM pg_database WHERE datname = '$NGT_AIRFLOW_DB_NAME'")" || {
    echo "Could not query Postgres as $NGT_AIRFLOW_DB_USER@$NGT_AIRFLOW_DB_HOST -- see README.md's Airflow setup section." >&2
    exit 1
  }
  if [ -z "$exists" ]; then
    echo "Creating Postgres database $NGT_AIRFLOW_DB_NAME (owner $NGT_AIRFLOW_DB_USER)"
    PGPASSWORD="$NGT_AIRFLOW_DB_PASSWORD" createdb -h "$NGT_AIRFLOW_DB_HOST" -p "$NGT_AIRFLOW_DB_PORT" \
      -U "$NGT_AIRFLOW_DB_USER" "$NGT_AIRFLOW_DB_NAME"
  fi
}

demo_env_exports() {
  # Printed as a block and eval'd inside the tmux session, after
  # $NGT_DEV_HOME/env.sh (see start_component), so the scheduler (which is what
  # actually executes task callables under LocalExecutor) inherits the
  # Airflow-only settings on top of the fake toolchain env.sh already spells out.
  #
  # NGT_RUN_END_GRACE_SECONDS: read by _process_tasks.py and used by the
  # file watchers (which run in the triggerer -- this block reaches
  # it too, start_component uses it for all four processes uniformly).
  # Production default (triggers.DEFAULT_RUN_END_GRACE_SECONDS) is 30 min;
  # 20s here is comfortably above NGT_FILE_POLL_SECONDS
  # (5s) without racing a normal in-flight poll, so a scenario's
  # end-run resolves in seconds instead of minutes.
  cat <<ENV
export NGT_RUN_END_GRACE_SECONDS="${NGT_RUN_END_GRACE_SECONDS:-20}"
ENV
}

cmd_setup() {
  activate_venv
  sim_setup
  ensure_airflow_db
  airflow db migrate
  echo
  echo "Set up $NGT_DEV_HOME and the Airflow instance in $AIRFLOW_HOME (database $NGT_AIRFLOW_DB_NAME)"
  echo "Next: $0 start"
}

tmux_console_log() {
  echo "$NGT_DEV_HOME/logs/tmux_console_airflow_$1.log"
}

session_env_file() {
  echo "$AIRFLOW_HOME/airflow_demo_session_env.sh"
}

write_session_env() {
  # What the four tmux sessions start from. A session's shell inherits the tmux *server's*
  # environment -- that of whichever shell first started the server, possibly another
  # checkout's `start` -- and inherited AIRFLOW_HOME/database/DAGs folder/ports would win over
  # airflow_env.sh's `${VAR:-default}`s, quietly pointing this checkout's services at the other
  # checkout's instance. So the file drops every inherited NGT_*/AIRFLOW_* variable and exports
  # this script's own, exactly as airflow_env.sh resolved them (local_*_env.sh, then the
  # caller's environment, then the defaults), plus PYTHONPATH. A file rather than the tmux
  # command line, because AIRFLOW__DATABASE__SQL_ALCHEMY_CONN carries the database password and
  # command lines are readable by every user.
  local file v
  file="$(session_env_file)"
  mkdir -p "$(dirname "$file")"
  rm -f "$file"
  (
    umask 077
    {
      echo "# Written by airflow_demo.sh start for this checkout's tmux sessions -- see its write_session_env."
      # Single-quoted on purpose: expanded when a session sources the file, not here.
      # shellcheck disable=SC2016
      echo 'for _v in $(compgen -e); do case "$_v" in NGT_*|AIRFLOW_*) unset "$_v" ;; esac; done; unset _v'
      for v in $(compgen -e); do
        case "$v" in NGT_*|AIRFLOW_*|PYTHONPATH) printf 'export %s=%q\n' "$v" "${!v}" ;; esac
      done
    } > "$file"
  )
}

start_component() {
  local session="$1" label="$2"; shift 2
  if tmux has-session -t "$session" 2>/dev/null; then
    echo "$session already running"
    return
  fi
  if [ ! -f "$NGT_DEV_HOME/env.sh" ]; then
    echo "No $NGT_DEV_HOME/env.sh -- run: $0 setup" >&2
    exit 1
  fi
  local log; log="$(tmux_console_log "$label")"
  # The session starts from the settings write_session_env saved (see there for why), and
  # ngt_tmux_new_session (sim_env.sh) keeps them out of the tmux server's own environment.
  # $NGT_DEV_HOME/env.sh is sim_env.sh's one spelling-out of the fake environment
  # (NGT_DEV_HOME itself included, which a non-default one needs to reach the fake
  # upload's ledger path) and drops any cmsenv/cmsrel shell functions that would
  # outrank the fake toolchain. The omsapi stub doesn't rely on its PYTHONPATH,
  # though: PYTHONPATH set in the launching shell was not reliably observed to reach
  # Airflow's forked/spawned worker subprocesses in testing, so sim_setup also
  # pip-installs tests/stubs into $NGT_AIRFLOW_VENV. Plain -c, not -lc, as in
  # live_demo.sh: nothing here needs the operator's login dotfiles.
  ngt_tmux_new_session -d -s "$session" "bash -c '
    source \"$(session_env_file)\"
    source \"$NGT_AIRFLOW_VENV/bin/activate\"
    source \"$NGT_DEV_HOME/env.sh\"
    $(demo_env_exports)
    $* 2>&1 | tee \"$log\"
    echo \"($label exited -- press enter to close)\"; read
  '"
  echo "Started $session (console log: $log)"
}

cmd_start() {
  activate_venv
  mkdir -p "$NGT_DEV_HOME/logs"
  write_session_env

  # Airflow 3 splits DAG parsing into its own mandatory process (the
  # scheduler no longer spawns it itself, unlike Airflow 2) -- the scheduler,
  # dag-processor and API server are the baseline. The triggerer is a fourth,
  # hosting ngt_dags_watch.py's AssetWatchers (see its module docstring) --
  # started unconditionally since it's harmless, near-zero-overhead idle
  # time if those DAGs are paused/unused.
  start_component "$SESSION_SCHEDULER" scheduler airflow scheduler
  start_component "$SESSION_DAG_PROCESSOR" dag-processor airflow dag-processor
  start_component "$SESSION_API_SERVER" api "airflow api-server -p $NGT_AIRFLOW_PORT"
  start_component "$SESSION_TRIGGERER" triggerer airflow triggerer

  echo
  echo "Give the dag-processor ~10-15s to parse airflow_automation/airflow_dags/ before unpausing DAGs."
  cmd_ui
}

cmd_ui() {
  echo "Airflow UI: http://localhost:$NGT_AIRFLOW_PORT"
  echo "  (Simple Auth Manager, all-admins mode for this offline dev instance -- no login needed)"
}

cmd_status() {
  tmux list-sessions 2>/dev/null | grep "^$NGT_AIRFLOW_TMUX_PREFIX" || echo "(no ${NGT_AIRFLOW_TMUX_PREFIX}* tmux sessions running)"
}

cmd_logs() {
  local which="$1"
  case "$which" in
    api|scheduler|dag-processor|triggerer) ;;
    *) echo "Usage: $0 logs <scheduler|dag-processor|api|triggerer>" >&2; exit 1 ;;
  esac
  local f; f="$(tmux_console_log "$which")"
  [ -f "$f" ] || { echo "No log yet at $f -- has '$0 start' been run?" >&2; exit 1; }
  tail -f "$f"
}

cmd_stop() {
  for session in "$SESSION_SCHEDULER" "$SESSION_DAG_PROCESSOR" "$SESSION_API_SERVER" "$SESSION_TRIGGERER"; do
    if tmux has-session -t "$session" 2>/dev/null; then
      tmux kill-session -t "$session"
      echo "Killed $session"
    fi
  done
}

cmd_pause_unpause_watch() {
  # ngt_dags_watch.py's 4 DAGs (the all-static-asset AssetWatcher design -- see
  # its module docstring). AssetWatchers only run while their consuming DAG is
  # unpaused, so this is also what starts/stops the 4 watchers in the
  # triggerer. Unpause the process DAGs (file watchers) first, then the primer
  # (run watcher), so the file watchers are live before a run is discovered.
  local action="$1"
  activate_venv
  for calibration in "${CALIBRATIONS[@]}"; do
    local lower; lower="$(echo "$calibration" | tr '[:upper:]' '[:lower:]')"
    airflow dags "$action" -y "ngt_watch_process_${lower}"
  done
  airflow dags "$action" -y "ngt_watch_run_primer"
}

_reset_ngt_assets() {
  # $1 label; $2/$3 AssetModel.uri LIKE patterns to remove. asset_state_store rows go
  # with the AssetModel via ON DELETE CASCADE. A dev-script admin action, not task code
  # (Airflow 3 forbids ORM access from task code, but this is a dev script); skipped with a
  # warning if the ORM shape has drifted rather than failing the whole reset.
  local label="$1" uri_a="$2" uri_b="$3"
  URI_A="$uri_a" URI_B="$uri_b" python3 - <<'PY' || echo "  (Asset row cleanup skipped for $label -- use reset-airflow for a full wipe)"
import os
from airflow.utils.session import create_session
from airflow.models.asset import AssetActive, AssetEvent, AssetModel
from sqlalchemy import or_

with create_session() as session:
    q = session.query(AssetModel).filter(
        or_(AssetModel.uri.like(os.environ["URI_A"]), AssetModel.uri.like(os.environ["URI_B"]))
    )
    assets = q.all()
    ids = [a.id for a in assets]
    uris = [a.uri for a in assets]
    if ids:
        session.query(AssetEvent).filter(AssetEvent.asset_id.in_(ids)).delete(synchronize_session=False)
        session.query(AssetActive).filter(AssetActive.uri.in_(uris)).delete(synchronize_session=False)
        session.query(AssetModel).filter(AssetModel.id.in_(ids)).delete(synchronize_session=False)
    print(f"  Removed {len(ids)} Asset row(s).")
PY
}

_reset_ngt_watermarks() {
  # Clears just the asset_state_store rows (seen_runs / emitted_run<N> watermarks --
  # triggers.py's _watermark_get/_set/_delete) for ngt://runs and ngt://files/<cal>, leaving the
  # AssetModel rows -- and therefore DAG-run history and pause state -- untouched. Unlike
  # _reset_ngt_assets (used by reset-watch), this does NOT cascade-delete the Asset itself; it's
  # the surgical counterpart used by reset-scenario, which has no reason to touch DAG state.
  #
  # Needed because these watermarks live in the Airflow metadata DB, scoped to the Asset row --
  # sim_env.sh's reset+setup only knows about $NGT_DEV_HOME, so without this a run/file number
  # reused after a reset-scenario (e.g. replaying the same scenario after a botched attempt)
  # looks "already seen"/"already emitted" to ngt_dags_watch.py's watchers forever, even though
  # the fake data backing that watermark is gone. A dev-script admin action, not task code (cf.
  # dev script, not task code); skipped with a warning, same as _reset_ngt_assets, if
  # the ORM shape has drifted rather than failing the whole reset. Expects activate_venv to have
  # already run (mirrors _reset_ngt_assets).
  python3 - <<'PY' || echo "  (AssetStateStore cleanup skipped -- use reset-watch for a full wipe)"
from airflow.utils.session import create_session
from airflow.models.asset import AssetModel
from airflow.models.asset_state_store import AssetStateStoreModel
from sqlalchemy import or_

with create_session() as session:
    ids = [
        a.id
        for a in session.query(AssetModel).filter(
            or_(AssetModel.uri.like("ngt://runs%"), AssetModel.uri.like("ngt://files/%"))
        )
    ]
    removed = 0
    if ids:
        removed = (
            session.query(AssetStateStoreModel)
            .filter(AssetStateStoreModel.asset_id.in_(ids))
            .delete(synchronize_session=False)
        )
    print(f"  Removed {removed} AssetStateStore row(s) (seen_runs / emitted_run<N> watermarks).")
PY
}

cmd_reset_watch() {
  # Clears ngt_dags_watch.py's 4 DAGs + its 4 static
  # Assets (ngt://runs, ngt://files/<cal>). Their AssetStateStore rows (the
  # watchers' "seen runs" / "emitted files" watermarks) are removed with the
  # AssetModel via ON DELETE CASCADE.
  activate_venv
  airflow dags delete -y ngt_watch_run_primer
  for calibration in "${CALIBRATIONS[@]}"; do
    local lower; lower="$(echo "$calibration" | tr '[:upper:]' '[:lower:]')"
    airflow dags delete -y "ngt_watch_process_${lower}"
  done
  _reset_ngt_assets "ngt_dags_watch.py" "ngt://runs%" "ngt://files/%"
  echo "Cleared DAG-run history for all 4 ngt_watch_* DAGs (ngt_dags_watch.py)."
}

cmd_reset_airflow() {
  # Drops and recreates the whole metadata DB schema -- all DAG-run
  # history, variables, connections, everything -- not just this repo's DAGs.
  # Back to the state right after `airflow db migrate` first ran (all DAGs
  # paused by default). Use reset-watch instead to clear just
  # one design's history and leave the rest of the DB alone.
  #
  # Stops/restarts the 4 tmux-managed processes around the reset (only if
  # they were already running): each one holds in-memory state tied to the
  # pre-reset schema (e.g. the scheduler/dag-processor's own Job row) and
  # reliably crashes on its next DB query once the tables underneath it have
  # been dropped and recreated -- confirmed live (UndefinedTable: relation
  # "job" does not exist) while testing this command. A reset that leaves the
  # instance broken until the user figures out to restart it manually isn't
  # a usable reset.
  local was_running=0
  if tmux has-session -t "$SESSION_SCHEDULER" 2>/dev/null; then
    was_running=1
    cmd_stop
  fi

  activate_venv
  echo "Resetting the entire Airflow metadata DB (both DAG designs, variables, connections)..."
  airflow db reset -y
  airflow db migrate
  echo "Airflow metadata DB reset and re-migrated -- all DAGs are paused again, as after a fresh install."

  if [ "$was_running" = "1" ]; then
    echo "Restarting the 4 processes (they were running before the reset)..."
    cmd_start
  fi
}

cmd_reset_scenario() {
  # Wraps scenario-player/sim_env.sh's own sim_reset/sim_setup (wipes and recreates
  # $NGT_DEV_HOME -- every fake OMS run/EOS file/output/log) around a
  # stop/restart of Airflow's processes, for the same reason reset-airflow
  # stops/restarts around its own DB reset: doing this while the triggerer
  # has in-flight watcher triggers (ngt_dags_watch.py's AssetWatchers)
  # polling paths under $NGT_DEV_HOME left that work stuck in a running/
  # deferred state indefinitely, and each process's own console log silently
  # stopped updating (confirmed live) -- restarting cleanly afterward is what
  # actually recovers them. sim_env.sh's own reset/setup deliberately don't
  # know or check any of this (see their comments) -- that coordination lives
  # here, in the Airflow-specific adapter, on purpose. Doesn't touch DAG-run
  # history -- pair with reset-watch/reset-airflow for that.
  #
  # Does clear ngt_dags_watch.py's AssetStateStore watermarks (_reset_ngt_watermarks), though:
  # they live in the Airflow metadata DB, scoped to the Asset row, so sim_env.sh's reset+setup
  # can't reach them, and left alone they'd keep describing fake run/file state this just wiped
  # (see _reset_ngt_watermarks' own comment). That's the only Airflow-DB side effect here --
  # it only touches the watermark rows, not the Assets/DAG history/pause state.
  local was_running=0
  if tmux has-session -t "$SESSION_SCHEDULER" 2>/dev/null; then
    was_running=1
    cmd_stop
  fi

  sim_reset
  activate_venv
  sim_setup
  _reset_ngt_watermarks
  echo "Reset and re-set-up $NGT_DEV_HOME."

  if [ "$was_running" = "1" ]; then
    echo "Restarting the 4 processes (they were running before the reset)..."
    cmd_start
  fi
}

case "${1:-}" in
  setup) cmd_setup ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  ui) cmd_ui ;;
  logs) cmd_logs "${2:?scheduler, dag-processor, or api required}" ;;
  unpause-watch) cmd_pause_unpause_watch unpause ;;
  pause-watch) cmd_pause_unpause_watch pause ;;
  reset-watch) cmd_reset_watch ;;
  reset-airflow) cmd_reset_airflow ;;
  reset-scenario) cmd_reset_scenario ;;
  seed-run)
    calibration="${2:?calibration required}"; run="${3:?run number required}"; shift 3 || true
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" seed-run --calibration "$calibration" --run "$run" "$@"
    ;;
  add-ls)
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" add-ls --calibration "${2:?calibration required}" --run "${3:?run required}" --ls "${4:?ls number required}"
    ;;
  end-run)
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" end-run --run "${2:?run number required}"
    ;;
  list-runs)
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" list-runs
    ;;
  arm-fault)
    shift
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" arm-fault "$@"
    ;;
  list-faults)
    shift
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" list-faults "$@"
    ;;
  clear-faults)
    shift
    activate_venv
    python3 "$REPO_DIR/scenario-player/seed.py" clear-faults "$@"
    ;;
  *) usage; exit 1 ;;
esac
