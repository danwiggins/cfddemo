"""Private, isolated MethylBERT child. Never publish per-read rows or logits.

Executed as an absolute script with Python ``-I`` and a private bytecode cache.
The parent worker owns this directory and removes it after the bounded child.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path


def _read_request(path: Path) -> dict[str, object]:
    if path.stat().st_size > 8192:
        raise ValueError("MethylBERT child request is oversized")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or set(value) != {
        "bam", "reference", "model_dir", "scratch", "aggregate", "code_sha256",
        "reference_sha256", "model_id", "n_mers", "seq_len", "eligible_reads", "min_classified_reads",
        "numerical_runtime",
    }:
        raise ValueError("MethylBERT child request is malformed")
    return value


def _training_counts(path: Path) -> tuple[object, set[int]]:
    """Compute exact N/T margins and observed labels without retaining rows."""

    import pandas as pd

    counts: Counter[str] = Counter()
    labels: set[int] = set()
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if (not {"ctype", "dmr_label"}.issubset(reader.fieldnames or ())
                or len(set(reader.fieldnames or ())) != len(reader.fieldnames or ())):
            raise ValueError("training table lacks class and DMR labels")
        for row in reader:
            cell = row["ctype"]
            if cell not in ("N", "T"):
                raise ValueError("training table is not a two-class N/T model")
            label = int(row["dmr_label"])
            if label < 0 or label > 100_000:
                raise ValueError("training DMR label is invalid")
            counts[cell] += 1
            labels.add(label)
    total = counts["N"] + counts["T"]
    if not counts["N"] or not counts["T"] or not labels:
        raise ValueError("training table lacks both classes or DMRs")
    return pd.Series({"N": counts["N"] / total, "T": counts["T"] / total}), labels


def _tumour_dmrs(path: Path) -> set[int]:
    """Require the pinned upstream's tumour-specific, dense DMR labels."""

    labels: set[int] = set()
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        required = {"chr", "start", "end", "ctype", "dmr_id"}
        if (not required.issubset(reader.fieldnames or ())
                or len(set(reader.fieldnames or ())) != len(reader.fieldnames or ())):
            raise ValueError("DMR table lacks unambiguous coordinates and tumour labels")
        for row in reader:
            if row["ctype"] != "T":
                raise ValueError("MethylBERT requires tumour-specific DMRs for the N/T class mapping")
            label = int(row["dmr_id"])
            if label < 0 or label > 100_000 or label in labels:
                raise ValueError("DMR labels must be unique and bounded")
            start, end = int(row["start"]), int(row["end"])
            if not row["chr"] or start < 0 or end <= start:
                raise ValueError("DMR coordinates are invalid")
            labels.add(label)
    if not labels or labels != set(range(len(labels))):
        raise ValueError("DMR labels must be dense from zero")
    return labels


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(16 << 20):
            digest.update(block)
    return digest.hexdigest()


