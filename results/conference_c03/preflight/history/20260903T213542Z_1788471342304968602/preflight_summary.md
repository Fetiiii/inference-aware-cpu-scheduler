# TASK-C03 AMD cross-vendor preflight

**PRECHECK STATUS: PASS**

- CPU model: AMD Ryzen AI 9 HX 370 w/ Radeon 890M
- Vendor: AuthenticAMD
- Physical/logical: 12/24
- Allowed affinity: 0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23
- Git commit: f9c7ab04193fc5dd582ecf4dcb4d35a5ad6b28e1
- Error: none

## Physical-core evidence

| package | core_id | representative | siblings | highest_perf | nominal_perf | max_freq_khz | capacity | core_type | conflicts |
|---:|---:|---:|---|---:|---:|---:|---:|---|---|
| 0 | 0 | 0 | 0,12 | 196 | 76 | 5157895 | 1024 | None |  |
| 0 | 1 | 1 | 1,13 | 208 | 76 | 5157895 | 1024 | None |  |
| 0 | 2 | 2 | 2,14 | 202 | 76 | 5157895 | 1024 | None |  |
| 0 | 3 | 3 | 3,15 | 208 | 76 | 5157895 | 1024 | None |  |
| 0 | 8 | 4 | 4,16 | 125 | 76 | 3289474 | 1024 | None |  |
| 0 | 9 | 5 | 5,17 | 125 | 76 | 3289474 | 1024 | None |  |
| 0 | 10 | 6 | 6,18 | 125 | 76 | 3289474 | 1024 | None |  |
| 0 | 11 | 7 | 7,19 | 125 | 76 | 3289474 | 1024 | None |  |
| 0 | 12 | 8 | 8,20 | 125 | 76 | 3289474 | 1024 | None |  |
| 0 | 13 | 9 | 9,21 | 125 | 76 | 3289474 | 1024 | None |  |
| 0 | 14 | 10 | 10,22 | 125 | 76 | 3289474 | 1024 | None |  |
| 0 | 15 | 11 | 11,23 | 125 | 76 | 3289474 | 1024 | None |  |

## Frozen selection

- Classification source: highest_perf
- BIG mask: 0,1,2,3
- COMPACT mask: 4,5,6,7,8,9,10,11
- Thread counts: BIG_ONLY=4, ALL_CORES=12
- Diagnostic binary SHA-256: a3d96e1df84b2c56a2c04f8253040fe4c88241c8193640128ff3b539d3cc11e1
- Model SHA-256: 03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8
