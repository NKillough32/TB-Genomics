#!/usr/bin/env bash

source ~/.bashrc >/dev/null 2>&1 || true

echo "[INFO] Checking TBProfiler and Mykrobe in WSL env: tbtools"
if command -v micromamba >/dev/null 2>&1; then
  micromamba run -n tbtools tb-profiler --version || true
  micromamba run -n tbtools mykrobe --help || true
  eval "$(micromamba shell hook -s bash)"
  micromamba activate tbtools || true
elif [ -x "$HOME/micromamba" ]; then
  "$HOME/micromamba" run -n tbtools tb-profiler --version || true
  "$HOME/micromamba" run -n tbtools mykrobe --help || true
  eval "$($HOME/micromamba shell hook -s bash)"
  micromamba activate tbtools || true
else
  echo "[WARN] micromamba not found in WSL. Install micromamba and the tbtools environment."
fi

echo
echo "[INFO] Leaving a WSL shell open. Close this window when finished."
echo "[INFO] Active environment: ${CONDA_DEFAULT_ENV:-none}"
exec bash -i
