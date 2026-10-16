args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3L) stop("expected panel, reference panel, and source IDs")
panel <- readRDS(args[[1L]])
reference <- args[[2L]]
source_ids <- args[-c(1L, 2L)]
if (!inherits(panel, "GRanges")) stop("panel is not GRanges")
columns <- S4Vectors::mcols(panel)
if (!identical(colnames(columns), c(source_ids, "Median"))) {
  stop("panel columns differ from the declared sources")
}
if (length(panel) == 0L || anyDuplicated(source_ids) ||
    any(!vapply(as.list(columns), is.numeric, logical(1L)))) {
  stop("panel has empty, repeated, or nonnumeric data")
}
if (any(vapply(as.list(columns), function(x) any(is.infinite(x) | is.nan(x)), logical(1L))) ||
    !any(is.finite(columns$Median))) {
  stop("panel has no finite median or contains infinite values")
}
if (reference != "-") {
  full <- readRDS(reference)
  if (!inherits(full, "GRanges") ||
      !identical(as.character(GenomicRanges::seqnames(panel)),
                 as.character(GenomicRanges::seqnames(full))) ||
      !identical(IRanges::start(panel), IRanges::start(full)) ||
      !identical(IRanges::end(panel), IRanges::end(full))) {
    stop("panel bins differ from the full panel")
  }
}
