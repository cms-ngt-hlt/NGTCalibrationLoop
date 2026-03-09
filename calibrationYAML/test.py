#!/usr/bin/env python
import yaml
from omsapi import OMSAPI

# --- Paste your config values here, or load from yaml ---
with open("EcalPedestals.yaml", "r") as f:  # or whichever
    calib_config = yaml.safe_load(f)

step_2_config = calib_config["step_2_config"]
fillType   = step_2_config["filltype"]
l1_hlt_mode = step_2_config["l1hltmode"]

print(f"Testing with filltype='{fillType}', l1hltmode='{l1_hlt_mode}'")

omsapi = OMSAPI("https://cmsoms.cms/agg/api", "v1", cert_verify=False)

# --- Test 1: same query as NewRunAvailable() ---
q = omsapi.query("runs")
q.filter("fill_type_runtime", fillType)
q.filter("l1_hlt_mode", l1_hlt_mode)
q.sort("run_number", asc=False).paginate(page=1, per_page=10)

# Print the URL before even firing it - useful to sanity check
print("Query URL:", q.data_query())

response = q.data().json()

if "data" not in response or not response["data"]:
    print("No runs returned! Check your filter values.")
else:
    print(f"Got {len(response['data'])} runs:")
    for run in response["data"]:
        attrs = run["attributes"]
        print(
            f"  Run {attrs['run_number']} | "
            f"fill_type={attrs.get('fill_type_runtime')} | "
            f"l1_hlt_mode={attrs.get('l1_hlt_mode')} | "
            f"last_ls={attrs.get('last_lumisection_number')} | "
            f"end_time={attrs.get('end_time')}"
        )

# --- Test 2: same query as LastLSRunNumber() for a specific run ---
test_run = int(input("\nEnter a run number to test LastLSRunNumber query (or 0 to skip): "))
if test_run:
    q2 = omsapi.query("runs")
    q2.filter("run_number", test_run)
    r2 = q2.data().json()
    if "data" in r2 and r2["data"]:
        print(f"Run {test_run}: last_ls = {r2['data'][0]['attributes'].get('last_lumisection_number')}")
    else:
        print(f"Run {test_run} not found in OMS.")