def _save_aggregate(path: Path, *, model_id: str, tumour: float, normal: float, classified: int) -> None:
    from traceback_runner.serialization import canonical_json_bytes

    body = canonical_json_bytes({
        "schema": "traceback.methylbert-aggregate.v1", "model_id": model_id,
        "tumour_fraction": tumour, "normal_fraction": normal, "classified_reads": classified,
    })
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        return 2
    request = _read_request(Path(argv[0]))
    scratch = Path(str(request["scratch"]))
    if not scratch.is_absolute() or not scratch.is_dir():
        raise ValueError("private MethylBERT scratch is missing")
    for key in ("bam", "reference", "model_dir", "aggregate"):
        if not Path(str(request[key])).is_absolute():
            raise ValueError("MethylBERT child paths must be absolute")
    if not Path(str(request["model_dir"])).resolve().is_relative_to(scratch.resolve()):
        raise ValueError("MethylBERT model escaped private scratch")
    if not Path(str(request["aggregate"])).parent.resolve().is_relative_to(scratch.resolve()):
        raise ValueError("MethylBERT aggregate escaped private scratch")
    if _file_sha256(Path(str(request["reference"]))) != request["reference_sha256"]:
        raise ValueError("MethylBERT child reference changed after registration")

    # Keep installed numerical packages ahead of the Lab source checkout.
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    import traceback_lab

    if Path(traceback_lab.__file__).resolve() != Path(__file__).resolve().parent / "__init__.py":
        raise ValueError("MethylBERT child loaded a different Lab source tree")
    from traceback_lab.methylbert_runtime import (
        PINNED_UPSTREAM_COMMIT, apply_precision_flags, installed_identity, require_numerical_runtime,
    )

    identity = installed_identity()
    if identity.commit != PINNED_UPSTREAM_COMMIT or identity.sha256 != request["code_sha256"]:
        raise ValueError("MethylBERT child imported-code identity differs from the trusted pin")
    import methylbert

    installed_dir = Path(metadata.distribution("methylbert").locate_file("methylbert")).resolve()
    if not Path(methylbert.__file__).resolve().is_relative_to(installed_dir):
        raise ValueError("MethylBERT child imports from outside the attested installation")
    if installed_identity() != identity:
        raise ValueError("MethylBERT installed sources changed during child import")
    apply_precision_flags(request["numerical_runtime"])
    require_numerical_runtime(request["numerical_runtime"])

    import numpy as np
    import torch
    from torch.utils.data import DataLoader
    from methylbert.data.finetune_data_generate import finetune_data_generate
    from methylbert.data.dataset import MethylBertFinetuneDataset
    from methylbert.data.vocab import MethylVocab
    from methylbert.deconvolute import purity_estimation
    from methylbert.trainer import MethylBertFinetuneTrainer

    if not torch.cuda.is_available():
        raise ValueError("MethylBERT CUDA GPU is unavailable in the child")
    model_dir = Path(str(request["model_dir"]))
    if request["n_mers"] != 3 or type(request["seq_len"]) is not int or not 1 <= request["seq_len"] <= 4096:
        raise ValueError("MethylBERT tokenizer does not match the registered bundle")
    if (type(request["eligible_reads"]) is not int or type(request["min_classified_reads"]) is not int
            or not 1 <= request["min_classified_reads"] <= request["eligible_reads"]):
        raise ValueError("MethylBERT classified-support floor is invalid")
    margins, training_labels = _training_counts(model_dir / "train_seq.csv")
    dmr_labels = _tumour_dmrs(model_dir / "dmrs.tsv")
    if not training_labels.issubset(dmr_labels):
        raise ValueError("training labels do not match the registered tumour DMRs")
    n_dmrs = len(dmr_labels)
    # The pinned trainer expects these pickle files beside bert.model rather
    # than inside it. Move only the already-verified private copies.
    for name in ("dmr_encoder.pickle", "read_classification_model.pickle"):
        os.replace(model_dir / "bert.model" / name, model_dir / name)
    input_dir = scratch / "prepared"
    finetune_data_generate(
        f_dmr=str(model_dir / "dmrs.tsv"), output_dir=str(input_dir),
        f_ref=str(request["reference"]), input_file=str(request["bam"]),
        n_mers=3, split_ratio=None, n_cores=1, methyl_caller="dorado",
    )
    tokenizer = MethylVocab(k=3)
    dataset = MethylBertFinetuneDataset(str(input_dir / "data.csv"), tokenizer,
                                         seq_len=int(request["seq_len"]), n_cores=1)
    loader = DataLoader(dataset, batch_size=64, num_workers=0, pin_memory=True)
    trainer = MethylBertFinetuneTrainer(
        len(tokenizer), save_path=str(scratch / "trainer"),
        train_dataloader=loader, test_dataloader=loader, with_cuda=True,
    )
    if trainer.device.type != "cuda":
        raise ValueError("MethylBERT trainer fell back from CUDA")
    trainer.load(str(model_dir / "bert.model"), load_fine_tune=True, n_dmrs=n_dmrs)
    apply_precision_flags(request["numerical_runtime"])
    require_numerical_runtime(request["numerical_runtime"])
    rows, logits = trainer.read_classification(data_loader=loader, tokenizer=tokenizer, logit=True)
    if (logits.ndim != 2 or logits.shape[1] != 2 or not np.isfinite(logits).all()
            or np.any(logits < 0) or np.any(logits > 1)
            or not np.allclose(logits.sum(axis=1), 1.0, atol=1e-5, rtol=0)):
        raise ValueError("MethylBERT classifier produced invalid probabilities")
    if len(rows) != len(logits) or not {"name", "methyl_seq", "dmr_label", "dmr_ctype", "ctype_label"}.issubset(rows.columns):
        raise ValueError("MethylBERT classifier returned malformed per-read rows")
    if not rows["dmr_ctype"].eq("T").all() or not rows["ctype_label"].eq(0).all():
        raise ValueError("MethylBERT inference rows violate the tumour-DMR class mapping")
    if not set(rows["dmr_label"]).issubset(dmr_labels):
        raise ValueError("MethylBERT inference DMR labels differ from the registered bundle")
    rows = rows.drop(columns=["ctype_label"])
    rows["P_N"] = logits[:, 0]
    rows["P_ctype"] = logits[:, 1]
    rows = rows[rows["methyl_seq"].str.count("[01]") > 0]
    classified = int(rows["name"].nunique())
    if not request["min_classified_reads"] <= classified <= request["eligible_reads"]:
        raise ValueError("MethylBERT classified-read support is invalid")
    estimate, _fisher = purity_estimation(rows, margins, n_grid=10000, adjustment=False)
    fractions = dict(zip(estimate["cell_type"], estimate["pred"]))
    if (set(fractions) != {"T", "N"} or not all(np.isfinite(value) and 0 <= value <= 1 for value in fractions.values())
            or not np.isclose(fractions["T"] + fractions["N"], 1.0, atol=1e-6, rtol=0)):
        raise ValueError("MethylBERT deconvolution returned invalid tumour/normal fractions")
    _save_aggregate(
        Path(str(request["aggregate"])), model_id=str(request["model_id"]),
        tumour=float(fractions["T"]), normal=float(fractions["N"]), classified=classified,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
