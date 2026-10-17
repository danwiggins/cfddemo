"""Exercise the private child entry point without a checkpoint or CUDA device."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import traceback_lab.methylbert_child as child
import traceback_lab.methylbert_runtime as runtime


def _module(monkeypatch: pytest.MonkeyPatch, name: str, **attributes: object) -> ModuleType:
    module = ModuleType(name)
    module.__dict__.update(attributes)
    if name in {"methylbert", "methylbert.data", "torch", "torch.utils"}:
        module.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_child_main_checks_private_paths_support_and_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "private"
    model = scratch / "model"
    weights = model / "bert.model"
    weights.mkdir(parents=True)
    (weights / "dmr_encoder.pickle").write_bytes(b"private encoder")
    (weights / "read_classification_model.pickle").write_bytes(b"private classifier")
    (model / "train_seq.csv").write_text("ctype\tdmr_label\nN\t0\nT\t0\n", encoding="utf-8")
    (model / "dmrs.tsv").write_text("chr\tstart\tend\tctype\tdmr_id\nchr1\t0\t20\tT\t0\n", encoding="utf-8")
    reference = tmp_path / "ref.fa"
    reference.write_text(">chr1\n" + "A" * 20 + "\n", encoding="ascii")
    bam = tmp_path / "input.bam"
    bam.write_bytes(b"stubbed upstream boundary")
    installed = scratch / "installed" / "methylbert"
    installed.mkdir(parents=True)
    package_file = installed / "__init__.py"
    package_file.write_text("", encoding="ascii")
    pin = "a" * 64
    identity = SimpleNamespace(commit=runtime.PINNED_UPSTREAM_COMMIT, sha256=pin)
    monkeypatch.setattr(runtime, "installed_identity", lambda: identity)
    monkeypatch.setattr(runtime, "apply_precision_flags", lambda _flags: None)
    monkeypatch.setattr(runtime, "require_numerical_runtime", lambda _flags: None)
    monkeypatch.setattr(child.metadata, "distribution", lambda _name: SimpleNamespace(
        locate_file=lambda _part: installed,
    ))

    _module(monkeypatch, "methylbert", __file__=str(package_file))
    _module(monkeypatch, "methylbert.data")
    _module(monkeypatch, "methylbert.data.finetune_data_generate", finetune_data_generate=lambda **_kwargs: None)
    _module(monkeypatch, "methylbert.data.dataset", MethylBertFinetuneDataset=lambda *_args, **_kwargs: object())

    class Vocab:
        def __init__(self, *, k: int) -> None:
            assert k == 3

        def __len__(self) -> int:
            return 4

    _module(monkeypatch, "methylbert.data.vocab", MethylVocab=Vocab)
    _module(monkeypatch, "methylbert.deconvolute", purity_estimation=lambda *_args, **_kwargs: (
        pd.DataFrame({"cell_type": ["N", "T"], "pred": [0.6, 0.4]}), None,
    ))

    class Trainer:
        device = SimpleNamespace(type="cuda")

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def load(self, *_args: object, **_kwargs: object) -> None:
            pass

        def read_classification(self, **_kwargs: object) -> tuple[pd.DataFrame, np.ndarray]:
            rows = pd.DataFrame({
                "name": ["read-1"], "methyl_seq": ["01"], "dmr_label": [0],
                "dmr_ctype": ["T"], "ctype_label": [0],
            })
            return rows, np.array([[0.6, 0.4]])

    _module(monkeypatch, "methylbert.trainer", MethylBertFinetuneTrainer=Trainer)
    _module(monkeypatch, "torch", cuda=SimpleNamespace(is_available=lambda: True))
    _module(monkeypatch, "torch.utils")
    _module(monkeypatch, "torch.utils.data", DataLoader=lambda *_args, **_kwargs: object())

    request = {
        "bam": str(bam), "reference": str(reference), "model_dir": str(model),
        "scratch": str(scratch), "aggregate": str(scratch / "aggregate.json"),
        "code_sha256": pin, "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        "model_id": "fixture-model", "n_mers": 3, "seq_len": 128,
        "eligible_reads": 2, "min_classified_reads": 2, "numerical_runtime": {},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="classified-read support is below"):
        child.main([str(request_path)])
    assert not (scratch / "aggregate.json").exists()
    assert json.loads((scratch / "support-refusal.json").read_text()) == {
        "reason": "below_floor", "classified_reads": 1, "min_classified_reads": 2,
    }

    request["min_classified_reads"] = 1
    request_path.write_text(json.dumps(request), encoding="utf-8")
    (weights / "dmr_encoder.pickle").write_bytes(b"private encoder")
    (weights / "read_classification_model.pickle").write_bytes(b"private classifier")
    assert child.main([str(request_path)]) == 0
    aggregate = json.loads((scratch / "aggregate.json").read_text())
    assert aggregate["model_id"] == "fixture-model"
    assert aggregate["classified_reads"] == 1
    assert aggregate["tumour_fraction"] == pytest.approx(0.4)
    assert aggregate["normal_fraction"] == pytest.approx(0.6)

    request["aggregate"] = str(tmp_path / "escaped.json")
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="aggregate escaped"):
        child.main([str(request_path)])
