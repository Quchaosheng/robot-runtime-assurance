#!/usr/bin/env bash
# Two passes, because ROS 2 changes the answer and both must be exercised:
#   1. no ROS 2  -> mock scenarios pass, the ros2 scenario must be blocked, not skipped
#   2. with ROS 2 -> the same scenario runs over real DDS
set -eu        # no `pipefail`/`-u` when sourcing ROS: its setup scripts read unset vars
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

echo "== pass 1: no ROS 2 =="
python3 -m assurance.cli doctor
python3 -m pytest -q -p no:cacheprovider

echo
echo "== pass 1b: black-box path (skipped, not faked, without a workbench checkout) =="
python3 -m assurance.cli run workbench/normal-task
python3 -m assurance.cli run workbench/runtime-killed
python3 -m assurance.cli run policy/raw-command-refused
python3 -m assurance.cli run negative-controls/sut-accepts-raw-command
python3 -m assurance.cli run trace/latency-evidence-verified
python3 -m assurance.cli run socketcan/device-not-on-this-host

echo
echo "== pass 1c: every shipped scenario matches its declared verdict =="
python3 ci/check_scenarios.py

echo
echo "== pass 2: with ROS 2 =="
# shellcheck disable=SC1091
set +u
# shellcheck disable=SC1090
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"
set -u
export ROS_LOG_DIR="${ROS_LOG_DIR:-/tmp/assurance-ros-log}" ROS_HOME="${ROS_HOME:-/tmp/assurance-ros-home}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-73}"
mkdir -p "$ROS_LOG_DIR" "$ROS_HOME"
python3 -m assurance.cli doctor
# ROS 2 ships pytest plugins written for pytest 7; they crash pytest 9 at collection.
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:randomly
