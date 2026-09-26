# Stage 3 validation

- **UPPER_BOUND_STAGE_OK**
- **MANDATORY_ROUTES_EVALUATED**
- **ALL_FINISHES_EVALUATED**
- **TOP_K_FEASIBLE_CANDIDATES_READY**
- **LOCAL_CHECKS_COMPLETE**
- **FIXED_ORDER_SOLVER_CERTIFIED**
- **FIRST_HIT_ORDER_CERTIFIED**
- **EXACT_GLOBAL_SOLVER_NOT_STARTED**

## M-boundary

| metric | value |
| --- | --- |
| A outcome | FEASIBLE; duration_s=248121.7; distance_m=2831884.895 |
| B outcome | FEASIBLE; duration_s=230495.6; distance_m=2605677.749 |
| C outcome | FEASIBLE; duration_s=246387.2; distance_m=2728699.705 |
| best found cost | 230495.6 |
| best found nominal order | Warszawa -> Łódź -> Toruń -> Bydgoszcz -> Gdańsk -> Olsztyn -> Białystok -> Lublin -> Rzeszów -> Kielce -> Kraków -> Katowice -> Opole -> Wrocław -> Poznań -> Zielona Góra -> Gorzów Wielkopolski -> Szczecin |
| actual first-hit order | Warszawa -> Łódź -> Toruń -> Bydgoszcz -> Gdańsk -> Olsztyn -> Białystok -> Lublin -> Rzeszów -> Kielce -> Kraków -> Katowice -> Opole -> Wrocław -> Poznań -> Zielona Góra -> Gorzów Wielkopolski -> Szczecin |
| second best found | 246387.2 |
| third best found | 248121.7 |
| best terminal city found | Szczecin |
| second terminal city found | Białystok |
| number of exact fixed-order evaluations | 197 |
| number of unique feasible orders | 3 |
| number of invalid first-hit orders | 0 |
| runtime | 2513.522 |
| peak RAM | 3506720 KiB |

### Evaluation outcomes

| category | count |
| --- | ---: |
| FEASIBLE | 11 |
| ORDER_INVALID_BY_FIRST_HIT | 0 |
| UNREACHABLE | 0 |
| SOLVER_ERROR | 0 |
| TIMEOUT | 186 |
| MISSING_STATE | 0 |
| OTHER_FAILURE | 0 |
| **total** | **197** |

## M-300

| metric | value |
| --- | --- |
| A outcome | FEASIBLE; duration_s=245537.4; distance_m=2823050.856 |
| B outcome | FEASIBLE; duration_s=229843.4; distance_m=2657783.272 |
| C outcome | FEASIBLE; duration_s=251302.1; distance_m=2760130.141 |
| best found cost | 229843.4 |
| best found nominal order | Warszawa -> Łódź -> Toruń -> Bydgoszcz -> Gdańsk -> Olsztyn -> Białystok -> Lublin -> Rzeszów -> Kielce -> Kraków -> Katowice -> Opole -> Wrocław -> Poznań -> Zielona Góra -> Gorzów Wielkopolski -> Szczecin |
| actual first-hit order | Warszawa -> Łódź -> Toruń -> Bydgoszcz -> Gdańsk -> Olsztyn -> Białystok -> Lublin -> Rzeszów -> Kielce -> Kraków -> Katowice -> Opole -> Wrocław -> Poznań -> Zielona Góra -> Gorzów Wielkopolski -> Szczecin |
| second best found | 245537.4 |
| third best found | 251302.1 |
| best terminal city found | Szczecin |
| second terminal city found | Białystok |
| number of exact fixed-order evaluations | 197 |
| number of unique feasible orders | 3 |
| number of invalid first-hit orders | 0 |
| runtime | 2464.009 |
| peak RAM | 3509408 KiB |

### Evaluation outcomes

| category | count |
| --- | ---: |
| FEASIBLE | 11 |
| ORDER_INVALID_BY_FIRST_HIT | 0 |
| UNREACHABLE | 0 |
| SOLVER_ERROR | 0 |
| TIMEOUT | 186 |
| MISSING_STATE | 0 |
| OTHER_FAILURE | 0 |
| **total** | **197** |

## M-500

| metric | value |
| --- | --- |
| A outcome | FEASIBLE; duration_s=239029.1; distance_m=2793456.866 |
| B outcome | FEASIBLE; duration_s=244206.5; distance_m=2738447.54 |
| C outcome | FEASIBLE; duration_s=244663.9; distance_m=2758081.277 |
| best found cost | 239029.1 |
| best found nominal order | Warszawa -> Łódź -> Kielce -> Lublin -> Rzeszów -> Kraków -> Katowice -> Opole -> Wrocław -> Zielona Góra -> Gorzów Wielkopolski -> Szczecin -> Poznań -> Bydgoszcz -> Toruń -> Gdańsk -> Olsztyn -> Białystok |
| actual first-hit order | Warszawa -> Łódź -> Kielce -> Lublin -> Rzeszów -> Kraków -> Katowice -> Opole -> Wrocław -> Zielona Góra -> Gorzów Wielkopolski -> Szczecin -> Poznań -> Bydgoszcz -> Toruń -> Gdańsk -> Olsztyn -> Białystok |
| second best found | 244206.5 |
| third best found | 244663.9 |
| best terminal city found | Białystok |
| second terminal city found | Szczecin |
| number of exact fixed-order evaluations | 197 |
| number of unique feasible orders | 3 |
| number of invalid first-hit orders | 0 |
| runtime | 2576.719 |
| peak RAM | 3511488 KiB |

### Evaluation outcomes

| category | count |
| --- | ---: |
| FEASIBLE | 11 |
| ORDER_INVALID_BY_FIRST_HIT | 0 |
| UNREACHABLE | 0 |
| SOLVER_ERROR | 0 |
| TIMEOUT | 186 |
| MISSING_STATE | 0 |
| OTHER_FAILURE | 0 |
| **total** | **197** |
