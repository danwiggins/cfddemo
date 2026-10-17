# MethylBERT model bundle contract

The Lab's planned `methylbert` result needs a **fine-tuned tumour/normal model**. The generic pretrained MethylBERT encoder is not a tumour-purity model. No suitable public checkpoint has been identified, so this contract is preparatory; registering a bundle does not yet enable a Lab result.

An operator-trusted, uncompressed TAR contains exactly these seven regular files, with no links, PAX/GNU extensions, or extra members. The archive and sum of member sizes are each capped at 2 GiB:

```text
bundle.json
bert.model/config.json
bert.model/pytorch_model.bin
bert.model/dmr_encoder.pickle
bert.model/read_classification_model.pickle
train_seq.csv
dmrs.tsv
```

`bundle.json` has schema `traceback.methylbert-bundle.v1`, a `model_id`, the 40-character pinned upstream Git commit, a human-readable `source` and `terms` statement, the hg38 reference SHA-256, `n_mers` of 3, a positive `seq_len` no greater than 4096, and a `files` object with the SHA-256 of each of the six non-manifest files. Registration refuses missing, extra, duplicate, linked, oversized, or digest-mismatched members. The model and training table must be the matched fine-tuning pair; the training table supplies class margins during deconvolution.

Register with `traceback method-asset register --root ROOT --kind methylbert-bundle --id ASSET_ID --file model.tar`. Registration pins the TAR's digest and checks every member; the worker will reverify the registration and copy the bundle to private scratch before loading it. Choose a new asset ID for each model revision. The two pickle files can execute code when loaded: accept bundles only from an operator-trusted source and never load them during registration or in the web process.

The runtime contract now refuses a model or installed package outside pinned commit `f82f83b0bc20e2af1441795536ec9f338fbbb3e7`, a mismatched reference, non-ONT input, fewer than 2.5 million eligible reads, or an unavailable CUDA GPU. Its contained process has a two-hour deadline and bounded logs; only a small, strict tumour/normal aggregate may be accepted from private scratch. This contract is not yet connected to a Lab algorithm. The eventual runner must pin the bundle digest, DMRs, tokenizer settings, and input basecalling model in execution identity, and use the registered training table by copied path rather than the original absolute `train_dataset` path in MethylBERT's `train_param.txt`. Upstream's CLI hardcodes 40 data-loader workers, so that CLI must not be invoked unchanged. Per-read logits and read names must remain private.

Upstream references: [MethylBERT source](https://github.com/CompEpigen/methylbert), [data preparation](https://methylbert.readthedocs.io/en/latest/tutorials/01_Data_Preparation.html), and [tumour deconvolution](https://methylbert.readthedocs.io/en/latest/tutorials/05_tumour_deconvolution.html). The hosted 2.0.2 data guide still describes Bismark/XM input, while current source includes Dorado MM/ML support. A pinned-source smoke of the Dorado parser (`src/methylbert/data/bam.py`, SHA-256 `c5b30a5c6dfdd64cf30241a18d193388924c82b670f0a2d82e893f2c7e71c6fe`) decoded a simple forward read. A synthetic reverse read did not preserve its high-confidence call under the same probe, so reverse-strand input needs a validated BAM fixture and further investigation. A trusted fine-tuned tumour/normal checkpoint is also required before runtime activation.
