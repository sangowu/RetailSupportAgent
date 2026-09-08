# Demo run archive

**Authoritative record: `20260906T132442Z/`** - the four flows (lookup / cancel / return /
refusal of an out-of-scope request) run end to end on the final code.

The other directories are intermediate runs from the implementation. The code has changed
since, so **do not cite their conclusions**. They are kept because they record the actual
occurrence of the problems described in section 6 of `docs/M0.3_BUSINESS_LOOP.md`:

| Directory | State at the time |
|---|---|
| `20260906T131912Z` | Only the lookup flow, verifying the wiring works |
| `20260906T132119Z` | Exposed the double confirmation; the pydantic serialization bug was fixed but the flow did not finish |
| `20260906T132208Z` | Attempted to suppress the agent's own confirmation with a prompt, which conflicts with the upstream policy text |
| `20260906T132251Z` | The model insisted on the tau2-bench identification protocol instead, leading to the demo-identity / evaluation-protocol boundary |
| `20260906T132334Z` | First complete cancellation run, with the database fingerprint confirming the write landed |
