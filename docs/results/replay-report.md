# Replay Lab report: locked scenarios

Generated 2026-11-26 17:44 by `uv run python -m paisa.replay locked` in 461 s. 30 seeds x 6,000 payments per scenario, the same worlds for every policy. Routing code fingerprint `4f7de824e9f2c433`.

Locked scenarios first opened 2026-11-26 17:44. The routing code has not changed since then.

`oracle` knows every gateway's true health at each moment (not each payment's luck). It is the yardstick for regret, not a real router.
`ALL` pools every scenario. Ranges are 95% bootstrap intervals over paired worlds.

## Success rate (%)

Share of payments where money moved.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 84.34 | 84.34 | 84.31 | 84.79 |
| full_outage | 84.32 | 84.30 | 84.32 | 84.77 |
| partial_outage | 84.34 | 84.22 | 84.27 | 84.75 |
| brownout | 84.34 | 84.34 | 84.31 | 84.79 |
| slow_decay | 84.27 | 83.98 | 84.16 | 84.58 |
| flapping | 84.32 | 84.35 | 84.33 | 84.78 |
| timeout_after_charge | 84.34 | 84.21 | 84.30 | 84.71 |
| timeout_in_flight | 84.34 | 84.15 | 84.32 | 84.64 |
| webhook_chaos | 84.34 | 84.34 | 84.31 | 84.79 |
| retry_storm | 82.50 | 83.37 | 83.09 | 83.72 |
| late_success_after_void | 84.34 | 84.22 | 84.28 | 84.67 |
| ALL | 84.16 | 84.16 | 84.18 | 84.63 |

## Regret (payments lost vs oracle, per 6,000)

Lower is better.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 27.0 [23.0, 31.0] | 27.2 [23.0, 31.5] | 28.6 [24.1, 33.4] | 0.0 [0.0, 0.0] |
| full_outage | 27.2 [23.3, 31.2] | 28.3 [24.6, 32.4] | 27.3 [23.5, 31.6] | 0.0 [0.0, 0.0] |
| partial_outage | 25.0 [21.3, 28.8] | 32.3 [28.1, 36.6] | 28.9 [24.4, 33.8] | 0.0 [0.0, 0.0] |
| brownout | 27.0 [23.0, 31.0] | 27.2 [23.0, 31.5] | 28.6 [24.1, 33.4] | 0.0 [0.0, 0.0] |
| slow_decay | 18.4 [15.9, 21.1] | 36.0 [31.8, 40.5] | 25.0 [19.6, 31.1] | 0.0 [0.0, 0.0] |
| flapping | 27.4 [23.5, 31.5] | 25.8 [21.6, 30.7] | 26.6 [22.7, 30.8] | 0.0 [0.0, 0.0] |
| timeout_after_charge | 22.3 [18.7, 26.0] | 29.9 [26.7, 33.0] | 24.7 [20.5, 29.1] | 0.0 [0.0, 0.0] |
| timeout_in_flight | 18.3 [15.2, 21.4] | 29.2 [24.6, 34.5] | 19.1 [15.6, 23.1] | 0.0 [0.0, 0.0] |
| webhook_chaos | 27.0 [23.0, 31.0] | 27.2 [23.0, 31.5] | 28.6 [24.1, 33.4] | 0.0 [0.0, 0.0] |
| retry_storm | 73.4 [68.1, 78.6] | 21.1 [16.5, 26.0] | 38.2 [32.3, 44.2] | 0.0 [0.0, 0.0] |
| late_success_after_void | 19.7 [16.3, 23.1] | 26.9 [22.5, 31.4] | 23.4 [19.3, 27.6] | 0.0 [0.0, 0.0] |
| ALL | 28.4 [26.5, 30.4] | 28.3 [26.9, 29.6] | 27.2 [25.7, 28.7] | 0.0 [0.0, 0.0] |

## Payments saved vs static (per 6,000)

Higher is better.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | +0.0 [+0.0, +0.0] | -0.2 [-5.8, +5.7] | -1.6 [-5.0, +1.7] | +27.0 [+23.0, +31.0] |
| full_outage | +0.0 [+0.0, +0.0] | -1.1 [-6.1, +4.2] | -0.1 [-3.0, +2.7] | +27.2 [+23.3, +31.2] |
| partial_outage | +0.0 [+0.0, +0.0] | -7.3 [-12.4, -2.0] | -3.9 [-6.9, -1.0] | +25.0 [+21.3, +28.8] |
| brownout | +0.0 [+0.0, +0.0] | -0.2 [-5.8, +5.7] | -1.6 [-5.0, +1.7] | +27.0 [+23.0, +31.0] |
| slow_decay | +0.0 [+0.0, +0.0] | -17.6 [-23.2, -11.7] | -6.6 [-12.4, -1.4] | +18.4 [+15.9, +21.1] |
| flapping | +0.0 [+0.0, +0.0] | +1.6 [-3.7, +6.9] | +0.8 [-2.1, +3.7] | +27.4 [+23.5, +31.5] |
| timeout_after_charge | +0.0 [+0.0, +0.0] | -7.6 [-12.2, -2.8] | -2.4 [-5.0, +0.2] | +22.3 [+18.7, +26.0] |
| timeout_in_flight | +0.0 [+0.0, +0.0] | -10.9 [-17.0, -5.1] | -0.9 [-3.8, +1.8] | +18.3 [+15.2, +21.4] |
| webhook_chaos | +0.0 [+0.0, +0.0] | -0.2 [-5.8, +5.7] | -1.6 [-5.0, +1.7] | +27.0 [+23.0, +31.0] |
| retry_storm | +0.0 [+0.0, +0.0] | +52.3 [+45.1, +59.0] | +35.2 [+28.6, +41.6] | +73.4 [+68.1, +78.6] |
| late_success_after_void | +0.0 [+0.0, +0.0] | -7.3 [-12.4, -2.0] | -3.7 [-6.3, -1.0] | +19.7 [+16.3, +23.1] |
| ALL | +0.0 [+0.0, +0.0] | +0.1 [-2.3, +2.7] | +1.2 [-0.4, +2.9] | +28.4 [+26.5, +30.4] |

