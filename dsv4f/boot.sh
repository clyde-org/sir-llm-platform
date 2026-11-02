#!/bin/bash
# dsv4f boot wrapper for nightly-releases-v0.30.0-openeuler (2026-10-26 build).
# This build has no vllm-ascend-entrypoint.sh; its image Entrypoint is:
#   bash -c "source /usr/local/Ascend/ascend-toolkit/set_env.sh &&
#            source /usr/local/Ascend/cann-${CANN_VERSION}/share/info/ascendnpu-ir/bin/set_env.sh &&
#            source /usr/local/Ascend/nnal/atb/set_env.sh --cxx_abi=${ATB_CXX_ABI:-1} &&
#            exec \"$@\"" --
# We mirror that, inserting the vllm-core import fix first.
python3 /dsv4f-patch/fix-import.py
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/cann-${CANN_VERSION}/share/info/ascendnpu-ir/bin/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh --cxx_abi=${ATB_CXX_ABI:-1}
exec vllm "$@"
