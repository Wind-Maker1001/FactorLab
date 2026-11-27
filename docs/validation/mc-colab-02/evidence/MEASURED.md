# Local measured results

Hardware: i7-14700KF (single process, BLAS threads=1) and RTX 2070 8GB. Float64 throughout.
Times include exact engine summary, exclude separately recorded external interval analysis. Rows with one seed are performance/capacity evidence, not formal statistical acceptance.

## Matched steady performance

| Study / workload | Paths | Assets | Steps | CPU s | GPU s | Speedup | Cold CPU / GPU s |
|---|---:|---:|---:|---:|---:|---:|---:|
| reliability/gbm-pilot | 100000 | 1 | 252 | 1.003568 | 0.094320 | 10.64 | 2.467 / 3.596 |
| reliability/bootstrap-pilot | 100000 | 1 | 252 | 0.782215 | 0.109847 | 7.12 | 2.297 / 3.156 |
| reliability/gbm-8assets | 100000 | 8 | 252 | 5.869876 | 0.385619 | 15.22 | 8.774 / 3.818 |
| reliability/bootstrap-8assets | 100000 | 8 | 252 | 1.410372 | 0.231428 | 6.09 | 4.039 / 3.462 |
| scale/gbm-1m-b1024 | 1000000 | 1 | 252 | 13.740267 | 0.798496 | 17.21 | 13.340 / 3.153 |
| scale/gbm-1m-b4096 | 1000000 | 1 | 252 | 17.041418 | 0.455560 | 37.41 | 20.656 / 3.894 |
| scale/bootstrap-1m | 1000000 | 1 | 252 | 14.445657 | 0.288307 | 50.11 | 15.331 / 3.843 |
| scale/gbm-8assets | 100000 | 8 | 252 | 8.947333 | 0.360387 | 24.83 | 16.946 / 2.569 |
| scale/gbm-32assets | 100000 | 32 | 252 | 25.737706 | 1.030867 | 24.97 | 29.072 / 3.120 |
| scale/gbm-168steps | 100000 | 1 | 168 | 0.656526 | 0.072005 | 9.12 | 2.126 / 2.313 |
| scale/gbm-8760steps | 100000 | 1 | 8760 | 52.058936 | 3.224975 | 16.14 | 51.512 / 5.467 |
| crossover/gbm-100 | 100 | 1 | 252 | 0.002498 | 0.002821 | 0.89 | 2.597 / 2.280 |
| crossover/gbm-1000 | 1000 | 1 | 252 | 0.014390 | 0.005658 | 2.54 | 2.600 / 3.249 |
| crossover/gbm-10000 | 10000 | 1 | 252 | 0.100222 | 0.012289 | 8.16 | 1.822 / 3.473 |
| crossover/bootstrap-100 | 100 | 1 | 252 | 0.001186 | 0.002839 | 0.42 | 1.781 / 2.400 |
| crossover/bootstrap-1000 | 1000 | 1 | 252 | 0.009860 | 0.003818 | 2.58 | 2.563 / 2.320 |
| crossover/bootstrap-10000 | 10000 | 1 | 252 | 0.088914 | 0.019924 | 4.46 | 2.721 / 3.195 |

## Five-seed risk differences

GPU minus CPU; simultaneous 95% Welch/Bonferroni intervals across nine metrics within each workload. Seed-level Student-t normal approximation; no business equivalence margin.

