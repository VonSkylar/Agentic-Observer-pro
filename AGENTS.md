# AGENTS.md -- python-pro

Guide for AI coding assistants working in this project. Humans: see README.md / README.zh.md.

## What this is

A strong, standard-library-only Python agent for the GOSIM survey26 telescope-survey challenge
(`participant-agent-protocol-v4`). A deterministic planner makes every observe decision; a model
(default Kimi Coding Plan `k3`) is called twice at the start of every night and once before any paid
fault report. Read `agent.py` first: it is the stdin/stdout loop and owns pacing, fault reporting and
the model stages.

## Module map

```
agent.py          entry point: protocol loop, pacing, instrument-fault reporting, model stages wiring
planner.py        one search for pointing + fibres + duration + program; learning from results
skymath.py        public sky maths: sidereal time, alt/az, gnomonic projection, fibre grid, Moon
advisor.py        the model stages: night plan, fault review, paid-report confirmation (prompts + validation)
llm_client.py     OpenAI-compatible chat client on background threads (Kimi Coding Plan defaults)
observer.project.json, pack_agent.py, .env.example
```

## Rules for changes

- Never read card files at run time. All information comes from stdin (catalogue, public score config,
  bulletins, forecasts, your own hits).
- Never call the model per decision. The night stages run in the background; `agent._model_wait_budget`
  decides how long a night start may wait for them.
- Every tunable constant can be overridden with a `PRO_<NAME>` environment variable for sweeps
  (`PRO_FIXED_LEVEL=0` pins the search level, useful for reproducible local comparisons).
- Without `OPENAI_API_KEY` (or `KIMI_API_KEY`) the agent exits at start-up with
  `missing API key: set OPENAI_API_KEY`.
