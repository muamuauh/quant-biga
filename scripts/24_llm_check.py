from __future__ import annotations

import argparse
import base64
import json
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.llm import (  # noqa: E402
    describe,
    relay_client,
    resolve_relay,
    resolve_vision,
    vision_client,
)


def _red_png_data_url(size: int = 64) -> str:
    """纯标准库生成无隐私的红色 PNG，供 Vision 连通性检查。"""
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    png = signature + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="脱敏检查中转站配置；默认不联网")
    parser.add_argument("--probe", action="store_true", help="发一个最小 Chat Completions 请求")
    parser.add_argument("--probe-vision", action="store_true",
                        help="发送本地生成的纯红 PNG，验证多模态输入；不含真实截图")
    args = parser.parse_args(argv)
    summary = describe()
    result = {"config": summary, "probe": "not_requested"}
    if args.probe:
        cfg = resolve_relay()
        try:
            response = relay_client(cfg).chat.completions.create(
                model=cfg.effective_quick_model,
                messages=[{"role": "user", "content": "只回复 OK"}],
                max_tokens=8,
                temperature=0,
            )
            result["probe"] = "ok" if response.choices[0].message.content else "empty_response"
        except Exception as exc:  # noqa: BLE001
            result["probe"] = f"{type(exc).__name__}: {str(exc).replace(cfg.api_key, '<redacted>')}"
    if args.probe_vision:
        cfg = resolve_vision()
        try:
            response = vision_client(cfg).chat.completions.create(
                model=cfg.model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "图片是什么纯色？只回答 RED 或其他颜色英文。"},
                    {"type": "image_url", "image_url": {"url": _red_png_data_url()}},
                ]}],
                max_tokens=8,
                temperature=0,
            )
            answer = str(response.choices[0].message.content or "").strip()
            result["vision_probe"] = {"ok": "RED" in answer.upper(), "answer": answer[:30]}
        except Exception as exc:  # noqa: BLE001
            safe = str(exc).replace(cfg.api_key, "<redacted>")
            result["vision_probe"] = {"ok": False, "error": f"{type(exc).__name__}: {safe}"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    vision_ok = not args.probe_vision or result.get("vision_probe", {}).get("ok", False)
    return 0 if (summary["relay_ready"] and result["probe"] in {"not_requested", "ok"}
                 and vision_ok) else 2


if __name__ == "__main__":
    raise SystemExit(main())
