/* Pure, bounded ResearchReportV1 projection. No DOM, fetch, or artifact access. */
(() => {
  "use strict";

  const MAX_ROWS = 5000;
  const MAX_BYTES = 5 * 1024 * 1024;
  const encoder = new TextEncoder();
  const built = new WeakSet();
  // Audited from the registered interpretation library and algorithm metric producers.
  // Each entry is [curated label, producer unit, library value kind].
  const audited = {
    "aligned-end-structure": {
      "ocf_like_end_asymmetry": ["Open-chromatin aligned-end asymmetry (OCF-like)", "end-count difference", "signed"],
      "preferred_end_support_fraction": ["Pinned preferred-end site support", "fraction", "proportion"]
    },
    "arm-summary": {
      "arms_with_value": ["Autosomal arms with an estimable corrected log2", "arms", "count"],
      "chr10p_log2": ["Chromosome 10 p-arm median corrected log2", "log2", "signed"],
      "chr10q_log2": ["Chromosome 10 q-arm median corrected log2", "log2", "signed"],
      "chr11p_log2": ["Chromosome 11 p-arm median corrected log2", "log2", "signed"],
      "chr11q_log2": ["Chromosome 11 q-arm median corrected log2", "log2", "signed"],
      "chr12p_log2": ["Chromosome 12 p-arm median corrected log2", "log2", "signed"],
      "chr12q_log2": ["Chromosome 12 q-arm median corrected log2", "log2", "signed"],
      "chr13p_log2": ["Chromosome 13 p-arm median corrected log2", "log2", "signed"],
      "chr13q_log2": ["Chromosome 13 q-arm median corrected log2", "log2", "signed"],
      "chr14p_log2": ["Chromosome 14 p-arm median corrected log2", "log2", "signed"],
      "chr14q_log2": ["Chromosome 14 q-arm median corrected log2", "log2", "signed"],
      "chr15p_log2": ["Chromosome 15 p-arm median corrected log2", "log2", "signed"],
      "chr15q_log2": ["Chromosome 15 q-arm median corrected log2", "log2", "signed"],
      "chr16p_log2": ["Chromosome 16 p-arm median corrected log2", "log2", "signed"],
      "chr16q_log2": ["Chromosome 16 q-arm median corrected log2", "log2", "signed"],
      "chr17p_log2": ["Chromosome 17 p-arm median corrected log2", "log2", "signed"],
      "chr17q_log2": ["Chromosome 17 q-arm median corrected log2", "log2", "signed"],
      "chr18p_log2": ["Chromosome 18 p-arm median corrected log2", "log2", "signed"],
      "chr18q_log2": ["Chromosome 18 q-arm median corrected log2", "log2", "signed"],
      "chr19p_log2": ["Chromosome 19 p-arm median corrected log2", "log2", "signed"],
      "chr19q_log2": ["Chromosome 19 q-arm median corrected log2", "log2", "signed"],
      "chr1p_log2": ["Chromosome 1 p-arm median corrected log2", "log2", "signed"],
      "chr1q_log2": ["Chromosome 1 q-arm median corrected log2", "log2", "signed"],
      "chr20p_log2": ["Chromosome 20 p-arm median corrected log2", "log2", "signed"],
      "chr20q_log2": ["Chromosome 20 q-arm median corrected log2", "log2", "signed"],
      "chr21p_log2": ["Chromosome 21 p-arm median corrected log2", "log2", "signed"],
      "chr21q_log2": ["Chromosome 21 q-arm median corrected log2", "log2", "signed"],
      "chr22p_log2": ["Chromosome 22 p-arm median corrected log2", "log2", "signed"],
      "chr22q_log2": ["Chromosome 22 q-arm median corrected log2", "log2", "signed"],
      "chr2p_log2": ["Chromosome 2 p-arm median corrected log2", "log2", "signed"],
      "chr2q_log2": ["Chromosome 2 q-arm median corrected log2", "log2", "signed"],
      "chr3p_log2": ["Chromosome 3 p-arm median corrected log2", "log2", "signed"],
      "chr3q_log2": ["Chromosome 3 q-arm median corrected log2", "log2", "signed"],
      "chr4p_log2": ["Chromosome 4 p-arm median corrected log2", "log2", "signed"],
      "chr4q_log2": ["Chromosome 4 q-arm median corrected log2", "log2", "signed"],
      "chr5p_log2": ["Chromosome 5 p-arm median corrected log2", "log2", "signed"],
      "chr5q_log2": ["Chromosome 5 q-arm median corrected log2", "log2", "signed"],
      "chr6p_log2": ["Chromosome 6 p-arm median corrected log2", "log2", "signed"],
      "chr6q_log2": ["Chromosome 6 q-arm median corrected log2", "log2", "signed"],
      "chr7p_log2": ["Chromosome 7 p-arm median corrected log2", "log2", "signed"],
      "chr7q_log2": ["Chromosome 7 q-arm median corrected log2", "log2", "signed"],
      "chr8p_log2": ["Chromosome 8 p-arm median corrected log2", "log2", "signed"],
      "chr8q_log2": ["Chromosome 8 q-arm median corrected log2", "log2", "signed"],
      "chr9p_log2": ["Chromosome 9 p-arm median corrected log2", "log2", "signed"],
      "chr9q_log2": ["Chromosome 9 q-arm median corrected log2", "log2", "signed"]
    },
    "bam-qc": {
      "duplicate_fraction": ["Duplicate-marked share", "fraction", "proportion"],
      "gc_bias_slope": ["GC bias (slope of log2 coverage on GC)", "log2/GC", "signed"],
      "genome_ge1x_fraction": ["Sampled chr1-22 bases covered at least once", "fraction", "proportion"],
      "mapped_fraction": ["Mapped share of primary reads", "fraction", "proportion"],
      "mapq_ge20_fraction": ["Mapped reads with MAPQ >= 20", "fraction", "proportion"],
      "mean_read_q": ["Mean read quality (Phred)", "phred", "positive"],
      "supplementary_fraction": ["Supplementary alignments per mapped read", "fraction", "proportion"],
      "total_reads": ["Primary reads", "reads", "count"]
    },
    "breakpoint-motifs": {
      "cpg_near_end_fraction": ["Aligned-span ends near a reference CpG", "fraction", "proportion"]
    },
    "cell-origin": {
      "classified_fragments": ["Fragments classified at atlas markers", "fragments", "count"],
      "fraction_adipocytes": ["Adipocytes share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_bladder_ep": ["Bladder-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_blood_b": ["Blood-B share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_blood_granul": ["Blood-Granul share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_blood_mono_macro": ["Blood-Mono-Macro share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_blood_nk": ["Blood-NK share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_blood_t": ["Blood-T share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_bone_osteob": ["Bone-Osteob share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_breast_basal_ep": ["Breast-Basal-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_breast_luminal_ep": ["Breast-Luminal-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_colon_ep": ["Colon-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_colon_fibro": ["Colon-Fibro share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_dermal_fibro": ["Dermal-Fibro share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_endothel": ["Endothel share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_epid_kerat": ["Epid-Kerat share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_eryth_prog": ["Eryth-prog share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_fallopian_ep": ["Fallopian-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_gallbladder": ["Gallbladder share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_gastric_ep": ["Gastric-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_head_neck_ep": ["Head-Neck-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_heart_cardio": ["Heart-Cardio share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_heart_fibro": ["Heart-Fibro share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_kidney_ep": ["Kidney-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_liver_hep": ["Liver-Hep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_lung_ep_alveo": ["Lung-Ep-Alveo share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_lung_ep_bron": ["Lung-Ep-Bron share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_megakaryocytes": ["Megakaryocytes share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_neuron": ["Neuron share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_oligodend": ["Oligodend share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_ovary_ep": ["Ovary-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_pancreas_acinar": ["Pancreas-Acinar share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_pancreas_alpha": ["Pancreas-Alpha share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_pancreas_beta": ["Pancreas-Beta share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_pancreas_delta": ["Pancreas-Delta share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_pancreas_duct": ["Pancreas-Duct share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_prostate_ep": ["Prostate-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_skeletal_musc": ["Skeletal-Musc share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_small_int_ep": ["Small-Int-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_smooth_musc": ["Smooth-Musc share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"],
      "fraction_thyroid_ep": ["Thyroid-Ep share of cfDNA (Loyfer U250 atlas)", "fraction", "proportion"]
    },
    "copy-number": {
      "counted_reads": ["Reads counted into copy-number bins", "reads", "count"],
      "identifiable": ["Tumour fraction and ploidy identifiable (ichorCNA solution)", null, "categorical"],
      "ploidy": ["Ploidy (ichorCNA)", "copies", "positive"],
      "pon_policy": ["Panel-of-normals policy used", null, "categorical"],
      "segments_altered": ["Segments called gained or lost", null, "count"],
      "tumor_fraction": ["Tumour fraction (ichorCNA)", "fraction", "proportion"]
    },
    "dd-cfdna-chry": {
      "chry_nonpar_reads": ["Eligible chrY non-PAR reads", "reads", "count"],
      "chry_reads_per_million": ["Eligible chrY non-PAR reads per million", "reads per million", "positive"],
      "dd_cfdna_fraction": ["Exploratory male-donor chrY fraction", "fraction", "proportion"]
    },
    "delfi-ratio": {
      "ratio_sd": ["DELFI-style short/long ratio variability (SD across 5 Mb bins)", "ratio", "positive"]
    },
    "depth": {
      "approx_depth_x": ["Approximate aligned-base depth (eligible reads)", "x", "positive"],
      "eligible_reads": ["Eligible reads (primary, MAPQ >= 20, chr1-22/X/Y)", "reads", "count"],
      "index_mapped_upper_bound": ["Index mapped records (upper bound)", "records", "count"],
      "mean_span_bp": ["Mean aligned span", "bp", "positive"]
    },
    "end-motifs": {
      "ccca_fraction": ["CCCA end-motif share", "fraction", "proportion"],
      "mds": ["End-motif diversity score (4-mer, both fragment ends)", "index", "positive"]
    },
    "epi-clock": {
      "cg02228185_modified_fraction": ["cg02228185 5mC+5hmC fraction", "fraction", "proportion"],
      "cg25809905_modified_fraction": ["cg25809905 5mC+5hmC fraction", "fraction", "proportion"],
      "epigenetic_age_years": ["Exploratory Weidner three-CpG age estimate", "years", "signed"],
      "pde4c_upstream_of_cg17861230_modified_fraction": ["Weidner PDE4C upstream CpG 5mC+5hmC fraction", "fraction", "proportion"]
    },
    "fetal-fraction": {
      "fetal_fraction": ["Fetal fraction (model unavailable)", "fraction", "proportion"],
      "trisomy_13_z": ["Chromosome 13 NIPTeR z-score", "z-score", "signed"],
      "trisomy_18_z": ["Chromosome 18 NIPTeR z-score", "z-score", "signed"],
      "trisomy_21_z": ["Chromosome 21 NIPTeR z-score", "z-score", "signed"]
    },
    "fetal-fraction-chry": {
      "fetal_chry_nonpar_reads": ["Male-fetus chrY non-PAR reads", "reads", "count"],
      "fetal_chry_reads_per_million": ["Male-fetus chrY non-PAR reads per million eligible", "reads per million", "positive"],
      "fetal_fraction": ["Male-fetus chrY calibrated fetal fraction", "fraction", "proportion"]
    },
    "finaletoolkit-xcheck": {
      "delfi_ratio_pearson_r": ["FinaleToolkit paired-end uncorrected DELFI-bin concordance", "correlation", "signed"],
      "motif_pearson_r": ["FinaleToolkit paired-end 4-mer frequency concordance", "correlation", "signed"]
    },
    "frag-x-meth": {
      "short_fragment_hypomethylation_delta": ["Long minus short per-molecule CpG 5mC share", "fraction", "signed"]
    },
    "fragment-size": {
      "mode_bp": ["Fragment length mode (aligned span)", "bp", "positive"],
      "peak_di_bp": ["Di-nucleosome peak (250-450 bp)", "bp", "positive"],
      "peak_mono_bp": ["Mono-nucleosome peak (100-250 bp)", "bp", "positive"],
      "periodicity_10bp_power": ["10 bp periodicity (FFT power share, 50-150 bp)", "fraction", "proportion"],
      "short_fraction_lt150": ["Short-fragment share (50-149 of 50-1000 bp)", "fraction", "proportion"]
    },
    "global-5hmc": {
      "cpg_5hmc_fraction": ["Genome-wide CpG 5hmC fraction (argmax call)", "fraction", "proportion"]
    },
    "global-5mc": {
      "cpg_5mc_fraction": ["Genome-wide CpG 5mC fraction (argmax call)", "fraction", "proportion"]
    },
    "griffin": {
      "central_coverage": ["Griffin GC-corrected central TF-site coverage", "relative", "positive"],
      "site_count": ["Sites in primary Griffin TF list", "sites", "count"]
    },
    "hypometh-reads": {
      "hypometh_read_fraction": ["Reads with < 25% methylated CpGs (among reads with >= 4 CpG calls)", "fraction", "proportion"],
      "u_x_m_counts": ["U / X / M read counts (UXM thresholds)", "reads", "vector"]
    },
    "identity": {
      "fingerprint_bytes": ["Somalier fingerprint size", "bytes", "count"]
    },
    "instability": {
      "bins_missing_estimate": ["Segment bins without a log2 estimate", "bins", "count"],
      "segments_missing_estimate": ["Segments without a log2 estimate", "segments", "count"],
      "segments_used": ["Segments with a log2 estimate used for TMAD", "segments", "count"],
      "tmad": ["Copy-number segment instability (weighted MAD)", "log2", "positive"]
    },
    "lionheart": {
      "cell_types_scored": ["LIONHEART chromatin-accessibility reference profiles scored", "profiles", "count"]
    },
    "long-tail": {
      "gt300_fraction": ["Molecules longer than 300 bp", "fraction", "proportion"],
      "gt7500_fraction": ["Molecules longer than 7.5 kb (cell-lysis class)", "fraction", "proportion"],
      "kb1_4_fraction": ["Molecules of 1-4 kb (delayed-processing class)", "fraction", "proportion"]
    },
    "meth-tf": {
      "cpg_calls_used": ["CpG calls at registered methylation markers", "calls", "count"],
      "fit_rms": ["Unnormalized marker-weight fit residual RMS", "fraction", "positive"],
      "identifiable": ["Two methylation reference columns have numerical rank and nonzero fit", null, "categorical"],
      "markers_covered": ["Registered methylation markers with CpG calls", "markers", "count"],
      "reference_condition_number": ["Count-weighted methylation reference condition number", null, "positive"],
      "tumor_fraction": ["Tumour share of fitted methylation reference weights", "fraction", "proportion"]
    },
    "methylbert": {
      "classified_reads": ["Unique reads contributing to MethylBERT deconvolution", "reads", "count"],
      "normal_fraction": ["MethylBERT normal-source fraction", "fraction", "proportion"],
      "tumour_fraction": ["MethylBERT tumour-source fraction (exploratory)", "fraction", "proportion"]
    },
    "microbial": {
      "candidate_unmapped_reads": ["Human-unmapped reads submitted to Kraken2", "reads", "count"],
      "classified_candidate_reads": ["Kraken2 classified candidate reads", "reads", "count"],
      "microbial_estimated_reads": ["Bracken estimated microbial reads", "estimated reads", "positive"],
      "microbial_rpm_primary": ["Candidate microbial estimated reads per million primary reads", "estimated reads/million", "positive"],
      "missing_sequence_unmapped_reads": ["Human-unmapped reads without sequence", "reads", "count"],
      "omitted_taxa": ["Microbial taxa omitted from the bounded display", "taxa", "count"],
      "primary_reads": ["Primary QC-passing non-duplicate reads", "reads", "count"],
      "too_short_unmapped_reads": ["Human-unmapped reads below Bracken length", "reads", "count"],
      "unclassified_candidate_reads": ["Kraken2 unclassified candidate reads", "reads", "count"]
    },
    "modbase-qc": {
      "invalid_mm_records": ["Tagged reads with unusable MM/ML (MN mismatch or unparsable)", "reads", "count"],
      "ml_below_filter_fraction": ["CpG calls below 0.7 confidence", "fraction", "proportion"],
      "ml_mean": ["Mean ML probability at CpG calls (C+m and C+h values)", "fraction", "proportion"],
      "mm_tagged_fraction": ["Eligible reads carrying modified-base (MM) tags", "fraction", "proportion"],
      "mod_codes": ["Modification codes in the MM tags (first 10,000 tagged reads)", null, "categorical"]
    },
    "mtdna": {
      "chrm_fraction": ["Mitochondrial (chrM) share of primary reads (chrM at MAPQ >= 20)", "fraction", "proportion"],
      "chrm_mode_bp": ["chrM fragment-length mode (aligned span)", "bp", "positive"]
    },
    "pmd-score": {
      "non_pmd_meth": ["CpG 5mC fraction outside common PMDs", "fraction", "proportion"],
      "pmd_delta": ["PMD hypomethylation (5mC outside minus inside common PMDs)", "fraction", "signed"],
      "pmd_meth": ["CpG 5mC fraction inside common PMDs", "fraction", "proportion"]
    },
    "sex-check": {
      "call": ["Sex call (male / female / indeterminate)", null, "categorical"],
      "chry_chrx_ratio": ["chrY/chrX read density (per Mb of non-N, non-PAR reference)", "ratio", "positive"]
    },
    "short-read-fragment-size": {
      "mode_bp": ["Short-read fragment mode (absolute TLEN, 100–250 bp)", "bp", "positive"],
      "peak_di_bp": ["Short-read di-nucleosome TLEN peak (250–450 bp)", "bp", "positive"],
      "peak_mono_bp": ["Short-read mono-nucleosome TLEN peak (100–250 bp)", "bp", "positive"],
      "periodicity_10bp_power": ["Short-read TLEN 10 bp periodicity (FFT power share, 50–150 bp)", "fraction", "proportion"],
      "short_fraction_lt150": ["Short-read template share (50–149 of 50–1000 bp TLEN)", "fraction", "proportion"]
    },
    "tss-profile": {
      "central_flank_ratio": ["TSS central-to-flank coverage ratio", "ratio", "positive"],
      "tss_genes": ["Protein-coding TSSs with complete windows", "genes", "count"]
    },
    "viral-reads": {
      "ebv_rpm": ["EBV-aligned reads per million human-unmapped primaries", "reads / 1M unmapped primaries", "positive"],
      "hbv_rpm": ["HBV-aligned reads per million human-unmapped primaries", "reads / 1M unmapped primaries", "positive"],
      "hpv_rpm": ["HPV-aligned reads per million human-unmapped primaries", "reads / 1M unmapped primaries", "positive"]
    },
    "wps": {
      "qualifying_fragment_depth_x": ["WPS-eligible 120–180 bp aligned-fragment depth", "x", "positive"]
    }
  };
  const sha = /^[0-9a-f]{64}$/;
  const id = /^[a-z0-9-]{1,40}$/;
  const metricId = /^[a-z0-9_]{1,64}$/;
  const version = /^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,119}$/;
  const utc = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
  const instant = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z$/;
  const tool = /^[A-Za-z0-9_.:-]{1,120}$/;
  const toolVersion = /^[A-Za-z0-9_.+:-]{1,40}$/;
  const reference = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
  const attempt = /^a-\d{8}T\d{12}-[0-9a-f]{8}$/;
  const units = new Set([
    "end-count difference", "fraction", "arms", "log2", "log2/GC", "phred", "reads",
    "copies", "reads per million", "ratio", "bp", "records", "x", "index", "fragments",
    "years", "z-score", "correlation", "relative", "sites", "bins", "segments", "genes",
    "profiles", "calls", "markers", "bytes", "estimated reads", "estimated reads/million",
    "taxa", "reads / 1M unmapped primaries",
  ]);
  const displayUnits = new Set([...units, "%", "percentage points", "unit not declared"]);
  const states = new Set([
    "not_interpretable", "no_reference", "in_range", "above_typical", "markedly_above",
    "below_typical", "markedly_below",
  ]);
  const reasons = new Set([
    "depth", "derived_input", "disk", "missing_toolchain", "missing_asset", "no_pon",
    "protocol_mismatch", "invalid_asset", "asset_unavailable", "missing_channel",
    "low_support", "missing_tags", "example_reference", "input_unavailable", "platform",
    "unsupported_input", "platform_unverified", "superseded_method", "derived_not_supported",
    "missing_input", "input_kind", "reference", "upstream", "unqualified_support",
    "missing_gpu", "model_not_ready",
  ]);
  const columns = [
    "schema_version", "generated_at", "research_use_only", "example", "sample_id", "source_id",
    "full_input_id", "run_utc", "interpretation_version", "cohort_version", "limitations",
    "algorithm_id", "metric_id", "label", "raw_value", "raw_unit", "display_value",
    "display_unit", "status", "skip_reason", "interpretation_state", "tool", "tool_version",
    "parameters_sha256", "attempt_id", "reference_id", "computed_at",
  ];

  function fail(code, message) {
    throw Object.assign(new Error(message), { code });
  }

  function validInstant(value, pattern = instant) {
    return typeof value === "string" && pattern.test(value) && !Number.isNaN(Date.parse(value));
  }

  function safeText(value, max = 160) {
    return typeof value === "string" && value.length > 0 && value.length <= max &&
      !/[\x00-\x1f\x7f/\\]/.test(value) &&
      !/(?:password|secret|token|api[_ -]?key|bearer|AKIA|sk-[A-Za-z0-9]{8}|[ACGTN]{30})/i.test(value);
  }

  function validProvenance(value, sample) {
    return value && typeof value === "object" &&
      typeof value.tool === "string" && tool.test(value.tool) &&
      typeof value.tool_version === "string" && toolVersion.test(value.tool_version) &&
      typeof value.parameters_sha256 === "string" && sha.test(value.parameters_sha256) &&
      value.source_id === sample.source_id && value.input_id === sample.full_input_id &&
      typeof value.reference_id === "string" && reference.test(value.reference_id) &&
      validInstant(value.timestamp_utc, utc) &&
      (value.attempt_id == null || (typeof value.attempt_id === "string" && attempt.test(value.attempt_id)));
  }

  function scalar(value, algorithmId, metricIdValue, valueKind) {
    if (value == null) return { valid: true, value: null };
    if (valueKind === "categorical") {
      if (metricIdValue === "identifiable" &&
          (algorithmId === "copy-number" || algorithmId === "meth-tf"))
        return { valid: typeof value === "boolean", value };
      if (typeof value !== "string") return { valid: false };
      const allowed = algorithmId === "sex-check" && metricIdValue === "call"
        ? ["male", "female", "indeterminate"]
        : algorithmId === "copy-number" && metricIdValue === "pon_policy"
          ? ["none", "lab_loo_v1"] : [];
      return { valid: allowed.includes(value), value };
    }
    if (typeof value === "number") return { valid: true, value: Number.isFinite(value) ? value : null,
      nonfinite: !Number.isFinite(value) };
    return { valid: false };
  }

  function sizeCheck(value) {
    if (encoder.encode(value).length > MAX_BYTES) fail("report_too_large", "The report exceeds the 5 MiB export limit.");
    return value;
  }

  function project(sample, generatedAt, formatResultMetric) {
    if (!sample || sample.found !== true || sample.state ||
        typeof sample.id12 !== "string" || !/^[0-9a-f]{12,64}$/.test(sample.id12) ||
        !sha.test(sample.source_id) || !sample.source_id.startsWith(sample.id12) ||
        !sha.test(sample.full_input_id) || typeof sample.example !== "boolean" ||
        !version.test(sample.interpretation_version) ||
        !(sample.cohort_version == null || version.test(sample.cohort_version)) ||
        !(sample.run_utc === "" || validInstant(sample.run_utc)) ||
        !validInstant(generatedAt) || !Array.isArray(sample.catalog) ||
        !Array.isArray(sample.algorithms) || typeof formatResultMetric !== "function") {
      fail("invalid_report_identity", "The selected sample cannot be prepared as a report.");
    }
    const limitations = [
      "Research use only; not for diagnosis or clinical decisions.",
      "Only visible registered scalar metrics are included; hidden processing steps, arrays, artifacts, and taxon detail are excluded.",
      "Stable input and provenance identifiers may be linkable to other local records.",
    ];
    const note = (message) => { if (!limitations.includes(message)) limitations.push(message); };
    const visible = new Set(sample.catalog.filter((row) => row && id.test(row.algorithm_id))
      .map((row) => row.algorithm_id));
    const metrics = [];
    let scanned = 0;
    for (const algorithm of sample.algorithms) {
      if (!algorithm || !visible.has(algorithm.algorithm_id) ||
          !Object.hasOwn(audited, algorithm.algorithm_id) || algorithm.hidden === true) continue;
      const algorithmId = algorithm.algorithm_id;
      const status = algorithm.status;
      const reason = status === "skipped" && reasons.has(algorithm.skip_reason) ? algorithm.skip_reason : null;
      if (status === "skipped" || status === "failed") {
        note(`Method ${algorithmId}: ${status}${reason ? ` (${reason})` : ""}; no scalar rows.`);
        continue;
      }
      if (status !== "ok") { note(`Method ${algorithmId}: no eligible scalar rows.`); continue; }
      if (!Array.isArray(algorithm.metrics)) { note(`Method ${algorithmId}: no eligible scalar rows.`); continue; }
      for (const metric of algorithm.metrics) {
        if (++scanned > 100000) fail("report_too_large", "The report exceeds the export processing limit.");
        const definition = metric && metricId.test(metric.metric_id) &&
          Object.hasOwn(audited[algorithmId], metric.metric_id)
          ? audited[algorithmId][metric.metric_id] : null;
        if (!Array.isArray(definition)) {
          note("Some metrics were omitted because their identities were not in the audited registry.");
          continue;
        }
        const [label, producerUnit, valueKind] = definition;
        if (valueKind === "vector") {
          note("Some metrics were omitted because their values were not scalar.");
          continue;
        }
        const raw = scalar(metric.value, algorithmId, metric.metric_id, valueKind);
        if (!raw.valid) {
          note("Some metrics were omitted because their values were unsupported.");
          continue;
        }
        if (!validProvenance(algorithm.provenance, sample)) {
          note("Some metrics were omitted because their provenance was unavailable or invalid.");
          continue;
        }
        if (!(metric.unit == null || metric.unit === producerUnit) ||
            (producerUnit !== null && !units.has(producerUnit))) {
          note("Some metrics were omitted because their units did not match audited producer metadata.");
          continue;
        }
        if (raw.nonfinite) note("Some nonfinite numeric values are unavailable in this report.");
        const interpretationState = metric.state == null || states.has(metric.state) ? metric.state ?? null : null;
        if (metric.state != null && interpretationState == null) note("Some interpretation states were unavailable.");
        // Pass only vetted scalar fields; free-form reason, entry, and API diagnostics never reach the formatter.
        let formatted;
        try {
          formatted = formatResultMetric({ algorithm_id: algorithmId, metric_id: metric.metric_id,
            value: raw.value, unit: metric.unit ?? null,
            value_kind: valueKind }, { algorithmId, reason: null });
        } catch (_) {
          note("Some metrics were omitted because their display values were unavailable.");
          continue;
        }
        if (!formatted || !safeText(formatted.displayValue, 120) ||
            !(formatted.displayUnit == null || displayUnits.has(formatted.displayUnit))) {
          note("Some metrics were omitted because their display values were unavailable.");
          continue;
        }
        if (metrics.length >= MAX_ROWS) fail("report_too_many_rows", "The report exceeds the 5,000 row export limit.");
        metrics.push({
          algorithm_id: algorithmId, metric_id: metric.metric_id, label,
          raw_value: raw.value, raw_unit: metric.unit ?? null,
          display_value: formatted.displayValue, display_unit: formatted.displayUnit ?? null,
          status, skip_reason: reason, interpretation_state: interpretationState,
          tool: algorithm.provenance.tool, tool_version: algorithm.provenance.tool_version,
          parameters_sha256: algorithm.provenance.parameters_sha256,
          attempt_id: algorithm.provenance.attempt_id ?? null,
          reference_id: algorithm.provenance.reference_id,
          computed_at: algorithm.provenance.timestamp_utc,
        });
      }
    }
    if (!metrics.length) fail("no_report_rows", "No eligible scalar metrics are available for this sample.");
    const report = {
      schema_version: "1", generated_at: generatedAt, research_use_only: true,
      example: sample.example, sample_id: sample.id12, source_id: sample.source_id,
      full_input_id: sample.full_input_id, run_utc: sample.run_utc || null,
      interpretation_version: sample.interpretation_version,
      cohort_version: sample.cohort_version ?? null,
      limitations: Object.freeze(limitations), metrics: Object.freeze(metrics.map((row) => Object.freeze(row))),
    };
    sizeCheck(JSON.stringify(report));
    Object.freeze(report);
    built.add(report);
    return report;
  }

  function json(report) {
    if (!built.has(report)) fail("invalid_report_model", "Prepare a report before downloading it.");
    return sizeCheck(`${JSON.stringify(report, null, 2)}\n`);
  }

  function csv(report) {
    if (!built.has(report)) fail("invalid_report_model", "Prepare a report before downloading it.");
    const text = (value) => {
      if (value == null) return "";
      if (typeof value === "boolean" || typeof value === "number") return String(value);
      let result = String(value);
      if (/^[\t\r\n]/.test(result) || /^[\s]*[=+\-@]/.test(result)) result = `'${result}`;
      return `"${result.replace(/"/g, '""')}"`;
    };
    const identity = Object.fromEntries(columns.slice(0, 10).map((key) => [key, report[key]]));
    const lines = [columns.join(",")];
    for (const row of report.metrics) {
      const values = { ...identity, limitations: report.limitations.join(" | "), ...row };
      lines.push(columns.map((key) => text(values[key])).join(","));
    }
    return sizeCheck(`${lines.join("\r\n")}\r\n`);
  }

  function filename(report, extension) {
    if (!built.has(report) || !["json", "csv"].includes(extension))
      fail("invalid_report_model", "Prepare a report before downloading it.");
    const stamp = report.generated_at.slice(0, 19).replace(/[-:]/g, "");
    return `traceback-research-${report.sample_id}-${stamp}Z.${extension}`;
  }

  globalThis.TracebackLabReport = { project, json, csv, filename };
})();
