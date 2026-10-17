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

After creating `bundle.json` with the six exact file hashes, run this from the directory containing those paths (GNU tar, default blocking factor):

```sh
tar --format=ustar -cf model.tar bundle.json bert.model/config.json bert.model/pytorch_model.bin bert.model/dmr_encoder.pickle bert.model/read_classification_model.pickle train_seq.csv dmrs.tsv
```

List the member paths explicitly: a leading `./`, directory entries, PAX/GNU headers, or nondefault TAR record padding is refused. Every listed file must be nonempty.

`bundle.json` has schema `traceback.methylbert-bundle.v1`, a `model_id`, the 40-character pinned upstream Git commit, a human-readable `source` and `terms` statement, the hg38 reference SHA-256, `n_mers` of 3, a positive `seq_len` no greater than 4096, and a `files` object with the SHA-256 of each of the six non-manifest files. Registration refuses missing, extra, duplicate, linked, oversized, or digest-mismatched members. The model and training table must be the matched fine-tuning pair; the training table supplies class margins during deconvolution.

Register with `traceback method-asset register --root ROOT --kind methylbert-bundle --id ASSET_ID --file model.tar`. Registration pins the TAR's digest and checks every member; the worker will reverify the registration and copy the bundle to private scratch before loading it. Choose a new asset ID for each model revision. The two pickle files can execute code when loaded: accept bundles only from an operator-trusted source and never load them during registration or in the web process.

The runtime contract now refuses a model or installed package outside pinned commit `f82f83b0bc20e2af1441795536ec9f338fbbb3e7`, a mismatch against an operator-trusted installed-code SHA-256, a mismatched reference, non-ONT input, fewer than 2.5 million eligible reads, or an unavailable CUDA GPU. The code digest covers installed MethylBERT `.py` files listed in the distribution RECORD; it does not cover bytecode caches, compiled extensions, or unlisted files. The operator must establish the expected digest from a trusted installation. Its standalone worker helper has a two-hour deadline and bounded logs; only a small, strict tumour/normal aggregate with an explicit positive `classified_reads` count no larger than the eligible-read count may be accepted from private scratch. Eligible input reads and classified reads are different denominators; no qualified minimum classified count or model reliability rule has been established. This contract is not yet connected to a Lab algorithm. Activation must qualify classification support from a trusted model and establish how the inner worker is cancelled with the outer Lab process group; it must also verify the bytes actually imported by the child. The eventual runner must pin the installed-code digest, bundle digest, DMRs, tokenizer settings, and input basecalling model in execution identity, and use the registered training table by copied path rather than the original absolute `train_dataset` path in MethylBERT's `train_param.txt`. Upstream's CLI hardcodes 40 data-loader workers, so that CLI must not be invoked unchanged. Per-read logits and read names must remain private.

Before activation, the Lab adapter must enforce this sequence: resolve and copy a digest-pinned registered bundle, check `require_ready` with an independently trusted installed-code digest, then run the worker with an allowlisted offline environment and a verified child import path. It must propagate outer Lab cancellation into any child process group, remove private scratch after success or failure, and refuse publication until a trusted tumour model establishes an interpretable classified-read support rule. The standalone helpers do not enforce this composition on their own.

Upstream references: [MethylBERT source](https://github.com/CompEpigen/methylbert), [data preparation](https://methylbert.readthedocs.io/en/latest/tutorials/01_Data_Preparation.html), and [tumour deconvolution](https://methylbert.readthedocs.io/en/latest/tutorials/05_tumour_deconvolution.html). The hosted 2.0.2 data guide still describes Bismark/XM input, while current source includes Dorado MM/ML support. A pinned-source smoke of the Dorado parser (`src/methylbert/data/bam.py`, SHA-256 `c5b30a5c6dfdd64cf30241a18d193388924c82b670f0a2d82e893f2c7e71c6fe`) decoded a forward read and the repository's modkit-validated synthetic reverse fixture. This verifies the parser on those cases, not a complete ONT BAM or tumour model. A trusted fine-tuned tumour/normal checkpoint and full input-path validation are required before runtime activation.
