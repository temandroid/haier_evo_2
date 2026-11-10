#!/usr/bin/env bash
# Test environment on Home Assistant 2026.11.0.
#
# Temporary: pytest-homeassistant-custom-component has no release for 2026.11
# yet (the latest, 0.13.375, pins 2026.10.4). Until it has, HA 2026.11.0 is
# installed over it and two incompatibilities of the old plugin are patched:
#  - HA 2026.11 aliases httpx to httpx2 and refuses to start once httpx is
#    imported, which the plugin does first: the alias is made at interpreter start;
#  - _clear_logger_overwrites moved to homeassistant.components.logger.helpers.
# Drop this script and pin the new plugin once it is out.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "homeassistant==2026.11.0" > /tmp/haier_evo_override.txt
uv pip install --override /tmp/haier_evo_override.txt -r requirements_test.txt
site=$(python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
echo "import httpx2; httpx2.alias_httpx()" > "$site/zz_alias_httpx.pth"
python - "$site/pytest_homeassistant_custom_component/common.py" <<'PY'
import sys
path = sys.argv[1]
src = open(path, encoding="utf-8").read()
old = "    SERVICE_SET_LEVEL,\n    _clear_logger_overwrites,\n)"
new = "    SERVICE_SET_LEVEL,\n)\nfrom homeassistant.components.logger.helpers import _clear_logger_overwrites"
if old in src:
    open(path, "w", encoding="utf-8").write(src.replace(old, new))
PY
python -c "import homeassistant.const as c; print('Home Assistant', c.__version__)"
