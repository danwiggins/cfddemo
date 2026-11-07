# Lab adaptation of broadinstitute/ichorCNA scripts/createPanelOfNormals.R
# at commit 5bfc03ed854f0e93fe5b624c97c1290fa0053837 (GPL-3.0).
# The upstream panel calculation is retained. Its optparse/eval CLI is replaced
# by fixed, bounded arguments for the Lab's autosomal, UCSC, median protocol.

library(HMMcopy)
library(GenomicRanges)
library(ichorCNA)

options(stringsAsFactors = FALSE, scipen = 0)
args <- commandArgs(trailingOnly = TRUE)
keys <- c("--filelist", "--gcWig", "--mapWig", "--centromere", "--outfile")
if (length(args) != 2L * length(keys) ||
    !identical(args[seq.int(1L, length(args), by = 2L)], keys)) {
  stop("expected exactly --filelist --gcWig --mapWig --centromere --outfile")
}
paths <- args[seq.int(2L, length(args), by = 2L)]
if (any(!nzchar(paths)) || any(!startsWith(paths, "/")) ||
    any(grepl("[\r\n\t]", paths))) {
  stop("panel paths must be absolute, nonempty and single-line")
}
names(paths) <- keys
if (any(!file.exists(paths[1:4]))) stop("a panel input is missing")

files <- readLines(paths[["--filelist"]], warn = FALSE)
if (length(files) < 9L || any(!startsWith(files, "/")) ||
    any(!grepl("^[0-9a-f]{64}\\.wig$", basename(files))) ||
    anyDuplicated(files) || any(!file.exists(files))) {
  stop("filelist needs at least nine distinct absolute source WIGs")
}
ids <- sub("\\.wig$", "", basename(files))
if (anyDuplicated(ids)) stop("filelist repeats a source")

chrs <- as.character(seq_len(22L))
seqlevelsStyle(chrs) <- "UCSC"
gc <- wigToGRanges(paths[["--gcWig"]])
map <- wigToGRanges(paths[["--mapWig"]])
centromere <- read.delim(paths[["--centromere"]], header = TRUE,
                         stringsAsFactors = FALSE, sep = "\t")
seqlevelsStyle(centromere$Chr) <- "UCSC"

normalGR <- NULL
for (i in seq_along(files)) {
  sid <- ids[[i]]
  message("Loading normal file: ", sid)
  normal_reads <- wigToGRanges(files[[i]])
  normal_counts <- loadReadCountsFromWig(
    normal_reads, chrs = chrs, gc = gc, map = map,
    centromere = centromere, targetedSequences = NULL,
    genomeStyle = "UCSC", chrNormalize = chrs
  )
  if (is.null(normalGR)) {
    normalGR <- normal_counts$counts
    values(normalGR) <- values(normalGR)$copy
    colnames(values(normalGR)) <- sid
  } else {
    if (!identical(as.character(seqnames(normalGR)),
                   as.character(seqnames(normal_counts$counts))) ||
        !identical(start(normalGR), start(normal_counts$counts)) ||
        !identical(end(normalGR), end(normal_counts$counts))) {
      stop("normal WIGs do not share corrected bins")
    }
    values(normalGR)[[sid]] <- normal_counts$counts$copy
  }
}

medianVal <- apply(values(normalGR), 1L, median, na.rm = TRUE)
values(normalGR)[["Median"]] <- medianVal
outfile <- paths[["--outfile"]]
write.table(as.data.frame(normalGR[, "Median"]),
            file = paste0(outfile, "_median.txt"),
            col.names = TRUE, row.names = FALSE, quote = FALSE, sep = "\t")
saveRDS(normalGR, file = paste0(outfile, "_median.rds"))
