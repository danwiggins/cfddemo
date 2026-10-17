"""Generate synthetic-only inputs for an isolated run of pinned upstream Griffin."""

import argparse
from pathlib import Path
import random
import tempfile

import pandas as pd
import pyBigWig
import pysam
import yaml


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base-dir', type=Path,
                    default=Path(tempfile.gettempdir()) / 'cfdna-griffin')
args = parser.parse_args()
root = args.base_dir.resolve() / 'proof'
root.mkdir(parents=True, exist_ok=True)
random.seed(160)
sequence = ''.join(random.choices('ACGT', k=30000))
(root / 'toy.fa').write_text('>chr1\n' + '\n'.join(sequence[i:i+80] for i in range(0, len(sequence), 80)) + '\n')
pysam.faidx(str(root / 'toy.fa'))
(root / 'chrom.sizes').write_text('chr1\t30000\n')
(root / 'mappable.bed').write_text('chr1\t0\t30000\n')
(root / 'GC_frequency').mkdir(exist_ok=True)
length = 160
for frequency_length in range(140, 181):
    frequency = [0] * (frequency_length + 1)
    gc_count = sum(base in 'GC' for base in sequence[:frequency_length])
    frequency[gc_count] += 1
    for start in range(1, len(sequence) - frequency_length + 1):
        gc_count += (sequence[start + frequency_length - 1] in 'GC') - (sequence[start - 1] in 'GC')
        frequency[gc_count] += 1
    pd.DataFrame({
        'length': [frequency_length] * (frequency_length + 1),
        'num_GC': list(range(frequency_length + 1)),
        'number_of_fragments': frequency,
    }).to_csv(root / 'GC_frequency' / f'mappable.{frequency_length}bp.GC_frequency.txt', sep='\t', index=False)
(root / 'sites.tsv').write_text('Chrom\tposition\tStrand\nchr1\t10000\t+\n')
(root / 'sites.yaml').write_text(yaml.safe_dump({'site_lists': {'ToySite': str(root / 'sites.tsv')}}))

with pyBigWig.open(str(root / 'mappability.bw'), 'w') as bw:
    bw.addHeader([('chr1', 30000)])
    bw.addEntries(['chr1'], [0], ends=[30000], values=[1.0])

pd.DataFrame({
    'length': [length] * (length + 1),
    'num_GC': list(range(length + 1)),
    'smoothed_GC_bias': [2.0] * (length + 1),
}).to_csv(root / 'GC_bias.tsv', sep='\t', index=False)
pd.DataFrame({
    'mappable_percent': list(range(101)),
    'smoothed_map_bias': [1.0] * 101,
}).to_csv(root / 'map_bias.tsv', sep='\t', index=False)

header = {'HD': {'VN': '1.6', 'SO': 'coordinate'}, 'SQ': [{'SN': 'chr1', 'LN': 30000}]}
random.seed(5219)
reads = []
for i in range(500):
    start = random.randint(8500, 11340)
    for mate in (1, 2):
        read = pysam.AlignedSegment()
        read.query_name = f'toy_{i:04d}'
        read.query_sequence = 'A' * 75
        read.flag = 99 if mate == 1 else 147
        read.reference_id = 0
        read.reference_start = start if mate == 1 else start + length - 75
        read.mapping_quality = 60
        read.cigarstring = '75M'
        read.next_reference_id = 0
        read.next_reference_start = start + length - 75 if mate == 1 else start
        read.template_length = length if mate == 1 else -length
        read.query_qualities = pysam.qualitystring_to_array('I' * 75)
        reads.append(read)
reads.sort(key=lambda read: read.reference_start)
with pysam.AlignmentFile(str(root / 'toy.bam'), 'wb', header=header) as bam:
    for read in reads:
        bam.write(read)
pysam.index(str(root / 'toy.bam'))
(root / 'tmp').mkdir(exist_ok=True)
(root / 'results').mkdir(exist_ok=True)
print(root)
