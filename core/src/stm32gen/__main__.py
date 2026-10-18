"""``python -m stm32gen`` 入口。"""

from __future__ import annotations

import sys

from stm32gen.cli import main

if __name__ == "__main__":
    sys.exit(main())
