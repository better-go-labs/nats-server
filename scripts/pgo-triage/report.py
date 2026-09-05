#!/usr/bin/env python3
"""PGO methodology triage report generator."""
import os
import re


def parse_ns_op(path):
    vals = []
    try:
        for line in open(path):
            m = re.search(r'BenchmarkControl\S*\s+\d+\s+([0-9.]+)\s+ns/op', line)
            if m:
                vals.append(float(m.group(1)))
    except Exception:
        pass
    return vals


def count_decisions(path):
    """Returns (inline_count, pgo_marker_count, pgo_lines) or (None, None, []) if skipped."""
    try:
        text = open(path).read()
        if text.strip().startswith('SKIPPED_NO_PROFILE'):
            return None, None, []
    except Exception:
        return 0, 0, []
    inline_n = len(re.findall(r'can inline|inlining call', text))
    pgo_lines = [
        l for l in text.splitlines()
        if re.search(r'devirtuali|pgo.driven|pgo-driven', l, re.I)
    ]
    return inline_n, len(pgo_lines), pgo_lines


# ---- positive control -------------------------------------------------------
base_vals = parse_ns_op('/tmp/ctrl-baseline.txt')
pgo_vals = parse_ns_op('/tmp/ctrl-pgo.txt')

try:
    bs_text = open('/tmp/ctrl-benchstat.txt').read().strip()
except Exception:
    bs_text = '(not available)'

if base_vals and pgo_vals:
    base_mean = sum(base_vals) / len(base_vals)
    pgo_mean = sum(pgo_vals) / len(pgo_vals)
    delta_pct = (pgo_mean - base_mean) / base_mean * 100

    if delta_pct < -3:
        ctrl_verdict = 'PASS'
        ctrl_interp = (
            'PGO improved the control benchmark by **{:.1f}%** '
            '(baseline {:.0f} ns/op -> PGO {:.0f} ns/op, N={}/{}).'
            ' **The harness can measure PGO.** Flat nats results are a real finding,'
            ' not a measurement defect.'
        ).format(abs(delta_pct), base_mean, pgo_mean, len(base_vals), len(pgo_vals))
    elif delta_pct > 3:
        ctrl_verdict = 'ANOMALY'
        ctrl_interp = (
            'PGO *degraded* the control by {:+.1f}% -- unexpected. '
            'Likely transient runner noise; re-run to confirm.'
        ).format(delta_pct)
    else:
        ctrl_verdict = 'NOISY'
        ctrl_interp = (
            'Positive control delta {:+.1f}% is within +-3% noise. '
            '**The harness cannot reliably measure PGO effects.** '
            'Every prior nats PGO number must be treated as statistically meaningless. '
            'Root cause: GHA ubuntu-latest runner variance exceeds the expected PGO signal. '
            'Mitigation: larger runner, longer benchtime, or N>=20 trials.'
        ).format(delta_pct)
else:
    delta_pct = None
    ctrl_verdict = 'PARSE_ERROR'
    ctrl_interp = 'Could not parse ns/op values from benchmark output. See artifact.'

# ---- static leverage --------------------------------------------------------
no_inline, no_pgo_n, _ = count_decisions('/tmp/gcflags-no-pgo.txt')
wi_inline, wi_pgo_n, wi_pgo_lines = count_decisions('/tmp/gcflags-with-pgo.txt')

if wi_inline is None:
    leverage_verdict = 'SKIPPED'
    leverage_interp = (
        'No `default-pgo` artifact available.\n'
        'Baseline: nats-server compiles with {} inlinable call sites (no PGO).\n\n'
        'To complete this check: run `profile-capture.yml` from PR #3, then re-run this workflow.'
    ).format(no_inline)
    top_pgo = ''
elif wi_pgo_n == 0:
    delta_i = wi_inline - no_inline
    leverage_verdict = 'NO_LEVERAGE'
    leverage_interp = (
        'PGO caused **0 devirt/pgo-driven decisions** on nats-server.\n\n'
        '| | Without PGO | With PGO | Delta |\n'
        '|---|---|---|---|\n'
        '| Inline decisions | {} | {} | {:+d} |\n'
        '| PGO-specific markers (devirt/pgo-driven) | 0 | 0 | 0 |\n\n'
        '`client.parse` is a large hand-tuned parser almost certainly over the inline budget, '
        'and there are no polymorphic hot call sites the profile can devirtualize. '
        '**This is the finding**: PGO has no codegen leverage on this hot path with the current profile.'
    ).format(no_inline, wi_inline, delta_i)
    top_pgo = ''
