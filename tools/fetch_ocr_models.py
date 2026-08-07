"""拉取 OCR 模型（PP-OCRV5 mobile）到 runs/models/rapidocr。

实现见 src/ocr.ensure_v5_models()；本脚本供本地手动 / CI 打包前调用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ocr import MODEL_ROOT, ensure_v5_models  # noqa: E402

if __name__ == '__main__':
    ok = ensure_v5_models()
    print('OCR 模型就绪: ' + str(MODEL_ROOT) if ok else 'OCR 模型下载失败（将回退内置模型）')
    sys.exit(0 if ok else 1)
