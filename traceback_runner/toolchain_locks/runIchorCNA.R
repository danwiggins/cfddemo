# Traceback driver for the locked ichorCNA package (bioconda r-ichorcna 0.5.1).
#
# Upstream ichorCNA v0.5.1 ships no runIchorCNA.R script; its entry point is
# the exported function ichorCNA::run_ichorCNA().  This driver accepts the
# script-style flags that evidence_inspector/ichor_adapter.py emits
# (_expected_argv) and calls that function with them.  Every flag is required
# exactly once except --mapWig and --normalPanel; an unknown flag stops the run.
# It installs nothing and reads no file itself.
#
# Its SHA-256 is declared in the toolchain lock files, so a change here is a
# new lock and a new install directory.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) %% 2L != 0L) {
  stop("arguments must be --flag value pairs")
}
flags <- args[c(TRUE, FALSE)]
values <- args[c(FALSE, TRUE)]
required <- c(
  "--id", "--WIG", "--gcWig", "--centromere", "--normal", "--ploidy", "--maxCN",
  "--estimateScPrevalence", "--scStates", "--lambda", "--minMapScore",
  "--rmCentromereFlankLength", "--txnE", "--txnStrength", "--minSegmentBins",
  "--altFracThreshold", "--chrs", "--chrTrain", "--chrNormalize",
  "--genomeBuild", "--genomeStyle", "--includeHOMD", "--outDir"
)
optional <- c("--mapWig", "--normalPanel")
unknown <- setdiff(flags, c(required, optional))
if (length(unknown) > 0L) {
  stop("unknown flag(s): ", paste(unknown, collapse = " "))
}
if (anyDuplicated(flags) > 0L) {
  stop("a flag is repeated")
}
missing_flags <- setdiff(required, flags)
if (length(missing_flags) > 0L) {
  stop("missing flag(s): ", paste(missing_flags, collapse = " "))
}
opt <- as.list(stats::setNames(values, flags))
get_opt <- function(name) if (is.null(opt[[name]])) NULL else opt[[name]]
as_number <- function(name) {
  value <- suppressWarnings(as.numeric(opt[[name]]))
  if (length(value) != 1L || is.na(value)) stop(name, " is not a number")
  value
}
as_flag <- function(name) {
  value <- opt[[name]]
  if (!value %in% c("TRUE", "FALSE")) stop(name, " must be TRUE or FALSE")
  identical(value, "TRUE")
}
# The adapter writes numeric vectors as c(...) literals of finite numbers
# (format ".15g", so 1e+15 is possible); accept nothing else.
numeric_vector_text <- function(name) {
  value <- opt[[name]]
  if (!grepl("^c\\([-+0-9.e,]+\\)$", value)) stop(name, " is not a c(...) numeric vector")
  value
}

# ichorCNA 0.5.1's run_ichorCNA() accepts `lambda` but never uses it (the
# initialisation ignores it), so an explicit value would be silently dropped.
if (!identical(opt[["--lambda"]], "NULL")) {
  stop("--lambda must be NULL: ichorCNA 0.5.1 ignores explicit lambda values")
}
# run_ichorCNA() evaluates scStates as R text; "NULL" evaluates to NULL.
scStates <- if (identical(opt[["--scStates"]], "NULL")) "NULL" else numeric_vector_text("--scStates")
suppressPackageStartupMessages(library(ichorCNA))
dir.create(opt[["--outDir"]], recursive = TRUE, showWarnings = FALSE)
run_ichorCNA(
  tumor_wig = opt[["--WIG"]],
  gcWig = opt[["--gcWig"]],
  mapWig = get_opt("--mapWig"),
  normal_panel = get_opt("--normalPanel"),
  id = opt[["--id"]],
  centromere = opt[["--centromere"]],
  minMapScore = as_number("--minMapScore"),
  flankLength = as_number("--rmCentromereFlankLength"),
  normal = numeric_vector_text("--normal"),
  ploidy = numeric_vector_text("--ploidy"),
  maxCN = as_number("--maxCN"),
  estimateNormal = TRUE,
  estimatePloidy = TRUE,
  estimateScPrevalence = as_flag("--estimateScPrevalence"),
  scStates = scStates,
  txnE = as_number("--txnE"),
  txnStrength = as_number("--txnStrength"),
  minSegmentBins = as_number("--minSegmentBins"),
  altFracThreshold = as_number("--altFracThreshold"),
  chrs = numeric_vector_text("--chrs"),
  chrTrain = numeric_vector_text("--chrTrain"),
  chrNormalize = numeric_vector_text("--chrNormalize"),
  genomeBuild = opt[["--genomeBuild"]],
  genomeStyle = opt[["--genomeStyle"]],
  includeHOMD = as_flag("--includeHOMD"),
  outDir = opt[["--outDir"]],
  cores = 1
)