| Workload | Metric | Difference | Lower | Upper |
|---|---|---:|---:|---:|
| gbm-pilot | var_0.95 | 0.0000772 | -0.0025088 | 0.0026633 |
| gbm-pilot | cvar_0.95 | -0.0000039 | -0.0017462 | 0.0017384 |
| gbm-pilot | var_0.99 | -0.0008060 | -0.0038010 | 0.0021889 |
| gbm-pilot | cvar_0.99 | -0.0006532 | -0.0054356 | 0.0041292 |
| gbm-pilot | loss_probability | 0.0001140 | -0.0027882 | 0.0030162 |
| gbm-pilot | drawdown_breach_probability | 0.0007500 | -0.0017035 | 0.0032035 |
| gbm-pilot | drawdown_0.5 | 0.0001070 | -0.0003389 | 0.0005529 |
| gbm-pilot | drawdown_0.95 | -0.0001194 | -0.0022377 | 0.0019990 |
| gbm-pilot | drawdown_0.99 | -0.0005828 | -0.0028455 | 0.0016799 |
| bootstrap-pilot | var_0.95 | 0.0005616 | -0.0013948 | 0.0025180 |
| bootstrap-pilot | cvar_0.95 | 0.0009091 | -0.0013168 | 0.0031350 |
| bootstrap-pilot | var_0.99 | 0.0011228 | -0.0027809 | 0.0050264 |
| bootstrap-pilot | cvar_0.99 | 0.0022265 | -0.0030805 | 0.0075335 |
| bootstrap-pilot | loss_probability | 0.0007380 | -0.0028011 | 0.0042771 |
| bootstrap-pilot | drawdown_breach_probability | -0.0000240 | -0.0043959 | 0.0043479 |
| bootstrap-pilot | drawdown_0.5 | 0.0001624 | -0.0007482 | 0.0010730 |
| bootstrap-pilot | drawdown_0.95 | 0.0006521 | -0.0011760 | 0.0024802 |
| bootstrap-pilot | drawdown_0.99 | 0.0011116 | -0.0015337 | 0.0037568 |
| gbm-8assets | var_0.95 | -0.0002906 | -0.0021012 | 0.0015200 |
| gbm-8assets | cvar_0.95 | 0.0002570 | -0.0016038 | 0.0021179 |
| gbm-8assets | var_0.99 | 0.0005208 | -0.0016948 | 0.0027363 |
| gbm-8assets | cvar_0.99 | 0.0015568 | -0.0010636 | 0.0041772 |
| gbm-8assets | loss_probability | -0.0007040 | -0.0054287 | 0.0040207 |
| gbm-8assets | drawdown_breach_probability | -0.0000560 | -0.0031328 | 0.0030208 |
| gbm-8assets | drawdown_0.5 | -0.0002335 | -0.0005526 | 0.0000856 |
| gbm-8assets | drawdown_0.95 | -0.0001006 | -0.0012867 | 0.0010856 |
| gbm-8assets | drawdown_0.99 | 0.0002611 | -0.0017042 | 0.0022263 |
| bootstrap-8assets | var_0.95 | 0.0000318 | -0.0015864 | 0.0016501 |
| bootstrap-8assets | cvar_0.95 | 0.0002338 | -0.0018758 | 0.0023435 |
| bootstrap-8assets | var_0.99 | 0.0003015 | -0.0027142 | 0.0033173 |
| bootstrap-8assets | cvar_0.99 | 0.0009310 | -0.0021115 | 0.0039735 |
| bootstrap-8assets | loss_probability | 0.0015440 | -0.0025372 | 0.0056252 |
| bootstrap-8assets | drawdown_breach_probability | 0.0000500 | -0.0020956 | 0.0021956 |
| bootstrap-8assets | drawdown_0.5 | 0.0000537 | -0.0003310 | 0.0004384 |
| bootstrap-8assets | drawdown_0.95 | -0.0001672 | -0.0010957 | 0.0007614 |
| bootstrap-8assets | drawdown_0.99 | 0.0008266 | -0.0014312 | 0.0030845 |

## Seed means and variation

Full per-seed path-level confidence intervals are retained in each study.json (`intervals`).

