"""允许 ``python -m stm32gen_gui`` 直接启动。"""

from __future__ import annotations

import sys

from stm32gen_gui.app import main

if __name__ == "__main__":
    sys.exit(main())