## Recovery: degraded seconds

Seconds in 10-second windows where first-try health was 5+ points below the same policy's calm world. Lower means faster recovery; calm shows the noise floor.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 0.3 | 0.0 | 0.0 | 0.0 |
| full_outage | 0.3 | 1.7 | 0.0 | 0.0 |
| partial_outage | 0.3 | 0.7 | 0.0 | 0.0 |
| brownout | 0.3 | 0.0 | 0.0 | 0.0 |
| slow_decay | 0.3 | 0.3 | 0.3 | 0.3 |
| flapping | 0.3 | 1.0 | 0.3 | 0.0 |
| timeout_after_charge | 0.3 | 1.0 | 0.3 | 21.7 |
| timeout_in_flight | 0.3 | 1.3 | 0.7 | 13.7 |
| webhook_chaos | 0.3 | 0.0 | 0.0 | 0.0 |
| retry_storm | 118.3 | 25.3 | 14.7 | 0.3 |
| late_success_after_void | 0.3 | 1.0 | 0.3 | 14.7 |
| ALL | 11.1 | 2.9 | 1.5 | 4.6 |

## Fees (rupees per 6,000 payments)

Gateway fees on successful charges.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 504,121 | 493,262 | 504,155 | 527,463 |
| full_outage | 505,142 | 499,058 | 507,777 | 527,515 |
| partial_outage | 504,119 | 499,863 | 504,725 | 529,409 |
| brownout | 504,121 | 493,262 | 504,155 | 527,463 |
| slow_decay | 502,813 | 465,251 | 493,721 | 496,402 |
| flapping | 504,745 | 502,689 | 509,096 | 527,492 |
| timeout_after_charge | 504,121 | 498,360 | 506,671 | 531,931 |
| timeout_in_flight | 504,091 | 468,025 | 495,277 | 509,354 |
| webhook_chaos | 504,121 | 493,262 | 504,155 | 527,463 |
| retry_storm | 495,450 | 480,403 | 485,639 | 503,569 |
| late_success_after_void | 504,120 | 499,133 | 505,628 | 533,878 |
| ALL | 503,360 | 490,233 | 501,909 | 521,994 |

## First-try latency (ms)

Average gateway delay the customer waits for.

| Scenario | static | rolling | thompson | oracle |
|---|---:|---:|---:|---:|
| calm | 224 | 252 | 231 | 260 |
| full_outage | 224 | 258 | 232 | 260 |
| partial_outage | 224 | 236 | 227 | 257 |
| brownout | 1184 | 752 | 1062 | 860 |
| slow_decay | 224 | 249 | 231 | 252 |
| flapping | 224 | 259 | 233 | 260 |
| timeout_after_charge | 224 | 237 | 228 | 251 |
| timeout_in_flight | 224 | 248 | 229 | 255 |
| webhook_chaos | 224 | 252 | 231 | 260 |
| retry_storm | 286 | 311 | 335 | 312 |
| late_success_after_void | 224 | 234 | 227 | 245 |
| ALL | 317 | 299 | 315 | 316 |

## Thompson vs rolling + breaker (where the bandit loses)

| Scenario | Change (points) | 95% range | Verdict |
|---|---:|---|---|
| calm | -0.024 | -0.104 to +0.052 | TIE |
| full_outage | +0.017 | -0.053 to +0.080 | TIE |
| partial_outage | +0.056 | -0.021 to +0.132 | TIE |
| brownout | -0.024 | -0.104 to +0.052 | TIE |
| slow_decay | +0.183 | +0.072 to +0.293 | WINS |
| flapping | -0.014 | -0.088 to +0.064 | TIE |
| timeout_after_charge | +0.087 | +0.014 to +0.156 | WINS |
| timeout_in_flight | +0.168 | +0.093 to +0.247 | WINS |
| webhook_chaos | -0.024 | -0.104 to +0.052 | TIE |
| retry_storm | -0.285 | -0.382 to -0.189 | LOSES |
| late_success_after_void | +0.059 | -0.019 to +0.136 | TIE |
| ALL | +0.018 | -0.009 to +0.046 | TIE |

The bandit is behind rolling on average in: calm, brownout, flapping, webhook_chaos, retry_storm. A LOSES verdict means the whole range is below zero.