| Device / workload | Metric | Mean | Seed SD | Min | Max |
|---|---|---:|---:|---:|---:|
| cpu/gbm-pilot | var_0.95 | 0.2579918 | 0.0011186 | 0.2561289 | 0.2587740 |
| cpu/gbm-pilot | cvar_0.95 | 0.3157537 | 0.0007844 | 0.3149274 | 0.3166859 |
| cpu/gbm-pilot | var_0.99 | 0.3528780 | 0.0013893 | 0.3512162 | 0.3547255 |
| cpu/gbm-pilot | cvar_0.99 | 0.3940407 | 0.0022000 | 0.3914014 | 0.3970170 |
| cpu/gbm-pilot | loss_probability | 0.4403020 | 0.0013710 | 0.4379600 | 0.4415400 |
| cpu/gbm-pilot | drawdown_breach_probability | 0.4192700 | 0.0007782 | 0.4180900 | 0.4202200 |
| cpu/gbm-pilot | drawdown_0.5 | 0.1841267 | 0.0002017 | 0.1838392 | 0.1843682 |
| cpu/gbm-pilot | drawdown_0.95 | 0.3378747 | 0.0007371 | 0.3370693 | 0.3386575 |
| cpu/gbm-pilot | drawdown_0.99 | 0.4073814 | 0.0010696 | 0.4059089 | 0.4084446 |
| cpu/bootstrap-pilot | var_0.95 | 0.3580983 | 0.0009210 | 0.3572357 | 0.3594915 |
| cpu/bootstrap-pilot | cvar_0.95 | 0.4068835 | 0.0005418 | 0.4063467 | 0.4076634 |
| cpu/bootstrap-pilot | var_0.99 | 0.4380749 | 0.0009423 | 0.4368954 | 0.4392178 |
| cpu/bootstrap-pilot | cvar_0.99 | 0.4720384 | 0.0006171 | 0.4711973 | 0.4729091 |
| cpu/bootstrap-pilot | loss_probability | 0.7135960 | 0.0014962 | 0.7117000 | 0.7158800 |
| cpu/bootstrap-pilot | drawdown_breach_probability | 0.6645460 | 0.0013712 | 0.6631700 | 0.6660600 |
| cpu/bootstrap-pilot | drawdown_0.5 | 0.2383064 | 0.0003775 | 0.2377147 | 0.2387037 |
| cpu/bootstrap-pilot | drawdown_0.95 | 0.4067680 | 0.0008087 | 0.4059251 | 0.4080269 |
| cpu/bootstrap-pilot | drawdown_0.99 | 0.4720513 | 0.0008881 | 0.4707895 | 0.4731121 |
| cpu/gbm-8assets | var_0.95 | 0.1499182 | 0.0005922 | 0.1492946 | 0.1506670 |
| cpu/gbm-8assets | cvar_0.95 | 0.1920538 | 0.0003571 | 0.1916391 | 0.1925486 |
| cpu/gbm-8assets | var_0.99 | 0.2189512 | 0.0006514 | 0.2181261 | 0.2199409 |
| cpu/gbm-8assets | cvar_0.99 | 0.2500333 | 0.0011077 | 0.2487734 | 0.2517078 |
| cpu/gbm-8assets | loss_probability | 0.3679660 | 0.0016121 | 0.3657600 | 0.3701600 |
| cpu/gbm-8assets | drawdown_breach_probability | 0.0727180 | 0.0003339 | 0.0722400 | 0.0730300 |
| cpu/gbm-8assets | drawdown_0.5 | 0.1120229 | 0.0001351 | 0.1119151 | 0.1122058 |
| cpu/gbm-8assets | drawdown_0.95 | 0.2138794 | 0.0004231 | 0.2135484 | 0.2145726 |
| cpu/gbm-8assets | drawdown_0.99 | 0.2644182 | 0.0004592 | 0.2636624 | 0.2647201 |
| cpu/bootstrap-8assets | var_0.95 | 0.1647443 | 0.0007639 | 0.1634497 | 0.1653521 |
| cpu/bootstrap-8assets | cvar_0.95 | 0.2037120 | 0.0009816 | 0.2020337 | 0.2045192 |
| cpu/bootstrap-8assets | var_0.99 | 0.2284864 | 0.0013161 | 0.2266106 | 0.2296860 |
| cpu/bootstrap-8assets | cvar_0.99 | 0.2574342 | 0.0014223 | 0.2550684 | 0.2586045 |
| cpu/bootstrap-8assets | loss_probability | 0.4499120 | 0.0006114 | 0.4493200 | 0.4508700 |
| cpu/bootstrap-8assets | drawdown_breach_probability | 0.0857120 | 0.0004239 | 0.0853600 | 0.0863800 |
| cpu/bootstrap-8assets | drawdown_0.5 | 0.1175859 | 0.0001591 | 0.1174034 | 0.1178242 |
| cpu/bootstrap-8assets | drawdown_0.95 | 0.2195950 | 0.0003461 | 0.2192669 | 0.2201523 |
| cpu/bootstrap-8assets | drawdown_0.99 | 0.2674384 | 0.0007299 | 0.2665234 | 0.2684587 |
| gpu/gbm-pilot | var_0.95 | 0.2580690 | 0.0010536 | 0.2565221 | 0.2589017 |
| gpu/gbm-pilot | cvar_0.95 | 0.3157498 | 0.0006625 | 0.3147126 | 0.3165636 |
| gpu/gbm-pilot | var_0.99 | 0.3520720 | 0.0010364 | 0.3510889 | 0.3533354 |
| gpu/gbm-pilot | cvar_0.99 | 0.3933875 | 0.0009010 | 0.3921913 | 0.3944468 |
| gpu/gbm-pilot | loss_probability | 0.4404160 | 0.0007763 | 0.4391300 | 0.4410500 |
| gpu/gbm-pilot | drawdown_breach_probability | 0.4200200 | 0.0011534 | 0.4187000 | 0.4215300 |
| gpu/gbm-pilot | drawdown_0.5 | 0.1842337 | 0.0001678 | 0.1839893 | 0.1844180 |
| gpu/gbm-pilot | drawdown_0.95 | 0.3377554 | 0.0009815 | 0.3367292 | 0.3392173 |
| gpu/gbm-pilot | drawdown_0.99 | 0.4067985 | 0.0006306 | 0.4061261 | 0.4077276 |
| gpu/bootstrap-pilot | var_0.95 | 0.3586599 | 0.0004811 | 0.3582002 | 0.3594127 |
| gpu/bootstrap-pilot | cvar_0.95 | 0.4077926 | 0.0010472 | 0.4062638 | 0.4089260 |
| gpu/bootstrap-pilot | var_0.99 | 0.4391976 | 0.0018355 | 0.4365009 | 0.4414953 |
| gpu/bootstrap-pilot | cvar_0.99 | 0.4742649 | 0.0023237 | 0.4713558 | 0.4767113 |
| gpu/bootstrap-pilot | loss_probability | 0.7143340 | 0.0014813 | 0.7120300 | 0.7159400 |
| gpu/bootstrap-pilot | drawdown_breach_probability | 0.6645220 | 0.0020574 | 0.6621900 | 0.6672200 |
| gpu/bootstrap-pilot | drawdown_0.5 | 0.2384688 | 0.0003884 | 0.2379677 | 0.2389223 |
| gpu/bootstrap-pilot | drawdown_0.95 | 0.4074201 | 0.0007198 | 0.4066570 | 0.4082729 |
| gpu/bootstrap-pilot | drawdown_0.99 | 0.4731628 | 0.0012342 | 0.4715049 | 0.4747462 |
| gpu/gbm-8assets | var_0.95 | 0.1496276 | 0.0008482 | 0.1485204 | 0.1508686 |
| gpu/gbm-8assets | cvar_0.95 | 0.1923108 | 0.0008576 | 0.1913904 | 0.1935817 |
| gpu/gbm-8assets | var_0.99 | 0.2194720 | 0.0010465 | 0.2182670 | 0.2206361 |
| gpu/gbm-8assets | cvar_0.99 | 0.2515901 | 0.0010969 | 0.2500163 | 0.2526642 |
| gpu/gbm-8assets | loss_probability | 0.3672620 | 0.0021980 | 0.3655300 | 0.3706900 |
| gpu/gbm-8assets | drawdown_breach_probability | 0.0726620 | 0.0013391 | 0.0715000 | 0.0749200 |
| gpu/gbm-8assets | drawdown_0.5 | 0.1117894 | 0.0001333 | 0.1116459 | 0.1119519 |
| gpu/gbm-8assets | drawdown_0.95 | 0.2137788 | 0.0005462 | 0.2131793 | 0.2146455 |
| gpu/gbm-8assets | drawdown_0.99 | 0.2646792 | 0.0009220 | 0.2634038 | 0.2659237 |
| gpu/bootstrap-8assets | var_0.95 | 0.1647761 | 0.0004836 | 0.1642942 | 0.1655150 |
| gpu/bootstrap-8assets | cvar_0.95 | 0.2039459 | 0.0007193 | 0.2029877 | 0.2050068 |
| gpu/bootstrap-8assets | var_0.99 | 0.2287879 | 0.0012135 | 0.2277256 | 0.2306776 |
| gpu/bootstrap-8assets | cvar_0.99 | 0.2583652 | 0.0006787 | 0.2577267 | 0.2594916 |
| gpu/bootstrap-8assets | loss_probability | 0.4514560 | 0.0018321 | 0.4484600 | 0.4532500 |
| gpu/bootstrap-8assets | drawdown_breach_probability | 0.0857620 | 0.0009918 | 0.0845500 | 0.0870000 |
| gpu/bootstrap-8assets | drawdown_0.5 | 0.1176396 | 0.0001644 | 0.1174166 | 0.1178246 |
| gpu/bootstrap-8assets | drawdown_0.95 | 0.2194278 | 0.0004215 | 0.2190445 | 0.2200315 |
| gpu/bootstrap-8assets | drawdown_0.99 | 0.2682651 | 0.0010592 | 0.2670852 | 0.2694023 |

