# python-pro -- a strong reference agent for participant-agent-protocol-v4

[中文说明见 README.zh.md](README.zh.md)

A high-scoring example agent for the GOSIM survey26 telescope-survey challenge, written to show how far a
careful planner plus a few well-placed model calls can go. Standard library only. It uses only what the
protocol gives an agent at run time (catalogue, public score configuration, bulletins, forecasts and its
own results) and the public engine code in the starter kit. It never reads card files.

## Results (local engine, Kimi `k3`)

| Card | Starter-kit baseline | python example | **python-pro** |
|---|---:|---:|---:|
| practice α | 3,617 | -- | **7,127 / 7,151** |
| practice β | 4,122 | -- | **6,106 / 6,344** |
| practice γ | 4,030 | -- | **6,749 / 6,814** |
| practice δ | 3,047 | -- | **6,474 / 6,268** |
| local L1 | -- | 4,459 | **6,225** |
| local L2 | -- | -- | **6,411** |

Practice cards: two runs each with the search level pinned (`PRO_FIXED_LEVEL=0`) so a busy laptop does not
change the plan; L1/L2: one run each with `run_local.py` and the normal 900 s clock. With the normal clock
practice α scored 7,198 and finished in 605 s on a heavily loaded machine. Runs differ mostly through the
timing of instrument-fault reports. For scale: on 2026-10-03 the best single-card practice scores on the
platform were about 7,400-7,900, so this agent is a strong starting point, not the ceiling.

## Layout

```
agent.py          entry point: protocol loop, pacing, instrument-fault reporting, model stages wiring
planner.py        one search for pointing + fibres + duration + program; learning from results
skymath.py        public sky maths: sidereal time, alt/az, gnomonic projection, fibre grid, Moon
advisor.py        the model stages: night plan, fault review, paid-report confirmation
llm_client.py     OpenAI-compatible chat client running calls on background threads
observer.project.json   platform manifest (python3 -u agent.py)
pack_agent.py     zip this folder for upload (.env is never packed)
.env.example      copy to .env and set an API key for local runs
```

## Where the model is used (two stages every night, plus one before a paid report)

At the start of every night the agent starts two calls in the background:

1. **Night plan** (natural-language understanding, plan adaptation). The model reads tonight's forecast
   and the current bulletin and answers `{"bad_night", "avoid_directions"}`. `bad_night` decides whether
   the planner spends one-hour exposures on faint required targets tonight or saves them for a better
   night; `avoid_directions` down-weights the named sectors all night. Only sectors that a notice actually
   names are accepted.
2. **Fault review** (data parsing, action decision). The model reads the agent's own hour-by-hour quality
   table (E = quality level / band level, the sky scale, the usual clear-sky scale, report history) and
   answers `{"fault_likely": 0..1}`. Tonight's paid fault reports follow it: with a low
   value the agent does not pay for a probe on the hour-by-hour E signal alone (only the three-low-nights
   persistence rule may still report); otherwise the rule's paid probes go ahead, after the model's own
   confirmation below. (`PRO_MODEL_FREE_PROBE=1` also lets a high value spend a free probe when the sky scale
   drops; it is off because on the practice cards it spent free probes on unannounced weather.)

Before any **paid** fault report (one that would cost 150 if wrong) the model sees the evidence and may
veto it.

Calls never stall the survey: they run on background threads, and a night start waits for the answers
only as long as the remaining wall clock allows (about 20 s at most on a 38-night card; less on long
cards, where late answers are applied when they arrive). Any failure, timeout or invalid answer leaves the
rule-based value in place for that night.

## What makes the planner strong

1. **One search for pointing, fibres, duration and program**, maximising `gain - lambda * T`. Gain is
   incremental over each target's best exposure so far (only the best exposure counts); lambda is a price
   for telescope time that follows the recent best gain rate and is scaled by how tight the season is.
2. **Where to point**: fields centred on the 12 most valuable targets (each placed on all 16 fibres), plus
   the 20 densest patches of remaining science, then refined by small pointing shifts.
