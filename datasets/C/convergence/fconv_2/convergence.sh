#!/bin/sh
#SBATCH -n 32
#SBATCH -N 1
#SBATCH -t 7-00:00
#SBATCH -e test.err
#SBATCH -p kozinsky
#SBATCH -o test.out
#SBATCH --mem-per-cpu=1000
#SBATCH --mail-type=ALL
#SBATCH --mail-user=jonathan_vandermause@g.harvard.edu

module load gcc/4.9.3-fasrc01 openmpi/2.1.0-fasrc01
module load python/3.6.3-fasrc01

python force_convergence.py