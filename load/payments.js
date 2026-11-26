// WHAT: k6 load test. Sends payments at rising, fixed rates (a "step" test), 30 seconds per step.
// WHY: shows at which rate latency starts to degrade on this laptop.
// RUN: uv run python load/run.py   (it starts k6 in Docker and writes docs/results/load-report.md)
// SAFE TO CHANGE: RATES and STEP_S. Every payment is fake (simulated gateways, fake token).
import http from "k6/http";

export const RATES = [5, 10, 20, 40, 60, 80, 120]; // CHANGE ME: payments per second, one step each.
const STEP_S = 30; // CHANGE ME: seconds per step.
const BANKS = ["BANK_A", "BANK_B", "BANK_C", "BANK_D", "BANK_E", "BANK_F"]; // Fictional banks from catalog.toml.
const METHODS = ["UPI", "UPI", "UPI", "NETBANKING"]; // Mostly UPI, like Indian traffic.

const scenarios = {}; // One fixed-rate scenario per step, started one after another.
const thresholds = {}; // Thresholds that always pass; they make k6 report each step separately.
RATES.forEach((rate, i) => {
  const name = `r${rate}`;
  scenarios[name] = {
    executor: "constant-arrival-rate", // New payments arrive at this rate even if the API slows down (like real customers).
    rate, timeUnit: "1s", duration: `${STEP_S}s`, startTime: `${i * STEP_S}s`,
    preAllocatedVUs: 50, maxVUs: 400, // Virtual users waiting on slow replies. If they run out, k6 counts "dropped" payments.
  };
  thresholds[`http_req_duration{scenario:${name}}`] = ["max>=0"];
  thresholds[`http_req_failed{scenario:${name}}`] = ["rate>=0"];
  thresholds[`http_reqs{scenario:${name}}`] = ["count>=0"];
  thresholds[`dropped_iterations{scenario:${name}}`] = ["count>=0"];
});

export const options = { scenarios, thresholds, summaryTrendStats: ["med", "p(95)", "p(99)", "max"] };

export default function () {
  const body = JSON.stringify({
    amount_paise: 1000 + Math.floor(Math.random() * 500000), // ₹10 to ₹5,010, in whole paise.
    method: METHODS[Math.floor(Math.random() * METHODS.length)],
    bank: BANKS[Math.floor(Math.random() * BANKS.length)],
    instrument_token: "tok_load_test", // CAREFUL: fake token only.
  });
  http.post(`${__ENV.BASE_URL}/v1/payments`, body, {
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${__ENV.API_KEY}`,
      "Idempotency-Key": `load-${__VU}-${__ITER}-${Date.now()}`, // New key: every request is a new payment.
    },
    timeout: "30s",
  });
}