3. **Required targets** get a bonus weighted by the probability of reaching factor 0.5, and wait for a sky
   close to the best they will ever get (and for a night not forecast bad).
4. **Programs from saturated hits.** A saturated hit shows the program multiplier exactly, so it tells
   whether the declared program matched. The band level is fitted to those hits, and does not follow the
   quality level, which an instrument fault lowers but the band does not.
5. **Instrument faults.** Weather lowers both the quality level and the band; a fault lowers only the
   quality. The agent reports when `E = quality / band` stays low, and uses the free false-report allowance before
   paid probes. Earthquakes need care: per the participant guide they lower instrument efficiency too, the loss
   fades night by night, and a report does not repair it. So the agent does not probe in the first 12 hours
   after an earthquake notice appears, and while the earthquake's effect may last it probes only on a new step
   down in E from the preceding hours. On 12 local cards this alone saved most of the free probes that used to
   go to earthquake drops (+0.9% in total).
6. **Hidden pointing offset (Hard-mode cards).** The participant guide says such cards add a fixed,
   unannounced offset to every pointing. The agent scores candidate offsets on a grid scaled to the fibre
   pitch (widening it if the best candidate sits on its edge), infers the offset from which assigned
   targets hit or missed, and commands `desired - offset`.
7. **Observation requests** get all-or-nothing value per remaining target (including targets already
   observed earlier: only exposures inside the request window count).
8. **Pace on the fair clock.** The platform charges normalized CPU time inside the agent's turns (waits
   are free) and caps real time per card. The agent measures its own CPU time per decision
   (`time.process_time`), compares it with `wallclock.remaining_real_cpu_seconds` spread over the decisions
   still to come, and also keeps the real-time cap (`wall_remaining_seconds`) in view; older runners that
   only send `remaining_seconds` are paced on real time.

## Configuration (.env)

```
OPENAI_API_KEY=sk-...                       # required (KIMI_API_KEY also accepted)
OPENAI_BASE_URL=https://api.kimi.com/coding/v1   # default; outside mainland China: https://api.kimi.ai/coding/v1
OPENAI_MODEL=k3                             # default
```

Without a key the agent exits at start-up with `missing API key: set OPENAI_API_KEY`, except under `OBSERVER_MODEL_DISABLED=1` (set by the platform for an evaluation started with “This evaluation without a model” / `survey26 eval start --no-model`): then it needs no key and runs on its rules only, so you can compare with and without an LLM. On the platform
`OPENAI_BASE_URL` / `OPENAI_API_KEY` are injected automatically. `k3` accepts only its default
temperature, so the client sends none.

## Running locally

```bash
python3 ../_local/runner/run_local.py --inherit-env --card ../_local/cards/L1 --agent "python3 agent.py" --agent-cwd .
python3 pack_agent.py --out ../python-pro-agent.zip
```

Every constant at the top of `planner.py` and `agent.py` can be overridden with `PRO_<NAME>` environment
variables (for example `PRO_LAMBDA_FRAC=0.5`). `PRO_FIXED_LEVEL=0` pins the search level, which makes
local comparisons reproducible on a busy machine (the platform run uses the adaptive pace).

## Where you can still beat it

- **Faster fault detection.** A fault can lower quality a lot, and each night it goes unreported can cost
  more than a paid probe. Deliberate diagnostic exposures could separate faults from unannounced weather.
- **Program choice under announced weather.** Mismatches cluster in hours with all-sky weather notices.
- **Season-level scheduling** of faint required targets on the best nights.
- **Partial exposures that get redone.** About a tenth of fibre-time goes to exposures that a later, longer
  exposure of the same target replaces. `PRO_PARTIAL_DISCOUNT<1` discounts partial exposures of targets a
  season plan expects to complete; it helped on the four practice cards and hurt on eight others, so it is off.
- **Better use of the model**, e.g. letting it read the whole forecast week and plan which nights to spend
  on which part of the sky.

## Public handover text

The agent retains `reason` from public observation requests received through stdin,
including active requests and newly issued messages. The nightly plan and fault
review receive the source text, site UTC offset and exact observing-night bounds;
paid report confirmation receives the same context. The first long handover is
retained for decoding conventions, alongside current requests and the latest
expired handover for continuity. No task-card files are read by the agent.

