- 2026-11-07: `finaletoolkit-xcheck` runs on native ONT single-end input. The
  Lab writes one FinaleDB fragment row per D21-eligible primary read (aligned
  reference span) and runs FinaleToolkit 1.1.0 end-motifs and DELFI on that
  file; the Lab counts the same rows. The artifact records `input_mode`
  (`paired_end` or `ont_single_end`) and the fragment definition. Illumina
  inputs and their execution identity are unchanged.
