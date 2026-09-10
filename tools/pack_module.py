#!/usr/bin/env python3
# 已迁移到 neon-dash/ndash.py —— 此 shim 仅做转发，参数不变。
import os
import subprocess
import sys

sys.exit(subprocess.call(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "neon-dash", "ndash.py")] + sys.argv[1:],
))