## Resource and analysis cost

| Study | Sampled worker RSS peak MiB | Whole-device peak MiB | External statistics total s | Study wall s |
|---|---:|---:|---:|---:|
| cpu-crossover | 165.0 | 0.0 | 0.023 | 16.562 |
| cpu-parallel | 167.2 | 0.0 | 0.135 | 36.200 |
| cpu-pilot | 165.3 | 0.0 | 0.021 | 8.728 |
| cpu-profile | 166.3 | 0.0 | 0.026 | 12.041 |
| cpu-reliability | 188.1 | 0.0 | 5.433 | 181.177 |
| cpu-scale | 281.3 | 0.0 | 0.563 | 416.474 |
| gpu-crossover | 549.9 | 2490.4 | 0.019 | 19.016 |
| gpu-large | 3639.3 | 2392.1 | 14.141 | 128.022 |
| gpu-pilot | 579.2 | 2054.9 | 0.024 | 6.862 |
| gpu-profile | 552.3 | 2565.9 | 0.031 | 7.489 |
| gpu-reliability | 556.3 | 2466.7 | 7.126 | 91.353 |
| gpu-scale | 581.5 | 2536.7 | 0.527 | 39.441 |

## Synchronized phases (profiling only)

| Device / workload | Phase | Warm median s |
|---|---|---:|
| cpu-profile/bootstrap-pilot | input_preparation | 0.0004391 |
| cpu-profile/bootstrap-pilot | device_initialization | 0.0000245 |
| cpu-profile/bootstrap-pilot | output_allocation | 0.0000153 |
| cpu-profile/bootstrap-pilot | input_views | 0.0000050 |
| cpu-profile/bootstrap-pilot | rng_and_kernel_setup | 0.0001565 |
| cpu-profile/bootstrap-pilot | generation_and_reduction | 0.8391708 |
| cpu-profile/bootstrap-pilot | simulation_cleanup | 0.0000774 |
| cpu-profile/bootstrap-pilot | result_statistics | 0.0074056 |
| cpu-profile/gbm-pilot | input_preparation | 0.0003379 |
| cpu-profile/gbm-pilot | device_initialization | 0.0000288 |
| cpu-profile/gbm-pilot | output_allocation | 0.0000234 |
| cpu-profile/gbm-pilot | input_views | 0.0000062 |
| cpu-profile/gbm-pilot | rng_and_kernel_setup | 0.0001829 |
| cpu-profile/gbm-pilot | generation_and_reduction | 1.3180869 |
| cpu-profile/gbm-pilot | simulation_cleanup | 0.0001472 |
| cpu-profile/gbm-pilot | result_statistics | 0.0107499 |
| gpu-profile/bootstrap-pilot | input_preparation | 0.0003648 |
| gpu-profile/bootstrap-pilot | device_initialization | 0.0017159 |
| gpu-profile/bootstrap-pilot | output_allocation | 0.0000391 |
| gpu-profile/bootstrap-pilot | host_to_device | 0.0003463 |
| gpu-profile/bootstrap-pilot | rng_and_kernel_setup | 0.0003449 |
| gpu-profile/bootstrap-pilot | generation_and_reduction | 0.1029190 |
| gpu-profile/bootstrap-pilot | device_to_host | 0.0090316 |
| gpu-profile/bootstrap-pilot | simulation_cleanup | 0.0015430 |
| gpu-profile/bootstrap-pilot | result_statistics | 0.0102631 |
| gpu-profile/gbm-pilot | input_preparation | 0.0003133 |
| gpu-profile/gbm-pilot | device_initialization | 0.0018284 |
| gpu-profile/gbm-pilot | output_allocation | 0.0000391 |
| gpu-profile/gbm-pilot | host_to_device | 0.0003388 |
| gpu-profile/gbm-pilot | rng_and_kernel_setup | 0.0002550 |
| gpu-profile/gbm-pilot | generation_and_reduction | 0.0684357 |
| gpu-profile/gbm-pilot | device_to_host | 0.0080454 |
| gpu-profile/gbm-pilot | simulation_cleanup | 0.0004566 |
| gpu-profile/gbm-pilot | result_statistics | 0.0101980 |

