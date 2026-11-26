# Replay Lab report: tuning scenarios

Generated 2026-11-26 17:30 by `uv run python -m paisa.replay` in 469 s. 30 seeds x 6,000 payments per scenario, the same worlds for every policy. Routing code fingerprint `4f7de824e9f2c433`.

`oracle` knows every gateway's true health at each moment (not each payment's luck). It is the yardstick for regret, not a real router.
`ALL` pools every scenario. Ranges are 95% bootstrap intervals over paired worlds.

## Success rate (%)

Share of payments where money moved.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 84.34 | 84.34 | 84.31 | 84.79 |
| full_outage | 83.93 | 84.12 | 83.90 | 84.58 |
| partial_outage | 84.32 | 84.29 | 84.31 | 84.77 |
| brownout | 84.34 | 84.34 | 84.31 | 84.79 |
| slow_decay | 84.32 | 83.98 | 84.21 | 84.60 |
| flapping | 83.46 | 84.05 | 83.94 | 84.62 |
| timeout_after_charge | 84.34 | 84.34 | 84.34 | 84.79 |
| timeout_in_flight | 84.05 | 83.96 | 83.99 | 84.62 |
| webhook_chaos | 84.34 | 84.34 | 84.31 | 84.79 |
| retry_storm | 83.90 | 84.00 | 83.89 | 84.60 |
| late_success_after_void | 84.33 | 84.35 | 84.34 | 84.78 |
| ALL | 84.15 | 84.19 | 84.17 | 84.70 |

## Regret (payments lost vs oracle, per 6,000)

Lower is better.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 27.0 [23.0, 31.0] | 27.2 [23.0, 31.5] | 28.6 [24.1, 33.4] | 0.0 [0.0, 0.0] |
| full_outage | 39.3 [35.7, 43.0] | 27.7 [24.4, 31.3] | 40.9 [34.1, 48.2] | 0.0 [0.0, 0.0] |
| partial_outage | 27.2 [23.1, 31.2] | 29.1 [24.8, 33.7] | 27.7 [23.6, 32.0] | 0.0 [0.0, 0.0] |
| brownout | 27.0 [23.0, 31.0] | 27.2 [23.0, 31.5] | 28.6 [24.1, 33.4] | 0.0 [0.0, 0.0] |
| slow_decay | 17.0 [13.7, 20.4] | 37.2 [32.1, 42.4] | 23.6 [19.1, 28.4] | 0.0 [0.0, 0.0] |
| flapping | 69.9 [65.8, 73.8] | 34.4 [30.4, 38.5] | 40.9 [34.7, 47.6] | 0.0 [0.0, 0.0] |
| timeout_after_charge | 27.0 [23.0, 31.0] | 27.1 [22.8, 31.7] | 26.9 [23.1, 31.0] | 0.0 [0.0, 0.0] |
| timeout_in_flight | 33.9 [30.2, 37.7] | 39.4 [35.4, 43.3] | 37.7 [30.7, 46.0] | 0.0 [0.0, 0.0] |
| webhook_chaos | 27.0 [23.0, 31.0] | 27.2 [23.0, 31.5] | 28.6 [24.1, 33.4] | 0.0 [0.0, 0.0] |
| retry_storm | 42.4 [38.4, 46.5] | 36.1 [31.8, 40.4] | 42.7 [36.3, 49.7] | 0.0 [0.0, 0.0] |
| late_success_after_void | 27.2 [23.2, 31.4] | 25.9 [22.0, 30.2] | 26.9 [23.1, 30.9] | 0.0 [0.0, 0.0] |
| ALL | 33.2 [31.3, 35.0] | 30.8 [29.4, 32.1] | 32.1 [30.4, 33.9] | 0.0 [0.0, 0.0] |

## Payments saved vs static (per 6,000)

Higher is better.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | +0.0 [+0.0, +0.0] | -0.2 [-5.8, +5.7] | -1.6 [-5.0, +1.7] | +27.0 [+23.0, +31.0] |
| full_outage | +0.0 [+0.0, +0.0] | +11.6 [+7.1, +16.1] | -1.6 [-8.5, +4.9] | +39.3 [+35.7, +43.0] |
| partial_outage | +0.0 [+0.0, +0.0] | -1.9 [-8.0, +4.1] | -0.5 [-3.4, +2.5] | +27.2 [+23.1, +31.2] |
| brownout | +0.0 [+0.0, +0.0] | -0.2 [-5.8, +5.7] | -1.6 [-5.0, +1.7] | +27.0 [+23.0, +31.0] |
| slow_decay | +0.0 [+0.0, +0.0] | -20.2 [-26.2, -14.1] | -6.6 [-10.0, -3.3] | +17.0 [+13.7, +20.4] |
| flapping | +0.0 [+0.0, +0.0] | +35.4 [+29.7, +41.0] | +28.9 [+22.5, +35.1] | +69.9 [+65.8, +73.8] |
| timeout_after_charge | +0.0 [+0.0, +0.0] | -0.1 [-5.9, +5.7] | +0.0 [-2.8, +2.9] | +27.0 [+23.0, +31.0] |
| timeout_in_flight | +0.0 [+0.0, +0.0] | -5.6 [-11.0, +0.3] | -3.8 [-11.0, +2.7] | +33.9 [+30.2, +37.7] |
| webhook_chaos | +0.0 [+0.0, +0.0] | -0.2 [-5.8, +5.7] | -1.6 [-5.0, +1.7] | +27.0 [+23.0, +31.0] |
| retry_storm | +0.0 [+0.0, +0.0] | +6.3 [+0.6, +12.4] | -0.3 [-6.7, +5.9] | +42.4 [+38.4, +46.5] |
| late_success_after_void | +0.0 [+0.0, +0.0] | +1.3 [-4.1, +6.9] | +0.4 [-2.3, +3.2] | +27.2 [+23.2, +31.4] |
| ALL | +0.0 [+0.0, +0.0] | +2.4 [+0.2, +4.6] | +1.1 [-0.6, +2.8] | +33.2 [+31.3, +35.0] |

