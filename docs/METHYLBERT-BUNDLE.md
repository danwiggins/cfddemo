# MethylBERT model bundle contract

The Lab's planned `methylbert` result needs a **fine-tuned tumour/normal model**. The generic pretrained MethylBERT encoder is not a tumour-purity model. No suitable public checkpoint has been identified, so this contract is preparatory; registering a bundle does not yet enable a Lab result.

An operator-trusted, uncompressed TAR contains exactly these seven regular files, with no links or extra members:

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

The planned runner must pin the bundle digest, upstream code revision, reference, DMRs, tokenizer settings, and input basecalling model in its execution identity. It must use the registered training table by copied path rather than the original absolute `train_dataset` path in MethylBERT's `train_param.txt`; the upstream CLI also hardcodes 40 data-loader workers, so the Lab will call the library under controlled resource limits. It will require 2.5 million eligible ONT reads and a usable GPU, and it will publish only aggregate tumour fraction and quality information, never per-read logits or read names.

Upstream references: [MethylBERT source](https://github.com/CompEpigen/methylbert), [data preparation](https://methylbert.readthedocs.io/en/latest/tutorials/01_Data_Preparation.html), and [tumour deconvolution](https://methylbert.readthedocs.io/en/latest/tutorials/05_tumour_deconvolution.html). The hosted 2.0.2 data guide still describes Bismark/XM input, while current source includes Dorado MM/ML support. A pinned ONT smoke test and licensed tumour/normal checkpoint are required before runtime activation.