## CPU multiprocess aggregate

Independent complete jobs; includes startup and repeated result analysis, not just simulation kernel time. Child-inclusive RSS is sampled and CPU seconds cover measured runner regions.

| Wave | Jobs | Completed paths | Wall s | Aggregate paths/s | Child-inclusive RSS MiB |
|---|---:|---:|---:|---:|---:|
| wave-bootstrap-pilot-0 | 2 | 600000 | 5.898 | 101737.6 | 497.1 |
| wave-bootstrap-pilot-2 | 2 | 600000 | 6.229 | 96319.9 | 497.2 |
| wave-bootstrap-pilot-4 | 1 | 300000 | 5.267 | 56956.5 | 327.7 |
| wave-gbm-pilot-0 | 2 | 600000 | 6.273 | 95646.3 | 494.8 |
| wave-gbm-pilot-2 | 2 | 600000 | 7.014 | 85539.1 | 495.7 |
| wave-gbm-pilot-4 | 1 | 300000 | 5.500 | 54547.6 | 329.4 |

## Large-path capacity (one seed, no path bootstrap)

| Paths | Repeat | Runner s | Paths/s | External exact metrics s | RSS MiB |
|---:|---:|---:|---:|---:|---:|
| 10000000 | 0 | 5.260 | 1901311.6 | 0.634 | 854.4 |
| 10000000 | 1 | 4.753 | 2104071.8 | 0.599 | 855.4 |
| 100000000 | 0 | 46.898 | 2132275.3 | 6.150 | 3639.1 |
| 100000000 | 1 | 49.205 | 2032318.6 | 6.758 | 3639.3 |
