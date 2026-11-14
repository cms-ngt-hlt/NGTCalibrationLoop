#!/usr/bin/env bash
# An input file onto an instance's data volume, at /ngt-data/inputs/<name>, where real-file scenarios name it
# ($NGT_DEV_HOME/../inputs/<name>): by default the file's own name. Through the tools pod, in parts of 200 MiB with a
# `kubectl exec` each: one stream of several GB is more than the tunnel reliably carries. Run it again to resume. It ends
# by comparing checksums.
#
#   NS=<namespace> airflow_automation/airflow_containers/daq-dev-multipod/upload_input.sh <file> [<name>]
set -euo pipefail
file=${1:?usage: NS=<namespace> upload_input.sh <file> [<name>]}
: "${NS:?set NS to the namespace of the instance}"
name=${2:-$(basename "$file")}
size=$(stat -c %s "$file")
mib=${PART_MB:-200}
part=$((mib * 1024 * 1024))
parts=$(((size + part - 1) / part))
dir=/ngt-data/inputs/.parts-$name
tools() { kubectl -n "$NS" exec "$@"; }
there() { tools deploy/ngt-airflow-tools -- sha256sum "/ngt-data/inputs/$name" 2> /dev/null | cut -d' ' -f1 || true; }

here=$(sha256sum "$file" | cut -d' ' -f1)
if [ "$(there)" = "$here" ]; then
  echo "/ngt-data/inputs/$name is there already"
  exit 0
fi
tools deploy/ngt-airflow-tools -- mkdir -p "$dir"
for ((i = 0; i < parts; i++)); do
  target=$(printf '%s/%04d' "$dir" "$i")
  want=$((i == parts - 1 ? size - i * part : part))
  [ "$(tools deploy/ngt-airflow-tools -- stat -c %s "$target" 2> /dev/null || true)" = "$want" ] && continue
  echo "part $((i + 1)) of $parts"
  dd if="$file" bs=1M skip=$((i * mib)) count="$mib" status=none \
    | tools -i deploy/ngt-airflow-tools -- sh -c "cat > '$target.tmp' && mv '$target.tmp' '$target'"
done
tools deploy/ngt-airflow-tools -- sh -c \
  "cat '$dir'/[0-9]* > '/ngt-data/inputs/$name.tmp' && mv '/ngt-data/inputs/$name.tmp' '/ngt-data/inputs/$name' && rm -r '$dir'"
[ "$(there)" = "$here" ] || { echo "upload_input.sh: the copy's checksum differs from $here" >&2; exit 1; }
echo "/ngt-data/inputs/$name: $size bytes, sha256 $here"
