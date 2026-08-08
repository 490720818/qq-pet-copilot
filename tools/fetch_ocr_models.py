"""拉取 OCR 模型（PP-OCRV5 mobile）到 runs/models/rapidocr。

实现见 src/ocr.ensure_v5_models()（下载失败自动重试 3 次）；
本脚本供本地手动 / CI 打包前调用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ocr import MODEL_ROOT, ensure_v5_models  # noqa: E402

if __name__ == '__main__':
    # CI（GitHub Actions Windows runner）默认 stdout 是 cp1252，直接打印中文会崩；
    # 显式切成 UTF-8，本地/打包/CI 都能正常输出
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass  # 个别重定向场景不支持 reconfigure，忽略
    ok = ensure_v5_models()
    print('OCR 模型就绪: ' + str(MODEL_ROOT) if ok else 'OCR 模型下载失败（将回退内置模型）')
    sys.exit(0 if ok else 1)
