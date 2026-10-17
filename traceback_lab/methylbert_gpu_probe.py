"""Standalone CUDA identity probe; the orchestrator must not retain a context."""

from __future__ import annotations

import json
from typing import Any


def probe_cuda() -> dict[str, Any]:
    try:
        import torch

        available = bool(torch.cuda.is_available())
        count = torch.cuda.device_count() if available else 0
        cuda: dict[str, Any] = {
            "available": available and count > 0,
            "torch_cuda": torch.version.cuda,
            "device_count": count,
        }
        if available:
            cuda.update({
                "current_device": torch.cuda.current_device(),
                "devices": [
                    {"name": torch.cuda.get_device_name(index),
                     "capability": list(torch.cuda.get_device_capability(index))}
                    for index in range(count)
                ],
                "cudnn": torch.backends.cudnn.version(),
                "matmul_allow_tf32": torch.backends.cuda.matmul.allow_tf32,
                "cudnn_allow_tf32": torch.backends.cudnn.allow_tf32,
                "cudnn_benchmark": torch.backends.cudnn.benchmark,
                "float32_matmul_precision": torch.get_float32_matmul_precision(),
            })
        return cuda
    except (ImportError, OSError, RuntimeError):
        return {"available": False, "torch_cuda": None, "device_count": 0}


if __name__ == "__main__":
    print(json.dumps(probe_cuda(), sort_keys=True))
