# Build Rules (apply to all four projects)

**Why this file exists:** you should be able to open any code file months from now, understand what was built, and decide what you want to change.

## 1. Code comment rule (always)

Every code file must be commented so it can be read like a lesson.

1. **Comment every line of real code wherever possible.** A short block comment is allowed only when several lines form one tiny step and separate comments would say the same thing.
2. **Use simple words.** Write as if explaining to a beginner who knows web development.
3. **Explain technical terms right there.** Keep the real term, because you need it for interviews, and explain it in the same comment.
4. **Say why, not only what.** Explain the reason, and what could go wrong without that line.
5. **Start every file with a summary:** what this file does, why it exists, what uses it, what it uses, and what is safe to change.
6. **Mark useful choices with `CHANGE ME`.** Say what the value controls and what happens if it goes up or down.
7. **Mark danger with `CAREFUL`.** Use it for code that protects money, data, security or correctness.
8. **Keep comments true.** When code changes, its comments change in the same edit.
9. **Applies to every language:** Python, Java, SQL, Terraform, Dockerfiles, YAML, shell scripts and frontend code.

**Exceptions:** blank lines, lone closing brackets, generated files and lock files. Strict JSON cannot hold comments, so explain it in a nearby `README.md`. Comments must never contain passwords, keys or real personal data.

**Example:**

```python
# CAREFUL: money is stored as whole paise (1 rupee = 100 paise), never as a decimal.
# Why: decimals like 0.1 cannot be stored exactly by computers, so totals can drift.
amount_paise = 12_550  # This means Rs 125.50.

# Idempotency key = a unique ID for one payment attempt.
# Why: if the network retries this request, we can spot it and avoid charging twice.
idempotency_key = request.headers["Idempotency-Key"]  # Read the ID sent by the caller.
```

## 2. Learning rule (every phase)

AI-written code is only useful on your resume if you can defend it.

1. **Before building:** write in your own words what this phase should do and how we will know it works.
2. **While building:** read the commented code and ask about anything unclear.
3. **When something breaks:** try to find the cause yourself first, then ask Claude.
4. **Before moving on:** finish a short decision note, one failure you reproduced, and a 60-second explain-back.
5. **For the most important logic:** rewrite one small piece without AI.

## 3. Honesty rule

- Publish only numbers we actually measured, and say how they were measured.
- Call simulated data simulated.
- Never call a project production-grade or regulation-compliant.
- List only tools you used and can explain.

## 4. Every finished project needs

Architecture diagram at the top of the README, one-command local run, automated tests, 3-5 decision notes, known limitations, a recorded demo, and resume bullets using measured results only.

## 5. Alongside the projects

Practise DSA and SQL every week. Many target employers use coding tests before anyone reads your projects.
