# Challenge scorecard baseline

Main at 1cbb1a9, `python tools/challenge_bench.py run out.json` (45 cases, Windows OCR, scratch key, this PC).
Columns are explained in the docstring of `tools/challenge_bench.py` and in CLAUDE.md 12d. Compare runs of the SAME
selection of cases only (the fakes of a case depend on the other cases in the folder); the same selection is deterministic.

What the baseline shows (first reading, not yet analysed):
- Not found at all: `geom:tilt12` (0 of 5), `deco:stamp`, `deco:strike`; partly: `geom:tilt18` 60 %, `geom:bend_strong` 60 %,
  `geom:bend_tilt` 80 %, `font:lucida` 80 %.
- Artefacts in the background (bg95 / bg_excess): `deco:underline` (bg95 30.6), `deco:frame`, `deco:cell`, `deco:double`,
  `deco:thick`, `deco:hl_*` (7-9), `bg:guilloche`, `bg:lined`; `deco:hl_yellow_part`: the highlight is painted wider than the original.
- Wrong ink: `deco:circled` (ink_rel 3.0, the pen circle is damaged), `geom:tilt18`, `geom:bend_strong`, `font:rockwell` and
  `tone:textlayer` (mass 0.75: too thin), `geom:persp` and `tone:halo` (dark 30-40 levels).

