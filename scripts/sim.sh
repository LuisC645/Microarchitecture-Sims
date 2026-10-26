# Uso:  source scripts/sim.sh ; sim nombre_carpeta --iq_entries=64 --issue_width=4
# Una simulación = gem5 -> conversor del profesor -> McPAT. Siempre rutas absolutas y --cwd.
sim() { local d=$HOME/gem5/pruebas_lab1/$1; shift; mkdir -p $d
  ( cd $HOME/gem5 && /usr/bin/time -v ./build/ARM/gem5.fast -d $d \
    scripts/CortexA76_scripts_gem5/CortexA76.py -c $HOME/gem5/workloads/mp3_enc/mp3_enc \
    -o "$HOME/gem5/workloads/mp3_enc/mp3enc_testfile.wav mp3enc_outfile.mp3" \
    --cwd=$d "$@" > $d/salida.log 2> $d/time.log )
  ( cd $d && python3 ~/gem5/scripts/McPAT/gem5toMcPAT_cortexA76.py stats.txt config.json ~/gem5/scripts/McPAT/ARM_A76_2.1GHz.xml \
      && ~/mcpat/mcpat -infile config.xml -print_level 1 > potencia.txt ); }
# Tandas de 6 simulaciones en paralelo:
#   export -f sim; printf '%s\n' "iq64 --iq_entries=64" ... | xargs -P 6 -L 1 bash -c 'sim "$@"' _
