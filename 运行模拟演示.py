"""一条命令运行完整模拟流程并显示中文结论。"""
import datetime
from pathlib import Path
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
out = Path(__file__).resolve().parent / ("演示结果-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
raise SystemExit(subprocess.run([sys.executable, "-m", "pandao_batch", "demo", "--out", str(out)]).returncode)
