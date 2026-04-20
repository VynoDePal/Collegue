# Memory soak — issue #207

Tool probed: `secret_scan` (constant, paced).
Total duration: `183.3 s`.
Requests sent: `60` (errors: `0`).

## Memory envelope
- min: **241.2 MiB**
- max: **241.7 MiB**
- mean: **241.5 MiB**
- drift: **0.21 %** (max-min / min)
- stabilité (±15 %): **✅**

## Timeseries

| t (s) | req | mem (MiB) | cpu (%) |
|---:|---:|---:|---:|
| 0 | 0 | 241.2 | 6.0 |
| 32 | 11 | 241.5 | 4.6 |
| 62 | 21 | 241.4 | 5.9 |
| 92 | 31 | 241.7 | 5.3 |
| 122 | 41 | 241.7 | 5.5 |
| 152 | 51 | 241.5 | 5.5 |
| 183 | 60 | 241.5 | 4.5 |