#!/opt/disc-steward-ocr-venv/bin/python
from __future__ import annotations

import json
import sys

from rapidocr_onnxruntime import RapidOCR


def main() -> None:
    engine = RapidOCR()
    results: list[str] = []
    for image_path in sys.argv[1:]:
        output, _ = engine(image_path)
        lines: list[str] = []
        for entry in output or []:
            _box, text, confidence = entry
            if confidence is not None and float(confidence) < 0.3:
                continue
            if text:
                lines.append(str(text).strip())
        results.append("\n".join(line for line in lines if line))
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
