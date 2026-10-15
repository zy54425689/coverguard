# CoverGuard — Parametric Insurance Adjudicated On-chain

> Parametric cover with **no trusted oracle**. The policy names a data-source URL and a trigger
> condition in plain language; when a claim is judged the contract fetches that page **on-chain**
> and an LLM jury decides whether the trigger fired — then real GEN moves to the right party.

**Category:** Parametric Insurance / Prediction Markets · Agentic Commerce

---

## Why GenLayer

Parametric insurance lives or dies on one question: *did the hazard actually occur?* Traditional
designs answer it with a trusted data oracle — a single point of failure and a single point of
trust. CoverGuard answers it with **validated LLM consensus**:

- the trigger is written in natural language by the parties ("the page reports rainfall above 50mm");
- at judging time the contract reads the named source with `gl.nondet.web.get`;
- an LLM jury (`gl.nondet.exec_prompt`) rules `triggered: true|false` with a cited justification;
- validators independently re-run the same reasoning through `gl.vm.run_nondet_unsafe` and must
  agree on the verdict before it is written to state.

Nobody has to trust the underwriter, the holder, or an oracle — the record is adjudicated.

---

## Money flows in both directions

```
holder  --premium(payable)-->  [ CoverGuard ]  <--collateral(payable)--  underwriter
                                     |
                       judge_claim() reads the source on-chain
                                     |
              triggered?  yes --> holder receives collateral + premium
                          no  --> underwriter receives premium + collateral back
```

The premium and the collateral are **custody, not metadata**: both are recorded from
`gl.message.value` and both leave the contract through `emit_transfer`.

| Account | Amount | Recorded as |
|---|---|---|
| holder | `create_policy` is `payable` | `premium` |
| underwriter | `underwrite_policy` is `payable`, must exceed the premium | `coverage` |
| contract | `get_contract_balance()` | live custody of both |

---

## Lifecycle

```
AWAITING UNDERWRITER --underwrite--> ACTIVE --judge_claim--> JUDGED --settle--> SETTLED
        |
        └--refund_unaccepted (holder)--> CANCELLED
```

---

## Contract: `contracts/cover_guard.py`

| Method | Type | Description |
|---|---|---|
| `create_policy(underwriter, hazard_url, condition)` | **payable** write | Pays the premium, opens the policy |
| `underwrite_policy(policy_id)` | **payable** write | Named underwriter locks collateral (> premium); activates |
| `refund_unaccepted(policy_id)` | write | Holder reclaims the premium if nobody underwrote |
| `judge_claim(policy_id)` | write | Fetches the source on-chain, jury rules if the hazard occurred |
| `settle(policy_id)` | write | **Pays out** per the verdict |
| `get_policy(id)` / `get_policy_count()` / `get_contract_balance()` | view | State + live custody |

### Judging

```python
def leader_fn():
    page = _fetch_text(url_mem)            # gl.nondet.web.get, read on-chain
    ...
    return gl.nondet.exec_prompt(prompt, response_format='json')

# validator_fn re-runs leader_fn() and must agree on the boolean `triggered`
result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
```

Conservative by construction: if the source is unreachable or does not contain the data the
condition refers to, the trigger has **not** occurred (the prompt says so explicitly, and a test
covers it).

### Payout

```python
@gl.evm.contract_interface
class _Payee:
    class View: pass
    class Write: pass

def _pay(self, to, amount):
    _Payee(to).emit_transfer(value=amount)   # external message → real GEN out

# settle(): if p.triggered -> pay holder (coverage + premium)
#            else           -> pay underwriter (premium + collateral)
```

---

## Frontend: `index.html`

Single-file, build-free (GenLayerJS from esm.sh), GitHub Pages ready. Third house style in this
series (deep teal/mint): live **"funds under custody"** panel, a policy ledger, and per-policy
actions — pay premium, lock collateral, refund, **judge the claim**, settle. The judge/settle
buttons enable only when the policy is in the right state.

---

## Local tests (12 passing)

```bash
pip install genlayer-test pytest
pytest tests/direct/ -v
```

Covers: premium required to open, premium recorded as custody, holder≠underwriter, URL/condition
validation, underwriter-only underwriting, collateral must exceed premium, holder refund while
unaccepted, **both payout branches** (trigger fires → holder, no trigger → underwriter), the
conservative "unreachable source means no trigger" case, and the settle/underwrite guards.

---

## Deploy

```bash
npm install -g genlayer
genlayer network set testnet-bradbury
genlayer deploy --contract contracts/cover_guard.py
genlayer call <CONTRACT> get_contract_balance
```

> **Header rule:** line 1 must be the runner directive
> `# { "Depends": "py-genlayer:<hash>" }` — `test`/`latest` are rejected outside debug mode.

---

## Verified end-to-end on Bradbury

| Step | Result |
|---|---|
| `create_policy` payable (0.1 GEN premium) | contract balance rises by the premium |
| `underwrite_policy` payable (0.3 GEN collateral) | contract holds premium + collateral |
| `judge_claim` | ACCEPTED · jury read `example.com` on-chain and ruled on the condition |
| `settle` | ACCEPTED · payout emitted to the entitled party |
| final state | `SETTLED`, `paid = true`, premium and coverage both zeroed |

```
coverguard/
├── contracts/cover_guard.py          the Intelligent Contract
├── tests/direct/test_cover_guard.py  12 direct-mode tests
├── index.html                        build-free frontend (GitHub Pages ready)
├── genlayer.config.yaml
└── README.md
```

Built on [GenLayer](https://docs.genlayer.com) Intelligent Contracts.