## Recovery: degraded seconds

Seconds in 10-second windows where first-try health was 5+ points below the same policy's calm world. Lower means faster recovery; calm shows the noise floor.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 0.3 | 0.0 | 0.0 | 0.0 |
| full_outage | 10.0 | 2.7 | 11.0 | 0.0 |
| partial_outage | 0.3 | 0.0 | 0.0 | 0.0 |
| brownout | 0.3 | 0.0 | 0.0 | 0.0 |
| slow_decay | 0.3 | 0.0 | 0.0 | 0.0 |
| flapping | 60.0 | 5.3 | 15.3 | 0.0 |
| timeout_after_charge | 0.3 | 0.0 | 0.3 | 0.0 |
| timeout_in_flight | 99.0 | 2.7 | 16.3 | 52.0 |
| webhook_chaos | 0.3 | 0.0 | 0.0 | 0.0 |
| retry_storm | 62.0 | 3.0 | 15.0 | 0.0 |
| late_success_after_void | 0.3 | 0.0 | 0.0 | 0.0 |
| ALL | 21.2 | 1.2 | 5.3 | 4.7 |

## Fees (rupees per 6,000 payments)

Gateway fees on successful charges.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 504,121 | 493,262 | 504,155 | 527,463 |
| full_outage | 470,782 | 474,121 | 473,251 | 504,898 |
| partial_outage | 504,166 | 497,731 | 505,971 | 527,518 |
| brownout | 504,121 | 493,262 | 504,155 | 527,463 |
| slow_decay | 504,072 | 496,752 | 504,922 | 536,923 |
| flapping | 471,945 | 467,414 | 475,000 | 510,468 |
| timeout_after_charge | 504,120 | 501,936 | 508,338 | 527,462 |
| timeout_in_flight | 479,141 | 463,659 | 475,105 | 508,014 |
| webhook_chaos | 504,121 | 493,262 | 504,155 | 527,463 |
| retry_storm | 470,572 | 464,093 | 473,070 | 506,646 |
| late_success_after_void | 504,079 | 502,323 | 507,737 | 527,450 |
| ALL | 492,840 | 486,165 | 494,169 | 521,070 |

## First-try latency (ms)

Average gateway delay the customer waits for.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 224 | 252 | 231 | 260 |
| full_outage | 221 | 262 | 250 | 259 |
| partial_outage | 224 | 256 | 232 | 260 |
| brownout | 224 | 389 | 281 | 447 |
| slow_decay | 224 | 237 | 228 | 241 |
| flapping | 222 | 264 | 246 | 259 |
| timeout_after_charge | 224 | 260 | 232 | 260 |
| timeout_in_flight | 222 | 267 | 248 | 260 |
| webhook_chaos | 224 | 252 | 231 | 260 |
| retry_storm | 221 | 265 | 249 | 259 |
| late_success_after_void | 224 | 259 | 232 | 260 |
| ALL | 223 | 269 | 242 | 275 |

## Thompson vs rolling + breaker (where the bandit loses)

| Scenario | Change (points) | 95% range | Verdict |
|---|---:|---|---|
| calm | -0.024 | -0.104 to +0.052 | TIE |
| full_outage | -0.221 | -0.358 to -0.097 | LOSES |
| partial_outage | +0.023 | -0.063 to +0.114 | TIE |
| brownout | -0.024 | -0.104 to +0.052 | TIE |
| slow_decay | +0.227 | +0.124 to +0.331 | WINS |
| flapping | -0.108 | -0.222 to +0.003 | TIE |
| timeout_after_charge | +0.003 | -0.081 to +0.088 | TIE |
| timeout_in_flight | +0.029 | -0.109 to +0.143 | TIE |
| webhook_chaos | -0.024 | -0.104 to +0.052 | TIE |
| retry_storm | -0.110 | -0.216 to -0.012 | LOSES |
| late_success_after_void | -0.016 | -0.089 to +0.060 | TIE |
| ALL | -0.022 | -0.053 to +0.010 | TIE |

The bandit is behind rolling on average in: calm, full_outage, brownout, flapping, webhook_chaos, retry_storm, late_success_after_void, ALL. A LOSES verdict means the whole range is below zero.