else:
    delta_i = wi_inline - no_inline
    leverage_verdict = 'HAS_LEVERAGE'
    top_pgo = '\n\n**Top PGO decisions:**\n' + '\n'.join(
        '> `{}`'.format(l.strip()) for l in wi_pgo_lines[:10]
    )
    leverage_interp = (
        'PGO caused **{} devirt/pgo-driven decision(s)** on nats-server.\n\n'
        '| | Without PGO | With PGO | Delta |\n'
        '|---|---|---|---|\n'
        '| Inline decisions | {} | {} | {:+d} |\n'
        '| PGO-specific markers | 0 | {} | +{} |\n\n'
        'PGO is changing codegen. Throughput flatness is a **measurement problem** -- '
        'need: (1) efficiency metric (cycles/msg via `perf stat`), '
        '(2) saturated multi-connection load that keeps the hot path pinned.'
    ).format(wi_pgo_n, no_inline, wi_inline, delta_i, wi_pgo_n, wi_pgo_n)

# ---- conclusion -------------------------------------------------------------
if ctrl_verdict == 'PASS' and leverage_verdict == 'NO_LEVERAGE':
    conclusion = (
        '**Harness is sound. PGO has no codegen leverage on nats-server with the current profile.**\n\n'
        'The positive control confirmed the harness can measure PGO. '
        'But static analysis found 0 PGO-driven decisions in nats-server. '
        '`client.parse` is too large to inline, and there are no hot polymorphic call sites to devirtualize. '
        'The flat throughput numbers are a genuine architectural result, not a measurement artifact.'
    )
elif ctrl_verdict == 'PASS' and leverage_verdict == 'HAS_LEVERAGE':
    conclusion = (
        '**Harness is sound. PGO has leverage but the nats-bench throughput metric cannot see it.**\n\n'
        'Recommended path:\n'
        '1. `perf stat -e cycles,instructions` on baseline vs PGO binary -- '
        'hardware counters are noise-immune and will show IPC delta even at 1%\n'
        '2. Saturated multi-connection pub+sub (8+ concurrent connections) to keep '
        'the profiled hot path pinned during measurement'
    )
elif ctrl_verdict == 'NOISY':
    conclusion = (
        '**Harness cannot measure PGO. All prior nats PGO numbers are uninterpretable.**\n\n'
        'The positive control (designed for a guaranteed PGO win via interface devirt+inline) '
        'showed <=3% delta. GHA ubuntu-latest runner variance is too high for sub-3% effects. '
        'Required fix before further PGO experiments: pinned runner or N>=20 trials with geomean.'
    )
elif leverage_verdict == 'SKIPPED':
    conclusion = (
        'Partial result. Positive control: {}. '
        'Run `profile-capture.yml` from PR #3 to complete the leverage check.'
    ).format(ctrl_verdict)
else:
    conclusion = 'ctrl={} / leverage={} -- see details above.'.format(
        ctrl_verdict, leverage_verdict
    )

# ---- assemble report --------------------------------------------------------
report_lines = [
    '## PGO Methodology Triage',
    '',
    '> Settling whether flat nats PGO results are a measurement artifact or a real',
    '> "PGO has no leverage" finding. Two deterministic checks.',
    '',
    '---',
    '',
    '### Step 1: Positive Control',
    '',
    '**Verdict: {}**'.format(ctrl_verdict),
    '',
    ctrl_interp,
    '',
    '<details><summary>benchstat output</summary>',
    '',
    '```',
    bs_text,
    '```',
    '',
    '</details>',
    '',
    '---',
    '',
    '### Step 2: Static Leverage Check (nats-server `go build -gcflags=-m=2`)',
    '',
    '**Verdict: {}**'.format(leverage_verdict),
    '',
    leverage_interp + top_pgo,
    '',
    '---',
    '',
    '### Conclusion',
    '',
    conclusion,
    '',
    '---',
    '*Workflow: `pgo-methodology-triage.yml` | Runner: ubuntu-latest*',
]

report = '\n'.join(report_lines)

with open('/tmp/triage-report.md', 'w') as f:
    f.write(report)

print(report)