The night plan can return sourced `report_fault` onsets and `test_window` intervals.
The model copies source clock times and UTC offsets; Python converts them to UTC.
Source request/line references, explicit source dates and night bounds are validated
before use. The plan focuses on nearby dated lines and undated conventions/corrections;
fault review and paid confirmation receive the full selected handovers.
The deterministic scheduler avoids exposures across these boundaries, waits through
tests without treating them as faults, and reports confirmed camera-work events
without waiting for a low E signal. Each event is attempted at most once; false-report
limits, minimum report spacing and paid confirmation remain in effect. Model calls
still occur only at night start and before paid reports. A late or invalid reply
leaves the rule fallback in control until usable advice arrives; text first received
after the night's calls is retained for the next nightly stage.

This implements the input/action connection for fault and test handovers, not a
general interpreter of every weather/terrain instruction. Structured bulletin and
forecast weather handling remains unchanged. Line references establish source provenance;
they do not mechanically prove the model's interpretation or time conversion.

For `deepseek-flash` on the official `api.deepseek.com` endpoint, the client defaults
to disabled thinking for bounded advisory JSON: a 2000-token cap with thinking on
can leave no final JSON after reasoning. Explicitly enabling thinking instead uses
an 8192-token default budget and `reasoning_effort=low`. Other providers keep
their existing request shape. Optional overrides are `PRO_MODEL_THINKING`
(`auto`, `enabled`, `disabled`), `PRO_MODEL_REASONING_EFFORT`, and
`PRO_MODEL_MAX_TOKENS`. Truncated completions are logged as `CompletionTruncated`.
See the [DeepSeek thinking-mode documentation](https://api-docs.deepseek.com/guides/thinking_mode/).

Run the focused regression suite with `python -m unittest discover -s tests -v`.

## Model cost controls

The handover parser, nightly fault review, and paid-report confirmation remain in place; the observe planner and fault thresholds are unchanged.
Only when there is no public request text is the night weather calculated directly using the exact structured rules from the prompt.
Unknown short and encoded texts still go to the model. Unicode is sent directly, and reusable source text comes before changing clocks/tables for provider prefix caching.
Original source lines and candidate quotes are retained. Only identical stage/system/input requests reuse replies; changed dates or quality evidence require a new request.

`PRO_MODEL_MAX_RETRIES=2` retries only network errors and transient HTTP errors. HTTP 401/402 disables new requests for this run.
Delivered invalid/truncated replies are not blindly retried at additional cost. Failures keep the original rule fallback.
`PRO_MODEL_MAX_CALLS` counts actual HTTP attempts per card, including retries; `PRO_MODEL_CACHE_SIZE` defaults to 128.
`PRO_MODEL_STRUCTURED_WEATHER=0` restores model weather calls even without handover text. See `.env.example` for overrides.

`llm: usage` stderr records input, cache hits, output, reasoning, and cumulative estimated charges, including late replies that the loop does not collect.
Official Flash estimates use conservative peak prices and are not invoices. Missing usage is marked unknown; reasoning tokens are already included in output charges.
Network errors with no usage can incur unmeasured provider charges. Prefix cache hits are not guaranteed. See [official pricing](https://api-docs.deepseek.com/quick_start/pricing/).

On 2026-10-07, a controlled three-night public A1 handover probe (six requests per version, same account, thinking disabled) passed all fault times and three test windows per night.
Input fell from 40,708 to 17,014 tokens. Off-peak usage-based estimates fell from CNY 0.04309584 to 0.01160728 (about 73%).
Cache warmth and output randomness affect this sample; it is not a full eight-card invoice or a platform score guarantee.
The rules-only L1 replay at `PRO_FIXED_LEVEL=2` produced 900 byte-identical actions to the baseline; full model-mode platform scores remain unmeasured.

## License

Task cards, simulated data, evaluation code and the example projects are licensed under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/); please cite the GOSIM 2026 Agentic
Observer Hackathon (https://create.gosim.org/survey26/). See `../LICENSE.md`.
