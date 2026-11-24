"""启动脚本，也是 PyInstaller 打包的入口。源码运行：python run.py"""

import sys

from phonecloud.cli import main

if __name__ == "__main__":
    sys.exit(main())