```
group                        n     found bg_excess      bg95  bg_worst   ink_rel      mass      dark    height        dy        dx      coll
ALL                        225       94%      0.19       3.7       4.8      0.55      1.03       5.2      1.04       0.2       2.7       0.0
family:bg                   45      100%      0.12       3.3       3.4      0.49      1.03       4.4      1.04       0.2       1.9       0.0
family:deco                 25       92%      0.71       6.9       4.8      0.57      1.06       3.6      1.04       0.1       2.2       0.0
family:font                 65       98%      0.17       3.7       2.2      0.61      1.07       5.6      1.00       0.2       4.4       0.0
family:geom                 50       78%      0.08       3.4       3.2      0.71      0.92      17.2      1.03       0.5       4.1       0.0
family:tone                 40      100%      0.43       3.9       2.7      0.47      1.00      10.8      1.00       0.1       2.1       0.0
split:dev                  140       94%      0.19       3.5       4.8      0.52      1.03       4.9      1.04       0.2       2.7       0.0
split:held                  85       93%      0.21       4.0       3.4      0.67      1.03       6.6      1.04       0.2       2.5       0.0
bg:flat                      5      100%     -0.20       2.5       1.4      0.66      1.02       4.6      1.04       0.2       1.9       0.0
bg:grad_h                    5      100%      0.55       3.6       2.1      0.36      1.04       4.2      1.00       0.2       3.0       0.0
bg:grad_v                    5      100%      0.23       3.1       2.1      0.54      1.03       4.5      1.04       0.1       1.9       0.0
bg:guilloche                 5      100%      0.78       5.4       3.4      0.49      1.03       4.6      1.04       0.2       1.5       0.0
bg:lined                     5      100%      0.45       5.4       2.5      0.36      1.03       3.7      1.04       0.0       2.1       0.0
bg:paper                     5      100%      0.02       3.4       2.0      0.47      1.03       4.0      1.04       0.2       3.4       0.0
bg:vignette                  5      100%     -0.16       2.8       1.6      0.46      1.02       3.9      1.04       0.2       0.9       0.0
bg:zebra                     5      100%      0.01       2.7       1.5      0.35      1.02       4.2      1.04       0.2       1.8       0.0
bg:zebra_edge                5      100%      0.12       3.1       3.2      0.68      1.01       7.1      1.04       0.2       1.7       0.0
deco:cell                    1      100%      2.09       7.8       3.9      0.62      1.05       4.9      1.00       0.1       1.2       8.7
deco:circled                 1      100%      0.71       8.3       3.2      3.01      3.77     111.0      2.21       2.3      72.2       8.7
deco:crease                  2      100%      0.45       5.1       2.4      0.35      1.04       3.6      1.04       0.3       1.5       4.4
deco:descender               1      100%      0.33       3.9       1.9      0.96      1.00       2.6      1.30       1.5       9.9       0.0
deco:dotted                  1      100%      0.60       5.9       2.2      0.57      1.09       3.6      1.04       0.1       0.6       0.0
deco:double                  1      100%      1.33       8.1       3.0      0.57      1.06       3.2      1.04       0.1       0.4       0.0
deco:frame                   1      100%      2.31       7.8       4.1      0.43      1.05       5.9      1.04       0.1       2.6       8.7
deco:hl_green                1      100%      0.94       6.9       3.6      0.57      1.08       0.2      1.04       0.2       2.2       0.0
deco:hl_green_opaque         1      100%      0.44       6.8       3.2      0.48      1.10       0.3      1.04       0.0       2.0       0.0
deco:hl_pink                 2      100%      0.66       5.0       2.7      0.37      1.06       5.5      1.02       0.1       1.8       0.0
deco:hl_yellow               1      100%      0.90       7.4       3.4      0.20      1.07       1.9      1.10       0.0       1.5       0.0
deco:hl_yellow_opaque        1      100%      0.94       8.8       4.1      0.32      1.09       3.1      1.00       0.1       0.0       0.0
deco:hl_yellow_part          2      100%      0.78       8.2       3.2      0.79      1.37       4.8      1.13       0.0      20.0       0.0
deco:specks                  3      100%      0.21       4.5       2.0      0.70      1.02       4.8      1.04       0.1       5.4       0.0
deco:stamp                   1        0%         -         -       0.0         -         -         -         -         -         -         -
deco:strike                  1        0%         -         -       0.0         -         -         -         -         -         -         -
deco:thick                   1      100%      1.33       8.4       3.0      0.80      1.01       3.2      1.04       0.3       1.1       0.0
deco:tintbox                 2      100%      0.56       4.4       2.5      0.58      1.05       3.0      1.05       0.2       5.5       0.0
deco:underline               1      100%      3.17      30.6       4.8      0.67      0.89      21.2      1.09       0.2       5.9       0.0
font:arial                 155       92%      0.23       3.8       4.8      0.53      1.03       4.9      1.04       0.2       2.2       0.0
font:bahnschrift             5      100%      0.24       3.7       2.0      0.76      0.96      18.4      1.00       0.2       8.5       0.0
font:calibri                 5      100%      0.18       3.8       2.0      0.45      1.05       3.6      1.00       0.2       1.6       0.0
font:candara                 5      100%      0.19       3.7       2.2      0.65      1.12       3.1      1.05       0.2       4.6       0.0
font:consolas                5      100%      0.22       4.3       1.9      0.20      1.06       2.0      1.05       0.1       0.7       0.0
font:constantia              5      100%      0.43       5.0       2.2      0.73      1.14       3.5      1.00       0.2       5.7       0.0
font:corbel                  5      100%      0.16       3.6       1.9      0.69      1.09       2.3      1.05       0.2       6.2       0.0
font:franklin                5      100%      0.13       3.4       1.9      0.76      0.91       8.4      0.96       0.2       9.4       0.0
font:gillsans                5      100%      0.16       3.7       1.9      0.55      1.06       5.5      1.04       0.1       2.6       0.0
font:lucida                  5       80%      0.06       3.6       1.8      0.92      1.25       9.4      1.04       0.1      15.4       0.0
font:palatino                5      100%      0.17       3.5       2.2      0.65      1.01       8.4      1.04       0.2       4.2       0.0
font:rockwell               10      100%      0.48       3.2       2.1      0.66      0.75      45.7      1.00       0.1       5.2       0.0
font:sylfaen                 5      100%      0.21       3.8       2.0      0.56      1.07      11.2      1.00       0.1       5.8       0.0
font:times                   5      100%      0.07       3.4       1.8      0.46      1.05       2.5      1.00       0.1       1.7       0.0
geom:bend_mild               5      100%      0.15       3.7       2.1      0.50      1.13       1.6      1.04       0.2       2.1       0.0
geom:bend_strong             5       60%      0.93       8.5       3.2      1.03      1.12      22.8      0.96       2.2      13.3       0.0
geom:bend_tilt               5       80%      0.09       3.3       1.7      0.77      0.90      16.9      1.02       1.2       6.9       0.0
geom:persp                   5      100%      0.05       3.4       3.0      0.67      0.81      39.9      1.04       0.5       3.4       0.0
geom:tilt1                   5      100%      0.23       4.1       2.0      0.77      1.06       1.7      0.96       0.1       3.8       0.0
geom:tilt12                  5        0%         -         -       0.0         -         -         -         -         -         -         -
geom:tilt18                  5       60%      0.59       6.2       2.5      1.58      1.38      31.5      0.46       3.7      12.7      36.2
geom:tilt3                   5      100%      0.05       3.1       1.6      0.61      0.87      20.4      1.06       0.4       6.0       0.0
geom:tilt6                   5       80%     -0.01       3.2       1.6      0.36      0.83      22.2      1.04       0.3       1.3       0.0
geom:wave                    5      100%      0.08       3.4       1.9      0.73      0.90      16.3      1.03       0.6       3.0       0.0
tone:binary                  5      100%     -0.15       0.5       0.3      0.59      1.04       7.9      1.00       0.2       2.0       0.0
tone:blue                    5      100%      0.04       3.9       2.1      0.33      1.04       1.5      1.04       0.1       1.2       0.0
tone:boost                   5      100%      0.56       3.1       1.8      0.46      1.02      14.1      1.04       0.1       2.9       0.0
tone:coarse                  5      100%      0.25       4.8       2.7      0.47      1.01       2.1      1.00       0.2       1.7       0.0
tone:dark                    5      100%      0.96       4.4       2.6      0.26      0.94      21.8      1.04       0.1       2.2       0.0
tone:faded                   5      100%      0.82       3.9       2.1      0.64      1.07       1.1      1.00       0.1       4.1       0.0
tone:halo                    5      100%     -0.18       4.5       2.0      0.74      0.90      31.1      1.04       0.2       1.7       0.0
tone:textlayer               5      100%      0.56       3.2       1.9      0.78      0.74      47.7      0.95       0.1       5.2       0.0
```


