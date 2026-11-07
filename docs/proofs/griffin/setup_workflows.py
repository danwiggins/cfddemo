"""Configure pinned Griffin Snakefiles against the generated synthetic toy."""

import argparse
from pathlib import Path
import shutil
import tempfile

import yaml


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base-dir', type=Path,
                    default=Path(tempfile.gettempdir()) / 'cfdna-griffin')
parser.add_argument('--upstream', type=Path,
                    help='clean checkout of the pinned Griffin commit; defaults to base-dir/upstream-posix')
args = parser.parse_args()
base = args.base_dir.resolve()
upstream = args.upstream.resolve() if args.upstream else base / 'upstream-posix'
toy = base / 'proof'
work = base / 'snakemake-proof'
work.mkdir(exist_ok=True)
compat = base / 'compat'
compat.mkdir(exist_ok=True)
shutil.copy2(Path(__file__).resolve().parents[3] / 'traceback_lab' / 'adapters'
             / 'griffin_compat' / 'sitecustomize.py', compat / 'sitecustomize.py')
(toy / 'exclude.bed').write_text('chr1\t0\t1\n')
scripts = upstream / 'scripts'

for workflow in ('griffin_GC_and_mappability_correction', 'griffin_nucleosome_profiling'):
    target = work / workflow
    if target.exists():
        raise FileExistsError(target)
    shutil.copytree(upstream / 'snakemakes' / workflow, target)
    cluster_path = target / 'config' / 'cluster_slurm.yaml'
    cluster = yaml.safe_load(cluster_path.read_text())
    for rule in cluster.values():
        if isinstance(rule, dict) and 'ncpus' in rule:
            rule['ncpus'] = 1
    cluster_path.write_text(yaml.safe_dump(cluster, sort_keys=False))
    config_path = target / 'config' / 'config.yaml'
    config = yaml.safe_load(config_path.read_text())
    config.update({
        'griffin_scripts_dir': str(scripts),
        'reference_genome': str(toy / 'toy.fa'),
        'chrom_sizes': str(toy / 'chrom.sizes'),
        'chrom_sizes_path': str(toy / 'chrom.sizes'),
        'mappability_bw': str(toy / 'mappability.bw'),
        'mappable_regions': str(toy / 'mappable.bed'),
        'genome_GC_frequency': str(toy / 'GC_frequency'),
        'encode_exclude': str(toy / 'exclude.bed'),
        'centromeres': str(toy / 'exclude.bed'),
        'gaps': str(toy / 'exclude.bed'),
        'patches': str(toy / 'exclude.bed'),
        'alternative_haplotypes': str(toy / 'exclude.bed'),
        'out_dir': 'results',
    })
    if workflow.startswith('griffin_GC_'):
        config.update({'mappability_correction': False, 'GC_bias_size_range': '140 180'})
        (target / 'config' / 'samples.yaml').write_text(yaml.safe_dump({'samples': {'Toy': str(toy / 'toy.bam')}}))
    else:
        config.update({'tmp_dir': 'tmp', 'results_dir': 'results', 'chroms': 'chr1',
                       'size_range': '100 200', 'mappability_correction': False})
        (target / 'config' / 'sites.yaml').write_text(yaml.safe_dump({'site_lists': {'ToySite': str(toy / 'sites.tsv')}}))
        bias = work / 'griffin_GC_and_mappability_correction' / 'results' / 'GC_bias' / 'Toy.GC_bias.txt'
        (target / 'config' / 'samples.GC.yaml').write_text(yaml.safe_dump({
            'samples': {'Toy': {'bam': str(toy / 'toy.bam'), 'GC_bias': str(bias)}}}))
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
print(work)