## Whole-page documents (`doc_`, `--only doc_`, 6 pages, 94 values; `det` column added to the bench afterwards)

Bank statement, insurance letter and lease, each laid out with several fonts, bold / italic runs, a zebra table,
highlighter (yellow / blue / magenta over values and ordinary words), then scanned: tilt, bend, wave, perspective.
Much harder than the five-row cases: 70 % found, collateral 20 per 10 000.

- Highlighter: of the values under a highlighter, detection saw 14 of 16 (the config has them) but only 6 were painted at
  their place: magenta 0 of 4, blue 2 of 6, yellow 4 of 6. The text under a highlighter is not read (or not at its place) by the OCR. The
  rest: 60 of 74 plain values found. Missing plain ones are mostly the second occurrence of a value on a bent or tilted page.
- Two reference kinds (`police n° AUT-9035127`, `HAB-4471902` in Courier / Book Antiqua) are not detected at all (detection, not rendering;
  they stay in the set: a leak the second check should offer).
- `coll` counts changes away from the marked values: the run also replaces words the page does not mark as values (a town in
  'Delhaize Namur', 'Liège, le ...'): over-redaction, accepted by design, so only compare `coll` between runs.
- Bad backgrounds: `doc_releve_bend` bg_excess 7.1 (bg95 25), the tinted table rows under a bend; `deco:hl_yellow` bg95 151,
  `hl_blue` 91: the patch of a value on a highlighter is the wrong colour.

```
group                        n     found bg_excess      bg95  bg_worst   ink_rel      mass      dark    height        dy        dx      coll
ALL                         94       70%      0.87       5.8      48.1      0.92      1.24       8.2      1.04       0.9       4.0      20.6
family:doc                  94       70%      0.87       5.8      48.1      0.92      1.24       8.2      1.04       0.9       4.0      20.6
split:dev                   47       74%      0.71       5.8      48.1      0.79      1.25       6.9      1.04       0.6       3.7      20.6
split:held                  47       66%      0.94       5.9      29.2      1.05      1.19      12.9      1.05       1.1       5.3      19.2
case:doc_assurance_bendtilt  16       50%      1.50       6.9      14.1      1.37      1.28      17.1      1.04       1.2       3.6       4.5
case:doc_assurance_wave     16       75%      0.81       6.2      13.3      0.79      1.24       6.8      1.00       0.2       3.5       7.3
case:doc_contrat_bend       16       75%      0.76       5.5      18.4      1.00      1.34       9.3      1.11       1.6       4.0      46.0
case:doc_contrat_persp      16       88%      0.59       4.8      11.6      0.92      1.23      13.7      1.05       1.2       4.9      34.4
case:doc_releve_bend        15       60%      7.09      25.5      29.2      1.09      1.06       6.5      1.05       1.1       6.8      19.2
case:doc_releve_tilt        15       73%      0.56       5.8      48.1      0.45      1.25       6.5      1.09       0.5       1.4      20.6
deco:hl_blue                 6       33%      9.09      91.1      14.1      2.03      0.99      29.8      0.94       3.6       3.4      11.8
deco:hl_magenta              4        0%         -         -       0.0         -         -         -         -         -         -         -
deco:hl_yellow               6       67%     19.63     151.4      48.1      1.47      0.84      28.9      0.80       4.4       8.1      27.5
font:arial                  30       67%      0.87       5.9      48.1      0.72      1.22       6.5      1.08       0.7       5.5      20.6
font:bookman                 2        0%         -         -       0.0         -         -         -         -         -         -         -
font:calibri                28       93%      0.72       5.5      18.4      0.98      1.28      11.3      1.08       1.3       4.3      34.4
font:courier                 4        0%         -         -       0.0         -         -         -         -         -         -         -
font:garamond               30       67%      1.03       6.2      14.1      0.97      1.25       7.5      1.00       0.6       3.5       7.3
geom:bend+rotate            31       68%      0.87       5.9      29.2      1.02      1.21       8.5      1.09       1.2       6.0      46.0
geom:persp+wave             16       88%      0.59       4.8      11.6      0.92      1.23      13.7      1.05       1.2       4.9      34.4
geom:rotate                 15       73%      0.56       5.8      48.1      0.45      1.25       6.5      1.09       0.5       1.4      20.6
geom:rotate+bend            16       50%      1.50       6.9      14.1      1.37      1.28      17.1      1.04       1.2       3.6       4.5
geom:wave+rotate            16       75%      0.81       6.2      13.3      0.79      1.24       6.8      1.00       0.2       3.5       7.3
```
